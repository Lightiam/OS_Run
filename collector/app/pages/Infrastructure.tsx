import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import {
  Activity, AlertTriangle, CheckCircle2, Clock, Copy, Gauge, KeyRound, Loader2, Plus, ShieldOff, Terminal, XCircle,
} from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { collectorApi, type CollectorEnvironment, type CollectorMeasurement, type MintedEnvironment } from "@/lib/collector/client";
import { formatBytes, formatPercent, formatUptime, health, relativeTime, summarize, type Health } from "@/lib/collector/format";

const HEALTH_STYLE: Record<Health, { label: string; icon: typeof CheckCircle2; className: string }> = {
  live: { label: "Live", icon: CheckCircle2, className: "text-emerald-400 border-emerald-400/40" },
  stale: { label: "Stale", icon: AlertTriangle, className: "text-amber-400 border-amber-400/40" },
  waiting: { label: "Waiting for first post", icon: Clock, className: "text-muted-foreground border-border" },
  revoked: { label: "Revoked", icon: XCircle, className: "text-muted-foreground border-border" },
};

const INGEST_HOST = (import.meta.env.VITE_SUPABASE_URL as string | undefined)?.replace(/\/+$/, "") ?? "";

function copy(text: string, what: string) {
  navigator.clipboard.writeText(text).then(
    () => toast.success(`${what} copied`),
    () => toast.error("Could not reach the clipboard. Select the text and copy it by hand."),
  );
}

export default function Infrastructure() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const [environments, setEnvironments] = useState<CollectorEnvironment[]>([]);
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [minted, setMinted] = useState<MintedEnvironment | null>(null);
  const [inspecting, setInspecting] = useState<CollectorEnvironment | null>(null);

  const refresh = useCallback(async () => {
    try {
      setEnvironments(await collectorApi.list());
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      const ok = !!data.session;
      setSignedIn(ok);
      if (ok) refresh();
      else setLoading(false);
    });
  }, [refresh]);

  const create = async () => {
    setCreating(true);
    try {
      const environment = await collectorApi.create(name);
      setMinted(environment);
      setName("");
      await refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (env: CollectorEnvironment) => {
    if (!confirm(`Revoke the key for "${env.name}"? Its collector will start failing immediately, and spooling rather than losing what it collects.`)) return;
    setBusy(env.id);
    try {
      await collectorApi.revoke(env.id);
      toast.success("Key revoked");
      await refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const live = useMemo(() => environments.filter((e) => health(e) === "live").length, [environments]);

  return (
    <div className="p-6 space-y-8 max-w-7xl">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold font-mono text-foreground">Infrastructure</h1>
          <p className="text-sm text-muted-foreground mt-1 max-w-2xl">
            An environment is one machine, or one fleet, running the LightCollect agent. Each has a key the agent
            presents when it posts host metrics. The agent only ever dials out, so the machine needs no inbound port
            and no public address.
          </p>
        </div>
        {!loading && environments.length > 0 && (
          <Badge variant="outline" className={live > 0 ? "text-emerald-400 border-emerald-400/40" : "text-amber-400 border-amber-400/40"}>
            <Activity className="h-3 w-3 mr-1" /> {live} of {environments.length} reporting
          </Badge>
        )}
      </div>

      {signedIn === false && (
        <div className="rounded-lg border border-border bg-card p-5 text-sm">
          <Link to="/auth?next=/dashboard/infrastructure" className="text-primary underline">Sign in</Link> to create a collector environment.
        </div>
      )}

      {signedIn && (
        <>
          <section className="space-y-3">
            <h2 className="font-mono text-sm uppercase tracking-wider text-primary">New environment</h2>
            <form
              className="rounded-lg border border-border bg-card p-5 flex flex-col sm:flex-row sm:items-end gap-3"
              onSubmit={(e) => { e.preventDefault(); if (name.trim() && !creating) create(); }}
            >
              <div className="flex-1 space-y-1.5">
                <Label htmlFor="env-name" className="text-xs font-mono uppercase tracking-wide text-muted-foreground">Name</Label>
                <Input
                  id="env-name"
                  maxLength={120}
                  placeholder="e.g. collector-box, lab-node-01, slurm-login"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <Button type="submit" disabled={creating || !name.trim()}>
                {creating ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Plus className="h-4 w-4 mr-2" />}
                Create key
              </Button>
            </form>
            <p className="text-xs text-muted-foreground">
              The key is shown once, here, and never again — only its SHA-256 is stored. Lose it and you mint a new one.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="font-mono text-sm uppercase tracking-wider text-primary">Your environments</h2>
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>
            ) : environments.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground">
                No environments yet. Create one above, then follow the setup it hands you.
              </div>
            ) : (
              <div className="rounded-lg border border-border bg-card overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Environment</TableHead>
                      <TableHead>Key</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Host</TableHead>
                      <TableHead>Last post</TableHead>
                      <TableHead className="text-right">Measurements</TableHead>
                      <TableHead className="text-right">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {environments.map((env) => {
                      const s = HEALTH_STYLE[health(env)];
                      return (
                        <TableRow key={env.id}>
                          <TableCell className="font-medium">{env.name}</TableCell>
                          <TableCell className="font-mono text-xs text-muted-foreground">{env.key_prefix}…</TableCell>
                          <TableCell>
                            <Badge variant="outline" className={s.className}><s.icon className="h-3 w-3 mr-1" /> {s.label}</Badge>
                          </TableCell>
                          <TableCell className="font-mono text-xs">{env.last_hostname ?? "—"}</TableCell>
                          <TableCell className="text-sm text-muted-foreground" title={env.last_seen_at ?? ""}>
                            {relativeTime(env.last_seen_at)}
                          </TableCell>
                          <TableCell className="text-right font-mono text-xs">{env.measurement_count.toLocaleString()}</TableCell>
                          <TableCell className="text-right whitespace-nowrap">
                            <Button size="sm" variant="ghost" disabled={env.measurement_count === 0} onClick={() => setInspecting(env)}>
                              <Gauge className="h-4 w-4 mr-1" /> Metrics
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="text-destructive"
                              disabled={busy === env.id || env.status === "revoked"}
                              onClick={() => revoke(env)}
                            >
                              {busy === env.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldOff className="h-4 w-4" />}
                            </Button>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </section>
        </>
      )}

      <KeyDialog minted={minted} onClose={() => setMinted(null)} />
      <MetricsSheet environment={inspecting} onClose={() => setInspecting(null)} />
    </div>
  );
}

/** The one and only sighting of the plaintext key, with the file it belongs in. */
function KeyDialog({ minted, onClose }: { minted: MintedEnvironment | null; onClose: () => void }) {
  const envFile = minted
    ? `LIGHTOS_URL=${INGEST_HOST || "https://<project-ref>.supabase.co"}\nLIGHTOS_COLLECTOR_KEY=${minted.key}`
    : "";

  return (
    <Dialog open={!!minted} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><KeyRound className="h-4 w-4 text-primary" /> Key for “{minted?.name}”</DialogTitle>
          <DialogDescription>
            Copy this now. It is not stored anywhere and cannot be shown again — only its SHA-256 is kept, so that a
            database leak cannot be replayed against the ingest endpoint.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label className="text-xs font-mono uppercase tracking-wide text-muted-foreground">Key</Label>
            <div className="flex items-center gap-2 rounded-md border border-primary/30 bg-muted/40 p-3">
              <code className="font-mono text-xs break-all flex-1 text-primary">{minted?.key}</code>
              <Button size="sm" variant="ghost" onClick={() => minted && copy(minted.key, "Key")}>
                <Copy className="h-4 w-4" />
              </Button>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label className="text-xs font-mono uppercase tracking-wide text-muted-foreground">
              /etc/lightos/collector.env on the collector box
            </Label>
            <div className="flex items-start gap-2 rounded-md border border-border bg-muted/40 p-3">
              <pre className="font-mono text-xs break-all whitespace-pre-wrap flex-1 text-foreground">{envFile}</pre>
              <Button size="sm" variant="ghost" onClick={() => copy(envFile, "Env file")}>
                <Copy className="h-4 w-4" />
              </Button>
            </div>
            {!INGEST_HOST && (
              <p className="text-xs text-amber-400/90 flex gap-1">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
                This browser has no VITE_SUPABASE_URL, so fill in the project host yourself.
              </p>
            )}
          </div>

          <div className="rounded-md border border-border p-3 text-xs text-muted-foreground space-y-1">
            <p className="flex items-center gap-1.5 font-mono uppercase tracking-wide text-foreground">
              <Terminal className="h-3.5 w-3.5" /> Then, on that machine
            </p>
            <p>
              Install the agent, prove it reads the host with <code className="font-mono text-primary">lightos-collector --print</code>,
              send one measurement with <code className="font-mono text-primary">--once</code>, and enable the systemd unit.
              Host metrics must be collected on the metal or in the VM you want to measure — never inside a container.
            </p>
          </div>
        </div>

        <DialogFooter>
          <Button onClick={onClose}>I have copied it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function MetricsSheet({ environment, onClose }: { environment: CollectorEnvironment | null; onClose: () => void }) {
  const [rows, setRows] = useState<CollectorMeasurement[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!environment) { setRows(null); setError(null); return; }
    let live = true;
    collectorApi.measurements(environment.id).then(
      (r) => { if (live) setRows(r); },
      (e) => { if (live) setError((e as Error).message); },
    );
    return () => { live = false; };
  }, [environment]);

  const latest = rows?.[0] ?? null;
  const summary = summarize(latest);

  return (
    <Sheet open={!!environment} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent className="w-full sm:max-w-2xl overflow-y-auto">
        <SheetHeader>
          <SheetTitle>{environment?.name}</SheetTitle>
          <SheetDescription>
            {latest
              ? `${latest.hostname} · ${summary.os ?? "unknown OS"} · ${summary.kernel ?? "unknown kernel"} · ${summary.arch ?? "unknown arch"}`
              : "The most recent measurements this environment has posted."}
          </SheetDescription>
        </SheetHeader>

        <div className="mt-6 space-y-6">
          {error && <p className="text-sm text-destructive">{error}</p>}
          {!rows && !error && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>
          )}
          {rows?.length === 0 && <p className="text-sm text-muted-foreground">Nothing posted yet.</p>}

          {latest && (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {[
                  { label: "CPU", value: formatPercent(summary.cpu) },
                  { label: "Memory", value: formatPercent(summary.memory) },
                  { label: `Disk ${summary.diskMount ?? ""}`.trim(), value: formatPercent(summary.disk) },
                  { label: "Load (1m)", value: summary.load1 === null ? "—" : summary.load1.toFixed(2) },
                ].map((stat) => (
                  <div key={stat.label} className="rounded-lg border border-border bg-card p-3">
                    <p className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">{stat.label}</p>
                    <p className="text-xl font-mono text-foreground mt-1">{stat.value}</p>
                  </div>
                ))}
              </div>

              <div className="text-xs text-muted-foreground space-y-1">
                <p>
                  Memory: {formatBytes(summary.memoryUsedBytes)} of {formatBytes(summary.memoryTotalBytes)} · Uptime:{" "}
                  {formatUptime(summary.uptimeSeconds)}
                </p>
                {summary.cpu === null && (
                  <p className="text-amber-400/90">
                    CPU is reported as unknown: the agent sends null rather than a fabricated zero when the counters
                    did not move or could not be read.
                  </p>
                )}
              </div>

              <div className="rounded-lg border border-border overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Collected</TableHead>
                      <TableHead>Host</TableHead>
                      <TableHead className="text-right">CPU</TableHead>
                      <TableHead className="text-right">Memory</TableHead>
                      <TableHead className="text-right">Load</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows?.map((row) => {
                      const s = summarize(row);
                      return (
                        <TableRow key={row.id}>
                          <TableCell className="font-mono text-xs whitespace-nowrap" title={row.collected_at}>
                            {relativeTime(row.collected_at)}
                          </TableCell>
                          <TableCell className="font-mono text-xs">{row.hostname}</TableCell>
                          <TableCell className="text-right font-mono text-xs">{formatPercent(s.cpu)}</TableCell>
                          <TableCell className="text-right font-mono text-xs">{formatPercent(s.memory)}</TableCell>
                          <TableCell className="text-right font-mono text-xs">{s.load1 === null ? "—" : s.load1.toFixed(2)}</TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
