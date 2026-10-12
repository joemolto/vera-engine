# Migrations

`001_baseline.sql` creates Vera's whole database schema on a fresh Supabase
project: tables, constraints, indexes, functions, row-level security and
storage buckets. It was generated on 2026-10-03 from the live schema and
replaces the 56 incremental migrations the hosted service grew up with,
which could not rebuild a database from empty.

Apply it once to a new project, either by pasting it into the Supabase SQL
editor or with psql:

    psql "$DATABASE_URL" -f migrations/001_baseline.sql

`002_require_mfa.sql` adds two-factor enforcement at the data layer: a
user with an authenticator set up can only read or write org data after
entering their code. Apply it after the baseline.

Put later changes in new numbered files (`003_name.sql`) and record each one:

    insert into public.schema_migrations (filename) values ('003_name.sql');

`scripts/schema_fingerprint.sql` prints one hash per part of the schema.
Run it on two databases to check their structure matches.
