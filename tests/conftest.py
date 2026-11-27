"""Shared fixtures. Database tests need Postgres.

Set RADIANT_TEST_DATABASE_URL to use an existing (throwaway) database, or
have Docker running and a postgres:17 container is started for the session.
Otherwise database tests are skipped.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

PG_IMAGE = "postgres:17.12-alpine"
FEE_RECIPIENT = "5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a"


@pytest.fixture(autouse=True)
def fee_recipient_env(monkeypatch):
    """The fixture's recipient comes from .env, as on a real validator."""
    monkeypatch.setenv("RADIANT_FEE_RECIPIENT", FEE_RECIPIENT)
    monkeypatch.delenv("RADIANT_FEE_TAO", raising=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_server_url():
    url = os.getenv("RADIANT_TEST_DATABASE_URL")
    if url:
        yield url
        return
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("no RADIANT_TEST_DATABASE_URL and Docker is not running")
    name = f"radiant-test-pg-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=test",
                    "-p", f"127.0.0.1:{port}:5432", PG_IMAGE], check=True, capture_output=True)
    url = f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
    try:
        import psycopg

        deadline = time.monotonic() + 60
        while True:
            try:
                psycopg.connect(url, connect_timeout=2).close()
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def db_url(pg_server_url, monkeypatch):
    """A fresh, empty database per test, with RADIANT_DATABASE_URL pointing at it."""
    import psycopg

    dbname = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server_url, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{dbname}"')
    url = pg_server_url.rsplit("/", 1)[0] + f"/{dbname}"
    monkeypatch.setenv("RADIANT_DATABASE_URL", url)
    monkeypatch.setenv("RADIANT_DB_READONLY_PASSWORD", "ro-test")
    monkeypatch.setenv("RADIANT_DB_READONLY_ROLE", f"ro_{dbname}")
    yield url
    with psycopg.connect(pg_server_url, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        c.execute(f'DROP ROLE IF EXISTS "ro_{dbname}"')
