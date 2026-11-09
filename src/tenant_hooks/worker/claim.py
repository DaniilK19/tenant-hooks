from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, text


@dataclass(frozen=True)
class Claim:
    event_id: UUID
    tenant_id: UUID
    lease_token: UUID
    attempt_no: int


def claim_events(engine: Engine, batch: int, lease_seconds: int, worker_id: str) -> list[Claim]:
    """Lease due events across tenants. The transaction commits before any handler runs."""
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT event_id, tenant_id, lease_token, attempt_no "
                "FROM claim_events(:batch, :lease_seconds, :worker_id)"
            ),
            {"batch": batch, "lease_seconds": lease_seconds, "worker_id": worker_id},
        ).all()
    return [Claim(row.event_id, row.tenant_id, row.lease_token, row.attempt_no) for row in rows]


def reclaim_expired_leases(engine: Engine, limit: int) -> int:
    """Requeue events whose worker stopped reporting, or dead-letter them if out of attempts."""
    with engine.begin() as connection:
        reclaimed = connection.execute(
            text("SELECT reclaim_expired_leases(:limit)"), {"limit": limit}
        ).scalar_one()
    return int(reclaimed)


def purge_expired_deliveries(engine: Engine) -> int:
    with engine.begin() as connection:
        return int(connection.execute(text("SELECT purge_expired_deliveries()")).scalar_one())
