"""Seed a campaign from a manifest file. Safe to rerun.

    python -m campaign.seed --manifest campaign/fixtures/testnet-cxr-01.json
    python -m campaign.seed --manifest FILE --check     # print the hash, no database

Behaviour on rerun:
    no row with that campaign_id     insert it (status from --status, default draft)
    a row with the same hash         nothing changes; exit 0
    a draft with a different hash    refuse, unless --replace-draft
    a non-draft with a different hash refuse: the manifest is frozen

The fee recipient and amount can be overridden from the environment
(RADIANT_FEE_RECIPIENT, RADIANT_FEE_TAO) so one fixture serves local, testnet
and mainnet without editing it. The fee is outside the hash either way.
Prints one line, ``<status-word> <campaign_id> <manifest_hash>``, so scripts
can compare hashes across reruns.
"""

from __future__ import annotations

import argparse
import os
import sys

from .manifest import load_manifest_file
from .models import ManifestError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m campaign.seed")
    p.add_argument("--manifest", required=True)
    p.add_argument("--status", choices=("draft", "open"), default="draft", help="status for a new row")
    p.add_argument("--fee-tao", default=os.getenv("RADIANT_FEE_TAO") or None)
    p.add_argument("--fee-recipient", default=os.getenv("RADIANT_FEE_RECIPIENT") or None)
    p.add_argument("--replace-draft", action="store_true")
    p.add_argument("--check", action="store_true", help="validate and print the hash without touching the database")
    args = p.parse_args(argv)

    try:
        raw_fee = None
        if args.fee_tao is not None or args.fee_recipient is not None:
            import json
            from pathlib import Path

            base = json.loads(Path(args.manifest).read_text()).get("submission_fee", {})
            raw_fee = {**base, **({"amount_tao": args.fee_tao} if args.fee_tao is not None else {}),
                       **({"recipient": args.fee_recipient} if args.fee_recipient is not None else {})}
        m = load_manifest_file(args.manifest, submission_fee=raw_fee)
    except (ManifestError, ValueError) as exc:
        print(f"seed: invalid manifest: {exc}", file=sys.stderr)
        return 2
    for w in m.warnings:
        print(f"seed: warning: {w}", file=sys.stderr)
    if args.check:
        print(f"valid {m.campaign_id} {m.manifest_hash}")
        return 0

    from db.connection import connect

    from . import store

    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('radiant_seed'))")
        row = store.get_campaign(conn, m.campaign_id)
        if row is None:
            row = store.insert_campaign(conn, m, status=args.status)
            word = "seeded"
        elif row["manifest_hash"] == m.manifest_hash:
            word = "unchanged"
        elif row["status"] == "draft" and args.replace_draft:
            row = store.replace_draft_manifest(conn, m)
            word = "replaced"
        else:
            print(f"seed: campaign {m.campaign_id} exists ({row['status']}) with hash {row['manifest_hash']}, "
                  f"not {m.manifest_hash}. "
                  + ("Pass --replace-draft to replace the draft." if row["status"] == "draft"
                     else "Its manifest is frozen; seed a new campaign_id instead."), file=sys.stderr)
            return 3
        if not store.stored_hash_matches(row):
            print(f"seed: stored manifest for {m.campaign_id} does not recompute to its hash", file=sys.stderr)
            return 4
    print(f"{word} {row['id']} {row['manifest_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
