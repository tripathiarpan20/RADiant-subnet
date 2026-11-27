import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from campaign.manifest import build_manifest, canonical_json, compute_manifest_hash, load_manifest_file
from campaign.models import ManifestError, curve_slopes, quality_curve

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "campaign" / "fixtures" / "testnet-cxr-01.json"
# Pinned on 27 Nov 2026. If this changes, every stored campaign of this shape
# would recompute to a different hash: change it only on purpose.
FIXTURE_HASH = "sha256:cce1ee17af7dc01cf65f99ff3d85726f7aeacdfe7820b15fd181aa85044c5ab2"


def raw():
    """The fixture as build_manifest sees it once the .env recipient is filled in."""
    r = json.loads(FIXTURE.read_text())
    r["submission_fee"]["recipient"] = "5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a"
    return r


def minimal():
    return {"campaign_id": "cxr-tb-01", "labels": ["tuberculosis"],
            "submission_fee": {"amount_tao": "0.01", "recipient": "5Dest"}}


def test_fixture_hash_is_pinned():
    assert load_manifest_file(FIXTURE).manifest_hash == FIXTURE_HASH


def test_hash_is_stable_across_processes():
    code = ("from campaign.manifest import load_manifest_file;"
            f"print(load_manifest_file({str(FIXTURE)!r}).manifest_hash)")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True,
                         env={"PYTHONHASHSEED": "12345", "PATH": "",
                              "RADIANT_FEE_RECIPIENT": "5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a"})
    assert out.stdout.strip() == FIXTURE_HASH


def test_defaults_make_the_fixture_and_a_minimal_file_agree():
    # The fixture states every default explicitly, so with the same id, labels
    # and fee the two must hash the same.
    full = raw()
    small = {"campaign_id": full["campaign_id"], "labels": full["labels"],
             "submission_fee": full["submission_fee"], "visibility": full["visibility"]}
    assert build_manifest(small).manifest_hash == FIXTURE_HASH


def test_meaning_preserving_changes_keep_the_hash():
    r = raw()
    r["labels"] = list(reversed(r["labels"]))
    r["labels"][0] = r["labels"][0].upper()
    r["hardware"]["allowed_gpus"] = ["L40S", "L4"]
    r["emission_rule"]["start_weight"] = 0.1
    r["scoring_rule"]["curve"]["curvature"] = 2  # int vs float
    assert build_manifest(r).manifest_hash == FIXTURE_HASH


def test_operational_fields_stay_outside_the_hash():
    r = raw()
    r["title"] = "something else"
    r["submission_fee"] = {"amount_tao": "5", "recipient": "5Other"}
    r["visibility"] = {"mode": "private"}
    assert build_manifest(r).manifest_hash == FIXTURE_HASH


@pytest.mark.parametrize("path,value", [
    (("labels",), ["tuberculosis"]),
    (("limits", "model_load_timeout_s"), 60),
    (("hardware", "allowed_gpus"), ["L4"]),
    (("scoring_rule", "weights"), {"quality": 0.8, "speed": 0.1, "cost": 0.1}),
    (("rounds", "round_size"), 6),
    (("solution", "sdk_version"), "1.1.0"),
    (("emission_rule", "decay_blocks"), 100),
    (("data_pool", "countries"), ["ZA"]),
])
def test_every_pinned_term_moves_the_hash(path, value):
    r = raw()
    node = r
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    if path == ("labels",):
        r["scoring_rule"].pop("label_weights", None)
    assert build_manifest(r).manifest_hash != FIXTURE_HASH


@pytest.mark.parametrize("mutate,message", [
    (lambda r: r.update(campaign_id="Bad_ID"), "campaign_id"),
    (lambda r: r.update(campaign_id="a" * 25), "campaign_id"),
    (lambda r: r.update(campaign_id="cxr--tb"), "campaign_id"),
    (lambda r: r.update(labels=[]), "labels"),
    (lambda r: r.update(labels=["tb", "TB"]), "unique"),
    (lambda r: r.update(stake=1), "unknown keys"),
    (lambda r: r["hardware"].update(allowed_gpus=["RTX4090"]), "allowed_gpus"),
    (lambda r: r["scoring_rule"].update(weights={"quality": 0.5, "speed": 0.2, "cost": 0.2}), "sum to 1"),
    (lambda r: r["scoring_rule"].update(curve={"knee": 0.8, "knee_value": 0.5, "curvature": 4.0}), "steeper"),
    (lambda r: r["scoring_rule"].update(fbeta={"beta": 1}), "only when metric"),
    (lambda r: r["scoring_rule"].update(label_weights={"tuberculosis": 2}), "every label"),
    (lambda r: r["limits"].update(model_load_timeout_s=0), "model_load_timeout_s"),
    (lambda r: r["limits"].update(model_load_timeout_s=True), "integer"),
    (lambda r: r["rounds"].update(round_sample_size=30), "exceeds"),
    (lambda r: r["emission_rule"].update(floor_weight=0.5), "floor_weight"),
    (lambda r: r["harness"].update(input_dtype="float32"), "uint16"),
    (lambda r: r.pop("submission_fee"), "submission_fee"),
])
def test_bad_manifests_are_rejected(mutate, message):
    r = raw()
    mutate(r)
    with pytest.raises(ValueError, match=message):
        build_manifest(r)


def test_fbeta_metric_pins_beta_and_threshold_and_chance_zero():
    r = minimal()
    r["scoring_rule"] = {"metric": "fbeta"}
    s = build_manifest(r).pinned["scoring_rule"]
    assert s["fbeta"] == {"beta": 1.0, "threshold": 0.5}
    assert s["curve"]["chance"] == 0.0 and s["curve"]["knee"] == 0.6


def test_label_weights_default_to_one_per_label():
    s = build_manifest(minimal()).pinned["scoring_rule"]
    assert s["label_weights"] == {"tuberculosis": 1.0}


def test_speed_cost_heavy_weights_warn():
    r = minimal()
    r["scoring_rule"] = {"weights": {"quality": 0.4, "speed": 0.3, "cost": 0.3}}
    assert build_manifest(r).warnings
    assert not build_manifest(minimal()).warnings


def test_canonical_json_rejects_nan():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    assert compute_manifest_hash({"b": 1, "a": 2}) == compute_manifest_hash({"a": 2, "b": 1})


def test_build_does_not_mutate_input():
    r = raw()
    before = copy.deepcopy(r)
    build_manifest(r)
    assert r == before


# The plan's quality-curve table (Scoring section) at the default parameters.
@pytest.mark.parametrize("auroc,q", [(0.5, 0.0), (0.6, 0.267), (0.7, 0.533), (0.8, 0.8), (0.85, 0.8875),
                                     (0.9, 0.95), (0.95, 0.9875), (1.0, 1.0)])
def test_quality_curve_matches_plan_table(auroc, q):
    curve = build_manifest(minimal()).pinned["scoring_rule"]["curve"]
    assert quality_curve(auroc, curve) == pytest.approx(q, abs=1e-3)


def test_default_curve_slopes():
    below, above = curve_slopes({"chance": 0.5, "knee": 0.8, "knee_value": 0.8, "curvature": 2.0})
    assert below == pytest.approx(2.667, abs=1e-3) and above == pytest.approx(2.0)


def test_weights_formats_default_and_validation():
    assert build_manifest(minimal()).pinned["gates"]["weights_formats"] == ["safetensors"]
    r = minimal()
    r["gates"] = {"weights_formats": ["pickle"]}
    with pytest.raises(ValueError, match="weights_formats"):
        build_manifest(r)


def test_fee_recipient_comes_from_env_when_the_file_has_none(monkeypatch):
    m = load_manifest_file(FIXTURE)
    assert m.submission_fee["recipient"] == "5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a"
    monkeypatch.delenv("RADIANT_FEE_RECIPIENT")
    with pytest.raises(ManifestError, match="RADIANT_FEE_RECIPIENT"):
        load_manifest_file(FIXTURE)
