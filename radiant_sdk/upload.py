"""The signed upload request, shared by the SDK (signs) and the API (verifies)."""

from __future__ import annotations

import json
from typing import Any

MESSAGE_PREFIX = b"radiant.solution-upload.v1\n"
FIELDS = ("campaign_id", "hotkey", "sha256", "code_sha256", "size_bytes", "upload_id", "expires_at", "network",
          "netuid")


def upload_message(fields: dict[str, Any]) -> bytes:
    if set(fields) != set(FIELDS):
        raise ValueError(f"upload fields must be exactly {list(FIELDS)}")
    return MESSAGE_PREFIX + json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
