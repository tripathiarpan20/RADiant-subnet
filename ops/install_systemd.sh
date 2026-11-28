#!/usr/bin/env bash
# Idempotent installer / upgrader for running the RADiant validator natively
# under systemd on any Linux VM (regular cloud VM, bare-metal, or Confidential VM).
#
# Usage:
#   sudo ./ops/install_systemd.sh
#
# Safe to re-run on upgrades: existing /etc/radiant/*.env files are never overwritten.
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "error: run as root (e.g. sudo $0)" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${RADIANT_INSTALL_DIR:-/opt/radiant}"
ETC_DIR="${RADIANT_ETC_DIR:-/etc/radiant}"
SCRATCH_DIR="${RADIANT_SCRATCH_DIR:-/var/lib/radiant/scratch}"
SYSTEMD_DIR="/etc/systemd/system"

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
    for candidate in /usr/local/bin/uv /root/.local/bin/uv /home/*/.local/bin/uv; do
        if [[ -x "$candidate" ]]; then
            UV_BIN="$candidate"
            break
        fi
    done
fi
if [[ -z "$UV_BIN" ]]; then
    echo "error: 'uv' not found in PATH; install uv first (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

NOLOGIN="/usr/sbin/nologin"
[[ -x "$NOLOGIN" ]] || NOLOGIN="/sbin/nologin"
[[ -x "$NOLOGIN" ]] || NOLOGIN="/bin/false"

echo "==> Ensuring dedicated service users"
for svc_user in radiant-api radiant-watcher radiant-worker; do
    if ! id -u "$svc_user" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir /nonexistent --shell "$NOLOGIN" "$svc_user"
    fi
done

echo "==> Syncing repository into ${INSTALL_DIR}"
mkdir -p "$INSTALL_DIR"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude='/.git/' \
        --exclude='/.venv/' \
        --exclude='/.env' \
        --exclude='/.env.local' \
        --exclude='__pycache__/' \
        --exclude='.pytest_cache/' \
        --exclude='.mypy_cache/' \
        --exclude='.ruff_cache/' \
        "${REPO_ROOT}/" "${INSTALL_DIR}/"
else
    echo "warning: rsync not found; using tar copy (stale files in ${INSTALL_DIR} will not be pruned)" >&2
    tar -C "$REPO_ROOT" \
        --exclude='.git' --exclude='.venv' --exclude='./.env' --exclude='./.env.local' \
        --exclude='__pycache__' --exclude='.pytest_cache' \
        -cf - . | tar -C "$INSTALL_DIR" -xf -
fi

echo "==> Installing Python dependencies with uv"
(
    cd "$INSTALL_DIR"
    "$UV_BIN" sync --frozen --extra validator --extra chain --extra sdk
)
chown -R root:root "$INSTALL_DIR"
chmod -R a+rX "$INSTALL_DIR"

echo "==> Preparing ${SCRATCH_DIR} and ${ETC_DIR}"
install -d -m 0755 /var/lib/radiant
install -d -o radiant-worker -g radiant-worker -m 0750 "$SCRATCH_DIR"
install -d -o root -g root -m 0700 "$ETC_DIR"

for tmpl in "${INSTALL_DIR}"/ops/systemd/env/*.env.example; do
    base="$(basename "$tmpl" .example)"
    dest="${ETC_DIR}/${base}"
    if [[ ! -f "$dest" ]]; then
        install -o root -g root -m 0600 "$tmpl" "$dest"
        echo "    created ${dest} (fill in secrets before starting services)"
    else
        echo "    kept existing ${dest}"
    fi
done

echo "==> Installing systemd unit files"
for unit in radiant-api.service radiant-watcher.service radiant-worker.service; do
    install -o root -g root -m 0644 "${INSTALL_DIR}/ops/systemd/${unit}" "${SYSTEMD_DIR}/${unit}"
done

if command -v caddy >/dev/null 2>&1 || [[ -d /etc/caddy ]]; then
    echo "==> Installing Caddyfile and caddy.service drop-in"
    install -d -m 0755 /etc/caddy "${SYSTEMD_DIR}/caddy.service.d"
    install -o root -g root -m 0644 "${INSTALL_DIR}/Caddyfile" /etc/caddy/Caddyfile
    install -o root -g root -m 0644 "${INSTALL_DIR}/ops/systemd/caddy-override.conf" \
        "${SYSTEMD_DIR}/caddy.service.d/override.conf"
else
    echo "note: caddy not installed; skipping /etc/caddy/Caddyfile (install caddy and re-run)"
fi

systemctl daemon-reload

cat <<EOF

Done. Next steps:
  1. Edit ${ETC_DIR}/{admin,api,watcher,worker,caddy}.env with your Neon DB URLs,
     Hippius S3 keys, Modal/HF tokens, and public API domain.
  2. Run database migrations & least-privilege grants (using Neon's DIRECT URL):
       sudo bash -c 'set -a; source ${ETC_DIR}/admin.env; set +a; \\
         cd ${INSTALL_DIR} && .venv/bin/python -m db.migrate --grants-only'
  3. Seed and open a campaign (see ${INSTALL_DIR}/ops/README.md).
  4. Enable and start the services:
       sudo systemctl enable --now radiant-api radiant-watcher radiant-worker caddy
EOF
