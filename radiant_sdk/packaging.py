"""``prepare``: a clean solution directory to a deterministic zip.

Only the files a solution may contain are taken (radiant_solution.json, .env,
requirements.lock, model.py, README.md, LICENSE, and src/ text files);
everything else is listed as skipped. The zip is byte-for-byte reproducible:
entries sorted, fixed timestamps and permissions, one compression level, so
the same tree always gives the same sha256.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path

from . import reasons as R
from .checks import REQUIRED_FILES, SRC_SUFFIXES, TOP_LEVEL_OPTIONAL, code_sha256, parse_env

FIXED_TIME = (1980, 1, 1, 0, 0, 0)
SKIP_DIRS = {"__pycache__", ".git", ".radiant", ".venv", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


@dataclass(frozen=True)
class Prepared:
    zip_path: str
    sha256: str
    code_sha256: str
    files: list[str]
    skipped: list[str]


def collect(src: Path) -> tuple[dict[str, bytes], list[str]]:
    src = src.resolve()
    files: dict[str, bytes] = {}
    skipped: list[str] = []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(src).parts):
            continue
        if path.is_symlink():
            raise R.SolutionRejected(R.SYMLINK, rel)
        if not path.is_file():
            continue
        top_ok = "/" not in rel and (rel in REQUIRED_FILES or rel in TOP_LEVEL_OPTIONAL)
        src_ok = rel.startswith("src/") and rel.lower().endswith(SRC_SUFFIXES)
        if top_ok or src_ok:
            files[rel] = path.read_bytes()
        else:
            skipped.append(rel)
    for required in REQUIRED_FILES:
        if required not in files:
            hint = " (copy .env.template to .env)" if required == ".env" else ""
            raise R.SolutionRejected(R.MISSING_FILE, required + hint)
    parse_env(files[".env"].decode("utf-8"))  # refuse stray keys before anything is zipped
    return files, skipped


def deterministic_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zf.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buf.getvalue()


def prepare(src: Path, out: Path) -> Prepared:
    import hashlib

    files, skipped = collect(src)
    data = deterministic_zip(files)
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / "solution.zip"
    zip_path.write_bytes(data)
    result = Prepared(str(zip_path), "sha256:" + hashlib.sha256(data).hexdigest(), code_sha256(files),
                      sorted(files), skipped)
    (out / "prepare.json").write_text(json.dumps(asdict(result), indent=2) + "\n")
    return result
