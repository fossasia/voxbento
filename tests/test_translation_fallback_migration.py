"""Revision 049 preserves existing rooms and can be rolled back on SQLite."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


def migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/049_room_translation_fallback.py"
    spec = importlib.util.spec_from_file_location("fallback_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_follows_upstream_without_duplicate_revisions():
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("049").down_revision == "048"


def test_sqlite_upgrade_and_downgrade_preserve_rooms_and_indexes(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fallback.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE rooms (id INTEGER PRIMARY KEY, display_name VARCHAR(200) NOT NULL)"))
            connection.execute(text("CREATE UNIQUE INDEX ix_room_name ON rooms (display_name)"))
            connection.execute(text("INSERT INTO rooms VALUES (1, 'Main Hall')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration().upgrade()
            columns = {column["name"]: column for column in inspect(connection).get_columns("rooms")}
            assert columns["translation_fallback_provider"]["nullable"]
            assert columns["translation_fallback_model"]["nullable"]
            assert connection.execute(text("SELECT * FROM rooms")).one() == (1, "Main Hall", None, None)
            connection.execute(
                text("UPDATE rooms SET translation_fallback_provider='local', translation_fallback_model='nllb'")
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration().downgrade()
            assert connection.execute(text("SELECT * FROM rooms")).one() == (1, "Main Hall")
            assert inspect(connection).get_indexes("rooms")[0]["name"] == "ix_room_name"
    finally:
        engine.dispose()


def test_postgres_migration_compiles_without_copying_the_rooms_table():
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration().upgrade()
        migration().downgrade()
    sql = output.getvalue()
    assert "ADD COLUMN translation_fallback_provider VARCHAR(50)" in sql
    assert "ADD COLUMN translation_fallback_model VARCHAR(100)" in sql
    assert "DROP COLUMN translation_fallback_model" in sql
    assert "CREATE TABLE" not in sql
