"""Add the narrow cross-tenant functions the worker uses.

These are the only code paths that see more than one tenant. They run as
hooks_claimer, which cannot log in and holds only the column grants they need. It
cannot read event payloads.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

CLAIM_EVENTS = """
CREATE FUNCTION claim_events(p_batch integer, p_lease_seconds integer, p_worker text)
RETURNS TABLE (event_id uuid, tenant_id uuid, lease_token uuid, attempt_no integer)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH picked AS (
        SELECT e.id
        FROM events e
        WHERE e.status = 'pending' AND e.next_attempt_at <= now()
        ORDER BY e.next_attempt_at
        LIMIT p_batch
        FOR UPDATE SKIP LOCKED
    ), claimed AS (
        UPDATE events e
        SET status = 'processing',
            attempt_count = e.attempt_count + 1,
            lease_token = gen_random_uuid(),
            leased_by = p_worker,
            lease_expires_at = now() + make_interval(secs => p_lease_seconds)
        FROM picked
        WHERE e.id = picked.id
        RETURNING e.id, e.tenant_id, e.lease_token, e.attempt_count
    ), logged AS (
        INSERT INTO event_attempts (tenant_id, lease_token, event_id, attempt_no, worker_id)
        SELECT c.tenant_id, c.lease_token, c.id, c.attempt_count, p_worker
        FROM claimed c
    )
    SELECT c.id, c.tenant_id, c.lease_token, c.attempt_count FROM claimed c
$$
"""

PURGE_EXPIRED_DELIVERIES = """
CREATE FUNCTION purge_expired_deliveries()
RETURNS integer
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH deleted AS (
        DELETE FROM seen_deliveries WHERE expires_at < now() RETURNING 1
    )
    SELECT count(*)::integer FROM deleted
$$
"""

FUNCTIONS = ("claim_events(integer, integer, text)", "purge_expired_deliveries()")
CLAIMER_TABLES = ("events", "event_attempts", "seen_deliveries")
OUTCOME_CHECK = "ck_event_attempts_outcome"


def replace_outcome_check(values: str) -> None:
    op.execute(f"ALTER TABLE event_attempts DROP CONSTRAINT {OUTCOME_CHECK}")
    op.execute(
        f"ALTER TABLE event_attempts ADD CONSTRAINT {OUTCOME_CHECK} "
        f"CHECK (outcome IS NULL OR outcome IN ({values}))"
    )


def upgrade() -> None:
    replace_outcome_check("'succeeded', 'failed', 'skipped', 'lease_lost'")

    # What this role may do is limited by the grants below, so the policies only need
    # to let it past the tenant filter.
    for table in CLAIMER_TABLES:
        op.execute(
            f"CREATE POLICY claimer_access ON {table} "
            "FOR ALL TO hooks_claimer USING (true) WITH CHECK (true)"
        )

    # Column-level on purpose: raw_body is not listed, so payloads stay out of reach.
    op.execute(
        "GRANT SELECT (id, tenant_id, status, next_attempt_at, attempt_count, lease_token) "
        "ON events TO hooks_claimer"
    )
    op.execute(
        "GRANT UPDATE (status, attempt_count, lease_token, leased_by, lease_expires_at) "
        "ON events TO hooks_claimer"
    )
    op.execute("GRANT INSERT ON event_attempts TO hooks_claimer")
    op.execute("GRANT SELECT (expires_at), DELETE ON seen_deliveries TO hooks_claimer")

    op.execute(CLAIM_EVENTS)
    op.execute(PURGE_EXPIRED_DELIVERIES)

    # Privileges come first: after the transfer, hooks_owner no longer controls them.
    # Functions are executable by PUBLIC unless that is revoked.
    for function in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO hooks_worker")

    # A new owner needs CREATE on the schema, but only for the moment of the transfer.
    op.execute("GRANT CREATE ON SCHEMA public TO hooks_claimer")
    for function in FUNCTIONS:
        op.execute(f"ALTER FUNCTION {function} OWNER TO hooks_claimer")
    op.execute("REVOKE CREATE ON SCHEMA public FROM hooks_claimer")


def downgrade() -> None:
    # Only the function owner can drop them, and hooks_owner does not inherit that role.
    op.execute("SET LOCAL ROLE hooks_claimer")
    for function in FUNCTIONS:
        op.execute(f"DROP FUNCTION {function}")
    op.execute("RESET ROLE")
    op.execute("REVOKE ALL ON seen_deliveries FROM hooks_claimer")
    op.execute("REVOKE ALL ON event_attempts FROM hooks_claimer")
    op.execute("REVOKE ALL ON events FROM hooks_claimer")
    for table in CLAIMER_TABLES:
        op.execute(f"DROP POLICY claimer_access ON {table}")
    replace_outcome_check("'succeeded', 'failed', 'lease_lost'")
