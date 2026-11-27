#!/usr/bin/env bash
# R2 exit gate: the reference miner passes `radiant-sdk validate`, and a miner
# missing an override fails with the same reason code the validator gives.
#
#   scripts/r2_exit_gate.sh [--api-url http://localhost:8080] [--static-only]
#
# The full run builds the image on Modal and qualifies on the reference
# miner's GPU (L4) with your Modal profile: a few cents, a few minutes the
# first time, faster once layers are cached. Without --api-url the campaign
# comes from the testnet fixture.
set -euo pipefail
cd "$(dirname "$0")/.."
API=""; STATIC=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-url) API="$2"; shift 2 ;;
    --static-only) STATIC="--static-only"; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
W=$(mktemp -d -t radiant-r2gate.XXXXXX)
trap 'rm -rf "$W"' EXIT
pass() { printf '  \033[32mpass\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; exit 1; }
sdk() { uv run --extra sdk python -m radiant_sdk "$@"; }

if [[ -n "$API" ]]; then
  SRC=(--api-url "$API")
else
  uv run --extra validator python - "$W/campaign.json" <<'PY'
import json, sys
from campaign.manifest import load_manifest_file
m = load_manifest_file("campaign/fixtures/testnet-cxr-01.json")
json.dump({"campaign_id": m.campaign_id, "status": "fixture", "manifest": m.pinned,
           "manifest_hash": m.manifest_hash}, open(sys.argv[1], "w"))
PY
  SRC=(--manifest "$W/campaign.json")
fi

echo "1. reference miner: prepare, validate${STATIC:+ (static only)}"
cp -r radiant_sdk/reference_miner "$W/miner" && cp "$W/miner/.env.template" "$W/miner/.env"
sdk prepare "$W/miner" --out "$W/build" >/dev/null
sdk validate "$W/build/solution.zip" --campaign testnet-cxr-01 "${SRC[@]}" $STATIC > "$W/validate.json" 2> "$W/validate.log" \
  || { tail -30 "$W/validate.log"; fail "reference miner did not pass validate"; }
STATUS=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$W/validate.json")
grep '^\[radiant-sdk:' "$W/validate.log" | sed 's/^/        /'
[[ "$STATUS" == "passed" || ( -n "$STATIC" && "$STATUS" == "static_passed" ) ]] || fail "status $STATUS"
pass "reference miner: $STATUS"

if [[ -z "$STATIC" ]]; then
  echo "2. submit with the fresh receipt"
  set +e; sdk submit "$W/build/solution.zip" --campaign testnet-cxr-01 "${SRC[@]}" 2> "$W/submit.log"; CODE=$?; set -e
  [[ $CODE -eq 3 ]] && grep -q "receipt OK" "$W/submit.log" || { cat "$W/submit.log"; fail "submit did not accept the receipt"; }
  pass "submit accepts the receipt and stops before sending anything (upload is R3)"
fi

echo "3. a miner missing map_to_labels"
cp -r "$W/miner" "$W/broken"
python3 - "$W/broken/model.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
open(p, "w").write(s[:s.index("    def map_to_labels")])
PY
sdk prepare "$W/broken" --out "$W/broken-build" >/dev/null
set +e; sdk validate "$W/broken-build/solution.zip" --campaign testnet-cxr-01 "${SRC[@]}" > "$W/broken.json" 2>/dev/null; set -e
SDK_CODE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("reason_code"))' "$W/broken.json")
VALIDATOR_CODE=$(uv run --extra validator python - "$W/broken-build/solution.zip" "${SRC[@]}" <<'PY'
import json, sys
from gate.contract import static_gates
zip_path, mode, src = sys.argv[1], sys.argv[2], sys.argv[3]
if mode == "--manifest":
    manifest = json.load(open(src))["manifest"]
else:
    import requests
    manifest = requests.get(f"{src}/v1/campaigns/testnet-cxr-01", timeout=10).json()["manifest"]
print(static_gates(open(zip_path, "rb").read(), manifest)[0])
PY
)
[[ "$SDK_CODE" == "CONTRACT_MISSING_OVERRIDE" ]] || fail "SDK gave $SDK_CODE"
[[ "$VALIDATOR_CODE" == "$SDK_CODE" ]] || fail "validator gave $VALIDATOR_CODE, SDK gave $SDK_CODE"
pass "SDK and validator both reject it with $SDK_CODE"

echo "4. no receipt, no submit"
set +e; sdk submit "$W/broken-build/solution.zip" --campaign testnet-cxr-01 "${SRC[@]}" 2> "$W/submit2.log"; CODE=$?; set -e
[[ $CODE -eq 2 ]] && grep -q refused "$W/submit2.log" || fail "submit without a receipt returned $CODE"
pass "submit refuses a zip without a receipt"
echo "R2 exit gate passed."
