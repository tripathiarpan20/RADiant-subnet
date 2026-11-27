"""Campaign rows in Postgres. All writes go through here or the seed CLI."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from .fees import append_fee as _append_fee
from .fees import genesis_history
from .manifest import CampaignManifest, compute_manifest_hash

COLUMNS = "id, title, status, manifest_version, manifest, manifest_hash, submission_fee_history, visibility, " \
          "created_at, updated_at"


def get_campaign(conn, campaign_id: str) -> dict[str, Any] | None:
    return conn.execute(f"SELECT {COLUMNS} FROM campaigns WHERE id = %s", (campaign_id,)).fetchone()


def list_campaigns(conn, status: str | None = None) -> list[dict[str, Any]]:
    if status:
        return conn.execute(f"SELECT {COLUMNS} FROM campaigns WHERE status = %s ORDER BY created_at", (status,)).fetchall()
    return conn.execute(f"SELECT {COLUMNS} FROM campaigns ORDER BY created_at").fetchall()


def insert_campaign(conn, m: CampaignManifest, status: str = "draft") -> dict[str, Any]:
    return conn.execute(
        f"""INSERT INTO campaigns (id, title, status, manifest_version, manifest, manifest_hash,
                                   submission_fee_history, visibility)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}""",
        (m.campaign_id, m.title, status, m.pinned["manifest_version"], Jsonb(m.pinned), m.manifest_hash,
         Jsonb(genesis_history(m.submission_fee)), Jsonb(m.visibility)),
    ).fetchone()


def replace_draft_manifest(conn, m: CampaignManifest) -> dict[str, Any]:
    """Only a draft's manifest can change; the database trigger enforces it too."""
    row = conn.execute(
        f"""UPDATE campaigns SET manifest = %s, manifest_hash = %s, title = %s, visibility = %s
            WHERE id = %s AND status = 'draft' RETURNING {COLUMNS}""",
        (Jsonb(m.pinned), m.manifest_hash, m.title, Jsonb(m.visibility), m.campaign_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"campaign {m.campaign_id} is not a draft; its manifest is frozen")
    return row


def set_status(conn, campaign_id: str, status: str) -> dict[str, Any]:
    row = conn.execute(f"UPDATE campaigns SET status = %s WHERE id = %s RETURNING {COLUMNS}",
                       (status, campaign_id)).fetchone()
    if row is None:
        raise ValueError(f"no campaign {campaign_id}")
    return row


def append_fee(conn, campaign_id: str, fee: dict[str, Any], effective_from_block: int) -> dict[str, Any]:
    row = conn.execute("SELECT submission_fee_history FROM campaigns WHERE id = %s FOR UPDATE",
                       (campaign_id,)).fetchone()
    if row is None:
        raise ValueError(f"no campaign {campaign_id}")
    history = _append_fee(row["submission_fee_history"], fee, effective_from_block)
    return conn.execute(f"UPDATE campaigns SET submission_fee_history = %s WHERE id = %s RETURNING {COLUMNS}",
                        (Jsonb(history), campaign_id)).fetchone()


def stored_hash_matches(row: dict[str, Any]) -> bool:
    """Recompute the hash from the stored pin set: the audit check for a row."""
    return compute_manifest_hash(row["manifest"]) == row["manifest_hash"]


# ── submissions (R3) ────────────────────────────────────────────────────────

SUBMISSION_COLUMNS = ("id, campaign_id, hotkey, coldkey, uid, sha256, code_sha256, object_key, size_bytes, "
                      "commit_block, payment_block, payment_tx, fee_exempt, state, created_at, updated_at")


def get_submission(conn, campaign_id: str, sha256: str) -> dict[str, Any] | None:
    return conn.execute(f"SELECT {SUBMISSION_COLUMNS} FROM submissions WHERE campaign_id = %s AND sha256 = %s",
                        (campaign_id, sha256)).fetchone()


def find_submissions(conn, sha256: str) -> list[dict[str, Any]]:
    return conn.execute(f"SELECT {SUBMISSION_COLUMNS} FROM submissions WHERE sha256 = %s ORDER BY id",
                        (sha256,)).fetchall()


def payment_ref_used(conn, block: int, tx: int) -> bool:
    return conn.execute("SELECT 1 FROM submissions WHERE payment_block = %s AND payment_tx = %s",
                        (block, tx)).fetchone() is not None


def insert_submission(conn, com, *, size_bytes: int, fee_exempt: bool) -> int | None:
    """Insert as 'pending' (waiting for the gates) and log it. None if the slot or the fee proof is taken."""
    row = conn.execute(
        """INSERT INTO submissions (campaign_id, hotkey, coldkey, uid, sha256, code_sha256, object_key, size_bytes,
                                    commit_block, payment_block, payment_tx, fee_exempt)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT DO NOTHING RETURNING id""",
        (com.campaign_id, com.hotkey, com.coldkey, com.uid, com.sha256, com.code_sha256, com.object_key, size_bytes,
         com.commit_block, com.payment_block, com.payment_tx, fee_exempt)).fetchone()
    if row is None:
        return None
    conn.execute("INSERT INTO submission_events (submission_id, state, detail) VALUES (%s, 'pending', %s)",
                 (row["id"], Jsonb({"commit_block": com.commit_block, "payment_block": com.payment_block,
                                    "payment_tx": com.payment_tx, "fee_exempt": fee_exempt})))
    return row["id"]


def record_rejection(conn, com, reason: str) -> None:
    conn.execute(
        """INSERT INTO intake_rejections (hotkey, commit_block, raw, campaign_id, sha256, reason)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (hotkey, commit_block, raw) DO UPDATE SET reason = EXCLUDED.reason, last_seen = now()""",
        (com.hotkey, com.commit_block, com.raw, com.campaign_id, com.sha256, reason))


def rejection_reason(conn, com) -> str | None:
    row = conn.execute("SELECT reason FROM intake_rejections WHERE hotkey = %s AND commit_block = %s AND raw = %s",
                       (com.hotkey, com.commit_block, com.raw)).fetchone()
    return row["reason"] if row else None


def rejections_for(conn, sha256: str) -> list[dict[str, Any]]:
    return conn.execute("SELECT hotkey, commit_block, campaign_id, reason, first_seen, last_seen FROM intake_rejections "
                        "WHERE sha256 = %s ORDER BY first_seen", (sha256,)).fetchall()


def submission_events(conn, submission_id: int) -> list[dict[str, Any]]:
    return conn.execute("SELECT state, detail, at FROM submission_events WHERE submission_id = %s ORDER BY at, id",
                        (submission_id,)).fetchall()


# ── upload gating (R3 hardening) ────────────────────────────────────────────


def replace_registered_hotkeys(conn, rows: list[tuple[str, int, str]], block: int) -> None:
    """Mirror the metagraph: [(hotkey, uid, coldkey)] as of ``block``, in one transaction."""
    with conn.transaction():
        conn.execute("DELETE FROM registered_hotkeys")
        if rows:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO registered_hotkeys (hotkey, uid, coldkey, synced_block) "
                                "VALUES (%s, %s, %s, %s)", [(h, u, c, block) for h, u, c in rows])


def registration(conn, hotkey: str) -> tuple[bool, bool]:
    """(the metagraph has been synced at least once, this hotkey is registered)."""
    synced = conn.execute("SELECT 1 FROM registered_hotkeys LIMIT 1").fetchone() is not None
    found = conn.execute("SELECT 1 FROM registered_hotkeys WHERE hotkey = %s", (hotkey,)).fetchone() is not None
    return synced, found


def uploads_in_last_hour(conn, hotkey: str) -> int:
    return conn.execute("SELECT count(*) AS n FROM upload_authorizations WHERE hotkey = %s "
                        "AND created_at > now() - interval '1 hour'", (hotkey,)).fetchone()["n"]


def record_upload(conn, *, hotkey: str, campaign_id: str, sha256: str, object_key: str, size_bytes: int) -> None:
    conn.execute("INSERT INTO upload_authorizations (hotkey, campaign_id, sha256, object_key, size_bytes) "
                 "VALUES (%s, %s, %s, %s, %s)", (hotkey, campaign_id, sha256, object_key, size_bytes))
