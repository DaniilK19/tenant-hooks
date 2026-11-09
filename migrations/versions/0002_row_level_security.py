"""Enable row-level security and grant least privilege to the runtime roles.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Table name -> the column that identifies the owning tenant.
TENANT_COLUMNS = {
    "tenants": "id",
    "endpoints": "tenant_id",
    "signing_secrets": "tenant_id",
    "api_keys": "tenant_id",
    "event_handlers": "tenant_id",
    "seen_deliveries": "tenant_id",
    "events": "tenant_id",
    "event_attempts": "tenant_id",
}

API_GRANTS = {
    "tenants": "SELECT",
    "endpoints": "SELECT",
    "signing_secrets": "SELECT",
    "api_keys": "SELECT",
    "event_handlers": "SELECT",
    "seen_deliveries": "SELECT, INSERT",
    "events": "SELECT, INSERT, UPDATE",
    "event_attempts": "SELECT",
}

WORKER_GRANTS = {
    "tenants": "SELECT",
    "event_handlers": "SELECT",
    "events": "SELECT, UPDATE",
    "event_attempts": "SELECT, UPDATE",
}


def upgrade() -> None:
    # NULLIF matters: once SET LOCAL has ended, a custom setting reads as '' on
    # that connection instead of NULL, and ''::uuid would raise an error.
    op.execute(
        """
        CREATE FUNCTION current_tenant() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$
        """
    )

    for table, column in TENANT_COLUMNS.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # Without FORCE, the table owner silently skips every policy.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            FOR ALL TO hooks_owner, hooks_api, hooks_worker
            USING ({column} = current_tenant())
            WITH CHECK ({column} = current_tenant())
            """
        )

    for table, privileges in API_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO hooks_api")
    for table, privileges in WORKER_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO hooks_worker")


def downgrade() -> None:
    for table in TENANT_COLUMNS:
        op.execute(f"REVOKE ALL ON {table} FROM hooks_api, hooks_worker")
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION current_tenant()")
