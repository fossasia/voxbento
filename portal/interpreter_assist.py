"""Interpreter Assist: the event glossary and the live floor transcript inside the booth (#1375).

Interpreters see the glossary entries that apply to their booth, and the room's floor captions
with every glossary term marked and its translation for the booth's language. Both travel over
the booth socket the interpreter already holds, so the booth's own access checks decide who
gets them; the public caption socket and its tokens are unchanged.

Matching runs here, with the same matcher the AI translation pipeline uses
(``find_term_spans``), so a term is found in the booth exactly where translation finds it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from portal.booth_identity import parse_booth_id
from portal.outage_log import OutageLog
from portal.tasks import track_task
from portal.translations.vocabulary import booth_glossary_entries, find_term_spans

logger = logging.getLogger(__name__)

FLOOR_LANGUAGE = "floor"
#: Entries sent to one booth. A glossary this long is already more than anyone can scan live.
MAX_GLOSSARY_TERMS = 500
#: How long a booth's glossary is reused before it is read again. Edits made through the room
#: page are pushed at once; this only bounds how stale a change made elsewhere can be.
GLOSSARY_CACHE_SECONDS = 60.0
#: Captions waiting to reach the booths of one room. Older partials are dropped first.
MAX_PENDING_CAPTIONS = 50
#: Longest caption text forwarded; a forced finalisation stops near 50 words.
MAX_CAPTION_CHARS = 2000
#: Caption statuses forwarded to the booth.
FORWARDED_STATUSES = frozenset({"partial", "final", "clear"})


@dataclass(frozen=True)
class AssistTerm:
    """One glossary entry as an interpreter sees it."""

    id: int
    source_term: str
    target_term: str
    description: str | None = None
    case_sensitive: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source_term": self.source_term,
            "target_term": self.target_term,
            "description": self.description,
        }


@dataclass(frozen=True)
class BoothGlossary:
    terms: tuple[AssistTerm, ...] = ()
    truncated: bool = False

    def message(self) -> dict:
        return {
            "type": "booth:glossary",
            "terms": [term.as_dict() for term in self.terms],
            "truncated": self.truncated,
        }


EMPTY_GLOSSARY = BoothGlossary()


def _utf16_offsets(text: str) -> list[int]:
    """UTF-16 offset of every code point boundary in ``text``, as JavaScript strings index it."""
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    return offsets


def find_hits(text: str, terms: Iterable[AssistTerm]) -> list[dict]:
    """Glossary terms in ``text``, without overlaps, with offsets the browser can slice by.

    Where two terms overlap ("machine learning" and "learning"), the one that starts first wins,
    then the longer one. Offsets are UTF-16 code units, so text with emoji or other characters
    outside the Basic Multilingual Plane is cut in the same place in JavaScript.
    """
    if not text:
        return []
    found: list[tuple[int, int, AssistTerm]] = []
    for term in terms:
        for start, end in find_term_spans(term, text):
            if end > start:
                found.append((start, end, term))
    if not found:
        return []
    found.sort(key=lambda hit: (hit[0], -(hit[1] - hit[0]), hit[2].id))
    utf16 = _utf16_offsets(text)
    hits: list[dict] = []
    last_end = 0
    for start, end, term in found:
        if start < last_end:
            continue
        hits.append({"start": utf16[start], "end": utf16[end], "term_id": term.id, "target_term": term.target_term})
        last_end = end
    return hits


def floor_caption_message(payload: dict, terms: Iterable[AssistTerm]) -> dict | None:
    """The booth message for one floor caption, or None when it is not one to forward."""
    status = payload.get("status")
    if payload.get("type") != "caption" or status not in FORWARDED_STATUSES:
        return None
    message: dict = {"type": "booth:floor-caption", "status": status}
    if status == "clear":
        return message
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    text = text[:MAX_CAPTION_CHARS]
    message["text"] = text
    message["hits"] = find_hits(text, terms)
    if isinstance(payload.get("segment_id"), str):
        message["segment_id"] = payload["segment_id"]
    seq = payload.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        message["seq"] = seq
    return message


async def load_booth_glossary(booth_id: str) -> BoothGlossary:
    """Read the glossary that applies to ``booth_id`` (event, its room and the booth itself)."""
    from portal.database import get_session
    from portal.models import DBBooth, Event

    try:
        event_slug, room_id, language_code = parse_booth_id(booth_id)
    except ValueError:
        return EMPTY_GLOSSARY
    if language_code == FLOOR_LANGUAGE:
        return EMPTY_GLOSSARY
    async with get_session() as session:
        event_id = await session.scalar(select(Event.id).where(Event.slug == event_slug))
        if event_id is None:
            return EMPTY_GLOSSARY
        db_booth_id = await session.scalar(
            select(DBBooth.id).where(
                DBBooth.event_id == event_id, DBBooth.room_id == room_id, DBBooth.language_code == language_code
            )
        )
        entries = await booth_glossary_entries(
            session,
            event_id=event_id,
            room_id=room_id,
            booth_id=db_booth_id,
            target_language=language_code,
            max_entries=MAX_GLOSSARY_TERMS + 1,
        )
    terms = tuple(
        AssistTerm(
            id=entry.id,
            source_term=entry.source_term,
            target_term=entry.target_term,
            description=entry.description,
            case_sensitive=bool(entry.case_sensitive),
        )
        for entry in entries[:MAX_GLOSSARY_TERMS]
    )
    return BoothGlossary(terms=terms, truncated=len(entries) > MAX_GLOSSARY_TERMS)


class GlossaryCache:
    """Each booth's glossary, read once per ``ttl`` seconds and dropped when its room's glossary changes."""

    def __init__(
        self,
        loader: Callable[[str], Awaitable[BoothGlossary]] = load_booth_glossary,
        ttl: float = GLOSSARY_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, tuple[float, BoothGlossary]] = {}
        self._outage = OutageLog(logger, "interpreter_assist_glossary_unavailable")

    async def get(self, booth_id: str) -> BoothGlossary:
        now = self._clock()
        cached = self._entries.get(booth_id)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        try:
            glossary = await self._loader(booth_id)
        except (SQLAlchemyError, OSError):
            # The glossary is reference material: without it, captions still reach the booth.
            self._outage.failed()
            return cached[1] if cached is not None else EMPTY_GLOSSARY
        self._outage.recovered()
        self._entries[booth_id] = (now, glossary)
        return glossary

    def invalidate_room(self, event_slug: str, room_id: int) -> None:
        for booth_id in list(self._entries):
            try:
                slug, booth_room_id, _ = parse_booth_id(booth_id)
            except ValueError:
                continue
            if slug == event_slug and booth_room_id == room_id:
                self._entries.pop(booth_id, None)

    def forget(self, booth_id: str) -> None:
        self._entries.pop(booth_id, None)


class FloorCaptionRelay:
    """Forward a room's floor captions to the interpreters of that room, in order, off the caption path.

    ``offer`` never waits: listener captions must not slow down because a booth is slow. Each
    floor has one queue and one sender task, so captions arrive in the order they were made.
    A partial still waiting when a newer caption arrives is replaced, since only the newest
    partial matters on screen.
    """

    def __init__(
        self,
        send: Callable[[str, dict], Awaitable[None]],
        booth_ids_in_room: Callable[[str, int], list[str]],
        glossary: GlossaryCache,
        max_pending: int = MAX_PENDING_CAPTIONS,
    ) -> None:
        self._send = send
        self._booth_ids_in_room = booth_ids_in_room
        self._glossary = glossary
        self._max_pending = max_pending
        self._queues: dict[str, deque[dict]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._outage = OutageLog(logger, "interpreter_assist_floor_caption_failed")

    def offer(self, floor_booth_id: str, payload: dict) -> None:
        if payload.get("type") != "caption" or payload.get("status") not in FORWARDED_STATUSES:
            return
        try:
            event_slug, room_id, language_code = parse_booth_id(floor_booth_id)
        except ValueError:
            return
        if language_code != FLOOR_LANGUAGE or not self._booth_ids_in_room(event_slug, room_id):
            return
        queue = self._queues.setdefault(floor_booth_id, deque())
        if queue and queue[-1].get("status") == "partial":
            # The newer caption supersedes the partial that has not been sent yet.
            queue.pop()
        queue.append(payload)
        while len(queue) > self._max_pending:
            queue.popleft()
        if floor_booth_id not in self._workers:
            task = asyncio.create_task(self._drain(floor_booth_id, event_slug, room_id))
            self._workers[floor_booth_id] = task
            track_task(task)

    async def _drain(self, floor_booth_id: str, event_slug: str, room_id: int) -> None:
        try:
            queue = self._queues.get(floor_booth_id)
            while queue:
                payload = queue.popleft()
                for booth_id in self._booth_ids_in_room(event_slug, room_id):
                    await self._deliver(booth_id, payload)
        finally:
            self._workers.pop(floor_booth_id, None)
            if not self._queues.get(floor_booth_id):
                self._queues.pop(floor_booth_id, None)

    async def _deliver(self, booth_id: str, payload: dict) -> None:
        try:
            glossary = await self._glossary.get(booth_id)
            message = floor_caption_message(payload, glossary.terms)
            if message is not None:
                await self._send(booth_id, message)
        except Exception:
            # One booth's failure must not stop the others or the next caption.
            self._outage.failed()
            logger.debug("Floor caption not delivered to %s", booth_id, exc_info=True)
        else:
            self._outage.recovered()

    async def idle(self) -> None:
        """Wait until every queued caption has been delivered (for tests and shutdown)."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)
