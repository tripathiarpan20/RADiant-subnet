"""Base models scored on the demo images, plus licence and label-set checks.

Candidates from the build plan: torchxrayvision densenet121-res224-all,
microsoft/rad-dino and google/cxr-foundation.

base_model_densenet_scored  densenet121-res224-all runs on the demo images
                            (raw 16-bit PNG in, the miner's own preprocessing)
                            and gets per-label AUROC wherever a DataHive
                            condition maps onto one of its 18 outputs
base_model_<m>_licence      the licence and access terms, read live from
                            PyPI / the Hugging Face API, and whether the model
                            has a classification head at all

Image sources:
    --source demo   DataHive demo API (DATAHIVE_DEMO_API_BASE, DATAHIVE_DEMO_API_KEY),
                    labels from /metadata findings
    --source dir    --images DIR --labels results.json in the old SotaRad
                    export schema (studies[].image_file, report_findings)

20 demo images give AUROC on a handful of positives, so this checks plumbing
and label coverage, not ranking.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from .report import GateResult, GateStatus

DENSENET_WEIGHTS = "densenet121-res224-all"

# DataHive condition (lower-cased) -> torchxrayvision output used as a proxy.
# Conditions with no entry (tuberculosis, silicosis, ...) have no head in the
# base model and need a fine-tuned one; the report lists them as uncovered.
CONDITION_TO_XRV = {
    "pneumonia": "Pneumonia",
    "viral pneumonitis": "Pneumonia",
    "pleural effusion": "Effusion",
    "effusion": "Effusion",
    "lobar collapse": "Atelectasis",
    "atelectasis": "Atelectasis",
    "cardiomegaly": "Cardiomegaly",
    "congestive heart failure": "Edema",
    "edema": "Edema",
    "pulmonary edema": "Edema",
    "consolidation": "Consolidation",
    "pneumothorax": "Pneumothorax",
    "nodule": "Nodule",
    "mass": "Mass",
    "emphysema": "Emphysema",
    "fibrosis": "Fibrosis",
    "fracture": "Fracture",
    "hernia": "Hernia",
    "pleural thickening": "Pleural_Thickening",
}

POSITIVE_CERTAINTY = {"definite", "probable"}
# A finding that means "nothing found" is not a label.
NO_FINDING = {"normal", "no finding", "no findings"}


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", choices=("demo", "dir"), default="demo")
    p.add_argument("--images", type=Path)
    p.add_argument("--labels", type=Path)
    p.add_argument("--device", default="cpu")
    p.add_argument("--skip-licences", action="store_true")


# ── labels ──────────────────────────────────────────────────────────────────


def positives_from_findings(findings: Iterable[dict]) -> set[str]:
    """Spec rule: certainty definite or probable is positive, possible is not.

    The old SotaRad export also carries status; 'previous' (healed) is not an
    active finding, so it is not a positive.
    """
    out = set()
    for f in findings:
        if f.get("certainty", "definite") not in POSITIVE_CERTAINTY:
            continue
        if f.get("status") in ("previous", "resolved"):
            continue
        cond = str(f["condition"]).strip().lower()
        if cond not in NO_FINDING:
            out.add(cond)
    return out


def load_dir(images: Path, labels: Path) -> list[tuple[str, bytes, set[str]]]:
    doc = json.loads(labels.read_text())
    out = []
    for s in doc["studies"]:
        name = Path(s["image_file"]).name
        out.append((name, (images / name).read_bytes(),
                    positives_from_findings(s.get("report_findings", {}).get("positive_findings", []))))
    return out


def load_demo() -> list[tuple[str, bytes, set[str]]]:
    from .datahive import DataHiveClient

    client = DataHiveClient(os.environ["DATAHIVE_DEMO_API_BASE"], os.environ["DATAHIVE_DEMO_API_KEY"], mode="demo")
    out = []
    for entry in client.list_all():
        name = entry["filename"]
        data, _, _, _ = client.fetch_image(name)
        meta = client.metadata(name)
        out.append((name, data, positives_from_findings(meta.get("findings", []))))
    return out


# ── model ───────────────────────────────────────────────────────────────────


def preprocess(png: bytes):
    """Raw PNG (8 or 16 bit) -> xrv's [-1024, 1024] 1x224x224 tensor."""
    import io

    import numpy as np
    import torchxrayvision as xrv
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    arr = np.array(img)
    if arr.ndim == 3:
        arr = arr[..., :3].mean(axis=2)
    maxval = 65535 if arr.dtype == np.uint16 or arr.max() > 255 else 255
    arr = xrv.datasets.normalize(arr.astype(np.float32), maxval)[None, ...]
    arr = xrv.datasets.XRayCenterCrop()(arr)
    arr = xrv.datasets.XRayResizer(224, engine="cv2" if _has_cv2() else "skimage")(arr)
    return arr


def _has_cv2() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


def auroc_by_label(y_true: dict[str, list[int]], y_score: dict[str, list[float]]) -> dict[str, Any]:
    from sklearn.metrics import roc_auc_score

    out = {}
    for label, t in y_true.items():
        pos = sum(t)
        if pos == 0 or pos == len(t):
            out[label] = {"auroc": None, "positives": pos, "n": len(t), "note": "one class only"}
        else:
            out[label] = {"auroc": round(float(roc_auc_score(t, y_score[label])), 3), "positives": pos, "n": len(t)}
    return out


def score_densenet(samples: list[tuple[str, bytes, set[str]]], device: str) -> GateResult:
    import numpy as np
    import torch
    import torchxrayvision as xrv

    t0 = time.monotonic()
    model = xrv.models.DenseNet(weights=DENSENET_WEIGHTS).eval().to(device)
    load_s = time.monotonic() - t0
    heads = list(model.pathologies)
    t0 = time.monotonic()
    batch = torch.from_numpy(np.stack([preprocess(b) for _, b, _ in samples])).to(device)
    prep_s = time.monotonic() - t0
    t0 = time.monotonic()
    with torch.no_grad():
        probs = model(batch).cpu().numpy()
    infer_s = time.monotonic() - t0

    conditions = sorted({c for _, _, pos in samples for c in pos})
    covered = {c: CONDITION_TO_XRV[c] for c in conditions if c in CONDITION_TO_XRV}
    uncovered = [c for c in conditions if c not in CONDITION_TO_XRV]
    y_true = {c: [int(c in pos) for _, _, pos in samples] for c in covered}
    y_score = {c: [round(float(p[heads.index(h)]), 3) for p in probs] for c, h in covered.items()}
    metrics = auroc_by_label(y_true, y_score)
    ok = len(samples) > 0 and probs.shape == (len(samples), len(heads))
    return GateResult(
        "base_model_densenet_scored", GateStatus.PASSED if ok else GateStatus.FAILED,
        f"{len(samples)} images scored; {len(covered)} of {len(conditions)} conditions map to a head "
        f"(uncovered: {', '.join(uncovered) or 'none'})",
        {"weights": DENSENET_WEIGHTS, "device": device, "images": len(samples),
         "load_model_s": round(load_s, 2), "preprocess_s": round(prep_s, 2), "inference_s": round(infer_s, 2),
         "heads": [h for h in heads if h], "condition_map": covered, "uncovered_conditions": uncovered,
         "per_label": metrics})


# ── licences ────────────────────────────────────────────────────────────────


def licence_gates() -> list[GateResult]:
    import requests

    out = []
    try:
        info = requests.get("https://pypi.org/pypi/torchxrayvision/json", timeout=20).json()["info"]
        classifiers = [c for c in info.get("classifiers", []) if c.startswith("License")]
        out.append(GateResult(
            "base_model_densenet_licence", GateStatus.PASSED,
            f"torchxrayvision {info['version']}: {info.get('license') or classifiers or 'see repo'}; "
            "weights trained on NIH, CheXpert, MIMIC-CXR, PadChest and others, whose data terms vary "
            "(CheXpert and MIMIC are research-use)",
            {"version": info["version"], "license": info.get("license"), "classifiers": classifiers,
             "head": "18 pathology outputs", "home": info.get("home_page") or info.get("project_url")}))
    except Exception as exc:
        out.append(GateResult("base_model_densenet_licence", GateStatus.FAILED, f"{type(exc).__name__}"))
    for gate, repo, head in (("base_model_rad_dino_licence", "microsoft/rad-dino", "none: image encoder only"),
                             ("base_model_cxr_foundation_licence", "google/cxr-foundation",
                              "none: embeddings (plus zero-shot text); needs a probe head")):
        try:
            d = requests.get(f"https://huggingface.co/api/models/{repo}", timeout=20).json()
            card = d.get("cardData") or {}
            lic = card.get("license_name") or card.get("license") or \
                next((t.split(":", 1)[1] for t in d.get("tags", []) if t.startswith("license:")), None)
            out.append(GateResult(
                gate, GateStatus.PASSED,
                f"{repo}: licence {lic}, gated={d.get('gated')}; classification head: {head}",
                {"repo": repo, "license": lic, "license_link": card.get("license_link"),
                 "gated": d.get("gated"), "library": d.get("library_name"), "head": head}))
        except Exception as exc:
            out.append(GateResult(gate, GateStatus.FAILED, f"{type(exc).__name__}"))
    return out


def run(args: argparse.Namespace) -> list[GateResult]:
    results: list[GateResult] = []
    try:
        import torchxrayvision  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError:
        results.append(GateResult("base_model_densenet_scored", GateStatus.BLOCKED,
                                  "model extras not installed (uv sync --extra r0 --extra r0-models)"))
    else:
        try:
            if args.source == "dir":
                if not (args.images and args.labels):
                    raise ValueError("--source dir needs --images and --labels")
                samples = load_dir(args.images, args.labels)
            else:
                if not (os.getenv("DATAHIVE_DEMO_API_BASE") and os.getenv("DATAHIVE_DEMO_API_KEY")):
                    samples = None
                    results.append(GateResult("base_model_densenet_scored", GateStatus.BLOCKED,
                                              "DATAHIVE_DEMO_API_BASE / DATAHIVE_DEMO_API_KEY not set"))
                else:
                    samples = load_demo()
            if samples is not None:
                r = score_densenet(samples, args.device)
                results.append(GateResult(r.gate, r.status, r.summary, {**r.details, "source": args.source}))
        except Exception as exc:
            results.append(GateResult("base_model_densenet_scored", GateStatus.FAILED,
                                      f"{type(exc).__name__}: {str(exc)[:300]}"))
    if not args.skip_licences:
        results += licence_gates()
    return results
