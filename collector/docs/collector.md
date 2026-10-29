# Host metrics collector

A collector environment is one `lfc_` key. An agent on the customer's own
machine presents it and POSTs host metrics; the Infrastructure page mints,
lists and revokes those keys and shows what has arrived.

Every connection is outbound, so the collected machine needs no inbound port,
no public address and no port forwarding. That is the whole point of this
design: it works from behind a home or office router.

| Piece | Where |
|---|---|
| Agent | `collector/lightcollect/` (Python, standard library only) |
| Endpoint | `supabase/functions/collector-ingest/` + `supabase/functions/_shared/collector-ingest-core.ts` |
| Schema | `supabase/migrations/20261028000000_collector_ingest.sql` |
| UI | `src/pages/Infrastructure.tsx`, `/dashboard/infrastructure` |
| Data layer | `src/lib/collector/client.ts`, `src/lib/collector/format.ts` |
| Box setup | `collector-box-runbook.md` (hardware spec and Ubuntu walkthrough) |

## Keys

`collector_environment_create(name)` returns the plaintext key exactly once
and stores only `sha256(key)` plus a 12-character display prefix, the same way
`node_enrollments.code_hash` does. A database leak therefore cannot be replayed
against the endpoint. `key_hash` is revoked from `authenticated` at column
level, so the browser cannot read it even with a valid session, and
`src/lib/collector/client.ts` names its columns rather than selecting `*` so
the query and that grant stay in step.

`collector_environment_revoke(id)` is scoped to `auth.uid()`, so one user
cannot retire another's key. A revoked key starts returning 401 immediately;
the agent spools what it collects rather than dropping it.

## Ingest

`POST /functions/v1/collector-ingest` with `Authorization: Bearer lfc_...` and
newline-delimited JSON, one `lightos.collector.v1` measurement per line.

`verify_jwt = false` in `supabase/config.toml`, because agents present a key
and not a JWT. The key is checked **before** the body is parsed, so an
unauthenticated caller learns nothing about what would have been accepted.

Two decisions follow from the agent having an offline spool:

- **A bad line does not fail the batch.** The agent retries anything it was not
  told was delivered, so rejecting a whole batch over one malformed line would
  wedge its spool behind that line forever. Invalid lines are counted by reason
  and dropped, and the response is still 202 — including when every line was
  garbage, which comes back as `accepted: 0, rejected: n`.
- **Ingest is idempotent**, via `unique (environment_id, hostname,
  collected_at)` and `ignoreDuplicates`. If a response is lost after the insert
  lands, the agent re-sends and the rows are counted as duplicates instead of
  stored twice.

The reply is `{ environment_id, accepted, duplicates, rejected, reasons,
truncated }`.

## Deploying

```
supabase db push
supabase functions deploy collector-ingest
```

Then mint a key in Infrastructure and follow the runbook on the box. Until the
migration is applied the page has no tables to read and will report a failure
rather than render empty.

## Tests

```
npm test
```

`src/test/collector-ingest.test.ts` covers the request contract,
`collector-client.test.ts` the data layer, and `collector-format.test.ts` the
rendering of a payload — including that `cpu.utilization` is a 0..1 fraction
while `used_percent` is 0..100, which is how 23% silently becomes 0%.

The core is deliberately runtime-agnostic (web-standard APIs, a `deps`
interface for the database, no `npm:` specifiers) so this app's own vitest can
exercise the Deno function's logic. The agent's own suite lives beside it:
`PYTHONPATH=.. python3 -m unittest discover -s tests`.
