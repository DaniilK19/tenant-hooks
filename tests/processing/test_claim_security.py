"""The cross-tenant functions are the one hole in RLS, so their limits are tested directly."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import ProgrammingError

from tenant_hooks.db import tenant_tx
from tenant_hooks.models import SeenDelivery
from tenant_hooks.worker.claim import claim_events, purge_expired_deliveries
from tests.conftest import PROVIDER
from tests.intake.helpers import SigningTenant
from tests.processing.helpers import add_event

FUNCTIONS = {
    "claim_events": "SELECT * FROM claim_events(1, 60, 'x')",
    "purge_expired_deliveries": "SELECT purge_expired_deliveries()",
    "reclaim_expired_leases": "SELECT reclaim_expired_leases(1)",
}


@pytest.mark.parametrize("statement", FUNCTIONS.values())
def test_api_role_cannot_execute_claim_functions(api_engine: Engine, statement: str) -> None:
    with pytest.raises(ProgrammingError, match="permission denied for function"):
        with api_engine.begin() as connection:
            connection.execute(text(statement))


@pytest.mark.parametrize("name", FUNCTIONS)
def test_function_is_definer_owned_by_claimer_with_pinned_search_path(
    owner_engine: Engine, name: str
) -> None:
    with owner_engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT prosecdef, pg_get_userbyid(proowner) AS owner, proconfig,
                       has_function_privilege('public', oid, 'EXECUTE') AS public_can_execute
                FROM pg_proc WHERE proname = :name
                """
            ),
            {"name": name},
        ).one()
    assert row.prosecdef is True
    assert row.owner == "hooks_claimer"
    assert row.proconfig == ["search_path=pg_catalog, public"]
    assert row.public_can_execute is False


def test_claimer_cannot_create_objects_after_migration(owner_engine: Engine) -> None:
    with owner_engine.connect() as connection:
        allowed = connection.execute(
            text("SELECT has_schema_privilege('hooks_claimer', 'public', 'CREATE')")
        ).scalar_one()
    assert allowed is False


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT raw_body FROM events",
        "SELECT * FROM events",
        "SELECT * FROM signing_secrets",
        "SELECT * FROM api_keys",
        "DELETE FROM events",
        "UPDATE events SET raw_body = ''",
    ],
)
def test_claimer_role_cannot_reach_payloads_or_secrets(
    owner_engine: Engine, statement: str
) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        with owner_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE hooks_claimer"))
            connection.execute(text(statement))


def test_claim_returns_identifiers_only_and_spans_tenants(
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    other_tenant: SigningTenant,
) -> None:
    first = add_event(owner_engine, tenant.id)
    second = add_event(owner_engine, other_tenant.id)

    with worker_engine.begin() as connection:
        result = connection.execute(text("SELECT * FROM claim_events(10, 60, 'w1')"))
        columns = list(result.keys())
        rows = result.all()

    assert columns == ["event_id", "tenant_id", "lease_token", "attempt_no"]
    assert {(row.event_id, row.tenant_id) for row in rows} == {
        (first, tenant.id),
        (second, other_tenant.id),
    }


def test_claim_respects_batch_size(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    for _ in range(3):
        add_event(owner_engine, tenant.id)

    assert len(claim_events(worker_engine, 2, 60, "w1")) == 2
    assert len(claim_events(worker_engine, 2, 60, "w1")) == 1
    assert claim_events(worker_engine, 2, 60, "w1") == []


def test_purge_removes_only_expired_deliveries_across_tenants(
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    other_tenant: SigningTenant,
) -> None:
    now = datetime.now(UTC)
    for current in (tenant, other_tenant):
        with tenant_tx(owner_engine, current.id) as session:
            session.add_all(
                SeenDelivery(
                    tenant_id=current.id,
                    provider=PROVIDER,
                    delivery_id=delivery_id,
                    expires_at=now + offset,
                )
                for delivery_id, offset in (
                    ("expired", timedelta(hours=-1)),
                    ("live", timedelta(hours=1)),
                )
            )

    assert purge_expired_deliveries(worker_engine) == 2

    for current in (tenant, other_tenant):
        with tenant_tx(owner_engine, current.id) as session:
            remaining = session.scalars(select(SeenDelivery.delivery_id)).all()
            assert remaining == ["live"]
            assert session.scalar(select(func.count()).select_from(SeenDelivery)) == 1
