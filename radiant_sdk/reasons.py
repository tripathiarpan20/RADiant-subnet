"""Stable reason codes, shared by the SDK and the validator's gates.

A miner sees the same code from ``radiant-sdk validate`` as from the
validator, because both call the same checks. Codes never change meaning;
add new ones instead.
"""

from __future__ import annotations


class SolutionRejected(Exception):
    """A solution failed a check. ``code`` is one of the constants below."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class InfraUnavailable(Exception):
    """Not the miner's fault (e.g. no GPU capacity). Retry later; never scored."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# Infrastructure, never a miner fault. In rounds and in g8 qualification these
# make an entry infra_failed: not scored, requeued once (as Pareton), and only
# a second one is terminal. Anything after the harness's start marker is the
# miner's (crash, timeout, bad output).
GPU_UNAVAILABLE = "GPU_UNAVAILABLE"
SANDBOX_START_FAILED = "SANDBOX_START_FAILED"
INFRA_CODES = frozenset({GPU_UNAVAILABLE, SANDBOX_START_FAILED})

# g1 integrity / g2 archive safety
ZIP_TOO_LARGE = "ZIP_TOO_LARGE"
ZIP_INVALID = "ZIP_INVALID"
UNZIPPED_TOO_LARGE = "UNZIPPED_TOO_LARGE"
TOO_MANY_FILES = "TOO_MANY_FILES"
PATH_ESCAPE = "PATH_ESCAPE"
SYMLINK = "SYMLINK"
NESTED_ARCHIVE = "NESTED_ARCHIVE"
OPAQUE_BINARY = "OPAQUE_BINARY"
MISSING_FILE = "MISSING_FILE"
UNEXPECTED_FILE = "UNEXPECTED_FILE"
# descriptor (radiant_solution.json)
DESCRIPTOR_INVALID = "DESCRIPTOR_INVALID"
BASE_CLASS_MISMATCH = "BASE_CLASS_MISMATCH"
SDK_VERSION_MISMATCH = "SDK_VERSION_MISMATCH"
GPU_NOT_ALLOWED = "GPU_NOT_ALLOWED"
CPUS_OVER_LIMIT = "CPUS_OVER_LIMIT"
BATCH_SIZE_INVALID = "BATCH_SIZE_INVALID"
# .env (g5's static part)
ENV_INVALID = "ENV_INVALID"
ENV_UNKNOWN_KEY = "ENV_UNKNOWN_KEY"
ENV_BAD_REPO = "ENV_BAD_REPO"
ENV_BAD_REVISION = "ENV_BAD_REVISION"
# requirements.lock
LOCK_INVALID = "LOCK_INVALID"
LOCK_UNPINNED = "LOCK_UNPINNED"
LOCK_MISSING_HASH = "LOCK_MISSING_HASH"
LOCK_FORBIDDEN_OPTION = "LOCK_FORBIDDEN_OPTION"
# g3 contract (AST, no import)
SYNTAX_ERROR = "SYNTAX_ERROR"
CONTRACT_NO_SUBCLASS = "CONTRACT_NO_SUBCLASS"
CONTRACT_MULTIPLE_SUBCLASSES = "CONTRACT_MULTIPLE_SUBCLASSES"
CONTRACT_MISSING_OVERRIDE = "CONTRACT_MISSING_OVERRIDE"
BANNED_IMPORT = "BANNED_IMPORT"
# g5 Hugging Face
HF_UNREACHABLE = "HF_UNREACHABLE"
HF_REVISION_MISMATCH = "HF_REVISION_MISMATCH"
HF_WEIGHTS_TOO_LARGE = "HF_WEIGHTS_TOO_LARGE"
HF_HASH_MISMATCH = "HF_HASH_MISMATCH"
HF_WEIGHTS_FORMAT = "HF_WEIGHTS_FORMAT"
HF_TOKEN_WRITE_SCOPE = "HF_TOKEN_WRITE_SCOPE"
# g7 build / g8 qualification (harness)
BUILD_FAILED = "BUILD_FAILED"
BUILD_TIMEOUT = "BUILD_TIMEOUT"
IMPORT_FAILED = "IMPORT_FAILED"
INIT_FAILED = "INIT_FAILED"
LOAD_TIMEOUT = "LOAD_TIMEOUT"
LOAD_CRASH = "LOAD_CRASH"
EVAL_CRASH = "EVAL_CRASH"
EVAL_TIMEOUT = "EVAL_TIMEOUT"
BAD_OUTPUT = "BAD_OUTPUT"
SANDBOX_FAILED = "SANDBOX_FAILED"
