from __future__ import annotations

import re
from pathlib import Path

ADMIN_CSS = Path(__file__).resolve().parent.parent / "portal" / "static" / "css" / "admin.css"


def _rules():
    css = re.sub(r"/\*.*?\*/", "", ADMIN_CSS.read_text(encoding="utf-8"), flags=re.S)
    return [(selector.strip(), body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _declarations(body):
    return {name.strip(): value.strip() for name, value in (d.split(":", 1) for d in body.split(";") if ":" in d)}


def test_scrolling_data_table_contains_its_visually_hidden_headers():
    # A visually hidden header label is position: absolute. Unless the table that scrolls is
    # positioned, the label is placed against the page and makes the whole page scroll sideways.
    scrolling = [
        declarations
        for selector, body in _rules()
        if selector == ".data-table" and (declarations := _declarations(body)).get("overflow-x") in {"auto", "scroll"}
    ]
    assert scrolling, ".data-table scroll rule not found in admin.css"
    for declarations in scrolling:
        assert declarations.get("position") == "relative"
