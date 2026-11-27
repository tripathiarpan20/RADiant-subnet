#!/usr/bin/env bash
# R1 exit gate: a fresh `docker compose up` seeds a campaign, and its
# manifest hash is stable across reruns.
#
#   scripts/r1_exit_gate.sh
#
# Runs under its own compose project (radiant-r1gate) with throwaway random
# passwords and ports, so it never touches a stack you run as `radiant`. It
# removes its containers and volumes when done (pass --keep to leave them).
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT=radiant-r1gate
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

ENV_FILE=$(mktemp -t radiant-r1gate-env.XXXXXX)
rand() { python3 -c 'import secrets; print(secrets.token_hex(20))'; }
port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
HTTP_PORT=$(port)
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(rand)
RADIANT_DB_READONLY_PASSWORD=$(rand)
RADIANT_DB_API_PASSWORD=$(rand)
RADIANT_DB_WATCHER_PASSWORD=$(rand)
RADIANT_DB_WORKER_PASSWORD=$(rand)
MINIO_ROOT_USER=radiant
MINIO_ROOT_PASSWORD=$(rand)
RADIANT_HTTP_BIND=127.0.0.1:${HTTP_PORT}
RADIANT_HTTPS_BIND=127.0.0.1:$(port)
RADIANT_MINIO_BIND=127.0.0.1
RADIANT_SEED_MANIFEST=campaign/fixtures/testnet-cxr-01.json
RADIANT_FEE_RECIPIENT=5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a
EOF

# Ports for MinIO and Adminer are fixed in the compose file; move them aside.
OVERRIDE=$(mktemp -t radiant-r1gate-override.XXXXXX).yml
cat > "$OVERRIDE" <<EOF
services:
  minio:
    ports: !override
      - "127.0.0.1:$(port):9000"
  adminer:
    ports: !override
      - "127.0.0.1:$(port):8080"
EOF

dc() { docker compose --progress quiet -p "$PROJECT" --env-file "$ENV_FILE" -f docker-compose.yml -f "$OVERRIDE" "$@"; }
cleanup() {
  if [[ $KEEP -eq 0 ]]; then dc down -v --remove-orphans >/dev/null 2>&1 || true; fi
  rm -f "$ENV_FILE" "$OVERRIDE"
}
trap cleanup EXIT

pass() { printf '  \033[32mpass\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; exit 1; }
seed_line() { dc logs --no-log-prefix init | grep -E '^(seeded|unchanged|replaced) ' | tail -1; }

EXPECTED=$(RADIANT_FEE_RECIPIENT=5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a \
           uv run --extra validator python -m campaign.seed --manifest campaign/fixtures/testnet-cxr-01.json --check \
           | awk '{print $3}')
echo "fixture hash (computed on the host): $EXPECTED"

echo "1. fresh up"
dc down -v --remove-orphans >/dev/null 2>&1 || true
dc up -d --build --wait >/dev/null
FIRST=$(seed_line)
[[ "$FIRST" == "seeded testnet-cxr-01 $EXPECTED" ]] || fail "first up: got '$FIRST'"
pass "init seeded testnet-cxr-01 with $EXPECTED"
dc logs --no-log-prefix init | grep -q '^bucket radiant-solutions created' || fail "bucket not created"
pass "MinIO bucket radiant-solutions created"

echo "2. rerun the seed job"
RERUN=$(dc run --rm --no-deps init 2>/dev/null | grep -E '^(seeded|unchanged) ' | tail -1)
[[ "$RERUN" == "unchanged testnet-cxr-01 $EXPECTED" ]] || fail "rerun: got '$RERUN'"
pass "rerun is a no-op with the same hash"

echo "3. down and up again (volumes kept)"
dc down >/dev/null
dc up -d --wait >/dev/null
AGAIN=$(seed_line)
[[ "$AGAIN" == "unchanged testnet-cxr-01 $EXPECTED" ]] || fail "second up: got '$AGAIN'"
pass "second up leaves the campaign unchanged"

echo "4. API through Caddy"
BODY=$(curl -fsS "http://127.0.0.1:${HTTP_PORT}/v1/campaigns/testnet-cxr-01")
API_HASH=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["manifest_hash"])' <<<"$BODY")
[[ "$API_HASH" == "$EXPECTED" ]] || fail "API hash $API_HASH"
curl -fsS "http://127.0.0.1:${HTTP_PORT}/health" >/dev/null || fail "/health"
pass "GET /v1/campaigns/testnet-cxr-01 returns $API_HASH"

echo "5. stored manifest recomputes to its hash"
STORED=$(dc exec -T postgres psql -U radiant -d radiant -Atc "SELECT manifest::text FROM campaigns WHERE id='testnet-cxr-01'")
RECOMPUTED=$(uv run --extra validator python -c 'import json,sys; from campaign.manifest import compute_manifest_hash; print(compute_manifest_hash(json.loads(sys.stdin.read())))' <<<"$STORED")
[[ "$RECOMPUTED" == "$EXPECTED" ]] || fail "recomputed $RECOMPUTED"
pass "hash recomputed from the stored JSONB matches"

echo "6. read-only role (what Adminer logs in with)"
RO_PW=$(grep '^RADIANT_DB_READONLY_PASSWORD=' "$ENV_FILE" | cut -d= -f2)
COUNT=$(dc exec -T -e PGPASSWORD="$RO_PW" postgres psql -h 127.0.0.1 -U radiant_readonly -d radiant -Atc "SELECT count(*) FROM campaigns")
[[ "$COUNT" == "1" ]] || fail "read-only SELECT returned '$COUNT'"
if dc exec -T -e PGPASSWORD="$RO_PW" postgres psql -h 127.0.0.1 -U radiant_readonly -d radiant -v ON_ERROR_STOP=1 \
     -c "SET default_transaction_read_only = off; UPDATE campaigns SET title='x'" >/dev/null 2>&1; then
  fail "read-only role could UPDATE"
fi
pass "read-only role can SELECT and cannot UPDATE"

echo "R1 exit gate passed."
