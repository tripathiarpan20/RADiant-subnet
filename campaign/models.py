"""Validators for every section of a campaign manifest.

Each ``validate_*`` takes the raw section (or None for the default), rejects
unknown keys and bad values, and returns a *total* normalised copy: every key
present, numbers in a canonical type, order-insensitive lists sorted. Two
manifests that mean the same thing therefore hash the same, and a default can
change later without moving the hash of a campaign already stored.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping

MANIFEST_VERSION = 1

CAMPAIGN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?!-)){0,22}[a-z0-9]$")
LABEL_RE = re.compile(r"^[a-z][a-z0-9 _-]{0,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
BASE_CLASS_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

# Modal GPU names a campaign may allow. Miners pick one per solution.
KNOWN_GPUS = frozenset({"T4", "L4", "A10G", "L40S", "A100-40GB", "A100-80GB", "H100", "H200", "B200"})
CERTAINTIES = ("definite", "probable", "possible")
METRICS = frozenset({"auroc", "fbeta"})
SCORING_RULE_NAMES = frozenset({"weighted_quality_speed_cost_v1"})
EMISSION_RULE_NAMES = frozenset({"linear_decay"})


class ManifestError(ValueError):
    """A manifest section is malformed. The message names the field."""


# ── small helpers ───────────────────────────────────────────────────────────


def _section(value: Any, name: str, allowed: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ManifestError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ManifestError(f"{name} has unknown keys: {sorted(unknown)} (allowed: {sorted(allowed)})")
    return dict(value)


def _int(value: Any, field: str, *, lo: int, hi: int = 2**31 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{field} must be an integer")
    if not lo <= value <= hi:
        raise ManifestError(f"{field} must be in [{lo}, {hi}], got {value}")
    return value


def _num(value: Any, field: str, *, lo: float, hi: float, lo_open: bool = False, hi_open: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"{field} must be a number")
    out = float(value)
    if not math.isfinite(out):
        raise ManifestError(f"{field} must be finite")
    if out < lo or out > hi or (lo_open and out == lo) or (hi_open and out == hi):
        lb, rb = ("(" if lo_open else "["), (")" if hi_open else "]")
        raise ManifestError(f"{field} must be in {lb}{lo}, {hi}{rb}, got {out}")
    return out


def _str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{field} must be a list of strings")
    return value


# ── identity ────────────────────────────────────────────────────────────────


def validate_campaign_id(value: Any) -> str:
    """2 to 24 chars of [a-z0-9-], no leading, trailing or doubled hyphen.

    24 is the cap the HF pin name rad-<campaign>-<hotkey>-s<id> leaves.
    """
    if not isinstance(value, str) or not CAMPAIGN_ID_RE.match(value):
        raise ManifestError(
            "campaign_id must be 2-24 characters of a-z, 0-9 and single hyphens, "
            f"starting and ending with a letter or digit; got {value!r}"
        )
    return value


def validate_labels(value: Any) -> list[str]:
    labels = _str_list(value, "labels")
    out = [v.strip().lower() for v in labels]
    if not out:
        raise ManifestError("labels must not be empty")
    for v in out:
        if not LABEL_RE.match(v):
            raise ManifestError(f"label {v!r} must match {LABEL_RE.pattern}")
    if len(set(out)) != len(out):
        raise ManifestError("labels must be unique")
    return sorted(out)


# ── sections ────────────────────────────────────────────────────────────────

SOLUTION_KEYS = {"base_class", "sdk_version", "format"}


def validate_solution(value: Any) -> dict[str, Any]:
    v = _section(value, "solution", SOLUTION_KEYS)
    base_class = v.get("base_class", "radiant_sdk.models.v1:ChestXRayClassifier")
    if not isinstance(base_class, str) or not BASE_CLASS_RE.match(base_class):
        raise ManifestError("solution.base_class must look like package.module:ClassName")
    sdk = v.get("sdk_version", "1.0.0")
    if not isinstance(sdk, str) or not SEMVER_RE.match(sdk):
        raise ManifestError("solution.sdk_version must be MAJOR.MINOR.PATCH")
    fmt = v.get("format", "r1")
    if fmt != "r1":
        raise ManifestError("solution.format must be 'r1'")
    return {"base_class": base_class, "sdk_version": sdk, "format": fmt}


DATA_POOL_KEYS = {"source", "api_version", "countries", "positive_certainty"}


def validate_data_pool(value: Any) -> dict[str, Any]:
    v = _section(value, "data_pool", DATA_POOL_KEYS)
    if v.get("source", "datahive") != "datahive":
        raise ManifestError("data_pool.source must be 'datahive'")
    if v.get("api_version", "v1") != "v1":
        raise ManifestError("data_pool.api_version must be 'v1'")
    countries = sorted(set(_str_list(v.get("countries", []), "data_pool.countries")))
    for c in countries:
        if not COUNTRY_RE.match(c):
            raise ManifestError(f"data_pool.countries entry {c!r} must be an ISO 3166-1 alpha-2 code")
    cert = _str_list(v.get("positive_certainty", ["definite", "probable"]), "data_pool.positive_certainty")
    if not cert or set(cert) - set(CERTAINTIES):
        raise ManifestError(f"data_pool.positive_certainty must be a non-empty subset of {list(CERTAINTIES)}")
    return {
        "source": "datahive",
        "api_version": "v1",
        "countries": countries,  # empty = any country
        "positive_certainty": [c for c in CERTAINTIES if c in set(cert)],
    }


HARDWARE_KEYS = {"allowed_gpus", "max_cpus", "max_memory_mib"}


def validate_hardware(value: Any) -> dict[str, Any]:
    v = _section(value, "hardware", HARDWARE_KEYS)
    # L4 is left out of the default: on 27 Nov Modal queued L4 sandboxes for 15+ minutes.
    gpus = sorted(set(_str_list(v.get("allowed_gpus", ["L40S", "T4"]), "hardware.allowed_gpus")))
    if not gpus or set(gpus) - KNOWN_GPUS:
        raise ManifestError(f"hardware.allowed_gpus must be a non-empty subset of {sorted(KNOWN_GPUS)}")
    return {
        "allowed_gpus": gpus,
        "max_cpus": _int(v.get("max_cpus", 8), "hardware.max_cpus", lo=1, hi=64),
        "max_memory_mib": _int(v.get("max_memory_mib", 32768), "hardware.max_memory_mib", lo=1024, hi=1048576),
    }


LIMIT_DEFAULTS: dict[str, Any] = {
    "max_solution_zip_mb": 10,
    "max_solution_unzipped_mb": 50,
    "max_weights_mb": 2048,
    "image_cap_gb": 10,
    "modal_build_timeout_s": 1800,
    "model_load_timeout_s": 120,
    "eval_timeout_min_s": 300,
    "eval_timeout_per_megapixel_s": 0.05,
}


def validate_limits(value: Any) -> dict[str, Any]:
    v = _section(value, "limits", set(LIMIT_DEFAULTS))
    d = {**LIMIT_DEFAULTS, **v}
    out = {
        "max_solution_zip_mb": _int(d["max_solution_zip_mb"], "limits.max_solution_zip_mb", lo=1, hi=1024),
        "max_solution_unzipped_mb": _int(d["max_solution_unzipped_mb"], "limits.max_solution_unzipped_mb", lo=1, hi=4096),
        "max_weights_mb": _int(d["max_weights_mb"], "limits.max_weights_mb", lo=1, hi=200_000),
        "image_cap_gb": _int(d["image_cap_gb"], "limits.image_cap_gb", lo=1, hi=100),
        "modal_build_timeout_s": _int(d["modal_build_timeout_s"], "limits.modal_build_timeout_s", lo=60, hi=86_400),
        "model_load_timeout_s": _int(d["model_load_timeout_s"], "limits.model_load_timeout_s", lo=1, hi=3_600),
        "eval_timeout_min_s": _int(d["eval_timeout_min_s"], "limits.eval_timeout_min_s", lo=1, hi=86_400),
        "eval_timeout_per_megapixel_s": _num(d["eval_timeout_per_megapixel_s"], "limits.eval_timeout_per_megapixel_s",
                                             lo=0.0, hi=60.0),
    }
    if out["max_solution_unzipped_mb"] < out["max_solution_zip_mb"]:
        raise ManifestError("limits.max_solution_unzipped_mb must be >= max_solution_zip_mb")
    return out


HARNESS_KEYS = {"input_dtype", "max_batch_size", "prediction_decimals"}


def validate_harness(value: Any) -> dict[str, Any]:
    v = _section(value, "harness", HARNESS_KEYS)
    if v.get("input_dtype", "uint16") != "uint16":
        raise ManifestError("harness.input_dtype must be 'uint16' (raw pixels; miners own preprocessing)")
    return {
        "input_dtype": "uint16",
        "max_batch_size": _int(v.get("max_batch_size", 64), "harness.max_batch_size", lo=1, hi=1024),
        "prediction_decimals": _int(v.get("prediction_decimals", 3), "harness.prediction_decimals", lo=1, hi=6),
    }


ROUND_DEFAULTS = {
    "round_size": 5,
    "round_max_wait_s": 21_600,
    "round_sample_size": 100,
    "min_positives_per_label": 20,
    "min_negatives_per_label": 20,
}


def validate_rounds(value: Any) -> dict[str, Any]:
    v = _section(value, "rounds", set(ROUND_DEFAULTS))
    d = {**ROUND_DEFAULTS, **v}
    out = {
        "round_size": _int(d["round_size"], "rounds.round_size", lo=1, hi=100),
        "round_max_wait_s": _int(d["round_max_wait_s"], "rounds.round_max_wait_s", lo=60, hi=30 * 86_400),
        "round_sample_size": _int(d["round_sample_size"], "rounds.round_sample_size", lo=1, hi=10_000),
        "min_positives_per_label": _int(d["min_positives_per_label"], "rounds.min_positives_per_label", lo=0, hi=10_000),
        "min_negatives_per_label": _int(d["min_negatives_per_label"], "rounds.min_negatives_per_label", lo=0, hi=10_000),
    }
    if out["min_positives_per_label"] + out["min_negatives_per_label"] > out["round_sample_size"]:
        raise ManifestError("rounds: min_positives_per_label + min_negatives_per_label exceeds round_sample_size")
    return out


# ── scoring ─────────────────────────────────────────────────────────────────

SCORING_KEYS = {"name", "metric", "fbeta", "curve", "weights", "label_weights", "overtake_epsilon"}
CURVE_KEYS = {"chance", "knee", "knee_value", "curvature"}
WEIGHT_KEYS = ("quality", "speed", "cost")


def curve_slopes(curve: Mapping[str, float]) -> tuple[float, float]:
    """(slope just below the knee, slope just above it) of the quality curve."""
    below = curve["knee_value"] / (curve["knee"] - curve["chance"])
    above = (1 - curve["knee_value"]) * curve["curvature"] / (1 - curve["knee"])
    return below, above


def quality_curve(m: float, curve: Mapping[str, float]) -> float:
    """q(m): linear from chance to the knee, then 1-(1-x)^p diminishing returns."""
    m0, k, qk, p = curve["chance"], curve["knee"], curve["knee_value"], curve["curvature"]
    if m <= m0:
        return 0.0
    if m <= k:
        return qk * (m - m0) / (k - m0)
    return qk + (1 - qk) * (1 - (1 - (m - k) / (1 - k)) ** p)


def validate_scoring_rule(value: Any, labels: list[str]) -> dict[str, Any]:
    v = _section(value, "scoring_rule", SCORING_KEYS)
    name = v.get("name", "weighted_quality_speed_cost_v1")
    if name not in SCORING_RULE_NAMES:
        raise ManifestError(f"scoring_rule.name must be one of {sorted(SCORING_RULE_NAMES)}")
    metric = v.get("metric", "auroc")
    if metric not in METRICS:
        raise ManifestError(f"scoring_rule.metric must be one of {sorted(METRICS)}")

    fbeta: dict[str, float] | None = None
    if metric == "fbeta":
        fb = _section(v.get("fbeta"), "scoring_rule.fbeta", {"beta", "threshold"})
        fbeta = {
            "beta": _num(fb.get("beta", 1.0), "scoring_rule.fbeta.beta", lo=0.0, hi=10.0, lo_open=True),
            "threshold": _num(fb.get("threshold", 0.5), "scoring_rule.fbeta.threshold", lo=0.0, hi=1.0,
                              lo_open=True, hi_open=True),
        }
    elif "fbeta" in v:
        raise ManifestError("scoring_rule.fbeta applies only when metric is 'fbeta'")

    c = _section(v.get("curve"), "scoring_rule.curve", CURVE_KEYS)
    curve = {
        "chance": _num(c.get("chance", 0.5 if metric == "auroc" else 0.0), "scoring_rule.curve.chance", lo=0.0, hi=1.0,
                       hi_open=True),
        # F-beta's default knee is 0.6, not 0.8: with chance 0 a knee at 0.8
        # would make the curve steeper above the knee than below it.
        "knee": _num(c.get("knee", 0.8 if metric == "auroc" else 0.6), "scoring_rule.curve.knee", lo=0.0, hi=1.0,
                     lo_open=True, hi_open=True),
        "knee_value": _num(c.get("knee_value", 0.8), "scoring_rule.curve.knee_value", lo=0.0, hi=1.0,
                           lo_open=True, hi_open=True),
        "curvature": _num(c.get("curvature", 2.0), "scoring_rule.curve.curvature", lo=1.0, hi=10.0),
    }
    if curve["knee"] <= curve["chance"]:
        raise ManifestError("scoring_rule.curve.knee must be above chance")
    below, above = curve_slopes(curve)
    if above > below + 1e-12:
        raise ManifestError(
            f"scoring_rule.curve: the slope just above the knee ({above:.3f}) is steeper than below it "
            f"({below:.3f}); gains past the knee must be diminishing")

    w = _section(v.get("weights"), "scoring_rule.weights", set(WEIGHT_KEYS))
    weights = {k: _num(w.get(k, {"quality": 0.7, "speed": 0.15, "cost": 0.15}[k]), f"scoring_rule.weights.{k}",
                       lo=0.0, hi=1.0) for k in WEIGHT_KEYS}
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ManifestError(f"scoring_rule.weights must sum to 1, got {sum(weights.values())}")
    if weights["quality"] <= 0:
        raise ManifestError("scoring_rule.weights.quality must be above 0")

    lw_raw = v.get("label_weights")
    if lw_raw is None:
        label_weights = {lbl: 1.0 for lbl in labels}
    else:
        if not isinstance(lw_raw, Mapping):
            raise ManifestError("scoring_rule.label_weights must be an object")
        lw = {str(k).strip().lower(): val for k, val in lw_raw.items()}
        if set(lw) != set(labels):
            raise ManifestError("scoring_rule.label_weights must name every label exactly once")
        label_weights = {k: _num(lw[k], f"scoring_rule.label_weights.{k}", lo=0.0, hi=100.0, lo_open=True)
                         for k in labels}

    out: dict[str, Any] = {
        "name": name,
        "metric": metric,
        "curve": curve,
        "weights": weights,
        "label_weights": label_weights,
        "overtake_epsilon": _num(v.get("overtake_epsilon", 0.01), "scoring_rule.overtake_epsilon", lo=0.0, hi=0.5),
    }
    if fbeta is not None:
        out["fbeta"] = fbeta
    return out


def scoring_warnings(rule: Mapping[str, Any]) -> list[str]:
    """Non-fatal warnings the seed CLI prints.

    An all-negative model scores Q = 0, and as the fastest, cheapest entry it
    can take full speed and cost credit. Warn when that alone would tie a model
    sitting at the knee (Q = knee_value) with no speed or cost credit.
    """
    w = rule["weights"]
    if w["speed"] + w["cost"] >= w["quality"] * rule["curve"]["knee_value"]:
        return [
            f"speed + cost weight ({w['speed'] + w['cost']:.2f}) is at least quality weight x knee value "
            f"({w['quality'] * rule['curve']['knee_value']:.2f}): a trivial all-negative model could beat a "
            "working one on speed and cost alone"
        ]
    return []


# ── gates and emissions ─────────────────────────────────────────────────────

GATES_KEYS = {"obfuscation_rules", "weights_formats"}
# Weight file formats g5 accepts. safetensors cannot execute code when loaded,
# unlike pickle-based .pt/.bin checkpoints (decided 27 Nov).
WEIGHT_FORMATS = ("safetensors",)


def validate_gates(value: Any) -> dict[str, Any]:
    v = _section(value, "gates", GATES_KEYS)
    rules = v.get("obfuscation_rules", "radiant-v1")
    if rules != "radiant-v1":
        raise ManifestError("gates.obfuscation_rules must be 'radiant-v1'")
    formats = _str_list(v.get("weights_formats", ["safetensors"]), "gates.weights_formats")
    if not formats or set(formats) - set(WEIGHT_FORMATS):
        raise ManifestError(f"gates.weights_formats must be a non-empty subset of {list(WEIGHT_FORMATS)}")
    return {"obfuscation_rules": rules, "weights_formats": sorted(set(formats))}


EMISSION_KEYS = {"name", "start_weight", "floor_weight", "decay_blocks"}


def validate_emission_rule(value: Any) -> dict[str, Any]:
    """Pareton's linear_decay, defaults start 0.10, floor 0.02, 201,600 blocks."""
    v = _section(value, "emission_rule", EMISSION_KEYS)
    name = v.get("name", "linear_decay")
    if name not in EMISSION_RULE_NAMES:
        raise ManifestError(f"emission_rule.name must be one of {sorted(EMISSION_RULE_NAMES)}")
    start = _num(v.get("start_weight", 0.10), "emission_rule.start_weight", lo=0.0, hi=1.0)
    floor = _num(v.get("floor_weight", 0.02), "emission_rule.floor_weight", lo=0.0, hi=1.0)
    if floor > start:
        raise ManifestError("emission_rule.floor_weight must be <= start_weight")
    return {
        "name": name,
        "start_weight": start,
        "floor_weight": floor,
        "decay_blocks": _int(v.get("decay_blocks", 201_600), "emission_rule.decay_blocks", lo=1, hi=2**31 - 1),
    }
