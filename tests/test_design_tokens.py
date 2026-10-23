"""Shared design tokens (#243): tokens.css is a pure token file linked first on every page."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "portal"
TOKENS = ROOT / "static" / "css" / "tokens.css"
LOCAL_STYLESHEET = re.compile(r"""<link rel="stylesheet" href="{{ url_for\('static', path='(css/[^']+)'\)""")
# embed.html is themed by the embedding site (server-sanitised params and custom_css_url).
NOT_PAGES = {"embed.html"}


def _token_values():
    css = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(encoding="utf-8"), flags=re.S)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", css))


def _page_templates():
    for template in sorted((ROOT / "templates").rglob("*.html")):
        name = template.relative_to(ROOT / "templates").as_posix()
        source = template.read_text(encoding="utf-8")
        if name.startswith("email/") or name in NOT_PAGES or not source.lower().startswith("<!doctype html>"):
            continue
        if LOCAL_STYLESHEET.search(source):
            yield pytest.param(source, id=name)


def test_tokens_file_only_defines_vb_custom_properties():
    css = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(encoding="utf-8"), flags=re.S)
    blocks = re.findall(r"([^{}]+){([^}]*)}", css)
    assert [selector.strip() for selector, _ in blocks] == [":root"]
    declarations = [d.strip() for d in blocks[0][1].split(";") if d.strip()]
    assert declarations
    for declaration in declarations:
        assert declaration.startswith("--vb-"), f"tokens.css may only declare --vb-* properties: {declaration}"


@pytest.mark.parametrize("source", list(_page_templates()))
def test_page_links_tokens_before_other_stylesheets(source):
    assert LOCAL_STYLESHEET.findall(source)[0] == "css/tokens.css"


def _contrast_on_white(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 1.05 / (0.2126 * r + 0.7152 * g + 0.0722 * b + 0.05)


@pytest.mark.parametrize(
    "token",
    [
        "--vb-color-text",
        "--vb-color-text-secondary",
        "--vb-color-primary",
        "--vb-color-danger",
        "--vb-color-success-text",
    ],
)
def test_text_token_meets_wcag_aa_on_white(token):
    assert _contrast_on_white(_token_values()[token].strip()) >= 4.5
