#!/bin/bash
# Creates the database roles. Runs once, as superuser, when the data directory
# is first initialised. The test suite mounts this same file, so tests exercise
# the real role setup rather than a copy of it.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  -v db="$POSTGRES_DB" \
  -v owner_password="$HOOKS_OWNER_PASSWORD" \
  -v api_password="$HOOKS_API_PASSWORD" \
  -v worker_password="$HOOKS_WORKER_PASSWORD" <<'SQL'
-- Owns the schema and runs migrations. Still subject to RLS through FORCE.
CREATE ROLE hooks_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
  PASSWORD :'owner_password';

-- Used by the HTTP service.
CREATE ROLE hooks_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
  PASSWORD :'api_password';

-- Used by the background worker.
CREATE ROLE hooks_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
  PASSWORD :'worker_password';

-- Cannot log in. Will own the narrow SECURITY DEFINER claim functions.
CREATE ROLE hooks_claimer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- Lets migrations hand function ownership to hooks_claimer.
GRANT hooks_claimer TO hooks_owner;

ALTER DATABASE :"db" OWNER TO hooks_owner;
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO hooks_api, hooks_worker;
SQL
