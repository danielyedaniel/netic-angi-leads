"""SQLite access.

Deliberately thin: plain SQL through the standard library, one short-lived connection
per unit of work. SQLite in WAL mode handles concurrent readers plus one writer, which
is plenty for a single-node prototype; the SQL itself is portable to Postgres.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
ADDED_COLUMNS = (
    ("webhook_events", "external_account_id", "TEXT"),
    ("webhook_events", "operator_tenant_id", "TEXT"),
    ("leads", "last_delivery_at", "TEXT"),
)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        # isolation_level=None = autocommit; transactions are opened explicitly below
        # so it is always obvious where a unit of work begins and ends.
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        # FULL: a committed transaction survives a power cut. "Stored" has to mean
        # stored, because we tell Angi "success" on the strength of it.
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def migrate(self) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            # Columns added after a database was first created (CREATE TABLE IF NOT
            # EXISTS does not add them). Enough for a prototype; a real deployment
            # would use numbered migrations.
            for table, column, ddl in ADDED_COLUMNS:
                present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One atomic write. BEGIN IMMEDIATE takes the write lock up front, so two
        concurrent deliveries of the same lead are serialised instead of racing."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
