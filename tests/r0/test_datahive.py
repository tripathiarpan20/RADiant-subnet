import argparse
import hashlib

import pytest

from scripts.r0 import datahive
from scripts.r0.datahive import DataHiveError, project_round_fetch_s
from scripts.r0.report import GateStatus

IMG = b"\x89PNG fake"
SHA = hashlib.sha256(IMG).hexdigest()


def make_args(**kw):
    p = argparse.ArgumentParser()
    datahive.add_arguments(p)
    args = p.parse_args([])
    for k, v in kw.items():
        setattr(args, k, v)
    return args


class FakeClient:
    def __init__(self, base, key, mode, *, n=30, daily=300, fail_status=None, throttle=0):
        self.n, self.daily, self.fail_status, self.throttle = n, daily, fail_status, throttle
        self.served, self.calls, self.was_reset = [], 0, False

    def list_page(self, page=1, page_size=500, date=None):
        self.calls += 1
        if self.fail_status:
            raise DataHiveError(self.fail_status, "nope")
        if self.throttle and self.calls > 2 and self.calls <= 2 + self.throttle:
            raise DataHiveError(429, "slow down", "30")
        files = [{"filename": f"{i}.png", "sha256": SHA} for i in range(self.n)]
        return {"page": page, "page_size": page_size, "total": self.n, "files": files[:page_size]}

    def list_all(self, date=None, page_size=1000):
        return self.list_page(page_size=page_size)["files"]

    def fetch_image(self, filename):
        self.served.append(filename)
        return IMG, SHA, 0.05, 0.2

    def usage(self):
        return {"daily_limit": self.daily, "used_today": len(self.served), "remaining_today": 0, "resets_at": "x"}

    def reset(self):
        self.was_reset = True
        return {"reset": True}


def factory(**kw):
    holder = {}

    def make(base, key, mode):
        holder["c"] = FakeClient(base, key, mode, **kw)
        return holder["c"]

    return make, holder


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("DATAHIVE_API_KEY", "k")


def by_gate(results):
    return {r.gate: r for r in results}


def test_unset_base_reports_every_gate_pending():
    res = datahive.run(make_args(base=""))
    assert [r.gate for r in res] == list(datahive.GATES)
    assert all(r.status is GateStatus.PENDING for r in res)


def test_missing_key_blocks(monkeypatch):
    monkeypatch.delenv("DATAHIVE_API_KEY")
    res = datahive.run(make_args(base="https://staging.datahive.example/api"))
    assert all(r.status is GateStatus.BLOCKED for r in res)


def test_serving_on_production_needs_explicit_flag():
    make, holder = factory()
    res = datahive.run(make_args(base="https://api.datahive.example/api", serve=3), client_factory=make)
    assert all(r.status is GateStatus.BLOCKED for r in res)
    assert "c" not in holder  # never even connected


def test_staging_happy_path_serves_only_what_was_asked_and_resets():
    make, holder = factory()
    res = by_gate(datahive.run(make_args(base="https://staging.datahive.example/api", serve=5, reset_after=True),
                               client_factory=make))
    assert res["datahive_reachability"].status is GateStatus.PASSED
    assert res["datahive_fetch_time"].status is GateStatus.PASSED
    assert res["datahive_fetch_time"].details["served"] == 5
    assert res["datahive_rate_limits"].status is GateStatus.PASSED
    assert len(holder["c"].served) == 5 and holder["c"].was_reset


def test_no_serve_leaves_fetch_time_pending_and_serves_nothing():
    make, holder = factory()
    res = by_gate(datahive.run(make_args(base="https://staging.datahive.example/api"), client_factory=make))
    assert res["datahive_fetch_time"].status is GateStatus.PENDING
    assert holder["c"].served == []


def test_403_is_reported_as_allowlist_failure():
    make, _ = factory(fail_status=403)
    res = by_gate(datahive.run(make_args(base="https://staging.datahive.example/api"), client_factory=make))
    assert res["datahive_reachability"].status is GateStatus.FAILED
    assert "allowlist" in res["datahive_reachability"].summary
    assert res["datahive_fetch_time"].status is GateStatus.BLOCKED


def test_low_daily_limit_or_throttling_fails_rate_gate():
    make, _ = factory(daily=200)
    res = by_gate(datahive.run(make_args(base="https://staging.datahive.example/api"), client_factory=make))
    assert res["datahive_rate_limits"].status is GateStatus.FAILED
    make, _ = factory(throttle=3)
    res = by_gate(datahive.run(make_args(base="https://staging.datahive.example/api"), client_factory=make))
    assert res["datahive_rate_limits"].details["burst_throttled"] == 3
    assert res["datahive_rate_limits"].status is GateStatus.FAILED


def test_round_projection_uses_p90_and_waves():
    times = [0.1] * 9 + [1.0]
    # p90 of 10 values is the 9th smallest = 0.1; 100 images / 10 workers = 10 waves
    assert project_round_fetch_s(times, 2.0, 100, 10) == pytest.approx(2.0 + 10 * 0.1)
    assert project_round_fetch_s([0.5], 0.0, 101, 10) == pytest.approx(11 * 0.5)
    with pytest.raises(ValueError):
        project_round_fetch_s([], 0, 1, 1)


@pytest.mark.parametrize("base,prod", [
    ("https://api.datahive.ai/api", True),
    ("https://staging.datahive.ai/api", False),
    ("https://demo-radiology-api-12345.azurewebsites.net/api", False),
    ("http://localhost:8080/api", False),
])
def test_production_detection(base, prod):
    assert datahive.looks_like_production(base) is prod
