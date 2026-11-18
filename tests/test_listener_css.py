from __future__ import annotations

import re
from pathlib import Path

LISTENER_CSS = Path(__file__).resolve().parent.parent / "portal" / "static" / "css" / "listener.css"


def _rules():
    css = re.sub(r"/\*.*?\*/", "", LISTENER_CSS.read_text(encoding="utf-8"), flags=re.S)
    return [(selector.strip(), body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _declarations(selector):
    for rule_selector, body in _rules():
        if rule_selector == selector:
            return {
                name.strip(): value.strip() for name, value in (d.split(":", 1) for d in body.split(";") if ":" in d)
            }
    raise AssertionError(f"{selector} rule not found in listener.css")


def test_caption_toolbar_row_wraps_on_narrow_screens():
    assert _declarations(".captions-toolbar__top").get("flex-wrap") == "wrap"


def test_wrapped_caption_actions_stay_right_aligned():
    assert _declarations(".captions-toolbar__actions").get("margin-left") == "auto"


def test_no_caption_toolbar_rule_turns_wrapping_off():
    for selector, body in _rules():
        if selector.endswith(".captions-toolbar__top"):
            assert not re.search(r"flex-(wrap|flow)\s*:[^;]*nowrap", body), selector
