"""Exact campaign fees and block-effective, append-only fee history.

Ported from Pareton's campaign/fees.py. Amounts are decimal strings in TAO,
never floats, and must resolve to a whole RAO. The fee sits outside
manifest_hash: changing it appends a history entry effective from a block,
and the watcher (R3) checks each payment against the fee at the payment's
own block.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

MAX_RAO = 2**64 - 1
RAO_PER_TAO = Decimal(1_000_000_000)
SUBMISSION_FEE_KEYS = frozenset({"amount_tao", "recipient"})
HISTORY_KEYS = frozenset({"effective_from_block", "amount_tao", "recipient"})


def validate_submission_fee(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("submission_fee must be an object")
    unknown = set(value) - SUBMISSION_FEE_KEYS
    if unknown:
        raise ValueError(f"submission_fee has unknown keys: {sorted(unknown)} (allowed: {sorted(SUBMISSION_FEE_KEYS)})")

    raw = value.get("amount_tao")
    if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
        raise ValueError("submission_fee.amount_tao must be a non-negative TAO amount given as a string")
    try:
        amount = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("submission_fee.amount_tao must be a non-negative TAO amount") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("submission_fee.amount_tao must be a non-negative TAO amount")
    if amount > Decimal(MAX_RAO) / RAO_PER_TAO:
        raise ValueError("submission_fee.amount_tao exceeds the chain balance range")
    with localcontext() as ctx:
        ctx.prec = max(40, len(amount.as_tuple().digits) + 10)
        rao = amount * RAO_PER_TAO
    if rao != rao.to_integral_value():
        raise ValueError("submission_fee.amount_tao must resolve to a whole RAO")

    recipient = value.get("recipient")
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError("submission_fee.recipient must be a non-empty address")
    recipient = recipient.strip()
    if any(ch.isspace() for ch in recipient):
        raise ValueError("submission_fee.recipient must not contain whitespace")
    return {"amount_tao": "0" if amount == 0 else format(amount.normalize(), "f"), "recipient": recipient}


def submission_fee_rao(value: dict[str, Any]) -> int:
    return int(Decimal(validate_submission_fee(value)["amount_tao"]) * RAO_PER_TAO)


def validate_fee_history(value: Any) -> list[dict[str, Any]]:
    """A genesis entry at block 0, then strictly increasing blocks."""
    if not isinstance(value, list) or not value:
        raise ValueError("submission_fee_history must be a non-empty array")
    out, previous = [], -1
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != HISTORY_KEYS:
            raise ValueError("invalid submission_fee_history entry")
        block = entry["effective_from_block"]
        if type(block) is not int or block <= previous or block > 2**63 - 1:
            raise ValueError("fee history blocks must be strictly increasing non-negative integers")
        fee = validate_submission_fee({k: entry[k] for k in SUBMISSION_FEE_KEYS})
        out.append({**fee, "effective_from_block": block})
        previous = block
    if out[0]["effective_from_block"] != 0:
        raise ValueError("fee history must start at block zero")
    return out


def genesis_history(fee: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**validate_submission_fee(fee), "effective_from_block": 0}]


def append_fee(history: list[dict[str, Any]], fee: dict[str, Any], effective_from_block: int) -> list[dict[str, Any]]:
    entries = validate_fee_history(history)
    return validate_fee_history([*entries, {**validate_submission_fee(fee), "effective_from_block": effective_from_block}])


def fee_at_block(history: Any, block: int) -> dict[str, str]:
    if type(block) is not int or block < 0:
        raise ValueError("fee lookup requires a non-negative integer block")
    entry = next(e for e in reversed(validate_fee_history(history)) if e["effective_from_block"] <= block)
    return {k: entry[k] for k in SUBMISSION_FEE_KEYS}
