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


@contextlib.contextmanager
def connect(url: str | None = None, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(url or config.settings().require_database(), row_factory=dict_row, autocommit=autocommit)
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
