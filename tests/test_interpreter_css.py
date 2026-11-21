from __future__ import annotations

import re
from pathlib import Path

INTERPRETER_CSS = Path(__file__).resolve().parent.parent / "portal" / "static" / "css" / "interpreter.css"


def _extract_media_block(css: str, feature: str = "prefers-reduced-motion: reduce") -> str | None:
    """Extract the contents of a media query block using balanced brace matching."""
    cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    pattern = re.compile(rf"@media\s*\(\s*{re.escape(feature)}\s*\)\s*\{{", re.IGNORECASE)
    match = pattern.search(cleaned)
    if not match:
        return None
    start_idx = match.end()
    depth = 1
    i = start_idx
    while i < len(cleaned) and depth > 0:
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
        i += 1
    if depth == 0:
        return cleaned[start_idx : i - 1]
    return None


def _parsed_rules(block_content: str) -> list[tuple[list[str], dict[str, str]]]:
    """Parse CSS rules into (list_of_selectors, dict_of_declarations)."""
    parsed = []
    for selector_group, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block_content):
        selectors = [s.strip() for s in selector_group.split(",") if s.strip()]
        declarations = {
            name.strip(): value.strip()
            for name, value in (d.split(":", 1) for d in body.split(";") if ":" in d)
        }
        parsed.append((selectors, declarations))
    return parsed


def _declarations_for(parsed_rules, target_selector: str) -> dict[str, str]:
    """Combine declarations across all rules matching target_selector in order."""
    combined = {}
    for selectors, declarations in parsed_rules:
        if target_selector in selectors:
            combined.update(declarations)
    return combined


def test_interpreter_css_has_prefers_reduced_motion():
    css = INTERPRETER_CSS.read_text(encoding="utf-8")
    block_content = _extract_media_block(css, "prefers-reduced-motion: reduce")
    assert block_content is not None, "interpreter.css must contain a @media (prefers-reduced-motion: reduce) block"

    rules = _parsed_rules(block_content)

    # 1. Target selectors must each declare animation: none !important
    for selector in [
        ".live-badge.on .live-dot",
        ".live-badge.standby .live-dot",
        ".header-btn--handover.state-flash-yellow",
    ]:
        decls = _declarations_for(rules, selector)
        assert decls.get("animation") == "none !important", f"{selector} must declare 'animation: none !important'"

    # 2. Handover button uses shared warning tokens
    handover_decls = _declarations_for(rules, ".header-btn--handover.state-flash-yellow")
    assert handover_decls.get("background") == "var(--vb-color-warning)"
    assert handover_decls.get("border-color") == "var(--vb-color-warning-text)"

    # 3. Transitions minimized on booth layout and toasts
    booth_decls = _declarations_for(rules, ".booth-layout")
    assert booth_decls.get("transition-duration") == "0.01ms !important"

    toast_decls = _declarations_for(rules, ".toast")
    assert toast_decls.get("transition-duration") == "0.01ms !important"


def test_extract_media_block_excludes_rules_outside_media_query():
    """Verify brace-balanced extraction does not capture rules that appear after the media block."""
    dummy_css = """
    @media (prefers-reduced-motion: reduce) {
        .inside { animation: none !important; }
    }
    .outside { animation: none !important; }
    """
    block = _extract_media_block(dummy_css, "prefers-reduced-motion: reduce")
    assert block is not None
    assert ".inside" in block
    assert ".outside" not in block
