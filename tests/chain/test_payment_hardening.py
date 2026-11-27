"""Unit tests for payment recency, campaign-start lower bound, and fee-hike bypass guards."""

from __future__ import annotations

from chain.commitment import SolutionCommitment
from chain.payment import BlockPaymentView
from chain.watcher import check_fee

RECIPIENT = "5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a"
HOTKEY = "5GZ9S5qgBrHCMr3kdvQ8hXdpZqz9D4eW7xvYxpRQHoVUuGKi"
COLDKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def _view(amount_rao: int, dest: str = RECIPIENT, signer: str = COLDKEY) -> BlockPaymentView:
    ext = {"address": signer, "call": {"call_module": "Balances", "call_function": "transfer_keep_alive",
                                       "call_args": [{"name": "dest", "value": dest},
                                                     {"name": "value", "value": amount_rao}]}}
    ev = [{"extrinsic_idx": 1, "event": {"module_id": "System", "event_id": "ExtrinsicSuccess"}}]
    return BlockPaymentView(extrinsics=[{"call": {}}, ext], events=ev)


def _com(commit_block: int, payment_block: int) -> SolutionCommitment:
    return SolutionCommitment(
        uid=1, hotkey=HOTKEY, coldkey=COLDKEY, commit_block=commit_block, campaign_id="testnet-cxr-01",
        sha256="sha256:" + "a" * 64, code_sha256="sha256:" + "b" * 64,
        object_key=f"radiant/private/campaigns/testnet-cxr-01/solutions/{HOTKEY}/00000000-0000-0000-0000-000000000001.zip",
        raw="raw", payment_block=payment_block, payment_tx=1,
    )


def test_payment_too_old_is_rejected():
    history = [{"amount_tao": "0.01", "recipient": RECIPIENT, "effective_from_block": 0}]
    res = check_fee(_com(commit_block=10_000, payment_block=1_000), history,
                    lambda b: _view(10_000_000), consumed=False, max_payment_age_blocks=7200)
    assert not res.ok and res.reason == "payment_too_old"

    ok = check_fee(_com(commit_block=8_200, payment_block=1_000), history,
                   lambda b: _view(10_000_000), consumed=False, max_payment_age_blocks=7200)
    assert ok.ok


def test_payment_before_campaign_open_is_rejected():
    history = [{"amount_tao": "0.01", "recipient": RECIPIENT, "effective_from_block": 0}]
    res = check_fee(_com(commit_block=5_050, payment_block=4_999), history,
                    lambda b: _view(10_000_000), consumed=False, min_payment_block=5_000)
    assert not res.ok and res.reason == "payment_before_campaign_open"

    ok = check_fee(_com(commit_block=5_050, payment_block=5_000), history,
                   lambda b: _view(10_000_000), consumed=False, min_payment_block=5_000)
    assert ok.ok


def test_zero_fee_era_dust_transfer_cannot_bypass_fee_hike():
    history = [
        {"amount_tao": "0", "recipient": RECIPIENT, "effective_from_block": 0},
        {"amount_tao": "0.05", "recipient": RECIPIENT, "effective_from_block": 1000},
    ]
    res = check_fee(_com(commit_block=1050, payment_block=999), history,
                    lambda b: _view(1), consumed=False)
    assert not res.ok and res.reason == "payment_below_fee"


def test_watcher_handles_storage_errors_oversize_and_leaves_zip_checks_to_worker(monkeypatch):
    import hashlib

    import config
    from campaign import store
    from chain.watcher import TRANSIENT, ObjectStorageError, ObjectTooLarge, ingest

    assert "object_too_large" not in TRANSIENT
    assert "storage_unavailable" in TRANSIENT

    recorded: list[str] = []
    inserted: list[int] = []
    monkeypatch.setattr(store, "rejection_reason", lambda conn, com: None)
    monkeypatch.setattr(store, "get_campaign", lambda conn, cid: {
        "id": cid, "status": "open", "opened_at_block": None,
        "submission_fee_history": [{"amount_tao": "0", "recipient": RECIPIENT, "effective_from_block": 0}],
        "manifest": {"limits": {"max_solution_zip_mb": 10, "max_solution_unzipped_mb": 50}},
    })
    monkeypatch.setattr(store, "get_submission", lambda conn, cid, sha: None)
    monkeypatch.setattr(store, "record_rejection", lambda conn, com, reason: recorded.append(reason))
    monkeypatch.setattr(store, "insert_submission", lambda conn, com, **kw: (inserted.append(1), 42)[1])

    s = config.Settings.from_env({"RADIANT_S3_PREFIX": "radiant"})
    com = _com(commit_block=100, payment_block=99)
    com = SolutionCommitment(**{**com.__dict__, "payment_block": None, "payment_tx": None})

    def raise_storage(key, cap):
        raise ObjectStorageError("s3 503")

    assert ingest(None, com, fetch_block=None, fetch_object=raise_storage, s=s).reason == "storage_unavailable"

    def raise_large(key, cap):
        raise ObjectTooLarge("too big")

    assert ingest(None, com, fetch_block=None, fetch_object=raise_large, s=s).reason == "object_too_large"

    # Non-zip / hostile bytes that match committed sha256 enter submissions so the worker's g1/g2 rejects them.
    bad_zip = b"not-a-valid-zip-or-zip-slip"
    com2 = SolutionCommitment(**{**com.__dict__, "sha256": "sha256:" + hashlib.sha256(bad_zip).hexdigest()})
    res = ingest(None, com2, fetch_block=None, fetch_object=lambda k, c: bad_zip, s=s)
    assert res.reason == "ingested" and res.submission_id == 42 and inserted == [1]
