"""Operator commands, run with `python -m tenant_hooks.cli`.

These connect as hooks_owner, the only role allowed to create tenants. That role is
still subject to RLS, so each command sets its tenant first, like any other writer.
"""

import argparse
import re
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from tenant_hooks.api.intake import EVENT_FIELD_PATTERN, PROVIDER_PATTERN
from tenant_hooks.config import Settings
from tenant_hooks.db import make_engine, tenant_tx
from tenant_hooks.models import ApiKey, Endpoint, EventHandler, SigningSecret, Tenant
from tenant_hooks.security import api_keys
from tenant_hooks.security.secret_box import SecretBox


@dataclass(frozen=True)
class NewTenant:
    id: UUID
    signing_secret: str
    api_key: str


def create_tenant(engine: Engine, box: SecretBox, name: str, provider: str) -> NewTenant:
    """Create a tenant with one endpoint, one signing secret and one API key.

    Everything shares a transaction, so a failure cannot leave a half-built tenant.
    """
    tenant_id = uuid4()
    signing_secret = secrets.token_urlsafe(32)
    api_key, key_hash = api_keys.generate(tenant_id)
    with tenant_tx(engine, tenant_id) as session:
        session.add(Tenant(id=tenant_id, name=name))
        session.flush()
        session.add(Endpoint(tenant_id=tenant_id, provider=provider))
        session.flush()
        session.add_all(
            [
                SigningSecret(
                    tenant_id=tenant_id,
                    provider=provider,
                    ciphertext=box.encrypt(signing_secret.encode()),
                ),
                ApiKey(tenant_id=tenant_id, name="default", key_hash=key_hash),
            ]
        )
    return NewTenant(tenant_id, signing_secret, api_key)


def bind_handler(
    engine: Engine, tenant_id: UUID, provider: str, event_type: str, handler_name: str
) -> None:
    """Route an event type to a handler, replacing any earlier binding for that type."""
    statement = insert(EventHandler).values(
        tenant_id=tenant_id, provider=provider, event_type=event_type, handler_name=handler_name
    )
    with tenant_tx(engine, tenant_id) as session:
        session.execute(
            statement.on_conflict_do_update(
                constraint="pk_event_handlers",
                set_={"handler_name": handler_name, "enabled": True},
            )
        )


def matching(pattern: str) -> Callable[[str], str]:
    """Apply the intake route's own rules, so no endpoint or binding is unreachable."""

    def check(value: str) -> str:
        if re.fullmatch(pattern, value) is None:
            raise argparse.ArgumentTypeError(f"must match {pattern}")
        return value

    return check


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tenant_hooks.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "create-tenant",
        help="create a tenant and print its credentials once, as shell assignments",
    )
    create.add_argument("name")
    create.add_argument("provider", type=matching(PROVIDER_PATTERN))

    bind = commands.add_parser("bind", help="route an event type to a handler")
    bind.add_argument("tenant_id", type=UUID)
    bind.add_argument("provider", type=matching(PROVIDER_PATTERN))
    bind.add_argument("event_type", type=matching(EVENT_FIELD_PATTERN))
    bind.add_argument("handler")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()  # type: ignore[call-arg]
    engine = make_engine(settings.database_url, pool_size=1)
    try:
        if args.command == "create-tenant":
            box = SecretBox(settings.secret_key)
            tenant = create_tenant(engine, box, args.name, args.provider)
            print(f"TENANT_ID={tenant.id}")
            print(f"SIGNING_SECRET={tenant.signing_secret}")
            print(f"API_KEY={tenant.api_key}")
            return 0

        try:
            bind_handler(engine, args.tenant_id, args.provider, args.event_type, args.handler)
        except IntegrityError:
            print(
                f"error: tenant {args.tenant_id} has no endpoint for provider {args.provider}",
                file=sys.stderr,
            )
            return 1
        print(f"bound {args.provider}/{args.event_type} to {args.handler}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
