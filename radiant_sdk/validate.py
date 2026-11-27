"""``validate``: run a solution the way the validator will, then write a receipt.

Steps, in the validator's gate order:

1. fetch the campaign (API or a saved API response) and check its hash
2. static checks on the zip (g1 size, g2 archive, descriptor, .env, lockfile, g3 contract)
3. Hugging Face: revision exists and is exact, weights under the cap, LFS hashes match (g5)
4. build the Modal image: the SDK's base image, the hash-locked extras
   (``pip install --no-deps --require-hashes``), the code without .env, and
   the weights (g7). No token or secret reaches the image.
5. qualification in an offline sandbox on the solution's GPU: the harness's
   ``qualify`` mode on the public warmup set (g8)

The full log of steps 4 and 5 is streamed to the terminal, as the validator
shows it to the miner. On success a receipt is written next to the zip;
``submit`` refuses to upload without a matching receipt under 24 hours old.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import queue
import secrets
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import SDK_VERSION
from . import reasons as R
from .checks import StaticReport, check_weight_files, read_zip, run_static_checks
from .harness import LOADED_MARKER, RESULT_PREFIX, STARTED_MARKER

RECEIPT_SCHEMA = 1
RECEIPT_MAX_AGE = timedelta(hours=24)
SDK_DIR = Path(__file__).resolve().parent
BASE_REQUIREMENTS = SDK_DIR / "base_image" / "requirements.txt"
# Approximate Modal list prices (USD per second) for the cost line only.
APPROX_USD_PER_S = {"T4": 0.000164, "L4": 0.000222, "A10G": 0.000306, "L40S": 0.000542,
                    "A100-40GB": 0.000583, "A100-80GB": 0.000694, "H100": 0.001097}

Log = Callable[[str, str], None]


def log(stage: str, msg: str) -> None:
    print(f"[radiant-sdk:{stage}] {msg}", file=sys.stderr, flush=True)


# ── campaign ────────────────────────────────────────────────────────────────


def manifest_hash(manifest: dict[str, Any]) -> str:
    canon = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


def _check_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme == "https" or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")):
        return url.rstrip("/")
    raise ValueError(f"RADIANT_API_URL must be https (http only for localhost): {url}")


def fetch_campaign(campaign_id: str, *, api_url: str | None = None, manifest_file: Path | None = None) -> dict:
    """The campaign as /v1/campaigns/{id} returns it, with its hash checked."""
    if manifest_file is not None:
        body = json.loads(Path(manifest_file).read_text())
    else:
        import requests

        url = f"{_check_url(api_url or os.environ.get('RADIANT_API_URL', ''))}/v1/campaigns/{campaign_id}"
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        body = r.json()
    if body.get("campaign_id") != campaign_id:
        raise ValueError(f"campaign {campaign_id!r} not in that response")
    if manifest_hash(body["manifest"]) != body["manifest_hash"]:
        raise ValueError("campaign manifest does not hash to its manifest_hash; refusing to trust it")
    return body


# ── Hugging Face (g5) ───────────────────────────────────────────────────────


def token_write_scopes(token: str) -> list[str]:
    """Write permissions a Hugging Face token carries ([] for a read-only or fine-grained read token)."""
    from huggingface_hub import HfApi

    auth = (HfApi().whoami(token=token).get("auth") or {}).get("accessToken") or {}
    if auth.get("role") == "write":
        return ["write (classic token)"]
    fine = auth.get("fineGrained") or {}
    return sorted({p for scope in fine.get("scoped", []) for p in scope.get("permissions", []) if "write" in p}
                  | {p for p in fine.get("global", []) if "write" in p})


def fetch_weights(env: dict[str, str], max_weights_mb: int, cache: Path,
                  formats: list[str] | None = None) -> tuple[Path, dict[str, Any]]:
    from huggingface_hub import HfApi, snapshot_download
    from huggingface_hub.utils import HfHubHTTPError

    repo, revision, token = env["HF_REPO_ID"], env["HF_REVISION"], env["HF_TOKEN"] or False
    if token:
        # The token travels inside the zip to the validator: it must not be able to write anything.
        try:
            writes = token_write_scopes(token)
        except HfHubHTTPError as exc:
            raise R.SolutionRejected(R.HF_UNREACHABLE, f"HF_TOKEN rejected: {str(exc).splitlines()[0]}") from None
        if writes:
            raise R.SolutionRejected(R.HF_TOKEN_WRITE_SCOPE,
                                     f"HF_TOKEN can {', '.join(writes)}; use a fine-grained read-only token "
                                     "scoped to this one repo (it is uploaded with your solution)")
    try:
        info = HfApi().model_info(repo, revision=revision, files_metadata=True, token=token)
    except HfHubHTTPError as exc:
        raise R.SolutionRejected(R.HF_UNREACHABLE, f"{repo}@{revision}: {str(exc).splitlines()[0]}") from None
    if info.sha != revision:
        raise R.SolutionRejected(R.HF_REVISION_MISMATCH, f"{revision} resolved to {info.sha}")
    check_weight_files([s.rfilename for s in info.siblings], formats or ["safetensors"])
    total = sum(s.size or 0 for s in info.siblings)
    if total > max_weights_mb * 1_000_000:
        raise R.SolutionRejected(R.HF_WEIGHTS_TOO_LARGE, f"{total / 1e6:.0f} MB, over {max_weights_mb} MB")
    target = cache / repo.replace("/", "--") / revision
    snapshot_download(repo, revision=revision, token=token, local_dir=target)
    bad = []
    for s in info.siblings:
        if s.lfs:
            h = hashlib.sha256()
            with (target / s.rfilename).open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != s.lfs.sha256:
                bad.append(s.rfilename)
    if bad:
        raise R.SolutionRejected(R.HF_HASH_MISMATCH, ", ".join(bad))
    shutil.rmtree(target / ".cache", ignore_errors=True)
    return target, {"repo": repo, "revision": revision, "bytes": total, "files": len(info.siblings)}


# ── Modal (g7, g8) ──────────────────────────────────────────────────────────


def base_image(modal):
    """The SDK version's base image (Python 3.12, torch, torchvision, numpy). Same as the validator's."""
    return modal.Image.debian_slim(python_version="3.12").pip_install_from_requirements(str(BASE_REQUIREMENTS))


# Refuse any wheel that installs a new .pth startup hook into site-packages
# (which would execute before radiant_sdk.harness starts its timer).
_PIP_INSTALL_CMD = (
    "python -c 'import glob, site; open(\"/tmp/.pth_before\", \"w\").write("
    "\"\\n\".join(sorted(p for d in site.getsitepackages() for p in glob.glob(d + \"/*.pth\"))))' && "
    "pip install --no-deps --require-hashes --only-binary=:all: --no-cache-dir -r /tmp/requirements.lock && "
    "python -c 'import glob, site, sys; before = set(open(\"/tmp/.pth_before\").read().splitlines()); "
    "after = set(p for d in site.getsitepackages() for p in glob.glob(d + \"/*.pth\")); "
    "extra = sorted(after - before); "
    "sys.exit(f\"refusing wheel-installed .pth hooks: {extra}\") if extra else None'"
)


def solution_image(modal, code_dir: Path, weights_dir: Path, lock_text: str):
    """Base, then the hash-locked wheels, then the SDK, the code and the weights.

    Heavy layers first, so editing model.py or the SDK reuses the cached
    dependency layers. ``--only-binary=:all:`` prevents source distributions
    from executing arbitrary setup.py code with network access at build time.
    """
    image = base_image(modal)
    if any(line.strip() and not line.lstrip().startswith("#") for line in lock_text.splitlines()):
        image = image.add_local_file(str(code_dir / "requirements.lock"), "/tmp/requirements.lock", copy=True) \
            .run_commands(_PIP_INSTALL_CMD)
    return (
        image.env({"PYTHONPATH": "/opt/radiant", "PYTHONDONTWRITEBYTECODE": "1"})
        .add_local_dir(str(SDK_DIR), "/opt/radiant/radiant_sdk", copy=True,
                       ignore=["**/__pycache__", "reference_miner", "**/*.pyc"])
        .add_local_dir(str(code_dir), "/solution", copy=True)
        .add_local_dir(str(weights_dir), "/weights", copy=True)
    )


MODAL_SETUP_HINT = (
    "Modal isn't set up on this machine. Run `modal setup` (opens a browser to log in), or set "
    "MODAL_TOKEN_ID and MODAL_TOKEN_SECRET from https://modal.com/settings/tokens. validate runs one short "
    "sandbox on your own Modal account, usually a few cents. `--static-only` skips Modal.")


def require_modal_credentials() -> None:
    from modal.config import config

    if not (config.get("token_id") and config.get("token_secret")):
        raise ValueError(MODAL_SETUP_HINT)


def _build_with_deadline(image, app, seconds: float):
    box: dict[str, Any] = {}

    def target():
        try:
            box["image"] = image.build(app)
        except Exception as exc:  # surfaced below
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise R.SolutionRejected(R.BUILD_TIMEOUT, f"image build exceeded modal_build_timeout_s={seconds:.0f}")
    if "error" in box:
        raise R.SolutionRejected(R.BUILD_FAILED, f"{type(box['error']).__name__}: {box['error']}")
    return box["image"]


def parse_result(output: str, prefix: str = "") -> dict[str, Any]:
    want = prefix + RESULT_PREFIX
    for line in reversed(output.splitlines()):
        if line.startswith(want):
            try:
                parsed = json.loads(line[len(want):])
            except json.JSONDecodeError as exc:
                raise R.SolutionRejected(R.SANDBOX_FAILED, f"malformed harness result: {exc}") from None
            if not isinstance(parsed, dict) or not isinstance(parsed.get("status"), str):
                raise R.SolutionRejected(R.SANDBOX_FAILED, "malformed harness result object")
            return parsed
    raise R.SolutionRejected(R.SANDBOX_FAILED, "the sandbox ended without a harness result")


def eval_timeout_for(limits: dict, megapixels: float) -> float:
    """The manifest's eval deadline: a minimum plus a per-megapixel allowance."""
    return max(float(limits["eval_timeout_min_s"]), limits["eval_timeout_per_megapixel_s"] * megapixels)


def run_qualification(modal, app, image, descriptor: dict, manifest: dict, *, eval_timeout_s: float | None = None,
                      gpu_wait_s: float = 900, sink: list[str] | None = None, echo: bool = True,
                      restart_after_s: float = 120, token: str = "", check_isolation: bool = False) -> dict:
    """Qualify in an offline sandbox. ``sink`` collects the sandbox's log (shown to the miner).

    Timing of ``load_model`` and evaluation is enforced externally from the
    host using monotonic wall time between protocol markers, so miner code
    cannot bypass ``model_load_timeout_s`` or forge its own load time.
    """
    limits, harness = manifest["limits"], manifest["harness"]
    if eval_timeout_s is None:
        from .harness import WARMUP_SHAPES

        eval_timeout_s = eval_timeout_for(limits, sum(h * w for h, w in WARMUP_SHAPES) / 1e6)
    prefix = f"{token} " if token else ""
    started_marker = prefix + STARTED_MARKER
    loaded_marker = prefix + LOADED_MARKER
    result_prefix = prefix + RESULT_PREFIX
    cmd = ["python", "-m", "radiant_sdk.harness", "qualify",
           "--labels", ",".join(manifest["labels"]), "--batch-size", str(descriptor["batch_size"]),
           "--decimals", str(harness["prediction_decimals"]),
           "--load-timeout", str(limits["model_load_timeout_s"]), "--eval-timeout", str(eval_timeout_s)]
    if token:
        cmd += ["--token", token]
    if check_isolation:
        cmd += ["--check-isolation"]
    kwargs = dict(
        app=app, image=image, gpu=descriptor["gpu"],
        cpu=(float(descriptor["cpus"]), float(descriptor["cpus"])),
        memory=(min(4096, manifest["hardware"]["max_memory_mib"]), manifest["hardware"]["max_memory_mib"]),
        block_network=True,
        timeout=int(limits["model_load_timeout_s"] + eval_timeout_s + 120),
        secrets=[], include_oidc_identity_token=False, workdir="/solution",
        tags={"purpose": "radiant-qualification"},
    )
    t0 = time.monotonic()
    sb, q, started = start_sandbox(modal, cmd, kwargs, gpu=descriptor["gpu"], wait_s=gpu_wait_s,
                                   restart_after_s=restart_after_s, started_marker=started_marker)
    lines: list[str] = []
    loaded_at: float | None = None
    load_limit = float(limits["model_load_timeout_s"])
    load_deadline = started + load_limit + 2.0  # 2s grace for in-sandbox watchdog to emit LOAD_TIMEOUT
    eval_deadline = started + load_limit + float(eval_timeout_s) + 30.0
    try:
        while True:
            now = time.monotonic()
            active_deadline = eval_deadline if loaded_at is not None else load_deadline
            left = active_deadline - now
            if left <= 0:
                if loaded_at is None:
                    raise R.SolutionRejected(R.LOAD_TIMEOUT, f"import + load_model exceeded {load_limit:.0f}s (host clock)")
                raise R.SolutionRejected(R.EVAL_TIMEOUT, f"evaluation exceeded {eval_timeout_s:.0f}s (host clock)")
            try:
                chunk = q.get(timeout=left)
            except queue.Empty:
                if loaded_at is None:
                    raise R.SolutionRejected(R.LOAD_TIMEOUT, f"import + load_model exceeded {load_limit:.0f}s (host clock)")
                raise R.SolutionRejected(R.EVAL_TIMEOUT, f"evaluation exceeded {eval_timeout_s:.0f}s (host clock)")
            if chunk is None:
                break
            for text in chunk.splitlines(keepends=True):
                stripped = text.strip()
                if stripped == started_marker:
                    continue
                if stripped == loaded_marker:
                    if loaded_at is None:
                        loaded_at = time.monotonic()
                        eval_deadline = loaded_at + float(eval_timeout_s) + 30.0
                    continue
                lines.append(text)
                if not text.startswith(result_prefix) and not text.startswith(RESULT_PREFIX):
                    if sink is not None:
                        sink.append(text.rstrip("\n"))
                    if echo:
                        print(f"  | {text.rstrip()}", file=sys.stderr, flush=True)
        sb.wait(raise_on_termination=False)
        err = sb.stderr.read()
        err = err if isinstance(err, str) else err.decode("utf-8", "replace")
        for text in err.splitlines()[-200:]:
            if sink is not None:
                sink.append("! " + text)
            if echo:
                print(f"  ! {text}", file=sys.stderr)
    finally:
        try:
            sb.terminate()
        except Exception:
            pass
    result = parse_result("".join(lines), prefix=prefix)
    wall = time.monotonic() - started
    host_load_s = round(loaded_at - started, 3) if loaded_at is not None else None
    result["sandbox_wall_s"] = round(wall, 1)
    result["gpu_wait_s"] = round(started - t0, 1)
    if host_load_s is not None:
        result["host_load_s"] = host_load_s
    result["approx_cost_usd"] = round(wall * APPROX_USD_PER_S.get(descriptor["gpu"], 0.0), 4)
    if result.get("status") != "passed":
        raise R.SolutionRejected(str(result.get("reason_code", R.SANDBOX_FAILED)), str(result.get("detail", "")))
    if token and loaded_at is None:
        raise R.SolutionRejected(R.SANDBOX_FAILED, "sandbox ended without completing model load")
    reported_load = result.get("load_total_s")
    if isinstance(reported_load, bool) or not isinstance(reported_load, (int, float)) or not math.isfinite(reported_load):
        raise R.SolutionRejected(R.SANDBOX_FAILED, "harness result missing valid load_total_s")
    if reported_load > load_limit or (host_load_s is not None and host_load_s > load_limit):
        actual = max(float(reported_load), host_load_s or 0.0)
        raise R.SolutionRejected(R.LOAD_TIMEOUT, f"load took {actual:.1f}s (limit {load_limit:.0f}s)")
    return result


def _is_capacity_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "resourceexhausted" in name or "capacity" in msg or "insufficient" in msg


def start_sandbox(modal, cmd: list[str], kwargs: dict, *, gpu: str, wait_s: float, restart_after_s: float = 120,
                  started_marker: str = STARTED_MARKER):
    """Create a sandbox and wait for the harness's start marker; (sandbox, output queue, started_at).

    Seen on Modal (27 Nov): one sandbox can sit unscheduled for 15+ minutes
    while a fresh request with the same spec starts in seconds, and
    ``Sandbox.create`` itself can block while it waits. So create runs in a
    thread, and a request that hasn't started within ``restart_after_s`` is
    cancelled and made again, until ``wait_s`` is used up. Then it is
    ``GPU_UNAVAILABLE``: infrastructure, never the miner's fault, and nothing
    was billed for the wait. The queue carries everything the sandbox prints,
    from the start marker on, then None when it exits.
    """
    t0 = time.monotonic()
    deadline = t0 + wait_s
    attempt = 0
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise R.InfraUnavailable(
                R.GPU_UNAVAILABLE,
                f"Modal didn't start a {gpu} sandbox within {wait_s:.0f}s ({attempt} requests); nothing was billed "
                "for the wait. Retry later, raise --gpu-wait, or pick another allowed GPU in radiant_solution.json")
        attempt += 1
        attempt_end = min(deadline, now + restart_after_s)
        box: dict = {}
        box_lock = threading.Lock()

        def create(box=box, box_lock=box_lock) -> None:
            sb_created = None
            err = None
            try:
                sb_created = modal.Sandbox.create(*cmd, **kwargs)
            except Exception as exc:  # surfaced below
                err = exc
            terminate_now = False
            with box_lock:
                if err is not None:
                    box["error"] = err
                elif box.get("abandoned"):
                    terminate_now = True
                else:
                    box["sb"] = sb_created
            if terminate_now and sb_created is not None:
                try:
                    sb_created.terminate()  # a request we gave up on landed after all
                except Exception:
                    pass

        th = threading.Thread(target=create, daemon=True)
        th.start()
        th.join(max(0.0, attempt_end - time.monotonic()))
        with box_lock:
            if "sb" not in box and "error" not in box:
                box["abandoned"] = True
                abandoned = True
            else:
                abandoned = False
            err = box.get("error")
            sb = box.get("sb")
        if abandoned:
            log("qualify", f"no {gpu} scheduled after {time.monotonic() - t0:.0f}s; making a fresh request")
            continue
        if err is not None:
            if _is_capacity_error(err):
                rem = deadline - time.monotonic()
                if rem > 0:
                    log("qualify", f"{gpu} capacity refused ({type(err).__name__}); retrying")
                    time.sleep(min(2.0, rem))
                continue
            raise R.InfraUnavailable(R.SANDBOX_START_FAILED, f"{type(err).__name__}: {err}")
        raw: queue.Queue = queue.Queue()

        def pump(sb=sb, raw=raw) -> None:
            try:
                for chunk in sb.stdout:
                    raw.put(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
            except Exception:
                pass
            finally:
                raw.put(None)

        threading.Thread(target=pump, daemon=True).start()
        seen: list = []
        while True:
            left = attempt_end - time.monotonic()
            if left <= 0:
                break
            try:
                chunk = raw.get(timeout=left)
            except queue.Empty:
                break
            if chunk is None:
                # Container exited before the harness ever printed STARTED_MARKER:
                # image pull failure, OOM at boot, or failed isolation probe -> infra, never miner fault.
                try:
                    sb.terminate()
                except Exception:
                    pass
                raise R.InfraUnavailable(R.SANDBOX_START_FAILED,
                                         "sandbox exited before the harness started (no start marker)")
            seen.append(chunk)
            if not any(line.strip() == started_marker for line in chunk.splitlines()):
                continue
            out: queue.Queue = queue.Queue()
            for c in seen:
                out.put(c)

            def relay(src=raw, dst=out) -> None:
                while True:
                    item = src.get()
                    dst.put(item)
                    if item is None:
                        return

            threading.Thread(target=relay, daemon=True).start()
            started_at = time.monotonic()
            log("qualify", f"container started after {started_at - t0:.0f}s (request {attempt})")
            return sb, out, started_at
        try:
            sb.terminate()
        except Exception:
            pass
        log("qualify", f"{gpu} sandbox not started after {time.monotonic() - t0:.0f}s; making a fresh request")


# ── receipt ─────────────────────────────────────────────────────────────────


def receipt_path(zip_path: Path) -> Path:
    return zip_path.with_name("receipt.json")


def write_receipt(zip_path: Path, report: StaticReport, campaign: dict, weights: dict, qual: dict) -> Path:
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "passed",
        "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sdk_version": SDK_VERSION,
        "campaign_id": campaign["campaign_id"],
        "manifest_hash": campaign["manifest_hash"],
        "sha256": report.sha256,
        "code_sha256": report.code_sha256,
        "descriptor": report.descriptor,
        "weights": weights,
        "qualification": qual,
    }
    path = receipt_path(zip_path)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return path


def check_receipt(zip_path: Path, campaign_id: str, manifest_hash_now: str | None,
                  now: datetime | None = None) -> dict:
    """The receipt if it matches this exact zip, campaign and SDK and is under 24 h old; else raise."""
    path = receipt_path(zip_path)
    if not path.is_file():
        raise ValueError("no validation receipt next to the zip; run `radiant-sdk validate` first")
    r = json.loads(path.read_text())
    sha = "sha256:" + hashlib.sha256(zip_path.read_bytes()).hexdigest()
    now = now or datetime.now(timezone.utc)
    checks = [
        (r.get("schema_version") == RECEIPT_SCHEMA and r.get("status") == "passed", "receipt is not a pass"),
        (r.get("sha256") == sha, "the zip changed since it was validated"),
        (r.get("campaign_id") == campaign_id, f"receipt is for campaign {r.get('campaign_id')!r}"),
        (manifest_hash_now is None or r.get("manifest_hash") == manifest_hash_now,
         "the campaign manifest changed since validation"),
        (r.get("sdk_version") == SDK_VERSION, f"receipt was made by SDK {r.get('sdk_version')}"),
    ]
    for ok, why in checks:
        if not ok:
            raise ValueError(why + "; run `radiant-sdk validate` again")
    age = now - datetime.fromisoformat(r["validated_at"])
    if age < timedelta(0) or age > RECEIPT_MAX_AGE:
        raise ValueError("validation receipt is older than 24 hours; run `radiant-sdk validate` again")
    return r


# ── the whole thing ─────────────────────────────────────────────────────────


@dataclass
class ValidateOptions:
    campaign_id: str
    api_url: str | None = None
    manifest_file: Path | None = None
    modal_environment: str | None = None
    cache_dir: Path = Path.home() / ".cache" / "radiant-sdk"
    static_only: bool = False
    gpu_wait_s: float = 900.0


def validate(zip_path: Path, opts: ValidateOptions) -> dict[str, Any]:
    campaign = fetch_campaign(opts.campaign_id, api_url=opts.api_url, manifest_file=opts.manifest_file)
    manifest = campaign["manifest"]
    log("campaign", f"{campaign['campaign_id']} ({campaign.get('status')}) {campaign['manifest_hash']}")
    data = zip_path.read_bytes()
    report = run_static_checks(data, manifest)
    descriptor = dict(report.descriptor)
    log("static", f"passed: {len(report.files)} files, {report.sha256}, code {report.code_sha256}")
    if opts.static_only:
        return {"status": "static_passed", "sha256": report.sha256, "code_sha256": report.code_sha256}

    weights_dir, weights = fetch_weights(report.env, manifest["limits"]["max_weights_mb"], opts.cache_dir / "hf",
                                         manifest["gates"].get("weights_formats", ["safetensors"]))
    log("weights", f"{weights['repo']}@{weights['revision'][:12]}: {weights['files']} files, "
                   f"{weights['bytes'] / 1e6:.1f} MB, hashes match")

    import modal

    require_modal_credentials()
    files = read_zip(data, max_zip_mb=manifest["limits"]["max_solution_zip_mb"],
                     max_unzipped_mb=manifest["limits"]["max_solution_unzipped_mb"])
    with tempfile.TemporaryDirectory(prefix="radiant-sdk-") as tmp:
        code_dir = Path(tmp) / "solution"
        for name, content in files.items():
            if name == ".env":
                continue  # the token never reaches the image
            p = code_dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        try:
            app = modal.App.lookup("radiant-sdk-validate", create_if_missing=True,
                                   environment_name=opts.modal_environment)
        except modal.exception.AuthError:
            raise ValueError("Modal rejected your token. " + MODAL_SETUP_HINT) from None
        image = solution_image(modal, code_dir, weights_dir, files["requirements.lock"].decode())
        log("build", f"building the image (limit {manifest['limits']['modal_build_timeout_s']}s); "
                     "cached layers are reused")
        t0 = time.monotonic()
        # Modal's build log goes to stdout; send it to stderr with the rest of
        # the log so stdout stays one JSON document.
        with contextlib.redirect_stdout(sys.stderr), modal.enable_output():
            image = _build_with_deadline(image, app, manifest["limits"]["modal_build_timeout_s"])
        log("build", f"done in {time.monotonic() - t0:.0f}s")
        log("qualify", f"offline sandbox on {descriptor['gpu']}, {descriptor['cpus']} CPUs; harness log follows")
        qual = run_qualification(modal, app, image, descriptor, manifest, gpu_wait_s=opts.gpu_wait_s,
                                 token=secrets.token_hex(16), check_isolation=True)
    log("qualify", f"passed: load {qual['load_total_s']}s (limit {manifest['limits']['model_load_timeout_s']}s), "
                   f"single {qual['single_s']}s, {qual['images']} images in {qual['batch_s']}s; "
                   f"about ${qual['approx_cost_usd']}")
    path = write_receipt(zip_path, report, campaign, weights, qual)
    log("receipt", f"wrote {path} (valid 24 h for this exact zip and campaign)")
    return {"status": "passed", "receipt": str(path), "sha256": report.sha256, "qualification": qual}
