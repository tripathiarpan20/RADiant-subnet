"""Watcher service: scan commitments every RADIANT_POLL_INTERVAL_S and queue submissions.

    python -m worker.watcher            # loop
    python -m worker.watcher --once     # one scan, print the results

Needs chain access (bittensor) and bucket credentials. A failed scan is
logged and retried next cycle; nothing is lost, because commitments stay on
chain and ingestion is idempotent.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from functools import partial

import config
from db.connection import connect

logger = logging.getLogger("radiant.watcher")


def scan_once(s: config.Settings) -> list[dict]:
    import bittensor as bt

    from chain.payment import fetch_block_payment_view
    from chain.rpc import fetch_chain_view
    from chain.watcher import scan
    from storage import s3

    subtensor = bt.Subtensor(network=s.network)
    meta, revealed, block, _ = fetch_chain_view(subtensor, s.netuid, network=s.network, attempts=2, delay_s=5)

    def fetch_object(key: str, max_bytes: int) -> bytes | None:
        size = s3.head_size(s, key)
        if size is None or size > max_bytes:
            return None
        return s3.fetch_bounded(s, key, max_bytes)

    with connect(s.require_database(), autocommit=True) as conn:
        results = scan(conn, meta, revealed, fetch_block=partial(fetch_block_payment_view, subtensor),
                       fetch_object=fetch_object, s=s)
    out = [{"submission_id": r.submission_id, "reason": r.reason} for r in results]
    logger.info("block %d: %d commitments, %d ingested", block, len(results),
                sum(r.reason == "ingested" for r in results))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m worker.watcher")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = config.settings()
    if args.once:
        print(json.dumps(scan_once(s), indent=2))
        return 0
    while True:
        try:
            scan_once(s)
        except Exception:
            logger.exception("scan failed; retrying next cycle")
        time.sleep(s.poll_interval_s)


if __name__ == "__main__":
    sys.exit(main())
