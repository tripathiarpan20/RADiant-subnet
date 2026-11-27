"""Static checks on a solution zip, shared by the SDK and the validator.

These are gates g1 (size), g2 (archive safety), g3 (contract) and the static
half of g5 (.env), plus the descriptor and lockfile rules. Nothing here
imports or runs miner code: the contract check reads the AST.

    report = run_static_checks(zip_bytes, manifest)   # raises SolutionRejected

``manifest`` is a campaign's normalised pin set (what /v1/campaigns/{id}
returns as ``manifest``).
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from . import reasons as R
from .models.v1 import REQUIRED_METHODS

MAX_FILES = 200
REQUIRED_FILES = ("radiant_solution.json", ".env", "requirements.lock", "model.py")
TOP_LEVEL_OPTIONAL = ("README.md", "LICENSE")
SRC_SUFFIXES = (".py", ".json", ".yaml", ".yml", ".txt", ".md")
ARCHIVE_SUFFIXES = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".whl", ".egg", ".jar")
ARCHIVE_MAGIC = (b"PK\x03\x04", b"\x1f\x8b", b"BZh", b"\xfd7zXZ", b"7z\xbc\xaf", b"Rar!")
ENV_KEYS = ("HF_REPO_ID", "HF_REVISION", "HF_TOKEN")
HF_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
LOCK_REQ_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9,._-]+\])?==([A-Za-z0-9.+!_-]+)$")
LOCK_HASH_RE = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")

# Network and process modules may not be imported by solution code.
BANNED_MODULES = frozenset({
    "socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "ftplib", "smtplib",
    "poplib", "imaplib", "telnetlib", "xmlrpc", "websocket", "websockets", "grpc", "paramiko", "asyncio",
    "subprocess", "multiprocessing", "pty", "ctypes", "cffi", "huggingface_hub", "boto3", "botocore", "modal",
})


@dataclass(frozen=True)
class StaticReport:
    sha256: str            # of the zip bytes, what the commitment carries
    code_sha256: str       # over every file except .env, so a stripped reveal stays checkable
    files: dict[str, int]  # path -> size
    descriptor: dict[str, Any]
    env: dict[str, str]


# ── hashing ─────────────────────────────────────────────────────────────────


def code_sha256(files: Mapping[str, bytes]) -> str:
    """sha256 over "path NUL sha256(content) LF" for every file but .env, sorted by path."""
    h = hashlib.sha256()
    for path in sorted(p for p in files if p != ".env"):
        h.update(f"{path}\0{hashlib.sha256(files[path]).hexdigest()}\n".encode())
    return "sha256:" + h.hexdigest()


# ── g1 / g2: the archive ────────────────────────────────────────────────────


def read_zip(data: bytes, *, max_zip_mb: int, max_unzipped_mb: int) -> dict[str, bytes]:
    if len(data) > max_zip_mb * 1_000_000:
        raise R.SolutionRejected(R.ZIP_TOO_LARGE, f"{len(data)} bytes, over {max_zip_mb} MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise R.SolutionRejected(R.ZIP_INVALID, str(exc)) from None
    if len(infos) > MAX_FILES:
        raise R.SolutionRejected(R.TOO_MANY_FILES, f"{len(infos)} entries, over {MAX_FILES}")
    limit = max_unzipped_mb * 1_000_000
    if sum(i.file_size for i in infos) > limit:
        raise R.SolutionRejected(R.UNZIPPED_TOO_LARGE, f"declared size over {max_unzipped_mb} MB")
    out: dict[str, bytes] = {}
    total = 0
    for info in infos:
        name = info.filename
        if info.is_dir():
            continue
        p = PurePosixPath(name)
        if name.startswith("/") or "\\" in name or ".." in p.parts or not p.parts or ":" in p.parts[0]:
            raise R.SolutionRejected(R.PATH_ESCAPE, name)
        if stat.S_ISLNK(info.external_attr >> 16):
            raise R.SolutionRejected(R.SYMLINK, name)
        if name in out:
            raise R.SolutionRejected(R.ZIP_INVALID, f"duplicate entry {name}")
        with zf.open(info) as f:
            content = f.read(limit - total + 1)  # never trust the declared size
        total += len(content)
        if total > limit:
            raise R.SolutionRejected(R.UNZIPPED_TOO_LARGE, f"over {max_unzipped_mb} MB once decompressed")
        out[name] = content
    return out


def check_layout(files: Mapping[str, bytes]) -> None:
    for name, content in files.items():
        lower = name.lower()
        if lower.endswith(ARCHIVE_SUFFIXES) or content.startswith(ARCHIVE_MAGIC):
            raise R.SolutionRejected(R.NESTED_ARCHIVE, name)
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise R.SolutionRejected(R.OPAQUE_BINARY, f"{name} is not UTF-8 text (weights belong on Hugging Face)") \
                from None
        if b"\x00" in content:
            raise R.SolutionRejected(R.OPAQUE_BINARY, f"{name} contains NUL bytes")
        top = name.split("/", 1)[0]
        if "/" not in name:
            if name not in REQUIRED_FILES and name not in TOP_LEVEL_OPTIONAL:
                raise R.SolutionRejected(R.UNEXPECTED_FILE, name)
        elif top != "src" or not lower.endswith(SRC_SUFFIXES):
            raise R.SolutionRejected(R.UNEXPECTED_FILE, f"{name} (extra files go under src/ as {', '.join(SRC_SUFFIXES)})")
    for required in REQUIRED_FILES:
        if required not in files:
            raise R.SolutionRejected(R.MISSING_FILE, required)


# ── descriptor, .env, lockfile ──────────────────────────────────────────────

DESCRIPTOR_KEYS = {"base_class", "sdk_version", "gpu", "cpus", "batch_size"}


def check_descriptor(text: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    try:
        d = json.loads(text)
    except json.JSONDecodeError as exc:
        raise R.SolutionRejected(R.DESCRIPTOR_INVALID, f"radiant_solution.json: {exc}") from None
    if not isinstance(d, dict) or set(d) != DESCRIPTOR_KEYS:
        raise R.SolutionRejected(R.DESCRIPTOR_INVALID, f"radiant_solution.json must have exactly {sorted(DESCRIPTOR_KEYS)}")
    sol, hw, harness = manifest["solution"], manifest["hardware"], manifest["harness"]
    if d["base_class"] != sol["base_class"]:
        raise R.SolutionRejected(R.BASE_CLASS_MISMATCH, f"{d['base_class']!r}, campaign pins {sol['base_class']!r}")
    if d["sdk_version"] != sol["sdk_version"]:
        raise R.SolutionRejected(R.SDK_VERSION_MISMATCH, f"{d['sdk_version']!r}, campaign pins {sol['sdk_version']!r}")
    if d["gpu"] not in hw["allowed_gpus"]:
        raise R.SolutionRejected(R.GPU_NOT_ALLOWED, f"{d['gpu']!r}, campaign allows {hw['allowed_gpus']}")
    cpus = d["cpus"]
    if isinstance(cpus, bool) or not isinstance(cpus, int) or not 1 <= cpus <= hw["max_cpus"]:
        raise R.SolutionRejected(R.CPUS_OVER_LIMIT, f"cpus must be an integer from 1 to {hw['max_cpus']}")
    bs = d["batch_size"]
    if isinstance(bs, bool) or not isinstance(bs, int) or not 1 <= bs <= harness["max_batch_size"]:
        raise R.SolutionRejected(R.BATCH_SIZE_INVALID, f"batch_size must be an integer from 1 to {harness['max_batch_size']}")
    return d


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise R.SolutionRejected(R.ENV_INVALID, f".env line {n} is not KEY=value")
        key, value = (s.strip() for s in line.split("=", 1))
        if key.startswith("export "):
            raise R.SolutionRejected(R.ENV_INVALID, f".env line {n}: no 'export'")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key not in ENV_KEYS:
            raise R.SolutionRejected(R.ENV_UNKNOWN_KEY, f"{key} (only {', '.join(ENV_KEYS)} are allowed)")
        if key in out:
            raise R.SolutionRejected(R.ENV_INVALID, f"{key} appears twice")
        out[key] = value
    if not HF_REPO_RE.match(out.get("HF_REPO_ID", "")):
        raise R.SolutionRejected(R.ENV_BAD_REPO, "HF_REPO_ID must be owner/name")
    if not SHA40_RE.match(out.get("HF_REVISION", "")):
        raise R.SolutionRejected(R.ENV_BAD_REVISION, "HF_REVISION must be a full 40-character commit sha")
    out.setdefault("HF_TOKEN", "")  # optional: only public repos may omit it
    return out


def check_lock(text: str) -> list[str]:
    """Every requirement pinned with ==, every one hashed, no indexes, URLs, paths or includes."""
    logical, buf = [], ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        if buf.strip():
            logical.append(buf.strip())
        buf = ""
    pins = []
    for entry in logical:
        req_part = entry.split("--hash=", 1)[0].strip()
        options = re.findall(r"(?:^|\s)(-{1,2}[A-Za-z][\w-]*)", entry)
        if req_part.startswith("-") or any(o != "--hash" for o in options):
            raise R.SolutionRejected(R.LOCK_FORBIDDEN_OPTION, f"requirements.lock: {entry[:80]} (only name==version lines)")
        if "://" in req_part or "@" in req_part or "/" in req_part.split(";", 1)[0]:
            raise R.SolutionRejected(R.LOCK_FORBIDDEN_OPTION, f"requirements.lock: {req_part} (no URLs or paths)")
        name = req_part.split(";", 1)[0].strip()
        if not LOCK_REQ_RE.match(name):
            raise R.SolutionRejected(R.LOCK_UNPINNED, f"requirements.lock: {name} must be name==version")
        hashes = re.findall(r"--hash=(\S+)", entry)
        if not hashes or not all(LOCK_HASH_RE.match("--hash=" + h) for h in hashes):
            raise R.SolutionRejected(R.LOCK_MISSING_HASH, f"requirements.lock: {name} needs --hash=sha256:<64 hex>")
        pins.append(name)
    return pins


# ── g3: the contract, by AST ────────────────────────────────────────────────


def _module_root(name: str) -> str:
    return name.split(".", 1)[0]


def _banned_imports(tree: ast.AST, path: str) -> None:
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif isinstance(node, ast.Call):
            fn = node.func
            is_dyn = (isinstance(fn, ast.Name) and fn.id == "__import__") or \
                     (isinstance(fn, ast.Attribute) and fn.attr == "import_module")
            if is_dyn and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names = [node.args[0].value]
        for n in names:
            if _module_root(n) in BANNED_MODULES:
                raise R.SolutionRejected(R.BANNED_IMPORT, f"{path}:{node.lineno} imports {n}")


def _base_aliases(tree: ast.Module, module: str, cls: str) -> tuple[set[str], set[str]]:
    """Names bound to the base class, and names bound to its module."""
    direct, modules = set(), set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == module and node.level == 0:
            direct |= {a.asname or a.name for a in node.names if a.name == cls}
        elif isinstance(node, ast.Import):
            modules |= {a.asname for a in node.names if a.name == module and a.asname}
    return direct, modules


def check_contract(files: Mapping[str, bytes], base_class: str) -> str:
    """Return the name of the one subclass in model.py that overrides every required method."""
    trees = {}
    for path, content in files.items():
        if path.endswith(".py"):
            try:
                trees[path] = ast.parse(content.decode("utf-8"), filename=path)
            except SyntaxError as exc:
                raise R.SolutionRejected(R.SYNTAX_ERROR, f"{path}:{exc.lineno}: {exc.msg}") from None
    for path, tree in sorted(trees.items()):
        _banned_imports(tree, path)

    module, cls = base_class.split(":")
    tree = trees["model.py"]
    direct, modules = _base_aliases(tree, module, cls)
    candidates = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if (isinstance(base, ast.Name) and base.id in direct) or (
                    isinstance(base, ast.Attribute) and base.attr == cls
                    and isinstance(base.value, ast.Name) and base.value.id in modules):
                candidates.append(node)
                break
    if not candidates:
        raise R.SolutionRejected(R.CONTRACT_NO_SUBCLASS, f"model.py defines no subclass of {base_class}")
    if len(candidates) > 1:
        raise R.SolutionRejected(R.CONTRACT_MULTIPLE_SUBCLASSES, ", ".join(c.name for c in candidates))
    node = candidates[0]
    defined = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
    missing = [m for m in REQUIRED_METHODS if m not in defined]
    if missing:
        raise R.SolutionRejected(R.CONTRACT_MISSING_OVERRIDE, f"{node.name} does not override {', '.join(missing)}")
    return node.name


# ── everything static, in gate order ────────────────────────────────────────


def run_static_checks(zip_bytes: bytes, manifest: Mapping[str, Any]) -> StaticReport:
    limits = manifest["limits"]
    files = read_zip(zip_bytes, max_zip_mb=limits["max_solution_zip_mb"],
                     max_unzipped_mb=limits["max_solution_unzipped_mb"])
    check_layout(files)
    descriptor = check_descriptor(files["radiant_solution.json"].decode(), manifest)
    env = parse_env(files[".env"].decode())
    check_lock(files["requirements.lock"].decode())
    check_contract(files, manifest["solution"]["base_class"])
    return StaticReport(
        sha256="sha256:" + hashlib.sha256(zip_bytes).hexdigest(),
        code_sha256=code_sha256(files),
        files={p: len(c) for p, c in sorted(files.items())},
        descriptor=descriptor,
        env=env,
    )
