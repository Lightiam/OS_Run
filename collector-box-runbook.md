# Collector box: hardware spec and Ubuntu setup

Target: a small always-on Linux machine that reads host metrics and pushes them
outbound over HTTPS to LightOS. Outbound only: no inbound ports, no public IP,
no port forwarding.

> **Status (2026-10-30):** the receiver is deployed and live at
> `https://lightos.lightrailabs.ai/api/public/v1/infrastructure`, with its
> tables in place. An unauthenticated POST returns `401`. A real measurement
> from a Linux host was sent there with the maintained collector, stored, and
> the key was then revoked and refused with `401`.
>
> The box agent is the **maintained collector** in
> [`Lightiam/product-launch-hub` → `collector/`](https://github.com/Lightiam/product-launch-hub/tree/main/collector),
> also downloadable as `https://lightos.lightrailabs.ai/downloads/lightos-collector.zip`.
> `collector/server/` and `collector/lightcollect/` in this repository speak an
> older, never-deployed contract (`/functions/v1/collector-ingest`). They are
> kept only for local development on port 8787 and **will not deliver to the
> live app**. Do not follow older instructions that mention
> `<project-ref>.supabase.co` or `supabase functions deploy`.
>
> Not yet run by anyone: section 9 on a dedicated box, including the reboot.

## 1. Hardware

Minimum for a collector-only box:

| Item | Minimum | Comfortable |
| --- | --- | --- |
| CPU | 2 cores, x86-64 | 4 cores |
| RAM | 2 GB | 4 GB |
| Disk | 20 GB SSD/eMMC | 32 GB+ |
| Network | Any outbound path to 443 | Wired Ethernet |
| GPU | None | None |

The agent itself wants well under 1 core and 256 MB. The rest is OS and journald.
If the network is down, a failed snapshot is not kept; the next poll sends a fresh
measurement.

Reasonable boxes:

- **Intel N100/N150 mini PC** (~$150) — 4 cores, fanless, ~6 W idle. Best fit.
- **A spare laptop** — fine; set it to not suspend on lid close (see step 3).
- **A VM on an existing host** — 2 vCPU / 2 GB / 20 GB.
- **Raspberry Pi 5** — only if the agent ships arm64 builds. Prefer x86-64
  until that is decided.

Two placement rules that follow from what you are measuring:

- **Linux host metrics** must be collected on the machine you want to measure,
  on the metal or in the VM — *not* inside a container, or CPU, memory and disk
  readings describe the container, not the host.
- **Slurm** needs an account on an approved login node with `scontrol` and
  `squeue` on its `PATH`.

## 2. Install the OS

**Ubuntu Server 24.04 LTS**, not Desktop. Standard support to mid-2029.
22.04 LTS also works. During the installer:

- Choose the minimal install.
- Tick **Install OpenSSH server**.
- Import your SSH key, or add it afterwards with `ssh-copy-id`.
- Give it a DHCP reservation or a static address so you can find it again.
  Not required for the collector, which only dials out.

## 3. Base configuration

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y unattended-upgrades curl ca-certificates
sudo dpkg-reconfigure -plow unattended-upgrades
```

Clock — metrics with skewed timestamps are worse than no metrics:

```bash
sudo timedatectl set-timezone UTC
timedatectl    # expect: System clock synchronized: yes
```

Keep the journal bounded so logs cannot fill the disk:

```bash
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

On a laptop, stop it sleeping when you shut the lid:

```bash
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind
```

## 4. Firewall: outbound only

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp          # keep your own SSH access
sudo ufw enable
sudo ufw status verbose
```

No rule for the collector. It originates every connection, so deny-incoming
costs it nothing.

## 5. Service account

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin lightos
```

`lightos` is the account the shipped systemd unit runs as.

For Slurm collection, use an account that can run `squeue`/`scontrol` instead,
and skip the `--shell /usr/sbin/nologin` part.

## 6. Credentials

Sign in at <https://lightos.lightrailabs.ai/infrastructure>. Enter an
environment name (name it after this box), choose **Linux / CVM**, click
**Add environment**, then **Create collector key**. The key starts with `lfc_`,
is shown once, and only its SHA-256 is stored: lose it and you create a new one.
It is not an `lfk_` trace key.

Put it in a root-owned file, never on the command line, where it would show up
in `ps` output:

```bash
sudo tee /etc/lightos-collector.env >/dev/null <<'ENV'
LIGHTOS_ORIGIN=https://lightos.lightrailabs.ai
LIGHTOS_PLATFORM=linux
LIGHTOS_COLLECTOR_KEY=lfc_replace_me
ENV
sudo chmod 0600 /etc/lightos-collector.env
sudo chown root:root /etc/lightos-collector.env
```

`LIGHTOS_ORIGIN` is the app's own address. The collector adds
`/api/public/v1/infrastructure` itself.

## 7. Install the collector

```bash
sudo apt install -y python3-venv unzip
cd /tmp
curl -fsSLO https://lightos.lightrailabs.ai/downloads/lightos-collector.zip
sudo unzip -o lightos-collector.zip -d /opt
ls /opt/lightos-collector/lightos_collector.py
sudo python3 -m venv /opt/lightos-collector/.venv
sudo /opt/lightos-collector/.venv/bin/pip install -r /opt/lightos-collector/requirements.txt
```

One real send, which exits `0` on an accepted snapshot, `1` on a delivery
failure and `2` on a rejected or revoked key:

```bash
sudo -u lightos env $(sudo cat /etc/lightos-collector.env | xargs) \
  /opt/lightos-collector/.venv/bin/python /opt/lightos-collector/lightos_collector.py --once; echo "exit=$?"
```

Expect `Snapshot accepted: 1 entities, 0 collection errors`.

## 8. systemd unit

```bash
sudo cp /opt/lightos-collector/deploy/lightos-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lightos-collector
systemctl status lightos-collector
journalctl -u lightos-collector -f
```

The unit restarts on failure but not on exit `2`, so a revoked key stops the
service instead of retrying forever.

## 9. Verify (the test that has not been run on a dedicated box yet)

1. Reachability: `curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST https://lightos.lightrailabs.ai/api/public/v1/infrastructure`
   must print `HTTP 401`. A timeout or DNS failure is a network problem.
2. Open <https://lightos.lightrailabs.ai/infrastructure>, select the
   environment. **Last received** and the node's CPU, memory and disk must
   update about every 60 seconds.
3. `sudo reboot`. After it comes back, without logging in, the next
   measurement must arrive within two minutes. This proves the unit starts on
   its own.
4. Revoke the key in Infrastructure. Within one interval the service logs
   `Collector credential rejected` and stops (`systemctl status` shows exit 2).

Record the date and the hostname here when steps 1 to 4 pass.
