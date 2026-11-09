from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update

from tenant_hooks.db import tenant_tx
from tenant_hooks.models import Event
from tenant_hooks.security import api_keys

router = APIRouter()

EventStatus = Literal["pending", "processing", "succeeded", "skipped", "dead_lettered"]


class EventSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: str
    provider_event_id: str
    event_type: str
    status: EventStatus
    attempt_count: int
    replay_count: int
    last_error: str | None
    received_at: datetime


class ReplayResult(BaseModel):
    status: Literal["pending"]
    event_id: UUID


def require_credentials(header: str | None) -> api_keys.Credentials:
    credentials = api_keys.parse_bearer(header)
    if credentials is None:
        raise unauthorized()
    return credentials


def unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="invalid api key")


@router.get("/v1/events")
def list_events(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    status: EventStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[EventSummary]:
    credentials = require_credentials(authorization)
    with tenant_tx(request.app.state.engine, credentials.tenant_id) as session:
        if not api_keys.is_valid(session, credentials.secret):
            raise unauthorized()
        query = select(Event).order_by(Event.received_at.desc()).limit(limit)
        if status is not None:
            query = query.where(Event.status == status)
        return [EventSummary.model_validate(event) for event in session.scalars(query)]


@router.post("/v1/events/{event_id}/replay", status_code=202)
def replay_event(
    event_id: UUID,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> ReplayResult:
    credentials = require_credentials(authorization)
    with tenant_tx(request.app.state.engine, credentials.tenant_id) as session:
        if not api_keys.is_valid(session, credentials.secret):
            raise unauthorized()

        # The status check is part of the UPDATE, so of two simultaneous replays,
        # the second finds the row already pending and changes nothing.
        replayed = session.execute(
            update(Event)
            .where(Event.id == event_id, Event.status == "dead_lettered")
            .values(
                status="pending",
                attempt_count=0,
                replay_count=Event.replay_count + 1,
                next_attempt_at=func.now(),
                last_error=None,
                completed_at=None,
            )
            .returning(Event.id)
        ).first()
        if replayed is not None:
            return ReplayResult(status="pending", event_id=event_id)

        # Another tenant's event is invisible here, so it is reported as not found.
        if session.get(Event, event_id) is None:
            raise HTTPException(status_code=404, detail="event not found")
        raise HTTPException(status_code=409, detail="event is not dead-lettered")
