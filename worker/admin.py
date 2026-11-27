"""Operator CLI for disqualifying hotkeys and requeuing infra-failed submissions.

    python -m worker.admin disqualify <campaign_id> <hotkey> --reason "..."
    python -m worker.admin undisqualify <campaign_id> <hotkey>
    python -m worker.admin requeue <submission_id>
"""

from __future__ import annotations

import argparse
import sys

from psycopg.types.json import Jsonb

from db.connection import connect


def disqualify(conn, campaign_id: str, hotkey: str, reason: str) -> None:
    conn.execute(
        """INSERT INTO campaign_disqualifications (campaign_id, hotkey, reason) VALUES (%s, %s, %s)
           ON CONFLICT (campaign_id, hotkey) DO UPDATE SET reason = EXCLUDED.reason, created_at = now()""",
        (campaign_id, hotkey, reason.strip()),
    )


def undisqualify(conn, campaign_id: str, hotkey: str) -> bool:
    cur = conn.execute("DELETE FROM campaign_disqualifications WHERE campaign_id = %s AND hotkey = %s",
                       (campaign_id, hotkey))
    return cur.rowcount > 0


def requeue_submission(conn, submission_id: int) -> dict:
    """Move a terminal infra_failed submission back to pending and reset its infra_failures counter."""
    row = conn.execute(
        """UPDATE submissions
           SET state = 'pending', infra_failures = 0, lease_until = NULL, next_attempt_at = NULL,
               reason_code = NULL, reason_detail = NULL, updated_at = now()
           WHERE id = %s AND state = 'infra_failed'
           RETURNING id, campaign_id, state""",
        (submission_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"submission {submission_id} not found or not in infra_failed state")
    conn.execute("INSERT INTO submission_events (submission_id, state, detail) VALUES (%s, 'pending', %s)",
                 (submission_id, Jsonb({"operator_requeue": True})))
    return row


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m worker.admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("disqualify", help="disqualify a hotkey from a campaign (blocks uploads and g0)")
    sp.add_argument("campaign_id")
    sp.add_argument("hotkey")
    sp.add_argument("--reason", required=True)

    sp = sub.add_parser("undisqualify", help="remove a hotkey's disqualification")
    sp.add_argument("campaign_id")
    sp.add_argument("hotkey")

    sp = sub.add_parser("requeue", help="requeue a terminal infra_failed submission")
    sp.add_argument("submission_id", type=int)

    args = p.parse_args(argv)
    try:
        with connect() as conn:
            if args.cmd == "disqualify":
                disqualify(conn, args.campaign_id, args.hotkey, args.reason)
                print(f"disqualified {args.hotkey} from {args.campaign_id}")
            elif args.cmd == "undisqualify":
                removed = undisqualify(conn, args.campaign_id, args.hotkey)
                print(f"{'removed' if removed else 'not present'}: {args.hotkey} in {args.campaign_id}")
            elif args.cmd == "requeue":
                row = requeue_submission(conn, args.submission_id)
                print(f"requeued submission {row['id']} ({row['campaign_id']}) -> {row['state']}")
    except Exception as exc:
        print(f"admin: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
