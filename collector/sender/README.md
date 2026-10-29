# Sender adapter

Forwards collector measurements to the Infrastructure endpoint as snapshots.
It reads from the local server over HTTP and never touches `collector.db`, so
local history stays exactly as it is while this is being tested.

Outbound only. The box needs no inbound port.

## What is unverified

The envelope — `version`, `platform`, `batch_id`, `observed_at`, `entities`,
`errors` — and the path `/api/public/v1/infrastructure` were given as the
contract. **Neither could be checked against any code available here.** The
contents of `entities` were not specified at all, so the shape below is a
construction, not a confirmed schema:

```json
{"kind": "host",
 "identity": {"hostname": "node-01", "os": "...", "kernel": "...", "arch": "..."},
 "observed_at": "2026-10-29T05:00:00Z",
 "readings": [{"name": "cpu.utilization", "value": 0.2308, "unit": "ratio"}]}
```

If the receiving end disagrees, change `snapshot.py` and nothing else. No other
file knows the shape.

## Running it

Put the key in an env file, never on the command line, so it stays out of shell
history and `ps` output:

```bash
# /etc/lightos/sender.env
LIGHTOS_APP_URL=https://<published-address>
LIGHTOS_COLLECTOR_KEY=lfc_...
```

Look first, without a key and without sending anything:

```bash
python send.py --from http://localhost:8787 --environment local-box --once --dry-run
```

Then one real send:

```bash
set -a; . /etc/lightos/sender.env; set +a
python send.py --from http://localhost:8787 --environment local-box --once
```

Then continuously:

```bash
python send.py --from http://localhost:8787 --environment local-box --interval 60
```

| Flag | Default | What it is |
|---|---|---|
| `--from` | `http://localhost:8787` | Local server to read from |
| `--url` | `$LIGHTOS_APP_URL` | Published address to send to |
| `--environment` | required | Environment id or name |
| `--platform` | `linux` | Goes in the envelope |
| `--limit` | `50` | Measurements to consider per pass |
| `--interval` | `60` | Seconds between passes |
| `--once` | | One pass, then exit |
| `--dry-run` | | Print the snapshot instead of sending it |

## What it guarantees

- **Host identity lives in the entity, never in a metric name.** Encoding it as
  `host.<name>.cpu.utilization` makes every name unique per machine, which
  breaks aggregation across a fleet and cannot be undone afterwards.
- **Units are explicit**, because the sources disagree: `cpu.utilization` is a
  0..1 ratio while `used_percent` is 0..100.
- **An unavailable reading is omitted, never sent as zero.** The agent reports
  null when a counter did not move, and a zero would be a measurement that was
  never taken.
- **Nothing is lost on refusal.** `sent-cursor.json` advances only after the
  endpoint accepts a batch, so a 401 from a revoked key, or any failure, leaves
  the window to be re-sent. Exit status is 2 for a refused key, 1 for any other
  rejection.
- **A measurement with no hostname is dropped and counted** in `errors` as
  `unattributed_measurement`, rather than being attributed to a guess.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Twelve tests over the mapping: identity placement, unit handling, omission of
unavailable readings, and that junk in place of metrics yields no readings
rather than an exception.

## Not included

Local SQLite history does not transfer by itself. `--limit` and the cursor only
look forward from the last accepted batch, so back-filling what the local server
already holds would be a separate import.
