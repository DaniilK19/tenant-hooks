import json
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from threading import Barrier
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from tenant_hooks.db import tenant_tx
from tenant_hooks.models import Endpoint, Event, SeenDelivery, SigningSecret, Tenant
from tenant_hooks.security import signatures
from tenant_hooks.security.secret_box import SecretBox
from tests.conftest import PROVIDER


@dataclass(frozen=True)
class SigningTenant:
    id: UUID
    secret: bytes


@dataclass(frozen=True)
class Delivery:
    tenant_id: UUID
    body: bytes
    headers: dict[str, str]


def provision(
    engine: Engine,
    box: SecretBox,
    name: str,
    *,
    enabled: bool = True,
    secret_expiries: Sequence[datetime | None] = (None,),
) -> SigningTenant:
    tenant_id = uuid4()
    secret = f"whsec-{uuid4()}".encode()
    with tenant_tx(engine, tenant_id) as session:
        session.add(Tenant(id=tenant_id, name=name))
        session.flush()
        session.add(Endpoint(tenant_id=tenant_id, provider=PROVIDER, enabled=enabled))
        session.flush()
        session.add_all(
            SigningSecret(
                tenant_id=tenant_id,
                provider=PROVIDER,
                ciphertext=box.encrypt(secret),
                expires_at=expires_at,
            )
            for expires_at in secret_expiries
        )
    return SigningTenant(tenant_id, secret)


def event_body(event_id: str = "evt_1", event_type: str = "order.created") -> bytes:
    return json.dumps({"id": event_id, "type": event_type, "data": {"total": 42}}).encode()


def signed_headers(
    secret: bytes,
    body: bytes,
    *,
    delivery_id: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    delivery = delivery_id or f"dlv_{uuid4().hex}"
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    return {
        "X-Hooks-Timestamp": stamp,
        "X-Hooks-Delivery-Id": delivery,
        "X-Hooks-Signature": signatures.sign(secret, stamp, delivery, body),
    }


def post(
    client: TestClient,
    tenant_id: UUID,
    body: bytes,
    headers: dict[str, str],
    provider: str = PROVIDER,
) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/v1/hooks/{tenant_id}/{provider}", content=body, headers=headers
    )
    return response


def send_concurrently(client: TestClient, deliveries: Sequence[Delivery]) -> list[int]:
    """Release every request at once and return their status codes."""
    start_line = Barrier(len(deliveries))

    def send(delivery: Delivery) -> int:
        start_line.wait()
        return post(client, delivery.tenant_id, delivery.body, delivery.headers).status_code

    with ThreadPoolExecutor(max_workers=len(deliveries)) as executor:
        return list(executor.map(send, deliveries))


def stored_events(engine: Engine, tenant_id: UUID) -> Sequence[Event]:
    with tenant_tx(engine, tenant_id) as session:
        return session.scalars(select(Event)).all()


def seen_delivery_count(engine: Engine, tenant_id: UUID) -> int:
    with tenant_tx(engine, tenant_id) as session:
        return session.execute(select(func.count()).select_from(SeenDelivery)).scalar_one()
