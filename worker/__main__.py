"""Gate worker: claim pending submissions and run g0 to g9; reap unreferenced uploads.

    python -m worker              # loop
    python -m worker --once       # drain the queue once and print the results
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

import config
from db.connection import connect

logger = logging.getLogger("radiant.worker")


def main(argv: list[str] | None = None) -> int:
    from storage import s3
    from worker.pipeline import drain, real_deps
    from worker.reaper import reap

    p = argparse.ArgumentParser(prog="python -m worker")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = config.settings()
    deps = real_deps(s)
    last_reap = 0.0
    while True:
        try:
            with connect(s.require_database(), autocommit=True) as conn:
                done = drain(conn, deps, s)
                if time.monotonic() - last_reap > 3600 or args.once:
                    reap(conn, lambda key: s3.delete_object(s, key))
                    last_reap = time.monotonic()
            for sid, state in done:
                logger.info("submission %d -> %s", sid, state)
            if args.once:
                print(json.dumps([{"submission_id": i, "state": st} for i, st in done], indent=2))
                return 0
        except Exception:
            logger.exception("worker cycle failed; retrying")
            if args.once:
                return 1
        time.sleep(s.poll_interval_s)


if __name__ == "__main__":
    sys.exit(main())
