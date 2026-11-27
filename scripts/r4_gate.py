"""R4 exit gate, run by scripts/r4_exit_gate.sh against real Modal, Hugging Face and storage.

    python -m scripts.r4_gate

Needs RADIANT_DATABASE_URL (a throwaway database), the RADIANT_S3_* bucket,
HF_PIN_ORG + HF_PIN_TOKEN, and Modal credentials (a `modal setup` login or
MODAL_TOKEN_ID/SECRET). Seeds testnet-cxr-01, then submits eight solutions
straight into the gate queue (intake was R3's gate) and drains it with the
real worker dependencies:

    reference miner        -> queued, with a private pin rad-<campaign>-<hotkey>-s<id>
    zip-slip               -> PATH_ESCAPE
    symlink                -> SYMLINK
    .pyc in src/           -> OPAQUE_BINARY
    missing override       -> CONTRACT_MISSING_OVERRIDE
    obfuscated loader      -> OBFUSCATED
    wrong HF revision      -> HF_UNREACHABLE (a commit that doesn't exist)
    slow load_model        -> LOAD_TIMEOUT (sleeps past model_load_timeout_s in g8, on Modal)

and checks that no failing submission was pinned, that no stored zip still
holds a .env, and that the pin is private. Prints one line per check.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

import config
from campaign import seed, store
from chain.commitment import SolutionCommitment
from db import migrate as mig
from db.connection import connect
from radiant_sdk import reasons as R
from radiant_sdk.checks import code_sha256, read_zip
from radiant_sdk.packaging import collect, deterministic_zip
from storage import s3
from worker.pipeline import drain, real_deps

REPO = Path(__file__).resolve().parents[1]
HOTKEY = "5GZ9S5qgBrHCMr3kdvQ8hXdpZqz9D4eW7xvYxpRQHoVUuGKi"  # bruh_dev_hotkey_3, registered on 292


def ok(msg: str) -> None:
    print(f"  pass  {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}", flush=True)
    sys.exit(1)


def reference_files(tmp: Path) -> dict[str, bytes]:
    d = tmp / "miner"
    shutil.copytree(REPO / "radiant_sdk/reference_miner", d, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(d / ".env.template", d / ".env")
    return collect(d)[0]


def raw_zip(entries: list) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


def fixtures(tmp: Path) -> dict[str, tuple[bytes, str]]:
    import json

    ref = reference_files(tmp)
    # R4_GPU qualifies the fixtures on another allowed GPU (the reference miner asks for T4).
    if os.environ.get("R4_GPU"):
        desc = json.loads(ref["radiant_solution.json"])
        desc["gpu"] = os.environ["R4_GPU"]
        ref["radiant_solution.json"] = (json.dumps(desc, indent=2) + "\n").encode()
    link = zipfile.ZipInfo("src/link.py")
    link.external_attr = 0o120777 << 16
    model = ref["model.py"].decode()
    slow = model.replace("    def load_model(self, weights_dir: Path, device: str) -> None:\n",
                         "    def load_model(self, weights_dir: Path, device: str) -> None:\n"
                         "        import time\n        time.sleep(600)\n", 1)
    env = ref[".env"].decode()
    wrong_rev = "\n".join(("HF_REVISION=" + "0" * 40) if line.startswith("HF_REVISION=") else line
                          for line in env.splitlines()) + "\n"
    return {
        "reference": (deterministic_zip(ref), "queued"),
        "zip_slip": (raw_zip([*ref.items(), ("../../evil.py", b"x = 1\n")]), R.PATH_ESCAPE),
        "symlink": (raw_zip([*ref.items(), (link, b"/etc/passwd")]), R.SYMLINK),
        "pyc": (raw_zip([*ref.items(), ("src/cache.pyc", b"\x55\x0d\x0d\x0a\x00\x00\x00\x00\xe3")]), R.OPAQUE_BINARY),
        "missing_override": (deterministic_zip({**ref, "model.py": model[:model.index("    def map_to_labels")].encode()}),
                             R.CONTRACT_MISSING_OVERRIDE),
        "obfuscated_loader": (deterministic_zip({**ref, "model.py": ref["model.py"]
                                                 + b"\nimport base64\nexec(base64.b64decode('cHJpbnQoMSk='))\n"}),
                              R.OBFUSCATED),
        "wrong_hf_revision": (deterministic_zip({**ref, ".env": wrong_rev.encode()}), R.HF_UNREACHABLE),
        "slow_load_model": (deterministic_zip({**ref, "model.py": slow.encode()}), R.LOAD_TIMEOUT),
    }


def main() -> int:
    s = config.settings()
    dry_pin = os.environ.get("R4_DRY_PIN") == "1"  # dev only: everything real except the HF upload
    if not dry_pin and not (s.hf_pin_org and s.hf_pin_token):
        fail("HF_PIN_ORG and HF_PIN_TOKEN must be set")
    assert mig.main([]) == 0
    assert seed.main(["--manifest", str(REPO / "campaign/fixtures/testnet-cxr-01.json"), "--status", "open"]) == 0
    tmp = Path(tempfile.mkdtemp(prefix="radiant-r4gate-"))
    ids: dict[str, tuple[int, str, str]] = {}
    with connect(s.database_url) as conn:
        store.replace_registered_hotkeys(conn, [(HOTKEY, 44, "")], 1)
        for i, (name, (data, expect)) in enumerate(fixtures(tmp).items()):
            key = f"{s.s3_prefix}/private/campaigns/testnet-cxr-01/solutions/{HOTKEY}/{uuid.uuid4()}.zip"
            s3.put_object(s, key, data)
            try:
                code = code_sha256(read_zip(data, max_zip_mb=10, max_unzipped_mb=50))
            except R.SolutionRejected:
                code = "sha256:" + "0" * 64
            com = SolutionCommitment(uid=44, hotkey=HOTKEY, coldkey="", commit_block=1000 + i,
                                     campaign_id="testnet-cxr-01", sha256="sha256:" + hashlib.sha256(data).hexdigest(),
                                     code_sha256=code, object_key=key, raw=f"r4-gate-{name}", payment_block=999 + i,
                                     payment_tx=i)
            ids[name] = (store.insert_submission(conn, com, size_bytes=len(data), fee_exempt=False), expect, key)
    print(f"  ..    submitted {len(ids)} solutions; running the gates (Modal builds and two qualifications)",
          flush=True)
    deps = real_deps(s)
    if dry_pin:
        from build.hf_pin import Pinned, pin_repo_name

        deps.pin = lambda w, c, h, i: Pinned(f"dry-run/{pin_repo_name(c, h, i)}", "0" * 40, {})
    with connect(s.database_url, autocommit=True) as conn:
        done = dict(drain(conn, deps, s))
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM submissions").fetchall()}
        logs = {(r["submission_id"], r["kind"]) for r in conn.execute(
            "SELECT submission_id, kind FROM submission_logs").fetchall()}

    from huggingface_hub import HfApi

    api = HfApi(token=s.hf_pin_token)
    pins = []
    for name, (sid, expect, key) in ids.items():
        r = rows[sid]
        got = "queued" if r["state"] == "queued" else r["reason_code"]
        if got != expect:
            fail(f"{name}: expected {expect}, got state {r['state']} / {r['reason_code']}: {r['reason_detail']}")
        if expect != "queued" and r["pin_repo"]:
            fail(f"{name}: failed but was pinned to {r['pin_repo']}")
        if s3.head_size(s, key) is not None:
            fail(f"{name}: the original zip (with .env) is still in the bucket")
        if r["stripped_object_key"]:
            stored = s3.fetch_bounded(s, r["stripped_object_key"], 10_000_000)
            if ".env" in read_zip(stored, max_zip_mb=10, max_unzipped_mb=50):
                fail(f"{name}: the stripped zip still holds .env")
        ok(f"{name}: {got}" + (f" ({r['reason_detail'][:70]})" if r["reason_detail"] else ""))
        if r["pin_repo"]:
            pins.append(r["pin_repo"])
    ref_id = ids["reference"][0]
    ref = rows[ref_id]
    if dry_pin:
        ok(f"dry pin run: reference reached queued with pin name {ref['pin_repo']}; no HF upload made")
        print(f"  ..    worker results: {done}")
        return 0
    want = f"{s.hf_pin_org}/rad-testnet-cxr-01-{HOTKEY}-s{ref_id}"
    if ref["pin_repo"] != want:
        fail(f"pin is {ref['pin_repo']}, expected {want}")
    info = api.model_info(want, revision=ref["pin_revision"], files_metadata=True)
    if not info.private:
        fail(f"{want} is public")
    ok(f"reference pinned privately as {want} at {ref['pin_revision'][:12]} "
       f"({', '.join(sorted(x.rfilename for x in info.siblings))})")
    if pins != [want]:
        fail(f"expected exactly one pin, found {pins}")
    ok("no failing submission was pinned")
    for kind in ("build", "qualification"):
        if (ref_id, kind) not in logs:
            fail(f"no {kind} log for the reference miner")
    ok(f"reference build and qualification logs saved; load {ref['qualification'].get('load_total_s')}s on "
       f"{ref['gpu']}")
    slow = rows[ids["slow_load_model"][0]]
    if (slow["id"], "qualification") not in logs:
        fail("no qualification log for the slow fixture")
    ok("every stored zip is stripped of .env or deleted")
    if os.environ.get("R4_KEEP_PIN") != "1":
        api.delete_repo(want)
        ok(f"deleted the test pin {want} (R4_KEEP_PIN=1 keeps it)")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"  ..    worker results: {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
