from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tenant_hooks.handlers.registry import PermanentError
from tenant_hooks.worker.loop import Worker
from tests.intake.helpers import SigningTenant
from tests.processing.helpers import (
    Recorder,
    add_api_key,
    add_event,
    bearer,
    bind_handler,
    get_event,
    registry_with,
)


def dead_letter_an_event(
    owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> UUID:
    bind_handler(owner_engine, tenant.id)
    event_id = add_event(owner_engine, tenant.id)
    handler = Recorder(failures=1, error=PermanentError("bad order"))
    Worker(worker_engine, registry_with(handler)).run_once()
    assert get_event(owner_engine, tenant.id, event_id).status == "dead_lettered"
    return event_id


def replay_url(event_id: UUID) -> str:
    return f"/v1/events/{event_id}/replay"


def test_dead_lettered_event_replays_successfully(
    client: TestClient, owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    event_id = dead_letter_an_event(owner_engine, worker_engine, tenant)
    token = add_api_key(owner_engine, tenant.id)

    response = client.post(replay_url(event_id), headers=bearer(token))

    assert response.status_code == 202
    assert response.json() == {"status": "pending", "event_id": str(event_id)}
    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "pending"
    assert event.attempt_count == 0
    assert event.replay_count == 1
    assert event.last_error is None
    assert event.completed_at is None

    handler = Recorder()
    assert Worker(worker_engine, registry_with(handler)).run_once() == 1
    [context] = handler.calls
    assert context.event_id == event_id
    assert context.attempt == 1
    assert get_event(owner_engine, tenant.id, event_id).status == "succeeded"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer nope"},
        {"Authorization": f"Bearer th_{'-' * 36}.{'a' * 43}"},
        {"Authorization": f"Bearer th_{uuid4()}"},
        {"Authorization": f"Basic th_{uuid4()}.{'a' * 43}"},
    ],
)
def test_replay_rejects_missing_or_malformed_credentials(
    client: TestClient,
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    headers: dict[str, str],
) -> None:
    event_id = dead_letter_an_event(owner_engine, worker_engine, tenant)

    response = client.post(replay_url(event_id), headers=headers)

    assert response.status_code == 401
    assert get_event(owner_engine, tenant.id, event_id).status == "dead_lettered"


def test_replay_rejects_wrong_secret_for_a_real_tenant(
    client: TestClient, owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    event_id = dead_letter_an_event(owner_engine, worker_engine, tenant)
    add_api_key(owner_engine, tenant.id)

    response = client.post(replay_url(event_id), headers=bearer(f"th_{tenant.id}.{'a' * 43}"))

    assert response.status_code == 401


def test_replay_rejects_revoked_key(
    client: TestClient, owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    event_id = dead_letter_an_event(owner_engine, worker_engine, tenant)
    token = add_api_key(owner_engine, tenant.id, revoked=True)

    assert client.post(replay_url(event_id), headers=bearer(token)).status_code == 401


def test_key_secret_is_not_valid_under_another_tenant_id(
    client: TestClient,
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    other_tenant: SigningTenant,
) -> None:
    event_id = dead_letter_an_event(owner_engine, worker_engine, tenant)
    other_token = add_api_key(owner_engine, other_tenant.id)
    secret = other_token.split(".", 1)[1]

    response = client.post(replay_url(event_id), headers=bearer(f"th_{tenant.id}.{secret}"))

    assert response.status_code == 401


def test_other_tenants_key_cannot_see_or_replay_the_event(
    client: TestClient,
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    other_tenant: SigningTenant,
) -> None:
    event_id = dead_letter_an_event(owner_engine, worker_engine, tenant)
    other_token = add_api_key(owner_engine, other_tenant.id)

    response = client.post(replay_url(event_id), headers=bearer(other_token))

    assert response.status_code == 404
    event = get_event(owner_engine, tenant.id, event_id)
    assert event.status == "dead_lettered"
    assert event.replay_count == 0


def test_unknown_event_is_not_found(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    token = add_api_key(owner_engine, tenant.id)

    assert client.post(replay_url(uuid4()), headers=bearer(token)).status_code == 404


def test_event_that_is_not_dead_lettered_cannot_be_replayed(
    client: TestClient, owner_engine: Engine, tenant: SigningTenant
) -> None:
    event_id = add_event(owner_engine, tenant.id)
    token = add_api_key(owner_engine, tenant.id)

    assert client.post(replay_url(event_id), headers=bearer(token)).status_code == 409
    assert get_event(owner_engine, tenant.id, event_id).replay_count == 0


def test_concurrent_replays_take_effect_once(
    client: TestClient, owner_engine: Engine, worker_engine: Engine, tenant: SigningTenant
) -> None:
    event_id = dead_letter_an_event(owner_engine, worker_engine, tenant)
    token = add_api_key(owner_engine, tenant.id)
    start_line = Barrier(8)

    def send(_: int) -> int:
        start_line.wait()
        return int(client.post(replay_url(event_id), headers=bearer(token)).status_code)

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(send, range(8)))

    assert sorted(statuses) == [202] + [409] * 7
    assert get_event(owner_engine, tenant.id, event_id).replay_count == 1


def test_list_returns_only_the_callers_events_and_filters_by_status(
    client: TestClient,
    owner_engine: Engine,
    worker_engine: Engine,
    tenant: SigningTenant,
    other_tenant: SigningTenant,
) -> None:
    dead = dead_letter_an_event(owner_engine, worker_engine, tenant)
    pending = add_event(owner_engine, tenant.id)
    add_event(owner_engine, other_tenant.id)
    token = add_api_key(owner_engine, tenant.id)

    everything = client.get("/v1/events", headers=bearer(token))
    dead_only = client.get("/v1/events", params={"status": "dead_lettered"}, headers=bearer(token))

    assert everything.status_code == 200
    assert {item["id"] for item in everything.json()} == {str(dead), str(pending)}
    assert [item["id"] for item in dead_only.json()] == [str(dead)]
    assert dead_only.json()[0]["last_error"] == "PermanentError: bad order"
    assert "raw_body" not in dead_only.json()[0]


def test_list_requires_a_valid_key(client: TestClient) -> None:
    assert client.get("/v1/events").status_code == 401
