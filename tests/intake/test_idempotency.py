"""Replays are rejected, duplicates are absorbed, and each event is stored exactly once."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tests.intake.helpers import (
    Delivery,
    SigningTenant,
    event_body,
    post,
    seen_delivery_count,
    send_concurrently,
    signed_headers,
    stored_events,
)

CONCURRENCY = 20


def test_replayed_delivery_is_rejected(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    body = event_body()
    headers = signed_headers(tenant.secret, body)

    first = post(client, tenant.id, body, headers)
    replay = post(client, tenant.id, body, headers)

    assert first.status_code == 202
    assert replay.status_code == 409
    assert len(stored_events(owner_engine, tenant.id)) == 1
    assert seen_delivery_count(owner_engine, tenant.id) == 1


def test_same_event_twice_is_stored_once(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    body = event_body()

    first = post(client, tenant.id, body, signed_headers(tenant.secret, body))
    second = post(client, tenant.id, body, signed_headers(tenant.secret, body))

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate", "event_id": first.json()["event_id"]}
    assert len(stored_events(owner_engine, tenant.id)) == 1
    assert seen_delivery_count(owner_engine, tenant.id) == 2


def test_same_event_20x_concurrently_is_stored_once(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    """Each attempt is a separate delivery, as with a provider retrying after a timeout."""
    body = event_body()
    deliveries = [
        Delivery(tenant.id, body, signed_headers(tenant.secret, body)) for _ in range(CONCURRENCY)
    ]

    statuses = send_concurrently(client, deliveries)

    assert sorted(statuses) == [200] * (CONCURRENCY - 1) + [202]
    assert len(stored_events(owner_engine, tenant.id)) == 1
    assert seen_delivery_count(owner_engine, tenant.id) == CONCURRENCY


def test_identical_request_20x_concurrently_is_stored_once(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    body = event_body()
    delivery = Delivery(tenant.id, body, signed_headers(tenant.secret, body))

    statuses = send_concurrently(client, [delivery] * CONCURRENCY)

    assert sorted(statuses) == [202] + [409] * (CONCURRENCY - 1)
    assert len(stored_events(owner_engine, tenant.id)) == 1
    assert seen_delivery_count(owner_engine, tenant.id) == 1


def test_distinct_events_concurrently_are_all_stored(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    deliveries = []
    for _ in range(CONCURRENCY):
        body = event_body(f"evt_{uuid4().hex}")
        deliveries.append(Delivery(tenant.id, body, signed_headers(tenant.secret, body)))

    statuses = send_concurrently(client, deliveries)

    assert statuses == [202] * CONCURRENCY
    assert len(stored_events(owner_engine, tenant.id)) == CONCURRENCY


def test_same_event_and_delivery_ids_are_independent_per_tenant(
    client: TestClient,
    owner_engine: Engine,
    tenant: SigningTenant,
    other_tenant: SigningTenant,
) -> None:
    body = event_body()

    for current in (tenant, other_tenant):
        headers = signed_headers(current.secret, body, delivery_id="dlv_shared")
        assert post(client, current.id, body, headers).status_code == 202
        assert len(stored_events(owner_engine, current.id)) == 1


def test_first_payload_wins_for_a_repeated_event_id(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    first_body = event_body(event_type="order.created")
    second_body = event_body(event_type="order.cancelled")

    post(client, tenant.id, first_body, signed_headers(tenant.secret, first_body))
    response = post(client, tenant.id, second_body, signed_headers(tenant.secret, second_body))

    assert response.status_code == 200
    [event] = stored_events(owner_engine, tenant.id)
    assert event.raw_body == first_body
    assert event.event_type == "order.created"
