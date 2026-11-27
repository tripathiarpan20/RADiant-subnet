"""Append a block-effective fee change to a campaign.

    python -m campaign.set_fee testnet-cxr-01 --amount-tao 0.05 --effective-from-block 123456

The recipient defaults to the current one. History is append-only: the new
block must be above every earlier entry's.
"""

from __future__ import annotations

import argparse
import sys

from db.connection import connect

from . import store
from .fees import fee_at_block


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m campaign.set_fee")
    p.add_argument("campaign_id")
    p.add_argument("--amount-tao", required=True)
    p.add_argument("--recipient")
    p.add_argument("--effective-from-block", type=int, required=True)
    args = p.parse_args(argv)
    with connect() as conn:
        row = store.get_campaign(conn, args.campaign_id)
        if row is None:
            print(f"set_fee: no campaign {args.campaign_id}", file=sys.stderr)
            return 2
        current = fee_at_block(row["submission_fee_history"], args.effective_from_block)
        fee = {"amount_tao": args.amount_tao, "recipient": args.recipient or current["recipient"]}
        try:
            row = store.append_fee(conn, args.campaign_id, fee, args.effective_from_block)
        except ValueError as exc:
            print(f"set_fee: {exc}", file=sys.stderr)
            return 2
    print(f"fee history for {args.campaign_id} now has {len(row['submission_fee_history'])} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
