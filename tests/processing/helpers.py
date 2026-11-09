from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select, update

from tenant_hooks.db import tenant_tx
from tenant_hooks.handlers.registry import Handler, HandlerContext, HandlerRegistry
from tenant_hooks.models import ApiKey, Event, EventAttempt, EventHandler
from tenant_hooks.security import api_keys
from tests.conftest import PROVIDER

HANDLER_NAME = "test_handler"


class Recorder:
    """A handler that remembers its calls and fails the first `failures` of them."""

    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.calls: list[HandlerContext] = []
        self.failures = failures
        self.error = error or RuntimeError("handler failed")

    def __call__(self, context: HandlerContext) -> None:
        self.calls.append(context)
        if len(self.calls) <= self.failures:
            raise self.error


def registry_with(handler: Handler, name: str = HANDLER_NAME) -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register(name, handler)
    return registry


def bind_handler(
    engine: Engine,
    tenant_id: UUID,
    event_type: str = "order.created",
    handler_name: str = HANDLER_NAME,
    enabled: bool = True,
) -> None:
    with tenant_tx(engine, tenant_id) as session:
        session.add(
            EventHandler(
                tenant_id=tenant_id,
                provider=PROVIDER,
                event_type=event_type,
                handler_name=handler_name,
                enabled=enabled,
            )
        )


def add_events(
    engine: Engine,
    tenant_id: UUID,
    count: int,
    event_type: str = "order.created",
    max_attempts: int = 3,
) -> list[UUID]:
    event_ids = [uuid4() for _ in range(count)]
    with tenant_tx(engine, tenant_id) as session:
        session.add_all(
            Event(
                id=event_id,
                tenant_id=tenant_id,
                provider=PROVIDER,
                provider_event_id=f"evt_{event_id.hex}",
                event_type=event_type,
                raw_body=b'{"data": {"total": 42}}',
                max_attempts=max_attempts,
            )
            for event_id in event_ids
        )
    return event_ids


def add_event(
    engine: Engine,
    tenant_id: UUID,
    event_type: str = "order.created",
    max_attempts: int = 3,
) -> UUID:
    return add_events(engine, tenant_id, 1, event_type, max_attempts)[0]


def expire_leases(engine: Engine, tenant_id: UUID, only: UUID | None = None) -> None:
    """Make leases look expired, so that tests do not have to wait out the timeout."""
    query = update(Event).where(Event.status == "processing")
    if only is not None:
        query = query.where(Event.id == only)
    with tenant_tx(engine, tenant_id) as session:
        session.execute(query.values(lease_expires_at=func.now() - timedelta(seconds=1)))


def get_event(engine: Engine, tenant_id: UUID, event_id: UUID) -> Event:
    with tenant_tx(engine, tenant_id) as session:
        event = session.get(Event, event_id)
    assert event is not None
    return event


def get_attempts(engine: Engine, tenant_id: UUID, event_id: UUID) -> list[EventAttempt]:
    with tenant_tx(engine, tenant_id) as session:
        return list(
            session.scalars(
                select(EventAttempt)
                .where(EventAttempt.event_id == event_id)
                .order_by(EventAttempt.started_at)
            )
        )


def make_due(engine: Engine, tenant_id: UUID, event_id: UUID) -> None:
    """Skip the backoff wait, so that tests do not have to sleep through it."""
    with tenant_tx(engine, tenant_id) as session:
        session.execute(
            update(Event).where(Event.id == event_id).values(next_attempt_at=func.now())
        )


def add_api_key(engine: Engine, tenant_id: UUID, *, revoked: bool = False) -> str:
    token, key_hash = api_keys.generate(tenant_id)
    with tenant_tx(engine, tenant_id) as session:
        session.add(
            ApiKey(
                tenant_id=tenant_id,
                name="test",
                key_hash=key_hash,
                revoked_at=func.now() if revoked else None,
            )
        )
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
