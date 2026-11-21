from __future__ import annotations

import re
from pathlib import Path

INTERPRETER_CSS = Path(__file__).resolve().parent.parent / "portal" / "static" / "css" / "interpreter.css"


def test_interpreter_css_has_prefers_reduced_motion():
    css = re.sub(r"/\*.*?\*/", "", INTERPRETER_CSS.read_text(encoding="utf-8"), flags=re.S)
    match = re.search(r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{([^@]+)\}", css)
    assert match is not None, "interpreter.css must contain a @media (prefers-reduced-motion: reduce) block"
    block_content = match.group(1)

    assert "animation: none !important" in block_content
    assert ".live-badge.on .live-dot" in block_content
    assert ".live-badge.standby .live-dot" in block_content
    assert ".header-btn--handover.state-flash-yellow" in block_content
    assert "transition-duration: 0.01ms !important" in block_content
