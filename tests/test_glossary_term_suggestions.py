"""Suggesting glossary entries from an uploaded talk script or slides (#1172)."""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import time
from types import SimpleNamespace

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import pytest
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.datastructures import FormData, UploadFile

from fastapi_app import app
from portal.auth import create_user_token
from portal.crypto import encrypt_val
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_event_by_id,
    get_room_by_id,
    get_session,
    init_db,
    set_room_membership,
)
from portal.models import AIVocabularyEntry, Event, RoomTranslationLanguage
from portal.routers.admin import limit_request_body
from portal.translations import source_documents, term_suggestions
from portal.translations.source_documents import SourceDocumentError, extract_source_text
from portal.translations.term_suggestions import (
    ALL_LANGUAGES,
    TermSuggestion,
    TranslationEngine,
    TranslationProposals,
    build_suggestion_rows,
    extract_terms,
    parse_accepted_suggestions,
    parse_numbered_reply,
    propose_translations,
    suggestion_engine,
)

ABSTRACT = """# Scaling Live Captions

Ada Lovelace explains how the FOSSASIA team runs WebRTC audio through MediaMTX and FastAPI.
The talk compares Kubernetes with plain Docker hosts. Every API call is measured, and the APIs stay small.
We also look at K8s upgrades, and at how speech recognition behaves. Good speech recognition needs clean audio.
Poor speech recognition is expensive. However, the team keeps improving.
Read more at https://Example.com/SomePage or write to Hidden.Person@Example.org.
"""


def build_pdf(text: str) -> bytes:
    """The smallest single-page PDF that pypdf can read text from."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    table_offset = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, table_offset)
    return bytes(pdf)


def terms_of(text: str, **kwargs) -> dict[str, TermSuggestion]:
    return {suggestion.term: suggestion for suggestion in extract_terms(text, **kwargs)}


# ---------------------------------------------------------------------------
# Deterministic extraction
# ---------------------------------------------------------------------------


def test_abstract_surfaces_names_products_and_acronyms():
    terms = terms_of(ABSTRACT)
    for expected in ("Ada Lovelace", "FOSSASIA", "WebRTC", "MediaMTX", "FastAPI", "Kubernetes", "Docker", "K8s", "API"):
        assert expected in terms, expected


def test_plain_sentence_openers_are_not_suggested():
    terms = terms_of(ABSTRACT)
    for noise in ("The", "We", "However", "Every", "Good", "Poor", "Read"):
        assert noise not in terms, noise


def test_urls_and_email_addresses_are_ignored():
    terms = terms_of(ABSTRACT)
    assert not any("Example" in term or "Hidden" in term or "SomePage" in term for term in terms)


def test_acronym_plural_is_counted_with_the_singular():
    terms = terms_of(ABSTRACT)
    assert terms["API"].count == 2
    assert "APIs" not in terms


def test_repeated_lowercase_pair_is_suggested_only_when_repeated():
    assert terms_of(ABSTRACT)["speech recognition"].count == 3
    assert "clean audio" not in terms_of(ABSTRACT)


def test_only_acronyms_are_case_sensitive():
    terms = terms_of(ABSTRACT)
    assert terms["FOSSASIA"].case_sensitive is True
    assert terms["WebRTC"].case_sensitive is False
    assert terms["Ada Lovelace"].case_sensitive is False


def test_terms_already_in_the_glossary_are_skipped_regardless_of_case():
    assert "WebRTC" not in terms_of(ABSTRACT, existing_terms=["webrtc"])


def test_capitalised_word_also_written_in_lowercase_is_not_a_name():
    assert "Captions" not in terms_of("Live demo. We saw Captions on screen. The captions were late.")


def test_heading_in_capitals_does_not_become_acronyms():
    assert terms_of("WELCOME TO OUR GREAT EVENT\nNothing else here.") == {}


def test_limit_keeps_the_strongest_terms():
    text = " ".join(f"Use the TERM{chr(65 + index)} tool." for index in range(10)) + " Use TERMA again."
    suggestions = extract_terms(text, limit=3)
    assert len(suggestions) == 3
    assert suggestions[0].term == "TERMA"


@pytest.mark.parametrize("blob", ["a" * 1_000_000, "](" * 500_000, "<" * 1_000_000, "a@" * 500_000, "A-" * 500_000])
def test_pathological_text_is_processed_quickly(blob):
    started = time.monotonic()
    extract_terms(blob)
    assert time.monotonic() - started < 10


def test_markdown_link_keeps_its_text_but_not_its_target():
    terms = terms_of("See [the Kubernetes docs](https://Example.com/SomePage) and <span class='Hidden'>Docker</span> too.")
    assert "Kubernetes" in terms and "Docker" in terms
    assert not any("Example" in term or "SomePage" in term or "Hidden" in term for term in terms)


def test_text_in_another_script_is_handled_without_error():
    assert "Иван Петров" in terms_of("Сегодня выступает Иван Петров.")
    assert extract_terms("这是一个测试") == []


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------


def test_reads_text_and_markdown_with_a_bom():
    assert extract_source_text("notes.txt", "﻿Hello".encode()) == "Hello"
    assert extract_source_text("NOTES.MD", b"# Hello") == "# Hello"


def test_reads_pdf_text():
    assert "WebRTC" in extract_source_text("slides.pdf", build_pdf("Our WebRTC talk"))


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("slides.pptx", b"PK", "Upload a plain text"),
        ("run.exe", b"MZ", "Upload a plain text"),
        ("noextension", b"text", "Upload a plain text"),
        ("fake.pdf", b"just text", "not a valid PDF"),
        ("broken.pdf", b"%PDF-1.4 nothing useful", "could not be read"),
        ("binary.txt", b"abc\x00def", "does not look like a text file"),
        ("latin.txt", "café".encode("latin-1"), "UTF-8"),
        ("empty.md", b"   \n", "No text was found"),
    ],
)
def test_unusable_files_are_rejected_with_a_clear_message(filename, content, message):
    with pytest.raises(SourceDocumentError, match=message):
        extract_source_text(filename, content)


def test_pdf_page_past_the_deadline_stops_mid_page(monkeypatch):
    # The deadline and the between-pages check see time 0; the first drawing operator sees a later time.
    clock = itertools.chain([0, 0], itertools.repeat(1000))
    monkeypatch.setattr(source_documents, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(SourceDocumentError, match="took too long"):
        extract_source_text("slides.pdf", build_pdf("Our WebRTC talk"))


def test_pdf_page_with_too_much_text_is_rejected(monkeypatch):
    monkeypatch.setattr(source_documents, "MAX_SOURCE_CHARS", 3)
    with pytest.raises(SourceDocumentError, match="too much text"):
        extract_source_text("slides.pdf", build_pdf("Our WebRTC talk"))


def test_pdf_without_text_is_rejected():
    with pytest.raises(SourceDocumentError, match="No text was found"):
        extract_source_text("scan.pdf", build_pdf(""))


# ---------------------------------------------------------------------------
# Proposed translations
# ---------------------------------------------------------------------------


def test_numbered_reply_is_mapped_back_to_terms():
    batch = ["open source", "speech recognition"]
    reply = '1. "Open Source"\n2) **Spracherkennung**\n3. invented\nsome chatter'
    assert parse_numbered_reply(reply, batch) == {"open source": "Open Source", "speech recognition": "Spracherkennung"}


def test_over_long_reply_line_is_dropped():
    assert parse_numbered_reply("1. " + "x" * 500, ["term"]) == {}


class FakeProvider:
    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing
        self.texts: list[str] = []

    async def translate(self, *, text: str, target_lang_code: str, **_kwargs: object) -> str | None:
        self.texts.append(text)
        if target_lang_code in self.failing:
            return None
        return "\n".join(f"{line.split('. ', 1)[0]}. {target_lang_code}-{line.split('. ', 1)[1]}" for line in text.splitlines())


ENGINE = TranslationEngine("fake", "fake-model", "key")


@pytest.mark.anyio
async def test_only_the_terms_are_sent_and_every_term_gets_a_translation(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setitem(term_suggestions.PROVIDERS, "fake", provider)
    terms = [f"Term{index}" for index in range(30)]

    proposals = await propose_translations(ENGINE, "English", [("de", "German")], terms)

    assert proposals.complete is True
    assert proposals.by_language["de"]["Term29"] == "de-Term29"
    assert len(provider.texts) == 2
    assert all(line.split(". ", 1)[1] in terms for text in provider.texts for line in text.splitlines())


@pytest.mark.anyio
async def test_a_failed_language_is_reported_and_keeps_the_others(monkeypatch):
    monkeypatch.setitem(term_suggestions.PROVIDERS, "fake", FakeProvider(failing=frozenset({"fr"})))

    proposals = await propose_translations(ENGINE, "English", [("de", "German"), ("fr", "French")], ["Docker"])

    assert proposals.complete is False
    assert proposals.by_language == {"de": {"Docker": "de-Docker"}, "fr": {}}


@pytest.mark.anyio
async def test_slow_translation_stops_at_the_budget(monkeypatch):
    class HangingProvider:
        async def translate(self, **_kwargs: object) -> str:
            await asyncio.sleep(30)
            return "1. late"

    monkeypatch.setitem(term_suggestions.PROVIDERS, "fake", HangingProvider())
    monkeypatch.setattr(term_suggestions, "TRANSLATION_BUDGET_SECONDS", 0.01)

    proposals = await propose_translations(ENGINE, "English", [("de", "German")], ["Docker"])

    assert proposals.complete is False


def room_with(provider: str | None, model: str | None = "some-model") -> SimpleNamespace:
    return SimpleNamespace(floor_translation_provider=provider, floor_translation_model=model)


def test_engine_needs_a_cloud_provider_with_a_key():
    with_key = Event(encrypted_groq_api_key=encrypt_val("secret"))
    without_key = Event()

    assert suggestion_engine(with_key, room_with("groq")) == TranslationEngine("groq", "some-model", "secret")
    assert suggestion_engine(without_key, room_with("groq")) is None
    assert suggestion_engine(with_key, room_with("groq", model=None)) is None
    assert suggestion_engine(with_key, room_with(None)) is None
    assert suggestion_engine(with_key, room_with("local")) is None


def test_self_hosted_engine_needs_no_event_key():
    engine = suggestion_engine(Event(), room_with("self_hosted"))
    assert engine is not None and engine.provider == "self_hosted"


def test_engine_treats_an_undecryptable_key_as_no_key():
    assert suggestion_engine(Event(encrypted_groq_api_key="not-a-token"), room_with("groq")) is None


@pytest.mark.anyio
async def test_no_translatable_language_is_a_complete_no_op():
    proposals = await propose_translations(ENGINE, "English", [ALL_LANGUAGES], ["Docker"])
    assert proposals.complete is True


def test_review_rows_prefer_a_proposal_and_otherwise_keep_the_term():
    proposals = TranslationProposals({"de": {"open source": "quelloffen"}}, complete=False)
    rows = build_suggestion_rows(
        [TermSuggestion("open source", 3, False), TermSuggestion("Docker", 1, False)],
        [("de", "German")],
        proposals,
    )
    assert [row.targets[0].value for row in rows] == ["quelloffen", "Docker"]


# ---------------------------------------------------------------------------
# Accepted rows go through the normal validation
# ---------------------------------------------------------------------------

LANGUAGES = (("de", "German"), ("fr", "French"))


def accepted_form(*pairs: tuple[str, str]) -> FormData:
    return FormData(list(pairs))


def test_unticked_rows_are_ignored():
    form = accepted_form(("source_0", "Docker"), ("target_0_de", "Docker"))
    assert parse_accepted_suggestions(form, LANGUAGES) == ([], [])


def test_ticked_row_becomes_one_entry_per_language_with_a_translation():
    form = accepted_form(
        ("accept", "0"),
        ("source_0", "API"),
        ("case_sensitive_0", "true"),
        ("target_0_de", "Schnittstelle"),
        ("target_0_fr", ""),
    )
    entries, warnings = parse_accepted_suggestions(form, LANGUAGES)
    assert warnings == []
    assert [(entry.source_term, entry.target_language, entry.target_term) for entry in entries] == [
        ("API", "de", "Schnittstelle")
    ]
    assert entries[0].case_sensitive is True
    assert entries[0].match_type == "exact"


@pytest.mark.parametrize(
    ("source", "target", "warning"),
    [
        ("=SUM(A1)", "ok", "spreadsheet formula"),
        ("ok", "@cmd", "spreadsheet formula"),
        ("", "ok", "source_term is required"),
        ("x" * 121, "ok", "longer than 120"),
    ],
)
def test_invalid_edits_are_refused_by_the_existing_validation(source, target, warning):
    form = accepted_form(("accept", "0"), ("source_0", source), ("target_0_de", target))
    entries, warnings = parse_accepted_suggestions(form, (("de", "German"),))
    assert entries == []
    assert warning in warnings[0]


def test_language_disabled_during_review_is_reported():
    form = accepted_form(("accept", "0"), ("source_0", "Docker"), ("target_0_de", "Docker"), ("target_0_es", "Docker"))
    entries, warnings = parse_accepted_suggestions(form, (("de", "German"),))
    assert [entry.target_language for entry in entries] == ["de"]
    assert warnings == ["Skipped languages no longer enabled in this room: es"]


def test_row_without_any_translation_is_reported():
    form = accepted_form(("accept", "0"), ("source_0", "Docker"), ("target_0_de", "  "))
    entries, warnings = parse_accepted_suggestions(form, LANGUAGES)
    assert entries == []
    assert "has no translation" in warnings[0]


def test_two_rows_edited_to_the_same_term_keep_only_the_first():
    form = accepted_form(
        ("accept", "0"),
        ("accept", "1"),
        ("source_0", "Docker"),
        ("target_0_de", "A"),
        ("source_1", "docker"),
        ("target_1_de", "B"),
    )
    entries, warnings = parse_accepted_suggestions(form, (("de", "German"),))
    assert [entry.target_term for entry in entries] == ["A"]
    assert "duplicate" in warnings[0]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
async def setup():
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as session:
        event = await create_event(session, slug="suggestcon", display_name="SuggestCon")
        room = await create_room(session, event_id=event.id, display_name="Main Hall")
        room.floor_language_code = "en"
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        session.add_all(
            [
                RoomTranslationLanguage(room_id=room.id, language_code="de", language_name="German"),
                RoomTranslationLanguage(room_id=room.id, language_code="en", language_name="English"),
                RoomTranslationLanguage(room_id=room.id, language_code="fr", language_name="French", enabled=False),
            ]
        )
        admin = await create_user(session, email="admin@example.com", display_name="Admin", is_admin=True)
        coordinator = await create_user(session, email="coord@example.com", display_name="Coord")
        await set_room_membership(session, user_id=coordinator.id, room_id=room.id, role="room_coordinator")
    yield {
        "event": event.id,
        "room": room.id,
        "admin": {"user_token": create_user_token(user_id=admin.id, email=admin.email, is_admin=True)},
        "coordinator": {"user_token": create_user_token(user_id=coordinator.id, email=coordinator.email)},
        "url": f"/admin/events/{event.id}/rooms/{room.id}/ai-vocabulary",
    }
    await dispose()


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"origin": "http://test"})


async def saved_entries(room_id: int) -> list[AIVocabularyEntry]:
    async with get_session() as session:
        return list(await session.scalars(select(AIVocabularyEntry).where(AIVocabularyEntry.room_id == room_id)))


async def post_file(c: AsyncClient, ctx: dict, name: str, content: bytes, cookies: dict | None = None, **data):
    return await c.post(
        f"{ctx['url']}/suggest",
        files={"source_file": (name, content, "application/octet-stream")},
        data=data,
        cookies=ctx["admin"] if cookies is None else cookies,
        headers={"accept": "text/html"},
        follow_redirects=False,
    )


@pytest.mark.anyio
async def test_upload_shows_suggestions_and_saves_nothing(setup):
    async with client() as c:
        response = await post_file(c, setup, "abstract.md", ABSTRACT.encode())
    assert response.status_code == 200
    for expected in (b"Ada Lovelace", b"FOSSASIA", b"WebRTC", b"abstract.md"):
        assert expected in response.content
    assert b"German" in response.content
    # The source language and disabled languages are not translation targets.
    assert b"<th>English</th>" not in response.content
    assert b"<th>French</th>" not in response.content
    assert await saved_entries(setup["room"]) == []


@pytest.mark.anyio
async def test_uploaded_pdf_shows_suggestions(setup):
    async with client() as c:
        response = await post_file(c, setup, "slides.pdf", build_pdf("Our WebRTC talk"))
    assert response.status_code == 200
    assert b"WebRTC" in response.content


@pytest.mark.anyio
async def test_upload_is_closed_once_read(setup, monkeypatch):
    closed = []
    original_close = UploadFile.close

    async def tracking_close(self: UploadFile) -> None:
        closed.append(self.filename)
        await original_close(self)

    monkeypatch.setattr(UploadFile, "close", tracking_close)
    async with client() as c:
        await post_file(c, setup, "secret-script.txt", b"The WebRTC script")
    assert closed == ["secret-script.txt"]


@pytest.mark.anyio
async def test_oversized_upload_is_rejected_with_a_message(setup, monkeypatch):
    monkeypatch.setattr("portal.routers.admin.MAX_SOURCE_BYTES", 10)
    async with client() as c:
        response = await post_file(c, setup, "big.txt", b"x" * 11)
    assert response.status_code == 413
    assert b"10 MB or smaller" in response.content


@pytest.mark.anyio
async def test_body_limit_stops_before_the_upload_is_handed_to_the_parser():
    chunks = [b"x" * 6, b"x" * 6, b"x" * 6]

    async def receive() -> dict:
        body = chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    limited = limit_request_body(Request({"type": "http"}, receive), max_bytes=10)
    first = await limited.receive()
    assert first["body"] == b"x" * 6
    with pytest.raises(HTTPException) as rejected:
        await limited.receive()
    assert rejected.value.status_code == 413
    # The rest was read and dropped, so the browser can receive the error.
    assert chunks == []


@pytest.mark.anyio
async def test_body_over_the_limit_is_rejected_by_the_route(setup, monkeypatch):
    monkeypatch.setattr("portal.routers.admin.MAX_SOURCE_BYTES", 10)
    monkeypatch.setattr("portal.routers.admin.MAX_MULTIPART_OVERHEAD_BYTES", 10)
    async with client() as c:
        response = await post_file(c, setup, "big.txt", b"x" * 100_000)
    assert response.status_code == 413
    assert b"10 MB or smaller" in response.content


@pytest.mark.anyio
async def test_requests_from_another_origin_are_refused(setup):
    evil = {"origin": "http://evil.example", "accept": "text/html"}
    async with client() as c:
        upload = await c.post(
            f"{setup['url']}/suggest",
            files={"source_file": ("a.txt", b"WebRTC", "text/plain")},
            cookies=setup["admin"],
            headers=evil,
        )
        accept = await c.post(
            f"{setup['url']}/accept",
            data={"accept": ["0"], "source_0": "WebRTC", "target_0_de": "WebRTC"},
            cookies=setup["admin"],
            headers=evil,
        )
    assert upload.status_code == 403
    assert accept.status_code == 403
    assert await saved_entries(setup["room"]) == []


@pytest.mark.anyio
async def test_double_submit_saves_each_term_once(setup):
    data = {"accept": ["0"], "source_0": "WebRTC", "target_0_de": "WebRTC"}
    async with client() as c:
        responses = await asyncio.gather(
            *(c.post(f"{setup['url']}/accept", data=data, cookies=setup["admin"]) for _ in range(2))
        )
    assert sorted("vocab_imported=1" in str(response.next_request.url) for response in responses) == [False, True]
    assert len(await saved_entries(setup["room"])) == 1


@pytest.mark.anyio
async def test_booth_only_terms_do_not_block_the_room_glossary(setup):
    async with get_session() as session:
        booth = await create_booth(
            session, event_id=setup["event"], room_id=setup["room"], language_code="de", language_name="German"
        )
        session.add(
            AIVocabularyEntry(
                event_id=setup["event"],
                room_id=setup["room"],
                booth_id=booth.id,
                source_term="WebRTC",
                target_language="de",
                target_term="Booth",
            )
        )
    async with client() as c:
        suggested = await post_file(c, setup, "a.txt", b"The WebRTC talk")
        accept = await c.post(
            f"{setup['url']}/accept",
            data={"accept": ["0"], "source_0": "WebRTC", "target_0_de": "WebRTC"},
            cookies=setup["admin"],
            follow_redirects=False,
        )
    assert b'value="WebRTC"' in suggested.content
    assert "vocab_imported=1" in accept.headers["location"]


@pytest.mark.anyio
async def test_unsupported_upload_is_rejected_with_a_message(setup):
    async with client() as c:
        response = await post_file(c, setup, "slides.pptx", b"PK\x03\x04")
    assert response.status_code == 400
    assert b"Upload a plain text" in response.content


@pytest.mark.anyio
async def test_upload_without_a_file_is_rejected(setup):
    async with client() as c:
        response = await c.post(
            f"{setup['url']}/suggest",
            data={"x": "y"},
            cookies=setup["admin"],
            headers={"accept": "text/html"},
        )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_accepted_rows_appear_in_the_glossary_and_csv(setup):
    async with client() as c:
        accept = await c.post(
            f"{setup['url']}/accept",
            data={
                "accept": ["0"],
                "source_0": "WebRTC",
                "case_sensitive_0": "false",
                "target_0_de": "WebRTC",
                "source_1": "Docker",
                "target_1_de": "Docker",
            },
            cookies=setup["admin"],
            follow_redirects=False,
        )
        export = await c.get(f"{setup['url']}/export", cookies=setup["admin"])
    assert accept.status_code == 303
    assert "vocab_imported=1" in accept.headers["location"]
    assert [entry.source_term for entry in await saved_entries(setup["room"])] == ["WebRTC"]
    assert "WebRTC,de,WebRTC" in export.text
    assert "Docker" not in export.text


@pytest.mark.anyio
async def test_accepting_a_term_already_in_the_glossary_is_reported(setup):
    async with get_session() as session:
        session.add(
            AIVocabularyEntry(
                event_id=setup["event"],
                room_id=setup["room"],
                source_term="WebRTC",
                target_language="de",
                target_term="WebRTC",
            )
        )
    async with client() as c:
        accept = await c.post(
            f"{setup['url']}/accept",
            data={"accept": ["0"], "source_0": "webrtc", "target_0_de": "Other"},
            cookies=setup["admin"],
            follow_redirects=False,
        )
    assert "vocab_imported=0" in accept.headers["location"]
    assert "Existing+duplicate" in accept.headers["location"]
    assert len(await saved_entries(setup["room"])) == 1


@pytest.mark.anyio
async def test_translations_are_requested_only_when_ticked(setup, monkeypatch):
    provider = FakeProvider()
    monkeypatch.setitem(term_suggestions.PROVIDERS, "fake", provider)
    monkeypatch.setattr(
        "portal.routers.admin.suggestion_engine",
        lambda _event, _room: TranslationEngine("fake", "fake-model", "key"),
    )
    async with client() as c:
        await post_file(c, setup, "a.txt", b"The WebRTC talk")
        assert provider.texts == []
        response = await post_file(c, setup, "a.txt", b"The WebRTC talk", propose_translations="on")
    assert b'value="de-WebRTC"' in response.content
    assert provider.texts == ["1. WebRTC"]


@pytest.mark.anyio
async def test_room_page_offers_translations_only_with_a_usable_key(setup):
    room_url = f"/admin/events/{setup['event']}/rooms/{setup['room']}/"
    async with client() as c:
        without_key = await c.get(room_url, cookies=setup["admin"])
        async with get_session() as session:
            room = await get_room_by_id(session, setup["room"])
            event = await get_event_by_id(session, setup["event"])
            room.floor_translation_provider = "groq"
            room.floor_translation_model = "some-model"
            event.encrypted_groq_api_key = encrypt_val("secret")
        with_key = await c.get(room_url, cookies=setup["admin"])
    assert without_key.status_code == 200
    assert b"/ai-vocabulary/suggest" in without_key.content
    assert b'name="propose_translations"' not in without_key.content
    assert b'name="propose_translations"' in with_key.content


@pytest.mark.anyio
async def test_coordinators_are_not_offered_the_upload(setup):
    async with client() as c:
        page = await c.get(f"/admin/events/{setup['event']}/rooms/{setup['room']}/", cookies=setup["coordinator"])
    assert page.status_code == 200
    assert b"/ai-vocabulary/suggest" not in page.content


@pytest.mark.anyio
async def test_coordinators_cannot_upload_or_accept(setup):
    async with client() as c:
        upload = await post_file(c, setup, "a.txt", b"WebRTC", cookies=setup["coordinator"])
        accept = await c.post(
            f"{setup['url']}/accept",
            data={"accept": ["0"], "source_0": "WebRTC", "target_0_de": "WebRTC"},
            cookies=setup["coordinator"],
            follow_redirects=False,
        )
    assert upload.status_code == 403
    assert accept.status_code == 403
    assert await saved_entries(setup["room"]) == []


@pytest.mark.anyio
async def test_room_of_another_event_is_not_found(setup):
    async with get_session() as session:
        other_event = await create_event(session, slug="othercon", display_name="Other")
    url = f"/admin/events/{other_event.id}/rooms/{setup['room']}/ai-vocabulary"
    async with client() as c:
        upload = await c.post(
            f"{url}/suggest",
            files={"source_file": ("a.txt", b"WebRTC", "text/plain")},
            cookies=setup["admin"],
        )
        accept = await c.post(f"{url}/accept", data={"accept": ["0"]}, cookies=setup["admin"])
    assert upload.status_code == 404
    assert accept.status_code == 404


@pytest.mark.anyio
async def test_room_without_target_languages_offers_all_languages(setup):
    async with get_session() as session:
        for language in await session.scalars(
            select(RoomTranslationLanguage).where(RoomTranslationLanguage.room_id == setup["room"])
        ):
            language.enabled = False
    async with client() as c:
        response = await post_file(c, setup, "a.txt", b"The WebRTC talk")
    assert f"<th>{ALL_LANGUAGES[1]}</th>".encode() in response.content
    assert re.search(rb'name="target_0_all" value="WebRTC"', response.content)
