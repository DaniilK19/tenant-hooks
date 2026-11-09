"""Tenant A must never see or change tenant B's rows.

The cross-tenant SELECT/UPDATE/DELETE tests run as hooks_owner on purpose. It holds
every table privilege, so a zero-row result can only come from RLS, not from a
missing GRANT. The hooks_api tests then cover the role the service really uses.
"""

from uuid import UUID

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from tenant_hooks.db import tenant_tx
from tenant_hooks.models import Event, Tenant
from tests.conftest import PROVIDER, TwoTenants

# Table -> (tenant column, a column that is safe to assign to itself in an UPDATE).
TABLES = {
    "tenants": ("id", "name"),
    "endpoints": ("tenant_id", "provider"),
    "signing_secrets": ("tenant_id", "ciphertext"),
    "api_keys": ("tenant_id", "name"),
    "event_handlers": ("tenant_id", "handler_name"),
    "seen_deliveries": ("tenant_id", "delivery_id"),
    "events": ("tenant_id", "status"),
    "event_attempts": ("tenant_id", "worker_id"),
}
# Deletes run child-first, so a surviving row cannot be blamed on a foreign key.
DELETE_ORDER = [
    "event_attempts",
    "events",
    "seen_deliveries",
    "event_handlers",
    "api_keys",
    "signing_secrets",
    "endpoints",
    "tenants",
]


def count_for_tenant(engine: Engine, table: str, tenant_id: UUID) -> int:
    column = TABLES[table][0]
    with tenant_tx(engine, tenant_id) as session:
        count = session.execute(
            text(f"SELECT count(*) FROM {table} WHERE {column} = :tenant"), {"tenant": tenant_id}
        ).scalar_one()
    return int(count)


@pytest.mark.parametrize("table", TABLES)
def test_select_with_explicit_other_tenant_filter_returns_nothing(
    owner_engine: Engine, tenants: TwoTenants, table: str
) -> None:
    column = TABLES[table][0]
    with tenant_tx(owner_engine, tenants.a.id) as session:
        rows = session.execute(
            text(f"SELECT * FROM {table} WHERE {column} = :other"), {"other": tenants.b.id}
        ).all()
    assert rows == []


@pytest.mark.parametrize("table", TABLES)
def test_update_cannot_touch_other_tenant(
    owner_engine: Engine, tenants: TwoTenants, table: str
) -> None:
    column, assignable = TABLES[table]
    with tenant_tx(owner_engine, tenants.a.id) as session:
        result = session.execute(
            text(f"UPDATE {table} SET {assignable} = {assignable} WHERE {column} = :other"),
            {"other": tenants.b.id},
        )
    assert result.rowcount == 0  # type: ignore[attr-defined]


def test_delete_cannot_touch_other_tenant(owner_engine: Engine, tenants: TwoTenants) -> None:
    with tenant_tx(owner_engine, tenants.a.id) as session:
        for table in DELETE_ORDER:
            session.execute(text(f"DELETE FROM {table}"))

    for table in DELETE_ORDER:
        assert count_for_tenant(owner_engine, table, tenants.a.id) == 0, table
        assert count_for_tenant(owner_engine, table, tenants.b.id) == 1, table


def test_api_role_cannot_read_other_tenant_event_by_id(
    api_engine: Engine, tenants: TwoTenants
) -> None:
    with tenant_tx(api_engine, tenants.a.id) as session:
        assert session.get(Event, tenants.b.event_id) is None


def test_api_role_update_of_other_tenant_changes_nothing(
    api_engine: Engine, tenants: TwoTenants
) -> None:
    with tenant_tx(api_engine, tenants.a.id) as session:
        result = session.execute(
            text("UPDATE events SET last_error = 'hacked' WHERE id = :id"),
            {"id": tenants.b.event_id},
        )
    assert result.rowcount == 0  # type: ignore[attr-defined]

    with tenant_tx(api_engine, tenants.b.id) as session:
        event = session.get(Event, tenants.b.event_id)
    assert event is not None
    assert event.last_error is None


def test_api_role_cannot_insert_for_other_tenant(api_engine: Engine, tenants: TwoTenants) -> None:
    with pytest.raises(ProgrammingError, match="row-level security"):
        with tenant_tx(api_engine, tenants.a.id) as session:
            session.add(
                Event(
                    tenant_id=tenants.b.id,
                    provider=PROVIDER,
                    provider_event_id="evt_injected",
                    event_type="order.created",
                    raw_body=b"{}",
                )
            )


def test_api_role_cannot_move_own_row_to_other_tenant(
    api_engine: Engine, tenants: TwoTenants
) -> None:
    with pytest.raises(ProgrammingError, match="row-level security"):
        with tenant_tx(api_engine, tenants.a.id) as session:
            session.execute(
                text("UPDATE events SET tenant_id = :other WHERE id = :id"),
                {"other": tenants.b.id, "id": tenants.a.event_id},
            )


def test_worker_role_is_isolated_too(worker_engine: Engine, tenants: TwoTenants) -> None:
    with tenant_tx(worker_engine, tenants.a.id) as session:
        event_ids = session.scalars(select(Event.id)).all()
    assert event_ids == [tenants.a.event_id]


def test_query_that_forgets_tenant_filter_returns_only_current_tenant(
    api_engine: Engine, tenants: TwoTenants
) -> None:
    with tenant_tx(api_engine, tenants.a.id) as session:
        events = session.scalars(select(Event)).all()
        tenant_rows = session.scalars(select(Tenant)).all()
    assert [event.id for event in events] == [tenants.a.event_id]
    assert [tenant.id for tenant in tenant_rows] == [tenants.a.id]


@pytest.mark.parametrize(
    "table", ["tenants", "endpoints", "signing_secrets", "api_keys", "event_handlers"]
)
def test_no_tenant_set_returns_nothing(api_engine: Engine, tenants: TwoTenants, table: str) -> None:
    with api_engine.connect() as connection:
        rows = connection.execute(text(f"SELECT * FROM {table}")).all()
    assert rows == []


def test_no_tenant_set_cannot_read_events(api_engine: Engine, tenants: TwoTenants) -> None:
    with api_engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM events")).all() == []


def test_composite_foreign_key_rejects_cross_tenant_reference(
    owner_engine: Engine, tenants: TwoTenants
) -> None:
    """Foreign key checks bypass RLS, so the key itself must include the tenant."""
    with pytest.raises(IntegrityError, match="fk_event_attempts_event"):
        with tenant_tx(owner_engine, tenants.a.id) as session:
            session.execute(
                text(
                    "INSERT INTO event_attempts "
                    "(tenant_id, lease_token, event_id, attempt_no, worker_id) "
                    "VALUES (:tenant, gen_random_uuid(), :other_event, 1, 'worker-1')"
                ),
                {"tenant": tenants.a.id, "other_event": tenants.b.event_id},
            )


def test_same_provider_event_id_is_independent_per_tenant(
    api_engine: Engine, tenants: TwoTenants
) -> None:
    """Both fixture tenants already hold evt_1, so uniqueness cannot reveal the other's data."""
    for tenant in (tenants.a, tenants.b):
        with tenant_tx(api_engine, tenant.id) as session:
            ids = session.scalars(
                select(Event.provider_event_id).where(Event.provider == PROVIDER)
            ).all()
        assert ids == ["evt_1"]
