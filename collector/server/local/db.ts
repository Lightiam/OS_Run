// SQLite store for the local server, mirroring the Postgres migration closely
// enough that behaviour matches: the same unique key gives the same
// idempotency, and key_hash is the only thing stored for a key.
import { DatabaseSync } from "node:sqlite";
import { randomUUID } from "node:crypto";
import { hashKey, KEY_PREFIX } from "../functions/_shared/collector-ingest-core.ts";
import type { EnvironmentRow, IngestDeps, MeasurementRow } from "../functions/_shared/collector-ingest-core.ts";

/** There is no auth locally, so every environment belongs to one stand-in user. */
export const LOCAL_USER = "00000000-0000-0000-0000-000000000000";

export function open(path: string) {
  const db = new DatabaseSync(path);
  db.exec(`
    pragma journal_mode = wal;
    create table if not exists collector_environments (
      id                text primary key,
      user_id           text not null,
      name              text not null unique,
      key_hash          text not null unique,
      key_prefix        text not null,
      status            text not null default 'active',
      last_seen_at      text,
      last_hostname     text,
      measurement_count integer not null default 0,
      created_at        text not null
    );
    create table if not exists collector_measurements (
      id             text primary key,
      environment_id text not null references collector_environments(id) on delete cascade,
      hostname       text not null,
      schema_version text not null,
      collected_at   text not null,
      received_at    text not null,
      host           text not null,
      metrics        text not null,
      unique (environment_id, hostname, collected_at)
    );
    create index if not exists collector_measurements_recent
      on collector_measurements (environment_id, collected_at desc);
  `);
  return db;
}

/**
 * Mint a key the same shape the migration does: `lfc_` followed by two
 * dash-stripped UUIDs. Only the hash is stored, so this is the one moment the
 * plaintext exists.
 */
export async function mint(db: DatabaseSync, name: string) {
  const trimmed = name.trim();
  if (!trimmed) throw new Error("A name is required.");
  if (trimmed.length > 120) throw new Error("That name is too long. Use 120 characters or fewer.");

  const key = KEY_PREFIX + randomUUID().replaceAll("-", "") + randomUUID().replaceAll("-", "");
  const row = {
    id: randomUUID(),
    key_hash: await hashKey(key),
    key_prefix: key.slice(0, 12),
    created_at: new Date().toISOString(),
  };
  try {
    db.prepare(
      `insert into collector_environments
         (id, user_id, name, key_hash, key_prefix, status, created_at)
       values (?, ?, ?, ?, ?, 'active', ?)`,
    ).run(row.id, LOCAL_USER, trimmed, row.key_hash, row.key_prefix, row.created_at);
  } catch (e) {
    if (String((e as Error).message).includes("UNIQUE")) {
      throw new Error("You already have an environment with that name.");
    }
    throw e;
  }
  return { id: row.id, name: trimmed, key, key_prefix: row.key_prefix, created_at: row.created_at };
}

export function revoke(db: DatabaseSync, id: string) {
  const r = db.prepare(`update collector_environments set status = 'revoked' where id = ?`).run(id);
  if (!r.changes) throw new Error("No such environment.");
}

/** The columns the real client is allowed to read. key_hash is never one. */
export function environments(db: DatabaseSync) {
  return db.prepare(
    `select id, name, key_prefix, status, last_seen_at, last_hostname,
            measurement_count, created_at
       from collector_environments
      order by created_at desc`,
  ).all();
}

export function measurements(db: DatabaseSync, environmentId: string, limit = 20) {
  const rows = db.prepare(
    `select id, environment_id, hostname, schema_version, collected_at, received_at, host, metrics
       from collector_measurements
      where environment_id = ?
      order by collected_at desc
      limit ?`,
  ).all(environmentId, limit);
  return rows.map((r) => ({
    ...r,
    host: JSON.parse(String(r.host)),
    metrics: JSON.parse(String(r.metrics)),
  }));
}

export function deps(db: DatabaseSync): IngestDeps {
  return {
    async lookupEnvironment(keyHash) {
      const row = db.prepare(
        `select id, user_id, status from collector_environments where key_hash = ?`,
      ).get(keyHash);
      return (row as EnvironmentRow | undefined) ?? null;
    },

    async insertMeasurements(rows: MeasurementRow[]) {
      // "insert or ignore" against the unique key is what upsert with
      // ignoreDuplicates does in Postgres: a retried batch stores nothing and
      // reports it as a duplicate rather than failing.
      const stmt = db.prepare(
        `insert or ignore into collector_measurements
           (id, environment_id, hostname, schema_version, collected_at, received_at, host, metrics)
         values (?, ?, ?, ?, ?, ?, ?, ?)`,
      );
      const receivedAt = new Date().toISOString();
      let stored = 0;
      for (const row of rows) {
        const r = stmt.run(
          randomUUID(), row.environment_id, row.hostname, row.schema_version,
          row.collected_at, receivedAt, JSON.stringify(row.host), JSON.stringify(row.metrics),
        );
        stored += r.changes ? 1 : 0;
      }
      return stored;
    },

    async touchEnvironment(environmentId, patch) {
      db.prepare(
        `update collector_environments
            set last_seen_at      = ?,
                last_hostname     = ?,
                measurement_count = measurement_count + ?
          where id = ?`,
      ).run(patch.lastSeenAt, patch.hostname, patch.accepted, environmentId);
    },
  };
}
