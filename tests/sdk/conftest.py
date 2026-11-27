import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from campaign.manifest import load_manifest_file

REPO = Path(__file__).resolve().parents[2]
REFERENCE = REPO / "radiant_sdk" / "reference_miner"


@pytest.fixture
def manifest():
    """The testnet fixture's normalised pin set, as /v1/campaigns/{id} serves it."""
    return load_manifest_file(REPO / "campaign" / "fixtures" / "testnet-cxr-01.json").pinned


@pytest.fixture
def campaign_file(tmp_path, manifest):
    m = load_manifest_file(REPO / "campaign" / "fixtures" / "testnet-cxr-01.json")
    body = {"campaign_id": m.campaign_id, "status": "open", "manifest": m.pinned, "manifest_hash": m.manifest_hash}
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(body))
    return path


@pytest.fixture
def miner_dir(tmp_path):
    """A copy of the reference miner with .env filled from its template."""
    d = tmp_path / "miner"
    shutil.copytree(REFERENCE, d, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(d / ".env.template", d / ".env")
    return d


def zip_of(files: dict[str, bytes | str], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def reference_files(miner_dir: Path) -> dict[str, bytes]:
    names = ("radiant_solution.json", ".env", "requirements.lock", "model.py", "README.md")
    return {n: (miner_dir / n).read_bytes() for n in names}
