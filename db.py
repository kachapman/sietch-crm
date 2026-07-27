"""PostgreSQL connection layer for Sietch CRM v3.0."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

_pool = None


def init_db() -> None:
    """Initialize connection pool. Call once at server startup."""
    import psycopg2
    from psycopg2 import pool

    global _pool
    _pool = pool.SimpleConnectionPool(
        minconn=2,
        maxconn=10,
        host=os.getenv("DB_HOST", "db"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "sietch_crm"),
        user=os.getenv("DB_USER", "sietch"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def get_conn():
    """Get a connection from the pool."""
    return _pool.getconn()


def put_conn(conn) -> None:
    """Return a connection to the pool."""
    _pool.putconn(conn)


@contextmanager
def db_cursor():
    """Context manager that yields a cursor, handles commit/rollback, and returns conn to pool."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        put_conn(conn)


def query(sql: str, params: tuple | None = None, fetch: str = "all") -> Any:
    """Execute a query and return results.

    fetch: 'all' -> list of rows, 'one' -> single row or None, 'none' -> commit only.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        if fetch == "all":
            result = cur.fetchall()
            conn.commit()
            return result
        elif fetch == "one":
            result = cur.fetchone()
            conn.commit()
            return result
        elif fetch == "none":
            conn.commit()
            return None
        else:
            raise ValueError(f"Invalid fetch mode: {fetch}")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        put_conn(conn)


def execute(sql: str, params: tuple | None = None) -> int:
    """Execute an INSERT/UPDATE/DELETE and return rowcount."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        rowcount = cur.rowcount
        conn.commit()
        return rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        put_conn(conn)


def insert_returning(sql: str, params: tuple | None = None) -> Any:
    """Execute an INSERT ... RETURNING and return the single value."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        result = cur.fetchone()
        conn.commit()
        return result[0] if result else None
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        put_conn(conn)


def query_one(sql: str, params: tuple | None = None) -> dict | None:
    """Execute a query and return a single row as a dict, or None."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        row = cur.fetchone()
        conn.commit()
        if not row:
            return None
        columns = [desc[0] for desc in cur.description]
        return row_to_dict(row, columns)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        put_conn(conn)


def row_to_dict(row, columns: list[str] | None = None) -> dict:
    """Convert a DB row (tuple) to a dict using column names from cursor.description, or explicit list."""
    if columns:
        return {columns[i]: row[i] for i in range(len(row))}
    return {f"col_{i}": v for i, v in enumerate(row)}


def query_dicts(sql: str, params: tuple | None = None) -> list[dict]:
    """Execute a query and return results as a list of dicts."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description] if cur.description else []
        conn.commit()
        return [row_to_dict(r, columns) for r in rows]
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        put_conn(conn)


def rows_to_dicts(rows: list, columns: list[str]) -> list[dict]:
    """Convert a list of DB rows to list of dicts."""
    return [row_to_dict(r, columns) for r in rows]
