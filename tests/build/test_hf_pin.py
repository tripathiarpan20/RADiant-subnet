"""Unit tests for build/hf_pin.py: full file hashing, LFS check, and orphan cleanup."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from build.hf_pin import pin, pin_repo_name

HOTKEY = "5GZ9S5qgBrHCMr3kdvQ8hXdpZqz9D4eW7xvYxpRQHoVUuGKi"


@dataclass
class FakeSibling:
    rfilename: str
    lfs: SimpleNamespace | None = None


def test_pin_records_all_file_hashes_and_verifies_lfs(tmp_path, monkeypatch):
    (tmp_path / "model.safetensors").write_bytes(b"weights-bytes")
    (tmp_path / "config.json").write_text('{"a": 1}\n')
    w_hash = hashlib.sha256(b"weights-bytes").hexdigest()
    c_hash = hashlib.sha256(b'{"a": 1}\n').hexdigest()

    deleted = []

    class FakeHfApi:
        def __init__(self, token=None):
            pass

        def repo_exists(self, repo, repo_type="model"):
            return False

        def create_repo(self, repo, **kw):
            assert kw["private"] is True

        def upload_folder(self, **kw):
            return SimpleNamespace(oid="b" * 40)

        def model_info(self, repo, **kw):
            return SimpleNamespace(
                private=True,
                siblings=[
                    FakeSibling("model.safetensors", SimpleNamespace(sha256=w_hash)),
                    FakeSibling("config.json", None),
                ],
            )

        def delete_repo(self, repo, **kw):
            deleted.append(repo)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeHfApi)
    res = pin(tmp_path, org="radiant-bittensor", token="hf_write",
              campaign_id="testnet-cxr-01", hotkey=HOTKEY, submission_id=7)
    assert res.repo == f"radiant-bittensor/{pin_repo_name('testnet-cxr-01', HOTKEY, 7)}"
    assert res.files == {"model.safetensors": w_hash, "config.json": c_hash}
    assert deleted == []


def test_pin_deletes_newly_created_orphan_on_upload_or_hash_failure(tmp_path, monkeypatch):
    (tmp_path / "model.safetensors").write_bytes(b"weights-bytes")
    deleted = []

    class FailingHfApi:
        def __init__(self, token=None):
            pass

        def repo_exists(self, repo, repo_type="model"):
            return False

        def create_repo(self, repo, **kw):
            pass

        def upload_folder(self, **kw):
            raise RuntimeError("upload boom")

        def delete_repo(self, repo, **kw):
            deleted.append(repo)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FailingHfApi)
    with pytest.raises(RuntimeError, match="upload boom"):
        pin(tmp_path, org="radiant-bittensor", token="hf_write",
            campaign_id="testnet-cxr-01", hotkey=HOTKEY, submission_id=8)
    assert len(deleted) == 1 and deleted[0].endswith("-s8")
