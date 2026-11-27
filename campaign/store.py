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
