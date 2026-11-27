"""Gates g1 to g3 and the static half of g5, as the validator's worker runs them.

R4's pipeline calls ``static_gates`` on the downloaded zip. It deliberately
delegates to ``radiant_sdk.checks``: one implementation, one set of reason
codes, so ``radiant-sdk validate`` predicts the validator's verdict exactly.
"""

from __future__ import annotations

from typing import Any, Mapping

from radiant_sdk.checks import StaticReport, run_static_checks
from radiant_sdk.reasons import SolutionRejected


def static_gates(zip_bytes: bytes, manifest: Mapping[str, Any]) -> tuple[str | None, StaticReport | None]:
    """(reason_code, None) on rejection, (None, report) on a pass."""
    try:
        return None, run_static_checks(zip_bytes, manifest)
    except SolutionRejected as exc:
        return exc.code, None
