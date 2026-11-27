"""Gate results for the R0 feasibility report.

Every probe returns one or more GateResult values. A gate is one of:

passed   the check ran and met its criterion
failed   the check ran and did not
blocked  the check could not run here (missing credentials, SDK, host)
pending  the check is waiting on something outside this repo (DataHive staging)
accepted the check did not pass but the owner explicitly accepted the gap

The R0 exit gate is "each gate passed or explicitly accepted".
"""

from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

RESULTS_DIR = Path(__file__).resolve().parents[2] / "docs" / "implementation" / "r0_results"


class GateStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PENDING = "pending"
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: GateStatus
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status.value,
            "summary": self.summary,
            "details": _redact(dict(self.details)),
        }


def blocked(gate: str, reason: str, **details: Any) -> GateResult:
    return GateResult(gate, GateStatus.BLOCKED, reason, details)


def exit_gate_met(results: Iterable[GateResult]) -> bool:
    return all(r.status in (GateStatus.PASSED, GateStatus.ACCEPTED) for r in results)


# Anything that looks like a credential is scrubbed before a result is written.
_SECRET_KEY = re.compile(r"(token|secret|password|api[_-]?key|authorization|signature)", re.I)
_SECRET_VALUE = re.compile(
    r"(hf_[A-Za-z0-9]{20,}|hip_[A-Za-z0-9]{8,}|X-Amz-Signature=[0-9a-f]+|X-Amz-Credential=[^&\s]+"
    r"|ak-[A-Za-z0-9]{10,}|as-[A-Za-z0-9]{10,}|AKIA[A-Z0-9]{12,}"
    r"|(?<=<AWSAccessKeyId>)[^<]+|(?<=<StringToSign>)[^<]+|(?<=<CanonicalRequest>)[^<]+)"
)


def _redact(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, key) for v in value]
    if isinstance(value, str):
        if _SECRET_KEY.search(key) and not key.endswith(("_present", "_absent", "_set")):
            return "[REDACTED]"
        return _SECRET_VALUE.sub("[REDACTED]", value)
    return value


def write_results(probe: str, results: list[GateResult], out_dir: Path = RESULTS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "probe": probe,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "exit_gate_met": exit_gate_met(results),
        "gates": [r.as_dict() for r in results],
    }
    path = out_dir / f"{probe}.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    return path


def merge_previous(probe: str, results: list[GateResult], out_dir: Path = RESULTS_DIR) -> list[GateResult]:
    """Prepend gates from the last report that this run didn't produce (a partial rerun)."""
    path = out_dir / f"{probe}.json"
    if not path.exists():
        return results
    fresh = {r.gate for r in results}
    old = [GateResult(g["gate"], GateStatus(g["status"]), g["summary"], g.get("details", {}))
           for g in json.loads(path.read_text())["gates"] if g["gate"] not in fresh]
    return old + results
