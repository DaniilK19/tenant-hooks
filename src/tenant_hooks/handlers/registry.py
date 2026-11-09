from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class HandlerContext:
    tenant_id: UUID
    event_id: UUID
    provider: str
    event_type: str
    payload: dict[str, Any]
    attempt: int

    @property
    def idempotency_key(self) -> str:
        """Stable across retries. Handlers run at least once, so side effects should use it."""
        return str(self.event_id)


Handler = Callable[[HandlerContext], None]


class PermanentError(Exception):
    """Raised by a handler when retrying cannot help. Dead-letters the event immediately."""


class UnknownHandlerError(Exception):
    """A tenant's binding names a handler this worker does not have."""


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        if name in self._handlers:
            raise ValueError(f"handler already registered: {name}")
        self._handlers[name] = handler

    def get(self, name: str) -> Handler:
        try:
            return self._handlers[name]
        except KeyError:
            raise UnknownHandlerError(name) from None
