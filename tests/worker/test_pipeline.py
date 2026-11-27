"""The gate pipeline on a real Postgres, with fake storage, Hugging Face, Modal and pinning.

Covers R4's exit-gate fixtures at the pipeline level (zip-slip, symlink, a
.pyc, a missing override, an obfuscated loader, a wrong HF revision, a slow
load_model), the no-pin-before-g9 rule, .env stripping, duplicates and the
infra_failed requeue-once rule. scripts/r4_exit_gate.sh repeats the Modal and
Hugging Face parts for real.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from campaign import seed, store
from chain.commitment import SolutionCommitment
from db import migrate as mig
from db.connection import connect
from radiant_sdk import reasons as R
from radiant_sdk.checks import code_sha256, read_zip
from radiant_sdk.packaging import collect, deterministic_zip
from worker.pipeline import Deps, drain
from worker.reaper import reap

from ..db.test_db import FIXTURE

REPO = Path(__file__).resolve().parents[2]
HOTKEY = "5GZ9S5qgBrHCMr3kdvQ8hXdpZqz9D4eW7xvYxpRQHoVUuGKi"
OTHER = "5DhV69RTR4yN6onp8cwNu3Mtgr41jXQDy4wjKxEWkhwvTUS6"


@dataclass
class Built:
    image_id: str
    log: str


@dataclass
class Pinned:
    repo: str
    revision: str
    files: dict


class Fakes:
    def __init__(self, tmp: Path):
        self.bucket: dict[str, bytes] = {}
        self.tmp = tmp
        self.pins: list[str] = []
        self.qualify_error: Exception | None = None
        self.weights_error: Exception | None = None
        self.weights_bytes = b"weights-v1"

    def deps(self) -> Deps:
        def fetch_weights(env, max_mb, cache, formats):
            if self.weights_error:
                raise self.weights_error
            d = cache / "w"
            d.mkdir(parents=True, exist_ok=True)
            (d / "model.safetensors").write_bytes(self.weights_bytes)
            (d / "config.json").write_text("{}")
            return d, {"repo": env["HF_REPO_ID"], "revision": env["HF_REVISION"], "bytes": 12, "files": 2}

        def qualify(image_id, descriptor, manifest, sink):
            sink.append("qualification log line")
            if self.qualify_error:
                raise self.qualify_error
            return {"status": "passed", "load_total_s": 1.2}

        def pin(weights_dir, campaign, hotkey, sid):
            name = f"pins/rad-{campaign}-{hotkey}-s{sid}"
            self.pins.append(name)
            return Pinned(name, "a" * 40, {"model.safetensors": "x"})

        return Deps(
            head_size=lambda k: len(self.bucket[k]) if k in self.bucket else None,
            fetch=lambda k, n: self.bucket[k],
            put=lambda k, d: self.bucket.__setitem__(k, d),
            delete=lambda k: self.bucket.pop(k, None),
            fetch_weights=fetch_weights,
            build=lambda code, weights, timeout: Built("im-" + uuid.uuid4().hex[:8], "build log line"),
            qualify=qualify,
            pin=pin,
        )


@pytest.fixture
def env(db_url, tmp_path, monkeypatch):
    monkeypatch.setenv("RADIANT_WORKER_SCRATCH", str(tmp_path / "scratch"))
    assert mig.main([]) == 0
    assert seed.main(["--manifest", str(FIXTURE), "--status", "open"]) == 0
    with connect(db_url) as conn:
        store.replace_registered_hotkeys(conn, [(HOTKEY, 44, "5Cold"), (OTHER, 16, "")], 100)
    import config

    return config.settings(), Fakes(tmp_path)


def reference_files(tmp_path: Path) -> dict[str, bytes]:
    d = tmp_path / "miner"
    shutil.copytree(REPO / "radiant_sdk/reference_miner", d, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(d / ".env.template", d / ".env")
    files, _ = collect(d)
    return files


def submit(s, fakes: Fakes, files: dict[str, bytes], *, hotkey: str = HOTKEY, block: int = 105,
           raw_zip: bytes | None = None) -> int:
    data = raw_zip if raw_zip is not None else deterministic_zip(files)
    sha = "sha256:" + hashlib.sha256(data).hexdigest()
    try:
        code = code_sha256(read_zip(data, max_zip_mb=10, max_unzipped_mb=50))
    except R.SolutionRejected:
        code = "sha256:" + "0" * 64
    key = f"radiant/private/campaigns/testnet-cxr-01/solutions/{hotkey}/{uuid.uuid4()}.zip"
    fakes.bucket[key] = data
    com = SolutionCommitment(uid=44, hotkey=hotkey, coldkey="5Cold", commit_block=block, campaign_id="testnet-cxr-01",
                             sha256=sha, code_sha256=code, object_key=key, raw=f"test-{uuid.uuid4()}",
                             payment_block=block - 1, payment_tx=uuid.uuid4().int % 1000)
    with connect(s.database_url) as conn:
        return store.insert_submission(conn, com, size_bytes=len(data), fee_exempt=False)


def row(s, sid: int) -> dict:
    with connect(s.database_url) as conn:
        return conn.execute("SELECT * FROM submissions WHERE id = %s", (sid,)).fetchone()


def run_all(s, fakes: Fakes):
    with connect(s.database_url, autocommit=True) as conn:
        return dict(drain(conn, fakes.deps(), s))


def events(s, sid: int) -> list[str]:
    with connect(s.database_url) as conn:
        return [e["state"] for e in store.submission_events(conn, sid)]


# ── the happy path ──────────────────────────────────────────────────────────

def test_reference_miner_reaches_queued_with_a_private_pin_and_no_env_left(env, tmp_path):
    s, fakes = env
    sid = submit(s, fakes, reference_files(tmp_path))
    original_key = row(s, sid)["object_key"]
    assert run_all(s, fakes) == {sid: "queued"}
    r = row(s, sid)
    assert events(s, sid) == ["pending", "identity_ok", "fetched", "static_ok", "weights_ok", "stripped", "unique",
                              "built", "qualified", "pinned", "queued"]
    assert r["pin_repo"] == f"pins/rad-testnet-cxr-01-{HOTKEY}-s{sid}" and r["gpu"] == "T4"
    assert original_key not in fakes.bucket  # the zip with .env is gone
    stripped = fakes.bucket[r["stripped_object_key"]]
    files = read_zip(stripped, max_zip_mb=10, max_unzipped_mb=50)
    assert ".env" not in files and code_sha256(files) == r["code_sha256"]
    with connect(s.database_url) as conn:
        logs = {x["kind"]: x["body"] for x in conn.execute(
            "SELECT kind, body FROM submission_logs WHERE submission_id = %s", (sid,)).fetchall()}
    assert logs == {"build": "build log line", "qualification": "qualification log line"}
    assert not any(Path(s.worker_scratch).iterdir())  # scratch cleaned


# ── malicious fixtures: each fails with a stable code, and nothing is pinned ─

def _zip_with(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


def _symlink_entry() -> zipfile.ZipInfo:
    info = zipfile.ZipInfo("src/link.py")
    info.external_attr = 0o120777 << 16
    return info


@pytest.mark.parametrize("fixture,code", [
    ("zip_slip", R.PATH_ESCAPE),
    ("symlink", R.SYMLINK),
    ("pyc", R.OPAQUE_BINARY),
    ("missing_override", R.CONTRACT_MISSING_OVERRIDE),
    ("obfuscated_loader", R.OBFUSCATED),
    ("wrong_hf_revision", R.HF_REVISION_MISMATCH),
    ("slow_load_model", R.LOAD_TIMEOUT),
])
def test_malicious_fixtures_fail_with_stable_codes_and_leave_no_pin(env, tmp_path, fixture, code):
    s, fakes = env
    files = reference_files(tmp_path)
    raw = None
    if fixture in ("zip_slip", "symlink", "pyc"):
        extra = {"zip_slip": ("../../evil.py", b"x = 1\n"), "symlink": (_symlink_entry(), b"/etc/passwd"),
                 "pyc": ("src/cache.pyc", b"\x55\x0d\x0d\x0a\x00\x00\x00\x00\xe3binary")}[fixture]
        raw = _zip_with([*files.items(), extra])
    elif fixture == "missing_override":
        src = files["model.py"].decode()
        files["model.py"] = src[:src.index("    def map_to_labels")].encode()
    elif fixture == "obfuscated_loader":
        files["model.py"] += b"\nimport base64\nexec(base64.b64decode('cHJpbnQoMSk='))\n"
    elif fixture == "wrong_hf_revision":
        fakes.weights_error = R.SolutionRejected(R.HF_REVISION_MISMATCH, "revision resolved elsewhere")
    elif fixture == "slow_load_model":
        fakes.qualify_error = R.SolutionRejected(R.LOAD_TIMEOUT, "import + load_model exceeded 120s")
    sid = submit(s, fakes, files, raw_zip=raw)
    key = row(s, sid)["object_key"]
    assert run_all(s, fakes) == {sid: "rejected"}
    r = row(s, sid)
    assert r["reason_code"] == code
    assert r["pin_repo"] is None and fakes.pins == []
    assert key not in fakes.bucket  # deleted, or replaced by a stripped copy
    if r["stripped_object_key"]:
        assert ".env" not in read_zip(fakes.bucket[r["stripped_object_key"]], max_zip_mb=10, max_unzipped_mb=50)


def test_code_sha256_mismatch_deletes_the_zip_with_env(env, tmp_path):
    # Intake no longer inflates zips, so a wrong code_sha256 is first caught at g1.
    # Such a zip cannot be stripped and verified, so it must not stay in the bucket.
    s, fakes = env
    data = deterministic_zip(reference_files(tmp_path))
    key = f"radiant/private/campaigns/testnet-cxr-01/solutions/{HOTKEY}/{uuid.uuid4()}.zip"
    fakes.bucket[key] = data
    com = SolutionCommitment(uid=44, hotkey=HOTKEY, coldkey="5Cold", commit_block=105, campaign_id="testnet-cxr-01",
                             sha256="sha256:" + hashlib.sha256(data).hexdigest(), code_sha256="sha256:" + "1" * 64,
                             object_key=key, raw=f"test-{uuid.uuid4()}", payment_block=104, payment_tx=7)
    with connect(s.database_url) as conn:
        sid = store.insert_submission(conn, com, size_bytes=len(data), fee_exempt=False)
    assert run_all(s, fakes) == {sid: "rejected"}
    assert row(s, sid)["reason_code"] == R.CODE_SHA256_MISMATCH
    assert key not in fakes.bucket and not fakes.bucket


# ── identity, duplicates, infrastructure ───────────────────────────────────

def test_unregistered_and_disqualified_hotkeys_are_rejected_at_g0(env, tmp_path):
    s, fakes = env
    files = reference_files(tmp_path)
    a = submit(s, fakes, files, hotkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty")
    files2 = {**files, "README.md": files["README.md"] + b"\n"}
    b = submit(s, fakes, files2, hotkey=OTHER)
    with connect(s.database_url) as conn:
        conn.execute("INSERT INTO campaign_disqualifications (campaign_id, hotkey, reason) VALUES "
                     "('testnet-cxr-01', %s, 'copied another miner')", (OTHER,))
    assert run_all(s, fakes) == {a: "rejected", b: "rejected"}
    assert row(s, a)["reason_code"] == R.NOT_REGISTERED and row(s, b)["reason_code"] == R.DISQUALIFIED


def test_same_code_and_weights_is_a_duplicate_of_the_earlier_commit(env, tmp_path):
    s, fakes = env
    files = reference_files(tmp_path)
    first = submit(s, fakes, files, block=105)
    # Same code, a different .env (so a different zip hash), same weights.
    files2 = {**files, ".env": files[".env"] + b"# another token\n"}
    second = submit(s, fakes, files2, hotkey=OTHER, block=110)
    assert run_all(s, fakes) == {first: "queued", second: "rejected_duplicate"}
    assert row(s, second)["reason_code"] == R.DUPLICATE and fakes.pins == [row(s, first)["pin_repo"]]


def test_gpu_unavailable_is_infra_failed_and_requeued_once(env, tmp_path, monkeypatch):
    from dataclasses import replace

    s, fakes = env
    s = replace(s, infra_retry_after_s=0)
    fakes.qualify_error = R.InfraUnavailable(R.GPU_UNAVAILABLE, "no L4 within 900s")
    sid = submit(s, fakes, reference_files(tmp_path))
    with connect(s.database_url, autocommit=True) as conn:
        from worker.pipeline import claim, process

        assert process(conn, claim(conn), fakes.deps(), s) == "pending"       # first: requeued
        assert row(s, sid)["infra_failures"] == 1 and row(s, sid)["pin_repo"] is None
        assert process(conn, claim(conn), fakes.deps(), s) == "infra_failed"  # second: terminal
    r = row(s, sid)
    assert r["state"] == "infra_failed" and r["reason_code"] == R.GPU_UNAVAILABLE and fakes.pins == []


def test_infra_then_success_reaches_queued(env, tmp_path, monkeypatch):
    from dataclasses import replace

    s, fakes = env
    s = replace(s, infra_retry_after_s=0)
    fakes.qualify_error = R.InfraUnavailable(R.GPU_UNAVAILABLE, "no L4")
    sid = submit(s, fakes, reference_files(tmp_path))
    with connect(s.database_url, autocommit=True) as conn:
        from worker.pipeline import claim, process

        assert process(conn, claim(conn), fakes.deps(), s) == "pending"
        fakes.qualify_error = None
        assert process(conn, claim(conn), fakes.deps(), s) == "queued"
    assert "infra_failed" in events(s, sid) and events(s, sid)[-1] == "queued"


def test_unexpected_errors_are_the_validators_not_the_miners(env, tmp_path):
    s, fakes = env
    fakes.qualify_error = RuntimeError("modal client exploded")
    sid = submit(s, fakes, reference_files(tmp_path))
    assert run_all(s, fakes) == {sid: "pending"}  # one automatic requeue, after a back-off
    r = row(s, sid)
    assert r["reason_code"] == R.WORKER_ERROR and r["next_attempt_at"] is not None
    assert run_all(s, fakes) == {}  # not retried inside the back-off
    # .env zip is scrubbed after g5, while weights remain on scratch and image_id is saved for resume
    assert not (Path(s.worker_scratch) / f"sub-{sid}" / "solution.zip").exists()
    assert r["stripped_object_key"] is not None and r["image_id"] is not None


def test_operator_requeue_after_terminal_infra_failure_resumes_without_refetching_hf(env, tmp_path, monkeypatch):
    from dataclasses import replace

    s, fakes = env
    s = replace(s, infra_retry_after_s=0)
    hf_calls = 0
    build_calls = 0
    base_deps = fakes.deps()

    def counted_weights(*a, **k):
        nonlocal hf_calls
        hf_calls += 1
        return base_deps.fetch_weights(*a, **k)

    def counted_build(*a, **k):
        nonlocal build_calls
        build_calls += 1
        return base_deps.build(*a, **k)

    deps = Deps(**{**base_deps.__dict__, "fetch_weights": counted_weights, "build": counted_build})
    fakes.qualify_error = R.InfraUnavailable(R.GPU_UNAVAILABLE, "no T4")
    sid = submit(s, fakes, reference_files(tmp_path))
    with connect(s.database_url, autocommit=True) as conn:
        from worker.admin import requeue_submission
        from worker.pipeline import claim, process

        assert process(conn, claim(conn), deps, s) == "pending"
        assert process(conn, claim(conn), deps, s) == "infra_failed"
        requeue_submission(conn, sid)
        fakes.qualify_error = None
        assert process(conn, claim(conn), deps, s) == "queued"
    assert hf_calls == 1 and build_calls == 1  # HF token and image build used once across all retries
    assert not any(Path(s.worker_scratch).iterdir())  # scratch removed once settled to queued


def test_duplicate_prefers_earlier_commit_block_even_with_higher_id(env, tmp_path):
    s, fakes = env
    files = reference_files(tmp_path)
    # Later commit ingested first (lower id), earlier commit ingested second (higher id).
    later = submit(s, fakes, {**files, ".env": files[".env"] + b"# copy\n"}, hotkey=OTHER, block=200)
    earlier = submit(s, fakes, files, hotkey=HOTKEY, block=100)
    assert later < earlier
    with connect(s.database_url, autocommit=True) as conn:
        from worker.pipeline import claim, process

        # Process the earlier-commit row first (or both): earlier commit must NOT be rejected as dup of later commit.
        sub_earlier = conn.execute("SELECT * FROM submissions WHERE id = %s", (earlier,)).fetchone()
        sub_later = conn.execute("SELECT * FROM submissions WHERE id = %s", (later,)).fetchone()
        assert process(conn, sub_earlier, fakes.deps(), s) == "queued"
        assert process(conn, sub_later, fakes.deps(), s) == "rejected_duplicate"


# ── reaper ──────────────────────────────────────────────────────────────────

def test_reaper_deletes_orphans_but_never_referenced_or_other_miners_uploads(env, tmp_path):
    s, fakes = env
    sid = submit(s, fakes, reference_files(tmp_path))
    referenced = row(s, sid)["object_key"]
    orphan = f"radiant/private/campaigns/testnet-cxr-01/solutions/{HOTKEY}/{uuid.uuid4()}.zip"
    fakes.bucket[orphan] = b"never committed"
    with connect(s.database_url) as conn:
        for key in (orphan, referenced):
            conn.execute("INSERT INTO upload_authorizations (hotkey, campaign_id, sha256, object_key, size_bytes, "
                         "created_at) VALUES (%s, 'testnet-cxr-01', 'sha256:x', %s, 1, now() - interval '3 days')",
                         (HOTKEY, key))
        # Another hotkey's refused commitment pointing at the miner's upload must not delete it.
        from chain.commitment import encode

        raw = encode(campaign_id="testnet-cxr-01", sha256="sha256:" + "1" * 64, code_sha256="sha256:" + "2" * 64,
                     object_key=referenced, payment_block=1, payment_tx=1)
        conn.execute("INSERT INTO intake_rejections (hotkey, commit_block, raw, reason) VALUES "
                     "(%s, 200, %s, 'locator_hotkey_mismatch')", (OTHER, raw))
    with connect(s.database_url, autocommit=True) as conn:
        assert reap(conn, lambda k: fakes.bucket.pop(k, None)) == [orphan]
    assert referenced in fakes.bucket and orphan not in fakes.bucket
