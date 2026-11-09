"""Fixtures for tests that go through the real HTTP app."""

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tenant_hooks.api.app import create_app
from tenant_hooks.config import Settings
from tenant_hooks.security.secret_box import SecretBox
from tests.conftest import DatabaseUrls, truncate_all
from tests.intake.helpers import SigningTenant, provision

TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 2048


@pytest.fixture(scope="session")
def master_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture(scope="session")
def box(master_key: str) -> SecretBox:
    return SecretBox(master_key)


@pytest.fixture
def client(database_urls: DatabaseUrls, master_key: str) -> Iterator[TestClient]:
    """The real app, connected as the restricted hooks_api role."""
    settings = Settings(
        database_url=database_urls.api,
        secret_key=master_key,
        signature_tolerance_seconds=TOLERANCE_SECONDS,
        max_body_bytes=MAX_BODY_BYTES,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def clean_db(owner_engine: Engine) -> Iterator[None]:
    yield
    truncate_all(owner_engine)


@pytest.fixture
def tenant(owner_engine: Engine, box: SecretBox, clean_db: None) -> SigningTenant:
    return provision(owner_engine, box, "tenant-a")


@pytest.fixture
def other_tenant(owner_engine: Engine, box: SecretBox, clean_db: None) -> SigningTenant:
    return provision(owner_engine, box, "tenant-b")
