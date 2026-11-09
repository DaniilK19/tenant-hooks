"""The CLI through its real entry point, connected as hooks_owner like in compose."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update

from tenant_hooks import cli
from tenant_hooks.db import tenant_tx
from tenant_hooks.models import ApiKey, EventHandler, SigningSecret
from tenant_hooks.security import api_keys
from tenant_hooks.security.secret_box import SecretBox
from tenant_hooks.worker.loop import Worker
from tests.conftest import PROVIDER, DatabaseUrls
from tests.intake.helpers import event_body, post, signed_headers, stored_events
from tests.processing.helpers import Recorder, bearer, registry_with

EVENT_TYPE = "order.created"


@dataclass(frozen=True)
class Provisioned:
    id: UUID
    secret: bytes
    api_key: str


@pytest.fixture
def admin_env(
    monkeypatch: pytest.MonkeyPatch,
    database_urls: DatabaseUrls,
    master_key: str,
    clean_db: None,
) -> None:
    monkeypatch.setenv("HOOKS_DATABASE_URL", database_urls.owner)
    monkeypatch.setenv("HOOKS_SECRET_KEY", master_key)


@pytest.fixture
def provisioned(admin_env: None, capsys: pytest.CaptureFixture[str]) -> Provisioned:
    assert cli.main(["create-tenant", "Acme Inc", PROVIDER]) == 0
    values = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    return Provisioned(
        UUID(values["TENANT_ID"]), values["SIGNING_SECRET"].encode(), values["API_KEY"]
    )


def bind(tenant_id: UUID, handler: str, provider: str = PROVIDER) -> int:
    return cli.main(["bind", str(tenant_id), provider, EVENT_TYPE, handler])


def stored_binding(engine: Engine, tenant_id: UUID) -> tuple[str, bool]:
    with tenant_tx(engine, tenant_id) as session:
        binding = session.scalars(select(EventHandler)).one()
    return binding.handler_name, binding.enabled


def test_created_tenant_can_deliver_webhooks_and_read_its_events(
    client: TestClient, provisioned: Provisioned
) -> None:
    body = event_body()

    delivered = post(client, provisioned.id, body, signed_headers(provisioned.secret, body))
    listed = client.get("/v1/events", headers=bearer(provisioned.api_key))

    assert delivered.status_code == 202
    assert [event["provider_event_id"] for event in listed.json()] == ["evt_1"]


def test_credentials_are_not_stored_in_the_clear(
    owner_engine: Engine, box: SecretBox, provisioned: Provisioned
) -> None:
    with tenant_tx(owner_engine, provisioned.id) as session:
        signing_secret = session.scalars(select(SigningSecret)).one()
        api_key = session.scalars(select(ApiKey)).one()

    assert provisioned.secret not in signing_secret.ciphertext
    assert box.decrypt(signing_secret.ciphertext) == provisioned.secret
    assert api_key.key_hash == api_keys.hash_secret(provisioned.api_key.split(".", 1)[1])


def test_bound_event_reaches_its_handler(
    client: TestClient, worker_engine: Engine, owner_engine: Engine, provisioned: Provisioned
) -> None:
    recorder = Recorder()
    assert bind(provisioned.id, "log_event") == 0
    body = event_body()
    post(client, provisioned.id, body, signed_headers(provisioned.secret, body))

    Worker(worker_engine, registry_with(recorder, "log_event")).run_once()

    assert len(recorder.calls) == 1
    assert stored_events(owner_engine, provisioned.id)[0].status == "succeeded"


def test_binding_again_replaces_the_handler_and_enables_it(
    owner_engine: Engine, provisioned: Provisioned
) -> None:
    assert bind(provisioned.id, "first") == 0
    with tenant_tx(owner_engine, provisioned.id) as session:
        session.execute(update(EventHandler).values(enabled=False))

    assert bind(provisioned.id, "second") == 0

    assert stored_binding(owner_engine, provisioned.id) == ("second", True)


def test_bind_reports_a_missing_provider_without_a_traceback(
    owner_engine: Engine, provisioned: Provisioned, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bind(provisioned.id, "log_event", provider="other") == 1

    assert "no endpoint for provider other" in capsys.readouterr().err
    with tenant_tx(owner_engine, provisioned.id) as session:
        assert session.scalars(select(EventHandler)).all() == []


def test_bind_reports_an_unknown_tenant_without_a_traceback(
    admin_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bind(uuid4(), "log_event") == 1

    assert "no endpoint for provider" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["create-tenant", "Acme Inc", "Not/A/Provider"],
        ["bind", str(uuid4()), "Not/A/Provider", EVENT_TYPE, "log_event"],
        ["bind", str(uuid4()), PROVIDER, "has space", "log_event"],
        ["bind", "not-a-uuid", PROVIDER, EVENT_TYPE, "log_event"],
    ],
)
def test_rejects_values_that_intake_could_never_match(admin_env: None, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)

    assert exit_info.value.code == 2
