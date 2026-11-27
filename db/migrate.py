"""Apply db/migrations/*.sql in order, then (re)grant the read-only role.

    python -m db.migrate            # apply pending migrations
    python -m db.migrate --status   # list applied and pending

Each file runs once, in its own transaction, and is recorded in
schema_migrations with its sha256. A recorded file whose contents changed
stops the run: migrations are append-only, so fix forward with a new file.
An advisory lock makes concurrent runs (two containers starting) safe.

After migrating, the read-only role Adminer logs in with is created if
missing, its password set from RADIANT_DB_READONLY_PASSWORD, and it is
granted SELECT on every table, now and in future, and nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from psycopg import sql

import config
from db.connection import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
LOCK_KEY = 0x5241_4449  # "RADI"


class MigrationError(RuntimeError):
    pass


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, str, str]]:
    """[(version, sha256, sql)] sorted by file name."""
    out = []
    for path in sorted(directory.glob("*.sql")):
        text = path.read_text()
        out.append((path.stem, hashlib.sha256(text.encode()).hexdigest(), text))
    return out


def migrate(url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    applied_now: list[str] = []
    with connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version TEXT PRIMARY KEY,
                  sha256 TEXT NOT NULL,
                  applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            applied = {r["version"]: r["sha256"] for r in conn.execute("SELECT version, sha256 FROM schema_migrations")}
            files = migration_files(directory)
            known = {v for v, _, _ in files}
            missing = sorted(set(applied) - known)
            if missing:
                raise MigrationError(f"database has migrations this code doesn't: {missing}")
            for version, digest, text in files:
                if version in applied:
                    if applied[version] != digest:
                        raise MigrationError(f"migration {version} changed after it was applied; add a new file instead")
                    continue
                with conn.transaction():
                    conn.execute(text)
                    conn.execute("INSERT INTO schema_migrations (version, sha256) VALUES (%s, %s)", (version, digest))
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied_now


def _upsert_role(conn, role: str, password: str) -> None:
    ident, lit = sql.Identifier(role), sql.Literal(password)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if exists:
        conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}")
                     .format(ident, lit))
    else:
        conn.execute(sql.SQL("CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}")
                     .format(ident, lit))
    dbname = conn.execute("SELECT current_database() AS d").fetchone()["d"]
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), ident))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
    conn.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(ident))


def ensure_readonly_role(url: str, role: str, password: str) -> None:
    if not password:
        raise MigrationError("RADIANT_DB_READONLY_PASSWORD is not set")
    ident = sql.Identifier(role)
    with connect(url) as conn:
        _upsert_role(conn, role, password)
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ident))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(ident))
        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(ident))


# Least-privilege grants per service. Each tuple is (privileges, table_or_sequence, is_sequence).
SERVICE_GRANTS: dict[str, list[tuple[str, str, bool]]] = {
    "api": [
        ("SELECT", "schema_migrations", False),
        ("SELECT", "campaigns", False),
        ("SELECT", "submissions", False),
        ("SELECT", "submission_events", False),
        ("SELECT", "submission_logs", False),
        ("SELECT", "intake_rejections", False),
        ("SELECT", "registered_hotkeys", False),
        ("SELECT, INSERT", "upload_authorizations", False),
        ("USAGE, SELECT", "upload_authorizations_id_seq", True),
        ("SELECT", "campaign_disqualifications", False),
    ],
    "watcher": [
        ("SELECT", "campaigns", False),
        ("SELECT, INSERT", "submissions", False),
        ("USAGE, SELECT", "submissions_id_seq", True),
        ("SELECT, INSERT", "submission_events", False),
        ("USAGE, SELECT", "submission_events_id_seq", True),
        ("SELECT, INSERT, UPDATE", "intake_rejections", False),
        ("SELECT, INSERT, DELETE", "registered_hotkeys", False),
    ],
    "worker": [
        ("SELECT", "campaigns", False),
        ("SELECT, UPDATE", "submissions", False),
        ("SELECT, INSERT", "submission_events", False),
        ("USAGE, SELECT", "submission_events_id_seq", True),
        ("SELECT, INSERT, UPDATE", "submission_logs", False),
        ("SELECT", "registered_hotkeys", False),
        ("SELECT", "campaign_disqualifications", False),
        ("SELECT, DELETE", "upload_authorizations", False),
        ("SELECT, UPDATE", "intake_rejections", False),
    ],
}


def ensure_service_roles(url: str, roles: dict[str, tuple[str, str]]) -> list[str]:
    """Provision least-privilege service roles for every service with a password configured.

    ``roles`` maps service key ('api', 'watcher', 'worker') -> (role_name, password).
    Re-revokes all table/sequence privileges first so removed grants never linger.
    """
    configured: list[str] = []
    with connect(url) as conn:
        for service, (role, password) in roles.items():
            if not password:
                continue
            ident = sql.Identifier(role)
            _upsert_role(conn, role, password)
            conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(ident))
            conn.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(ident))
            for privs, obj, is_seq in SERVICE_GRANTS[service]:
                kind = "SEQUENCE" if is_seq else "TABLE"
                conn.execute(sql.SQL(f"GRANT {privs} ON {kind} {{}} TO {{}}").format(sql.Identifier(obj), ident))
            configured.append(role)
    return configured


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m db.migrate")
    p.add_argument("--status", action="store_true")
    p.add_argument("--skip-readonly-role", action="store_true")
    args = p.parse_args(argv)
    s = config.settings()
    url = s.require_database()
    if args.status:
        with connect(url) as conn:
            has = conn.execute("SELECT to_regclass('schema_migrations') AS t").fetchone()["t"]
            applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")} if has else set()
        for version, _, _ in migration_files():
            print(f"{'applied' if version in applied else 'pending'}  {version}")
        return 0
    try:
        done = migrate(url)
        if not args.skip_readonly_role:
            ensure_readonly_role(url, s.readonly_role, s.readonly_password)
        svc_roles = ensure_service_roles(url, {
            "api": (s.api_db_role, s.api_db_password),
            "watcher": (s.watcher_db_role, s.watcher_db_password),
            "worker": (s.worker_db_role, s.worker_db_password),
        })
    except MigrationError as exc:
        print(f"migrate: {exc}", file=sys.stderr)
        return 1
    print(f"migrate: applied {done or 'nothing new'}; read-only role {s.readonly_role!r} "
          f"{'skipped' if args.skip_readonly_role else 'granted SELECT'}; "
          f"service roles: {svc_roles or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
