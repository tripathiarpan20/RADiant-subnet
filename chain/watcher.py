"""Turn on-chain solution commitments into queued submissions, exactly once.

Ported from Pareton chain/watcher.py and adapted to solution zips. For each
registered hotkey's latest RADiant commitment, in chain order:

1. the campaign exists and is open
2. not already ingested (first seen per (campaign, sha256) wins)
3. the object key names this campaign and *this* hotkey (locator ownership)
4. the fee proof: present (unless the hotkey is fee-exempt), paid no later
   than the commitment, never used before, and a successful transfer from the
   hotkey or its coldkey to the recipient at the fee in force at its block
5. the object exists, fits the campaign's zip cap, and its bytes hash to the
   committed sha256 and code_sha256 (gate g1 starts here)

A refusal is recorded in intake_rejections with a reason code and never
claims the dedupe slot, so a bad proof can't block the honest commitment.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import config
from campaign.fees import fee_at_block, submission_fee_rao
from chain.commitment import SolutionCommitment, build, parse_object_key
from chain.payment import BlockPaymentView, PaymentCheck, verify_payment

logger = logging.getLogger(__name__)

BlockFetcher = Callable[[int], BlockPaymentView | None]
ObjectFetcher = Callable[[str, int], bytes | None]   # (key, max_bytes) -> bytes, or None if missing


# Refusals that may resolve on their own (chain hiccup, campaign opening
# later). Everything else is permanent for that exact commitment, so it is
# not re-checked on every scan.
TRANSIENT = frozenset({"payment_block_unavailable", "payment_no_chain_access", "payment_outcome_unknown",
                       "campaign_not_open", "object_missing_or_too_large"})


@dataclass(frozen=True)
class IngestResult:
    submission_id: int | None
    reason: str  # "ingested", "duplicate", or a rejection code


def check_fee(com: SolutionCommitment, history: list, fetch_block: BlockFetcher | None, consumed: bool, *,
              max_payment_age_blocks: int = 7200, min_payment_block: int = 0) -> PaymentCheck:
    if com.payment_block is None or com.payment_tx is None:
        return PaymentCheck.reject("payment_proof_missing")
    if com.payment_block > com.commit_block:
        return PaymentCheck.reject("payment_after_commitment")
    if min_payment_block > 0 and com.payment_block < min_payment_block:
        return PaymentCheck.reject("payment_before_campaign_open")
    if max_payment_age_blocks > 0 and (com.commit_block - com.payment_block) > max_payment_age_blocks:
        return PaymentCheck.reject("payment_too_old")
    if consumed:
        return PaymentCheck.reject("payment_ref_already_used")
    fee = fee_at_block(history, com.payment_block)
    fee_commit = fee_at_block(history, com.commit_block)
    min_rao = submission_fee_rao(fee)
    # A transfer from a zero-fee era cannot satisfy a campaign that now charges a fee.
    if min_rao == 0 and submission_fee_rao(fee_commit) > 0:
        min_rao = submission_fee_rao(fee_commit)
    if fetch_block is None:
        return PaymentCheck.reject("payment_no_chain_access")
    view = fetch_block(com.payment_block)
    if view is None:
        return PaymentCheck.reject("payment_block_unavailable")
    return verify_payment(extrinsics=view.extrinsics, events=view.events, extrinsic_index=com.payment_tx,
                          recipient=fee["recipient"], min_amount_rao=min_rao,
                          hotkey=com.hotkey, coldkey=com.coldkey)


def ingest(conn, com: SolutionCommitment, *, fetch_block: BlockFetcher | None, fetch_object: ObjectFetcher,
           s: config.Settings) -> IngestResult:
    from campaign import store
    from radiant_sdk.checks import code_sha256, read_zip
    from radiant_sdk.reasons import SolutionRejected

    def reject(reason: str) -> IngestResult:
        store.record_rejection(conn, com, reason)
        logger.info("rejected commitment hotkey=%s block=%d: %s", com.hotkey[:10], com.commit_block, reason)
        return IngestResult(None, reason)

    previous = store.rejection_reason(conn, com)
    if previous is not None and previous not in TRANSIENT:
        return IngestResult(None, previous)
    campaign = store.get_campaign(conn, com.campaign_id)
    if campaign is None:
        return reject("unknown_campaign")
    if campaign["status"] != "open":
        return reject("campaign_not_open")
    if store.get_submission(conn, com.campaign_id, com.sha256) is not None:
        return IngestResult(None, "duplicate")
    loc = parse_object_key(com.object_key)
    if loc is None or loc["prefix"] != s.s3_prefix or loc["campaign"] != com.campaign_id:
        return reject("locator_not_ours")
    if loc["hotkey"] != com.hotkey:
        return reject("locator_hotkey_mismatch")

    exempt = com.hotkey in s.fee_exempt_hotkeys
    if not exempt:
        history = campaign["submission_fee_history"]
        fee_due = submission_fee_rao(fee_at_block(history, com.commit_block)) > 0
        if fee_due or com.payment_block is not None:
            consumed = com.payment_block is not None and store.payment_ref_used(conn, com.payment_block,
                                                                                com.payment_tx)
            check = check_fee(com, history, fetch_block, consumed,
                              max_payment_age_blocks=s.max_payment_age_blocks,
                              min_payment_block=campaign.get("opened_at_block") or 0)
            if not check.ok:
                return reject(check.reason)

    limits = campaign["manifest"]["limits"]
    data = fetch_object(com.object_key, limits["max_solution_zip_mb"] * 1_000_000)
    if data is None:
        return reject("object_missing_or_too_large")
    if "sha256:" + hashlib.sha256(data).hexdigest() != com.sha256:
        return reject("sha256_mismatch")
    try:
        files = read_zip(data, max_zip_mb=limits["max_solution_zip_mb"],
                         max_unzipped_mb=limits["max_solution_unzipped_mb"])
    except SolutionRejected as exc:
        return reject(exc.code.lower())
    if code_sha256(files) != com.code_sha256:
        return reject("code_sha256_mismatch")

    sid = store.insert_submission(conn, com, size_bytes=len(data), fee_exempt=exempt)
    if sid is None:  # lost a race to an identical commitment, or the proof was used in between
        return IngestResult(None, "duplicate")
    logger.info("queued submission %d campaign=%s hotkey=%s %s", sid, com.campaign_id, com.hotkey[:10], com.sha256)
    return IngestResult(sid, "ingested")


def scan(conn, meta: Any, revealed: dict[str, list[tuple[int, str]]], *, fetch_block: BlockFetcher | None,
         fetch_object: ObjectFetcher, s: config.Settings) -> list[IngestResult]:
    """Ingest every registered hotkey's latest commitment, oldest commitment first.

    Plaintext commitments make the hash public at commit time, so chain order
    (not UID order) decides first-seen: a later copycat can't win the slot.
    """
    ordered = sorted(build(meta, revealed).values(), key=lambda c: (c.commit_block, c.hotkey))
    results = []
    for com in ordered:
        with conn.transaction():
            results.append(ingest(conn, com, fetch_block=fetch_block, fetch_object=fetch_object, s=s))
    return results
