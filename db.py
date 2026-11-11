"""PostgreSQL connection layer for Sietch CRM v3.0."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

_pool = None


class MigrationFailed(RuntimeError):
    """A migrations/*.sql file failed. It was rolled back and was not recorded
    as applied. ``init_db`` turns this into ``SystemExit`` so a deploy stops.
    """


def init_db() -> None:
    """Initialize connection pool. Call once at server startup."""
    import logging
    import psycopg2
    from psycopg2 import pool

    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None
    _pool = pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=20,
        host=os.getenv("DB_HOST", "db"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "sietch_crm"),
        user=os.getenv("DB_USER", "sietch"),
        password=os.getenv("DB_PASSWORD", ""),
        connect_timeout=10,
    )

    # Apply any pending migrations/*.sql files in lexicographic order.
    # Idempotent: each applied filename is recorded in _applied_migrations.
    # This runs after init.sql has created the base schema but assumes the
    # server.py inlined "Schema migrations" block is redundant with these
    # files (kept for historical reasons). The home box's server.py has a
    # long block of inlined DDL — for a fresh deploy we skip those by
    # running the .sql files first; the inlined ALTERs are all
    # `IF NOT EXISTS`-style and remain harmless on re-run.
    try:
        _run_pending_migrations()
    except MigrationFailed as exc:
        logging.getLogger("db.migrations").exception("%s", exc)
        raise SystemExit(str(exc)) from exc


def _run_pending_migrations(migrations_dir: str | None = None) -> None:
    """Apply any migrations/*.sql not yet recorded in `_applied_migrations`.

    Migrations are tracked by filename; once a filename is in the table it is
    never re-applied (even if the file would fail or change schema if run
    again). Each file runs in its own transaction: a successful apply is
    committed together with the `_applied_migrations` insert; a failure
    rolls back and is *not* recorded. The first failure raises
    ``MigrationFailed`` and stops the remaining files so a deploy cannot
    boot on a half-applied schema.

    ``migrations_dir`` (or env ``SIETCH_MIGRATIONS_DIR``) overrides the
    default search path; tests use this so they never touch the repo
    migrations directory.
    """
    import glob
    import logging
    import os as _os
    import psycopg2

    logger = logging.getLogger("db.migrations")

    # Resolve the migrations directory. In Docker it's /app/migrations.
    # In a workstation venv it's <repo>/migrations (next to db.py).
    override = migrations_dir or _os.environ.get("SIETCH_MIGRATIONS_DIR")
    candidates = [
        override,
        "/app/migrations",
        _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "migrations"),
    ]
    mig_dir = next((c for c in candidates if c and _os.path.isdir(c)), None)
    if not mig_dir:
        logger.info("No migrations/ directory; skipping migration runner")
        return

    files = sorted(_os.path.basename(p) for p in glob.glob(_os.path.join(mig_dir, "0*.sql")))
    if not files:
        return

    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS _applied_migrations (
                    filename  TEXT PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            conn.commit()

            cur.execute("SELECT filename FROM _applied_migrations")
            applied = {row[0] for row in cur.fetchall()}

            for fname in files:
                if fname in applied:
                    continue
                path = _os.path.join(mig_dir, fname)
                logger.info("Applying migration %s", fname)
                with open(path, "r") as fh:
                    sql = fh.read()
                try:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO _applied_migrations (filename) VALUES (%s) "
                        "ON CONFLICT (filename) DO NOTHING",
                        (fname,),
                    )
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.error(
                        "Migration %s failed; rolled back and not recorded "
                        "as applied: %s",
                        fname, e,
                    )
                    raise MigrationFailed(
                        f"Migration {fname} failed; not marked applied: {e}"
                    ) from e
    finally:
        _pool.putconn(conn)


def get_conn():
    """Get a connection from the pool."""
    return _pool.getconn()


def put_conn(conn) -> None:
    """Return a connection to the pool."""
    _pool.putconn(conn)


def close_pool() -> None:
    """Shut down the pool. Used by tests that call init_db more than once."""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None


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


def _exec(cur, sql: str, params: tuple | None) -> None:
    """Execute a cursor statement, handling None/empty params so literal % wildcards are safe."""
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)


def query(sql: str, params: tuple | None = None, fetch: str = "all") -> Any:
    """Execute a query and return results.

    fetch: 'all' -> list of rows, 'one' -> single row or None, 'none' -> commit only.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        _exec(cur, sql, params)
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
        _exec(cur, sql, params)
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
        _exec(cur, sql, params)
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
        _exec(cur, sql, params)
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
        _exec(cur, sql, params)
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
