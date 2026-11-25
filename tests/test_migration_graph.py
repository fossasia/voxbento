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
from functools import cache
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "alembic" / "versions"
FIX_HINT = (
    "Renumber the newer migration to the next free number (file name and `revision`) "
    "and set its `down_revision` to the current head."
)


def _assigned(tree: ast.Module, name: str, source: str = "<module>") -> object:
    """The literal assigned to a module-level name; there must be exactly one such assignment."""
    values = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            if node.value is not None:  # a bare annotation (`revision: str`) doesn't assign anything
                values.append(node.value)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            values.append(node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            values.append(node)  # `revision += "x"` changes the value after the checked assignment
    if not values:
        hint = (
            'set it to the migration\'s number from its file name (`revision = "NNN"`)'
            if name == "revision"
            else "set it to the current head for a new migration, or `None` for the first (root) migration"
        )
        raise AssertionError(f"{source}: no module-level `{name}` value; {hint}.")
    if len(values) > 1:
        raise AssertionError(f"{source}: `{name}` is assigned {len(values)} times; assign it exactly once.")
    try:
        return ast.literal_eval(values[0])
    except ValueError:
        raise AssertionError(f"{source}: `{name}` must be a plain literal, not a computed value.") from None


@cache
def _migrations() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """(file name, revision, parent revisions) for every migration file.

    Called by each test instead of at import time, so a malformed file fails the tests with a
    clear message rather than aborting collection.
    """
    found = []
    for path in sorted(VERSIONS.glob("*.py")):
        if path.name == "__init__.py" or path.name.startswith(".#"):  # editor lock files; Alembic skips both
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        revision = _assigned(tree, "revision", path.name)
        down = _assigned(tree, "down_revision", path.name)
        parents = () if down is None else (down,) if isinstance(down, str) else tuple(down)
        found.append((path.name, revision, parents))
    return tuple(found)


def test_assigned_reads_one_value_and_rejects_duplicates():
    assert _assigned(ast.parse('revision: str\nrevision = "040"\n'), "revision") == "040"
    with pytest.raises(AssertionError, match="assigned 2 times"):
        _assigned(ast.parse('revision = "040"\nrevision = "041"\n'), "revision")
    with pytest.raises(AssertionError, match="assigned 2 times"):
        _assigned(ast.parse('revision = "040"\nrevision += "1"\n'), "revision")
    with pytest.raises(AssertionError, match="plain literal"):
        _assigned(ast.parse('revision = "04" + "0"\n'), "revision")


def test_migrations_are_found():
    assert len(_migrations()) > 1


def test_revision_ids_are_unique():
    counts = Counter(revision for _, revision, _ in _migrations())
    clashes = {
        revision: [name for name, rev, _ in _migrations() if rev == revision]
        for revision, count in counts.items()
        if count > 1
    }
    assert not clashes, f"Migrations reuse a revision ID: {clashes}. {FIX_HINT}"


def test_file_numbers_match_revisions():
    mismatched = [
        (name, revision)
        for name, revision, _ in _migrations()
        if not (match := re.match(r"(\d{3})_", name)) or match.group(1) != revision
    ]
    assert not mismatched, f"File names must start with their revision (NNN_...): {mismatched}"


def test_parents_exist():
    known = {revision for _, revision, _ in _migrations()}
    missing = [(name, parent) for name, _, parents in _migrations() for parent in parents if parent not in known]
    assert not missing, (
        f"`down_revision` points at a revision that doesn't exist: {missing}. "
        "If the parent migration was deleted by mistake, restore it; if it was renumbered, "
        "point `down_revision` at the renumbered revision or the current head."
    )


def test_no_two_migrations_share_a_parent():
    children: dict[str, list[str]] = {}
    for name, _, parents in _migrations():
        for parent in parents:
            children.setdefault(parent, []).append(name)
    branches = {parent: names for parent, names in children.items() if len(names) > 1}
    assert not branches, f"Several migrations follow the same revision: {branches}. {FIX_HINT}"


def test_each_migration_follows_one_revision():
    roots = [name for name, _, parents in _migrations() if not parents]
    merges = [(name, parents) for name, _, parents in _migrations() if len(parents) > 1]
    assert len(roots) == 1, f"Exactly one migration may have no `down_revision`, found: {roots}. {FIX_HINT}"
    assert not merges, f"Migrations must follow exactly one revision, not merge several: {merges}. {FIX_HINT}"


@cache
def _script_directory() -> ScriptDirectory:
    """The migrations as Alembic itself loads them."""
    # No alembic.ini: it interpolates %(here)s, which breaks when the checkout path contains %.
    # Values set here are interpolated too, so the % is doubled.
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic").replace("%", "%%"))
    return ScriptDirectory.from_config(config)


def test_alembic_revisions_match_file_names():
    # Compares against the values Alembic actually loads, so computed or reassigned values can't slip past.
    mismatched = [
        (Path(script.path).name, script.revision)
        for script in _script_directory().walk_revisions()
        if not Path(script.path).name.startswith(f"{script.revision}_")
    ]
    assert not mismatched, f"File names must start with the revision Alembic loads (NNN_...): {mismatched}"


def test_alembic_sees_one_head():
    heads = _script_directory().get_heads()
    assert len(heads) == 1, f"Alembic has {len(heads)} heads: {sorted(heads)}. {FIX_HINT}"
