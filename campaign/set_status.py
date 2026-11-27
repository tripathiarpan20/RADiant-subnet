"""Move a campaign along draft -> open <-> closed -> archived.

    python -m campaign.set_status testnet-cxr-01 open

The database trigger refuses any other move, and refuses opening a campaign
when the open campaigns' emission start weights would sum past 1.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from db.connection import connect

from . import store


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m campaign.set_status")
    p.add_argument("campaign_id")
    p.add_argument("status", choices=("draft", "open", "closed", "archived"))
    args = p.parse_args(argv)
    try:
        with connect() as conn:
            row = store.set_status(conn, args.campaign_id, args.status)
    except (ValueError, psycopg.errors.RaiseException) as exc:
        print(f"set_status: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 2
    print(f"{row['id']} is now {row['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
