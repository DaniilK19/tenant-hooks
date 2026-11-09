"""Competing workers on separate connections, against real row locks."""

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import Engine, select, text

from tenant_hooks.db import tenant_tx
from tenant_hooks.handlers.registry import HandlerContext
from tenant_hooks.models import Event
from tenant_hooks.worker.claim import claim_events
from tenant_hooks.worker.loop import Worker
from tests.intake.helpers import SigningTenant, stored_events
from tests.processing.helpers import (
    Recorder,
    add_event,
    add_events,
    bind_handler,
    expire_leases,
    registry_with,
)


def drain_with_competing_workers(engine: Engine, count: int, handler: Recorder) -> list[int]:
    """Run workers side by side until the queue is empty. Returns each worker's claim count."""
    start_line = Barrier(count)

    def slow_handler(context: HandlerContext) -> None:
        handler(context)
        time.sleep(0.001)  # Gives the other workers a chance to interleave.

    def run_worker(index: int) -> int:
        worker = Worker(engine, registry_with(slow_handler), worker_id=f"worker-{index}")
        start_line.wait()
        claimed = 0
        while worker.run_once():
            claimed += 1
        return claimed

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run_worker, range(count)))


def assert_each_event_handled_exactly_once(handler: Recorder, event_ids: list[UUID]) -> None:
    handled = Counter(context.event_id for context in handler.calls)
    assert set(handled) == set(event_ids)
    assert set(handled.values()) == {1}


@pytest.mark.parametrize(("workers", "events_per_tenant"), [(2, 100), (8, 250)])
def test_competing_workers_never_process_the_same_event_twice(
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    other_tenant: SigningTenant,
    workers: int,
    events_per_tenant: int,
) -> None:
    event_ids: list[UUID] = []
    for current in (tenant, other_tenant):
        bind_handler(owner_engine, current.id)
        event_ids += add_events(owner_engine, current.id, events_per_tenant)
    handler = Recorder()

    claimed = drain_with_competing_workers(worker_engine, workers, handler)

    assert_each_event_handled_exactly_once(handler, event_ids)
    assert sum(claimed) == len(event_ids)
    assert sum(1 for count in claimed if count > 0) > 1, "the workers never actually competed"
    for current in (tenant, other_tenant):
        events = stored_events(owner_engine, current.id)
        assert {event.status for event in events} == {"succeeded"}
        assert {event.attempt_count for event in events} == {1}


def test_competing_workers_reclaim_each_expired_lease_once(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_ids = add_events(owner_engine, tenant.id, 200)
    assert len(claim_events(worker_engine, 200, 60, "dead-worker")) == 200
    expire_leases(owner_engine, tenant.id)
    handler = Recorder()

    drain_with_competing_workers(worker_engine, 8, handler)

    assert_each_event_handled_exactly_once(handler, event_ids)
    events = stored_events(owner_engine, tenant.id)
    assert {event.status for event in events} == {"succeeded"}
    assert {event.attempt_count for event in events} == {2}


def test_claim_skips_a_locked_row_instead_of_waiting_for_it(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    locked = add_event(owner_engine, tenant.id)
    free = add_event(owner_engine, tenant.id)

    with tenant_tx(owner_engine, tenant.id) as session:
        session.execute(select(Event.id).where(Event.id == locked).with_for_update())
        with worker_engine.begin() as connection:
            # Without SKIP LOCKED this statement would block, and then fail here.
            connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            rows = connection.execute(text("SELECT * FROM claim_events(10, 60, 'w1')")).all()

    assert [row.event_id for row in rows] == [free]


def test_reclaim_skips_a_row_being_finished_by_its_worker(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    """A worker finishing at the moment its lease expires holds the row lock. It must win."""
    event_id = add_event(owner_engine, tenant.id)
    claim_events(worker_engine, 1, 60, "w1")
    expire_leases(owner_engine, tenant.id)

    with tenant_tx(owner_engine, tenant.id) as session:
        session.execute(select(Event.id).where(Event.id == event_id).with_for_update())
        with worker_engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            reclaimed = connection.execute(text("SELECT reclaim_expired_leases(10)")).scalar_one()

    assert reclaimed == 0
