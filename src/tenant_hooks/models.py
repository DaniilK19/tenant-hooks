"""ORM mappings. The migrations in migrations/versions are the source of truth for
constraints that SQLAlchemy cannot express, and a test keeps the two in sync."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NEW_UUID = text("gen_random_uuid()")
Timestamp = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (PrimaryKeyConstraint("id", name="pk_tenants"),)

    id: Mapped[UUID] = mapped_column(Uuid, server_default=NEW_UUID)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "provider", name="pk_endpoints"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_endpoints_tenant"),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    provider: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class SigningSecret(Base):
    __tablename__ = "signing_secrets"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_signing_secrets"),
        ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["endpoints.tenant_id", "endpoints.provider"],
            name="fk_signing_secrets_endpoint",
        ),
        Index("ix_signing_secrets_endpoint", "tenant_id", "provider"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, server_default=NEW_UUID)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    provider: Mapped[str] = mapped_column(Text)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(Timestamp)


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_api_keys"),
        UniqueConstraint("tenant_id", "key_hash", name="uq_api_keys_tenant_hash"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_api_keys_tenant"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, server_default=NEW_UUID)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp)


class EventHandler(Base):
    __tablename__ = "event_handlers"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "provider", "event_type", name="pk_event_handlers"),
        ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["endpoints.tenant_id", "endpoints.provider"],
            name="fk_event_handlers_endpoint",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    provider: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    handler_name: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())


class SeenDelivery(Base):
    __tablename__ = "seen_deliveries"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "provider", "delivery_id", name="pk_seen_deliveries"),
        ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["endpoints.tenant_id", "endpoints.provider"],
            name="fk_seen_deliveries_endpoint",
        ),
        Index("ix_seen_deliveries_expires_at", "expires_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    provider: Mapped[str] = mapped_column(Text)
    delivery_id: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(Timestamp)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_events"),
        UniqueConstraint(
            "tenant_id", "provider", "provider_event_id", name="uq_events_idempotency"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_events_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["endpoints.tenant_id", "endpoints.provider"],
            name="fk_events_endpoint",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'skipped', 'dead_lettered')",
            name="ck_events_status",
        ),
        CheckConstraint(
            "(status = 'processing') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_events_lease_matches_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_events_attempt_count"),
        CheckConstraint("max_attempts > 0", name="ck_events_max_attempts"),
        Index(
            "ix_events_claimable",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_events_expired_leases",
            "lease_expires_at",
            postgresql_where=text("status = 'processing'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, server_default=NEW_UUID)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    provider: Mapped[str] = mapped_column(Text)
    provider_event_id: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    raw_body: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    next_attempt_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    leased_by: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(Timestamp)
    replay_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)


class EventAttempt(Base):
    __tablename__ = "event_attempts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "lease_token", name="pk_event_attempts"),
        ForeignKeyConstraint(
            ["tenant_id", "event_id"],
            ["events.tenant_id", "events.id"],
            name="fk_event_attempts_event",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded', 'failed', 'lease_lost')",
            name="ck_event_attempts_outcome",
        ),
        Index("ix_event_attempts_event", "tenant_id", "event_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    lease_token: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    attempt_no: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)
    outcome: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
