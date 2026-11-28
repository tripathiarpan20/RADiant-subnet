"""Unit tests for managed Postgres (Neon) connection and --grants-only migration support."""

from __future__ import annotations

import contextlib

import psycopg
import pytest

from db import connection, migrate


class _FakeCur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, *, can_manage: bool, existing_roles: set[str]):
        self.can_manage = can_manage
        self.existing_roles = existing_roles
        self.executed: list[str] = []

    def execute(self, query, params=()):
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.executed.append(text)
        if "rolsuper OR rolcreaterole" in text:
            return _FakeCur({"ok": self.can_manage})
        if "FROM pg_roles WHERE rolname" in text:
            return _FakeCur({"?": 1} if params[0] in self.existing_roles else None)
        if "current_database()" in text:
            return _FakeCur({"d": "neondb"})
        return _FakeCur(None)


def test_connect_disables_prepared_statements_for_neon_pooler_urls(monkeypatch):
    seen: list[dict] = []

    class DummyConn:
        closed = False

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    def fake_connect(url, **kwargs):
        seen.append({"url": url, **kwargs})
        return DummyConn()

    monkeypatch.setattr(psycopg, "connect", fake_connect)

    with connection.connect("postgresql://u:p@ep-cool-123-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"):
        pass
    with connection.connect("postgresql://u:p@ep-cool-123.us-east-2.aws.neon.tech/neondb?sslmode=require"):
        pass

    assert seen[0].get("prepare_threshold") is None and "prepare_threshold" in seen[0]
    assert "prepare_threshold" not in seen[1]


def test_grants_only_skips_create_alter_role_for_neon(monkeypatch):
    conn = _FakeConn(can_manage=False, existing_roles={"radiant_api", "radiant_readonly"})

    @contextlib.contextmanager
    def fake_connect(url, **kw):
        yield conn

    monkeypatch.setattr(migrate, "connect", fake_connect)

    # Pre-created roles get GRANT/REVOKE without CREATE/ALTER ROLE or needing passwords.
    migrate.ensure_readonly_role("postgresql://neon/neondb", "radiant_readonly", "", grants_only=True)
    configured = migrate.ensure_service_roles("postgresql://neon/neondb", {"api": ("radiant_api", "")}, grants_only=True)
    assert configured == ["radiant_api"]
    assert not any("CREATE ROLE" in q or "ALTER ROLE" in q for q in conn.executed)
    assert any("GRANT SELECT ON ALL TABLES" in q for q in conn.executed)
    assert any("GRANT SELECT, INSERT ON TABLE" in q for q in conn.executed)

    # Missing role in managed mode raises a clear operator error.
    with pytest.raises(migrate.MigrationError, match="does not exist"):
        migrate.ensure_service_roles("postgresql://neon/neondb", {"worker": ("radiant_worker", "")}, grants_only=True)


def test_migrate_rejects_pooled_url():
    with pytest.raises(migrate.MigrationError, match="direct \\(unpooled\\)"):
        migrate.migrate("postgresql://neondb_owner:pw@ep-cool-123-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require")
