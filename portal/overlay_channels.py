"""Caption channels use the same room/language keys as translation broadcasts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from portal.models import DBBooth, Room


@dataclass(frozen=True, slots=True)
class OverlayChannel:
    room_id: int
    room_name: str
    code: str
    name: str
    source: str


def overlay_channels(rooms: Sequence[Room], booths: Sequence[DBBooth]) -> list[OverlayChannel]:
    """Include all booths and enabled translation targets, even without a DBBooth.

    Multiple producers for one room/language share a feed in the existing
    pipeline; expose that feed once and name all of its configured producers.
    """
    result = []
    for room in rooms:
        names: dict[str, str] = {}
        sources: dict[str, list[str]] = {}

        def add(code: str, name: str, source: str) -> None:
            names[code] = name
            sources.setdefault(code, []).append(source)

        if room.has_floor_captions:
            add("floor", "Floor (original)", "Room audio transcription")
            if room.floor_translation_enabled:
                for lang in room.translation_languages:
                    if lang.enabled:
                        add(lang.language_code, lang.language_name, "Floor translation")
        for booth in booths:
            if booth.room_id != room.id:
                continue
            source = (
                "Interpreter booth" if booth.transcription_enabled else "Enable booth transcription to receive captions"
            )
            add(booth.language_code, booth.language_name, source)
            if booth.translation_enabled:
                for lang in booth.translation_languages:
                    if lang.enabled:
                        add(lang.language_code, lang.language_name, f"Translation from {booth.language_name} booth")
        for code in sorted(names, key=lambda c: (c != "floor", names[c].casefold(), c)):
            result.append(OverlayChannel(room.id, room.display_name, code, names[code], "; ".join(sources[code])))
    return result
