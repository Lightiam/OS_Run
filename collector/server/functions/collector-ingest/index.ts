// LightOS collector ingest.
//  POST <ndjson>  (lfc_ environment key)  -> 202 {accepted, duplicates, rejected}
//
// Agents authenticate with an environment key rather than a user JWT, so this
// function runs with verify_jwt = false (see supabase/config.toml) and writes
// with the service role. The request contract lives in
// _shared/collector-ingest-core.ts so it can be tested outside Deno.
import { createClient } from "npm:@supabase/supabase-js@2";
import {
  handleIngest,
  json,
  type EnvironmentRow,
  type IngestDeps,
  type MeasurementRow,
} from "../_shared/collector-ingest-core.ts";

const admin = () =>
  createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);

Deno.serve(async (req) => {
  const db = admin();

  const deps: IngestDeps = {
    async lookupEnvironment(keyHash) {
      const { data } = await db
        .from("collector_environments")
        .select("id,user_id,status")
        .eq("key_hash", keyHash)
        .maybeSingle();
      return (data as EnvironmentRow | null) ?? null;
    },

    async insertMeasurements(rows: MeasurementRow[]) {
      // ignoreDuplicates leans on the (environment_id, hostname, collected_at)
      // unique key, so a retried batch returns only the rows actually stored.
      const { data, error } = await db
        .from("collector_measurements")
        .upsert(rows, {
          onConflict: "environment_id,hostname,collected_at",
          ignoreDuplicates: true,
        })
        .select("id");
      if (error) throw new Error(error.message);
      return data?.length ?? 0;
    },

    async touchEnvironment(environmentId, patch) {
      const { error } = await db.rpc("collector_environment_touch", {
        p_id: environmentId,
        p_last_seen: patch.lastSeenAt,
        p_hostname: patch.hostname,
        p_accepted: patch.accepted,
      });
      if (error) throw new Error(error.message);
    },
  };

  try {
    return await handleIngest(req, deps);
  } catch (e) {
    // The agent spools and retries on any non-2xx, so a failure here is safe.
    console.error("collector-ingest", (e as Error).message);
    return json({ error: "Ingest failed" }, 500);
  }
});
