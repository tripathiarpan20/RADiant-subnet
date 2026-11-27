#!/usr/bin/env bash
# R3 exit gate on testnet (netuid 292): a paid submission is ingested exactly
# once, and reused, underpaid, wrong-recipient and foreign-locator proofs are
# rejected.
#
#   scripts/r3_exit_gate.sh [--wallet bruh_dev_coldkey] [--hotkey bruh_dev_hotkey_3] [--storage hippius|minio]
#                           [--keep] [--stack-only] [--resume sha256:<zip hash>]
#
# --resume skips validate and submit and checks a submission already
# committed on chain (e.g. after a failed run that had already paid).
#
# Run it in your own terminal: it pays the campaign fee (0.01 testTAO) from
# the wallet's coldkey, so it asks for the coldkey password once. It brings up
# its own stack (project radiant-r3gate, random passwords and ports). Uploads
# go to the Hippius bucket from .env (prefix radiant-r3gate/) by default, or to
# the stack's own MinIO with --storage minio. Removes the stack unless --keep.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)
WALLET=bruh_dev_coldkey; HOTKEY=bruh_dev_hotkey_3; KEEP=0; STACK_ONLY=0; STORAGE=hippius; RESUME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wallet) WALLET="$2"; shift 2 ;;
    --hotkey) HOTKEY="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --stack-only) STACK_ONLY=1; shift ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --resume) RESUME="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
pass() { printf '  \033[32mpass\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; exit 1; }

if [[ $STACK_ONLY -eq 0 && -z "$RESUME" ]] && grep -q '^HF_REPO_ID=torchxrayvision/' radiant_sdk/reference_miner/.env.template; then
  fail "the reference miner still points at the pickle weights; publish the safetensors copy first:
    HF_TOKEN=<write token> uv run --extra sdk --extra r0-models python scripts/publish_reference_weights.py --out /tmp/ref-weights --repo <org>/densenet121-res224-all-safetensors
  then set HF_REPO_ID and HF_REVISION in radiant_sdk/reference_miner/.env.template"
fi

PROJECT=radiant-r3gate
W=$(mktemp -d -t radiant-r3gate.XXXXXX)
rand() { python3 -c 'import secrets; print(secrets.token_hex(20))'; }
port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
HTTP_PORT=$(port); MINIO_PORT=$(port)
# The fee recipient is the owner wallet in .env (RADIANT_FEE_RECIPIENT).
RECIPIENT=$(bash -c 'set -a; source .env 2>/dev/null; echo "${RADIANT_FEE_RECIPIENT:-}"')
[[ -n "$RECIPIENT" ]] || fail "set RADIANT_FEE_RECIPIENT (the owner coldkey) in .env"
cat > "$W/env" <<EOF
POSTGRES_PASSWORD=$(rand)
RADIANT_DB_READONLY_PASSWORD=$(rand)
MINIO_ROOT_USER=radiant
MINIO_ROOT_PASSWORD=$(rand)
RADIANT_HTTP_BIND=127.0.0.1:${HTTP_PORT}
RADIANT_HTTPS_BIND=127.0.0.1:$(port)
RADIANT_NETWORK=test
RADIANT_NETUID=292
RADIANT_FEE_RECIPIENT=${RECIPIENT}
RADIANT_FEE_TAO=0.01
RADIANT_POLL_INTERVAL_S=12
RADIANT_S3_PRESIGN_ENDPOINT_URL=http://127.0.0.1:${MINIO_PORT}
RADIANT_SEED_MANIFEST=campaign/fixtures/testnet-cxr-01.json
EOF
if [[ "$STORAGE" == "hippius" ]]; then
  # The bucket the storage probe passed on (R0_HIPPIUS_S3_* in .env), under its own prefix.
  ( set -a; source .env; set +a
    echo "RADIANT_S3_ENDPOINT_URL=$R0_HIPPIUS_S3_ENDPOINT"
    echo "RADIANT_S3_PRESIGN_ENDPOINT_URL=$R0_HIPPIUS_S3_ENDPOINT"
    echo "RADIANT_S3_REGION=$R0_HIPPIUS_S3_REGION"
    echo "RADIANT_S3_BUCKET=$R0_HIPPIUS_S3_BUCKET"
    echo "RADIANT_S3_ACCESS_KEY=$R0_HIPPIUS_S3_ACCESS_KEY"
    echo "RADIANT_S3_SECRET_KEY=$R0_HIPPIUS_S3_SECRET_KEY" ) >> "$W/env"
  grep -q '^RADIANT_S3_BUCKET=.' "$W/env" || { echo "R0_HIPPIUS_S3_* not set in .env; use --storage minio"; exit 2; }
fi
cat > "$W/override.yml" <<EOF
services:
  minio:
    ports: !override
      - "127.0.0.1:${MINIO_PORT}:9000"
  adminer:
    ports: !override
      - "127.0.0.1:$(port):8080"
EOF
dc() { docker compose --progress quiet -p "$PROJECT" --env-file "$W/env" -f docker-compose.yml -f "$W/override.yml" "$@"; }
cleanup() { if [[ $KEEP -eq 0 ]]; then dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$W"; fi; }
trap cleanup EXIT
API="http://127.0.0.1:${HTTP_PORT}"

echo "1. stack up ($STORAGE storage), campaign open, fee 0.01 testTAO to ${RECIPIENT:0:8}…"
dc down -v --remove-orphans >/dev/null 2>&1 || true
dc up -d --build --wait >/dev/null
dc run --rm --no-deps init python -m campaign.set_status testnet-cxr-01 open >/dev/null
curl -fsS "$API/v1/info" | grep -q '"netuid":292' || fail "/v1/info does not report netuid 292"
SEEDED=$(curl -fsS "$API/v1/campaigns/testnet-cxr-01" | python3 -c 'import json,sys; print(json.load(sys.stdin)["submission_fee"]["recipient"])')
[[ "$SEEDED" == "$RECIPIENT" ]] || fail "campaign fee recipient is $SEEDED, .env says $RECIPIENT"
pass "API at $API on test/292; campaign fee goes to ${RECIPIENT:0:8}… (from .env)"
# The API presigns only for registered hotkeys, which the watcher syncs.
for i in $(seq 1 20); do
  dc exec -T postgres psql -U radiant -d radiant -Atc "SELECT count(*) FROM registered_hotkeys" 2>/dev/null \
    | grep -qv '^0$' && break
  sleep 3
  [[ $i -eq 20 ]] && fail "the watcher has not synced the metagraph"
done
pass "watcher synced netuid 292's registered hotkeys"
if [[ $STACK_ONLY -eq 1 ]]; then
  dc run --rm --no-deps watcher python -m worker.watcher --once > "$W/scan.json" || fail "watcher scan failed"
  pass "watcher scanned netuid 292: $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$W/scan.json") RADiant commitments on chain"
  echo "stack-only check passed."; exit 0
fi

if [[ -n "$RESUME" ]]; then
  SHA="$RESUME"
  echo "2-3. resuming: $SHA is already committed on chain"
else
  echo "2. reference miner: prepare and validate (Modal)"
  cp -r radiant_sdk/reference_miner "$W/miner" && cp "$W/miner/.env.template" "$W/miner/.env"
  printf 'RADIANT_API_URL=%s\nRADIANT_NETWORK=test\nRADIANT_NETUID=292\nWALLET_NAME=%s\nWALLET_HOTKEY=%s\n' \
    "$API" "$WALLET" "$HOTKEY" > "$W/miner/miner.env"
  sdk() { (cd "$W/miner" && uv run --project "$REPO" --extra sdk radiant-sdk "$@"); }
  sdk prepare . --out build >/dev/null
  sdk validate build/solution.zip --campaign testnet-cxr-01 > "$W/validate.json" 2> "$W/validate.log" \
    || { tail -25 "$W/validate.log"; fail "validate did not pass"; }
  pass "validate passed; receipt written"

  echo "3. submit: upload, pay (you will be asked to confirm and for the coldkey password), commit"
  sdk submit build/solution.zip --campaign testnet-cxr-01 \
    | tee "$W/submit.json"
  SHA=$(python3 -c 'import json,re,sys; t=open(sys.argv[1]).read(); print(json.loads(t[t.index("{"):t.rindex("}")+1])["sha256"])' "$W/submit.json")
  pass "committed $SHA"
fi

echo "4. watcher ingests it"
for i in $(seq 1 50); do
  if curl -fsS "$API/v1/submissions/$SHA" 2>/dev/null | grep -q '"submission_id"'; then break; fi
  sleep 12
  [[ $i -eq 50 ]] && { dc logs --tail 40 watcher; fail "not ingested after 10 minutes"; }
done
pass "ingested by the watcher"

echo "5. exactly once, and bad proofs rejected (using the real payment)"
dc run --rm --no-deps -v "$(pwd)/scripts:/app/scripts:ro" watcher python -m scripts.r3_checks "$SHA"
echo "R3 exit gate passed."
