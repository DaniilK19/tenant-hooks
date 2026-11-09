import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, select, update
from sqlalchemy.sql.elements import ColumnElement

from tenant_hooks.db import tenant_tx
from tenant_hooks.handlers.registry import HandlerContext, HandlerRegistry, PermanentError
from tenant_hooks.models import Event, EventAttempt, EventHandler
from tenant_hooks.worker.backoff import RetryPolicy
from tenant_hooks.worker.claim import (
    Claim,
    claim_events,
    purge_expired_deliveries,
    reclaim_expired_leases,
)

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 2000
RECLAIM_LIMIT = 100


@dataclass(frozen=True)
class Job:
    provider: str
    event_type: str
    raw_body: bytes
    max_attempts: int
    handler_name: str | None


def describe_error(error: Exception) -> str:
    # PostgreSQL text cannot hold NUL, and handler errors can contain anything.
    return f"{type(error).__name__}: {error}".replace("\x00", "")[:MAX_ERROR_LENGTH]


class Worker:
    def __init__(
        self,
        engine: Engine,
        registry: HandlerRegistry,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        batch_size: int = 1,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.engine = engine
        self.registry = registry
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.lease_seconds = lease_seconds
        self.batch_size = batch_size
        self.retry = retry or RetryPolicy()

    def run_once(self) -> int:
        """Requeue expired leases, then claim and process one batch. Returns the claim count."""
        reclaimed = reclaim_expired_leases(self.engine, RECLAIM_LIMIT)
        if reclaimed:
            logger.warning("reclaimed %d expired leases", reclaimed)
        claims = claim_events(self.engine, self.batch_size, self.lease_seconds, self.worker_id)
        for claim in claims:
            self._process(claim)
        return len(claims)

    def run_forever(
        self, stop: threading.Event, poll_seconds: float, purge_interval_seconds: float
    ) -> None:
        next_purge = time.monotonic()
        while not stop.is_set():
            claimed = 0
            try:
                claimed = self.run_once()
                if time.monotonic() >= next_purge:
                    purge_expired_deliveries(self.engine)
                    next_purge = time.monotonic() + purge_interval_seconds
            except Exception:
                # Typically a lost database connection. Any leased event stays leased,
                # and the loop carries on once the database is back.
                logger.exception("worker iteration failed")
            if claimed == 0:
                stop.wait(poll_seconds)

    def _load(self, claim: Claim) -> Job | None:
        with tenant_tx(self.engine, claim.tenant_id) as session:
            event = session.execute(
                select(Event.provider, Event.event_type, Event.raw_body, Event.max_attempts).where(
                    Event.id == claim.event_id, Event.lease_token == claim.lease_token
                )
            ).one_or_none()
            if event is None:
                return None
            handler_name = session.scalar(
                select(EventHandler.handler_name).where(
                    EventHandler.provider == event.provider,
                    EventHandler.event_type == event.event_type,
                    EventHandler.enabled,
                )
            )
        return Job(
            event.provider, event.event_type, event.raw_body, event.max_attempts, handler_name
        )

    def _process(self, claim: Claim) -> None:
        job = self._load(claim)
        if job is None:
            # The lease was reclaimed before this event started. The fenced update in
            # _finish leaves the event alone and only records what happened to the attempt.
            self._finish(claim, status="pending", outcome="lease_lost")
            return
        if job.handler_name is None:
            self._finish(claim, status="skipped", outcome="skipped")
            return

        # No transaction is open here, so a slow handler holds no connection or lock.
        try:
            handler = self.registry.get(job.handler_name)
            handler(
                HandlerContext(
                    tenant_id=claim.tenant_id,
                    event_id=claim.event_id,
                    provider=job.provider,
                    event_type=job.event_type,
                    payload=json.loads(job.raw_body),
                    attempt=claim.attempt_no,
                )
            )
        except PermanentError as error:
            self._dead_letter(claim, describe_error(error))
        except Exception as error:
            message = describe_error(error)
            if claim.attempt_no >= job.max_attempts:
                self._dead_letter(claim, message)
            else:
                self._retry_later(claim, message)
        else:
            self._finish(claim, status="succeeded", outcome="succeeded")

    def _dead_letter(self, claim: Claim, message: str) -> None:
        logger.warning("dead-lettering event %s: %s", claim.event_id, message)
        self._finish(claim, status="dead_lettered", outcome="failed", error=message)

    def _retry_later(self, claim: Claim, message: str) -> None:
        delay = timedelta(seconds=self.retry.delay(claim.attempt_no))
        self._finish(
            claim,
            status="pending",
            outcome="failed",
            error=message,
            next_attempt_at=func.now() + delay,
        )

    def _finish(
        self,
        claim: Claim,
        *,
        status: str,
        outcome: str,
        error: str | None = None,
        next_attempt_at: ColumnElement[datetime] | None = None,
    ) -> None:
        """Record the result, but only if this worker still holds the lease."""
        values: dict[str, object] = {
            "status": status,
            "lease_token": None,
            "leased_by": None,
            "lease_expires_at": None,
            "last_error": error,
            "completed_at": None if status == "pending" else func.now(),
        }
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at

        with tenant_tx(self.engine, claim.tenant_id) as session:
            still_held = session.execute(
                update(Event)
                .where(Event.id == claim.event_id, Event.lease_token == claim.lease_token)
                .values(**values)
                .returning(Event.id)
            ).first()
            if still_held is None:
                logger.warning("lease lost on event %s", claim.event_id)
            session.execute(
                update(EventAttempt)
                .where(EventAttempt.lease_token == claim.lease_token)
                .values(
                    outcome=outcome if still_held is not None else "lease_lost",
                    error=error,
                    finished_at=func.now(),
                )
            )
