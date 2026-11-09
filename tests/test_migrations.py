"""Alembic migrations stay runnable on the SQLite versions users actually have, and form one chain.

Revision IDs are sequential numbers, so two branches that each add the next
one collide: Alembic only warns about a duplicate ID, keeps one of the two
files and silently skips the other, and two files on the same parent leave
``alembic upgrade head`` with more than one head. Both are caught here, on the
pull request that lands second.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path
from types import ModuleType

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "alembic" / "versions"
VERSIONS_DIR = VERSIONS


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


def _run(db_path: str, func, statements: list[str] | None = None) -> None:
    """Run a migration function and optionally record every SQL statement it emits."""
    engine = create_engine(f"sqlite:///{db_path}")
    if statements is not None:

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _record)
    try:
        with engine.connect() as sa_conn:
            with Operations.context(MigrationContext.configure(sa_conn)):
                func()
            sa_conn.commit()
    finally:
        engine.dispose()


def _create_pre_018_users(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
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
    finally:
        conn.close()


def test_migration_018_alters_password_hash_on_sqlite():
    """Migration 018 must not emit ALTER COLUMN (#671).

    ``op.alter_column`` renders ``ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL``,
    which SQLite only understands from 3.53.0 onwards. Batch mode recreates the
    table instead, so the migration runs on any supported SQLite build. The
    statement assertion keeps this covered even when the test suite itself runs
    on a newer SQLite, where the plain ALTER would silently succeed.
    """
    migration = _load_migration("018_add_authtoken_and_user_email_verified.py")

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _create_pre_018_users(db_path)

        statements: list[str] = []
        _run(db_path, migration.upgrade, statements)

        offenders = [s for s in statements if "ALTER COLUMN" in s.upper()]
        assert not offenders, f"upgrade must not ALTER COLUMN on SQLite: {offenders}"

        columns = _columns(db_path, "users")
        assert "email_verified" in columns
        assert columns["password_hash"][3] == 0, "password_hash should accept NULL after 018"

        conn = sqlite3.connect(db_path)
        try:
            # Batch mode recreates the table; the unique index and the row must survive.
            indexes = {row[1]: row[2] for row in conn.execute("PRAGMA index_list(users)")}
            assert indexes.get("ix_users_email") == 1, "email index must stay unique after the table recreate"
            assert [row[2] for row in conn.execute("PRAGMA index_info(ix_users_email)")] == ["email"]
            assert conn.execute("SELECT password_hash FROM users").fetchone() == ("hash",)
        finally:
            conn.close()

        _run(db_path, migration.downgrade)

        columns = _columns(db_path, "users")
        assert "email_verified" not in columns
        assert columns["password_hash"][3] == 1, "password_hash should be NOT NULL again after downgrade"
    finally:
        os.unlink(db_path)


def test_migration_018_downgrade_backfills_passwordless_accounts():
    """Downgrading 018 must not fail because an account has no password.

    Upgrading makes ``password_hash`` nullable, and passwordless accounts created
    afterwards keep NULL there. Rolling back restores NOT NULL, so the downgrade
    has to give those rows an unusable hash instead of aborting the migration.
    """
    migration = _load_migration("018_add_authtoken_and_user_email_verified.py")

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _create_pre_018_users(db_path)
        _run(db_path, migration.upgrade)

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO users (email, display_name, password_hash) VALUES ('grace@example.org', 'Grace', NULL)"
            )
            conn.commit()
        finally:
            conn.close()

        _run(db_path, migration.downgrade)

        assert _columns(db_path, "users")["password_hash"][3] == 1
        conn = sqlite3.connect(db_path)
        try:
            hashes = dict(conn.execute("SELECT email, password_hash FROM users"))
        finally:
            conn.close()
        assert hashes["ada@example.org"] == "hash"
        assert hashes["grace@example.org"] == migration.UNUSABLE_PASSWORD_HASH
    finally:
        os.unlink(db_path)


def _declared(path: Path, name: str) -> str | None:
    """Return the string assigned to *name* at the top level of *path*."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name and isinstance(node.value, ast.Constant):
            return node.value.value
    return None


def test_every_revision_id_is_unique():
    files_by_revision = defaultdict(list)
    for path in sorted(VERSIONS.glob("*.py")):
        files_by_revision[_declared(path, "revision")].append(path.name)

    duplicates = {rev: names for rev, names in files_by_revision.items() if len(names) > 1}
    assert not duplicates, f"Revision IDs used by more than one migration: {duplicates}"


def test_migrations_form_a_single_chain():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))

    heads = scripts.get_heads()
    assert len(heads) == 1, f"Alembic has several heads, so `upgrade head` is ambiguous: {heads}"

    # Walking down from the head reaches every migration file exactly once.
    chain = [script.revision for script in scripts.walk_revisions()]
    assert len(chain) == len(list(VERSIONS.glob("*.py")))
