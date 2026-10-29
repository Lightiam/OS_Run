# OS_Run

Working folder for the LightOS host-metrics collector: a small always-on Linux
box that reads its own host state and pushes it outbound over HTTPS to the
LightOS app. No inbound ports, no public IP, no port forwarding.

## What is here

| Path | What it is |
|---|---|
| [`collector-box-runbook.md`](collector-box-runbook.md) | Hardware spec and the Ubuntu setup walkthrough for the collector box, end to end |
| `Olares/` | Submodule pointing at [beclab/Olares](https://github.com/beclab/Olares) at `3a1a0fb` |
| `Olares-main/` | A snapshot of the same upstream tree, extracted from the repository zip |

Olares was evaluated as a ready-made platform to run the collector on and then
set aside: its documented floor is 4 CPU cores, 8 GB RAM and 150 GB of SSD
(16 GB RAM on the Windows/WSL2 path, which its own docs mark as not for
production), which is far more machine than a metrics collector needs. The two
copies are kept here as the reference that conclusion was drawn from. They are
upstream's code, not this project's.

## The collector itself

The runbook is the entry point. The code it installs and talks to lives in two
other repositories:

- **Agent** — `tools/lightcollect/` in [Lightiam/LightOS](https://github.com/Lightiam/LightOS).
  One standard-library Python file: reads `/proc`, batches measurements as
  newline-delimited JSON, spools to disk when the network is down.
- **Endpoint, schema and UI** — the `collector-ingest` edge function, the
  `collector_environments` / `collector_measurements` migration, and the
  Infrastructure page in
  [Lightingmedia/agenticlightossh](https://github.com/Lightingmedia/agenticlightossh).

Keys are `lfc_` strings minted in the app's Infrastructure page. Only their
SHA-256 is stored, so a key is shown once and never recoverable.

## Cloning

`Olares/` is a submodule, so a plain clone leaves it empty:

```bash
git clone --recurse-submodules https://github.com/Lightiam/OS_Run.git
```

Or, in an existing clone, `git submodule update --init`.
