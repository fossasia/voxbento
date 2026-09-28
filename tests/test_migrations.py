"""Alembic migration configuration and graph tests."""

from __future__ import annotations

import re

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import rev_id


def test_new_revision_ids_use_alembic_hex_format() -> None:
    config = Config("alembic.ini")

    assert config.get_main_option("file_template") == "%(rev)s_%(slug)s"
    assert re.fullmatch(r"[0-9a-f]{12}", rev_id())


def test_existing_migration_graph_has_one_head() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))

    assert len(scripts.get_heads()) == 1
    assert list(scripts.walk_revisions())
