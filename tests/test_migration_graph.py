"""The Alembic migrations form one straight line (#1150).

Two PRs that each add a migration with the same number pass their own CI, but together
they leave Alembic with two heads, and ``alembic upgrade head`` fails at portal startup.
These checks read the migration files directly, so they fail in the unit tests with a
message that says how to fix it, instead of only in the container logs of docker-smoke.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "alembic" / "versions"
FIX_HINT = (
    "Renumber the newer migration to the next free number (file name and `revision`) "
    "and set its `down_revision` to the current head."
)


def _assigned(tree: ast.Module, name: str) -> object:
    """The literal value assigned to a module-level name, with or without a type annotation."""
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value) if node.value is not None else None
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no module-level `{name}` assignment")


def _migrations() -> list[tuple[str, str, tuple[str, ...]]]:
    """(file name, revision, parent revisions) for every migration file."""
    found = []
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        revision = _assigned(tree, "revision")
        down = _assigned(tree, "down_revision")
        parents = () if down is None else (down,) if isinstance(down, str) else tuple(down)
        found.append((path.name, revision, parents))
    return found


MIGRATIONS = _migrations()


def test_migrations_are_found():
    assert len(MIGRATIONS) > 1


def test_revision_ids_are_unique():
    counts = Counter(revision for _, revision, _ in MIGRATIONS)
    clashes = {
        revision: [name for name, rev, _ in MIGRATIONS if rev == revision]
        for revision, count in counts.items()
        if count > 1
    }
    assert not clashes, f"Migrations reuse a revision ID: {clashes}. {FIX_HINT}"


def test_file_numbers_match_revisions():
    mismatched = [
        (name, revision)
        for name, revision, _ in MIGRATIONS
        if not (match := re.match(r"(\d{3})_", name)) or match.group(1) != revision
    ]
    assert not mismatched, f"File names must start with their revision (NNN_...): {mismatched}"


def test_parents_exist():
    known = {revision for _, revision, _ in MIGRATIONS}
    missing = [(name, parent) for name, _, parents in MIGRATIONS for parent in parents if parent not in known]
    assert not missing, f"`down_revision` points at a revision that doesn't exist: {missing}"


def test_no_two_migrations_share_a_parent():
    children: dict[str, list[str]] = {}
    for name, _, parents in MIGRATIONS:
        for parent in parents:
            children.setdefault(parent, []).append(name)
    branches = {parent: names for parent, names in children.items() if len(names) > 1}
    assert not branches, f"Several migrations follow the same revision: {branches}. {FIX_HINT}"


def test_alembic_sees_one_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"Alembic has {len(heads)} heads: {sorted(heads)}. {FIX_HINT}"
