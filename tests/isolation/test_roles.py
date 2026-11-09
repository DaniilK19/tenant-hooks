"""The configuration that RLS depends on. If any of this drifts, isolation is gone."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from tenant_hooks.db import tenant_tx
from tenant_hooks.models import Base
from tests.conftest import REPO_ROOT, DatabaseUrls, TwoTenants

ROLES = ["hooks_owner", "hooks_api", "hooks_worker", "hooks_claimer"]
TENANT_TABLES = sorted(Base.metadata.tables)


@pytest.mark.parametrize("role", ROLES)
def test_role_cannot_bypass_rls(owner_engine: Engine, role: str) -> None:
    with owner_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                "FROM pg_roles WHERE rolname = :role"
            ),
            {"role": role},
        ).one()
    assert tuple(row) == (False, False, False, False)


def test_claimer_role_cannot_log_in(owner_engine: Engine) -> None:
    with owner_engine.connect() as connection:
        can_login = connection.execute(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'hooks_claimer'")
        ).scalar_one()
    assert can_login is False


def test_every_table_except_alembics_has_forced_rls_and_a_policy(owner_engine: Engine) -> None:
    with owner_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       pg_get_userbyid(c.relowner) AS owner,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                  AND c.relname <> 'alembic_version'
                """
            )
        ).all()
    assert sorted(row.relname for row in rows) == TENANT_TABLES
    for row in rows:
        assert row.relrowsecurity, row.relname
        assert row.relforcerowsecurity, row.relname
        assert row.owner == "hooks_owner", row.relname
        assert row.policies >= 1, row.relname


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM events",
        "TRUNCATE events",
        "ALTER TABLE events DISABLE ROW LEVEL SECURITY",
        "DROP POLICY tenant_isolation ON events",
        "SET ROLE hooks_owner",
        "SET ROLE hooks_claimer",
        "INSERT INTO tenants (name) VALUES ('rogue')",
    ],
)
def test_api_role_cannot_escalate(api_engine: Engine, statement: str) -> None:
    with pytest.raises(ProgrammingError, match="permission denied|must be owner"):
        with api_engine.begin() as connection:
            connection.execute(text(statement))


def test_api_role_cannot_read_alembic_version(api_engine: Engine) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        with api_engine.begin() as connection:
            connection.execute(text("SELECT * FROM alembic_version"))


def test_malformed_tenant_id_fails_closed(api_engine: Engine, tenants: TwoTenants) -> None:
    with pytest.raises(Exception, match="invalid input syntax for type uuid"):
        with api_engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', 'not-a-uuid', true)"))
            connection.execute(text("SELECT * FROM events"))


def test_tenant_tx_commits_are_visible(api_engine: Engine, tenants: TwoTenants) -> None:
    with tenant_tx(api_engine, tenants.a.id) as session:
        session.execute(text("UPDATE events SET last_error = 'seen'"))
    with tenant_tx(api_engine, tenants.a.id) as session:
        assert session.execute(text("SELECT last_error FROM events")).scalar_one() == "seen"


def test_owner_does_not_inherit_the_claimer_policies(owner_engine: Engine) -> None:
    """A policy TO hooks_claimer applies to anyone inheriting it, so the owner must not."""
    with owner_engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT m.inherit_option, m.set_option
                FROM pg_auth_members m
                WHERE m.roleid = 'hooks_claimer'::regrole AND m.member = 'hooks_owner'::regrole
                """
            )
        ).one()
    assert row.inherit_option is False
    assert row.set_option is True


def test_only_the_claimer_is_covered_by_unrestricted_policies(owner_engine: Engine) -> None:
    with owner_engine.connect() as connection:
        roles = connection.execute(
            text(
                """
                SELECT DISTINCT r.rolname
                FROM pg_policy p
                CROSS JOIN LATERAL unnest(p.polroles) AS grantee(oid)
                JOIN pg_roles r ON r.oid = grantee.oid
                WHERE pg_get_expr(p.polqual, p.polrelid) = 'true'
                """
            )
        ).scalars()
        assert list(roles) == ["hooks_claimer"]


def test_migrations_are_reversible(database_urls: DatabaseUrls) -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    try:
        command.downgrade(config, "base")
    finally:
        command.upgrade(config, "head")


def test_models_match_migrations(owner_engine: Engine) -> None:
    with owner_engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []
