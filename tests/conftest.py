"""Fixtures that run against a real PostgreSQL 16 started by testcontainers.

The container mounts docker/postgres-init and the schema comes from Alembic, so
tests exercise the same roles and migrations that `docker compose up` uses.
"""

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from testcontainers.community.postgres import PostgresContainer

from tenant_hooks.db import make_engine, tenant_tx
from tenant_hooks.models import (
    ApiKey,
    Endpoint,
    Event,
    EventAttempt,
    EventHandler,
    SeenDelivery,
    SigningSecret,
    Tenant,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "tenant_hooks"
PROVIDER = "acme"
ROLE_PASSWORDS = {
    "hooks_owner": "test-only-owner",
    "hooks_api": "test-only-api",
    "hooks_worker": "test-only-worker",
}


@dataclass(frozen=True)
class DatabaseUrls:
    owner: str
    api: str
    worker: str


@dataclass(frozen=True)
class SeededTenant:
    id: UUID
    event_id: UUID
    lease_token: UUID


@dataclass(frozen=True)
class TwoTenants:
    a: SeededTenant
    b: SeededTenant


def wait_until_connectable(url: str, timeout_seconds: float = 60) -> None:
    probe = create_engine(url)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                with probe.connect():
                    return
            except OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.25)
    finally:
        probe.dispose()


@pytest.fixture(scope="session")
def database_urls() -> Iterator[DatabaseUrls]:
    container = (
        PostgresContainer(
            "postgres:16", username="postgres", password="test-only-superuser", dbname=DB_NAME
        )
        .with_env("HOOKS_OWNER_PASSWORD", ROLE_PASSWORDS["hooks_owner"])
        .with_env("HOOKS_API_PASSWORD", ROLE_PASSWORDS["hooks_api"])
        .with_env("HOOKS_WORKER_PASSWORD", ROLE_PASSWORDS["hooks_worker"])
        .with_volume_mapping(
            str(REPO_ROOT / "docker" / "postgres-init"), "/docker-entrypoint-initdb.d", "ro"
        )
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)

        def url(role: str) -> str:
            password = ROLE_PASSWORDS[role]
            return f"postgresql+psycopg://{role}:{password}@{host}:{port}/{DB_NAME}"

        urls = DatabaseUrls(url("hooks_owner"), url("hooks_api"), url("hooks_worker"))
        wait_until_connectable(urls.owner)

        os.environ["HOOKS_MIGRATION_DATABASE_URL"] = urls.owner
        command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
        yield urls


@pytest.fixture(scope="session")
def owner_engine(database_urls: DatabaseUrls) -> Iterator[Engine]:
    engine = make_engine(database_urls.owner)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def api_engine(database_urls: DatabaseUrls) -> Iterator[Engine]:
    engine = make_engine(database_urls.api)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def worker_engine(database_urls: DatabaseUrls) -> Iterator[Engine]:
    engine = make_engine(database_urls.worker)
    yield engine
    engine.dispose()


def seed_tenant(engine: Engine, name: str) -> SeededTenant:
    """Insert one row in every tenant table. Runs under RLS like any other writer."""
    tenant_id, event_id, lease_token = uuid4(), uuid4(), uuid4()
    with tenant_tx(engine, tenant_id) as session:
        session.add(Tenant(id=tenant_id, name=name))
        session.flush()
        session.add(Endpoint(tenant_id=tenant_id, provider=PROVIDER))
        session.flush()
        session.add_all(
            [
                SigningSecret(tenant_id=tenant_id, provider=PROVIDER, ciphertext=b"fake"),
                ApiKey(tenant_id=tenant_id, name="demo", key_hash=b"fake"),
                EventHandler(
                    tenant_id=tenant_id,
                    provider=PROVIDER,
                    event_type="order.created",
                    handler_name="demo",
                ),
                SeenDelivery(
                    tenant_id=tenant_id,
                    provider=PROVIDER,
                    delivery_id="dlv_1",
                    expires_at=text("now() + interval '10 minutes'"),
                ),
                Event(
                    id=event_id,
                    tenant_id=tenant_id,
                    provider=PROVIDER,
                    provider_event_id="evt_1",
                    event_type="order.created",
                    raw_body=b"{}",
                ),
            ]
        )
        session.flush()
        session.add(
            EventAttempt(
                tenant_id=tenant_id,
                lease_token=lease_token,
                event_id=event_id,
                attempt_no=1,
                worker_id="worker-1",
            )
        )
    return SeededTenant(tenant_id, event_id, lease_token)


@pytest.fixture
def tenants(owner_engine: Engine) -> Iterator[TwoTenants]:
    yield TwoTenants(seed_tenant(owner_engine, "tenant-a"), seed_tenant(owner_engine, "tenant-b"))
    # TRUNCATE is not filtered by RLS, which is why only the owner is granted it.
    with owner_engine.begin() as connection:
        connection.execute(text("TRUNCATE tenants CASCADE"))
