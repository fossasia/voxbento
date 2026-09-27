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
