"""Gate g9: pin a submission's weights into a private repo in the validator's HF org.

The last gate, so only submissions that pass everything are pinned. The
files are the ones g5 downloaded and hash-checked; no token can both read the
miner's private repo and write ours, so this is an upload, not a server-side
copy. Pins are named rad-<campaign_id>-<hotkey>-s<submission_id>, stay
private until reveal (R7), and rebuild an image if Modal ever loses one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HF_REPO_NAME_MAX = 96
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$")


def pin_repo_name(campaign_id: str, hotkey: str, submission_id: int) -> str:
    if isinstance(submission_id, bool) or not isinstance(submission_id, int) or submission_id < 0:
        raise ValueError("submission_id must be a non-negative int")
    if len(campaign_id) > 24:
        raise ValueError("campaign_id is capped at 24 characters")
    name = f"rad-{campaign_id}-{hotkey}-s{submission_id}"
    if len(name) > HF_REPO_NAME_MAX:
        raise ValueError(f"pin name is {len(name)} characters, over {HF_REPO_NAME_MAX}")
    if "--" in name or ".." in name or not _NAME_OK.match(name):
        raise ValueError(f"pin name {name!r} breaks Hugging Face naming rules")
    return name


@dataclass(frozen=True)
class Pinned:
    repo: str
    revision: str
    files: dict[str, str]  # path -> sha256 (LFS) or "" for small files


def pin(weights_dir: Path, *, org: str, token: str, campaign_id: str, hotkey: str, submission_id: int) -> Pinned:
    from huggingface_hub import HfApi

    if not (org and token):
        raise RuntimeError("HF_PIN_ORG and HF_PIN_TOKEN must be set for gate g9")
    api = HfApi(token=token)
    repo = f"{org}/{pin_repo_name(campaign_id, hotkey, submission_id)}"
    api.create_repo(repo, repo_type="model", private=True, exist_ok=True)
    commit = api.upload_folder(repo_id=repo, folder_path=str(weights_dir),
                               commit_message=f"pin submission {submission_id}",
                               ignore_patterns=[".cache/*", ".cache/**"])
    info = api.model_info(repo, revision=commit.oid, files_metadata=True)
    if not info.private:
        raise RuntimeError(f"{repo} is not private")
    files = {s.rfilename: (s.lfs.sha256 if s.lfs else "") for s in info.siblings}
    return Pinned(repo, commit.oid, files)
