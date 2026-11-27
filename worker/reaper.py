"""Delete solution zips that will never be gated.

Hippius accepts S3 lifecycle rules but doesn't apply them, so the worker
cleans up itself:

* an upload authorized more than ORPHAN_AFTER ago whose key no submission
  references was never committed (or its commitment was refused);
* a commitment refused for a permanent reason (bad proof, wrong hashes)
  leaves its upload behind, holding the miner's HF token.

Only the uploader's own objects are deleted: a refused commitment that
points at someone else's key (locator_hotkey_mismatch) never deletes it.
"""

from __future__ import annotations

import logging
from typing import Callable

from chain.commitment import parse, parse_object_key
from chain.watcher import TRANSIENT

logger = logging.getLogger("radiant.reaper")

ORPHAN_AFTER = "48 hours"


def reap(conn, delete: Callable[[str], None]) -> list[str]:
    referenced = {r["object_key"] for r in conn.execute("SELECT object_key FROM submissions").fetchall()}
    referenced |= {r["k"] for r in conn.execute(
        "SELECT stripped_object_key AS k FROM submissions WHERE stripped_object_key IS NOT NULL").fetchall()}
    doomed: set[str] = set()
    for r in conn.execute(f"""SELECT DISTINCT object_key FROM upload_authorizations
                             WHERE created_at < now() - interval '{ORPHAN_AFTER}'""").fetchall():
        doomed.add(r["object_key"])
    for r in conn.execute("SELECT hotkey, raw, reason FROM intake_rejections").fetchall():
        if r["reason"] in TRANSIENT:
            continue
        p = parse(r["raw"])
        loc = parse_object_key(p["object_key"]) if p else None
        if loc and loc["hotkey"] == r["hotkey"]:  # the miner's own upload only
            doomed.add(p["object_key"])
    deleted = []
    for key in sorted(doomed - referenced):
        try:
            delete(key)
            deleted.append(key)
        except Exception:
            logger.exception("could not delete %s", key)
    with conn.transaction():
        for key in deleted:
            conn.execute("DELETE FROM upload_authorizations WHERE object_key = %s", (key,))
    if deleted:
        logger.info("reaped %d unreferenced uploads", len(deleted))
    return deleted
