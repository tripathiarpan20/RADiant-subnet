#!/usr/bin/env bash
# Create .env from .env.example with fresh random passwords. Never overwrites.
#   scripts/init_env.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -e .env ]]; then echo ".env already exists; edit it by hand"; exit 1; fi
rand() { python3 -c 'import secrets; print(secrets.token_hex(24))'; }
sed -e "s|^POSTGRES_PASSWORD=$|POSTGRES_PASSWORD=$(rand)|" \
    -e "s|^RADIANT_DB_READONLY_PASSWORD=$|RADIANT_DB_READONLY_PASSWORD=$(rand)|" \
    -e "s|^RADIANT_DB_API_PASSWORD=$|RADIANT_DB_API_PASSWORD=$(rand)|" \
    -e "s|^RADIANT_DB_WATCHER_PASSWORD=$|RADIANT_DB_WATCHER_PASSWORD=$(rand)|" \
    -e "s|^RADIANT_DB_WORKER_PASSWORD=$|RADIANT_DB_WORKER_PASSWORD=$(rand)|" \
    -e "s|^MINIO_ROOT_PASSWORD=$|MINIO_ROOT_PASSWORD=$(rand)|" .env.example > .env
chmod 600 .env
echo "wrote .env with generated database and MinIO passwords (mode 600)."
echo "Still to fill in yourself: storage keys if not using MinIO, RADIANT_FEE_RECIPIENT, and later Modal, HF and DataHive keys."
