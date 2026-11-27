"""Public API behind Caddy.

R1 routes:
    GET /health                    database reachable, migrations applied
    GET /v1/campaigns              every non-draft campaign (drafts with ?include_drafts=1)
    GET /v1/campaigns/{id}         manifest, hash, fee history, reveal policy

The API container holds only Postgres and bucket credentials: no Modal,
Hugging Face, DataHive or wallet keys. Live data is served with no-store.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Response

from campaign import store
from db.connection import connect

app = FastAPI(title="RADiant API", version="0.1.0", docs_url=None, redoc_url=None)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    history = row["submission_fee_history"]
    return {
        "campaign_id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "manifest_hash": row["manifest_hash"],
        "manifest": row["manifest"],
        "labels": row["manifest"]["labels"],
        "allowed_gpus": row["manifest"]["hardware"]["allowed_gpus"],
        "submission_fee": {k: history[-1][k] for k in ("amount_tao", "recipient")},
        "submission_fee_history": history,
        "visibility": row["visibility"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@app.get("/health")
def health(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        with connect() as conn:
            n = conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"]
    except Exception as exc:
        raise HTTPException(503, f"database unavailable: {type(exc).__name__}") from None
    return {"ok": True, "migrations": n}


@app.get("/v1/campaigns")
def campaigns(response: Response, include_drafts: bool = False) -> list[dict[str, Any]]:
    response.headers["Cache-Control"] = "no-store"
    with connect() as conn:
        rows = store.list_campaigns(conn)
    return [_public(r) for r in rows if include_drafts or r["status"] != "draft"]


@app.get("/v1/campaigns/{campaign_id}")
def campaign(campaign_id: str, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    with connect() as conn:
        row = store.get_campaign(conn, campaign_id)
    if row is None:
        raise HTTPException(404, "no such campaign")
    return _public(row)

