import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Result = { data: unknown; error: unknown };
type Query = { table: string; calls: [string, unknown[]][] };

const queries: Query[] = [];
const rpc = vi.fn();
let result: Result = { data: [], error: null };

/** A thenable stand-in for the PostgREST builder that records what was asked for. */
function builder(query: Query) {
  const chain: Record<string, unknown> = {};
  for (const method of ["select", "order", "eq", "limit"]) {
    chain[method] = (...args: unknown[]) => {
      query.calls.push([method, args]);
      return chain;
    };
  }
  chain.then = (resolve: (r: Result) => unknown, reject?: (e: unknown) => unknown) =>
    Promise.resolve(result).then(resolve, reject);
  return chain;
}

vi.mock("@/integrations/supabase/client", () => ({
  supabase: {
    from: (table: string) => {
      const query: Query = { table, calls: [] };
      queries.push(query);
      return builder(query);
    },
    rpc: (...args: unknown[]) => rpc(...args),
  },
}));

import { COLUMNS, collectorApi, localApi } from "@/lib/collector/client";

const lastQuery = () => queries[queries.length - 1];
const callsOf = (query: Query) => query.calls.map(([method, args]) => [method, ...args]);

beforeEach(() => {
  result = { data: [], error: null };
});

afterEach(() => {
  queries.length = 0;
  vi.clearAllMocks();
});

describe("listing environments", () => {
  it("reads the newest first and returns the rows", async () => {
    const row = {
      id: "e1", name: "collector-box", key_prefix: "lfc_0a1b2c3d", status: "active",
      last_seen_at: "2026-10-28T12:00:00Z", last_hostname: "node-01", measurement_count: 42,
      created_at: "2026-10-01T00:00:00Z",
    };
    result = { data: [row], error: null };

    expect(await collectorApi.list()).toEqual([row]);
    expect(lastQuery().table).toBe("collector_environments");
    expect(callsOf(lastQuery())).toEqual([
      ["select", COLUMNS.environment],
      ["order", "created_at", { ascending: false }],
    ]);
  });

  it("never asks for key_hash, which the column grant would refuse anyway", async () => {
    await collectorApi.list();
    const [, columns] = lastQuery().calls[0];
    expect(columns).not.toContain("key_hash");
    expect(columns).not.toBe("*");
    // Every column it does ask for is one the migration grants to `authenticated`.
    const granted = [
      "id", "user_id", "name", "key_prefix", "status", "last_seen_at", "last_hostname",
      "measurement_count", "created_at", "updated_at",
    ];
    for (const column of String(columns).split(",").map((c) => c.trim())) {
      expect(granted).toContain(column);
    }
  });

  it("treats a missing payload as no environments", async () => {
    result = { data: null, error: null };
    expect(await collectorApi.list()).toEqual([]);
  });

  it("surfaces a failure as a readable message", async () => {
    result = { data: null, error: { code: "42501", message: "permission denied for table collector_environments" } };
    await expect(collectorApi.list()).rejects.toThrow("You are not allowed to do that.");
  });

  it("falls back to the database's own message for a code it does not know", async () => {
    result = { data: null, error: { code: "08006", message: "connection failure" } };
    await expect(collectorApi.list()).rejects.toThrow("connection failure");
  });

  it("has something to say even for an error with no message at all", async () => {
    result = { data: null, error: {} };
    await expect(collectorApi.list()).rejects.toThrow("Could not load your collector environments.");
  });
});

describe("minting a key", () => {
  const minted = {
    id: "e1", name: "collector-box", key: `lfc_${"a".repeat(64)}`,
    key_prefix: "lfc_aaaaaaaa", created_at: "2026-10-28T12:00:00Z",
  };

  it("returns the one and only copy of the plaintext key", async () => {
    rpc.mockResolvedValue({ data: [minted], error: null });
    expect(await collectorApi.create("collector-box")).toEqual(minted);
    expect(rpc).toHaveBeenCalledWith("collector_environment_create", { p_name: "collector-box" });
  });

  it("trims the name before sending it", async () => {
    rpc.mockResolvedValue({ data: [minted], error: null });
    await collectorApi.create("  collector-box \n");
    expect(rpc).toHaveBeenCalledWith("collector_environment_create", { p_name: "collector-box" });
  });

  it("refuses a blank name without troubling the database", async () => {
    await expect(collectorApi.create("   ")).rejects.toThrow("Give the environment a name.");
    expect(rpc).not.toHaveBeenCalled();
  });

  it("explains a duplicate name rather than echoing the constraint", async () => {
    rpc.mockResolvedValue({
      data: null,
      error: { code: "23505", message: 'duplicate key value violates unique constraint "collector_environments_user_id_name_key"' },
    });
    await expect(collectorApi.create("collector-box")).rejects.toThrow("You already have an environment with that name.");
  });

  it("explains an over-long name", async () => {
    rpc.mockResolvedValue({ data: null, error: { code: "23514", message: "violates check constraint" } });
    await expect(collectorApi.create("x".repeat(200))).rejects.toThrow("That name is too long. Use 120 characters or fewer.");
  });

  it("turns the function's own authentication guard into a sign-in prompt", async () => {
    rpc.mockResolvedValue({ data: null, error: { code: "P0001", message: "not authenticated" } });
    await expect(collectorApi.create("collector-box")).rejects.toThrow("Please sign in first.");
  });

  it("keeps any other raise_exception message the function sends", async () => {
    rpc.mockResolvedValue({ data: null, error: { code: "P0001", message: "environment quota reached" } });
    await expect(collectorApi.create("collector-box")).rejects.toThrow("environment quota reached");
  });

  it("does not pretend to have a key when the function returns no row", async () => {
    rpc.mockResolvedValue({ data: [], error: null });
    await expect(collectorApi.create("collector-box")).rejects.toThrow("no key came back");
  });

  it("does not pretend to have a key when the row is missing one", async () => {
    rpc.mockResolvedValue({ data: [{ id: "e1", name: "collector-box" }], error: null });
    await expect(collectorApi.create("collector-box")).rejects.toThrow("no key came back");
  });
});

describe("revoking a key", () => {
  it("passes the id to the owner-scoped function", async () => {
    rpc.mockResolvedValue({ data: null, error: null });
    await collectorApi.revoke("e1");
    expect(rpc).toHaveBeenCalledWith("collector_environment_revoke", { p_id: "e1" });
  });

  it("reports a failure instead of claiming success", async () => {
    rpc.mockResolvedValue({ data: null, error: { code: "42501", message: "permission denied" } });
    await expect(collectorApi.revoke("e1")).rejects.toThrow("You are not allowed to do that.");
  });
});

describe("reading measurements", () => {
  it("asks for one environment's newest rows", async () => {
    const row = {
      id: 7, environment_id: "e1", hostname: "node-01", schema_version: "lightos.collector.v1",
      collected_at: "2026-10-28T12:00:00Z", received_at: "2026-10-28T12:00:01Z", host: {}, metrics: {},
    };
    result = { data: [row], error: null };

    expect(await collectorApi.measurements("e1")).toEqual([row]);
    expect(lastQuery().table).toBe("collector_measurements");
    expect(callsOf(lastQuery())).toEqual([
      ["select", COLUMNS.measurement],
      ["eq", "environment_id", "e1"],
      ["order", "collected_at", { ascending: false }],
      ["limit", 20],
    ]);
  });

  it("honours a caller's limit", async () => {
    await collectorApi.measurements("e1", 5);
    expect(callsOf(lastQuery())).toContainEqual(["limit", 5]);
  });

  it("surfaces a failure", async () => {
    result = { data: null, error: { code: "PGRST301", message: "JWT expired" } };
    await expect(collectorApi.measurements("e1")).rejects.toThrow("Your session has expired. Please sign in again.");
  });
});

describe("the local transport", () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  let reply: { ok: boolean; status: number; body: unknown } | Error;

  beforeEach(() => {
    calls.length = 0;
    reply = { ok: true, status: 200, body: [] };
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      calls.push([String(url), init]);
      if (reply instanceof Error) return Promise.reject(reply);
      const { ok, status, body } = reply;
      return Promise.resolve({ ok, status, json: () => Promise.resolve(body) } as Response);
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  it("lists over HTTP rather than through Postgres", async () => {
    reply = { ok: true, status: 200, body: [{ id: "e1", name: "box" }] };
    const rows = await localApi.list();
    expect(rows).toEqual([{ id: "e1", name: "box" }]);
    expect(calls[0][0]).toContain("/api/environments");
  });

  it("posts the name as JSON and hands back the minted key", async () => {
    reply = { ok: true, status: 201, body: { id: "e1", name: "box", key: "lfc_abc", key_prefix: "lfc_abc" } };
    const minted = await localApi.create("  box  ");
    expect(minted.key).toBe("lfc_abc");
    const [url, init] = calls[0];
    expect(url).toContain("/api/environments");
    expect(init?.method).toBe("POST");
    // Trimmed, so a stray space does not become part of the name.
    expect(JSON.parse(String(init?.body))).toEqual({ name: "box" });
  });

  it("refuses a blank name without troubling the server", async () => {
    await expect(localApi.create("   ")).rejects.toThrow("Give the environment a name.");
    expect(calls).toHaveLength(0);
  });

  it("says the server is unreachable rather than passing on a bare network error", async () => {
    reply = new TypeError("Failed to fetch");
    await expect(localApi.list()).rejects.toThrow(/Could not reach the local collector server/);
  });

  it("surfaces what the server said about a refusal", async () => {
    reply = { ok: false, status: 400, body: { error: "You already have an environment with that name." } };
    await expect(localApi.create("box")).rejects.toThrow("You already have an environment with that name.");
  });

  it("still says something when a failure carries no message", async () => {
    reply = { ok: false, status: 500, body: null };
    await expect(localApi.list()).rejects.toThrow("The local server returned 500.");
  });
});
