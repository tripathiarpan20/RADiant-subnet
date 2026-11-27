"""Gate g7: build a solution's Modal image in a killable child process.

The parent (``build_image``) runs ``python -m build.modal_image SPEC.json``
and kills its whole process group when ``modal_build_timeout_s`` passes, so a
hostile build can't run on past its deadline (vidaio's lesson: an in-process
build can't be stopped). The child builds the image the SDK builds for
``validate`` (same layers, same base), with the code minus .env and the
weights from g5, and prints ``RADIANT_IMAGE_ID <id>``. Its whole output is
the build log shown to the miner.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from radiant_sdk import reasons as R
from radiant_sdk.redact import redact_text

IMAGE_PREFIX = "RADIANT_IMAGE_ID "
MAX_LOG_BYTES = 200_000
INFRA_EXIT_CODE = 75  # EX_TEMPFAIL: Modal auth/network/service outage, never the miner's fault
_MODAL_INFRA_EXC = frozenset({
    "AuthError", "ConnectionError", "TimeoutError", "InternalError", "ServiceError",
    "ResourceExhaustedError", "RemoteError", "ServerError", "UnavailableError",
})
_CHILD_ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONPATH",
    "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_PROFILE", "MODAL_ENVIRONMENT", "MODAL_CONFIG_PATH",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
})


def _child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for the build child: Modal + system vars only, no DB/S3/HF secrets."""
    src = os.environ if environ is None else environ
    return {k: v for k, v in src.items() if k in _CHILD_ENV_ALLOW}


@dataclass(frozen=True)
class Built:
    image_id: str
    log: str
    seconds: float


def _tail(text: str) -> str:
    return text if len(text) <= MAX_LOG_BYTES else "[earlier output trimmed]\n" + text[-MAX_LOG_BYTES:]


def build_image(code_dir: Path, weights_dir: Path, timeout_s: float, *, app_name: str = "radiant-validator",
                environment: str | None = None) -> Built:
    spec = {"code_dir": str(code_dir), "weights_dir": str(weights_dir), "app_name": app_name,
            "environment": environment or None}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec, f)
    t0 = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "build.modal_image", f.name], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True,
                            env=_child_env(), cwd=str(Path(__file__).resolve().parents[1]))
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        raise R.SolutionRejected(R.BUILD_TIMEOUT, f"image build exceeded {timeout_s:.0f}s") from None
    finally:
        os.unlink(f.name)
    log = redact_text(_tail(out or ""))
    ids = [line[len(IMAGE_PREFIX):].strip() for line in (out or "").splitlines() if line.startswith(IMAGE_PREFIX)]
    if proc.returncode == INFRA_EXIT_CODE:
        last = redact_text("\n".join((out or "").strip().splitlines()[-15:]))
        raise R.InfraUnavailable(R.BUILD_INFRA_ERROR, f"Modal build infrastructure error: {last[-1500:]}")
    if proc.returncode != 0 or not ids:
        last = redact_text("\n".join((out or "").strip().splitlines()[-15:]))
        raise R.SolutionRejected(R.BUILD_FAILED, f"exit {proc.returncode}: {last[-1500:]}")
    return Built(ids[-1], log, time.monotonic() - t0)


def _child(spec_path: str) -> int:
    import modal

    from radiant_sdk.validate import solution_image

    spec = json.loads(Path(spec_path).read_text())
    code_dir, weights_dir = Path(spec["code_dir"]), Path(spec["weights_dir"])
    lock = (code_dir / "requirements.lock").read_text()
    try:
        app = modal.App.lookup(spec["app_name"], create_if_missing=True, environment_name=spec["environment"])
        image = solution_image(modal, code_dir, weights_dir, lock)
        with modal.enable_output():
            built = image.build(app)
    except Exception as exc:
        if type(exc).__name__ in _MODAL_INFRA_EXC or isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            print(f"modal infra error: {type(exc).__name__}: {exc}", flush=True)
            return INFRA_EXIT_CODE
        raise
    print(IMAGE_PREFIX + built.object_id, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_child(sys.argv[1]))
