"""Postgres connections (psycopg 3).

Writes run in one transaction that commits on success and rolls back on any
exception. Rows come back as dicts.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

import config


def is_pooled_url(url: str) -> bool:
    """True for transaction-pooled Postgres URLs (e.g. Neon's -pooler. endpoint / PgBouncer)."""
    return "-pooler." in url or "pgbouncer=true" in url.lower()


@contextlib.contextmanager
def connect(url: str | None = None, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    target = url or config.settings().require_database()
    # Managed transaction poolers (e.g. Neon's -pooler. endpoint / PgBouncer)
    # cannot share server-side prepared statements across transactions.
    kwargs: dict = {"row_factory": dict_row, "autocommit": autocommit}
    if is_pooled_url(target):
        kwargs["prepare_threshold"] = None
    conn = psycopg.connect(target, **kwargs)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit and not conn.closed:
            conn.rollback()
        raise
    finally:
        conn.close()
