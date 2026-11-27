"""Public API behind Caddy.

R3 adds:
    POST /v1/uploads/solution      hotkey-signed request -> presigned PUT
    GET  /v1/submissions/{sha256}  state, events and any intake rejections

R1 routes:
    GET /health                    database reachable, migrations applied
    GET /v1/info                   network, netuid and supported SDK versions
    GET /v1/campaigns              every non-draft campaign (drafts with ?include_drafts=1)
    GET /v1/campaigns/{id}         manifest, hash, fee history, reveal policy

The API container holds only Postgres and bucket credentials: no Modal,
Hugging Face, DataHive or wallet keys. Live data is served with no-store.
"""

from __future__ import annotations

from typing import Any

import re

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

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


@app.get("/v1/info")
def info(response: Response) -> dict[str, Any]:
    """Which chain this validator runs on, so the SDK can refuse to pay on the wrong one (R3)."""
    import config
    from radiant_sdk import SDK_VERSION

    response.headers["Cache-Control"] = "no-store"
    s = config.settings()
    return {"network": s.network, "netuid": s.netuid, "sdk_versions": [SDK_VERSION], "fee_currency":
            "testTAO" if s.network == "test" else "TAO"}


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



# ── uploads (R3) ────────────────────────────────────────────────────────────

class UploadRequest(BaseModel):
    campaign_id: str = Field(pattern=r"^[a-z0-9-]{2,24}$")
    hotkey: str = Field(pattern=r"^5[1-9A-HJ-NP-Za-km-z]{46,47}$")
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    code_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    upload_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    expires_at: int
    network: str
    netuid: int
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")


@app.post("/v1/uploads/solution")
def upload_solution(body: UploadRequest, response: Response) -> dict[str, Any]:
    import config
    from storage import s3
    from storage.upload_auth import verify_upload_request

    response.headers["Cache-Control"] = "no-store"
    s = config.settings()
    fields = body.model_dump(exclude={"signature"})
    try:
        remaining = verify_upload_request(fields, body.signature, s)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    with connect() as conn:
        row = store.get_campaign(conn, body.campaign_id)
    if row is None:
        raise HTTPException(404, "no such campaign")
    if row["status"] != "open":
        raise HTTPException(400, "campaign is not open")
    cap = row["manifest"]["limits"]["max_solution_zip_mb"] * 1_000_000
    if body.size_bytes > cap:
        raise HTTPException(413, f"solution zip is {body.size_bytes} bytes, over the campaign's {cap}")
    try:
        p = s3.presign_upload(s, campaign_id=body.campaign_id, hotkey=body.hotkey, sha256=body.sha256,
                              size_bytes=body.size_bytes, upload_id=body.upload_id,
                              expires_in=min(remaining, s.presign_expires_s))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, "upload storage unavailable") from None
    return {"upload_url": p.upload_url, "object_key": p.object_key, "expires_in": p.expires_in,
            "required_headers": p.required_headers, "already_uploaded": p.already_uploaded}


@app.get("/v1/submissions/{sha256}")
def submission(sha256: str, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", sha256):
        raise HTTPException(400, "expected sha256:<64 hex>")
    with connect() as conn:
        subs = store.find_submissions(conn, sha256)
        out = [{"submission_id": r["id"], "campaign_id": r["campaign_id"], "hotkey": r["hotkey"],
                "state": r["state"], "commit_block": r["commit_block"],
                "payment": None if r["payment_block"] is None else f"{r['payment_block']}-{r['payment_tx']}",
                "events": [{"state": e["state"], "at": e["at"].isoformat()} for e in store.submission_events(conn, r["id"])]}
               for r in subs]
        rejected = [{"hotkey": r["hotkey"], "commit_block": r["commit_block"], "campaign_id": r["campaign_id"],
                     "reason": r["reason"], "last_seen": r["last_seen"].isoformat()}
                    for r in store.rejections_for(conn, sha256)]
    if not out and not rejected:
        raise HTTPException(404, "not seen on chain yet")
    return {"sha256": sha256, "submissions": out, "rejections": rejected}
