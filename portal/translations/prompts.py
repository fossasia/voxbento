"""Shared prompt builder for AI interpretation.

Both the translation worker (subtitle captions) and the TTS worker
(translated text-to-speech) call ``build_interpretation_messages`` so
that identical persona, style, and vocabulary instructions are applied
regardless of the output channel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from portal.models import AIVocabularyEntry


def build_interpretation_messages(
    *,
    target_language_name: str,
    text: str,
    source_language_code: str | None = None,
    persona: str | None = None,
    style: str | None = None,
    vocabulary_entries: list[AIVocabularyEntry] | None = None,
) -> list[dict[str, str]]:
    """Build the ``messages`` list used by OpenAI-compatible chat endpoints.

    The returned list always has exactly two items:

    1. ``{"role": "system", "content": <system_prompt>}``
    2. ``{"role": "user", "content": <text>}``

    Other providers (Gemini, Anthropic) can extract the system prompt
    from ``messages[0]["content"]``.
    """
    parts: list[str] = []

    # --- base role ---
    parts.append("You are an AI interpretation engine for live events.")
    if source_language_code:
        parts.append(f"The source speech is in {source_language_code}.")
    parts.append(f"Translate the source speech into {target_language_name}.")
    parts.append(
        "Output only the translated speech text. "
        "Do not add explanations, commentary, alternatives, or Markdown formatting.\n"
        "CRITICAL: Translate EXACTLY what is provided. Do not attempt to complete incomplete thoughts, "
        "do not guess missing context, and do not summarize. If the input is grammatically incomplete, "
        "your translation must reflect that exact incompleteness."
    )

    # --- persona ---
    if persona and persona.strip():
        parts.append("")
        parts.append("Interpreter persona:")
        parts.append(persona.strip())

    # --- style ---
    if style and style.strip():
        parts.append("")
        parts.append("Interpretation style:")
        parts.append(style.strip())

    # --- vocabulary ---
    if vocabulary_entries:
        parts.append("")
        parts.append("Event-specific vocabulary (use these exact translations):")
        for entry in vocabulary_entries:
            line = f"{entry.source_term} -> {entry.target_term}"
            if entry.description:
                line += f" ({entry.description})"
            parts.append(line)

    system_prompt = "\n".join(parts)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]
