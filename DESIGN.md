# Design decisions

Each entry states the choice and why it was made. Entries marked *(planned)* are
decided but land in a later phase.

## Data access

**Sync SQLAlchemy with psycopg 3.** Each request is one short transaction, so the
database is the bottleneck, not the event loop. Sync keeps the tenant transaction and
the worker as plain linear code, and handlers do not have to be async-safe. The cost is
threadpool concurrency, which is bounded by matching it to the connection pool.

## Tenant isolation

**Row-level security on every table.** Each table has one policy comparing its tenant
column to `current_tenant()`, for both `USING` and `WITH CHECK`. An unset tenant gives
NULL, the comparison is never true, and the query returns no rows, so the failure mode
is closed. `WITH CHECK` blocks inserts into, and updates moving rows to, another tenant.

**`NULLIF` in `current_tenant()`.** After `SET LOCAL` ends, a custom setting reads as
`''` on that connection rather than NULL. Without `NULLIF`, `''::uuid` would raise an
error on every later unscoped query on a reused connection.

**Runtime roles have no BYPASSRLS and own nothing.** Superusers and `BYPASSRLS` roles
skip every policy even with `FORCE`. Owners skip policies unless `FORCE` is set. If the
service ran as either, a forgotten `WHERE` clause would be a real cross-tenant leak.

**`FORCE ROW LEVEL SECURITY`.** This also subjects the owner to policies, so a mistake
made through the migration role is contained too. It is why tests can run as the owner:
the owner holds every table privilege, so zero rows can only come from RLS.

**The tenant is set per transaction.** A session-level `SET` stays on the connection
when it returns to the pool, so the next request would inherit the previous tenant.
`SET LOCAL` ends with the transaction, including on rollback, and also works with
transaction-mode PgBouncer. The code calls `set_config('app.tenant_id', :id, true)`,
which has the same semantics but accepts a bind parameter. A negative-control test shows
that the session-level form does leak.

**Composite foreign keys.** Foreign key and uniqueness checks bypass RLS by design. A
plain `event_id` key could point at another tenant's row, so child tables reference
`(tenant_id, id)`. Unique keys include `tenant_id` so that a constraint violation cannot
reveal whether a value exists for another tenant.

**Least-privilege grants.** RLS decides which rows a role sees, and grants decide what
it can do to them. `hooks_api` cannot delete events, and neither runtime role can
`TRUNCATE`, which RLS does not filter.

**Tenant provisioning runs as the owner under RLS.** The new tenant's id is set first,
so `WITH CHECK` passes. `hooks_api` has no `INSERT` on `tenants`, so a service bug cannot
create tenants, and no policy exception is needed.

### Cross-tenant claiming *(planned)*

| Option | Upside | Downside |
|---|---|---|
| Worker role with `USING (true)` | Simplest | Any worker or handler query can read every tenant |
| `SECURITY DEFINER` owned by the table owner | Narrow surface | Blocked by `FORCE` unless the owner bypasses RLS |
| Claim-only definer function, owned by a dedicated role | Cross-tenant access is one small audited function | One extra NOLOGIN role |

The third option is chosen. `hooks_claimer` cannot log in, owns the function, and has its
own policy with only the grants the function needs. The function returns only ids and a
lease token, and the worker does everything else under ordinary tenant-scoped RLS.

## Intake *(planned)*

**Claim, then prove.** The URL carries the tenant UUID. The service sets that tenant,
reads its secrets under RLS, and verifies the HMAC. An unknown tenant finds no rows and
gets the same 401 as a bad signature, so pre-auth needs no cross-tenant lookup.

**Unique constraint, not an application check.** Check-then-insert has a race window
between the two statements under READ COMMITTED. With `INSERT ... ON CONFLICT DO NOTHING`
the database serializes concurrent inserts on the index, and the losers wait for the
winner's commit. No application bug can create a second row.

**Replay and duplicate are different things.** A replay is the same signed delivery sent
again, caught by `seen_deliveries` and answered with 409. A duplicate is a new delivery of
the same event, caught by the unique constraint and answered with 200. Both inserts share
one transaction, so a crash cannot record a delivery without its event.

## Processing *(planned)*

**`FOR UPDATE SKIP LOCKED`.** Plain `FOR UPDATE` makes workers queue behind one row, and
`NOWAIT` makes them fail. `SKIP LOCKED` lets each worker take the next free row, so the
table works as a queue without a broker.

**A lease instead of holding the row lock.** Holding the lock during a handler ties up a
connection per in-flight event, and a rollback after a crash erases the attempt counter,
so a poison event could crash workers forever. The claim commits `attempt_count + 1` and a
lease, the handler runs outside any transaction, and completion is fenced by
`WHERE lease_token = :mine`. Lease expiry uses the database clock.

**Backoff with full jitter.** The delay is `random(0, min(cap, base * 2^(n-1)))`. Without
jitter, events that failed together during an outage retry together and hit the recovering
dependency in synchronized waves.

**Delivery semantics.** Intake is exactly-once. Handler execution is at-least-once,
because a worker killed mid-handler means that handler started twice. Handlers receive the
event id as an idempotency key.

## Known limitations

- RLS stops forgotten filters and logic bugs. It does not stop a bug that sets the wrong
  tenant, or arbitrary SQL injection, because the runtime role can set `app.tenant_id`
  itself. Only `tenant_hooks.db.tenant_tx` sets it, which keeps that surface small.
- Compose defaults are fake local values so that one command works. They are not suitable
  for anything beyond a local machine.
