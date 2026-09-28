from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class VocabularyEntry(Protocol):
    source_term: str
    target_term: str
    description: str | None


def build_interpretation_system_prompt(
    *,
    source_language_name: str | None,
    target_language_name: str,
    persona: str | None = None,
    style: str | None = None,
    vocabulary_entries: Iterable[VocabularyEntry] = (),
) -> str:
    source = source_language_name or "the source language"
    sections = [
        "You are an AI interpretation engine for live events.",
        f"Translate the source speech from {source} into {target_language_name}.",
        "Output only the translated speech text. Do not add explanations, comments, alternatives, or Markdown.",
        "Preserve the speaker's meaning, names, numbers, URLs, code terms, product names, and project names.",
        "Translate only what is present in this segment. Do not add facts, examples, context, or commentary.",
        "Do not infer or supply missing information, and do not answer questions the speaker asks.",
        "Do not summarize, shorten, expand, or continue the speech. One input segment yields one translated segment.",
        "If the segment is unclear or incomplete, translate it as it stands rather than repairing it.",
    ]
    persona_text = (persona or "").strip()
    style_text = (style or "").strip()
    if persona_text:
        sections.extend(("", "Interpreter persona:", persona_text))
    if style_text:
        sections.extend(("", "Interpretation style:", style_text))

    entries = list(vocabulary_entries)
    if entries:
        sections.extend(("", "Event-specific vocabulary:"))
        for entry in entries:
            line = f"{entry.source_term} -> {entry.target_term}"
            if entry.description:
                line += f" ({entry.description.strip()})"
            sections.append(line)

    if persona_text or style_text or entries:
        # Last instruction wins: event configuration may shape wording, never content.
        sections.extend(
            (
                "",
                "The persona, style, and vocabulary above constrain word choice and register only.",
                "They never authorize adding, removing, or altering what the speaker said.",
                "Where they conflict with the rules above, follow the rules above.",
            )
        )
    return "\n".join(sections)


def build_interpretation_messages(
    *,
    source_language_name: str | None,
    target_language_name: str,
    text: str,
    persona: str | None = None,
    style: str | None = None,
    vocabulary_entries: Iterable[VocabularyEntry] = (),
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": build_interpretation_system_prompt(
                source_language_name=source_language_name,
                target_language_name=target_language_name,
                persona=persona,
                style=style,
                vocabulary_entries=vocabulary_entries,
            ),
        },
        {"role": "user", "content": text},
    ]
