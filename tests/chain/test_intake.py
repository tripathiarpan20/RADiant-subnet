"""R3 intake: codec, upload auth, fee proofs and the watcher, end to end on Postgres + moto S3.

The chain is faked at the boundary the watcher actually uses: a metagraph
(hotkeys, coldkeys), revealed commitments, and decoded block payment views in
the same dict shapes bittensor 11 returns (see chain/payment.py).
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
import zipfile
from dataclasses import dataclass

import boto3
import pytest

import config
from campaign import seed, store
from chain.commitment import encode, parse
from chain.payment import BlockPaymentView
from chain.watcher import scan
from db import migrate as mig
from db.connection import connect
from storage import s3
from storage.upload_auth import upload_message, verify_upload_request

from ..db.test_db import FIXTURE

moto_server = pytest.importorskip("moto.server")
bt_sp = pytest.importorskip("bittensor.sp_core")

RECIPIENT = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"  # the fixture's fee recipient (dev key Alice)
FEE_RAO = 10_000_000  # 0.01 TAO


def keypair(uri: str):
    return bt_sp.Keypair.create_from_uri(uri)


MINER = keypair("//Bob")
MINER_COLD = keypair("//Bob//cold")
OTHER = keypair("//Charlie")


# ── a solution zip with known hashes ────────────────────────────────────────

def solution_zip(tag: str = "a") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("model.py", f"# {tag}\n")
        zf.writestr(".env", "HF_REPO_ID=a/b\nHF_REVISION=" + "0" * 40 + "\n")
    return buf.getvalue()


def hashes(data: bytes) -> tuple[str, str]:
    from radiant_sdk.checks import code_sha256, read_zip

    return "sha256:" + hashlib.sha256(data).hexdigest(), code_sha256(read_zip(data, max_zip_mb=10,
                                                                               max_unzipped_mb=50))


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def s3_endpoint():
    srv = moto_server.ThreadedMotoServer(port=0, verbose=False)
    srv.start()
    host, port = srv.get_host_and_port()
    yield f"http://{host}:{port}"
    srv.stop()


@pytest.fixture
def settings(db_url, s3_endpoint, monkeypatch):
    env = {"RADIANT_S3_ENDPOINT_URL": s3_endpoint, "RADIANT_S3_ACCESS_KEY": "a", "RADIANT_S3_SECRET_KEY": "b",
           "RADIANT_S3_BUCKET": f"b-{uuid.uuid4().hex[:8]}", "RADIANT_NETWORK": "test", "RADIANT_NETUID": "292"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = config.settings()
    boto3.client("s3", endpoint_url=s3_endpoint, region_name="us-east-1", aws_access_key_id="a",
                 aws_secret_access_key="b").create_bucket(Bucket=s.s3_bucket)
    assert mig.main([]) == 0
    assert seed.main(["--manifest", str(FIXTURE), "--status", "open"]) == 0
    return s


@dataclass
class Meta:
    hotkeys: list[str]
    coldkeys: list[str]


def transfer(signer: str, dest: str, amount: int, ok: bool = True) -> tuple[dict, list[dict]]:
    ext = {"address": signer, "call": {"call_module": "Balances", "call_function": "transfer_keep_alive",
                                       "call_args": [{"name": "dest", "value": dest},
                                                     {"name": "value", "value": amount}]}}
    ev = [{"extrinsic_idx": 1, "event": {"module_id": "System",
                                         "event_id": "ExtrinsicSuccess" if ok else "ExtrinsicFailed"}}]
    return ext, ev


class FakeChain:
    def __init__(self):
        self.blocks: dict[int, BlockPaymentView] = {}

    def pay(self, block: int, signer: str, dest: str = RECIPIENT, amount: int = FEE_RAO, ok: bool = True) -> None:
        ext, ev = transfer(signer, dest, amount, ok)
        self.blocks[block] = BlockPaymentView(extrinsics=[{"call": {}}, ext], events=ev)

    def fetch_block(self, block: int):
        return self.blocks.get(block)


def upload(s, data: bytes, hotkey: str) -> str:
    sha, _ = hashes(data)
    p = s3.presign_upload(s, campaign_id="testnet-cxr-01", hotkey=hotkey, sha256=sha, size_bytes=len(data),
                          upload_id=str(uuid.uuid4()), expires_in=300)
    import requests

    assert requests.put(p.upload_url, data=data, headers=p.required_headers, timeout=10).status_code == 200
    return p.object_key


def fetch_object(s):
    def f(key, max_bytes):
        size = s3.head_size(s, key)
        return None if size is None or size > max_bytes else s3.fetch_bounded(s, key, max_bytes)
    return f


def commitment(data: bytes, key: str, block: int | None, tx: int | None = 1) -> str:
    sha, code = hashes(data)
    return encode(campaign_id="testnet-cxr-01", sha256=sha, code_sha256=code, object_key=key,
                  payment_block=block, payment_tx=tx if block is not None else None)


def run_scan(s, chain, revealed, meta=None):
    meta = meta or Meta([MINER.ss58_address, OTHER.ss58_address], [MINER_COLD.ss58_address, ""])
    with connect(s.database_url, autocommit=True) as conn:
        return [r.reason for r in scan(conn, meta, revealed, fetch_block=chain.fetch_block,
                                       fetch_object=fetch_object(s), s=s)]


# ── tests ───────────────────────────────────────────────────────────────────

def test_commitment_round_trip_and_size():
    key = ("radiant/private/campaigns/testnet-cxr-01/solutions/" + MINER.ss58_address + "/"
           + str(uuid.uuid4()) + ".zip")
    raw = encode(campaign_id="testnet-cxr-01", sha256="sha256:" + "a" * 64, code_sha256="sha256:" + "b" * 64,
                 object_key=key, payment_block=2**31 - 1, payment_tx=2**31 - 1)
    assert len(raw.encode()) <= 384
    p = parse(raw)
    assert p["key_hotkey"] == MINER.ss58_address and p["code_sha256"] == "sha256:" + "b" * 64
    assert parse(raw.replace("r1|", "v2|")) is None
    assert parse(raw.replace("testnet-cxr-01|", "other-campaign|", 1)) is None  # key names another campaign


def test_upload_auth_signature_network_and_expiry(settings):
    fields = {"campaign_id": "testnet-cxr-01", "hotkey": MINER.ss58_address, "sha256": "sha256:" + "a" * 64,
              "code_sha256": "sha256:" + "b" * 64, "size_bytes": 10, "upload_id": str(uuid.uuid4()),
              "expires_at": int(time.time()) + 120, "network": "test", "netuid": 292}
    sig = MINER.sign(upload_message(fields)).hex()
    assert verify_upload_request(fields, sig, settings) > 0
    with pytest.raises(ValueError, match="signature"):
        verify_upload_request(fields, OTHER.sign(upload_message(fields)).hex(), settings)
    with pytest.raises(ValueError, match="test/292"):
        f2 = {**fields, "netuid": 1}
        verify_upload_request(f2, MINER.sign(upload_message(f2)).hex(), settings)
    with pytest.raises(ValueError, match="expired"):
        f3 = {**fields, "expires_at": int(time.time()) - 1}
        verify_upload_request(f3, MINER.sign(upload_message(f3)).hex(), settings)


def test_paid_submission_is_ingested_exactly_once(settings):
    chain, data = FakeChain(), solution_zip()
    key = upload(settings, data, MINER.ss58_address)
    chain.pay(100, MINER_COLD.ss58_address)
    revealed = {MINER.ss58_address: [(105, commitment(data, key, 100))]}
    assert run_scan(settings, chain, revealed) == ["ingested"]
    assert run_scan(settings, chain, revealed) == ["duplicate"]  # every later scan
    with connect(settings.database_url) as conn:
        rows = store.find_submissions(conn, hashes(data)[0])
        assert len(rows) == 1 and rows[0]["state"] == "queued" and rows[0]["uid"] == 0
        assert [e["state"] for e in store.submission_events(conn, rows[0]["id"])] == ["queued"]


@pytest.mark.parametrize("case,expected", [
    ("underpaid", "payment_below_fee"),
    ("wrong_recipient", "payment_recipient_mismatch"),
    ("someone_elses_transfer", "payment_signer_not_miner"),
    ("failed_transfer", "payment_extrinsic_failed"),
    ("paid_after_commit", "payment_after_commitment"),
    ("no_proof", "payment_proof_missing"),
    ("foreign_locator", "locator_hotkey_mismatch"),
    ("tampered_zip", "sha256_mismatch"),
])
def test_bad_proofs_and_locators_are_rejected_without_taking_the_slot(settings, case, expected):
    chain, data = FakeChain(), solution_zip(case)
    owner = OTHER.ss58_address if case == "foreign_locator" else MINER.ss58_address
    key = upload(settings, data, owner)
    signer = OTHER.ss58_address if case == "someone_elses_transfer" else MINER_COLD.ss58_address
    chain.pay(100, signer, dest="5Elsewhere" if case == "wrong_recipient" else RECIPIENT,
              amount=FEE_RAO // 2 if case == "underpaid" else FEE_RAO, ok=case != "failed_transfer")
    block = None if case == "no_proof" else 100
    commit_block = 99 if case == "paid_after_commit" else 105
    raw = commitment(data, key, block)
    if case == "tampered_zip":
        other = solution_zip("different")
        raw = commitment(other, key, block)  # commits other bytes than were uploaded
    assert run_scan(settings, chain, {MINER.ss58_address: [(commit_block, raw)]}) == [expected]
    with connect(settings.database_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM submissions").fetchone()["n"] == 0
        assert conn.execute("SELECT reason FROM intake_rejections").fetchone()["reason"] == expected


def test_a_payment_reference_backs_only_one_submission(settings):
    chain = FakeChain()
    chain.pay(100, MINER_COLD.ss58_address)
    first, second = solution_zip("one"), solution_zip("two")
    k1, k2 = upload(settings, first, MINER.ss58_address), upload(settings, second, MINER.ss58_address)
    assert run_scan(settings, chain, {MINER.ss58_address: [(105, commitment(first, k1, 100))]}) == ["ingested"]
    # Same hotkey commits a new zip reusing the same payment.
    assert run_scan(settings, chain, {MINER.ss58_address: [(205, commitment(second, k2, 100))]}) == \
        ["payment_ref_already_used"]


def test_first_seen_in_chain_order_wins_against_a_copycat(settings):
    chain, data = FakeChain(), solution_zip("orig")
    k_orig = upload(settings, data, MINER.ss58_address)
    k_copy = upload(settings, data, OTHER.ss58_address)
    chain.pay(100, MINER_COLD.ss58_address)
    chain.pay(101, OTHER.ss58_address)
    revealed = {OTHER.ss58_address: [(120, commitment(data, k_copy, 101))],   # copycat, later block, lower uid?
                MINER.ss58_address: [(110, commitment(data, k_orig, 100))]}
    meta = Meta([OTHER.ss58_address, MINER.ss58_address], ["", MINER_COLD.ss58_address])
    assert run_scan(settings, chain, revealed, meta) == ["ingested", "duplicate"]
    with connect(settings.database_url) as conn:
        assert store.find_submissions(conn, hashes(data)[0])[0]["hotkey"] == MINER.ss58_address


def test_fee_exempt_hotkey_needs_no_proof(settings, monkeypatch):
    monkeypatch.setenv("RADIANT_FEE_EXEMPT_HOTKEYS", MINER.ss58_address)
    s = config.settings()
    data = solution_zip("seed")
    key = upload(s, data, MINER.ss58_address)
    assert run_scan(s, FakeChain(), {MINER.ss58_address: [(105, commitment(data, key, None))]}) == ["ingested"]


def test_presigned_put_is_bound_to_checksum_and_key(settings):
    data = solution_zip("put")
    sha, _ = hashes(data)
    p = s3.presign_upload(settings, campaign_id="testnet-cxr-01", hotkey=MINER.ss58_address, sha256=sha,
                          size_bytes=len(data), upload_id=str(uuid.uuid4()), expires_in=300)
    assert f"/solutions/{MINER.ss58_address}/" in p.object_key and p.required_headers["If-None-Match"] == "*"
    import requests

    assert requests.put(p.upload_url, data=data, headers=p.required_headers, timeout=10).status_code == 200
    assert requests.put(p.upload_url, data=data, headers=p.required_headers, timeout=10).status_code == 412
    again = s3.presign_upload(settings, campaign_id="testnet-cxr-01", hotkey=MINER.ss58_address, sha256=sha,
                              size_bytes=len(data), upload_id=p.object_key.rsplit("/", 1)[1][:-4], expires_in=300)
    assert again.already_uploaded
    _ = json  # keep import for debugging dumps


def test_upload_api_presigns_for_a_signed_request(settings):
    from fastapi.testclient import TestClient

    from api.server import app

    data = solution_zip("api")
    sha, code = hashes(data)
    fields = {"campaign_id": "testnet-cxr-01", "hotkey": MINER.ss58_address, "sha256": sha, "code_sha256": code,
              "size_bytes": len(data), "upload_id": str(uuid.uuid4()), "expires_at": int(time.time()) + 120,
              "network": "test", "netuid": 292}
    client = TestClient(app)
    body = {**fields, "signature": MINER.sign(upload_message(fields)).hex()}
    r = client.post("/v1/uploads/solution", json=body)
    assert r.status_code == 200, r.text
    p = r.json()
    import requests

    assert requests.put(p["upload_url"], data=data, headers=p["required_headers"], timeout=10).status_code == 200
    forged = {**body, "signature": OTHER.sign(upload_message(fields)).hex()}
    assert client.post("/v1/uploads/solution", json=forged).status_code == 403
    big = {**fields, "size_bytes": 11_000_000}
    assert client.post("/v1/uploads/solution",
                       json={**big, "signature": MINER.sign(upload_message(big)).hex()}).status_code == 413
    assert client.get(f"/v1/submissions/{sha}").status_code == 404
