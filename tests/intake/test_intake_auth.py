"""Who is allowed to deliver a webhook, and what a rejected delivery leaves behind."""

import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tenant_hooks.security.secret_box import SecretBox
from tests.conftest import PROVIDER
from tests.http_fixtures import MAX_BODY_BYTES, TOLERANCE_SECONDS
from tests.intake.helpers import (
    SigningTenant,
    event_body,
    post,
    provision,
    seen_delivery_count,
    signed_headers,
    stored_events,
)

INVALID_SIGNATURE = {"detail": "invalid signature"}


def test_valid_delivery_is_stored_byte_for_byte(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    body = b'{ "id": "evt_1",   "type": "order.created", "data": {} }'

    response = post(client, tenant.id, body, signed_headers(tenant.secret, body))

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    [event] = stored_events(owner_engine, tenant.id)
    assert str(event.id) == response.json()["event_id"]
    assert event.raw_body == body
    assert event.tenant_id == tenant.id
    assert event.provider == PROVIDER
    assert event.provider_event_id == "evt_1"
    assert event.event_type == "order.created"
    assert event.status == "pending"


def test_wrong_secret_is_rejected(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    body = event_body()

    response = post(client, tenant.id, body, signed_headers(b"wrong", body))

    assert response.status_code == 401
    assert response.json() == INVALID_SIGNATURE
    assert stored_events(owner_engine, tenant.id) == []


def test_other_tenants_secret_is_rejected(
    client: TestClient, tenant: SigningTenant, other_tenant: SigningTenant
) -> None:
    body = event_body()

    response = post(client, tenant.id, body, signed_headers(other_tenant.secret, body))

    assert response.status_code == 401


def test_tampered_body_is_rejected(client: TestClient, tenant: SigningTenant) -> None:
    headers = signed_headers(tenant.secret, event_body("evt_1"))

    response = post(client, tenant.id, event_body("evt_2"), headers)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "header", ["X-Hooks-Timestamp", "X-Hooks-Delivery-Id", "X-Hooks-Signature"]
)
def test_missing_header_is_rejected(client: TestClient, tenant: SigningTenant, header: str) -> None:
    body = event_body()
    headers = signed_headers(tenant.secret, body)
    del headers[header]

    response = post(client, tenant.id, body, headers)

    assert response.status_code == 401
    assert response.json() == INVALID_SIGNATURE


@pytest.mark.parametrize("timestamp", ["soon", "-5", "1.5", "9" * 13])
def test_malformed_timestamp_is_rejected(
    client: TestClient, tenant: SigningTenant, timestamp: str
) -> None:
    body = event_body()
    headers = signed_headers(tenant.secret, body) | {"X-Hooks-Timestamp": timestamp}

    assert post(client, tenant.id, body, headers).status_code == 401


def test_delivery_id_with_field_separator_is_rejected(
    client: TestClient, tenant: SigningTenant
) -> None:
    """A dot would let bytes move between the delivery id and the body under one signature."""
    body = event_body()
    headers = signed_headers(tenant.secret, body, delivery_id="dlv.1")

    assert post(client, tenant.id, body, headers).status_code == 401


def test_non_ascii_signature_header_is_rejected(client: TestClient, tenant: SigningTenant) -> None:
    body = event_body()
    headers: dict[str, str | bytes] = {
        **signed_headers(tenant.secret, body),
        "X-Hooks-Signature": "v1=é".encode("latin-1"),
    }

    response = client.post(f"/v1/hooks/{tenant.id}/{PROVIDER}", content=body, headers=headers)

    assert response.status_code == 401


def test_unknown_tenant_gets_the_same_response_as_a_bad_signature(
    client: TestClient, tenant: SigningTenant
) -> None:
    body = event_body()

    response = post(client, uuid4(), body, signed_headers(tenant.secret, body))

    assert response.status_code == 401
    assert response.json() == INVALID_SIGNATURE


def test_unknown_provider_is_rejected(client: TestClient, tenant: SigningTenant) -> None:
    body = event_body()

    response = post(client, tenant.id, body, signed_headers(tenant.secret, body), "other")

    assert response.status_code == 401
    assert response.json() == INVALID_SIGNATURE


def test_disabled_endpoint_is_rejected(
    client: TestClient, owner_engine: Engine, box: SecretBox, clean_db: None
) -> None:
    disabled = provision(owner_engine, box, "tenant-d", enabled=False)
    body = event_body()

    assert post(client, disabled.id, body, signed_headers(disabled.secret, body)).status_code == 401


def test_expired_secret_is_rejected(
    client: TestClient, owner_engine: Engine, box: SecretBox, clean_db: None
) -> None:
    past = datetime.now(UTC) - timedelta(minutes=1)
    expired = provision(owner_engine, box, "tenant-e", secret_expiries=[past])
    body = event_body()

    assert post(client, expired.id, body, signed_headers(expired.secret, body)).status_code == 401


def test_secret_with_future_expiry_still_works_during_rotation(
    client: TestClient, owner_engine: Engine, box: SecretBox, clean_db: None
) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    rotating = provision(owner_engine, box, "tenant-r", secret_expiries=[future])
    body = event_body()

    assert post(client, rotating.id, body, signed_headers(rotating.secret, body)).status_code == 202


@pytest.mark.parametrize("offset", [-(TOLERANCE_SECONDS + 30), TOLERANCE_SECONDS + 30])
def test_timestamp_outside_tolerance_is_rejected(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant, offset: int
) -> None:
    body = event_body()
    headers = signed_headers(tenant.secret, body, timestamp=int(time.time()) + offset)

    response = post(client, tenant.id, body, headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "timestamp outside tolerance"}
    assert stored_events(owner_engine, tenant.id) == []


def test_stale_timestamp_with_bad_signature_reveals_nothing(
    client: TestClient, tenant: SigningTenant
) -> None:
    body = event_body()
    headers = signed_headers(b"wrong", body, timestamp=int(time.time()) - 100_000)

    assert post(client, tenant.id, body, headers).json() == INVALID_SIGNATURE


def test_timestamp_inside_tolerance_is_accepted(client: TestClient, tenant: SigningTenant) -> None:
    body = event_body()
    headers = signed_headers(tenant.secret, body, timestamp=int(time.time()) - 200)

    assert post(client, tenant.id, body, headers).status_code == 202


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b'{"type": "order.created"}',
        b'{"id": "evt_1"}',
        b'{"id": 1, "type": "order.created"}',
        b'{"id": "", "type": "order.created"}',
        b'{"id": "evt\\u0000", "type": "order.created"}',
    ],
)
def test_invalid_body_with_valid_signature_is_rejected(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant, body: bytes
) -> None:
    response = post(client, tenant.id, body, signed_headers(tenant.secret, body))

    assert response.status_code == 400
    assert stored_events(owner_engine, tenant.id) == []


def test_oversized_body_is_rejected(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    body = json.dumps(
        {"id": "evt_1", "type": "order.created", "data": "x" * MAX_BODY_BYTES}
    ).encode()

    response = post(client, tenant.id, body, signed_headers(tenant.secret, body))

    assert response.status_code == 413
    assert stored_events(owner_engine, tenant.id) == []


@pytest.mark.parametrize("provider", ["ac%00me", "ACME", "a%2Fb"])
def test_invalid_provider_in_path_is_rejected_before_the_database(
    client: TestClient, tenant: SigningTenant, provider: str
) -> None:
    response = client.post(f"/v1/hooks/{tenant.id}/{provider}", content=b"{}")

    assert response.status_code in (404, 422)


def test_rejected_delivery_does_not_use_up_its_delivery_id(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    bad_body = b"not json"
    delivery_id = "dlv_retry"
    rejected = post(
        client,
        tenant.id,
        bad_body,
        signed_headers(tenant.secret, bad_body, delivery_id=delivery_id),
    )
    assert rejected.status_code == 400
    assert seen_delivery_count(owner_engine, tenant.id) == 0

    body = event_body()
    accepted = post(
        client, tenant.id, body, signed_headers(tenant.secret, body, delivery_id=delivery_id)
    )
    assert accepted.status_code == 202
