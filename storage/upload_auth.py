"""Hotkey-signed, expiring authorization for one immutable solution upload.

Ported from Pareton storage/upload_auth.py. The miner signs the canonical
fields with its hotkey; the API verifies with the public key only, then
presigns one PUT. Replays hit the same upload UUID and checksum, and the
store's If-None-Match makes them idempotent, so no nonce table is needed.
"""

from __future__ import annotations

import time
from typing import Any

import config

from radiant_sdk.upload import FIELDS, upload_message  # noqa: F401  (one definition, SDK side)


def verify_upload_request(fields: dict[str, Any], signature: str, s: config.Settings | None = None,
                          now: float | None = None) -> int:
    """Return the authorization's remaining seconds, or raise ValueError."""
    from bittensor.sp_core import verify

    s = s or config.settings()
    remaining = int(fields["expires_at"]) - int(now if now is not None else time.time())
    if not 0 < remaining <= s.upload_auth_ttl_s + 60:  # miners' clocks may lead by a minute
        raise ValueError("upload authorization expired or too far in the future")
    if fields["network"] != s.network or fields["netuid"] != s.netuid:
        raise ValueError(f"upload authorization is for {fields['network']}/{fields['netuid']}, "
                         f"this validator runs {s.network}/{s.netuid}")
    try:
        ok = verify(upload_message(fields), bytes.fromhex(signature), fields["hotkey"])
    except (ValueError, TypeError):
        ok = False
    if not ok:
        raise ValueError("invalid hotkey signature")
    return remaining
