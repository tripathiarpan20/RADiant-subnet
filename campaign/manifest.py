"""Campaign manifest: the pinned terms, their canonical form and hash.

A manifest file (see campaign/fixtures/) holds the pinned terms plus three
operational fields that are deliberately *outside* the hash, as in Pareton:

    title            display name
    submission_fee   the initial fee; later fees are block-effective history
    visibility       the reveal policy (an operational disclosure policy)

Everything else is pinned. ``manifest_hash`` is sha256 over canonical JSON of
the normalised pin set (sorted keys, no whitespace, UTF-8), so it depends
only on what the terms mean, not on key order, list order where order is
meaningless, or 1 vs 1.0.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import models as m
from .fees import validate_submission_fee
from .visibility import validate_visibility

PINNED_KEYS = (
    "manifest_version",
    "campaign_id",
    "labels",
    "solution",
    "data_pool",
    "hardware",
    "limits",
    "harness",
    "rounds",
    "scoring_rule",
    "gates",
    "emission_rule",
)
OPERATIONAL_KEYS = ("title", "submission_fee", "visibility")


@dataclass(frozen=True)
class CampaignManifest:
    campaign_id: str
    pinned: dict[str, Any]
    manifest_hash: str
    title: str
    submission_fee: dict[str, str]
    visibility: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def normalise_pins(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise the pinned part of a manifest. Total: every key set."""
    version = raw.get("manifest_version", m.MANIFEST_VERSION)
    if version != m.MANIFEST_VERSION:
        raise m.ManifestError(f"manifest_version must be {m.MANIFEST_VERSION}")
    labels = m.validate_labels(raw.get("labels"))
    return {
        "manifest_version": m.MANIFEST_VERSION,
        "campaign_id": m.validate_campaign_id(raw.get("campaign_id")),
        "labels": labels,
        "solution": m.validate_solution(raw.get("solution")),
        "data_pool": m.validate_data_pool(raw.get("data_pool")),
        "hardware": m.validate_hardware(raw.get("hardware")),
        "limits": m.validate_limits(raw.get("limits")),
        "harness": m.validate_harness(raw.get("harness")),
        "rounds": m.validate_rounds(raw.get("rounds")),
        "scoring_rule": m.validate_scoring_rule(raw.get("scoring_rule"), labels),
        "gates": m.validate_gates(raw.get("gates")),
        "emission_rule": m.validate_emission_rule(raw.get("emission_rule")),
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def compute_manifest_hash(pinned: Mapping[str, Any]) -> str:
    """sha256:<hex> over canonical JSON of an already-normalised pin set."""
    return "sha256:" + hashlib.sha256(canonical_json(pinned).encode("utf-8")).hexdigest()


def build_manifest(raw: Mapping[str, Any]) -> CampaignManifest:
    if not isinstance(raw, Mapping):
        raise m.ManifestError("manifest must be an object")
    unknown = set(raw) - set(PINNED_KEYS) - set(OPERATIONAL_KEYS)
    if unknown:
        raise m.ManifestError(f"manifest has unknown keys: {sorted(unknown)}")
    pinned = normalise_pins(raw)
    title = raw.get("title", pinned["campaign_id"])
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        raise m.ManifestError("title must be 1-120 characters")
    if "submission_fee" not in raw:
        raise m.ManifestError("submission_fee is required ({amount_tao, recipient})")
    return CampaignManifest(
        campaign_id=pinned["campaign_id"],
        pinned=pinned,
        manifest_hash=compute_manifest_hash(pinned),
        title=title.strip(),
        submission_fee=validate_submission_fee(raw["submission_fee"]),
        visibility=validate_visibility(raw.get("visibility")),
        warnings=m.scoring_warnings(pinned["scoring_rule"]),
    )


def load_manifest_file(path: str | Path, **overrides: Any) -> CampaignManifest:
    """Read a manifest JSON file; ``overrides`` replace top-level keys (e.g. submission_fee).

    The fee recipient normally lives in the validator's .env, not in the
    file: when the file's submission_fee has no recipient, RADIANT_FEE_RECIPIENT
    supplies it. The recipient is outside the hash either way.
    """
    import os

    raw = json.loads(Path(path).read_text())
    raw.update({k: v for k, v in overrides.items() if v is not None})
    fee = raw.get("submission_fee")
    if isinstance(fee, dict) and "recipient" not in fee:
        recipient = os.environ.get("RADIANT_FEE_RECIPIENT", "").strip()
        if not recipient:
            raise m.ManifestError("the fee recipient is not set: put the owner coldkey in RADIANT_FEE_RECIPIENT "
                                  "(.env), or pass --fee-recipient")
        raw["submission_fee"] = {**fee, "recipient": recipient}
    return build_manifest(raw)
