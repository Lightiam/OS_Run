# Sender adapter

Forwards collector measurements to the hosted Infrastructure endpoint.

It reads through the local HTTP API and never opens `collector.db`, so the
local server, its environments and its history are untouched. Outbound only:
no inbound port, no public IP.

Runtime: Python 3.9+, standard library only. Nothing to install.

## What it sends

```
POST <LIGHTOS_APP_URL>/api/public/v1/infrastructure
Content-Type: application/json
Authorization: Bearer <LIGHTOS_COLLECTOR_KEY>
```

```json
{
  "version": 1,
  "platform": "linux",
  "batch_id": "<uuid>",
  "observed_at": "2026-10-29T06:37:45Z",
  "entities": [
    {"id": "local-box", "name": "local-box", "kind": "node", "state": "observed",
     "metrics": {"cpu_pct": 23.08, "memory_used_bytes": 1073741824,
                 "memory_total_bytes": 8589934592, "disk_used_bytes": 21474836480,
                 "disk_total_bytes": 107374182400}}
  ],
  "errors": []
}
```

The destination environment comes from the collector key, not from anything in
the payload. **The local environment id is not the hosted environment id.**

Mapping rules, each one covered by a test:

- `cpu.utilization` is a 0..1 ratio at the source, `cpu_pct` is a percent at the
  destination: 0.2308 becomes 23.08. A value outside 0..1 is not a ratio and is
  dropped rather than clamped.
- Only the root mount `/` is mapped. Mounts are never summed, because bind
  mounts and overlays report the same bytes more than once.
- Memory and disk stay in bytes.
- Unavailable, negative, non-finite and non-numeric readings are omitted. Zero
  is never substituted, and no GPU, network or fabric signal is invented.
- `observed_at` is the newest real collection time, never wall-clock now.
  Refreshing it would make stale data look live.
- One entity per hostname, the newest measurement for that host. Identity comes
  from `host.hostname`, never from a metric name.
- Nothing else is forwarded: the receiver rejects unknown fields.
- Caps: 1000 entities, 20 error strings of 240 characters, 1 MiB serialized.

## Delivery

- The pending payload and its `batch_id` are written to the state file
  **before** the first POST, so a timeout, a dropped connection or a restart
  retries the identical payload. A receiver that already stored it answers
  `duplicate`, which counts as delivered.
- Only **HTTP 200 with a JSON `status` of `accepted` or `duplicate`** confirms
  delivery. A 202, or a 200 with a friendly message and no status, leaves the
  batch pending.
- The source cursor advances only after confirmed delivery.
- 401/403 stops continuous sending and explains that the key needs checking.
- 429 is honoured, including `Retry-After`. 5xx and timeouts keep the batch.
- A payload the receiver rejects outright, or one that has aged past the 24 hour
  window, is marked blocked: it is never retried in a loop and never silently
  dropped. Run `--discard-pending` once you have looked at it.
- State is keyed by source, destination and source environment. The key is
  never written to it.

## Commands (PowerShell)

Enter the key in the shell, so it is not in a file, a commit or a command you
can scroll back to. `Read-Host -AsSecureString` keeps it off the screen:

```powershell
$secure = Read-Host "Collector key (lfc_...)" -AsSecureString
$env:LIGHTOS_COLLECTOR_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$env:LIGHTOS_APP_URL = "https://<published-origin>"
```

Look first. This sends nothing, changes no state, and needs no key:

```powershell
Set-Location 'C:\Users\bolao\Downloads\OS_run\collector\sender'
```

```powershell
python send.py --environment local-box --once --dry-run
```

One real send:

```powershell
python send.py --environment local-box --once
```

Continuously, at the default 60s:

```powershell
python send.py --environment local-box
```

| Flag | Default | Meaning |
|---|---|---|
| `--source` | `http://localhost:8787` | Local server. Must be loopback. |
| `--url` | `$LIGHTOS_APP_URL` | Hosted origin. Must be HTTPS. |
| `--environment` | required | Source environment, exact id or name |
| `--limit` | `200` | Measurements fetched per pass |
| `--interval` | `60` | Seconds between passes, minimum 10 |
| `--state-file` | `sender-state.json` | Delivery state |
| `--once` | | One pass, then exit |
| `--dry-run` | | Build and print; send nothing, change nothing |
| `--discard-pending` | | Drop a blocked batch after looking at it |
| `--allow-loopback-destination` | | Permit `http://localhost` for receiver development |

Exit codes: 0 delivered or nothing to do, 1 transient, 2 key refused,
3 needs you to look at it, 4 usage.

## Running the agent for real measurements

Forwarding cannot fix a stopped agent. If the source is stale, the agent is the
problem.

On Windows the agent cannot read `/proc`, so it must be pointed at a fixture
tree:

```powershell
Set-Location 'C:\Users\bolao\Downloads\OS_run\collector\lightcollect'
```

```powershell
python lightcollect.py --root <fixture-tree> --interval 60
```

**That is fixture data, not host monitoring.** CPU, memory, load and uptime come
from static files, so `cpu.utilization` is null, memory never moves, and the
numbers describe the fixture. It exercises delivery and nothing else.

For genuine measurements run the agent on the Linux machine being measured:

```bash
python3 lightcollect.py --interval 60
```

WSL is only a partial answer: it measures the Linux environment inside WSL, not
the Windows host. Its CPU and memory figures describe the WSL VM.

## Tests

```powershell
python -m unittest discover -s tests -t .
```

44 tests. `test_snapshot.py` covers payload shape, the CPU conversion,
omissions, root-only disk, latest-per-host, timestamp preservation and the
limits. `test_delivery.py` covers pending-before-post, restart reuse, the
confirmation rules, refusals, rate limiting, blocked recovery and that the key
is never printed, stored or followed through a redirect.

**Transport tests are mocked.** They prove this client behaves correctly against
a receiver that follows the contract. They are not evidence that the hosted
endpoint accepts anything.

## What is unverified

- No submission has ever been made to the hosted endpoint. It is not published
  yet, and no origin has been supplied.
- The contract above was given, not discovered. Nothing in this repository or
  the app repository defines `/api/public/v1/infrastructure`, so the field names
  and the confirmation semantics are as supplied and unconfirmed against a
  running receiver.
- Revocation behaviour has been exercised against a mock returning 401, not
  against a real revoked key.
