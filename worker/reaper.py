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
    rej_by_key: dict[str, list[tuple[str, int, str]]] = {}
    rej_skip: list[tuple[str, int, str]] = []
    for r in conn.execute(
            "SELECT hotkey, commit_block, raw, reason FROM intake_rejections WHERE reaped_at IS NULL").fetchall():
        if r["reason"] in TRANSIENT:
            continue
        ident = (r["hotkey"], r["commit_block"], r["raw"])
        p = parse(r["raw"])
        loc = parse_object_key(p["object_key"]) if p else None
        if loc and loc["hotkey"] == r["hotkey"]:  # the miner's own upload only
            doomed.add(p["object_key"])
            rej_by_key.setdefault(p["object_key"], []).append(ident)
        else:
            # Foreign locator or unparseable raw: nothing to delete, mark done so we don't rescan forever.
            rej_skip.append(ident)
    deleted = []
    reaped_rejections = list(rej_skip)
    for key in sorted(doomed - referenced):
        try:
            delete(key)
            deleted.append(key)
            reaped_rejections.extend(rej_by_key.get(key, ()))
        except Exception:
            logger.exception("could not delete %s", key)
    # Rejections pointing to an already-referenced key won't be deleted; mark them done.
    for key in doomed & referenced:
        reaped_rejections.extend(rej_by_key.get(key, ()))
    with conn.transaction():
        for key in deleted:
            # Keep authorizations under 1 hour old so hourly rate limiting isn't reset.
            conn.execute(
                "DELETE FROM upload_authorizations WHERE object_key = %s "
                "AND created_at < now() - interval '1 hour'", (key,))
        for hotkey, commit_block, raw in reaped_rejections:
            conn.execute(
                "UPDATE intake_rejections SET reaped_at = now() "
                "WHERE hotkey = %s AND commit_block = %s AND raw = %s",
                (hotkey, commit_block, raw))
    if deleted:
        logger.info("reaped %d unreferenced uploads", len(deleted))
    return deleted
