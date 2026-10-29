import { supabase } from "@/integrations/supabase/client";

export type CollectorEnvironment = {
  id: string;
  name: string;
  key_prefix: string;
  status: "active" | "revoked";
  last_seen_at: string | null;
  last_hostname: string | null;
  measurement_count: number;
  created_at: string;
};

/** Returned by `create` only. The plaintext key exists nowhere else, ever again. */
export type MintedEnvironment = {
  id: string;
  name: string;
  key: string;
  key_prefix: string;
  created_at: string;
};

export type CollectorMeasurement = {
  id: number;
  environment_id: string;
  hostname: string;
  schema_version: string;
  collected_at: string;
  received_at: string;
  host: Record<string, unknown>;
  metrics: Record<string, unknown>;
};

/**
 * `key_hash` is revoked from the `authenticated` role at column level, so a
 * `select("*")` here would fail with a permission error rather than leak it.
 * Listing the columns keeps that grant and this query in step.
 */
const ENVIRONMENT_COLUMNS =
  "id, name, key_prefix, status, last_seen_at, last_hostname, measurement_count, created_at";

const MEASUREMENT_COLUMNS =
  "id, environment_id, hostname, schema_version, collected_at, received_at, host, metrics";

type PostgresFailure = { code?: string; message?: string; details?: string | null };

/** One readable sentence from a PostgREST / Postgres error. */
function describeFailure(error: PostgresFailure | null, fallback: string): string {
  if (!error) return fallback;
  switch (error.code) {
    case "23505": // unique_violation — (user_id, name) or, absurdly improbably, key_hash
      return "You already have an environment with that name.";
    case "23514": // check_violation — the 1..120 character bound on name
      return "That name is too long. Use 120 characters or fewer.";
    case "42501": // insufficient_privilege
      return "You are not allowed to do that.";
    case "P0001": // raise_exception — collector_environment_create's own guard
      return error.message === "not authenticated" ? "Please sign in first." : error.message || fallback;
    case "PGRST301":
    case "401":
      return "Your session has expired. Please sign in again.";
    default:
      return error.message || fallback;
  }
}

function fail(error: PostgresFailure | null, fallback: string): never {
  throw new Error(describeFailure(error, fallback));
}

export type CollectorApi = {
  list: () => Promise<CollectorEnvironment[]>;
  create: (name: string) => Promise<MintedEnvironment>;
  revoke: (id: string) => Promise<void>;
  measurements: (environmentId: string, limit?: number) => Promise<CollectorMeasurement[]>;
};

/**
 * Collector environments are read straight from Postgres: row-level security
 * scopes every select to the signed-in user, and minting and revoking are
 * `security definer` functions, so there is no edge function to deploy and no
 * place for the browser to write either table directly.
 */
export const collectorApi: CollectorApi = {
  async list() {
    const { data, error } = await supabase
      .from("collector_environments")
      .select(ENVIRONMENT_COLUMNS)
      .order("created_at", { ascending: false });
    if (error) fail(error, "Could not load your collector environments.");
    // status is constrained to 'active' | 'revoked' by the table's check constraint.
    return (data ?? []) as CollectorEnvironment[];
  },

  async create(name) {
    const trimmed = name.trim();
    if (!trimmed) throw new Error("Give the environment a name.");

    const { data, error } = await supabase.rpc("collector_environment_create", { p_name: trimmed });
    if (error) fail(error, "Could not create the environment.");

    const minted = data?.[0];
    if (!minted?.key) throw new Error("The environment was created but no key came back. Reload and try again.");
    return minted as MintedEnvironment;
  },

  async revoke(id) {
    const { error } = await supabase.rpc("collector_environment_revoke", { p_id: id });
    if (error) fail(error, "Could not revoke that key.");
  },

  async measurements(environmentId, limit = 20) {
    const { data, error } = await supabase
      .from("collector_measurements")
      .select(MEASUREMENT_COLUMNS)
      .eq("environment_id", environmentId)
      .order("collected_at", { ascending: false })
      .limit(limit);
    if (error) fail(error, "Could not load measurements.");
    return (data ?? []) as CollectorMeasurement[];
  },
};

/** Exported for the test that keeps the select in step with the column grant. */
export const COLUMNS = { environment: ENVIRONMENT_COLUMNS, measurement: MEASUREMENT_COLUMNS };
