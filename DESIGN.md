# Design decisions

Each entry states the choice and why it was made.

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

### Cross-tenant claiming

| Option | Upside | Downside |
|---|---|---|
| Worker role with `USING (true)` | Simplest | Any worker or handler query can read every tenant |
| `SECURITY DEFINER` owned by the table owner | Narrow surface | Blocked by `FORCE` unless the owner bypasses RLS |
| Narrow definer functions, owned by a dedicated role | Cross-tenant access is limited to a few small audited functions | One extra NOLOGIN role |

The third option is chosen. `hooks_claimer` cannot log in, owns the functions, and has its
own policy with only the grants they need. `claim_events` returns only ids and a lease
token, the recovery and purge functions return counts, and the worker does everything else
under ordinary tenant-scoped RLS.

**Why not the table owner as definer.** `FORCE ROW LEVEL SECURITY` applies to the owner, so
a function it owns would see no rows without a tenant. Giving the owner `BYPASSRLS` would
remove the protection that `FORCE` provides.

**`hooks_owner` is a non-inheriting member of `hooks_claimer`.** A policy written `TO role`
also applies to every role that inherits it. With ordinary membership, the owner picked up
the claimer's `USING (true)` and could see every tenant, which the isolation suite caught.
`WITH INHERIT FALSE, SET TRUE` still lets migrations transfer function ownership.

**Column-level grants for `hooks_claimer`.** Its policy passes every row, so the grants are
what limit it. `raw_body` is not granted and neither are the secrets tables, so a bug in
these functions cannot expose a payload. `hooks_api` cannot execute them at all.

**Hardened definer functions.** `search_path` is pinned, so a shadow object cannot be
substituted during privileged execution. `EXECUTE` is revoked from `PUBLIC`, because
functions are executable by everyone by default.

## Intake

**Claim, then prove.** The URL carries the tenant UUID. The service sets that tenant,
reads its secrets under RLS, and verifies the HMAC. An unknown tenant finds no rows and
gets the same 401 as a bad signature, so pre-auth needs no cross-tenant lookup.

**What is signed.** The signature covers `{timestamp}.{delivery_id}.{raw body}`, computed
over the exact bytes received, because re-serialized JSON may not match them. Signing the
timestamp stops an attacker refreshing an old request. Timestamps are digits only and
delivery ids cannot contain a dot, otherwise bytes could move between the delivery id and
the body while the signature stayed valid.

**Signature before timestamp.** The HMAC is checked first, so only a real signer can learn
that a timestamp was out of tolerance. Everyone else gets the same generic 401. The
comparison is constant-time, and on bytes because `compare_digest` raises on non-ASCII
strings, which would turn a hostile header into a 500.

**Encrypted signing secrets.** HMAC needs the original secret, so a one-way hash cannot
work. Secrets are Fernet-encrypted with a master key from the environment, so a database
dump alone does not expose them. Several active secrets per provider allow rotation
without dropping deliveries.

**Delivery retention is twice the tolerance, plus a margin.** A timestamp can be up to one
tolerance in the future, so a signed request can stay valid for two tolerances. A shorter
retention would forget a delivery id while its request could still be replayed.

**One transaction for the whole request.** Secrets, the delivery record and the event share
a transaction. A rejected request rolls back and does not use up its delivery id.

**The first payload wins.** A repeated event id with a different body is reported as a
duplicate, and the stored event is not changed. Only a holder of the signing secret can
produce that situation.

**Unique constraint, not an application check.** Check-then-insert has a race window
between the two statements under READ COMMITTED. With `INSERT ... ON CONFLICT DO NOTHING`
the database serializes concurrent inserts on the index, and the losers wait for the
winner's commit. No application bug can create a second row.

**Replay and duplicate are different things.** A replay is the same signed delivery sent
again, caught by `seen_deliveries` and answered with 409. A duplicate is a new delivery of
the same event, caught by the unique constraint and answered with 200. Both inserts share
one transaction, so a crash cannot record a delivery without its event.

## Processing

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

**Attempts are counted at claim time.** The claim commits before the handler runs, so the
count survives a crash. An event that kills every worker touching it still reaches
`max_attempts` and is dead-lettered, instead of crashing workers forever.

**Fencing with a lease token.** A worker that stalls past its lease can come back after
another one has taken over. Each claim writes a new token and results are written only
`WHERE lease_token = :mine`, so the late result changes nothing. The late worker's side
effects may still have happened, which is why handlers get an idempotency key.

**Reclaim is a separate step from claim.** One query over "pending or expired" would have
to sort both sets on every poll. Instead, expired leases go back to the queue first, and
claiming stays a single ordered scan of a partial index. A reclaimed event keeps its old
`next_attempt_at`, so it goes to the front of the queue. The lease timeout has already
acted as its backoff.

**Fixed 60-second lease, no heartbeat.** A short lease recovers faster but re-runs slow
handlers, and a long one does the reverse. Heartbeats would remove that trade-off but need
a second thread per handler and more failure modes, so handlers are expected to finish
within the lease and the limit is documented.

**Batch size defaults to 1.** A lease starts at claim time, so events waiting behind others
in a large batch burn their lease before they start. Concurrency comes from running more
workers, which is also what `SKIP LOCKED` is designed for.

**No binding means `skipped`, an unknown handler means failure.** A tenant that did not
subscribe to an event type made a valid choice, so retrying is pointless. A binding that
names a handler this worker lacks is a deployment problem, so the event retries and can
succeed once the handler ships.

**Handlers can declare a failure permanent.** Retrying a malformed order wastes attempts
and delays the operator, so `PermanentError` dead-letters immediately.

**Replay resets the attempt counter in one conditional `UPDATE`.** The status check is part
of the statement, so of two simultaneous replays only one takes effect. A replayed event
gets a full retry budget, and `replay_count` keeps the history.

**API keys are hashed with plain SHA-256.** Slow hashes protect low-entropy passwords from
offline guessing. A 256-bit random key cannot be guessed, so a fast hash loses nothing. Keys
carry the tenant id as a claim, using the same claim-then-prove pattern as intake.

## Operations

**Provisioning is a CLI, not an HTTP endpoint.** An admin route would need its own
authentication and would put tenant creation on the network-facing service. A command that
connects as `hooks_owner` keeps `hooks_api` unable to create tenants, and using it requires
the owner's database credentials. In compose it sits behind a profile, so `up` does not
start a container holding those credentials.

**Credentials are printed once, as shell assignments.** Signing secrets are encrypted and
API keys are hashed, so a key cannot be shown again later. `NAME=value` lines can be captured
with `eval` and need no JSON tool. Every printed value is generated by the service, and the
tenant name is not printed, so operator input never reaches that `eval`.

**The CLI applies the intake route's patterns.** A provider that fails the route's pattern
could never receive a webhook. Rejecting it when it is typed is better than creating an
endpoint that looks configured and never works.

**`bind` replaces an earlier binding and does not check that the handler exists.** The primary
key allows one handler per event type, so changing a handler is the same command as adding
one. The CLI and the worker can ship separately, so a binding may come before its handler,
which is why an unknown handler is retried at runtime instead of being rejected here.

## Known limitations

- RLS stops forgotten filters and logic bugs. It does not stop a bug that sets the wrong
  tenant, or arbitrary SQL injection, because the runtime role can set `app.tenant_id`
  itself. Only `tenant_hooks.db.tenant_tx` sets it, which keeps that surface small.
- One signature scheme is implemented. Providers with their own header formats would need
  a per-provider verifier.
- Senders must use a new delivery id for each attempt. A provider that retries with a
  byte-identical request gets 409, even though the event is already safely stored.
- An unknown tenant skips the HMAC computation, so its response can be slightly faster.
  Tenant ids are random UUIDs, so this is not treated as a practical way to enumerate them.
- There is no master key rotation. Changing `HOOKS_SECRET_KEY` makes stored secrets
  unreadable until they are re-encrypted.
- There is no lease heartbeat. A handler that outlives its lease can overlap with its
  replacement. The result is fenced, but side effects are not.
- A worker that dies after its handler succeeded, but before recording that, causes one more
  run. This is inherent to at-least-once execution.
- Each event type has one handler per tenant. Fan-out to several handlers is not implemented.
- Workers poll on an interval rather than using `LISTEN/NOTIFY`, so an idle system adds up
  to one poll interval of latency.
- Events are claimed oldest-due-first across all tenants, with no per-tenant fairness. A very
  busy tenant can delay others.
- Compose defaults are fake local values so that one command works. They are not suitable
  for anything beyond a local machine.
