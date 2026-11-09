"""Return events held by dead or stalled workers to the queue.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

FUNCTION = "reclaim_expired_leases(integer)"

# next_attempt_at is left alone on purpose. It is already in the past, so a reclaimed
# event goes to the front of the queue, and the lease timeout has served as its backoff.
RECLAIM_EXPIRED_LEASES = """
CREATE FUNCTION reclaim_expired_leases(p_limit integer)
RETURNS integer
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH expired AS (
        SELECT e.id
        FROM events e
        WHERE e.status = 'processing' AND e.lease_expires_at <= now()
        ORDER BY e.lease_expires_at
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    ), reclaimed AS (
        UPDATE events e
        SET status = CASE
                WHEN e.attempt_count >= e.max_attempts THEN 'dead_lettered'
                ELSE 'pending'
            END,
            lease_token = NULL,
            leased_by = NULL,
            lease_expires_at = NULL,
            last_error = 'lease expired before the worker reported a result',
            completed_at = CASE WHEN e.attempt_count >= e.max_attempts THEN now() END
        FROM expired
        WHERE e.id = expired.id
        RETURNING 1
    )
    SELECT count(*)::integer FROM reclaimed
$$
"""


def upgrade() -> None:
    # Still column-level, so raw_body stays out of reach.
    op.execute("GRANT SELECT (lease_expires_at, max_attempts) ON events TO hooks_claimer")
    op.execute("GRANT UPDATE (last_error, completed_at) ON events TO hooks_claimer")

    op.execute(RECLAIM_EXPIRED_LEASES)
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO hooks_worker")

    # Same ownership transfer as in 0003: CREATE is held only for this moment.
    op.execute("GRANT CREATE ON SCHEMA public TO hooks_claimer")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO hooks_claimer")
    op.execute("REVOKE CREATE ON SCHEMA public FROM hooks_claimer")


def downgrade() -> None:
    op.execute("SET LOCAL ROLE hooks_claimer")
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute("RESET ROLE")
    op.execute("REVOKE UPDATE (last_error, completed_at) ON events FROM hooks_claimer")
    op.execute("REVOKE SELECT (lease_expires_at, max_attempts) ON events FROM hooks_claimer")
