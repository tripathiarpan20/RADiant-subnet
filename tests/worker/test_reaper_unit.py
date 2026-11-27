"""Non-DB unit tests for worker/reaper.py rate-limit preservation and reaped_at tracking."""

from __future__ import annotations

import contextlib

from chain.commitment import encode
from worker.reaper import reap

HOTKEY = "5GZ9S5qgBrHCMr3kdvQ8hXdpZqz9D4eW7xvYxpRQHoVUuGKi"
OTHER = "5DhV69RTR4yN6onp8cwNu3Mtgr41jXQDy4wjKxEWkhwvTUS6"


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, rejections):
        self.rejections = rejections
        self.queries = []

    @contextlib.contextmanager
    def transaction(self):
        yield

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        if "FROM submissions" in sql:
            return FakeResult([])
        if "FROM upload_authorizations" in sql:
            return FakeResult([])
        if "FROM intake_rejections" in sql:
            return FakeResult([r for r in self.rejections if not r.get("reaped")])
        if "UPDATE intake_rejections SET reaped_at" in sql:
            for r in self.rejections:
                if (r["hotkey"], r["commit_block"], r["raw"]) == params:
                    r["reaped"] = True
        return FakeResult([])


def test_reaper_marks_rejections_reaped_and_keeps_recent_rate_limit_rows():
    key = f"radiant/private/campaigns/testnet-cxr-01/solutions/{HOTKEY}/00000000-0000-0000-0000-000000000001.zip"
    raw = encode(campaign_id="testnet-cxr-01", sha256="sha256:" + "1" * 64,
                 code_sha256="sha256:" + "2" * 64, object_key=key, payment_block=1, payment_tx=1)
    conn = FakeConn([
        {"hotkey": HOTKEY, "commit_block": 100, "raw": raw, "reason": "sha256_mismatch"},
        {"hotkey": OTHER, "commit_block": 101, "raw": raw, "reason": "locator_hotkey_mismatch"},
    ])
    deleted = []
    assert reap(conn, deleted.append) == [key]
    # Second run does not re-delete the same key because intake_rejections was marked reaped_at.
    assert reap(conn, deleted.append) == []
    assert deleted == [key]
    # DELETE on upload_authorizations preserves rows under 1 hour old.
    del_sqls = [sql for sql, _ in conn.queries if "DELETE FROM upload_authorizations" in sql]
    assert del_sqls and all("interval '1 hour'" in s for s in del_sqls)
