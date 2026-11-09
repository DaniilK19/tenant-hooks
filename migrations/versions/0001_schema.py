"""Create the tenant-scoped schema.

Foreign keys between tenant tables are composite and include tenant_id, because
foreign key checks bypass RLS. A plain key could point at another tenant's row.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

NOW = sa.text("now()")
NEW_UUID = sa.text("gen_random_uuid()")


def timestamp(name: str, *, nullable: bool = False, default: bool = False) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=NOW if default else None,
    )


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=NEW_UUID),
        sa.Column("name", sa.Text(), nullable=False),
        timestamp("created_at", default=True),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
    )

    op.create_table(
        "endpoints",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        timestamp("created_at", default=True),
        sa.PrimaryKeyConstraint("tenant_id", "provider", name="pk_endpoints"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_endpoints_tenant"),
    )

    op.create_table(
        "signing_secrets",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=NEW_UUID),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        timestamp("created_at", default=True),
        timestamp("expires_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_signing_secrets"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["endpoints.tenant_id", "endpoints.provider"],
            name="fk_signing_secrets_endpoint",
        ),
    )
    op.create_index("ix_signing_secrets_endpoint", "signing_secrets", ["tenant_id", "provider"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=NEW_UUID),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(), nullable=False),
        timestamp("created_at", default=True),
        timestamp("revoked_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.UniqueConstraint("tenant_id", "key_hash", name="uq_api_keys_tenant_hash"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_api_keys_tenant"),
    )

    op.create_table(
        "event_handlers",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("handler_name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("tenant_id", "provider", "event_type", name="pk_event_handlers"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["endpoints.tenant_id", "endpoints.provider"],
            name="fk_event_handlers_endpoint",
        ),
    )

    op.create_table(
        "seen_deliveries",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=False),
        timestamp("expires_at"),
        sa.PrimaryKeyConstraint("tenant_id", "provider", "delivery_id", name="pk_seen_deliveries"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["endpoints.tenant_id", "endpoints.provider"],
            name="fk_seen_deliveries_endpoint",
        ),
    )
    op.create_index("ix_seen_deliveries_expires_at", "seen_deliveries", ["expires_at"])

    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=NEW_UUID),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("raw_body", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        timestamp("next_attempt_at", default=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("leased_by", sa.Text(), nullable=True),
        timestamp("lease_expires_at", nullable=True),
        sa.Column("replay_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        timestamp("received_at", default=True),
        timestamp("completed_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        # The idempotency guarantee: one row per event, per provider, per tenant.
        sa.UniqueConstraint(
            "tenant_id", "provider", "provider_event_id", name="uq_events_idempotency"
        ),
        # Target of the composite foreign key on event_attempts.
        sa.UniqueConstraint("tenant_id", "id", name="uq_events_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["endpoints.tenant_id", "endpoints.provider"],
            name="fk_events_endpoint",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'skipped', 'dead_lettered')",
            name="ck_events_status",
        ),
        sa.CheckConstraint(
            "(status = 'processing') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_events_lease_matches_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_events_attempt_count"),
        sa.CheckConstraint("max_attempts > 0", name="ck_events_max_attempts"),
    )
    op.create_index(
        "ix_events_claimable",
        "events",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_events_expired_leases",
        "events",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )

    op.create_table(
        "event_attempts",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        timestamp("started_at", default=True),
        timestamp("finished_at", nullable=True),
        # NULL means the worker died before reporting a result.
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "lease_token", name="pk_event_attempts"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "event_id"],
            ["events.tenant_id", "events.id"],
            name="fk_event_attempts_event",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded', 'failed', 'lease_lost')",
            name="ck_event_attempts_outcome",
        ),
    )
    op.create_index("ix_event_attempts_event", "event_attempts", ["tenant_id", "event_id"])


def downgrade() -> None:
    for table in (
        "event_attempts",
        "events",
        "seen_deliveries",
        "event_handlers",
        "api_keys",
        "signing_secrets",
        "endpoints",
        "tenants",
    ):
        op.drop_table(table)
