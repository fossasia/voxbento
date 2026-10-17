"""The Alembic revision graph stays a single line.

Revision IDs are sequential numbers, so two branches that each add the next
one collide: Alembic only warns about a duplicate ID, keeps one of the two
files and silently skips the other, and two files on the same parent leave
``alembic upgrade head`` with more than one head. Both are caught here, on the
pull request that lands second.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "alembic" / "versions"


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
