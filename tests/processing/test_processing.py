from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tenant_hooks.handlers.registry import HandlerContext, HandlerRegistry, PermanentError
from tenant_hooks.worker.backoff import RetryPolicy
from tenant_hooks.worker.loop import Worker
from tests.intake.helpers import (
    Delivery,
    SigningTenant,
    event_body,
    post,
    send_concurrently,
    signed_headers,
    stored_events,
)
from tests.processing.helpers import (
    HANDLER_NAME,
    Recorder,
    add_event,
    bind_handler,
    get_attempts,
    get_event,
    make_due,
    registry_with,
)

NO_JITTER_RETRY = RetryPolicy(base_seconds=1, cap_seconds=8, rng=lambda _low, high: high)


def test_backoff_doubles_up_to_the_cap() -> None:
    delays = [NO_JITTER_RETRY.delay(attempt) for attempt in range(1, 7)]

    assert delays == [1, 2, 4, 8, 8, 8]


def test_backoff_jitter_stays_within_bounds() -> None:
    policy = RetryPolicy(base_seconds=1, cap_seconds=8)

    for attempt in range(1, 8):
        ceiling = min(8, 2 ** (attempt - 1))
        for _ in range(200):
            assert 0 <= policy.delay(attempt) <= ceiling


def test_backoff_samples_the_full_range_from_zero() -> None:
    bounds: list[tuple[float, float]] = []

    def recording_rng(low: float, high: float) -> float:
        bounds.append((low, high))
        return low

    policy = RetryPolicy(base_seconds=1, cap_seconds=8, rng=recording_rng)
    assert policy.delay(3) == 0
    assert bounds == [(0, 4)]


def test_successful_handler_completes_the_event(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    handler = Recorder()

    assert Worker(worker_engine, registry_with(handler), worker_id="w1").run_once() == 1

    [context] = handler.calls
    assert context.tenant_id == tenant.id
    assert context.event_id == event_id
    assert context.idempotency_key == str(event_id)
    assert context.event_type == "order.created"
    assert context.payload == {"data": {"total": 42}}
    assert context.attempt == 1

    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "succeeded"
    assert event.completed_at is not None
    assert event.lease_token is None
    assert event.leased_by is None
    assert event.lease_expires_at is None

    [attempt] = get_attempts(owner_engine, tenant.id, event_id)
    assert attempt.outcome == "succeeded"
    assert attempt.worker_id == "w1"
    assert attempt.finished_at is not None


def test_claim_is_committed_while_the_handler_runs(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    seen: list[tuple[str, int]] = []

    def inspect(context: HandlerContext) -> None:
        event = get_event(owner_engine, tenant.id, event_id)
        seen.append((event.status, event.attempt_count))

    Worker(worker_engine, registry_with(inspect)).run_once()

    assert seen == [("processing", 1)]


def test_one_worker_serves_every_tenant_with_its_own_binding(
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    other_tenant: SigningTenant,
) -> None:
    for current in (tenant, other_tenant):
        bind_handler(owner_engine, current.id)
        add_event(owner_engine, current.id)
    handler = Recorder()

    worker = Worker(worker_engine, registry_with(handler), batch_size=10)
    assert worker.run_once() == 2

    assert {context.tenant_id for context in handler.calls} == {tenant.id, other_tenant.id}


def test_handlers_are_dispatched_by_event_type(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id, "order.created", "on_created")
    bind_handler(owner_engine, tenant.id, "order.cancelled", "on_cancelled")
    add_event(owner_engine, tenant.id, "order.created")
    add_event(owner_engine, tenant.id, "order.cancelled")
    created, cancelled = Recorder(), Recorder()
    registry = HandlerRegistry()
    registry.register("on_created", created)
    registry.register("on_cancelled", cancelled)

    Worker(worker_engine, registry, batch_size=10).run_once()

    assert [context.event_type for context in created.calls] == ["order.created"]
    assert [context.event_type for context in cancelled.calls] == ["order.cancelled"]


def test_event_without_binding_is_skipped(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    event_id = add_event(owner_engine, tenant.id)
    handler = Recorder()

    Worker(worker_engine, registry_with(handler)).run_once()

    assert handler.calls == []
    assert get_event(owner_engine, tenant.id, event_id).status == "skipped"
    [attempt] = get_attempts(owner_engine, tenant.id, event_id)
    assert attempt.outcome == "skipped"


def test_disabled_binding_is_skipped(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id, enabled=False)
    event_id = add_event(owner_engine, tenant.id)
    handler = Recorder()

    Worker(worker_engine, registry_with(handler)).run_once()

    assert handler.calls == []
    assert get_event(owner_engine, tenant.id, event_id).status == "skipped"


def test_binding_to_unknown_handler_is_retried_not_skipped(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id, handler_name="not_deployed_yet")
    event_id = add_event(owner_engine, tenant.id)

    Worker(worker_engine, registry_with(Recorder()), retry=NO_JITTER_RETRY).run_once()

    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "pending"
    assert "not_deployed_yet" in (event.last_error or "")


def test_failed_handler_is_rescheduled_and_not_claimed_before_it_is_due(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    handler = Recorder(failures=1)
    worker = Worker(worker_engine, registry_with(handler), retry=NO_JITTER_RETRY)

    before = get_event(owner_engine, tenant.id, event_id).next_attempt_at
    assert worker.run_once() == 1
    assert worker.run_once() == 0

    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "pending"
    assert event.attempt_count == 1
    assert event.lease_token is None
    assert event.completed_at is None
    assert event.last_error == "RuntimeError: handler failed"
    assert event.next_attempt_at > before
    [attempt] = get_attempts(owner_engine, tenant.id, event_id)
    assert attempt.outcome == "failed"
    assert attempt.error == "RuntimeError: handler failed"

    make_due(owner_engine, tenant.id, event_id)
    assert worker.run_once() == 1
    assert len(handler.calls) == 2
    assert get_event(owner_engine, tenant.id, event_id).status == "succeeded"


def test_handler_keeps_failing_then_event_is_dead_lettered(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id, max_attempts=3)
    handler = Recorder(failures=99)
    asked_ceilings: list[float] = []

    def recording_rng(_low: float, high: float) -> float:
        asked_ceilings.append(high)
        return high

    worker = Worker(
        worker_engine,
        registry_with(handler),
        retry=RetryPolicy(base_seconds=1, cap_seconds=8, rng=recording_rng),
    )

    for _ in range(3):
        assert worker.run_once() == 1
        make_due(owner_engine, tenant.id, event_id)

    assert worker.run_once() == 0
    assert len(handler.calls) == 3
    assert [context.attempt for context in handler.calls] == [1, 2, 3]
    # Two waits: there is no backoff after the attempt that dead-letters the event.
    assert asked_ceilings == [1, 2]

    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "dead_lettered"
    assert event.attempt_count == 3
    assert event.lease_token is None
    assert event.completed_at is not None
    assert event.last_error == "RuntimeError: handler failed"
    attempts = get_attempts(owner_engine, tenant.id, event_id)
    assert [attempt.outcome for attempt in attempts] == ["failed", "failed", "failed"]


def test_permanent_error_skips_remaining_retries(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id, max_attempts=5)
    handler = Recorder(failures=99, error=PermanentError("bad order"))

    Worker(worker_engine, registry_with(handler)).run_once()

    assert len(handler.calls) == 1
    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "dead_lettered"
    assert event.last_error == "PermanentError: bad order"


def test_error_text_with_nul_bytes_is_stored_safely(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    handler = Recorder(failures=1, error=ValueError("bad\x00value " + "x" * 5000))

    Worker(worker_engine, registry_with(handler), retry=NO_JITTER_RETRY).run_once()

    last_error = get_event(owner_engine, tenant.id, event_id).last_error or ""
    assert last_error.startswith("ValueError: badvalue")
    assert len(last_error) == 2000


def test_same_event_delivered_twice_is_processed_once(
    client: TestClient, owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    body = event_body()
    assert post(client, tenant.id, body, signed_headers(tenant.secret, body)).status_code == 202
    assert post(client, tenant.id, body, signed_headers(tenant.secret, body)).status_code == 200
    handler = Recorder()
    worker = Worker(worker_engine, registry_with(handler, HANDLER_NAME))

    while worker.run_once():
        pass

    assert len(handler.calls) == 1


def test_same_event_delivered_20x_concurrently_is_processed_once(
    client: TestClient, owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    bind_handler(owner_engine, tenant.id)
    body = event_body()
    deliveries = [Delivery(tenant.id, body, signed_headers(tenant.secret, body)) for _ in range(20)]
    send_concurrently(client, deliveries)
    handler = Recorder()
    worker = Worker(worker_engine, registry_with(handler))

    while worker.run_once():
        pass

    assert len(handler.calls) == 1
    [event] = stored_events(owner_engine, tenant.id)
    assert event.status == "succeeded"
