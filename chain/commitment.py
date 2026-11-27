"""Fold on-chain commitments into SolutionCommitment rows (latest per hotkey).

The wire format lives in radiant_sdk.commitment, so the SDK that writes a
commitment and the watcher that reads it share one codec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from radiant_sdk.commitment import (MAX_PLAINTEXT_BYTES, encode, parse, parse_object_key,  # noqa: F401
                                    plaintext_fields)


@dataclass(frozen=True)
class SolutionCommitment:
    uid: int
    hotkey: str
    coldkey: str
    commit_block: int
    campaign_id: str
    sha256: str
    code_sha256: str
    object_key: str
    raw: str
    payment_block: int | None = None
    payment_tx: int | None = None


class _MetagraphLike(Protocol):
    hotkeys: list[str]


def build(metagraph: _MetagraphLike, revealed: dict[str, list[tuple[int, str]]]) -> dict[int, SolutionCommitment]:
    """{uid: latest valid RADiant commitment} for every registered hotkey."""
    out: dict[int, SolutionCommitment] = {}
    coldkeys = list(getattr(metagraph, "coldkeys", []) or [])
    for uid, hotkey in enumerate(str(h) for h in metagraph.hotkeys):
        entries = revealed.get(hotkey) or []
        if not entries:
            continue
        block, raw = max(entries, key=lambda e: e[0])
        p = parse(raw)
        if p is None:
            continue
        out[uid] = SolutionCommitment(
            uid=uid, hotkey=hotkey, coldkey=str(coldkeys[uid]) if uid < len(coldkeys) else "",
            commit_block=int(block), campaign_id=p["campaign_id"], sha256=p["sha256"],
            code_sha256=p["code_sha256"], object_key=p["object_key"], raw=raw,
            payment_block=p["payment_block"], payment_tx=p["payment_tx"])
    return out
