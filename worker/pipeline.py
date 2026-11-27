"""The gate pipeline: g0 to g9, fail-fast, with a persisted state and event per step.

    pending -> gating -> [g0 .. g9] -> queued          (ready for rounds, R6)
                      -> rejected (reason_code)        (the miner's fault; final)
                      -> rejected_duplicate (g6)
                      -> infra_failed                  (not the miner's fault)

An infrastructure failure (no GPU within SANDBOX_GPU_WAIT_S, a Modal or
storage error, a validator bug) is never scored or rejected. The submission
goes back to pending once; a second infrastructure failure is terminal until
an operator requeues it (as Pareton's infra_failed).

Every external effect goes through ``Deps``, so the pipeline runs the same
in tests (fakes) and in the worker (Modal, Hugging Face, the bucket).

After g5 the stored zip is replaced by a copy without .env (checked against
code_sha256), so the miner's HF token leaves the bucket as soon as it has
been used. A submission rejected after its zip was fetched gets the same
treatment; a zip that can't be read at all is deleted.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from psycopg.types.json import Jsonb

import config
from campaign import store
from radiant_sdk import reasons as R
from radiant_sdk.checks import code_sha256, read_zip, run_static_checks
from radiant_sdk.redact import redact_text

logger = logging.getLogger("radiant.pipeline")

LEASE = "2 hours"
TERMINAL = ("queued", "rejected", "rejected_duplicate", "infra_failed")


class Infra(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


class Duplicate(Exception):
    pass


@dataclass
class Deps:
    """External effects. The worker wires the real ones (see ``real_deps``)."""

    head_size: Callable[[str], int | None]
    fetch: Callable[[str, int], bytes]
    put: Callable[[str, bytes], None]
    delete: Callable[[str], None]
    fetch_weights: Callable[[dict, int, Path, list], tuple[Path, dict]]
    build: Callable[[Path, Path, float], Any]             # -> object with image_id, log
    qualify: Callable[[str, dict, dict, list], dict]      # (image_id, descriptor, manifest, sink) -> result
    pin: Callable[[Path, str, str, int], Any]             # (weights_dir, campaign, hotkey, id) -> repo, revision, files


@dataclass
class Run:
    sub: dict
    campaign: dict
    scratch: Path
    data: bytes | None = None
    files: dict[str, bytes] | None = None
    events: list[str] = field(default_factory=list)


# ── helpers ─────────────────────────────────────────────────────────────────

def event(conn, run: Run, state: str, **detail: Any) -> None:
    conn.execute("INSERT INTO submission_events (submission_id, state, detail) VALUES (%s, %s, %s)",
                 (run.sub["id"], state, Jsonb(detail)))
    run.events.append(state)


def update(conn, sub_id: int, **cols: Any) -> None:
    sets = ", ".join(f"{k} = %s" for k in cols) + ", updated_at = now()"
    vals = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in cols.values()]
    conn.execute(f"UPDATE submissions SET {sets} WHERE id = %s", (*vals, sub_id))


def save_log(conn, sub_id: int, kind: str, body: str) -> None:
    conn.execute("""INSERT INTO submission_logs (submission_id, kind, body) VALUES (%s, %s, %s)
                    ON CONFLICT (submission_id, kind) DO UPDATE SET body = EXCLUDED.body, created_at = now()""",
                 (sub_id, kind, redact_text(body)[-200_000:]))


def stripped_zip(files: dict[str, bytes]) -> bytes:
    from radiant_sdk.packaging import deterministic_zip

    return deterministic_zip({k: v for k, v in files.items() if k != ".env"})


def weights_digest(weights_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in weights_dir.rglob("*") if x.is_file() and ".cache" not in x.parts):
        fh = hashlib.sha256(p.read_bytes()).hexdigest()
        h.update(f"{p.relative_to(weights_dir).as_posix()}\0{fh}\n".encode())
    return "sha256:" + h.hexdigest()


def _cached_weights_dir(scratch: Path, expected_sha: str | None) -> Path | None:
    """Return the existing g5 weights dir on scratch if its digest still matches."""
    if not expected_sha:
        return None
    hf_root = scratch / "hf"
    if not hf_root.is_dir():
        return None
    for repo_dir in sorted(hf_root.iterdir()):
        if not repo_dir.is_dir():
            continue
        for rev_dir in sorted(repo_dir.iterdir()):
            if rev_dir.is_dir() and weights_digest(rev_dir) == expected_sha:
                return rev_dir
        if weights_digest(repo_dir) == expected_sha:
            return repo_dir
    return None


# ── claiming ────────────────────────────────────────────────────────────────

def claim(conn) -> dict | None:
    """Take the oldest pending submission (or one whose lease expired)."""
    with conn.transaction():
        row = conn.execute(f"""
            UPDATE submissions SET state = 'gating', lease_until = now() + interval '{LEASE}', updated_at = now()
            WHERE id = (SELECT id FROM submissions
                        WHERE (state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
                           OR (state = 'gating' AND lease_until < now())
                        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *""").fetchone()
    return row


# ── the gates ───────────────────────────────────────────────────────────────

def g0_identity(conn, run: Run) -> None:
    sub, campaign = run.sub, run.campaign
    synced, registered = store.registration(conn, sub["hotkey"])
    if not synced:
        raise Infra("METAGRAPH_UNSYNCED", "the watcher has not synced the metagraph yet")
    if not registered:
        raise R.SolutionRejected(R.NOT_REGISTERED, "hotkey is no longer registered on the subnet")
    if campaign["status"] != "open":
        raise R.SolutionRejected(R.CAMPAIGN_NOT_OPEN, f"campaign is {campaign['status']}")
    dq = conn.execute("SELECT reason FROM campaign_disqualifications WHERE campaign_id = %s AND hotkey = %s",
                      (sub["campaign_id"], sub["hotkey"])).fetchone()
    if dq:
        raise R.SolutionRejected(R.DISQUALIFIED, dq["reason"])
    event(conn, run, "identity_ok")


def g1_integrity(conn, run: Run, deps: Deps) -> None:
    limits = run.campaign["manifest"]["limits"]
    cap = limits["max_solution_zip_mb"] * 1_000_000
    kept = run.scratch / "solution.zip"
    stripped_key = run.sub.get("stripped_object_key")
    if stripped_key:
        # Retry/requeue after g5 already stripped .env: reload the verified code zip from S3.
        size = deps.head_size(stripped_key)
        if size is None:
            raise Infra(R.STORAGE_UNAVAILABLE, "stripped code zip missing from bucket during retry")
        if size > cap:
            raise R.SolutionRejected(R.ZIP_TOO_LARGE, f"{size} bytes, over {cap}")
        data = deps.fetch(stripped_key, cap)
        run.data = data
        run.files = read_zip(data, max_zip_mb=limits["max_solution_zip_mb"],
                             max_unzipped_mb=limits["max_solution_unzipped_mb"])
        if code_sha256(run.files) != run.sub["code_sha256"]:
            raise R.SolutionRejected(R.CODE_SHA256_MISMATCH, "stripped code_sha256 does not match the commitment")
        event(conn, run, "fetched", bytes=len(data), resumed=True)
        return
    if kept.is_file():
        data = kept.read_bytes()
    else:
        size = deps.head_size(run.sub["object_key"])
        if size is None:
            raise R.SolutionRejected(R.ZIP_MISSING, "the committed object is not in the bucket")
        if size > cap:
            raise R.SolutionRejected(R.ZIP_TOO_LARGE, f"{size} bytes, over {cap}")
        data = deps.fetch(run.sub["object_key"], cap)
    if "sha256:" + hashlib.sha256(data).hexdigest() != run.sub["sha256"]:
        raise R.SolutionRejected(R.SHA256_MISMATCH, "the stored zip does not hash to the commitment")
    run.data = data
    kept.write_bytes(data)  # deleted as soon as g5 strips .env or the submission settles
    run.files = read_zip(data, max_zip_mb=limits["max_solution_zip_mb"],
                         max_unzipped_mb=limits["max_solution_unzipped_mb"])
    if code_sha256(run.files) != run.sub["code_sha256"]:
        raise R.SolutionRejected(R.CODE_SHA256_MISMATCH, "code_sha256 does not match the commitment")
    event(conn, run, "fetched", bytes=len(data))


def g2_to_g4(conn, run: Run):
    """Archive safety, descriptor, .env and lockfile, contract, obfuscation: the SDK's own checks."""
    if run.sub.get("stripped_object_key"):
        # Resuming after g5: .env is already stripped; validate descriptor only.
        from radiant_sdk.checks import StaticReport, check_descriptor

        descriptor = check_descriptor(run.files["radiant_solution.json"].decode(), run.campaign["manifest"])
        return StaticReport(
            sha256=run.sub["sha256"],
            code_sha256=run.sub["code_sha256"],
            files={p: len(c) for p, c in sorted(run.files.items())},
            descriptor=descriptor,
            env={},
        )
    report = run_static_checks(run.data, run.campaign["manifest"])
    update(conn, run.sub["id"], gpu=report.descriptor["gpu"])
    event(conn, run, "static_ok", files=len(report.files))
    return report


def g5_weights(conn, run: Run, deps: Deps, env: dict) -> Path:
    cached = _cached_weights_dir(run.scratch, run.sub.get("weights_sha256"))
    if cached is not None:
        return cached
    if run.sub.get("stripped_object_key"):
        raise Infra(R.STORAGE_UNAVAILABLE, "weights missing from worker scratch after .env was stripped; "
                    "cannot re-download without miner HF_TOKEN")
    manifest = run.campaign["manifest"]
    try:
        weights_dir, info = deps.fetch_weights(env, manifest["limits"]["max_weights_mb"], run.scratch / "hf",
                                               manifest["gates"]["weights_formats"])
    except R.InfraUnavailable as exc:
        raise Infra(exc.code, exc.detail) from None
    digest = weights_digest(weights_dir)
    with conn.transaction():
        update(conn, run.sub["id"], weights_repo=info["repo"], weights_revision=info["revision"], weights_sha256=digest)
        event(conn, run, "weights_ok", repo=info["repo"], revision=info["revision"], bytes=info["bytes"])
    run.sub["weights_sha256"] = digest
    return weights_dir


def strip_stored_zip(conn, run: Run, deps: Deps) -> None:
    """Replace the stored zip with one without .env, verified against code_sha256, and scrub local .env zip."""
    if run.files is None or run.sub.get("stripped_object_key"):
        return
    data = stripped_zip(run.files)
    files = read_zip(data, max_zip_mb=1024, max_unzipped_mb=4096)
    if code_sha256(files) != run.sub["code_sha256"]:
        raise Infra(R.WORKER_ERROR, "stripped zip does not match code_sha256")
    key = run.sub["object_key"][:-len(".zip")] + ".code.zip"
    deps.put(key, data)
    with conn.transaction():
        update(conn, run.sub["id"], stripped_object_key=key)
        event(conn, run, "stripped", key=key)
    run.sub["stripped_object_key"] = key
    deps.delete(run.sub["object_key"])
    kept = run.scratch / "solution.zip"
    if kept.exists():
        kept.unlink()


def g6_duplicate(conn, run: Run) -> None:
    sub = conn.execute("SELECT commit_block, weights_sha256 FROM submissions WHERE id = %s",
                       (run.sub["id"],)).fetchone()
    with conn.transaction():
        # Serialise concurrent workers checking the same (campaign, code, weights) tuple.
        lock_key = f"radiant_dedupe:{run.sub['campaign_id']}:{run.sub['code_sha256']}:{sub['weights_sha256']}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
        earlier = conn.execute(
            """SELECT id FROM submissions
               WHERE campaign_id = %s
                 AND (commit_block, id) < (%s, %s)
                 AND code_sha256 = %s
                 AND weights_sha256 = %s
                 AND state NOT IN ('rejected', 'rejected_duplicate')
               ORDER BY commit_block, id LIMIT 1""",
            (run.sub["campaign_id"], sub["commit_block"], run.sub["id"],
             run.sub["code_sha256"], sub["weights_sha256"])).fetchone()
        if earlier:
            raise Duplicate(f"same code and weights as submission {earlier['id']}")
        event(conn, run, "unique")


def g7_build(conn, run: Run, deps: Deps, weights_dir: Path) -> str:
    if run.sub.get("image_id"):
        return run.sub["image_id"]
    code_dir = run.scratch / "code"
    shutil.rmtree(code_dir, ignore_errors=True)
    for name, content in run.files.items():
        if name == ".env":
            continue  # no token reaches the build
        p = code_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    timeout = run.campaign["manifest"]["limits"]["modal_build_timeout_s"]
    try:
        built = deps.build(code_dir, weights_dir, timeout)
    except R.InfraUnavailable as exc:
        raise Infra(exc.code, exc.detail) from None
    except R.SolutionRejected as exc:
        save_log(conn, run.sub["id"], "build", exc.detail)
        raise
    with conn.transaction():
        save_log(conn, run.sub["id"], "build", built.log)
        update(conn, run.sub["id"], image_id=built.image_id)
        event(conn, run, "built", image_id=built.image_id)
    run.sub["image_id"] = built.image_id
    return built.image_id


def g8_qualify(conn, run: Run, deps: Deps, image_id: str, descriptor: dict) -> None:
    sink: list[str] = []
    try:
        result = deps.qualify(image_id, descriptor, run.campaign["manifest"], sink)
    except R.InfraUnavailable as exc:
        raise Infra(exc.code, exc.detail) from None
    finally:
        save_log(conn, run.sub["id"], "qualification", "\n".join(sink))
    update(conn, run.sub["id"], qualification=result)
    event(conn, run, "qualified", load_s=result.get("load_total_s"), gpu=descriptor["gpu"])


def g9_pin(conn, run: Run, deps: Deps, weights_dir: Path) -> None:
    pinned = deps.pin(weights_dir, run.sub["campaign_id"], run.sub["hotkey"], run.sub["id"])
    update(conn, run.sub["id"], pin_repo=pinned.repo, pin_revision=pinned.revision, pin_files=pinned.files)
    event(conn, run, "pinned", repo=pinned.repo, revision=pinned.revision)


# ── one submission ──────────────────────────────────────────────────────────

def process(conn, sub: dict, deps: Deps, s: config.Settings) -> str:
    """Run every gate on one claimed submission; return its final state."""
    campaign = store.get_campaign(conn, sub["campaign_id"])
    # Extend the lease to cover this campaign's build + GPU wait + load + eval budget.
    limits = campaign["manifest"]["limits"]
    lease_s = max(7200, int(limits["modal_build_timeout_s"] + s.sandbox_gpu_wait_s
                            + limits["model_load_timeout_s"] + limits["eval_timeout_min_s"] + 1800))
    conn.execute("UPDATE submissions SET lease_until = now() + make_interval(secs => %s) WHERE id = %s",
                 (lease_s, sub["id"]))
    # One scratch directory per submission, kept across an infra retry / operator
    # requeue so downloaded weights stay available after .env is stripped.
    scratch = Path(_scratch_root(s)) / f"sub-{sub['id']}"
    scratch.mkdir(parents=True, exist_ok=True)
    run = Run(sub=dict(sub), campaign=campaign, scratch=scratch)
    final = "pending"
    try:
        g0_identity(conn, run)
        g1_integrity(conn, run, deps)
        report = g2_to_g4(conn, run)
        weights_dir = g5_weights(conn, run, deps, report.env)
        strip_stored_zip(conn, run, deps)
        g6_duplicate(conn, run)
        image_id = g7_build(conn, run, deps, weights_dir)
        g8_qualify(conn, run, deps, image_id, report.descriptor)
        g9_pin(conn, run, deps, weights_dir)
        with conn.transaction():
            update(conn, sub["id"], state="queued", lease_until=None, next_attempt_at=None,
                   reason_code=None, reason_detail=None)
            event(conn, run, "queued")
        final = "queued"
    except R.SolutionRejected as exc:
        _settle_rejection(conn, run, deps, "rejected", exc.code, exc.detail)
        final = "rejected"
    except Duplicate as exc:
        _settle_rejection(conn, run, deps, "rejected_duplicate", R.DUPLICATE, str(exc))
        final = "rejected_duplicate"
    except Infra as exc:
        final = _settle_infra(conn, run, exc.code, exc.detail, s)
    except Exception as exc:  # a validator-side failure is never the miner's
        logger.exception("submission %s: unexpected error", sub["id"])
        final = _settle_infra(conn, run, R.WORKER_ERROR, f"{type(exc).__name__}: {str(exc)[:300]}", s)
    finally:
        # Clean up scratch on terminal non-requeueable states; keep weights on
        # pending/infra_failed so automatic retries and operator requeues can resume.
        if final in ("queued", "rejected", "rejected_duplicate"):
            shutil.rmtree(scratch, ignore_errors=True)
    return final


def _settle_rejection(conn, run: Run, deps: Deps, state: str, code: str, detail: str) -> None:
    clean = redact_text(detail)
    with conn.transaction():
        update(conn, run.sub["id"], state=state, reason_code=code, reason_detail=clean[:2000],
               lease_until=None, next_attempt_at=None)
        event(conn, run, state, reason_code=code, detail=clean[:500])
    try:  # the miner's HF token must not stay in the bucket
        # A zip whose code does not hash to its commitment cannot be stripped and
        # verified (intake no longer inflates zips, so this lands here): delete it.
        if run.files is not None and code != R.CODE_SHA256_MISMATCH:
            strip_stored_zip(conn, run, deps)
        elif deps.head_size(run.sub["object_key"]) is not None:
            deps.delete(run.sub["object_key"])
    except Exception:
        logger.exception("submission %s: could not strip the stored zip", run.sub["id"])


def _settle_infra(conn, run: Run, code: str, detail: str, s: config.Settings | None = None) -> str:
    clean = redact_text(detail)
    if code == "METAGRAPH_UNSYNCED":  # not a failure of this submission at all: just put it back
        update(conn, run.sub["id"], state="pending", lease_until=None)
        return "pending"
    retry_after = s.infra_retry_after_s if s is not None else s_retry_after()
    failures = run.sub["infra_failures"] + 1
    with conn.transaction():
        if failures <= 1:
            update(conn, run.sub["id"], state="pending", infra_failures=failures, lease_until=None,
                   reason_code=code, reason_detail=clean[:2000])
            conn.execute("UPDATE submissions SET next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s",
                         (retry_after, run.sub["id"]))
            event(conn, run, "infra_failed", reason_code=code, detail=clean[:500], requeued=True)
            return "pending"
        update(conn, run.sub["id"], state="infra_failed", infra_failures=failures, lease_until=None,
               next_attempt_at=None, reason_code=code, reason_detail=clean[:2000])
        event(conn, run, "infra_failed", reason_code=code, detail=clean[:500], requeued=False)
    return "infra_failed"


def s_retry_after() -> int:
    return config.settings().infra_retry_after_s


def _scratch_root(s: config.Settings) -> str:
    Path(s.worker_scratch).mkdir(parents=True, exist_ok=True)
    return s.worker_scratch


# ── the real dependencies ───────────────────────────────────────────────────

def real_deps(s: config.Settings) -> Deps:
    from build.hf_pin import pin
    from build.modal_image import build_image
    from radiant_sdk.validate import fetch_weights
    from storage import s3

    def qualify(image_id: str, descriptor: dict, manifest: dict, sink: list) -> dict:
        import modal

        from radiant_sdk.validate import run_qualification

        import secrets

        app = modal.App.lookup("radiant-validator", create_if_missing=True,
                               environment_name=s.modal_environment or None)
        return run_qualification(modal, app, modal.Image.from_id(image_id), descriptor, manifest,
                                 gpu_wait_s=s.sandbox_gpu_wait_s, sink=sink, echo=False,
                                 token=secrets.token_hex(16), check_isolation=True)

    return Deps(
        head_size=lambda key: s3.head_size(s, key),
        fetch=lambda key, n: s3.fetch_bounded(s, key, n),
        put=lambda key, data: s3.put_object(s, key, data),
        delete=lambda key: s3.delete_object(s, key),
        fetch_weights=fetch_weights,
        build=lambda code, weights, timeout: build_image(code, weights, timeout,
                                                         environment=s.modal_environment or None),
        qualify=qualify,
        pin=lambda weights, campaign, hotkey, sid: pin(weights, org=s.hf_pin_org, token=s.hf_pin_token,
                                                       campaign_id=campaign, hotkey=hotkey, submission_id=sid),
    )


def drain(conn, deps: Deps, s: config.Settings, limit: int | None = None) -> list[tuple[int, str]]:
    """Process pending submissions until none are left (or ``limit``); [(id, final state)]."""
    done = []
    while limit is None or len(done) < limit:
        sub = claim(conn)
        if sub is None:
            break
        done.append((sub["id"], process(conn, sub, deps, s)))
    return done

