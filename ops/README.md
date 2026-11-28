# RADiant Validator Operations (Production: Neon DB + systemd)

## Production vs Local Development

| Environment | Database | Object Storage | Process Manager |
| :--- | :--- | :--- | :--- |
| **Production / Testnet VM** | **Neon Postgres** (managed) | **Hippius S3** | Native **`systemd`** + **Caddy** (`ops/systemd/`) |
| **Local dev & CI** | Local Postgres 17 (`docker-compose.yml`) | Local MinIO (`docker-compose.yml`) | `docker compose` |

The `ops/systemd/` deployment is portable across **any Linux VM with `systemd`** — regular cloud VMs (Hetzner, AWS, GCP, etc.), bare-metal servers, and Confidential CPU VMs (AMD SEV-SNP / Intel TDX). No Docker daemon, local Postgres, MinIO, or Adminer is required on the production host.

---

## 1. Provision Neon Postgres (Production DB)

1. Create a Neon project and database (e.g. `neondb`).
2. Create four login roles in the **Neon Console** (or Neon API) and record their passwords:
   - `radiant_api` — Intake API (`api.env`)
   - `radiant_watcher` — Chain watcher (`watcher.env`)
   - `radiant_worker` — Gate worker (`worker.env`)
   - `radiant_readonly` — Read-only operator inspection (`psql`)
3. Note the two Neon hostnames for your branch:
   - **Direct (unpooled):** `ep-xxxx.<region>.aws.neon.tech` — **required** for `admin.env` (`python -m db.migrate` uses session-level `pg_advisory_lock` and refuses pooled URLs).
   - **Pooled (`-pooler.`):** `ep-xxxx-pooler.<region>.aws.neon.tech` — recommended for `api.env`, `watcher.env`, and `worker.env` (`db/connection.py` automatically sets `prepare_threshold=None` for `-pooler.` URLs so psycopg3 works cleanly with Neon's PgBouncer transaction pooler). Direct URLs also work for runtime services.

> **Why `--grants-only`?** Neon's `neondb_owner` role cannot run `CREATE ROLE` / `ALTER ROLE`. Running `python -m db.migrate --grants-only` with `admin.env` verifies the pre-created roles exist and applies exact least-privilege `REVOKE`/`GRANT` rules on every table and sequence without needing service role passwords in `admin.env`.

---

## 2. Prepare the Linux Host

Install prerequisites on any modern Linux distro (Ubuntu 24.04 / Debian 12+ / RHEL / etc.):
- `python3` (>= 3.12)
- [`uv`](https://docs.astral.sh/uv/)
- [`caddy`](https://caddyserver.com/docs/install) (with ports `80/tcp` and `443/tcp` open to the internet)
- `rsync` (recommended for clean upgrades)

Ensure DNS for your API domain (e.g. `api.radiant.example`) points to the host's public IP before starting Caddy.

---

## 3. Install (or Upgrade) Services

From a clone of this repository on the host:

```bash
sudo ./ops/install_systemd.sh
```

What this does (idempotently — safe to re-run on upgrades):
- Creates isolated system users: `radiant-api`, `radiant-watcher`, `radiant-worker`.
- Syncs code into `/opt/radiant` and installs locked dependencies (`uv sync --frozen --extra validator --extra chain --extra sdk`).
- Creates `/var/lib/radiant/scratch` (`0750`, owned by `radiant-worker`) and `/etc/radiant` (`0700`, owned by `root:root`).
- Copies missing templates into `/etc/radiant/{admin,api,watcher,worker,caddy}.env` (`0600 root:root`). Existing `.env` files are never overwritten.
- Installs `radiant-{api,watcher,worker}.service`, `/etc/caddy/Caddyfile`, and `/etc/systemd/system/caddy.service.d/override.conf`, then runs `systemctl daemon-reload`.

Fill in secrets in each `/etc/radiant/*.env` file:
- `/etc/radiant/admin.env` — Neon **direct** URL (`neondb_owner`), `RADIANT_FEE_RECIPIENT`, Hippius S3 keys. Never loaded by daemons.
- `/etc/radiant/api.env` — Neon URL (`radiant_api`), Hippius S3 keys. **No** Modal, HF, DataHive, or wallet keys.
- `/etc/radiant/watcher.env` — Neon URL (`radiant_watcher`), Hippius S3 keys, fee-exempt hotkeys.
- `/etc/radiant/worker.env` — Neon URL (`radiant_worker`), Hippius S3 keys, `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`, `HF_PIN_ORG`/`HF_PIN_TOKEN`.
- `/etc/radiant/caddy.env` — `RADIANT_API_SITE=<your-domain>` and `RADIANT_API_UPSTREAM=127.0.0.1:8000`.

---

## 4. Optional: RAM-Backed Scratch (`tmpfs`)

By default `/var/lib/radiant/scratch` is an ordinary directory on disk, which works on any regular VM or bare-metal server.

On **Confidential CPU VMs (AMD SEV-SNP / Intel TDX)** — or any VM with sufficient RAM where you want zero scratch data on persistent disk — mount `tmpfs` on `/var/lib/radiant/scratch` so miner zips, weights, and future DataHive evaluation images stay exclusively in (hardware-encrypted) RAM:

```bash
# Add to /etc/fstab (adjust size to fit concurrent evaluations):
echo 'tmpfs /var/lib/radiant/scratch tmpfs size=8G,mode=0750,uid=radiant-worker,gid=radiant-worker,noexec,nosuid,nodev 0 0' | sudo tee -a /etc/fstab
sudo mount /var/lib/radiant/scratch
```

The `radiant-worker.service` unit works identically with or without this mount.

---

## 5. Migrate Database, Seed Campaign, and Start Services

Run migrations and apply least-privilege Neon grants using `admin.env`:

```bash
sudo bash -c '
  set -a; source /etc/radiant/admin.env; set +a
  cd /opt/radiant
  .venv/bin/python -m db.migrate --grants-only
  .venv/bin/python -m storage.bucket
'
```

Seed and open a campaign when ready:

```bash
sudo bash -c '
  set -a; source /etc/radiant/admin.env; set +a
  cd /opt/radiant
  .venv/bin/python -m campaign.seed --manifest campaign/fixtures/testnet-cxr-01.json
  .venv/bin/python -m campaign.set_status --campaign-id testnet-cxr-01 --status open
'
```

Enable and start all services:

```bash
sudo systemctl enable --now radiant-api radiant-watcher radiant-worker caddy
```

Verify health:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS "https://$(grep '^RADIANT_API_SITE=' /etc/radiant/caddy.env | cut -d= -f2)/health"
```

---

## 6. Day-2 Operations

### Logs & Status
```bash
sudo systemctl status radiant-api radiant-watcher radiant-worker caddy
sudo journalctl -u radiant-api -u radiant-watcher -u radiant-worker -f
```

### Upgrades
```bash
git pull
sudo ./ops/install_systemd.sh
sudo bash -c 'set -a; source /etc/radiant/admin.env; set +a; cd /opt/radiant && .venv/bin/python -m db.migrate --grants-only'
sudo systemctl restart radiant-api radiant-watcher radiant-worker
```

### Read-Only DB Inspection
Use `psql` (or any SQL client over TLS) with the `radiant_readonly` Neon connection string — no Adminer container is needed in production.

### Backups
Neon provides built-in point-in-time recovery (PITR) and branch snapshots. Before mainnet launch, pair Neon PITR with a scheduled off-Neon `pg_dump` to object storage (see `TODO.md`).
