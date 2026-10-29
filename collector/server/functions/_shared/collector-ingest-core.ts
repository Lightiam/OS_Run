// Collector ingest, runtime-agnostic.
//
// This module holds the whole request contract and touches no Deno or Supabase
// API, so the edge function stays a thin shell and the behaviour below can be
// tested under vitest (see src/test/collector-ingest.test.ts). Only web
// standard APIs are used: Request/Response, crypto.subtle, TextEncoder.

export const SCHEMA = "lightos.collector.v1";
export const KEY_PREFIX = "lfc_";

export const MAX_BODY_BYTES = 1024 * 1024;
export const MAX_LINES = 500;
export const MAX_LINE_BYTES = 64 * 1024;
// Agents are not trusted to have a correct clock, but a wildly wrong one means
// the measurement cannot be placed on a timeline, so it is dropped.
export const MAX_FUTURE_SKEW_MS = 60 * 60 * 1000;
export const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

export type RejectReason = "not_an_object" | "schema" | "hostname" | "timestamp" | "metrics" | "too_long";

export interface Measurement {
  hostname: string;
  schema: string;
  collectedAt: string;
  host: Record<string, unknown>;
  metrics: Record<string, unknown>;
}

export interface MeasurementRow {
  environment_id: string;
  user_id: string;
  hostname: string;
  schema_version: string;
  collected_at: string;
  host: Record<string, unknown>;
  metrics: Record<string, unknown>;
}

export interface EnvironmentRow {
  id: string;
  user_id: string;
  status: string;
}

export interface IngestDeps {
  /** Look up an environment by the SHA-256 of the presented key. */
  lookupEnvironment(keyHash: string): Promise<EnvironmentRow | null>;
  /** Insert rows, ignoring ones that already exist. Returns rows stored. */
  insertMeasurements(rows: MeasurementRow[]): Promise<number>;
  /** Record that the environment reported in. */
  touchEnvironment(
    environmentId: string,
    patch: { lastSeenAt: string; hostname: string; accepted: number },
  ): Promise<void>;
  now?(): Date;
}

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });

/**
 * SHA-256 of the key, hex encoded.
 *
 * Unlike the human-typed setup codes in node-enroll, collector keys are
 * machine-generated and case-sensitive, so the input is hashed verbatim.
 */
export async function hashKey(key: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Pull the collector key out of an Authorization header, or null. */
export function extractKey(header: string | null): string | null {
  if (!header?.startsWith("Bearer ")) return null;
  const key = header.slice(7).trim();
  if (!key.startsWith(KEY_PREFIX) || key.length < 16 || key.length > 200) return null;
  return key;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * A valid measurement, or the reason it was dropped. The discriminant is a
 * string rather than a boolean because the app this shares a tsconfig with has
 * strictNullChecks off, and TypeScript does not narrow a union on a boolean
 * discriminant without it.
 */
export type Validation =
  | { kind: "ok"; measurement: Measurement }
  | { kind: "rejected"; reason: RejectReason };

/** Validate one decoded measurement, returning the reason it was dropped. */
export function validateMeasurement(value: unknown, now: Date): Validation {
  if (!isPlainObject(value)) return { kind: "rejected", reason: "not_an_object" };
  if (value.schema !== SCHEMA) return { kind: "rejected", reason: "schema" };

  const host = isPlainObject(value.host) ? value.host : null;
  const hostname = typeof host?.hostname === "string" ? host.hostname.trim() : "";
  if (!hostname || hostname.length > 255) return { kind: "rejected", reason: "hostname" };

  if (typeof value.collected_at !== "string") return { kind: "rejected", reason: "timestamp" };
  const collected = new Date(value.collected_at);
  const elapsed = now.getTime() - collected.getTime();
  if (Number.isNaN(collected.getTime())) return { kind: "rejected", reason: "timestamp" };
  if (elapsed < -MAX_FUTURE_SKEW_MS || elapsed > MAX_AGE_MS) {
    return { kind: "rejected", reason: "timestamp" };
  }

  if (!isPlainObject(value.metrics)) return { kind: "rejected", reason: "metrics" };

  return {
    kind: "ok",
    measurement: {
      hostname,
      schema: SCHEMA,
      collectedAt: collected.toISOString(),
      host: host ?? {},
      metrics: value.metrics,
    },
  };
}

export interface ParsedBatch {
  measurements: Measurement[];
  rejected: number;
  reasons: Partial<Record<RejectReason, number>>;
  truncated: boolean;
}

/**
 * Parse a newline-delimited JSON batch.
 *
 * Bad lines are counted and dropped rather than failing the batch: the agent
 * retries anything it was not told was delivered, so rejecting a whole batch
 * over one malformed line would wedge its spool behind that line forever.
 */
export function parseBatch(text: string, now: Date): ParsedBatch {
  const measurements: Measurement[] = [];
  const reasons: Partial<Record<RejectReason, number>> = {};
  let rejected = 0;
  let truncated = false;

  const note = (reason: RejectReason) => {
    reasons[reason] = (reasons[reason] ?? 0) + 1;
    rejected += 1;
  };

  const lines = text.split("\n").filter((line) => line.trim().length > 0);
  for (const line of lines) {
    if (measurements.length + rejected >= MAX_LINES) {
      truncated = true;
      break;
    }
    if (line.length > MAX_LINE_BYTES) {
      note("too_long");
      continue;
    }
    let decoded: unknown;
    try {
      decoded = JSON.parse(line);
    } catch {
      note("not_an_object");
      continue;
    }
    const result = validateMeasurement(decoded, now);
    if (result.kind === "ok") measurements.push(result.measurement);
    else note(result.reason);
  }

  return { measurements, rejected, reasons, truncated };
}

/** Build rows, dropping repeats inside the batch itself. */
export function buildRows(
  environment: EnvironmentRow,
  measurements: Measurement[],
): { rows: MeasurementRow[]; duplicates: number } {
  const seen = new Set<string>();
  const rows: MeasurementRow[] = [];
  let duplicates = 0;

  for (const measurement of measurements) {
    const identity = `${measurement.hostname}\u0000${measurement.collectedAt}`;
    if (seen.has(identity)) {
      duplicates += 1;
      continue;
    }
    seen.add(identity);
    rows.push({
      environment_id: environment.id,
      user_id: environment.user_id,
      hostname: measurement.hostname,
      schema_version: measurement.schema,
      collected_at: measurement.collectedAt,
      host: measurement.host,
      metrics: measurement.metrics,
    });
  }

  return { rows, duplicates };
}

export async function handleIngest(req: Request, deps: IngestDeps): Promise<Response> {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (req.method !== "POST") return json({ error: "Method not allowed" }, 405);

  const key = extractKey(req.headers.get("Authorization"));
  if (!key) return json({ error: "Unauthorized" }, 401);

  const declared = Number(req.headers.get("Content-Length") ?? "0");
  if (declared > MAX_BODY_BYTES) return json({ error: "Payload too large" }, 413);

  const body = await req.text();
  if (new TextEncoder().encode(body).length > MAX_BODY_BYTES) {
    return json({ error: "Payload too large" }, 413);
  }
  if (!body.trim()) return json({ error: "Empty body" }, 400);

  // The key is checked before the body is parsed further, so an unauthenticated
  // caller learns nothing about which payloads would have been accepted.
  const environment = await deps.lookupEnvironment(await hashKey(key));
  if (!environment || environment.status !== "active") {
    return json({ error: "Unauthorized" }, 401);
  }

  const now = deps.now?.() ?? new Date();
  const batch = parseBatch(body, now);
  const { rows, duplicates: inBatch } = buildRows(environment, batch.measurements);

  let accepted = 0;
  if (rows.length) {
    accepted = await deps.insertMeasurements(rows);
    await deps.touchEnvironment(environment.id, {
      lastSeenAt: rows[rows.length - 1].collected_at,
      hostname: rows[rows.length - 1].hostname,
      accepted,
    });
  }

  return json(
    {
      environment_id: environment.id,
      accepted,
      duplicates: inBatch + (rows.length - accepted),
      rejected: batch.rejected,
      reasons: batch.reasons,
      truncated: batch.truncated,
    },
    202,
  );
}
