// LightOS collector: local ingest server.
//
// Runs the real request contract. It imports handleIngest from
// ../functions/_shared/collector-ingest-core.ts, the same module the Supabase
// edge function uses, against a local SQLite file instead of Postgres. So a
// key accepted here is accepted there, and a payload rejected here is rejected
// there for the same reason.
//
//   node server.ts                 serve on 8787
//   node server.ts --mint my-box   mint a key and exit
//   node server.ts --port 9000     serve elsewhere
//
// Needs no npm install: node:sqlite and TypeScript stripping are both built
// into Node 24. What this is not is the production path. There is no auth, and
// every environment belongs to one stand-in user.
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { handleIngest } from "../functions/_shared/collector-ingest-core.ts";
import { deps, environments, measurements, mint, open, revoke } from "./db.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const INGEST_PATH = "/functions/v1/collector-ingest";

/** Three missed ticks on a 60s cadence, matching src/lib/collector/format.ts. */
const STALE_AFTER_MS = 3 * 60 * 1000;

const args = process.argv.slice(2);
const flag = (name: string) => {
  const i = args.indexOf(name);
  return i === -1 ? null : args[i + 1] ?? "";
};

const db = open(join(HERE, flag("--db") ?? "collector.db"));

// --- mint and exit -----------------------------------------------------------

const mintName = flag("--mint");
if (mintName !== null) {
  const env = await mint(db, mintName);
  console.log("");
  console.log(`  environment  ${env.name}`);
  console.log(`  id           ${env.id}`);
  console.log("");
  console.log("  Shown once. Only its SHA-256 is stored.");
  console.log("");
  console.log(`  LIGHTOS_COLLECTOR_KEY=${env.key}`);
  console.log("");
  process.exit(0);
}

// --- helpers -----------------------------------------------------------------

const send = (res: ServerResponse, status: number, body: unknown) => {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
  });
  res.end(JSON.stringify(body));
};

const readBody = async (req: IncomingMessage) => {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString("utf8");
};

/** The Node request as a web Request, so the shared core handles it unchanged. */
const toWebRequest = (req: IncomingMessage, body: string) => {
  const headers = new Headers();
  for (const [k, v] of Object.entries(req.headers)) {
    if (Array.isArray(v)) v.forEach((one) => headers.append(k, one));
    else if (v != null) headers.set(k, v);
  }
  const method = req.method ?? "GET";
  return new Request(`http://${req.headers.host ?? "localhost"}${req.url ?? "/"}`, {
    method,
    headers,
    body: method === "GET" || method === "HEAD" ? undefined : body,
  });
};

const health = (row: Record<string, unknown>) => {
  if (row.status === "revoked") return "revoked";
  const seen = row.last_seen_at ? Date.parse(String(row.last_seen_at)) : NaN;
  if (!Number.isFinite(seen)) return "waiting";
  return Date.now() - seen <= STALE_AFTER_MS ? "live" : "stale";
};

const esc = (s: string) =>
  s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" })[c]!);

// --- the page ----------------------------------------------------------------

const page = (port: number) => {
  const rows = environments(db).map((r) => ({ ...r, health: health(r) }));
  const body = rows.length
    ? rows
        .map((r) => {
          const latest = measurements(db, String(r.id), 1)[0];
          const host = latest ? String(latest.host.hostname ?? "—") : "—";
          return `<tr>
            <td><strong>${esc(String(r.name))}</strong><div class=mono>${esc(String(r.key_prefix))}…</div></td>
            <td><span class="pill ${r.health}">${r.health}</span></td>
            <td>${esc(host)}</td>
            <td>${r.measurement_count}</td>
            <td class=mono>${r.last_seen_at ? esc(String(r.last_seen_at)) : "never"}</td>
          </tr>`;
        })
        .join("")
    : `<tr><td colspan=5 class=empty>No environments yet. Mint one with
         <code>node server.ts --mint my-box</code></td></tr>`;

  return `<!doctype html><meta charset=utf-8><title>Collector</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=10>
<style>
:root{--bg:#0b0d10;--fg:#e6e8eb;--dim:#8b949e;--line:#21262d;--live:#3fb950;--stale:#d29922;--wait:#58a6ff;--rev:#f85149}
@media(prefers-color-scheme:light){:root{--bg:#fff;--fg:#1f2328;--dim:#656d76;--line:#d0d7de}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,sans-serif;margin:0;padding:32px 16px}
main{max-width:860px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
p.sub{color:var(--dim);margin:0 0 24px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.mono,code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--dim)}
.pill{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid currentColor}
.live{color:var(--live)}.stale{color:var(--stale)}.waiting{color:var(--wait)}.revoked{color:var(--rev)}
.empty{color:var(--dim);text-align:center;padding:32px 10px}
footer{margin-top:24px;color:var(--dim);font-size:12px}
</style>
<main>
<h1>Collector</h1>
<p class=sub>Local server on port ${port}. Ingest endpoint <code>POST ${INGEST_PATH}</code></p>
<table><thead><tr><th>Environment<th>Health<th>Host<th>Measurements<th>Last seen</tr></thead>
<tbody>${body}</tbody></table>
<footer>Refreshes every 10s. <code>live</code> means a measurement arrived within 3 minutes;
<code>stale</code> means three missed ticks.</footer>
</main>`;
};

// --- routes ------------------------------------------------------------------

const ingestDeps = deps(db);
const port = Number(flag("--port") ?? process.env.PORT ?? 8787);

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);
  const path = url.pathname.replace(/\/+$/, "") || "/";

  try {
    // The browser preflights the JSON POSTs from the app. The ingest path does
    // its own CORS inside the shared core, so it is left alone here.
    if (req.method === "OPTIONS" && path !== INGEST_PATH) {
      res.writeHead(204, {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Max-Age": "86400",
      });
      return res.end();
    }

    if (path === INGEST_PATH) {
      const body = await readBody(req);
      const response = await handleIngest(toWebRequest(req, body), ingestDeps);
      const text = await response.text();
      res.writeHead(response.status, Object.fromEntries(response.headers));
      res.end(text);
      if (req.method === "POST") {
        console.log(`${new Date().toISOString()}  ingest ${response.status}  ${body.length}B  ${text}`);
      }
      return;
    }

    if (path === "/healthz") return send(res, 200, { ok: true });

    if (path === "/api/environments" && req.method === "GET") {
      return send(res, 200, environments(db).map((r) => ({ ...r, health: health(r) })));
    }

    if (path === "/api/measurements" && req.method === "GET") {
      const id = url.searchParams.get("environment");
      if (!id) return send(res, 400, { error: "environment is required" });
      return send(res, 200, measurements(db, id, Number(url.searchParams.get("limit") ?? 20)));
    }

    if (path === "/api/environments" && req.method === "POST") {
      const parsed = JSON.parse((await readBody(req)) || "{}");
      return send(res, 201, await mint(db, String(parsed.name ?? "")));
    }

    if (path.startsWith("/api/environments/") && path.endsWith("/revoke") && req.method === "POST") {
      revoke(db, path.split("/")[3]);
      return send(res, 200, { ok: true });
    }

    if (path === "/" && req.method === "GET") {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      return res.end(page(port));
    }

    send(res, 404, { error: "Not found" });
  } catch (e) {
    console.error("request failed:", (e as Error).message);
    send(res, 500, { error: (e as Error).message });
  }
});

server.listen(port, () => {
  console.log(`collector server on http://localhost:${port}`);
  console.log(`  ingest   POST http://localhost:${port}${INGEST_PATH}`);
  console.log(`  mint     node server.ts --mint <name>`);
});
