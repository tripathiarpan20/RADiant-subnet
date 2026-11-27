"""R3 exit-gate checks against a real testnet payment (run inside the stack's watcher container).

    python -m scripts.r3_checks <sha256 of the ingested zip>

With the submission the watcher ingested from the real chain:

1. it exists exactly once, with one intake ('pending') event, and rescanning twice adds nothing
2. its real fee proof, re-checked against wrong terms, is rejected:
   wrong recipient, a fee above what was paid, and a signer that isn't the miner
3. a second commitment reusing that payment is rejected (payment_ref_already_used)
4. a commitment from another hotkey pointing at the miner's upload is rejected
   (locator_hotkey_mismatch), and neither claims a submission row

Prints one line per check and exits non-zero on the first failure.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from functools import partial

import config
from campaign import store
from chain.commitment import SolutionCommitment
from chain.payment import fetch_block_payment_view, verify_payment
from chain.watcher import ingest
from db.connection import connect


def ok(msg: str) -> None:
    print(f"  pass  {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}", flush=True)
    sys.exit(1)


def main(sha: str) -> int:
    import bittensor as bt

    from worker.watcher import scan_once

    s = config.settings()
    for _ in range(2):
        scan_once(s)
    with connect(s.require_database(), autocommit=True) as conn:
        rows = store.find_submissions(conn, sha)
        if len(rows) != 1:
            fail(f"expected exactly one submission for {sha}, found {len(rows)}")
        sub = rows[0]
        events = [e["state"] for e in store.submission_events(conn, sub["id"])]
        if not events or events[0] != "pending" or events.count("pending") != 1:
            fail(f"expected exactly one intake ('pending') event first, found {events}")
        ok(f"submission {sub['id']} ingested exactly once (uid {sub['uid']}, payment "
           f"{sub['payment_block']}-{sub['payment_tx']}), unchanged after two more scans")

        subtensor = bt.Subtensor(network=s.network)
        view = fetch_block_payment_view(subtensor, sub["payment_block"])
        if view is None:
            fail("could not read the payment block back from the chain")
        campaign = store.get_campaign(conn, sub["campaign_id"])
        from campaign.fees import fee_at_block, submission_fee_rao

        fee = fee_at_block(campaign["submission_fee_history"], sub["payment_block"])

        paid = verify_payment(extrinsics=view.extrinsics, events=view.events, extrinsic_index=sub["payment_tx"],
                              recipient=fee["recipient"], min_amount_rao=submission_fee_rao(fee),
                              hotkey=sub["hotkey"], coldkey=sub["coldkey"])
        if not paid.ok:
            fail(f"the real payment does not verify: {paid.reason}")
        ok(f"real payment verifies: {paid.amount_rao} rao to {fee['recipient'][:8]}…")
        cases = {
            "payment_recipient_mismatch": dict(recipient="5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM"),
            "payment_below_fee": dict(min_amount_rao=paid.amount_rao + 1),
            "payment_signer_not_miner": dict(hotkey="5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM", coldkey=""),
        }
        base = dict(extrinsics=view.extrinsics, events=view.events, extrinsic_index=sub["payment_tx"],
                    recipient=fee["recipient"], min_amount_rao=submission_fee_rao(fee), hotkey=sub["hotkey"],
                    coldkey=sub["coldkey"])
        for expected, change in cases.items():
            got = verify_payment(**{**base, **change}).reason
            if got != expected:
                fail(f"{expected}: got {got!r}")
            ok(f"the same real transfer is rejected as {expected}")

        com = SolutionCommitment(uid=sub["uid"] or 0, hotkey=sub["hotkey"], coldkey=sub["coldkey"],
                                 commit_block=sub["commit_block"] + 1, campaign_id=sub["campaign_id"],
                                 sha256="sha256:" + "1" * 64, code_sha256="sha256:" + "2" * 64,
                                 object_key=sub["object_key"], raw="r3-gate-reuse", payment_block=sub["payment_block"],
                                 payment_tx=sub["payment_tx"])
        fetch_block = partial(fetch_block_payment_view, subtensor)
        never = lambda key, n: None  # noqa: E731  (must not get as far as fetching)
        r = ingest(conn, com, fetch_block=fetch_block, fetch_object=never, s=s)
        if r.reason != "payment_ref_already_used":
            fail(f"reused payment: got {r.reason}")
        ok("a second commitment reusing the payment is rejected (payment_ref_already_used)")

        meta_validator = "5DhV69RTR4yN6onp8cwNu3Mtgr41jXQDy4wjKxEWkhwvTUS6"
        foreign = replace(com, hotkey=meta_validator, coldkey="", raw="r3-gate-foreign", payment_block=None,
                          payment_tx=None)
        r = ingest(conn, foreign, fetch_block=fetch_block, fetch_object=never, s=s)
        if r.reason != "locator_hotkey_mismatch":
            fail(f"foreign locator: got {r.reason}")
        ok("another hotkey committing the miner's upload is rejected (locator_hotkey_mismatch)")
        n = conn.execute("SELECT count(*) AS n FROM submissions").fetchone()["n"]
        if n != 1:
            fail(f"rejections claimed rows: {n} submissions")
        ok("rejected commitments claimed no submission rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
