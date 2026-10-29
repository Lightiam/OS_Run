# Collector box: hardware spec and Ubuntu setup

Target: a small always-on Linux machine that reads host metrics and pushes them
outbound over HTTPS to the LightOS app. Outbound only — no inbound ports, no
public IP, no port forwarding.

> **Status:** all three pieces now exist. The agent is `tools/lightcollect/` in
> the LightOS repo; the endpoint is the `collector-ingest` edge function plus
> the `20261028000000_collector_ingest.sql` migration in `agenticlightossh`;
> and keys are minted from **Infrastructure** in that app's dashboard
> (`/dashboard/infrastructure`). Nothing is deployed yet: push the migration
> and `supabase functions deploy collector-ingest` first, or the page will have
> no tables to read. If you would rather not run the app, the same key comes
> out of `select * from collector_environment_create('collector-box');`.

## 1. Hardware

Minimum for a collector-only box:

| Item | Minimum | Comfortable |
| --- | --- | --- |
| CPU | 2 cores, x86-64 | 4 cores |
| RAM | 2 GB | 4 GB |
| Disk | 20 GB SSD/eMMC | 32 GB+ |
| Network | Any outbound path to 443 | Wired Ethernet |
| GPU | None | None |

The agent itself wants roughly 1 core and 256-512 MB. The rest is OS, journald,
and an on-disk spool so measurements survive a network outage.

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
sudo useradd --system --no-create-home --shell /usr/sbin/nologin lightos-collector
```

For Slurm collection, use an account that can run `squeue`/`scontrol` instead,
and skip the `--shell /usr/sbin/nologin` part.

## 6. Credentials

Mint the key in the app first: **Dashboard → Infrastructure → New
environment**. Name it after this box. The key is shown once, with the two
lines below already filled in and a copy button, because only its SHA-256 is
stored — lose it and you mint a new one rather than recovering it.

The key is a secret. Put it in a root-owned file, never on the command line,
where it would show up in `ps` output:

```bash
sudo install -d -m 0750 /etc/lightos
sudo tee /etc/lightos/collector.env >/dev/null <<'ENV'
LIGHTOS_URL=https://<project-ref>.supabase.co
LIGHTOS_COLLECTOR_KEY=lfc_replace_me
ENV
sudo chmod 0640 /etc/lightos/collector.env
sudo chown root:lightos-collector /etc/lightos/collector.env
```

`LIGHTOS_URL` is the Supabase project host, which is what the Infrastructure
page copies for you — not the app's own domain.

## 7. Install the collector

The agent is a single standard-library file, so there is nothing to build and no
`pip install`. Ubuntu 24.04 already ships `python3`.

Copy `tools/lightcollect/lightcollect.py` from the LightOS repo to the box, then:

```bash
sudo install -m 0755 lightcollect.py /usr/local/bin/lightos-collector
python3 --version      # 3.9 or newer
```

Prove it reads this host correctly before wiring up systemd — `--print`
collects a measurement and prints it instead of sending it, so it needs no key
and no network:

```bash
/usr/local/bin/lightos-collector --print
```

You should see real `memory`, `load`, `disk` and `network` figures for the box.
`cpu.utilization` is measured over a one-second window here and reports `null`
rather than a fabricated zero if the counters do not move.

Then one real send, which exits non-zero if delivery failed:

```bash
sudo -u lightos-collector env $(sudo cat /etc/lightos/collector.env | xargs) \
  /usr/local/bin/lightos-collector --once; echo "exit=$?"
```

## 8. systemd unit

Save as `/etc/systemd/system/lightos-collector.service`:

```ini
[Unit]
Description=LightOS metrics collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=lightos-collector
EnvironmentFile=/etc/lightos/collector.env
ExecStart=/usr/local/bin/lightos-collector
Restart=always
RestartSec=10

StateDirectory=lightos-collector
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=256M
CPUQuota=25%

[Install]
WantedBy=multi-user.target
```

`ProtectSystem=strict` leaves `/proc` and `/sys` readable, so host metrics
still work; `StateDirectory` gives the agent `/var/lib/lightos-collector` for
its offline spool. Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lightos-collector
systemctl status lightos-collector
journalctl -u lightos-collector -f
```

## 9. Verify

Reachability from the box, before blaming the agent:

```bash
getent hosts <project-ref>.supabase.co
curl -sS -o /dev/null -w 'HTTP %{http_code} in %{time_total}s\n' \
  https://<project-ref>.supabase.co/functions/v1/collector-ingest
```

Any HTTP status back — `405` from the function itself, which only accepts POST,
or a platform `401` — proves DNS, TLS and egress work. Only a timeout or a
resolution failure is a real problem here. A `401` on an actual send means the
key is wrong or revoked.

Then open **Dashboard → Infrastructure**. The environment should turn *Live*
within a minute or two, showing this box's hostname and a rising measurement
count; **Metrics** opens the latest sample. *Stale* means it posted once and
then stopped — three missed ticks. Reboot once to prove the unit comes back up
on its own.

If the app shows nothing, check whether the agent is spooling rather than
sending — a non-empty spool means it is collecting fine and failing to deliver:

```bash
sudo wc -l /var/lib/lightos-collector/spool/measurements.jsonl
journalctl -u lightos-collector | grep "delivery failed" | tail -5
```
