# OS_Run

Working folder for the host-metrics collector box: a small always-on Linux
machine that reads its own host state and pushes it outbound over HTTPS to
LightOS at <https://lightos.lightrailabs.ai>. No inbound ports, no public IP, no
port forwarding.

## What is here

| Path | What it is |
|---|---|
| [`collector-box-runbook.md`](collector-box-runbook.md) | Hardware spec and the Ubuntu setup for the box, end to end. Start here. |
| [`collector/`](collector/README.md) | Local development only: a SQLite ingest server on port 8787, the `lightcollect` agent and UI experiments. |

## What is deployed

- **Receiver:** `https://lightos.lightrailabs.ai/api/public/v1/infrastructure`,
  live, with its tables applied. Unauthenticated requests get `401`.
- **Box agent:** the maintained collector in
  [`Lightiam/product-launch-hub` → `collector/`](https://github.com/Lightiam/product-launch-hub/tree/main/collector),
  also at `https://lightos.lightrailabs.ai/downloads/lightos-collector.zip`.
- **Forwarding this repo's local server to the live app:** `collector/sender/`
  in product-launch-hub.

`collector/server/` (the `collector-ingest` function and its migration) and
`collector/lightcollect/` here use an older contract that was never deployed and
will not deliver to the live app. Use them only against the local server.

Keys are `lfc_` strings created in the app's Infrastructure page. Only their
SHA-256 is stored, so a key is shown once and never recoverable.

## Olares

Olares was evaluated as a platform to run the collector on and set aside: its
documented floor is 4 CPU cores, 8 GB RAM and 150 GB SSD, far more machine than
a metrics collector needs. The upstream copies that were kept for reference have
been removed; the source is at <https://github.com/beclab/Olares>.
