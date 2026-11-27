#!/usr/bin/env bash
# Regenerate the reference miner's requirements.lock for the sandbox platform
# (linux x86_64, Python 3.12), hash-pinned, without the packages the base
# image already provides. The validator installs it with --no-deps
# --require-hashes, so the lock must list every extra package.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=radiant_sdk/base_image/requirements.txt
OUT=radiant_sdk/reference_miner/requirements.lock
TMP=$(mktemp)
uv pip compile radiant_sdk/reference_miner/requirements.in -c "$BASE" \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 --generate-hashes --no-header -q -o "$TMP"
BASE_NAMES=$(grep -oE '^[A-Za-z0-9._-]+==' "$BASE" | sed 's/==//' | tr 'A-Z_' 'a-z-' | sort -u)
EXCLUDE=$(printf ' --no-emit-package %s' $BASE_NAMES)
uv pip compile radiant_sdk/reference_miner/requirements.in -c "$BASE" $EXCLUDE \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 --generate-hashes --no-header -q -o "$OUT"
rm -f "$TMP"
echo "wrote $OUT: $(grep -cE '^[A-Za-z0-9]' "$OUT") packages"
