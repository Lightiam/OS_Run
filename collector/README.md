# Collector

The LightOS host-metrics collector, kept here as its own subsystem because it
is separate from the app. An agent on a Linux box reads its own host state and
POSTs it outbound over HTTPS. The box needs no inbound port, no public address
and no port forwarding.

One collector environment is one `lfc_` key.

## What Lovable points at

This repository:

```
https://github.com/Lightiam/OS_Run
```

Everything the server needs is under `collector/server/`, and everything the
app needs is under `collector/app/`. Neither runs from here — `server/`
deploys to Supabase, `app/` is copied into the app's own tree. This directory
is the source of truth for both; nothing in it depends on the app repo.

| Path | What it is | Where it has to end up |
|---|---|---|
| `server/migrations/20261028000000_collector_ingest.sql` | Tables, RLS, key minting and revoking | `supabase/migrations/` |
| `server/functions/collector-ingest/index.ts` | The ingest endpoint | `supabase/functions/collector-ingest/` |
| `server/functions/_shared/collector-ingest-core.ts` | Its logic, runtime-agnostic so it is testable | `supabase/functions/_shared/` |
| `server/config.toml.patch` | Sets `verify_jwt = false` for the function | `supabase/config.toml` |
| `app/pages/Infrastructure.tsx` | Mint, list and revoke keys; view what arrived | `src/pages/` |
| `app/lib/collector/` | Data layer and formatting | `src/lib/collector/` |
| `app/tests/` | 89 tests over the endpoint, data layer and formatting | `src/test/` |
| `app/wiring.patch` | The route, the sidebar entry, and the generated Supabase types | apply with `git apply` |
| `lightcollect/` | The agent that runs on the collector box | the box, as one file |
| `docs/collector.md` | How the pieces fit together | `docs/` |

`wiring.patch` and `config.toml.patch` are real patches against the app repo's
`main`, so `git apply` them rather than hand-editing. The types entry matters:
without it `.from("collector_environments")` does not typecheck, because the
generated types file predates these tables.

## Running it locally

A working ingest server with no deployment, no Docker and no `npm install` —
it uses `node:sqlite` and Node 24 TypeScript stripping, both built in. It
imports `handleIngest` from the same `_shared/collector-ingest-core.ts` the
edge function uses, so the contract it enforces is the deployed one.

```bash
cd collector/server/local
node server.ts --mint my-box     # prints the key once
node server.ts                   # serves on 8787
```

Open <http://localhost:8787> for the environment list with derived health.
Point an agent at it with `LIGHTOS_URL=http://localhost:8787`.

| Route | What it does |
|---|---|
| `POST /functions/v1/collector-ingest` | The real endpoint, same path as production |
| `GET /` | Environment list, health, last seen; refreshes every 10s |
| `GET /api/environments` | The same as JSON |
| `GET /api/measurements?environment=<id>` | Recent measurements |
| `POST /api/environments` | Mint, `{"name":"..."}` |
| `POST /api/environments/<id>/revoke` | Revoke |

Measurements land in `collector.db` beside the server, which is gitignored.
Delete it to start clean.

What this is not: there is no auth, so every environment belongs to one
stand-in user. It is for proving the wire contract and developing the agent,
not for holding real data.

### Running the agent where there is no /proc

The agent reads `/proc`, so on Windows or macOS point it at a fixture tree
instead, which is what its own tests use:

```bash
python lightcollect.py --print --root /path/to/fakeroot
```

`collector/lightcollect/tests/test_lightcollect.py` has `write_proc_root()`,
which writes a usable tree. Readings then describe the fixture, not the
machine, which is fine for checking delivery and wrong for anything else.

## Deploying

```bash
supabase db push
supabase functions deploy collector-ingest
```

The ingest endpoint is then:

```
https://<project-ref>.supabase.co/functions/v1/collector-ingest
```

A `GET` on it returning **405** means it is deployed and refusing the wrong
method, which is the signal to look for. **404** means it was never deployed.

That host, not the app's own domain, is what goes in the box's
`LIGHTOS_URL`. See [`../collector-box-runbook.md`](../collector-box-runbook.md).

## Keys

`collector_environment_create(name)` returns the plaintext key exactly once and
stores only its SHA-256 plus a short display prefix, so a database leak cannot
be replayed against the endpoint. `key_hash` is revoked from `authenticated` at
column level, so the browser cannot read it even with a valid session, and the
data layer names its columns rather than selecting `*` to keep the query and
that grant in step. A test asserts every requested column is one the migration
actually grants.

`collector_environment_revoke(id)` is scoped to `auth.uid()`. A revoked key
starts failing immediately and the agent spools rather than dropping what it
collects.

## Two decisions worth knowing

Both follow from the agent having an offline spool:

- **A malformed line does not fail the batch.** The agent retries anything it
  was not told was delivered, so rejecting a whole batch over one bad line
  would wedge its spool behind that line forever. Bad lines are counted by
  reason and dropped, and the response is still 202.
- **Ingest is idempotent**, via `unique (environment_id, hostname,
  collected_at)`. If a response is lost after the insert lands, the agent
  re-sends and the rows count as duplicates instead of storing twice.

## Tests

The agent, from this directory:

```bash
python -m unittest discover -s lightcollect/tests -t .
```

Run it from here, not from inside `lightcollect/` — that directory contains
`lightcollect.py`, which shadows the `lightcollect` package and makes the
import fail. A line reading `delivery failed; spooling` is a test exercising
the offline spool, not a failure.

The TypeScript suites under `app/tests/` run with the app's own vitest, once
copied into `src/test/`.

## Status

The migration has never been applied to any project, so the endpoint returns
404 and the Infrastructure page has no tables to read. That is the next step,
and it is the only thing standing between this and a working collector.
