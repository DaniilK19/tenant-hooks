"""The tenant must never follow a connection back into the pool."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import cycle, islice
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, select, text

from tenant_hooks.db import make_engine, tenant_tx
from tenant_hooks.models import Event
from tests.conftest import DatabaseUrls, TwoTenants


@pytest.fixture
def single_connection_engine(database_urls: DatabaseUrls) -> Iterator[Engine]:
    """A pool of one, so every checkout is guaranteed to reuse the same backend."""
    engine = make_engine(database_urls.api, pool_size=1)
    yield engine
    engine.dispose()


def backend_pid(connection: Connection) -> int:
    return int(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())


def assert_no_tenant_visible(connection: Connection) -> None:
    setting = connection.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar()
    assert setting in (None, "")
    assert connection.execute(text("SELECT count(*) FROM events")).scalar_one() == 0


def test_no_leak_after_commit(single_connection_engine: Engine, tenants: TwoTenants) -> None:
    with tenant_tx(single_connection_engine, tenants.a.id) as session:
        first_pid = int(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
        assert session.execute(text("SELECT count(*) FROM events")).scalar_one() == 1

    with single_connection_engine.connect() as connection:
        assert backend_pid(connection) == first_pid
        assert_no_tenant_visible(connection)


def test_no_leak_after_rollback(single_connection_engine: Engine, tenants: TwoTenants) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with tenant_tx(single_connection_engine, tenants.a.id) as session:
            first_pid = int(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            raise RuntimeError("boom")

    with single_connection_engine.connect() as connection:
        assert backend_pid(connection) == first_pid
        assert_no_tenant_visible(connection)


def test_next_tenant_on_same_connection_sees_only_its_rows(
    single_connection_engine: Engine, tenants: TwoTenants
) -> None:
    with tenant_tx(single_connection_engine, tenants.a.id) as session:
        first_pid = int(session.execute(text("SELECT pg_backend_pid()")).scalar_one())

    with tenant_tx(single_connection_engine, tenants.b.id) as session:
        assert int(session.execute(text("SELECT pg_backend_pid()")).scalar_one()) == first_pid
        event_ids = session.scalars(select(Event.id)).all()
    assert event_ids == [tenants.b.event_id]


def test_concurrent_tenants_on_small_pool_never_mix(
    database_urls: DatabaseUrls, tenants: TwoTenants
) -> None:
    engine = make_engine(database_urls.api, pool_size=3)

    def check(tenant_and_event: tuple[UUID, UUID]) -> int:
        tenant_id, event_id = tenant_and_event
        with tenant_tx(engine, tenant_id) as session:
            # Hold the transaction so that the threads really do interleave.
            session.execute(text("SELECT pg_sleep(0.002)"))
            pid = int(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            assert session.scalars(select(Event.id)).all() == [event_id]
        return pid

    pairs = [(tenants.a.id, tenants.a.event_id), (tenants.b.id, tenants.b.event_id)]
    try:
        with ThreadPoolExecutor(max_workers=12) as executor:
            pids = list(executor.map(check, islice(cycle(pairs), 400)))
    finally:
        engine.dispose()

    # More than one backend, and fewer than one per call, proves connections were shared.
    assert 1 < len(set(pids)) <= 3


def test_session_level_setting_would_leak(
    single_connection_engine: Engine, tenants: TwoTenants
) -> None:
    """Negative control: shows the checks above can detect a leak, and why SET LOCAL is used."""
    with single_connection_engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant, false)"),
            {"tenant": str(tenants.a.id)},
        )

    with single_connection_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM events")).scalar_one() == 1
