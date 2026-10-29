# LightCollect

LightCollect is the outbound host-metrics collector for LightOS. It reads host
state from `/proc`, batches measurements as newline-delimited JSON, and POSTs
them to the LightOS app.

Every connection is outbound, so a collector host needs no inbound port, no
public address, and no port forwarding. It is standard library only: a stock
Ubuntu host runs it with no `pip install` step.

Do not confuse it with `fabric-os/lightos-fabric-alpha/collector/lightos_collector.py`,
which is the opposite thing — an *inbound* JSONL receiver used for local tests.

## Usage

```bash
# Print one measurement without sending it anywhere.
python3 lightcollect.py --print

# Send a single measurement, then exit. Useful for proving a key works.
LIGHTOS_URL=https://app.example.com \
LIGHTOS_COLLECTOR_KEY=lfc_... \
python3 lightcollect.py --once

# Run continuously on a 60 second cadence.
python3 lightcollect.py
```

### Configuration

Flags override environment variables. Prefer the environment for the key, so it
stays out of `ps` output.

| Variable | Flag | Default |
| --- | --- | --- |
| `LIGHTOS_URL` | `--url` | required |
| `LIGHTOS_COLLECTOR_KEY` | `--key` | required |
| `LIGHTOS_INGEST_PATH` | `--ingest-path` | `/functions/v1/collector-ingest` |
| `LIGHTOS_INTERVAL` | `--interval` | `60` seconds |
| `LIGHTOS_SPOOL_DIR` | `--spool-dir` | `/var/lib/lightos-collector/spool` |
| `LIGHTOS_MOUNTS` | `--mounts` | `/` |
| — | `--root` | `/` (point at a fixture tree to test) |
| — | `--settle` | `1.0` seconds |

The endpoint is the `collector-ingest` Supabase edge function in
`agenticlightossh`, so `LIGHTOS_URL` is the project's functions host:

```bash
LIGHTOS_URL=https://<project-ref>.supabase.co
LIGHTOS_COLLECTOR_KEY=lfc_...
```

Mint a key from **Dashboard → Infrastructure** in that app, which also lists
each environment's health, host and measurement count. It is shown once and
only its SHA-256 is stored. Revoking it there stops it being accepted; the
agent then spools rather than dropping what it collects.

The page is a thin shell over two `security definer` functions, so the same
thing works from SQL: `collector_environment_create(name)` returns the
plaintext key once, and `collector_environment_revoke(id)` retires it.

## Behaviour worth knowing

- **CPU utilization spans the interval.** Each tick reuses the previous tick's
  `/proc/stat` sample, so the figure covers the whole minute rather than a
  one-second window. The first tick, and any `--once` run, can only measure the
  short `--settle` window; a static or unreadable counter reports `null` rather
  than a fabricated zero.
- **Undeliverable measurements are spooled, not dropped.** They append to
  `measurements.jsonl` and flush oldest-first on a later tick, so an outage
  costs latency. A batch is only removed from the spool once the POST for it
  returns, so a failure mid-flush loses nothing.
- **The spool is capped** at 32 MB, dropping the oldest lines first.
- **Virtual interfaces are skipped** (`lo`, `veth*`, `docker*`, `br-*`, `cni*`,
  `flannel*`, `kube*`), since they do not describe real host traffic.
- **Host metrics must be collected on the host.** Run it on the metal or in the
  VM you want to measure, never inside a container, or the readings describe the
  container.

## Payload

```json
{
  "schema": "lightos.collector.v1",
  "collected_at": "2026-10-28T11:22:33Z",
  "host": {"hostname": "node-01", "os": "Ubuntu 24.04.1 LTS", "kernel": "6.8.0-45-generic", "arch": "x86_64"},
  "metrics": {
    "cpu": {"utilization": 0.2308},
    "memory": {"total_bytes": 0, "available_bytes": 0, "used_bytes": 0, "used_percent": 0.0},
    "load": {"load1": 0.52, "load5": 0.31, "load15": 0.15},
    "uptime_seconds": 123456.78,
    "disk": [{"mount": "/", "total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "used_percent": 0.0}],
    "network": [{"interface": "eth0", "rx_bytes": 5000, "tx_bytes": 7000}]
  }
}
```

Requests carry `Content-Type: application/x-ndjson` and
`Authorization: Bearer <key>`, one measurement per line.

## Tests

```bash
python3 -m unittest discover -s lightcollect/tests -t .
```

Run it from the directory *above* this one. Run it from inside and the
import fails: this directory contains `lightcollect.py`, which shadows the
`lightcollect` package, and Python searches the working directory before
`PYTHONPATH`. The suite uses `/proc` fixtures, so it runs on any OS, and
exercises the sender over a real loopback socket. A line reading
`delivery failed; spooling` is a test covering the offline spool.
