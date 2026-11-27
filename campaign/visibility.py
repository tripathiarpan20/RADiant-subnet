"""Reveal policy, deliberately outside manifest_hash (as Pareton's patch_visibility).

RADiant reveals every evaluated submission, so the default is
``public_after_reveal`` with Pareton's 48 h delay. A campaign may add a fast
reveal: when a round's leader has quality Q below
``fast_reveal_quality_threshold``, every entry scored in that round uses the
shorter ``fast_reveal_delay_s`` instead (decided 26 Nov).
"""

from __future__ import annotations

import math
from typing import Any

DEFAULT_REVEAL_DELAY_S = 172_800
MAX_DELAY_S = 2_147_483_647
PUBLIC_KEYS = {"mode", "reveal_delay_s", "fast_reveal_quality_threshold", "fast_reveal_delay_s"}


def _delay(value: Any, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_DELAY_S:
        raise ValueError(f"visibility.{field} must be an integer from 0 to {MAX_DELAY_S}")
    return value


def validate_visibility(value: Any) -> dict[str, Any]:
    if value is None:
        value = {"mode": "public_after_reveal"}
    if not isinstance(value, dict):
        raise ValueError("visibility must be an object")
    mode = value.get("mode")
    if mode == "private":
        if set(value) != {"mode"}:
            raise ValueError("visibility: private takes no other fields")
        return {"mode": "private"}
    if mode != "public_after_reveal":
        raise ValueError("visibility.mode must be private or public_after_reveal")
    unknown = set(value) - PUBLIC_KEYS
    if unknown:
        raise ValueError(f"visibility has unknown keys: {sorted(unknown)}")
    delay = _delay(value.get("reveal_delay_s", DEFAULT_REVEAL_DELAY_S), "reveal_delay_s")
    threshold = value.get("fast_reveal_quality_threshold")
    if threshold is None:
        if "fast_reveal_delay_s" in value:
            raise ValueError("visibility.fast_reveal_delay_s needs fast_reveal_quality_threshold")
        return {"mode": mode, "reveal_delay_s": delay, "fast_reveal_quality_threshold": None, "fast_reveal_delay_s": None}
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) \
            or not 0 < threshold <= 1:
        raise ValueError("visibility.fast_reveal_quality_threshold must be in (0, 1]")
    if "fast_reveal_delay_s" not in value:
        raise ValueError("visibility.fast_reveal_delay_s is required with fast_reveal_quality_threshold")
    fast = _delay(value["fast_reveal_delay_s"], "fast_reveal_delay_s")
    if fast > delay:
        raise ValueError("visibility.fast_reveal_delay_s must not exceed reveal_delay_s")
    return {"mode": mode, "reveal_delay_s": delay, "fast_reveal_quality_threshold": float(threshold),
            "fast_reveal_delay_s": fast}


def reveal_delay_for(policy: dict[str, Any], leader_quality: float | None) -> int | None:
    """Delay that applies to a round's entries, or None when private."""
    p = validate_visibility(policy)
    if p["mode"] == "private":
        return None
    t = p["fast_reveal_quality_threshold"]
    if t is not None and leader_quality is not None and leader_quality < t:
        return p["fast_reveal_delay_s"]
    return p["reveal_delay_s"]
