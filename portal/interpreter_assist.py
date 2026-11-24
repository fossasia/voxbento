"""Interpreter Assist: the event glossary and the live floor transcript inside the booth (#1375).

Interpreters see the glossary entries that apply to their booth, and the room's floor captions
with every glossary term marked and its translation for the booth's language. Both travel over
the booth socket the interpreter already holds, so the booth's own access checks decide who
gets them; the public caption socket and its tokens are unchanged.

Matching runs here, with the same matcher and the same overlap rule as the AI translation
pipeline (``find_term_spans`` and ``protect_terms``), so a term is marked in the booth exactly
where translation protects it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

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
#: After a failed read, how long the last known glossary is reused before the next attempt, so
#: a database outage costs one read per booth per interval rather than one per caption.
GLOSSARY_RETRY_SECONDS = 10.0
#: Booth glossaries kept in memory; expired ones are evicted first once this is reached.
MAX_CACHED_GLOSSARIES = 1024
#: Captions waiting to reach one booth. Partials and clears are dropped before finals.
MAX_PENDING_CAPTIONS = 50
#: Longest one booth's send may take. A stalled socket then delays only its own booth, by this much.
DELIVERY_TIMEOUT_SECONDS = 5.0
#: Longest caption text forwarded; a forced finalisation stops near 50 words.
MAX_CAPTION_CHARS = 2000
#: Caption statuses forwarded to the booth.
FORWARDED_STATUSES = frozenset({"partial", "final", "clear"})

CaptionStatus = Literal["partial", "final", "clear"]


class GlossaryTermPayload(TypedDict):
    id: int
    source_term: str
    target_term: str
    description: str | None


class GlossaryMessage(TypedDict):
    type: Literal["booth:glossary"]
    terms: list[GlossaryTermPayload]
    truncated: bool


class CaptionHit(TypedDict):
    """A glossary term in a caption; ``start`` and ``end`` are UTF-16 offsets into its text."""

    start: int
    end: int
    term_id: int
    target_term: str


class FloorCaptionMessage(TypedDict):
    type: Literal["booth:floor-caption"]
    status: CaptionStatus
    text: NotRequired[str]
    hits: NotRequired[list[CaptionHit]]
    segment_id: NotRequired[str]
    seq: NotRequired[int]


@dataclass(frozen=True)
class AssistTerm:
    """One glossary entry as an interpreter sees it."""

    id: int
    source_term: str
    target_term: str
    description: str | None = None
    case_sensitive: bool = False

    def as_payload(self) -> GlossaryTermPayload:
        return {
            "id": self.id,
            "source_term": self.source_term,
            "target_term": self.target_term,
            "description": self.description,
        }


@dataclass(frozen=True)
class BoothGlossary:
    #: Highest priority first: earlier terms win overlap ties, as in the translation pipeline.
    terms: tuple[AssistTerm, ...] = ()
    truncated: bool = False

    def message(self) -> GlossaryMessage:
        return {
            "type": "booth:glossary",
            "terms": [term.as_payload() for term in self.terms],
            "truncated": self.truncated,
        }


EMPTY_GLOSSARY = BoothGlossary()


def _utf16_offsets(text: str) -> list[int]:
    """UTF-16 offset of every code point boundary in ``text``, as JavaScript strings index it."""
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    return offsets


def find_hits(text: str, terms: Sequence[AssistTerm]) -> list[CaptionHit]:
    """Glossary terms in ``text``, without overlaps, with offsets the browser can slice by.

    Overlaps are settled as ``protect_terms`` settles them for translation: the longest match
    wins, then the term listed first (highest priority). Offsets are UTF-16 code units, so text
    with emoji or other characters outside the Basic Multilingual Plane is cut in the same
    place in JavaScript.
    """
    if not text:
        return []
    found = sorted(
        (
            (start, end, rank, term)
            for rank, term in enumerate(terms)
            for start, end in find_term_spans(term, text)
            if end > start
        ),
        key=lambda hit: (hit[0] - hit[1], hit[2], hit[0]),
    )
    chosen: list[tuple[int, int, int, AssistTerm]] = []
    for hit in found:
        if all(hit[1] <= other[0] or hit[0] >= other[1] for other in chosen):
            chosen.append(hit)
    chosen.sort(key=lambda hit: hit[0])
    utf16 = _utf16_offsets(text)
    return [
        {"start": utf16[start], "end": utf16[end], "term_id": term.id, "target_term": term.target_term}
        for start, end, _, term in chosen
    ]


def floor_caption_message(payload: dict, terms: Sequence[AssistTerm]) -> FloorCaptionMessage | None:
    """The booth message for one floor caption, or None when it is not one to forward."""
    status = payload.get("status")
    if payload.get("type") != "caption" or status not in FORWARDED_STATUSES:
        return None
    message: FloorCaptionMessage = {"type": "booth:floor-caption", "status": status}
    if status == "clear":
        return message
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    text = text[:MAX_CAPTION_CHARS]
    message["text"] = text
    message["hits"] = find_hits(text, terms)
    # Partials carry their segment too, so the booth can ignore one that arrives after its final.
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
    """Each booth's glossary, read once per ``ttl`` seconds and read again when its room's glossary changes.

    The last glossary read stays available after it expires or is invalidated, so a failed read
    falls back to what the booth already had instead of to nothing.
    """

    def __init__(
        self,
        loader: Callable[[str], Awaitable[BoothGlossary]] = load_booth_glossary,
        ttl: float = GLOSSARY_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        retry_after: float = GLOSSARY_RETRY_SECONDS,
        max_entries: int = MAX_CACHED_GLOSSARIES,
    ) -> None:
        self._loader = loader
        self._ttl = ttl
        self._clock = clock
        self._retry_after = retry_after
        self._max_entries = max_entries
        # booth_id -> (reuse until, glossary)
        self._entries: dict[str, tuple[float, BoothGlossary]] = {}
        # Bumped by every invalidation: a read that overlaps one must not be cached.
        self._generation = 0
        self._outage = OutageLog(logger, "interpreter_assist_glossary_unavailable")

    def peek(self, booth_id: str) -> BoothGlossary | None:
        """The last glossary read for this booth, fresh or not, without reading the database."""
        entry = self._entries.get(booth_id)
        return entry[1] if entry is not None else None

    async def get(self, booth_id: str) -> BoothGlossary:
        entry = self._entries.get(booth_id)
        if entry is not None and self._clock() < entry[0]:
            return entry[1]
        glossary = await self.refresh(booth_id)
        if glossary is not None:
            return glossary
        # The glossary is reference material: without it, captions still reach the booth.
        return entry[1] if entry is not None else EMPTY_GLOSSARY

    async def refresh(self, booth_id: str) -> BoothGlossary | None:
        """Read the glossary again; None when the read failed (the last one stays cached)."""
        generation = self._generation
        try:
            glossary = await self._loader(booth_id)
        except (SQLAlchemyError, OSError):
            self._outage.failed()
            # Wait before the next attempt; keep whatever the booth had.
            fallback = self.peek(booth_id) or EMPTY_GLOSSARY
            self._store(booth_id, fallback, self._clock() + self._retry_after, generation)
            return None
        self._outage.recovered()
        self._store(booth_id, glossary, self._clock() + self._ttl, generation)
        return glossary

    def _store(self, booth_id: str, glossary: BoothGlossary, until: float, generation: int) -> None:
        if generation != self._generation:
            # The room's glossary changed while this read ran; the next request reads it again.
            return
        self._entries[booth_id] = (until, glossary)
        if len(self._entries) > self._max_entries:
            now = self._clock()
            for key in [key for key, (expires, _) in self._entries.items() if expires <= now]:
                del self._entries[key]
            while len(self._entries) > self._max_entries:
                # Oldest insertion first: dicts keep insertion order.
                del self._entries[next(iter(self._entries))]

    def invalidate_room(self, event_slug: str, room_id: int) -> None:
        self._generation += 1
        for booth_id, (_, glossary) in list(self._entries.items()):
            try:
                slug, booth_room_id, _ = parse_booth_id(booth_id)
            except ValueError:
                continue
            if slug == event_slug and booth_room_id == room_id:
                # Expired, but kept as the fallback should the next read fail.
                self._entries[booth_id] = (-math.inf, glossary)

    def forget(self, booth_id: str) -> None:
        self._entries.pop(booth_id, None)


class FloorCaptionRelay:
    """Forward a room's floor captions to the interpreters of that room, in order, off the caption path.

    ``offer`` never waits: listener captions must not slow down because a booth is slow. Each
    booth has its own queue and sender task, so captions reach it in the order they were made,
    and a booth whose socket stalls delays only itself (each send is bounded by
    ``DELIVERY_TIMEOUT_SECONDS``). A partial still waiting when a newer caption arrives is
    replaced, since only the newest partial matters on screen.
    """

    def __init__(
        self,
        send: Callable[[str, dict], Awaitable[None]],
        booth_ids_in_room: Callable[[str, int], list[str]],
        glossary: GlossaryCache,
        max_pending: int = MAX_PENDING_CAPTIONS,
        delivery_timeout: float = DELIVERY_TIMEOUT_SECONDS,
    ) -> None:
        self._send = send
        self._booth_ids_in_room = booth_ids_in_room
        self._glossary = glossary
        self._max_pending = max_pending
        self._delivery_timeout = delivery_timeout
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
        if language_code != FLOOR_LANGUAGE:
            return
        for booth_id in self._booth_ids_in_room(event_slug, room_id):
            self._enqueue(booth_id, payload)

    def _enqueue(self, booth_id: str, payload: dict) -> None:
        queue = self._queues.setdefault(booth_id, deque())
        if queue and queue[-1].get("status") == "partial":
            # The newer caption supersedes the partial that has not been sent yet.
            queue.pop()
        queue.append(payload)
        while len(queue) > self._max_pending:
            # Only a stalled booth gets here. Keep its finished lines as long as possible.
            for index, waiting in enumerate(queue):
                if waiting.get("status") != "final":
                    del queue[index]
                    break
            else:
                queue.popleft()
        if booth_id not in self._workers:
            task = asyncio.create_task(self._drain(booth_id))
            self._workers[booth_id] = task
            track_task(task)

    async def _drain(self, booth_id: str) -> None:
        try:
            queue = self._queues.get(booth_id)
            while queue:
                payload = queue.popleft()
                try:
                    await asyncio.wait_for(self._deliver(booth_id, payload), self._delivery_timeout)
                except TimeoutError:
                    self._outage.failed()
                    logger.debug("Floor caption to %s timed out", booth_id)
        finally:
            self._workers.pop(booth_id, None)
            if not self._queues.get(booth_id):
                self._queues.pop(booth_id, None)

    async def _deliver(self, booth_id: str, payload: dict) -> None:
        # The glossary read and manager.broadcast handle database and socket failures themselves;
        # these are the ones that can still reach here. Anything else is a bug and is raised.
        try:
            glossary = await self._glossary.get(booth_id)
            message = floor_caption_message(payload, glossary.terms)
            if message is not None:
                await self._send(booth_id, dict(message))
        except (SQLAlchemyError, OSError, RuntimeError):
            self._outage.failed()
            logger.debug("Floor caption not delivered to %s", booth_id, exc_info=True)
        else:
            self._outage.recovered()

    async def idle(self) -> None:
        """Wait until every queued caption has been delivered (for tests and shutdown)."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)
