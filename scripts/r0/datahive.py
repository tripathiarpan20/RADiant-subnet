"""DataHive reachability, fetch time and rate limits (run from the validator host).

Three gates, all built to the DataHive API v1 spec
(project library: datahive-api/datahive-api-v1.openapi.yaml):

datahive_reachability  the API answers from this host with this key (403 means
                       the host IP is not on the key's allowlist)
datahive_fetch_time    listing a day and fetching a sample of images, projected
                       to one round of --round-size studies, fits the budget
datahive_rate_limits   /usage's daily limit covers MAX_ROUNDS_PER_24H rounds,
                       and a burst of listing calls is not throttled

Serving an image is permanent on production (it leaves /files for good), so
the probe downloads nothing unless --serve N is given, and refuses --serve on
a host that does not look like staging or the demo unless --i-know-this-is-production.

With DATAHIVE_API_BASE unset every gate is reported as pending: DataHive's
staging API does not exist yet.

--mode demo talks to the demo API's older shape (GET /files/{filename} returns
the PNG directly), so the timing code can be exercised before staging exists.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import statistics
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from .report import GateResult, GateStatus

GATES = ("datahive_reachability", "datahive_fetch_time", "datahive_rate_limits")
PENDING_REASON = "DataHive staging API not available yet; run from the Hetzner validator host once it is"


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--base", default=os.getenv("DATAHIVE_API_BASE", ""), help="API base URL ending in /api")
    p.add_argument("--mode", choices=("spec", "demo"), default=os.getenv("DATAHIVE_API_MODE", "spec"))
    p.add_argument("--date", default=None, help="UTC day to list (default: no date filter)")
    p.add_argument("--serve", type=int, default=0, help="images to download for timing (consumes daily limit)")
    p.add_argument("--concurrency", type=int, default=8, help="parallel downloads assumed in the round projection")
    p.add_argument("--round-size", type=int, default=100, help="studies per round (round_sample_size)")
    p.add_argument("--rounds-per-day", type=int, default=int(os.getenv("MAX_ROUNDS_PER_24H", "3")))
    p.add_argument("--round-fetch-budget-s", type=float, default=900.0,
                   help="max acceptable projected fetch time for one round")
    p.add_argument("--burst", type=int, default=20, help="listing calls in the throttle burst")
    p.add_argument("--reset-after", action="store_true", help="POST /reset after serving (staging only)")
    p.add_argument("--i-know-this-is-production", action="store_true")


# ── client ──────────────────────────────────────────────────────────────────


class DataHiveError(RuntimeError):
    def __init__(self, status: int, body: str, retry_after: str | None = None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: {body[:200]}")


@dataclass
class Timed:
    value: Any
    seconds: float


class DataHiveClient:
    """Minimal client for the v1 spec, with a demo-API compatibility mode."""

    def __init__(self, base: str, key: str, mode: str = "spec", session=None, timeout: float = 30.0):
        import requests

        self.base = base.rstrip("/")
        self.mode = mode
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.headers["x-api-key"] = key

    def _get(self, path: str, **params) -> Any:
        r = self.s.get(f"{self.base}{path}", params={k: v for k, v in params.items() if v is not None},
                       timeout=self.timeout)
        if r.status_code >= 400:
            raise DataHiveError(r.status_code, r.text, r.headers.get("Retry-After"))
        return r

    def list_page(self, page: int = 1, page_size: int = 500, date: str | None = None) -> dict:
        if self.mode == "demo":
            body = self._get("/files").json()
            files = body if isinstance(body, list) else body.get("files", [])
            files = [{"filename": f} if isinstance(f, str) else f for f in files]
            return {"page": 1, "page_size": len(files), "total": len(files), "files": files}
        return self._get("/files", page=page, page_size=page_size, date=date).json()

    def list_all(self, date: str | None = None, page_size: int = 1000) -> list[dict]:
        out, page = [], 1
        while True:
            body = self.list_page(page=page, page_size=page_size, date=date)
            out.extend(body["files"])
            if self.mode == "demo" or len(out) >= body["total"] or not body["files"]:
                return out
            page += 1

    def fetch_image(self, filename: str) -> tuple[bytes, str | None, float, float]:
        """Return (bytes, expected sha256 or None, link seconds, download seconds)."""
        if self.mode == "demo":
            t0 = time.monotonic()
            r = self._get(f"/files/{filename}")
            return r.content, None, 0.0, time.monotonic() - t0
        t0 = time.monotonic()
        link = self._get(f"/files/{filename}/url").json()
        t1 = time.monotonic()
        r = self.s.get(link["url"], timeout=self.timeout, headers={"x-api-key": None})
        if r.status_code >= 400:
            raise DataHiveError(r.status_code, r.text)
        return r.content, link.get("sha256"), t1 - t0, time.monotonic() - t1

    def metadata(self, filename: str) -> dict:
        return self._get(f"/metadata/{filename}").json()

    def usage(self) -> dict:
        return self._get("/usage").json()

    def reset(self) -> dict:
        r = self.s.post(f"{self.base}/reset", timeout=self.timeout)
        if r.status_code >= 400:
            raise DataHiveError(r.status_code, r.text)
        return r.json()


# ── projections (pure, unit-tested) ─────────────────────────────────────────


def project_round_fetch_s(per_image_s: list[float], list_s: float, round_size: int, concurrency: int) -> float:
    """Wall time to list a day and fetch round_size images with `concurrency` workers.

    Uses the 90th-percentile per-image time so a few slow downloads in the
    round don't blow the budget unnoticed.
    """
    if not per_image_s:
        raise ValueError("need at least one timed image")
    ordered = sorted(per_image_s)
    p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
    waves = math.ceil(round_size / max(1, concurrency))
    return list_s + waves * p90


def required_daily_images(round_size: int, rounds_per_day: int) -> int:
    return round_size * rounds_per_day


def looks_like_production(base: str) -> bool:
    host = (urlparse(base).hostname or "").lower()
    return not any(tag in host for tag in ("staging", "stage", "demo", "localhost", "127.0.0.1", "dev", "test"))


# ── gates ───────────────────────────────────────────────────────────────────


def _pending(reason: str = PENDING_REASON) -> list[GateResult]:
    return [GateResult(g, GateStatus.PENDING, reason) for g in GATES]


def run(args: argparse.Namespace, client_factory: Callable[..., DataHiveClient] = DataHiveClient) -> list[GateResult]:
    if not args.base:
        return _pending()
    key = os.getenv("DATAHIVE_API_KEY", "")
    if not key:
        return [GateResult(g, GateStatus.BLOCKED, "DATAHIVE_API_KEY is not set") for g in GATES]
    if args.serve and looks_like_production(args.base) and not args.i_know_this_is_production:
        return [GateResult(g, GateStatus.BLOCKED,
                           "--serve on what looks like production consumes images for good; "
                           "pass --i-know-this-is-production to proceed") for g in GATES]

    client = client_factory(args.base, key, args.mode)
    host = urlparse(args.base).hostname
    results: list[GateResult] = []

    # 1. reachability
    t0 = time.monotonic()
    try:
        first = client.list_page(page=1, page_size=1, date=args.date)
    except DataHiveError as exc:
        hint = {401: "key rejected", 403: "host IP not on the key's allowlist", 429: "throttled"}.get(exc.status, "")
        results.append(GateResult("datahive_reachability", GateStatus.FAILED,
                                  f"HTTP {exc.status} from {host} {hint}".strip(),
                                  {"status": exc.status, "mode": args.mode}))
        results += [GateResult(g, GateStatus.BLOCKED, "API not reachable") for g in GATES[1:]]
        return results
    except Exception as exc:  # DNS, TLS, egress allowlist on the host
        results.append(GateResult("datahive_reachability", GateStatus.FAILED,
                                  f"{type(exc).__name__} reaching {host}", {"error": str(exc)[:300]}))
        results += [GateResult(g, GateStatus.BLOCKED, "API not reachable") for g in GATES[1:]]
        return results
    reach_s = time.monotonic() - t0
    results.append(GateResult("datahive_reachability", GateStatus.PASSED,
                              f"{host} answered in {reach_s:.2f}s, {first.get('total')} files listed",
                              {"latency_s": round(reach_s, 3), "total": first.get("total"), "mode": args.mode}))

    # 2. fetch time
    t0 = time.monotonic()
    listing = client.list_all(date=args.date)
    list_s = time.monotonic() - t0
    fetch: dict[str, Any] = {"listing_s": round(list_s, 3), "listed": len(listing)}
    if args.serve <= 0:
        results.append(GateResult("datahive_fetch_time", GateStatus.PENDING,
                                  "listing timed; no images served (pass --serve N on staging)", fetch))
    else:
        per_image, sizes, mismatches = [], [], 0
        for entry in listing[: args.serve]:
            data, sha, link_s, dl_s = client.fetch_image(entry["filename"])
            expected = sha or entry.get("sha256")
            if expected and hashlib.sha256(data).hexdigest() != expected:
                mismatches += 1
            per_image.append(link_s + dl_s)
            sizes.append(len(data))
        if not per_image:
            results.append(GateResult("datahive_fetch_time", GateStatus.FAILED, "no images to serve", fetch))
        else:
            projected = project_round_fetch_s(per_image, list_s, args.round_size, args.concurrency)
            fetch.update({
                "served": len(per_image),
                "per_image_s_median": round(statistics.median(per_image), 3),
                "per_image_s_max": round(max(per_image), 3),
                "mean_bytes": int(statistics.mean(sizes)),
                "sha256_mismatches": mismatches,
                "projected_round_fetch_s": round(projected, 1),
                "round_size": args.round_size,
                "concurrency": args.concurrency,
                "budget_s": args.round_fetch_budget_s,
            })
            ok = projected <= args.round_fetch_budget_s and mismatches == 0
            results.append(GateResult(
                "datahive_fetch_time", GateStatus.PASSED if ok else GateStatus.FAILED,
                f"projected {projected:.0f}s per {args.round_size}-study round "
                f"(budget {args.round_fetch_budget_s:.0f}s), {mismatches} hash mismatches", fetch))
        if args.reset_after:
            client.reset()

    # 3. rate limits
    need = required_daily_images(args.round_size, args.rounds_per_day)
    limits: dict[str, Any] = {"required_daily": need}
    throttled, retry_after = 0, None
    for _ in range(args.burst):
        try:
            client.list_page(page=1, page_size=1, date=args.date)
        except DataHiveError as exc:
            if exc.status != 429:
                raise
            throttled += 1
            retry_after = exc.retry_after
    limits.update({"burst_calls": args.burst, "burst_throttled": throttled, "retry_after": retry_after})
    if args.mode == "demo":
        results.append(GateResult("datahive_rate_limits", GateStatus.PENDING,
                                  "demo API has no /usage; daily limit unchecked", limits))
        return results
    try:
        usage = client.usage()
    except DataHiveError as exc:
        results.append(GateResult("datahive_rate_limits", GateStatus.FAILED, f"/usage HTTP {exc.status}", limits))
        return results
    limits.update({k: usage.get(k) for k in ("daily_limit", "used_today", "remaining_today", "resets_at")})
    ok = (usage.get("daily_limit") or 0) >= need and throttled == 0
    results.append(GateResult(
        "datahive_rate_limits", GateStatus.PASSED if ok else GateStatus.FAILED,
        f"daily limit {usage.get('daily_limit')} vs {need} needed; {throttled}/{args.burst} listing calls throttled",
        limits))
    return results
