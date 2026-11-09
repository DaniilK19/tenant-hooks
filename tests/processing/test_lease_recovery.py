"""What happens when a worker dies, stalls, or comes back after losing its lease."""

import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from tenant_hooks.db import tenant_tx
from tenant_hooks.handlers.registry import HandlerContext
from tenant_hooks.models import SeenDelivery
from tenant_hooks.worker import loop
from tenant_hooks.worker.claim import Claim, claim_events, reclaim_expired_leases
from tenant_hooks.worker.loop import Worker
from tests.conftest import PROVIDER, REPO_ROOT, DatabaseUrls
from tests.intake.helpers import SigningTenant
from tests.processing import crash_worker
from tests.processing.file_effects import FileEffects
from tests.processing.helpers import (
    Recorder,
    add_api_key,
    add_event,
    add_events,
    bearer,
    bind_handler,
    expire_leases,
    get_attempts,
    get_event,
    registry_with,
)

CRASH_LEASE_SECONDS = 4
EXPIRED_MESSAGE = "lease expired before the worker reported a result"


def test_lease_duration_is_measured_on_the_database_clock(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    event_id = add_event(owner_engine, tenant.id)

    claim_events(worker_engine, 1, 60, "w1")

    event = get_event(owner_engine, tenant.id, event_id)
    [attempt] = get_attempts(owner_engine, tenant.id, event_id)
    assert event.lease_expires_at == attempt.started_at + timedelta(seconds=60)


def test_live_lease_is_left_alone(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    [claim] = claim_events(worker_engine, 1, 60, "w1")
    handler = Recorder()

    assert Worker(worker_engine, registry_with(handler)).run_once() == 0

    assert handler.calls == []
    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "processing"
    assert event.lease_token == claim.lease_token


def test_expired_lease_is_reclaimed_and_completed_with_a_new_token(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    [first_claim] = claim_events(worker_engine, 1, 60, "dead-worker")
    expire_leases(owner_engine, tenant.id)
    handler = Recorder()

    assert Worker(worker_engine, registry_with(handler), worker_id="w2").run_once() == 1

    assert [context.attempt for context in handler.calls] == [2]
    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "succeeded"
    assert event.attempt_count == 2
    assert event.last_error is None

    attempts = get_attempts(owner_engine, tenant.id, event_id)
    assert [attempt.worker_id for attempt in attempts] == ["dead-worker", "w2"]
    assert [attempt.outcome for attempt in attempts] == [None, "succeeded"]
    assert attempts[0].lease_token == first_claim.lease_token
    assert attempts[1].lease_token != first_claim.lease_token


def test_reclaim_spans_tenants_and_reports_how_many_it_moved(
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    other_tenant: SigningTenant,
) -> None:
    first = add_event(owner_engine, tenant.id)
    second = add_event(owner_engine, other_tenant.id)
    claim_events(worker_engine, 2, 60, "dead-worker")
    expire_leases(owner_engine, tenant.id)
    expire_leases(owner_engine, other_tenant.id)

    assert reclaim_expired_leases(worker_engine, 10) == 2

    for tenant_id, event_id in ((tenant.id, first), (other_tenant.id, second)):
        event = get_event(owner_engine, tenant_id, event_id)
        assert event.status == "pending"
        assert event.lease_token is None
        assert event.last_error == EXPIRED_MESSAGE


def test_reclaim_respects_its_limit(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    add_events(owner_engine, tenant.id, 3)
    claim_events(worker_engine, 3, 60, "dead-worker")
    expire_leases(owner_engine, tenant.id)

    assert reclaim_expired_leases(worker_engine, 2) == 2
    assert reclaim_expired_leases(worker_engine, 2) == 1
    assert reclaim_expired_leases(worker_engine, 2) == 0


def wait_until_handler_started(
    effects: FileEffects, process: subprocess.Popen[str], timeout_seconds: float = 30
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not effects.has_started(1):
        if process.poll() is not None:
            _, stderr = process.communicate()
            pytest.fail(f"crash worker exited early: {stderr}")
        if time.monotonic() > deadline:
            pytest.fail("crash worker never started its handler")
        time.sleep(0.02)


def test_worker_killed_mid_handler_is_recovered_and_its_effect_applies_once(
    database_urls: DatabaseUrls,
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    tmp_path: Path,
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    effects = FileEffects(tmp_path)
    command = [
        sys.executable,
        "-m",
        "tests.processing.crash_worker",
        database_urls.worker,
        str(tmp_path),
        str(CRASH_LEASE_SECONDS),
    ]

    with subprocess.Popen(command, cwd=REPO_ROOT, stderr=subprocess.PIPE, text=True) as process:
        try:
            wait_until_handler_started(effects, process)
        finally:
            process.kill()  # SIGKILL or TerminateProcess: no cleanup code gets to run.
    assert process.returncode != 0

    abandoned = get_event(owner_engine, tenant.id, event_id)
    assert abandoned.status == "processing"
    assert abandoned.leased_by == crash_worker.WORKER_ID
    assert abandoned.attempt_count == 1

    second_applications: list[bool] = []

    def apply_effect(context: HandlerContext) -> None:
        assert context.attempt == 2
        second_applications.append(effects.apply(context.idempotency_key))

    rescuer = Worker(
        worker_engine, registry_with(apply_effect, crash_worker.HANDLER_NAME), worker_id="rescuer"
    )
    assert rescuer.run_once() == 0, "the lease was reclaimed before it expired"

    deadline = time.monotonic() + CRASH_LEASE_SECONDS + 15
    while rescuer.run_once() == 0:
        assert time.monotonic() < deadline, "the event was never reclaimed"
        time.sleep(0.1)

    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "succeeded"
    assert event.attempt_count == 2
    attempts = get_attempts(owner_engine, tenant.id, event_id)
    assert [attempt.worker_id for attempt in attempts] == [crash_worker.WORKER_ID, "rescuer"]
    assert [attempt.outcome for attempt in attempts] == [None, "succeeded"]

    # The handler started twice, which is at-least-once. The idempotency key made the
    # second run a no-op, so the side effect itself happened once.
    assert effects.path(str(event_id)).exists()
    assert second_applications == [False]


def crash_the_worker(context: HandlerContext) -> None:
    raise SystemExit(1)


def test_event_that_keeps_killing_workers_is_dead_lettered(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    """Attempts are committed at claim time, so even a crash uses one up."""
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id, max_attempts=2)
    calls = Recorder()

    def poison(context: HandlerContext) -> None:
        calls(context)
        crash_the_worker(context)

    worker = Worker(worker_engine, registry_with(poison))
    for attempt in (1, 2):
        with pytest.raises(SystemExit):
            worker.run_once()
        assert get_event(owner_engine, tenant.id, event_id).attempt_count == attempt
        expire_leases(owner_engine, tenant.id)

    assert worker.run_once() == 0

    assert len(calls.calls) == 2
    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "dead_lettered"
    assert event.last_error == EXPIRED_MESSAGE
    assert event.lease_token is None
    assert event.completed_at is not None


def test_event_dead_lettered_by_lease_expiry_can_be_replayed(
    client: TestClient, owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id, max_attempts=1)
    claim_events(worker_engine, 1, 60, "dead-worker")
    expire_leases(owner_engine, tenant.id)
    assert reclaim_expired_leases(worker_engine, 10) == 1
    token = add_api_key(owner_engine, tenant.id)

    response = client.post(f"/v1/events/{event_id}/replay", headers=bearer(token))

    assert response.status_code == 202
    handler = Recorder()
    assert Worker(worker_engine, registry_with(handler)).run_once() == 1
    assert len(handler.calls) == 1
    assert get_event(owner_engine, tenant.id, event_id).status == "succeeded"


@pytest.mark.parametrize("zombie_fails", [False, True], ids=["late_success", "late_failure"])
def test_late_result_from_a_zombie_worker_is_discarded(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant, zombie_fails: bool
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    rescuer = Worker(worker_engine, registry_with(Recorder()), worker_id="rescuer")

    def stall_past_the_lease(context: HandlerContext) -> None:
        expire_leases(owner_engine, tenant.id)
        assert rescuer.run_once() == 1
        if zombie_fails:
            raise RuntimeError("late failure")

    Worker(worker_engine, registry_with(stall_past_the_lease), worker_id="zombie").run_once()

    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "succeeded"
    assert event.attempt_count == 2
    assert event.last_error is None
    outcomes = {
        attempt.worker_id: attempt.outcome
        for attempt in get_attempts(owner_engine, tenant.id, event_id)
    }
    assert outcomes == {"zombie": "lease_lost", "rescuer": "succeeded"}


def test_zombie_cannot_disturb_the_worker_that_took_over(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    takeover: list[Claim] = []

    def lose_the_lease(context: HandlerContext) -> None:
        expire_leases(owner_engine, tenant.id)
        reclaim_expired_leases(worker_engine, 10)
        takeover.extend(claim_events(worker_engine, 1, 60, "rescuer"))

    Worker(worker_engine, registry_with(lose_the_lease), worker_id="zombie").run_once()

    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "processing"
    assert event.lease_token == takeover[0].lease_token
    assert event.leased_by == "rescuer"


def test_event_whose_lease_is_lost_before_it_starts_is_not_handled(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    """In a batch, the second event can lose its lease while the first one is running."""
    bind_handler(owner_engine, tenant.id)
    event_ids = set(add_events(owner_engine, tenant.id, 2))
    handled: list[UUID] = []

    def expire_the_other_event(context: HandlerContext) -> None:
        handled.append(context.event_id)
        [other] = event_ids - {context.event_id}
        expire_leases(owner_engine, tenant.id, only=other)
        assert reclaim_expired_leases(worker_engine, 10) == 1

    Worker(
        worker_engine, registry_with(expire_the_other_event), worker_id="w1", batch_size=2
    ).run_once()

    [only_handled] = handled
    [other] = event_ids - {only_handled}
    assert get_event(owner_engine, tenant.id, only_handled).status == "succeeded"
    assert get_event(owner_engine, tenant.id, other).status == "pending"
    [attempt] = get_attempts(owner_engine, tenant.id, other)
    assert attempt.outcome == "lease_lost"


def run_in_background(worker: Worker, stop: threading.Event) -> threading.Thread:
    thread = threading.Thread(
        target=worker.run_forever,
        kwargs={"stop": stop, "poll_seconds": 0.01, "purge_interval_seconds": 60},
        daemon=True,
    )
    thread.start()
    return thread


def test_run_forever_processes_events_purges_deliveries_and_stops_cleanly(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    with tenant_tx(owner_engine, tenant.id) as session:
        session.add(
            SeenDelivery(
                tenant_id=tenant.id,
                provider=PROVIDER,
                delivery_id="expired",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    started = threading.Event()
    stop = threading.Event()

    def signal_started(context: HandlerContext) -> None:
        started.set()

    thread = run_in_background(Worker(worker_engine, registry_with(signal_started)), stop)
    assert started.wait(10)

    # A stop request lets the event in progress finish before the loop exits.
    stop.set()
    thread.join(10)

    assert not thread.is_alive()
    assert get_event(owner_engine, tenant.id, event_id).status == "succeeded"
    with tenant_tx(owner_engine, tenant.id) as session:
        assert session.scalar(select(func.count()).select_from(SeenDelivery)) == 0


def test_run_forever_survives_a_failed_iteration(
    monkeypatch: pytest.MonkeyPatch,
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    failures: list[int] = []

    def fail_once(*args: Any) -> list[Claim]:
        if not failures:
            failures.append(1)
            raise RuntimeError("connection lost")
        return claim_events(*args)

    monkeypatch.setattr(loop, "claim_events", fail_once)
    handled = threading.Event()
    stop = threading.Event()

    def signal_handled(context: HandlerContext) -> None:
        handled.set()

    thread = run_in_background(Worker(worker_engine, registry_with(signal_handled)), stop)
    assert handled.wait(10)
    stop.set()
    thread.join(10)

    assert failures == [1]
    assert get_event(owner_engine, tenant.id, event_id).status == "succeeded"
