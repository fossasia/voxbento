"""The migration chain must build the schema that portal.models describes.

The rest of the suite creates tables with ``Base.metadata.create_all``, so a model change that
no migration covers (or the reverse) would pass every other test and only break a deployment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from portal.models import Base

ROOT = Path(__file__).resolve().parent.parent


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def test_migrations_have_a_single_head():
    # Two branches (for example two PRs that both add revision 039) make ``alembic upgrade head`` fail.
    assert len(ScriptDirectory.from_config(alembic_config()).get_heads()) == 1


def test_migrations_build_the_schema_the_models_describe(tmp_path):
    db_path = tmp_path / "migrated.db"
    # Run the command a deployment runs, in its own process: Alembic's env.py reconfigures logging
    # (silencing existing loggers) and starts its own event loop, which must not leak into other tests.
    # Drop every spelling of DATABASE_URL first: settings read environment names case-insensitively,
    # so an inherited lowercase one could win and the upgrade would run against another database.
    env = {key: value for key, value in os.environ.items() if key.upper() != "DATABASE_URL"}
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    assert differences == [], f"models and migrations disagree: {differences}"
