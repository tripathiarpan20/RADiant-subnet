#!/usr/bin/env bash
# R4 exit gate: malicious fixtures fail with stable codes, nothing that fails
# before g9 is pinned, pins are private and named rad-<campaign>-<hotkey>-s<id>,
# and the reference miner reaches queued. Real Modal, Hugging Face and storage.
#
#   scripts/r4_exit_gate.sh
#
# Needs in .env: HF_PIN_ORG and HF_PIN_TOKEN, and the RADIANT_S3_* bucket
# (Hippius). Modal: your `modal setup` login on this machine, or
# MODAL_TOKEN_ID/SECRET in .env. Uses a throwaway Postgres and the bucket
# prefix radiant-r4gate/. Cost: two image builds (cached layers) and two L40S
# qualifications (R4_GPU=L4 to use L4), one of which waits out the 120 s load
# timeout: about $0.10.
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="radiant-r4gate-pg-$$"
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=gate -p "127.0.0.1:${PORT}:5432" postgres:17.12-alpine >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
for i in $(seq 1 40); do docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 0.5; done
sleep 1
(
  set -a; source .env; set +a
  export RADIANT_DATABASE_URL="postgresql://postgres:gate@127.0.0.1:${PORT}/postgres"
  export RADIANT_DB_READONLY_PASSWORD=gate-ro
  export RADIANT_S3_PREFIX=radiant-r4gate
  export RADIANT_WORKER_SCRATCH="$(mktemp -d -t radiant-r4scratch.XXXXXX)"
  export RADIANT_FEE_RECIPIENT="${RADIANT_FEE_RECIPIENT:-5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a}"
  R4_DRY_PIN="${R4_DRY_PIN:-0}" uv run --extra validator --extra sdk --extra chain python -m scripts.r4_gate
)
echo "R4 exit gate passed."
