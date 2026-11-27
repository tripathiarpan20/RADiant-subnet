"""The validator-owned entrypoint that runs a solution inside the sandbox.

    python -m radiant_sdk.harness qualify --solution /solution --weights /weights \
        --labels tuberculosis,silicosis --batch-size 32 --load-timeout 120 --decimals 3

Modes:
    qualify  gate g8 and ``radiant-sdk validate``: no evaluation data. Runs the
             public warmup set (synthetic, deterministic uint16 X-ray-sized
             arrays), checks every output, and prints a report. Its whole log
             is shown to the miner.
    run      a round (R6): reads .npy uint16 images from --inputs and returns
             predictions only. Its log is never shown to the miner.

The last stdout line is ``RADIANT_RESULT <json>``. The clock for
``model_load_timeout`` starts before the solution's module is imported and
stops when ``load_model`` returns, so import-time work counts. Container
start and image pull happen before this process exists and never count.
The validator also times the sandbox from outside; the harness's own timings
are for the miner's report.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import math
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

from . import reasons as R
from .models.v1 import ChestXRayClassifier

RESULT_PREFIX = "RADIANT_RESULT "

# Public warmup set: sizes like real chest X-rays, including non-square and
# odd sizes, so a miner's resizing is exercised. Contents are deterministic.
WARMUP_SHAPES = ((2500, 2048), (2048, 2048), (3000, 2500), (1760, 2140), (1024, 1024), (2836, 2336),
                 (2048, 1662), (2500, 3000))


def warmup_images(n: int = len(WARMUP_SHAPES), seed: int = 20261127) -> list[np.ndarray]:
    """Synthetic chest-X-ray-like uint16 arrays: a bright mediastinum, darker lungs, noise."""
    rng = np.random.default_rng(seed)
    out = []
    for h, w in (WARMUP_SHAPES * (n // len(WARMUP_SHAPES) + 1))[:n]:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cx, cy = w / 2, h / 2
        body = np.exp(-(((xx - cx) / (0.45 * w)) ** 2 + ((yy - cy) / (0.55 * h)) ** 2))
        lungs = np.exp(-(((np.abs(xx - cx) - 0.2 * w) / (0.13 * w)) ** 2 + ((yy - 0.45 * h) / (0.3 * h)) ** 2))
        img = 2400 * body - 1500 * lungs + 900 + rng.normal(0, 60, (h, w))
        out.append(np.clip(img, 0, 4095).astype(np.uint16))  # 12-bit, like most CR/DX
    return out


def emit(result: dict[str, Any]) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)


def fail(code: str, detail: str, **extra: Any) -> None:
    emit({"status": "failed", "reason_code": code, "detail": detail[-2000:], **extra})
    sys.stdout.flush()
    os._exit(1)


def import_solution(solution_dir: Path) -> type[ChestXRayClassifier]:
    sys.path.insert(0, str(solution_dir / "src"))
    sys.path.insert(0, str(solution_dir))
    spec = importlib.util.spec_from_file_location("radiant_solution_model", solution_dir / "model.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    found = [obj for obj in vars(module).values()
             if inspect.isclass(obj) and issubclass(obj, ChestXRayClassifier) and obj is not ChestXRayClassifier
             and obj.__module__ == module.__name__]
    if len(found) != 1:
        raise LookupError(f"model.py must define exactly one ChestXRayClassifier subclass, found {len(found)}")
    return found[0]


def check_output(mapped: Any, labels: tuple[str, ...], decimals: int) -> dict[str, float]:
    """Exactly the labels, each a finite float in [0, 1]; rounded. Raises ValueError otherwise."""
    if not isinstance(mapped, dict):
        raise ValueError(f"map_to_labels returned {type(mapped).__name__}, not dict")
    if set(mapped) != set(labels):
        missing, extra = sorted(set(labels) - set(mapped)), sorted(set(mapped) - set(labels))
        raise ValueError(f"labels differ: missing {missing}, unexpected {extra}")
    out = {}
    for k in labels:
        v = mapped[k]
        if isinstance(v, bool) or not isinstance(v, (int, float, np.floating, np.integer)):
            raise ValueError(f"{k}: {type(v).__name__} is not a number")
        f = float(v)
        if not math.isfinite(f) or not 0.0 <= f <= 1.0:
            raise ValueError(f"{k}: {f} is not a probability in [0, 1]")
        out[k] = round(f, decimals)
    return out


class _Watchdog:
    """Kills the process with a reason code if a phase overruns."""

    def __init__(self, seconds: float, code: str, detail: str) -> None:
        self._timer = threading.Timer(seconds, fail, args=(code, detail))
        self._timer.daemon = True

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()


def _device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def load(args) -> tuple[ChestXRayClassifier, dict[str, float]]:
    labels = tuple(args.labels.split(","))
    t0 = time.monotonic()
    with _Watchdog(args.load_timeout, R.LOAD_TIMEOUT, f"import + load_model exceeded {args.load_timeout}s"):
        try:
            cls = import_solution(Path(args.solution))
        except Exception:
            fail(R.IMPORT_FAILED, traceback.format_exc())
        cls.labels = labels
        try:
            model = cls()
        except Exception:
            fail(R.INIT_FAILED, traceback.format_exc())
        t_import = time.monotonic() - t0
        device = _device()
        try:
            model.load_model(Path(args.weights), device)
        except Exception:
            fail(R.LOAD_CRASH, traceback.format_exc())
    return model, {"import_s": round(t_import, 3), "load_total_s": round(time.monotonic() - t0, 3),
                   "device": device, "load_timeout_s": args.load_timeout}


def evaluate(model: ChestXRayClassifier, images: list[np.ndarray], batch_size: int, decimals: int,
             timeout: float) -> tuple[list[dict[str, float] | None], list[str], float]:
    """Batched predictions; a bad output fails that item only. Returns (preds, errors, seconds)."""
    labels = tuple(model.labels)
    preds: list[dict[str, float] | None] = []
    errors: list[str] = []
    t0 = time.monotonic()
    with _Watchdog(timeout, R.EVAL_TIMEOUT, f"evaluation exceeded {timeout}s"):
        for i in range(0, len(images), batch_size):
            chunk = images[i:i + batch_size]
            try:
                raw = model.run_batched_evaluation(list(chunk))
                if not isinstance(raw, (list, tuple)) or len(raw) != len(chunk):
                    raise ValueError(f"run_batched_evaluation returned {len(raw) if hasattr(raw, '__len__') else '?'} "
                                     f"outputs for {len(chunk)} images")
            except Exception:
                fail(R.EVAL_CRASH, traceback.format_exc())
            for j, r in enumerate(raw):
                try:
                    preds.append(check_output(model.map_to_labels(r), labels, decimals))
                except Exception as exc:
                    preds.append(None)
                    errors.append(f"item {i + j}: {exc}")
    return preds, errors, time.monotonic() - t0


def qualify(args) -> None:
    model, timing = load(args)
    images = warmup_images()
    labels = tuple(model.labels)
    t0 = time.monotonic()
    with _Watchdog(args.eval_timeout, R.EVAL_TIMEOUT, "run_single_evaluation exceeded the timeout"):
        try:
            single = check_output(model.map_to_labels(model.run_single_evaluation(images[0])), labels, args.decimals)
        except ValueError as exc:
            fail(R.BAD_OUTPUT, f"run_single_evaluation/map_to_labels: {exc}")
        except Exception:
            fail(R.EVAL_CRASH, traceback.format_exc())
    single_s = time.monotonic() - t0
    preds, errors, batch_s = evaluate(model, images, args.batch_size, args.decimals, args.eval_timeout)
    if errors:
        fail(R.BAD_OUTPUT, "; ".join(errors[:5]), **timing)
    emit({"status": "passed", "mode": "qualify", **timing, "single_s": round(single_s, 4),
          "batch_s": round(batch_s, 4), "images": len(images), "batch_size": args.batch_size,
          "labels": list(labels), "example_output": single})


def run(args) -> None:
    model, timing = load(args)
    paths = sorted(Path(args.inputs).glob("*.npy"))
    images = [np.load(p, allow_pickle=False) for p in paths]
    for p, img in zip(paths, images):
        if img.dtype != np.uint16 or img.ndim != 2:
            fail(R.SANDBOX_FAILED, f"input {p.name} is not a 2-D uint16 array")  # validator bug, not the miner's
    preds, errors, batch_s = evaluate(model, images, args.batch_size, args.decimals, args.eval_timeout)
    emit({"status": "completed", "mode": "run", **timing, "batch_s": round(batch_s, 4), "failed_items": len(errors),
          "predictions": {p.stem: pr for p, pr in zip(paths, preds)}})


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m radiant_sdk.harness")
    p.add_argument("mode", choices=("qualify", "run"))
    p.add_argument("--solution", default="/solution")
    p.add_argument("--weights", default="/weights")
    p.add_argument("--inputs", default="/inputs")
    p.add_argument("--labels", required=True)
    p.add_argument("--batch-size", type=int, required=True)
    p.add_argument("--decimals", type=int, default=3)
    p.add_argument("--load-timeout", type=float, required=True)
    p.add_argument("--eval-timeout", type=float, default=600.0)
    args = p.parse_args(argv)
    (qualify if args.mode == "qualify" else run)(args)
    sys.stdout.flush()
    os._exit(0)  # don't let miner-started threads keep the sandbox alive


if __name__ == "__main__":
    main()
