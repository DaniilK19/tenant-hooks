import re
import time
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tenant_hooks.db import tenant_tx
from tenant_hooks.models import Endpoint, Event, SeenDelivery, SigningSecret
from tenant_hooks.security import signatures
from tenant_hooks.security.secret_box import SecretBox

router = APIRouter()

PROVIDER_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
# Visible ASCII only. This also keeps NUL bytes, which PostgreSQL text rejects, out of the query.
EVENT_FIELD_PATTERN = r"^[!-~]{1,255}$"
TIMESTAMP_FORMAT = re.compile(r"[0-9]{1,12}")
DELIVERY_ID_FORMAT = re.compile(r"[A-Za-z0-9_-]{1,128}")

# A timestamp may be up to one tolerance in the future, so a delivery can stay valid for
# two tolerances. The margin covers clock differences between the app and the database.
DELIVERY_RETENTION_MARGIN = timedelta(seconds=60)


class EventEnvelope(BaseModel):
    id: str = Field(pattern=EVENT_FIELD_PATTERN)
    type: str = Field(pattern=EVENT_FIELD_PATTERN)


class IntakeResult(BaseModel):
    status: Literal["accepted", "duplicate"]
    event_id: UUID


def unauthorized() -> HTTPException:
    # One response for every pre-authentication failure, so a caller cannot tell
    # an unknown tenant or provider from a wrong signature.
    return HTTPException(status_code=401, detail="invalid signature")


async def read_limited_body(request: Request) -> bytes:
    """Read the exact request bytes, stopping as soon as the size limit is passed."""
    limit: int = request.app.state.settings.max_body_bytes
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="body too large")
    return bytes(body)


def active_secrets(session: Session, box: SecretBox, provider: str) -> list[bytes]:
    """Load the secrets for the tenant set on this transaction. RLS scopes the query."""
    ciphertexts = session.scalars(
        select(SigningSecret.ciphertext)
        .join(
            Endpoint,
            and_(
                Endpoint.tenant_id == SigningSecret.tenant_id,
                Endpoint.provider == SigningSecret.provider,
            ),
        )
        .where(
            SigningSecret.provider == provider,
            Endpoint.enabled,
            or_(SigningSecret.expires_at.is_(None), SigningSecret.expires_at > func.now()),
        )
    )
    return [box.decrypt(ciphertext) for ciphertext in ciphertexts]


def record_delivery(
    session: Session, tenant_id: UUID, provider: str, delivery_id: str, retention: timedelta
) -> bool:
    """Return False if this delivery id was already recorded, which makes it a replay."""
    inserted = session.execute(
        insert(SeenDelivery)
        .values(
            tenant_id=tenant_id,
            provider=provider,
            delivery_id=delivery_id,
            expires_at=func.now() + retention,
        )
        .on_conflict_do_nothing(constraint="pk_seen_deliveries")
        .returning(SeenDelivery.delivery_id)
    ).first()
    return inserted is not None


def insert_event(
    session: Session, tenant_id: UUID, provider: str, envelope: EventEnvelope, body: bytes
) -> UUID | None:
    """Insert the event, or return None if the unique constraint says it already exists."""
    return session.execute(
        insert(Event)
        .values(
            tenant_id=tenant_id,
            provider=provider,
            provider_event_id=envelope.id,
            event_type=envelope.type,
            raw_body=body,
        )
        .on_conflict_do_nothing(constraint="uq_events_idempotency")
        .returning(Event.id)
    ).scalar_one_or_none()


@router.post("/v1/hooks/{tenant_id}/{provider}", status_code=202)
def receive_webhook(
    tenant_id: UUID,
    provider: Annotated[str, Path(pattern=PROVIDER_PATTERN)],
    request: Request,
    response: Response,
    body: Annotated[bytes, Depends(read_limited_body)],
    timestamp: Annotated[str | None, Header(alias="X-Hooks-Timestamp")] = None,
    delivery_id: Annotated[str | None, Header(alias="X-Hooks-Delivery-Id")] = None,
    signature: Annotated[str | None, Header(alias="X-Hooks-Signature")] = None,
) -> IntakeResult:
    if (
        timestamp is None
        or delivery_id is None
        or signature is None
        or not TIMESTAMP_FORMAT.fullmatch(timestamp)
        or not DELIVERY_ID_FORMAT.fullmatch(delivery_id)
    ):
        raise unauthorized()

    state = request.app.state
    tolerance: int = state.settings.signature_tolerance_seconds

    # One transaction for everything below. Any rejection rolls it back, so a failed
    # request cannot use up a delivery id, and a crash cannot store only half the work.
    with tenant_tx(state.engine, tenant_id) as session:
        secrets = active_secrets(session, state.secret_box, provider)
        if not signatures.verify(secrets, timestamp, delivery_id, body, signature):
            raise unauthorized()

        # Checked after the signature, so only a real signer can get this response.
        if not signatures.timestamp_is_fresh(int(timestamp), time.time(), tolerance):
            raise HTTPException(status_code=401, detail="timestamp outside tolerance")

        try:
            envelope = EventEnvelope.model_validate_json(body)
        except ValidationError as error:
            raise HTTPException(status_code=400, detail="invalid event body") from error

        retention = 2 * timedelta(seconds=tolerance) + DELIVERY_RETENTION_MARGIN
        if not record_delivery(session, tenant_id, provider, delivery_id, retention):
            raise HTTPException(status_code=409, detail="delivery already received")

        event_id = insert_event(session, tenant_id, provider, envelope, body)
        if event_id is not None:
            return IntakeResult(status="accepted", event_id=event_id)

        response.status_code = 200
        existing_id = session.scalars(
            select(Event.id).where(
                Event.provider == provider, Event.provider_event_id == envelope.id
            )
        ).one()
        return IntakeResult(status="duplicate", event_id=existing_id)
