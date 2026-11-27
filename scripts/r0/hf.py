"""Private Hugging Face download with a miner token, pin upload, org quota.

Mirrors gates g5 and g9 (see the build plan's Weight pinning section):

hf_private_download  the miner's private repo is unreadable anonymously, and
                     the miner's token downloads it at a pinned revision with
                     every LFS file matching its recorded sha256
hf_token_scope       the miner token is fine-grained and has no write scope
hf_pin_upload        the validator's write token creates a private repo in
                     HF_PIN_ORG named by the pin rule, uploads the same files,
                     and the repo is unreadable anonymously; deleted afterwards
hf_org_quota         the pin org's private storage use and limit

Environment:
    HF_MINER_REPO       a private model repo, e.g. someuser/rad-r0-miner
    HF_MINER_REVISION   optional commit sha (default: resolve main)
    HF_MINER_TOKEN      fine-grained read token scoped to that repo
    HF_PIN_ORG          validator org that holds pins
    HF_PIN_TOKEN        write token for HF_PIN_ORG (only the pin gate uses it)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .report import GateResult, GateStatus

HF_REPO_NAME_MAX = 96
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$")


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--keep", action="store_true", help="keep the probe pin repo")


def pin_repo_name(campaign_id: str, hotkey: str, submission_id: int) -> str:
    """rad-<campaign_id>-<hotkey>-s<submission_id>, within Hugging Face's rules."""
    if not isinstance(submission_id, int) or submission_id < 0:
        raise ValueError("submission_id must be a non-negative int")
    if len(campaign_id) > 24:
        raise ValueError("campaign_id is capped at 24 characters")
    name = f"rad-{campaign_id}-{hotkey}-s{submission_id}"
    if len(name) > HF_REPO_NAME_MAX:
        raise ValueError(f"pin name is {len(name)} characters, over {HF_REPO_NAME_MAX}")
    if "--" in name or ".." in name or not _NAME_OK.match(name):
        raise ValueError(f"pin name {name!r} breaks Hugging Face naming rules")
    return name


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def anonymous_can_read(api, repo_id: str) -> bool:
    from huggingface_hub.utils import HfHubHTTPError

    try:
        api.model_info(repo_id, token=False)
        return True
    except HfHubHTTPError:
        return False


def token_scope(api, token: str) -> dict[str, Any]:
    who = api.whoami(token=token)
    auth = (who.get("auth") or {}).get("accessToken") or {}
    fine = auth.get("fineGrained") or {}
    perms = sorted({p for s in fine.get("scoped", []) for p in s.get("permissions", [])})
    return {"role": auth.get("role"), "permissions": perms,
            "scoped_entities": [s.get("entity", {}).get("name") for s in fine.get("scoped", [])]}


def run(args: argparse.Namespace) -> list[GateResult]:
    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError:
        return [GateResult("hf", GateStatus.BLOCKED, "huggingface_hub not installed (uv sync --extra r0)")]

    repo = os.getenv("HF_MINER_REPO", "")
    miner_token = os.getenv("HF_MINER_TOKEN", "")
    org = os.getenv("HF_PIN_ORG", "")
    pin_token = os.getenv("HF_PIN_TOKEN", "")
    api = HfApi()
    results: list[GateResult] = []

    # g5: private download with the miner token
    files: dict[str, str] = {}
    local: Path | None = None
    if not (repo and miner_token):
        results.append(GateResult("hf_private_download", GateStatus.BLOCKED, "HF_MINER_REPO / HF_MINER_TOKEN not set"))
        results.append(GateResult("hf_token_scope", GateStatus.BLOCKED, "HF_MINER_TOKEN not set"))
    else:
        try:
            public = anonymous_can_read(api, repo)
            info = api.model_info(repo, revision=os.getenv("HF_MINER_REVISION") or None,
                                  files_metadata=True, token=miner_token)
            expected = {s.rfilename: s.lfs.sha256 for s in info.siblings if getattr(s, "lfs", None)}
            local = Path(tempfile.mkdtemp(prefix="r0-hf-"))
            snapshot_download(repo, revision=info.sha, token=miner_token, local_dir=local)
            for path in sorted(p for p in local.rglob("*") if p.is_file() and ".cache" not in p.parts):
                files[str(path.relative_to(local))] = sha256_file(path)
            mismatched = [f for f, h in expected.items() if files.get(f) != h]
            ok = not public and files and not mismatched
            results.append(GateResult(
                "hf_private_download", GateStatus.PASSED if ok else GateStatus.FAILED,
                f"{len(files)} files at {info.sha[:12]}; anonymous read {'allowed' if public else 'refused'}; "
                f"{len(mismatched)} LFS hash mismatches",
                {"repo": repo, "revision": info.sha, "files": len(files),
                 "bytes": sum(p.stat().st_size for p in local.rglob('*') if p.is_file()),
                 "anonymous_read": public, "lfs_mismatches": mismatched}))
        except Exception as exc:
            results.append(GateResult("hf_private_download", GateStatus.FAILED, f"{type(exc).__name__}: {str(exc)[:300]}"))
        try:
            scope = token_scope(api, miner_token)
            writes = [p for p in scope["permissions"] if "write" in p]
            ok = scope["role"] == "fineGrained" and not writes
            results.append(GateResult("hf_token_scope", GateStatus.PASSED if ok else GateStatus.FAILED,
                                      f"role {scope['role']}, write permissions: {writes or 'none'}", scope))
        except Exception as exc:
            results.append(GateResult("hf_token_scope", GateStatus.FAILED, f"{type(exc).__name__}: {str(exc)[:300]}"))

    # g9: pin into the validator org
    if not (org and pin_token):
        results.append(GateResult("hf_pin_upload", GateStatus.BLOCKED, "HF_PIN_ORG / HF_PIN_TOKEN not set"))
        results.append(GateResult("hf_org_quota", GateStatus.BLOCKED, "HF_PIN_ORG / HF_PIN_TOKEN not set"))
    elif local is None or not files:
        results.append(GateResult("hf_pin_upload", GateStatus.BLOCKED, "nothing downloaded to pin"))
    else:
        name = pin_repo_name("r0probe", uuid.uuid4().hex, 0)
        pin_id = f"{org}/{name}"
        created = False
        try:
            api.create_repo(pin_id, private=True, repo_type="model", token=pin_token)
            created = True
            api.upload_folder(repo_id=pin_id, folder_path=str(local), token=pin_token,
                              ignore_patterns=[".cache/*"], commit_message="R0 pin probe")
            info = api.model_info(pin_id, files_metadata=True, token=pin_token)
            pinned = {s.rfilename: s.lfs.sha256 for s in info.siblings if getattr(s, "lfs", None)}
            mismatched = [f for f, h in pinned.items() if files.get(f) != h]
            public = anonymous_can_read(api, pin_id)
            ok = info.private and not public and not mismatched
            results.append(GateResult(
                "hf_pin_upload", GateStatus.PASSED if ok else GateStatus.FAILED,
                f"pinned {len(info.siblings)} files to a private repo; anonymous read "
                f"{'allowed' if public else 'refused'}; {len(mismatched)} hash mismatches",
                {"pin_repo": pin_id, "name_length": len(name), "private": info.private,
                 "anonymous_read": public, "revision": info.sha}))
        except Exception as exc:
            results.append(GateResult("hf_pin_upload", GateStatus.FAILED, f"{type(exc).__name__}: {str(exc)[:300]}"))
        finally:
            if created and not args.keep:
                try:
                    api.delete_repo(pin_id, token=pin_token)
                except Exception:
                    pass

    if org and pin_token:
        results.append(_quota(org, pin_token))
    if local is not None:
        shutil.rmtree(local, ignore_errors=True)
    return results


def _quota(org: str, token: str) -> GateResult:
    """Hugging Face has no documented quota endpoint; try the known candidates."""
    import requests

    tried = {}
    for path in (f"/api/organizations/{org}/storage", f"/api/organizations/{org}/overview"):
        try:
            r = requests.get(f"https://huggingface.co{path}", headers={"Authorization": f"Bearer {token}"}, timeout=20)
            tried[path] = r.status_code
            if r.ok:
                body = r.json()
                used = body.get("usedStorage") or body.get("storageUsed") or body.get("privateStorageUsed")
                limit = body.get("storageLimit") or body.get("privateStorageLimit")
                if used is not None and limit:
                    return GateResult("hf_org_quota", GateStatus.PASSED,
                                      f"{used / 1e9:.1f} GB of {limit / 1e9:.0f} GB private storage used",
                                      {"org": org, "used_bytes": used, "limit_bytes": limit})
        except Exception as exc:
            tried[path] = type(exc).__name__
    return GateResult("hf_org_quota", GateStatus.PENDING,
                      f"no quota API answered; read it at https://huggingface.co/organizations/{org}/settings/billing",
                      {"org": org, "tried": tried})
