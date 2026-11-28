"""Interpreter Assist (#1375): booth glossary and floor captions with glossary hits."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy.exc import OperationalError

from portal.database import get_session
from portal.interpreter_assist import (
    MAX_CAPTION_CHARS,
    AssistTerm,
    BoothGlossary,
    FloorCaptionRelay,
    GlossaryCache,
    find_hits,
    floor_caption_message,
    load_booth_glossary,
)
from portal.models import AIVocabularyEntry, DBBooth, Event, Room
from portal.websockets.manager import ConnectionManager, Session


def _term(term_id: int, source: str, target: str, *, case_sensitive: bool = False) -> AssistTerm:
    return AssistTerm(id=term_id, source_term=source, target_term=target, case_sensitive=case_sensitive)


def _utf16_slice(text: str, start: int, end: int) -> str:
    """Slice ``text`` the way JavaScript does, by UTF-16 code units."""
    encoded = text.encode("utf-16-le")
    return encoded[start * 2 : end * 2].decode("utf-16-le")


# ── Matching ──────────────────────────────────────────────────────────────


def test_hits_mark_each_term_with_its_translation():
    terms = [_term(1, "Kubernetes", "Kubernetes (FR)"), _term(2, "keynote", "discours d'ouverture")]
    text = "After the keynote we deploy Kubernetes."

    hits = find_hits(text, terms)

    assert [(text[h["start"] : h["end"]], h["target_term"], h["term_id"]) for h in hits] == [
        ("keynote", "discours d'ouverture", 2),
        ("Kubernetes", "Kubernetes (FR)", 1),
    ]


def test_hits_never_cut_a_word_apart():
    # "AI" must not be found inside "said", the same rule the translation pipeline uses.
    assert find_hits("She said it works.", [_term(1, "AI", "IA")]) == []
    assert len(find_hits("The AI works.", [_term(1, "AI", "IA")])) == 1


def test_case_sensitive_terms_only_match_their_case():
    terms = [_term(1, "Go", "Go (langage)", case_sensitive=True)]
    assert find_hits("We go home.", terms) == []
    assert len(find_hits("We use Go daily.", terms)) == 1


def test_overlapping_terms_keep_the_longer_one():
    terms = [_term(1, "learning", "apprentissage"), _term(2, "machine learning", "apprentissage automatique")]
    text = "Machine learning is everywhere."

    hits = find_hits(text, terms)

    assert len(hits) == 1
    assert hits[0]["term_id"] == 2
    assert text[hits[0]["start"] : hits[0]["end"]] == "Machine learning"


def test_a_longer_term_that_starts_later_wins_as_in_translation():
    from portal.translations.vocabulary import protect_terms

    # "data" starts first, but "data science" is longer: translation protects the longer one.
    terms = [_term(1, "big data", "mégadonnées"), _term(2, "data science", "science des données")]
    text = "We do big data science here."

    hits = find_hits(text, terms)

    assert [text[h["start"] : h["end"]] for h in hits] == ["data science"]
    assert protect_terms(text, terms).target_terms == ("science des données",)


def test_equal_length_overlaps_go_to_the_term_listed_first():
    terms = [_term(1, "AB CD", "first"), _term(2, "CD EF", "second")]
    hits = find_hits("AB CD EF", terms)
    assert [h["target_term"] for h in hits] == ["first"]


def test_hit_offsets_are_utf16_so_the_browser_cuts_the_same_text():
    text = "🎤 Welcome to PyCon 🎉 and PyCon again"
    hits = find_hits(text, [_term(1, "PyCon", "PyCon FR")])

    assert [_utf16_slice(text, h["start"], h["end"]) for h in hits] == ["PyCon", "PyCon"]
    # Python indices differ once the emoji is counted, so they must not be what is sent.
    assert hits[0]["start"] != text.index("PyCon")


def test_terms_are_found_in_scripts_written_without_spaces():
    text = "今日は機械学習について話します"
    hits = find_hits(text, [_term(1, "機械学習", "machine learning")])

    assert [_utf16_slice(text, h["start"], h["end"]) for h in hits] == ["機械学習"]


# ── Floor caption messages ────────────────────────────────────────────────


def test_final_and_partial_captions_become_booth_messages():
    terms = [_term(1, "keynote", "Hauptvortrag")]
    final = floor_caption_message(
        {"type": "caption", "status": "final", "text": "The keynote starts.", "segment_id": "s1", "seq": 7}, terms
    )
    partial = floor_caption_message({"type": "caption", "status": "partial", "text": "The keyn"}, terms)

    assert final["type"] == "booth:floor-caption"
    assert final["status"] == "final"
    assert final["segment_id"] == "s1"
    assert final["seq"] == 7
    assert final["hits"][0]["target_term"] == "Hauptvortrag"
    assert partial == {"type": "booth:floor-caption", "status": "partial", "text": "The keyn", "hits": []}


def test_clear_is_forwarded_and_other_messages_are_not():
    assert floor_caption_message({"type": "caption", "status": "clear", "text": ""}, []) == {
        "type": "booth:floor-caption",
        "status": "clear",
    }
    # Translations, timing-only or empty captions are not the speaker's words.
    assert floor_caption_message({"type": "translated_caption", "text": "Hallo", "status": "final"}, []) is None
    assert floor_caption_message({"type": "caption", "status": "final", "text": "   "}, []) is None
    assert floor_caption_message({"type": "caption", "status": "final", "text": "Hi", "seq": True}, [])[
        "text"
    ] == "Hi"
    assert "seq" not in floor_caption_message({"type": "caption", "status": "final", "text": "Hi", "seq": True}, [])


def test_long_captions_are_cut_before_matching():
    message = floor_caption_message({"type": "caption", "status": "final", "text": "word " * 2000}, [])
    assert len(message["text"]) == MAX_CAPTION_CHARS


# ── Glossary cache ────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.anyio
async def test_glossary_is_reused_until_it_expires_or_its_room_changes():
    calls: list[str] = []

    async def loader(booth_id: str) -> BoothGlossary:
        calls.append(booth_id)
        return BoothGlossary(terms=(_term(len(calls), "term", "Begriff"),))

    clock = _Clock()
    cache = GlossaryCache(loader=loader, ttl=60, clock=clock)

    await cache.get("conf-1-de")
    await cache.get("conf-1-de")
    assert calls == ["conf-1-de"]

    clock.now = 61
    await cache.get("conf-1-de")
    assert calls == ["conf-1-de", "conf-1-de"]

    await cache.get("conf-2-de")
    cache.invalidate_room("conf", 1)
    await cache.get("conf-1-de")
    await cache.get("conf-2-de")
    # Only room 1 was read again.
    assert calls == ["conf-1-de", "conf-1-de", "conf-2-de", "conf-1-de"]


@pytest.mark.anyio
async def test_a_database_failure_keeps_the_last_glossary():
    results: list[object] = [BoothGlossary(terms=(_term(1, "a", "b"),)), OperationalError("SELECT", {}, Exception())]

    async def loader(_booth_id: str) -> BoothGlossary:
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    clock = _Clock()
    cache = GlossaryCache(loader=loader, ttl=1, clock=clock)
    first = await cache.get("conf-1-de")
    clock.now = 5
    assert await cache.get("conf-1-de") is first

    empty = GlossaryCache(loader=loader, ttl=1, clock=clock)
    results.append(OperationalError("SELECT", {}, Exception()))
    assert (await empty.get("conf-1-fr")).terms == ()


# ── Relay ─────────────────────────────────────────────────────────────────


class _StaticGlossary(GlossaryCache):
    """A real cache whose reads return fixed glossaries instead of reading the database."""

    def __init__(self, glossaries: dict[str, BoothGlossary]) -> None:
        async def load(booth_id: str) -> BoothGlossary:
            return glossaries.get(booth_id, BoothGlossary())

        super().__init__(loader=load)


def _caption(status: str, text: str = "", **extra) -> dict:
    return {"type": "caption", "status": status, "text": text, **extra}


@pytest.mark.anyio
async def test_relay_sends_each_booth_its_own_translations_in_order():
    sent: list[tuple[str, dict]] = []

    async def send(booth_id: str, message: dict) -> None:
        sent.append((booth_id, message))

    glossary = _StaticGlossary(
        {
            "conf-3-de": BoothGlossary(terms=(_term(1, "keynote", "Hauptvortrag"),)),
            "conf-3-fr": BoothGlossary(terms=(_term(2, "keynote", "discours"),)),
        }
    )
    relay = FloorCaptionRelay(send, lambda slug, room: ["conf-3-de", "conf-3-fr"], glossary)

    relay.offer("conf-3-floor", _caption("final", "First keynote.", segment_id="a"))
    relay.offer("conf-3-floor", _caption("final", "Second keynote.", segment_id="b"))
    await relay.idle()

    # Each booth has its own sender, so order is guaranteed per booth.
    def received(booth_id: str) -> list[tuple[str, str]]:
        return [(m["text"], m["hits"][0]["target_term"]) for booth, m in sent if booth == booth_id]

    assert received("conf-3-de") == [("First keynote.", "Hauptvortrag"), ("Second keynote.", "Hauptvortrag")]
    assert received("conf-3-fr") == [("First keynote.", "discours"), ("Second keynote.", "discours")]


@pytest.mark.anyio
async def test_relay_replaces_a_partial_that_was_not_sent_yet():
    sent: list[dict] = []
    gate = asyncio.Event()

    async def send(_booth_id: str, message: dict) -> None:
        await gate.wait()
        sent.append(message)

    relay = FloorCaptionRelay(send, lambda slug, room: ["conf-3-de"], _StaticGlossary({}))
    relay.offer("conf-3-floor", _caption("partial", "Hel"))
    await asyncio.sleep(0)  # the first partial is now being sent
    relay.offer("conf-3-floor", _caption("partial", "Hello wo"))
    relay.offer("conf-3-floor", _caption("partial", "Hello world"))
    relay.offer("conf-3-floor", _caption("final", "Hello world.", segment_id="x"))
    gate.set()
    await relay.idle()

    assert [(m["status"], m["text"]) for m in sent] == [("partial", "Hel"), ("final", "Hello world.")]


@pytest.mark.anyio
async def test_relay_does_nothing_without_open_booths_or_for_non_floor_ids():
    sent: list[dict] = []

    async def send(_booth_id: str, message: dict) -> None:
        sent.append(message)

    open_booths: list[str] = []
    relay = FloorCaptionRelay(send, lambda slug, room: list(open_booths), _StaticGlossary({}))
    relay.offer("conf-3-floor", _caption("final", "Nobody is listening."))
    open_booths.append("conf-3-de")
    relay.offer("conf-3-de", _caption("final", "An interpreter booth's own captions."))
    relay.offer("not a booth id", _caption("final", "Ignored."))
    await relay.idle()

    assert sent == []


@pytest.mark.anyio
async def test_turning_floor_transcription_off_sends_the_booths_nothing(monkeypatch):
    from portal.websockets import manager as ws_manager

    offered: list[tuple[str, object]] = []
    monkeypatch.setattr(ws_manager.floor_caption_relay, "offer", lambda bid, payload: offered.append((bid, payload)))

    # Switching the floor's transcription off stops its worker and sends this empty caption to listeners;
    # with no worker running, nothing else is broadcast for the floor.
    await ws_manager.broadcast_transcription("conf-3-floor", "")
    assert offered == []

    # While it runs, the worker's captions do reach the booths.
    await ws_manager.broadcast_transcription("conf-3-floor", _caption("final", "Welcome."))
    assert [bid for bid, _ in offered] == ["conf-3-floor"]


@pytest.mark.anyio
async def test_one_failing_booth_does_not_stop_the_others():
    sent: list[str] = []

    async def send(booth_id: str, message: dict) -> None:
        if booth_id == "conf-3-de":
            raise RuntimeError("socket gone")
        sent.append(booth_id)

    relay = FloorCaptionRelay(send, lambda slug, room: ["conf-3-de", "conf-3-fr"], _StaticGlossary({}))
    relay.offer("conf-3-floor", _caption("final", "One."))
    relay.offer("conf-3-floor", _caption("final", "Two."))
    await relay.idle()

    assert sent == ["conf-3-fr", "conf-3-fr"]


# ── Access: delivery goes through the booth's own authorization ───────────


class _MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass


def _booth_session(booth_id: str, role: str | None = "interpreter") -> Session:
    return Session(
        booth_id=booth_id, participant_id=None, language="German", channel_id=f"{booth_id}-audio", granted_role=role
    )


@pytest.mark.anyio
async def test_floor_captions_reach_only_authorized_sockets_of_the_same_room():
    manager = ConnectionManager()
    same_room = _MockWebSocket()
    no_role = _MockWebSocket()
    other_room = _MockWebSocket()
    other_event = _MockWebSocket()
    manager.add(same_room, _booth_session("conf-3-de"))
    manager.add(no_role, _booth_session("conf-3-fr", role=None))
    manager.add(other_room, _booth_session("conf-4-de"))
    manager.add(other_event, _booth_session("other-3-de"))

    assert manager.booth_ids_in_room("conf", 3) == ["conf-3-de", "conf-3-fr"]

    relay = FloorCaptionRelay(manager.broadcast, manager.booth_ids_in_room, _StaticGlossary({}))
    relay.offer("conf-3-floor", _caption("final", "Only room three.", segment_id="z"))
    await relay.idle()

    assert [m["text"] for m in same_room.sent] == ["Only room three."]
    assert no_role.sent == other_room.sent == other_event.sent == []


# ── Glossary scope (database) ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_booth_glossary_has_the_event_room_and_booth_entries_for_its_language():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    try:
        async with get_session() as session:
            event = Event(slug="assist-scope", display_name="Assist Scope")
            other_event = Event(slug="assist-other", display_name="Other")
            session.add_all([event, other_event])
            await session.flush()
            hall = Room(event_id=event.id, display_name="Hall")
            annex = Room(event_id=event.id, display_name="Annex")
            session.add_all([hall, annex])
            await session.flush()
            german = DBBooth(event_id=event.id, room_id=hall.id, language_code="de", language_name="German")
            french = DBBooth(event_id=event.id, room_id=hall.id, language_code="fr", language_name="French")
            session.add_all([german, french])
            await session.flush()

            def entry(source: str, target: str, language: str = "all", **scope) -> AIVocabularyEntry:
                return AIVocabularyEntry(
                    event_id=scope.pop("event_id", event.id),
                    source_term=source,
                    target_language=language,
                    target_term=target,
                    **scope,
                )

            session.add_all(
                [
                    entry("Event-wide", "Veranstaltung"),
                    entry("Keynote", "Keynote", room_id=hall.id),
                    entry("Keynote", "Hauptvortrag", room_id=hall.id, booth_id=german.id),
                    entry("Cloud", "Wolke", "de", room_id=hall.id),
                    entry("Cloud", "Nuage", "fr", room_id=hall.id),
                    entry("Annex only", "Nur Anbau", room_id=annex.id),
                    entry("French booth only", "Seulement", room_id=hall.id, booth_id=french.id),
                    entry("Other event", "Andere", event_id=other_event.id),
                ]
            )
            hall_id = hall.id

        glossary = await load_booth_glossary(f"assist-scope-{hall_id}-de")

        assert sorted((t.source_term, t.target_term) for t in glossary.terms) == [
            ("Cloud", "Wolke"),
            ("Event-wide", "Veranstaltung"),
            # The booth's own entry wins over the room's entry for the same term.
            ("Keynote", "Hauptvortrag"),
        ]
        assert (await load_booth_glossary(f"assist-scope-{hall_id}-floor")).terms == ()
        assert (await load_booth_glossary("no-such-event-1-de")).terms == ()
    finally:
        await dispose()


@pytest.mark.anyio
async def test_a_long_glossary_is_sent_and_matched_in_full():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    try:
        async with get_session() as session:
            event = Event(slug="assist-cap", display_name="Cap")
            session.add(event)
            await session.flush()
            room = Room(event_id=event.id, display_name="Hall")
            session.add(room)
            await session.flush()
            session.add_all(
                AIVocabularyEntry(
                    event_id=event.id,
                    room_id=room.id,
                    source_term=f"term {i}",
                    target_language="all",
                    target_term=f"Begriff {i}",
                    priority=i,
                )
                for i in range(1200)
            )
            room_id = room.id

        glossary = await load_booth_glossary(f"assist-cap-{room_id}-de")

        assert len(glossary.terms) == 1200
        # Highest priority first.
        assert [t.source_term for t in glossary.terms[:3]] == ["term 1199", "term 1198", "term 1197"]
        # The last, lowest-priority entry is still marked in a caption.
        hits = find_hits("We start with term 0 today.", glossary.terms)
        assert [(h["target_term"], h["term_id"]) for h in hits] == [("Begriff 0", glossary.terms[-1].id)]
    finally:
        await dispose()


# ── End to end: the glossary comes with booth:joined ──────────────────────


@pytest.fixture
def booth_ws_app(monkeypatch, tmp_path):
    """The app with a file database and no shared booth token, as in test_ws_broadcast_isolation."""
    import anyio
    from fastapi.testclient import TestClient

    from fastapi_app import app
    from portal.config import settings
    from portal.database import configure, dispose, init_db
    from portal.websockets import manager as manager_module

    monkeypatch.setattr(settings, "booth_access_token", "")
    monkeypatch.setenv("BOOTH_ACCESS_TOKEN", "")
    monkeypatch.setattr(manager_module.manager, "membership_checker", None)
    monkeypatch.setattr(manager_module.manager, "token_checker", None)
    monkeypatch.setattr(manager_module, "glossary_cache", GlossaryCache())
    configure(f"sqlite+aiosqlite:///{tmp_path / 'assist.db'}")
    anyio.run(init_db)
    yield TestClient(app)
    anyio.run(dispose)


@pytest.mark.usefixtures("auto_csrf")
def test_joining_a_booth_brings_its_glossary(booth_ws_app):
    import anyio

    from portal.auth import create_participant_token

    async def seed() -> int:
        async with get_session() as session:
            event = Event(slug="assist-e2e", display_name="Assist E2E")
            session.add(event)
            await session.flush()
            room = Room(event_id=event.id, display_name="Hall")
            session.add(room)
            await session.flush()
            session.add(DBBooth(event_id=event.id, room_id=room.id, language_code="fr", language_name="French"))
            session.add(
                AIVocabularyEntry(
                    event_id=event.id,
                    room_id=room.id,
                    source_term="keynote",
                    target_language="fr",
                    target_term="discours d'ouverture",
                    description="Opening talk",
                )
            )
            return room.id

    room_id = anyio.run(seed)
    booth_id = f"assist-e2e-{room_id}-fr"
    token = create_participant_token(
        booth_id=1, role="interpreter", event_slug="assist-e2e", room_id=room_id, language_code="fr"
    )

    with booth_ws_app.websocket_connect(f"/ws/booth/{booth_id}", cookies={"session_token": token}) as ws:
        ws.send_text(json.dumps({"type": "booth:join", "display_name": "Pat", "role": "interpreter"}))
        joined = json.loads(ws.receive_text())

    assert joined["type"] == "booth:joined"
    assert joined["glossary"]["type"] == "booth:glossary"
    assert [(t["source_term"], t["target_term"], t["description"]) for t in joined["glossary"]["terms"]] == [
        ("keynote", "discours d'ouverture", "Opening talk")
    ]


# ── Review follow-ups: cache races, outages, stalled booths ───────────────


@pytest.mark.anyio
async def test_a_read_that_overlaps_an_invalidation_is_not_cached():
    release = asyncio.Event()
    versions = ["old", "new"]

    async def loader(_booth_id: str) -> BoothGlossary:
        version = versions.pop(0)
        if version == "old":
            await release.wait()
        return BoothGlossary(terms=(_term(1, "term", version),))

    cache = GlossaryCache(loader=loader, ttl=60, clock=_Clock())
    slow_read = asyncio.create_task(cache.get("conf-1-de"))
    await asyncio.sleep(0)
    cache.invalidate_room("conf", 1)  # the room's glossary is edited while the old read runs
    release.set()
    assert (await slow_read).terms == ()  # the outdated glossary is not used
    # The old read was not stored: the next request reads the edited glossary.
    assert (await cache.get("conf-1-de")).terms[0].target_term == "new"


@pytest.mark.anyio
async def test_an_older_refresh_that_finishes_last_is_not_broadcast():
    release_old = asyncio.Event()
    versions = ["old", "new"]

    async def loader(_booth_id: str) -> BoothGlossary:
        version = versions.pop(0)
        if version == "old":
            await release_old.wait()
        return BoothGlossary(terms=(_term(1, "term", version),))

    cache = GlossaryCache(loader=loader, ttl=60, clock=_Clock())
    # Two saved edits in the same room, each refreshing the booth in its own task.
    cache.invalidate_room("conf", 1)
    older = asyncio.create_task(cache.refresh("conf-1-de"))
    await asyncio.sleep(0)
    cache.invalidate_room("conf", 1)
    newer = await cache.refresh("conf-1-de")
    release_old.set()

    assert newer is not None and newer.terms[0].target_term == "new"
    assert await older is None  # nothing outdated is sent to the booth
    assert cache.peek("conf-1-de") is newer


@pytest.mark.anyio
async def test_a_failed_refresh_keeps_the_last_glossary_and_backs_off():
    calls = 0
    fail = False

    async def loader(_booth_id: str) -> BoothGlossary:
        nonlocal calls
        calls += 1
        if fail:
            raise OperationalError("SELECT", {}, Exception())
        return BoothGlossary(terms=(_term(1, "term", "Begriff"),))

    clock = _Clock()
    cache = GlossaryCache(loader=loader, ttl=60, clock=clock, retry_after=10)
    first = await cache.get("conf-1-de")
    fail = True
    cache.invalidate_room("conf", 1)

    assert await cache.refresh("conf-1-de") is None
    assert cache.peek("conf-1-de") is first
    # During the outage every caption reuses the last glossary instead of reading again.
    for _ in range(5):
        assert await cache.get("conf-1-de") is first
    assert calls == 2
    clock.now = 11
    await cache.get("conf-1-de")
    assert calls == 3


@pytest.mark.anyio
async def test_expired_glossaries_are_evicted_once_the_cache_is_full():
    async def loader(booth_id: str) -> BoothGlossary:
        return BoothGlossary()

    clock = _Clock()
    cache = GlossaryCache(loader=loader, ttl=10, clock=clock, max_entries=3)
    for room in range(3):
        await cache.get(f"conf-{room}-de")
    clock.now = 20  # all three have expired
    await cache.get("conf-9-de")

    assert cache.peek("conf-0-de") is None
    assert cache.peek("conf-9-de") is not None


@pytest.mark.anyio
async def test_a_stalled_booth_does_not_hold_up_the_others():
    stalled = asyncio.Event()
    sent: list[str] = []

    async def send(booth_id: str, message: dict) -> None:
        if booth_id == "conf-3-de":
            await stalled.wait()  # never completes
        sent.append(f"{booth_id}:{message['text']}")

    relay = FloorCaptionRelay(
        send, lambda slug, room: ["conf-3-de", "conf-3-fr"], _StaticGlossary({}), delivery_timeout=0.05
    )
    relay.offer("conf-3-floor", _caption("final", "One."))
    relay.offer("conf-3-floor", _caption("final", "Two."))
    await relay.idle()

    assert sent == ["conf-3-fr:One.", "conf-3-fr:Two."]


@pytest.mark.anyio
async def test_a_full_queue_drops_partials_before_finals():
    gate = asyncio.Event()
    sent: list[str] = []

    async def send(_booth_id: str, message: dict) -> None:
        await gate.wait()
        sent.append(message.get("text", message["status"]))

    relay = FloorCaptionRelay(send, lambda slug, room: ["conf-3-de"], _StaticGlossary({}), max_pending=3)
    relay.offer("conf-3-floor", _caption("final", "first"))
    await asyncio.sleep(0)  # "first" is being sent
    relay.offer("conf-3-floor", _caption("final", "a"))
    relay.offer("conf-3-floor", _caption("clear"))
    relay.offer("conf-3-floor", _caption("final", "b"))
    relay.offer("conf-3-floor", _caption("final", "c"))  # over the limit: the clear goes, not a final
    gate.set()
    await relay.idle()

    assert sent == ["first", "a", "b", "c"]


@pytest.mark.anyio
async def test_a_failed_refresh_after_an_edit_does_not_empty_the_booth(monkeypatch):
    from portal.websockets import manager as manager_module

    async def failing_loader(_booth_id: str) -> BoothGlossary:
        raise OperationalError("SELECT", {}, Exception())

    sent: list[tuple[str, dict]] = []

    async def broadcast(booth_id: str, message: dict) -> None:
        sent.append((booth_id, message))

    monkeypatch.setattr(manager_module, "glossary_cache", GlossaryCache(loader=failing_loader))
    monkeypatch.setattr(manager_module.manager, "booth_ids_in_room", lambda slug, room: ["conf-1-de"])
    monkeypatch.setattr(manager_module.manager, "broadcast", broadcast)

    await manager_module.notify_glossary_changed("conf", 1)

    assert sent == []


@pytest.mark.anyio
async def test_a_slow_glossary_read_does_not_hold_up_the_join(monkeypatch):
    from portal.websockets import manager as manager_module

    release = asyncio.Event()

    async def slow_loader(_booth_id: str) -> BoothGlossary:
        await release.wait()
        return BoothGlossary(terms=(_term(1, "keynote", "discours"),))

    ws = _MockWebSocket()
    monkeypatch.setattr(manager_module, "glossary_cache", GlossaryCache(loader=slow_loader))
    monkeypatch.setattr(manager_module, "JOIN_GLOSSARY_TIMEOUT", 0.05)
    monkeypatch.setattr(manager_module.manager, "get_session", lambda _ws: _booth_session("conf-1-de"))

    joined_glossary = await manager_module._join_glossary(ws, "conf-1-de")
    assert joined_glossary["terms"] == []  # the join went out without waiting

    release.set()
    for _ in range(20):
        if ws.sent:
            break
        await asyncio.sleep(0.01)
    assert ws.sent and ws.sent[0]["type"] == "booth:glossary"
    assert ws.sent[0]["terms"][0]["target_term"] == "discours"


@pytest.mark.anyio
async def test_a_slow_glossary_read_does_not_cost_a_healthy_booth_its_captions():
    release = asyncio.Event()
    reads = 0

    async def slow_loader(_booth_id: str) -> BoothGlossary:
        nonlocal reads
        reads += 1
        await release.wait()
        return BoothGlossary(terms=(_term(1, "keynote", "discours"),))

    sent: list[dict] = []

    async def send(_booth_id: str, message: dict) -> None:
        sent.append(message)

    relay = FloorCaptionRelay(
        send,
        lambda slug, room: ["conf-3-fr"],
        GlossaryCache(loader=slow_loader),
        delivery_timeout=0.2,
        glossary_timeout=0.05,
    )
    relay.offer("conf-3-floor", _caption("final", "The keynote starts.", segment_id="a"))
    relay.offer("conf-3-floor", _caption("final", "The keynote ends.", segment_id="b"))
    await relay.idle()

    # Both captions went out, without marks, while the read was still running; one read was shared.
    assert [(m["text"], m["hits"]) for m in sent] == [("The keynote starts.", []), ("The keynote ends.", [])]
    assert reads == 1

    release.set()
    await asyncio.sleep(0.01)  # the background read finishes and fills the cache
    relay.offer("conf-3-floor", _caption("final", "Another keynote.", segment_id="c"))
    await relay.idle()
    assert sent[-1]["hits"][0]["target_term"] == "discours"
    assert reads == 1


@pytest.mark.anyio
async def test_a_slow_join_read_overtaken_by_an_edit_is_not_sent(monkeypatch):
    from portal.websockets import manager as manager_module

    release_old = asyncio.Event()
    versions = ["old", "new"]

    async def loader(_booth_id: str) -> BoothGlossary:
        version = versions.pop(0)
        if version == "old":
            await release_old.wait()
        return BoothGlossary(terms=(_term(1, "keynote", version),))

    ws = _MockWebSocket()
    pushed: list[dict] = []

    async def broadcast(_booth_id: str, message: dict) -> None:
        pushed.append(message)

    monkeypatch.setattr(manager_module, "glossary_cache", GlossaryCache(loader=loader))
    monkeypatch.setattr(manager_module, "JOIN_GLOSSARY_TIMEOUT", 0.05)
    monkeypatch.setattr(manager_module.manager, "get_session", lambda _ws: _booth_session("conf-1-de"))
    monkeypatch.setattr(manager_module.manager, "booth_ids_in_room", lambda slug, room: ["conf-1-de"])
    monkeypatch.setattr(manager_module.manager, "broadcast", broadcast)

    await manager_module._join_glossary(ws, "conf-1-de")  # the "old" read is too slow and is deferred
    await manager_module.notify_glossary_changed("conf", 1)  # an edit pushes the "new" glossary
    release_old.set()
    await asyncio.sleep(0.05)

    assert [m["terms"][0]["target_term"] for m in pushed] == ["new"]
    assert ws.sent == []  # the overtaken "old" read is never sent after it
    assert manager_module.glossary_cache.peek("conf-1-de").terms[0].target_term == "new"


@pytest.mark.anyio
async def test_an_edit_in_another_room_does_not_suppress_a_slow_read():
    release = asyncio.Event()

    async def loader(_booth_id: str) -> BoothGlossary:
        await release.wait()
        return BoothGlossary(terms=(_term(1, "keynote", "Hauptvortrag"),))

    cache = GlossaryCache(loader=loader, ttl=60, clock=_Clock())
    pending = cache.read("conf-2-de")
    await asyncio.sleep(0)
    cache.invalidate_room("conf", 1)  # a different room's glossary is edited meanwhile
    release.set()

    assert (await pending).terms[0].target_term == "Hauptvortrag"
    assert cache.peek("conf-2-de") is not None  # and it was cached
