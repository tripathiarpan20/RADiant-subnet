"""Offline Modal sandbox on L4 and L40S: isolation, observed GPU, model load.

Ported from vidaio's scripts/competition_modal_phase0_probe.py and extended to
GPUs and a timed model load. Per GPU it builds one image that bakes the
torchxrayvision DenseNet-121 weights in at build time (network on), then runs
a sandbox with block_network=True and no secrets that:

  - tries direct-IP, DNS and HTTPS egress (all must fail)
  - checks no Modal credential or OIDC token is in the environment
  - reads the GPU with nvidia-smi and torch
  - loads the model from the baked weights and times load and inference

Gates per GPU (g = l4, l40s):
    modal_<g>_offline       egress blocked and no credentials inside
    modal_<g>_gpu_observed  the sandbox saw the GPU that was asked for
    modal_<g>_model_load    load_model finished inside --model-load-timeout and
                            single and batched inference ran on CUDA
and once:
    modal_timeout_enforced  a sandbox that overruns its timeout is killed

Spend is bounded: each GPU sandbox has a hard --sandbox-timeout (default
240 s). At list prices that is at most a few cents per GPU.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from typing import Any

from .report import GateResult, GateStatus

# Approximate Modal list prices (USD per GPU-second), Nov 2026, for the
# report's cost estimate only. R6 locks real rates in sandbox/pricing.py.
APPROX_USD_PER_S = {"L4": 0.000222, "L40S": 0.000542, "CPU": 0.0000131}

REMOTE_PROBE = r'''
import json, os, socket, subprocess, time, urllib.request
out = {}
def blocked(fn):
    try:
        fn(); return False
    except Exception as exc:
        return type(exc).__name__
out["direct_ip_blocked"] = blocked(lambda: socket.create_connection(("1.1.1.1", 443), timeout=3).close())
out["dns_blocked"] = blocked(lambda: socket.getaddrinfo("huggingface.co", 443))
out["https_blocked"] = blocked(lambda: urllib.request.urlopen("https://huggingface.co", timeout=3).read(1))
out["network_blocked"] = all(out[k] for k in ("direct_ip_blocked", "dns_blocked", "https_blocked"))
out["credentials_absent"] = not any(os.getenv(k) for k in (
    "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_IDENTITY_TOKEN", "HF_TOKEN", "AWS_SECRET_ACCESS_KEY"))
try:
    smi = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                         capture_output=True, text=True, timeout=20)
    out["nvidia_smi"] = smi.stdout.strip()
except Exception as exc:
    out["nvidia_smi_error"] = type(exc).__name__
t0 = time.monotonic()
import torch, torchxrayvision as xrv
out["import_s"] = round(time.monotonic() - t0, 3)
out["cuda"] = torch.cuda.is_available()
out["torch_gpu"] = torch.cuda.get_device_name(0) if out["cuda"] else None
t0 = time.monotonic()
model = xrv.models.DenseNet(weights="densenet121-res224-all").eval().cuda()
torch.cuda.synchronize()
out["load_model_s"] = round(time.monotonic() - t0, 3)
out["labels"] = [p for p in model.pathologies if p]
with torch.no_grad():
    x1 = torch.randn(1, 1, 224, 224, device="cuda")
    model(x1); torch.cuda.synchronize()          # warm-up
    t0 = time.monotonic(); y = model(x1); torch.cuda.synchronize()
    out["single_s"] = round(time.monotonic() - t0, 4)
    xb = torch.randn(64, 1, 224, 224, device="cuda")
    t0 = time.monotonic(); yb = model(xb); torch.cuda.synchronize()
    out["batch64_s"] = round(time.monotonic() - t0, 4)
    out["output_shape"] = list(yb.shape)
print("R0RESULT " + json.dumps(out), flush=True)
'''

GPUS = ("L4", "L40S")


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gpus", default=",".join(GPUS))
    p.add_argument("--app-name", default="radiant-r0-probe")
    p.add_argument("--environment", default=os.getenv("MODAL_ENVIRONMENT") or None)
    p.add_argument("--model-load-timeout", type=float, default=120.0)
    p.add_argument("--sandbox-timeout", type=int, default=240)
    p.add_argument("--skip-timeout-check", action="store_true")


def build_image(modal):
    bake = "python -c \"import torchxrayvision as xrv; xrv.models.DenseNet(weights='densenet121-res224-all')\""
    return (
        modal.Image.debian_slim(python_version="3.12")
        .pip_install("torch==2.5.1", "torchvision==0.20.1", "torchxrayvision==1.5.7", "numpy<2.3")
        .run_commands(bake)
    )


def parse_remote(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith("R0RESULT "):
            return json.loads(line[len("R0RESULT "):])
    raise ValueError("no R0RESULT line in sandbox output")


def gpu_gates(gpu: str, remote: dict[str, Any], wall_s: float, load_timeout: float) -> list[GateResult]:
    g = gpu.lower()
    seen = f"{remote.get('nvidia_smi', '')} {remote.get('torch_gpu') or ''}"
    common = {"gpu_requested": gpu, "sandbox_wall_s": round(wall_s, 1),
              "approx_cost_usd": round(wall_s * APPROX_USD_PER_S.get(gpu, 0), 4)}
    offline = bool(remote.get("network_blocked") and remote.get("credentials_absent"))
    # "L4" is a prefix of "L40S": match the whole token.
    tokens = seen.replace(",", " ").split()
    observed = gpu in tokens
    load_s = remote.get("load_model_s")
    loaded = bool(remote.get("cuda") and load_s is not None and load_s <= load_timeout and remote.get("output_shape"))
    return [
        GateResult(f"modal_{g}_offline", GateStatus.PASSED if offline else GateStatus.FAILED,
                   "egress blocked, no credentials inside" if offline else "egress or credentials leaked",
                   {**common, **{k: remote.get(k) for k in
                                 ("direct_ip_blocked", "dns_blocked", "https_blocked", "credentials_absent")}}),
        GateResult(f"modal_{g}_gpu_observed", GateStatus.PASSED if observed else GateStatus.FAILED,
                   f"sandbox saw {remote.get('torch_gpu') or remote.get('nvidia_smi') or 'no GPU'}",
                   {**common, "nvidia_smi": remote.get("nvidia_smi"), "torch_gpu": remote.get("torch_gpu")}),
        GateResult(f"modal_{g}_model_load", GateStatus.PASSED if loaded else GateStatus.FAILED,
                   f"DenseNet-121 loaded offline in {load_s}s (limit {load_timeout:.0f}s); "
                   f"single {remote.get('single_s')}s, batch of 64 {remote.get('batch64_s')}s",
                   {**common, **{k: remote.get(k) for k in
                                 ("import_s", "load_model_s", "single_s", "batch64_s", "output_shape")},
                    "label_count": len(remote.get("labels") or [])}),
    ]


def _run_sandbox(modal, app, image, *, gpu: str | None, timeout: int,
                 command: list[str]) -> tuple[str, int | None, float, bool]:
    """Return (output, returncode, wall seconds, timed_out)."""
    sb = None
    t0 = time.monotonic()
    try:
        sb = modal.Sandbox.create(
            *command, app=app, image=image, gpu=gpu, timeout=timeout,
            block_network=True, name=f"r0-{(gpu or 'cpu').lower()}-{uuid.uuid4().hex[:8]}",
            tags={"purpose": "r0-feasibility"},
        )
        try:
            sb.wait(raise_on_termination=False)
        except Exception as exc:
            if type(exc).__name__ == "SandboxTimeoutError":
                return "", None, time.monotonic() - t0, True
            raise
        stdout = sb.stdout.read()
        stderr = sb.stderr.read()
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return stdout + ("\n" + stderr[-2000:] if stderr else ""), sb.returncode, time.monotonic() - t0, False
    finally:
        if sb is not None:
            try:
                sb.terminate()
            except Exception:
                pass


def run(args: argparse.Namespace) -> list[GateResult]:
    try:
        import modal
    except ImportError:
        return [GateResult("modal", GateStatus.BLOCKED, "modal SDK not installed (uv sync --extra r0)")]
    if not (os.getenv("MODAL_TOKEN_ID") or os.path.isfile(os.path.expanduser("~/.modal.toml"))):
        return [GateResult("modal", GateStatus.BLOCKED, "no Modal credentials")]

    app = modal.App.lookup(args.app_name, create_if_missing=True, environment_name=args.environment)
    image = build_image(modal)
    if hasattr(image, "build"):  # build up front so sandbox wall time excludes the build
        image = image.build(app)
    results: list[GateResult] = []
    for gpu in [g.strip() for g in args.gpus.split(",") if g.strip()]:
        try:
            out, code, wall, _ = _run_sandbox(modal, app, image, gpu=gpu, timeout=args.sandbox_timeout,
                                           command=["python", "-c", REMOTE_PROBE])
            remote = parse_remote(out)
            results += gpu_gates(gpu, remote, wall, args.model_load_timeout)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {str(exc)[:300]}"
            results += [GateResult(f"modal_{gpu.lower()}_{k}", GateStatus.FAILED, msg)
                        for k in ("offline", "gpu_observed", "model_load")]

    if not args.skip_timeout_check:
        cpu_image = modal.Image.debian_slim(python_version="3.12")
        limit = 10
        try:
            out, code, wall, timed_out = _run_sandbox(modal, app, cpu_image, gpu=None, timeout=limit,
                                                      command=["sleep", "120"])
            killed = wall < 60 and (timed_out or code not in (0, None))
            results.append(GateResult(
                "modal_timeout_enforced", GateStatus.PASSED if killed else GateStatus.FAILED,
                f"sandbox with a {limit}s timeout running sleep 120 was stopped after {wall:.0f}s "
                f"({'SandboxTimeoutError' if timed_out else f'exit {code}'})",
                {"timeout_s": limit, "wall_s": round(wall, 1), "returncode": code, "timed_out": timed_out}))
        except Exception as exc:
            results.append(GateResult("modal_timeout_enforced", GateStatus.FAILED,
                                      f"{type(exc).__name__}: {str(exc)[:300]}"))
    return results
