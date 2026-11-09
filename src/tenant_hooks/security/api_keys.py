"""API keys look like `th_<tenant uuid>.<secret>`.

The tenant part is only a claim. It selects which tenant's keys to check, under RLS,
so authentication needs no cross-tenant lookup. Keys have 256 bits of entropy, so plain
SHA-256 is enough. Slow hashes exist to protect low-entropy passwords.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenant_hooks.models import ApiKey

BEARER_FORMAT = re.compile(r"Bearer th_([0-9a-fA-F-]{36})\.([A-Za-z0-9_-]{32,128})")


@dataclass(frozen=True)
class Credentials:
    tenant_id: UUID
    secret: str


def hash_secret(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def generate(tenant_id: UUID) -> tuple[str, bytes]:
    """Return the key to show once, and the hash to store."""
    secret = secrets.token_urlsafe(32)
    return f"th_{tenant_id}.{secret}", hash_secret(secret)


def parse_bearer(header: str | None) -> Credentials | None:
    match = BEARER_FORMAT.fullmatch(header or "")
    if match is None:
        return None
    try:
        return Credentials(UUID(match[1]), match[2])
    except ValueError:
        return None


def is_valid(session: Session, secret: str) -> bool:
    """Check a key against the tenant set on this transaction. RLS scopes the query."""
    key_id = session.scalar(
        select(ApiKey.id).where(ApiKey.key_hash == hash_secret(secret), ApiKey.revoked_at.is_(None))
    )
    return key_id is not None
