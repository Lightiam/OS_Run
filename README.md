# OS_Run

Working folder for the host-metrics collector: a small always-on Linux
box that reads its own host state and pushes it outbound over HTTPS to the
the app. No inbound ports, no public IP, no port forwarding.

## What is here

| Path | What it is |
|---|---|
| [`collector-box-runbook.md`](collector-box-runbook.md) | Hardware spec and the Ubuntu setup walkthrough for the collector box, end to end |
| [`collector/`](collector/README.md) | The collector subsystem: agent, ingest endpoint, schema and UI |
| `Olares/` | Submodule pointing at [beclab/Olares](https://github.com/beclab/Olares) at `3a1a0fb` |
| `Olares-main/` | A snapshot of the same upstream tree, extracted from the repository zip |

Olares was evaluated as a ready-made platform to run the collector on and then
set aside: its documented floor is 4 CPU cores, 8 GB RAM and 150 GB of SSD
(16 GB RAM on the Windows/WSL2 path, which its own docs mark as not for
production), which is far more machine than a metrics collector needs. The two
copies are kept here as the reference that conclusion was drawn from. They are
upstream's code, not this project's.

## The collector itself

The runbook is the entry point and [`collector/`](collector/README.md) holds
the code, all of it, so this repository is what you point at:

- **Agent** — `collector/lightcollect/`. One standard-library Python file:
  reads `/proc`, batches measurements as newline-delimited JSON, spools to disk
  when the network is down.
- **Endpoint and schema** — `collector/server/`. Deploys to Supabase.
- **UI** — `collector/app/`. Copied into the app's own tree, with the route,
  sidebar entry and generated types supplied as a patch.

Nothing here is deployed yet. `collector/README.md` has the two commands.

Keys are `lfc_` strings minted in the app's Infrastructure page. Only their
SHA-256 is stored, so a key is shown once and never recoverable.

## Cloning

`Olares/` is a submodule, so a plain clone leaves it empty:

```bash
git clone --recurse-submodules https://github.com/Lightiam/OS_Run.git
```

Or, in an existing clone, `git submodule update --init`.
