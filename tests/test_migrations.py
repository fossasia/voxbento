"""Alembic migrations stay runnable on the SQLite versions users actually have."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _load_migration(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_under_test", VERSIONS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(db_path: str, table: str) -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _run(db_path: str, func) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as sa_conn:
            with Operations.context(MigrationContext.configure(sa_conn)):
                func()
            sa_conn.commit()
    finally:
        engine.dispose()


def test_migration_018_alters_password_hash_on_sqlite():
    """Migration 018 must not emit ALTER COLUMN on SQLite < 3.53 (#671).

    ``op.alter_column`` renders ``ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL``,
    which SQLite only understands from 3.53.0 onwards. Batch mode recreates the
    table instead, so the migration runs on any supported SQLite build.
    """
    migration = _load_migration("018_add_authtoken_and_user_email_verified.py")

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email VARCHAR(320) NOT NULL,
                display_name VARCHAR(200) NOT NULL,
                password_hash VARCHAR(200) NOT NULL,
                is_admin BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX ix_users_email ON users (email)")
        conn.execute("INSERT INTO users (email, display_name, password_hash) VALUES ('ada@example.org', 'Ada', 'hash')")
        conn.commit()
        conn.close()

        _run(db_path, migration.upgrade)

        columns = _columns(db_path, "users")
        assert "email_verified" in columns
        assert columns["password_hash"][3] == 0, "password_hash should accept NULL after 018"

        conn = sqlite3.connect(db_path)
        try:
            # Batch mode recreates the table; the unique index and the row must survive.
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(users)")}
            assert "ix_users_email" in indexes
            assert conn.execute("SELECT password_hash FROM users").fetchone() == ("hash",)
        finally:
            conn.close()

        _run(db_path, migration.downgrade)

        columns = _columns(db_path, "users")
        assert "email_verified" not in columns
        assert columns["password_hash"][3] == 1, "password_hash should be NOT NULL again after downgrade"
    finally:
        os.unlink(db_path)
