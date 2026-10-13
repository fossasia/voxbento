"""Every static file a template links to must exist, or each page view logs a 404."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "portal"
# url_for('static', path='...') and literal src="/static/..." / href="/static/..." references.
STATIC_REFS = (
    re.compile(r"url_for\(\s*['\"]static['\"]\s*,\s*path\s*=\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"(?:src|href)\s*=\s*['\"]/static/([^'\"?#{]+)"),
)


def _static_references():
    for template in sorted((ROOT / "templates").rglob("*.html")):
        source = template.read_text(encoding="utf-8")
        for pattern in STATIC_REFS:
            for match in pattern.finditer(source):
                name = template.relative_to(ROOT).as_posix()
                yield pytest.param(name, match.group(1), id=f"{name}:{match.group(1)}")


@pytest.mark.parametrize(("template", "path"), list(_static_references()))
def test_template_static_reference_exists(template, path):
    assert (ROOT / "static" / path).is_file(), f"{template} links to missing static file {path}"


# url(...) inside a stylesheet, e.g. an @font-face src. Resolved relative to the stylesheet.
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+?)['\"]?\s*\)")


def _stylesheet_references():
    for stylesheet in sorted((ROOT / "static").rglob("*.css")):
        for match in CSS_URL.finditer(stylesheet.read_text(encoding="utf-8")):
            ref = match.group(1).split("?")[0].split("#")[0]
            if not ref or ref.startswith(("data:", "http:", "https:", "//")):
                continue
            name = stylesheet.relative_to(ROOT).as_posix()
            yield pytest.param(stylesheet, ref, id=f"{name}:{ref}")


@pytest.mark.parametrize(("stylesheet", "ref"), list(_stylesheet_references()))
def test_stylesheet_url_reference_exists(stylesheet, ref):
    if ref.startswith("/static/"):
        target = ROOT / "static" / ref.removeprefix("/static/")
    else:
        target = stylesheet.parent / ref
    assert target.resolve().is_file(), f"{stylesheet.name} points to missing file {ref}"
