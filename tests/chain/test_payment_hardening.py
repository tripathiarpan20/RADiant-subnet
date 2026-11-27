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
