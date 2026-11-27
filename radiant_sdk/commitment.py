"""RADiant solution commitment codec, shared by the SDK and the validator's watcher.

Positional pipe format within 384 bytes:

    r1|<campaign_id>|sha256:<64 hex>|code:<64 hex>|<object_key>|<pay_block>|<pay_tx>

* ``sha256`` is the whole solution zip, as uploaded; ``code`` is
  ``code_sha256`` (every file but .env), so a reveal with .env stripped stays
  checkable against the chain.
* ``object_key`` is the key in the validator's bucket that the API presigned:
  ``<prefix>/private/campaigns/<campaign>/solutions/<hotkey>/<upload_uuid>.zip``.
  The hotkey in it must be the committing hotkey (locator ownership).
* The fee proof is the transfer's ``(block, extrinsic index)``, as Pareton.

Finney's CommitmentInfo allows three 128-byte Raw fields, so the encoded
string must fit in 384 bytes. Only the latest commitment per hotkey counts.
"""

from __future__ import annotations

import re
from typing import Any

VERSION = "r1"
MAX_PLAINTEXT_BYTES = 384
_MAX_REF = 2**31 - 1
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CODE_RE = re.compile(r"^code:[0-9a-f]{64}$")
CAMPAIGN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?!-)){0,22}[a-z0-9]$")
KEY_RE = re.compile(
    r"^(?P<prefix>[a-z0-9][a-z0-9-]{0,30})/private/campaigns/(?P<campaign>[a-z0-9-]{2,24})/solutions/"
    r"(?P<hotkey>5[1-9A-HJ-NP-Za-km-z]{46,47})/(?P<upload>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.zip$")


def parse_object_key(key: str) -> dict[str, str] | None:
    m = KEY_RE.match(key)
    return m.groupdict() if m else None


def parse(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw.startswith(VERSION + "|"):
        return None
    parts = raw.split("|")
    if len(parts) not in (5, 7):
        return None
    _v, campaign_id, sha, code, key = parts[:5]
    if not CAMPAIGN_RE.match(campaign_id) or not HASH_RE.match(sha) or not CODE_RE.match(code):
        return None
    loc = parse_object_key(key)
    if loc is None or loc["campaign"] != campaign_id:
        return None
    out: dict[str, Any] = {"campaign_id": campaign_id, "sha256": sha, "code_sha256": "sha256:" + code[5:],
                           "object_key": key, "key_hotkey": loc["hotkey"], "payment_block": None, "payment_tx": None}
    if len(parts) == 7:
        b, t = parts[5], parts[6]
        if not (b.isdigit() and t.isdigit()):
            return None
        block, tx = int(b), int(t)
        if not 1 <= block <= _MAX_REF or tx > _MAX_REF:
            return None
        out.update(payment_block=block, payment_tx=tx)
    return out


def encode(*, campaign_id: str, sha256: str, code_sha256: str, object_key: str,
           payment_block: int | None = None, payment_tx: int | None = None) -> str:
    if not HASH_RE.match(code_sha256):
        raise ValueError("code_sha256 must be sha256:<64 hex>")
    parts = [VERSION, campaign_id, sha256, "code:" + code_sha256[7:], object_key]
    if (payment_block is None) != (payment_tx is None):
        raise ValueError("payment_block and payment_tx go together")
    if payment_block is not None:
        parts += [str(payment_block), str(payment_tx)]
    raw = "|".join(parts)
    if len(raw.encode()) > MAX_PLAINTEXT_BYTES:
        raise ValueError(f"commitment is {len(raw.encode())} bytes, over {MAX_PLAINTEXT_BYTES}")
    if parse(raw) is None:
        raise ValueError("commitment does not round-trip; check the campaign id and object key")
    return raw


def plaintext_fields(payload: str) -> list[dict]:
    """Data::Raw chunks for Commitments.set_commitment (at most three of 128 bytes)."""
    raw = payload.encode()
    fields = [{f"Raw{len(c)}": "0x" + c.hex()} for c in (raw[i:i + 128] for i in range(0, len(raw), 128))]
    if len(fields) > 3:
        raise ValueError("commitment needs more than 3 Raw fields")
    return fields
