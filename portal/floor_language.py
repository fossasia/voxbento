"""The language spoken on each room's floor right now (#1397).

It starts as the room's spoken language (``Room.floor_language_code``). Mission Control can
pick another one (``Room.floor_language_override``), for example when a panel switches to the
language of one of the room's channels. A two-way booth that outputs into the spoken language
makes the floor its own language while it does, because the person on the floor is then
speaking the booth's language. The booth wins over the Mission Control choice.

The floor reconcilers read ``current`` every tick and restart the floor worker when it changes.
A change reaches the worker only once it has held for ``DEBOUNCE_SECS``: a switch and a switch
back inside that window restart nothing, and quick back and forth restarts the worker at most
once per window.

In memory, for one portal replica like ``BoothRegistry``. The Mission Control choice is stored
on the room, so it survives a restart, and is dropped when the room's spoken language changes;
a booth's direction comes back with booth recovery.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable

from portal.models import DBBooth, Room
from portal.translations.model_languages import unsupported_languages

DEBOUNCE_SECS = 1.0

# Tests replace this to move time on.
clock = time.monotonic

# room id -> (booth id, language) of the two-way booth outputting into the room's spoken language.
_booths: dict[int, tuple[str, str]] = {}
# room id -> when its requested language last changed.
_changed_at: dict[int, float] = {}
# room id -> the language last handed to the floor worker.
_applied: dict[int, str | None] = {}


def requested(room: Room) -> str | None:
    """The language the floor should be transcribed in, before the debounce."""
    booth = _booths.get(room.id)
    if booth is not None:
        return booth[1]
    return room.floor_language_override or room.floor_language_code


def booth_holding(room_id: int) -> str | None:
    """The language of the two-way booth that holds the room's floor, or None."""
    held = _booths.get(room_id)
    return held[1] if held is not None else None


def current(room: Room) -> str | None:
    """The language the floor worker transcribes in: the requested one once it has held for DEBOUNCE_SECS."""
    wanted = requested(room)
    if (
        room.id in _applied
        and _applied[room.id] != wanted
        and clock() - _changed_at.get(room.id, -math.inf) < DEBOUNCE_SECS
    ):
        return _applied[room.id]
    _applied[room.id] = wanted
    return wanted


def note_change(room_id: int) -> None:
    """Start the debounce window: call after changing what ``requested`` returns for the room."""
    _changed_at[room_id] = clock()


def set_booth_language(room_id: int, booth_id: str, language: str | None) -> None:
    """A two-way booth started (``language``) or stopped (None) outputting into the room's spoken language.

    Only the booth holding the floor can release it, so a late release from a booth that already
    handed over does not undo the next booth's claim.
    """
    held = _booths.get(room_id)
    if language is None:
        if held is None or held[0] != booth_id:
            return
        del _booths[room_id]
    elif held == (booth_id, language):
        return
    else:
        _booths[room_id] = (booth_id, language)
    note_change(room_id)


def choices(room: Room, booths: Iterable[DBBooth]) -> list[str]:
    """What Mission Control may pick: the spoken language first, then the room's listener languages.

    With floor translation on, a language its model cannot translate from is left out: picking it
    would stop translation without saying so. The spoken language was checked when it was saved.
    """
    codes = [
        room.floor_language_code,
        *(booth.language_code for booth in booths),
        *(lang.language_code for lang in room.translation_languages if lang.enabled),
    ]
    codes = list(dict.fromkeys(code for code in codes if code and code != "floor"))
    if not room.floor_translation_enabled:
        return codes
    lacking = set(unsupported_languages(room.floor_translation_provider, room.floor_translation_model, codes))
    return [code for code in codes if code == room.floor_language_code or code not in lacking]
