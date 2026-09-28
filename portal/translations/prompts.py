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
    ]
    if persona and persona.strip():
        sections.extend(("", "Interpreter persona:", persona.strip()))
    if style and style.strip():
        sections.extend(("", "Interpretation style:", style.strip()))

    entries = list(vocabulary_entries)
    if entries:
        sections.extend(("", "Event-specific vocabulary:"))
        for entry in entries:
            line = f"{entry.source_term} -> {entry.target_term}"
            if entry.description:
                line += f" ({entry.description.strip()})"
            sections.append(line)
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
