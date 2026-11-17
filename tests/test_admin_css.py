from __future__ import annotations

import re
from pathlib import Path

ADMIN_CSS = Path(__file__).resolve().parent.parent / "portal" / "static" / "css" / "admin.css"


def _rules():
    css = re.sub(r"/\*.*?\*/", "", ADMIN_CSS.read_text(encoding="utf-8"), flags=re.S)
    return [(selector.strip(), body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _declarations(body):
    return {name.strip(): value.strip() for name, value in (d.split(":", 1) for d in body.split(";") if ":" in d)}


def _values_on(class_name, prop):
    """Every value admin.css gives ``prop`` on an element with ``class_name``, in source order.

    Covers the plain class and any selector whose last part adds to it (``div.x``, ``.x.y``, ``.x:hover``),
    so an override anywhere in the cascade is seen, not only the first matching rule.
    """
    targets = re.compile(rf"\.{re.escape(class_name)}(?![\w-])")
    return [
        declarations[prop]
        for selector, body in _rules()
        if prop in (declarations := _declarations(body))
        and any(targets.search(re.split(r"[\s>+~]+", item.strip())[-1]) for item in selector.split(","))
    ]


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
    # No later or more specific rule may take that back.
    assert set(_values_on("data-table", "position")) == {"relative"}


def test_modal_overlay_stacks_above_scrolling_tables():
    # Scrolling tables are positioned on phones, so an overlay that relied on page order would
    # be drawn under a table that comes after it in the page.
    z_indexes = _values_on("custom-modal-overlay", "z-index")
    assert z_indexes, ".custom-modal-overlay has no z-index in admin.css"
    for z_index in z_indexes:
        assert z_index.isdigit() and int(z_index) > 0, z_index
