"""Home-page live demo: request validation, glossary, settings, utterance segmentation, the
session pipeline, the public /ws/demo/live endpoint (with fake engines, so no models are
loaded) and the admin settings page."""

from __future__ import annotations

import asyncio
import dataclasses

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from portal import live_demo, rate_limit
from portal.config import settings
from portal.routers.demo import router as demo_router
from portal.translations.vocabulary import VocabularyEntryInput

FRAME_SECONDS = 0.1
FRAME_SAMPLES = int(live_demo.SAMPLE_RATE * FRAME_SECONDS)
ORIGIN = {"origin": "http://testserver"}

DEMO = live_demo.DemoSettings(
    enabled=True,
    voice_enabled=True,
    whisper_model="base",
    max_sessions=2,
    max_session_seconds=120,
    sessions_per_ip_per_hour=12,
    max_targets=3,
)


def speech_frame(amplitude: int = 4000) -> bytes:
    t = np.arange(FRAME_SAMPLES) / live_demo.SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * amplitude).astype(np.int16).tobytes()


def silence_frame() -> bytes:
    return np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()


def lang(code: str) -> live_demo.DemoLanguage:
    return next(item for item in live_demo.demo_languages(voice=True) if item.code == code)


def keep(term: str, language: str = "all") -> VocabularyEntryInput:
    return VocabularyEntryInput(source_term=term, target_language=language, target_term=term)


@pytest.fixture(autouse=True)
def demo_state(monkeypatch):
    monkeypatch.setattr(settings, "live_demo_enabled", True)
    # Voice is decided by the installed package; pin it so tests behave the same everywhere.
    monkeypatch.setattr(live_demo, "voice_installed", lambda: True)
    monkeypatch.setattr(live_demo, "_active_sessions", 0)
    monkeypatch.setattr(live_demo, "_cached_settings", None)
    monkeypatch.setattr(rate_limit, "_rates", {})


# ── Request validation ────────────────────────────────────────────────────────


def test_languages_are_limited_to_the_local_translator_and_voice_follows_supertonic():
    languages = {item.code: item for item in live_demo.demo_languages(voice=True)}
    assert {"en", "es", "ja", "ko", "th"} <= set(languages)
    assert languages["es"].voice is True
    # Thai is translated by NLLB but Supertonic has no Thai voice.
    assert languages["th"].voice is False
    assert not any(item.voice for item in live_demo.demo_languages(voice=False))


def test_public_config_exposes_languages_limits_and_site_glossary_only():
    demo = dataclasses.replace(DEMO, glossary=(keep("VoxBento"),))
    config = live_demo.public_config(demo)
    assert config["enabled"] is True
    assert config["max_targets"] == 3
    assert config["max_seconds"] == 120
    assert {"code": "es", "name": "Spanish", "voice": True} in config["languages"]
    assert config["glossary"] == [{"term": "VoxBento", "lang": "all", "translation": "VoxBento"}]
    assert set(config) == {
        "enabled",
        "languages",
        "max_targets",
        "max_seconds",
        "sample_rate",
        "max_glossary",
        "glossary",
    }


def test_public_config_says_only_disabled_when_off():
    assert live_demo.public_config(dataclasses.replace(DEMO, enabled=False)) == {"enabled": False}
    assert live_demo.public_config(dataclasses.replace(DEMO, server_allowed=False)) == {"enabled": False}


def test_parse_start_accepts_a_valid_request_and_drops_the_spoken_language_from_targets():
    request = live_demo.parse_start(
        {"source": "en", "targets": ["es", "en", "ja", "es"], "listen": "ja", "names": "VoxBento, Eventyay"},
        DEMO,
    )
    assert request.source.code == "en"
    assert [t.code for t in request.targets] == ["es", "ja"]
    assert request.listen == "ja"
    assert request.hints == ("VoxBento", "Eventyay")
    assert request.whisper_model == "base"


@pytest.mark.parametrize(
    "message",
    [
        {"source": "xx", "targets": ["es"]},
        {"source": "en", "targets": "es"},
        {"source": "en", "targets": ["en"]},
        {"source": "en", "targets": ["zz"]},
        {"source": "en", "targets": ["es", "fr", "de", "it"]},
        {"source": "en", "targets": ["es"], "names": ["not", "text"]},
        {"source": "en", "targets": ["es"], "names": "x" * (live_demo.MAX_NAMES_CHARS + 1)},
        {"source": "en", "targets": ["es"], "glossary": "keynote"},
        {"source": "en", "targets": ["es"], "glossary": [{"term": ""}]},
        {"source": "en", "targets": ["es"], "glossary": [{"term": "keynote", "lang": "fr", "translation": "x"}]},
        {"source": "en", "targets": ["es"], "glossary": [{"term": "x" * 81}]},
        {"source": "en", "targets": ["es"], "glossary": [{"term": f"t{i}"} for i in range(11)]},
        {"source": "en", "targets": ["es"], "glossary": [{"term": "@SUM(A1)"}]},
    ],
)
def test_parse_start_rejects_requests_the_demo_does_not_offer(message):
    with pytest.raises(live_demo.DemoRequestError):
        live_demo.parse_start(message, DEMO)


def test_parse_start_uses_the_admins_language_limit():
    with pytest.raises(live_demo.DemoRequestError, match="up to 1"):
        live_demo.parse_start({"source": "en", "targets": ["es", "fr"]}, dataclasses.replace(DEMO, max_targets=1))


def test_listen_falls_back_to_none_for_a_captions_only_or_unchosen_language():
    assert live_demo.parse_start({"source": "en", "targets": ["th"], "listen": "th"}, DEMO).listen is None
    assert live_demo.parse_start({"source": "en", "targets": ["es"], "listen": "fr"}, DEMO).listen is None
    no_voice = dataclasses.replace(DEMO, voice_enabled=False)
    assert live_demo.parse_start({"source": "en", "targets": ["es"], "listen": "es"}, no_voice).listen is None


def test_parse_names_dedupes_and_caps_the_count():
    raw = "Ada, ada,  Grace   Hopper\nLinus," + ",".join(f"n{i}" for i in range(20))
    names = live_demo.parse_names(raw)
    assert names[:3] == ("Ada", "Grace Hopper", "Linus")
    assert len(names) == live_demo.MAX_NAMES


def test_clean_transcript_drops_noise_hallucinations():
    assert live_demo.clean_transcript("  Thank you. ") == ""
    assert live_demo.clean_transcript(" Hello   there ") == "Hello there"


# ── Glossary ──────────────────────────────────────────────────────────────────


def test_parse_glossary_reads_keep_all_and_per_language_lines():
    entries, errors = live_demo.parse_glossary(
        "# site terms\nVoxBento\n\nkeynote = plenary session\nkeynote = fr: séance plénière\nhttps = ok: no\n"
    )
    assert errors == []
    assert [(e.source_term, e.target_language, e.target_term) for e in entries] == [
        ("VoxBento", "all", "VoxBento"),
        ("keynote", "all", "plenary session"),
        ("keynote", "fr", "séance plénière"),
        # "ok" is not a language code, so the whole right-hand side is the translation.
        ("https", "all", "ok: no"),
    ]


def test_parse_glossary_reports_each_bad_line():
    _, errors = live_demo.parse_glossary("keynote =\nVoxBento\nvoxbento\n@SUM(A1)\nterm = fr: =cmd")
    assert errors[0].startswith("Line 1:")
    assert "already in the glossary" in errors[1]
    assert errors[2].startswith("Line 4:") and "formula" in errors[2]
    assert errors[3].startswith("Line 5:") and "formula" in errors[3]


def test_parse_glossary_caps_entries():
    entries, errors = live_demo.parse_glossary("\n".join(f"term{i}" for i in range(105)), limit=100)
    assert len(entries) == 100
    assert errors == ["The glossary can hold 100 entries."]


def test_visitor_entries_win_over_names_and_the_site_glossary():
    demo = dataclasses.replace(
        DEMO,
        glossary=(
            VocabularyEntryInput("keynote", "all", "plenary"),
            keep("VoxBento"),
        ),
    )
    request = live_demo.parse_start(
        {
            "source": "en",
            "targets": ["fr", "es"],
            "names": "Siobhan Nguyen",
            "glossary": [{"term": "Keynote", "lang": "all", "translation": "conférence"}],
        },
        demo,
    )
    terms = [(e.source_term, e.target_language, e.target_term) for e in request.glossary]
    assert terms == [
        ("Keynote", "all", "conférence"),
        ("Siobhan Nguyen", "all", "Siobhan Nguyen"),
        ("VoxBento", "all", "VoxBento"),
    ]
    assert request.hints == ("Keynote", "Siobhan Nguyen", "VoxBento")


def test_a_language_specific_entry_beats_an_all_languages_one():
    glossary = (VocabularyEntryInput("keynote", "all", "plenary"), VocabularyEntryInput("keynote", "fr", "plénière"))
    assert [e.target_term for e in live_demo.entries_for(glossary, "fr")] == ["plénière"]
    assert [e.target_term for e in live_demo.entries_for(glossary, "es")] == ["plenary"]


def test_speech_hints_fit_whispers_prompt():
    glossary = tuple(keep(f"term number {i}") for i in range(100))
    hints = live_demo.speech_hints(glossary)
    assert sum(len(h) + 2 for h in hints) <= live_demo.MAX_HINT_CHARS
    assert hints[0] == "term number 0"


def test_split_sentences():
    assert live_demo.split_sentences("Welcome to Eventyay. Visit us!  Thanks?  ") == [
        "Welcome to Eventyay.",
        "Visit us!",
        "Thanks?",
    ]
    assert live_demo.split_sentences("こんにちは。 ようこそ。") == ["こんにちは。", "ようこそ。"]


@pytest.mark.anyio
async def test_local_translate_sends_one_sentence_at_a_time_with_that_languages_glossary(monkeypatch):
    calls = []

    class FakeTranslator:
        async def translate(self, **kwargs):
            calls.append(kwargs)
            return f"<{kwargs['text']}>"

    monkeypatch.setattr(live_demo, "_translator", lambda: FakeTranslator())
    request = live_demo.DemoRequest(
        source=lang("en"),
        targets=(lang("fr"),),
        listen=None,
        glossary=(VocabularyEntryInput("keynote", "fr", "plénière"), VocabularyEntryInput("keynote", "es", "plenaria")),
    )
    result = await live_demo._local_translate("Welcome. The keynote starts.", request, lang("fr"))
    assert result == "<Welcome.> <The keynote starts.>"
    assert [call["text"] for call in calls] == ["Welcome.", "The keynote starts."]
    assert [e.target_term for e in calls[1]["vocabulary_entries"]] == ["plénière"]


@pytest.mark.anyio
async def test_local_translate_retries_clause_by_clause_when_a_glossary_term_is_lost(monkeypatch):
    calls = []

    class DroppingTranslator:
        """Like NLLB on a long sentence: the part with the term goes missing."""

        async def translate(self, **kwargs):
            text = kwargs["text"]
            calls.append(text)
            if "," in text:
                return "Bienvenue"
            return "Eventyay" if "Eventyay" in text else "Bienvenue"

    monkeypatch.setattr(live_demo, "_translator", lambda: DroppingTranslator())
    request = live_demo.DemoRequest(source=lang("en"), targets=(lang("ja"),), listen=None, glossary=(keep("Eventyay"),))
    result = await live_demo._local_translate("Welcome, see you at Eventyay", request, lang("ja"))
    assert result == "Bienvenue Eventyay"
    assert calls == ["Welcome, see you at Eventyay", "Welcome,", "see you at Eventyay"]


def test_missing_terms_counts_terms_said_but_not_kept():
    glossary = [keep("VoxBento"), VocabularyEntryInput("keynote", "fr", "plénière"), keep("Eventyay")]
    assert live_demo.missing_terms("The VoxBento keynote", "La plénière de ヴォックスベント", glossary) == 1
    assert live_demo.missing_terms("The VoxBento keynote", "La plénière VoxBento", glossary) == 0


# ── Settings ──────────────────────────────────────────────────────────────────


def test_env_defaults_apply_before_anything_is_saved(monkeypatch):
    monkeypatch.setattr(settings, "live_demo_max_sessions", 4)
    demo = live_demo.settings_from_row(None)
    assert demo.max_sessions == 4
    assert demo.live


def test_server_switch_overrides_saved_settings(monkeypatch):
    from portal.models import LiveDemoSettings

    monkeypatch.setattr(settings, "live_demo_enabled", False)
    row = LiveDemoSettings(
        id=1,
        enabled=True,
        voice_enabled=True,
        whisper_model="small",
        max_sessions=3,
        max_session_seconds=60,
        sessions_per_ip_per_hour=5,
        max_targets=2,
        glossary="VoxBento",
    )
    demo = live_demo.settings_from_row(row)
    assert demo.enabled and not demo.live
    assert demo.whisper_model == "small"
    assert [e.source_term for e in demo.glossary] == ["VoxBento"]


# ── Segmentation ──────────────────────────────────────────────────────────────


def test_segmenter_keeps_only_a_short_preroll_while_nobody_speaks():
    seg = live_demo.Segmenter()
    for _ in range(30):
        assert seg.push(silence_frame()) is None
    assert seg.seconds == pytest.approx(live_demo.PREROLL_SECONDS)
    assert seg.flush() is None


def test_segmenter_ends_an_utterance_after_a_pause():
    seg = live_demo.Segmenter()
    for _ in range(10):
        assert seg.push(speech_frame()) is None
    result = None
    for _ in range(10):
        result = seg.push(silence_frame()) or result
    assert result is not None
    assert len(result) / live_demo.BYTES_PER_SECOND >= 1.0
    assert not seg.has_speech
    assert seg.seconds <= live_demo.PREROLL_SECONDS


def test_segmenter_cuts_someone_who_never_pauses():
    seg = live_demo.Segmenter()
    cuts = [seg.push(speech_frame()) for _ in range(int(live_demo.HARD_CUT_SECONDS / FRAME_SECONDS))]
    assert sum(cut is not None for cut in cuts) == 1


def test_segmenter_ignores_steady_background_noise():
    seg = live_demo.Segmenter()
    for _ in range(50):
        seg.push(speech_frame(amplitude=200))
    assert not seg.has_speech


# ── Session pipeline ──────────────────────────────────────────────────────────


class FakeEngines:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def transcribe(self, audio: bytes, request: live_demo.DemoRequest, partial: bool) -> str:
        self.calls.append(("transcribe", partial))
        return "hello world"

    async def translate(self, text: str, request: live_demo.DemoRequest, target: live_demo.DemoLanguage) -> str:
        self.calls.append(("translate", target.code))
        return f"{text} [{target.code}]"

    async def synthesize(self, text: str, language: str) -> bytes:
        self.calls.append(("synthesize", language))
        return b"\x01\x00" * 10

    def engines(self) -> live_demo.Engines:
        return live_demo.Engines(self.transcribe, self.translate, self.synthesize)


def collector():
    sent: list[object] = []

    async def send_json(message):
        sent.append(message)

    async def send_bytes(data):
        sent.append(data)

    return sent, send_json, send_bytes


@pytest.mark.anyio
async def test_session_sends_caption_translations_and_voice_for_the_listened_language():
    fake = FakeEngines()
    sent, send_json, send_bytes = collector()
    request = live_demo.DemoRequest(source=lang("en"), targets=(lang("es"), lang("ja")), listen="ja")
    session = live_demo.LiveDemoSession(request, send_json, send_bytes, engines=fake.engines())
    session.start()
    for _ in range(8):
        session.push_audio(speech_frame())
    await session.finish()

    finals = [m for m in sent if isinstance(m, dict) and m["type"] == "final"]
    assert finals == [{"type": "final", "id": 1, "text": "hello world"}]
    translations = [(m["lang"], m["text"]) for m in sent if isinstance(m, dict) and m["type"] == "translation"]
    # The language being heard is translated first.
    assert translations == [("ja", "hello world [ja]"), ("es", "hello world [es]")]
    # Only the language being listened to is spoken; its header comes right before its audio.
    audio_index = next(i for i, m in enumerate(sent) if isinstance(m, dict) and m["type"] == "audio")
    assert sent[audio_index]["lang"] == "ja"
    assert sent[audio_index + 1] == b"\x01\x00" * 10
    assert ("synthesize", "es") not in fake.calls


@pytest.mark.anyio
async def test_session_skips_voice_after_switching_to_a_captions_only_language():
    fake = FakeEngines()
    sent, send_json, send_bytes = collector()
    request = live_demo.DemoRequest(source=lang("en"), targets=(lang("es"), lang("th")), listen="es")
    session = live_demo.LiveDemoSession(request, send_json, send_bytes, engines=fake.engines())
    session.set_listen("th")
    assert session.listen is None
    session.start()
    for _ in range(8):
        session.push_audio(speech_frame())
    await session.finish()
    assert not any(isinstance(m, bytes) for m in sent)


@pytest.mark.anyio
async def test_a_failing_translation_leaves_the_row_blank_and_the_rest_continue():
    fake = FakeEngines()

    async def broken_translate(text, request, target):
        if target.code == "es":
            raise RuntimeError("model crashed")
        return "ok"

    sent, send_json, send_bytes = collector()
    request = live_demo.DemoRequest(source=lang("en"), targets=(lang("es"), lang("fr")), listen=None)
    engines = live_demo.Engines(fake.transcribe, broken_translate, fake.synthesize)
    session = live_demo.LiveDemoSession(request, send_json, send_bytes, engines=engines)
    session.start()
    for _ in range(5):
        session.push_audio(speech_frame())
    await session.finish()
    translations = {m["lang"]: m["text"] for m in sent if isinstance(m, dict) and m["type"] == "translation"}
    assert translations == {"es": "", "fr": "ok"}


def test_admission_is_capped():
    assert live_demo.try_admit(2)
    assert live_demo.try_admit(2)
    assert not live_demo.try_admit(2)
    live_demo.release()
    assert live_demo.try_admit(2)


# ── WebSocket endpoint ────────────────────────────────────────────────────────


@pytest.fixture
def demo_config(monkeypatch):
    """The settings the endpoint sees; tests change fields on it."""
    holder = {"demo": DEMO}

    async def current() -> live_demo.DemoSettings:
        return holder["demo"]

    monkeypatch.setattr(live_demo, "current_settings", current)
    return holder


@pytest.fixture
def client(monkeypatch, demo_config):
    fake = FakeEngines()

    async def no_warm_up(demo) -> None:
        return None

    monkeypatch.setattr(live_demo, "LOCAL_ENGINES", fake.engines())
    monkeypatch.setattr(live_demo, "warm_up", no_warm_up)
    app = FastAPI()
    app.include_router(demo_router)
    return TestClient(app)


START = {"type": "start", "source": "en", "targets": ["es"], "listen": "es", "names": ""}


def test_config_endpoint_follows_the_admin_switch(client, demo_config):
    response = client.get("/api/demo/live/config")
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.headers["cache-control"] == "no-store"
    demo_config["demo"] = dataclasses.replace(DEMO, enabled=False)
    assert client.get("/api/demo/live/config").json() == {"enabled": False}


def test_socket_runs_a_whole_demo(client):
    with client.websocket_connect("/ws/demo/live", headers=ORIGIN) as ws:
        ws.send_json({**START, "glossary": [{"term": "keynote", "lang": "es", "translation": "plenaria"}]})
        assert ws.receive_json() == {"type": "status", "state": "warming"}
        assert ws.receive_json()["type"] == "ready"
        for _ in range(6):
            ws.send_bytes(speech_frame())
        ws.send_json({"type": "stop"})
        messages = []
        while True:
            message = ws.receive()
            if message["type"] == "websocket.close":
                break
            messages.append(message.get("text") or message.get("bytes"))
    texts = [m for m in messages if isinstance(m, str)]
    assert any('"type":"final"' in t.replace(" ", "") for t in texts)
    assert any('"type":"translation"' in t.replace(" ", "") for t in texts)
    assert any(isinstance(m, bytes) for m in messages)
    assert '"reason":"stopped"' in texts[-1].replace(" ", "")
    assert live_demo.active_sessions() == 0


def test_socket_refuses_other_origins(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/demo/live", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert exc.value.code == 4003


def test_socket_refuses_when_switched_off(client, demo_config):
    demo_config["demo"] = dataclasses.replace(DEMO, enabled=False)
    with client.websocket_connect("/ws/demo/live", headers=ORIGIN) as ws:
        assert ws.receive_json()["code"] == "disabled"


def test_socket_refuses_when_busy(client, monkeypatch):
    monkeypatch.setattr(live_demo, "_active_sessions", DEMO.max_sessions)
    with client.websocket_connect("/ws/demo/live", headers=ORIGIN) as ws:
        assert ws.receive_json()["code"] == "busy"


def test_socket_rate_limits_each_address_and_frees_its_slot(client, demo_config):
    demo_config["demo"] = dataclasses.replace(DEMO, sessions_per_ip_per_hour=1)
    with client.websocket_connect("/ws/demo/live", headers=ORIGIN) as ws:
        ws.send_json({**START, "source": "xx"})
        assert ws.receive_json()["code"] == "invalid"
    with client.websocket_connect("/ws/demo/live", headers=ORIGIN) as ws:
        assert ws.receive_json()["code"] == "rate_limited"
    assert live_demo.active_sessions() == 0


def test_socket_ends_the_run_at_the_time_limit(client, demo_config):
    demo_config["demo"] = dataclasses.replace(DEMO, max_session_seconds=0.3)
    with client.websocket_connect("/ws/demo/live", headers=ORIGIN) as ws:
        ws.send_json(START)
        ws.receive_json()
        assert ws.receive_json()["type"] == "ready"
        assert ws.receive_json() == {"type": "done", "reason": "time_limit"}


@pytest.mark.anyio
async def test_warm_up_loads_each_model_once_per_choice(monkeypatch):
    loads: list[str] = []
    monkeypatch.setattr(live_demo, "_warmed", set())
    monkeypatch.setattr(live_demo, "get_whisper_model", lambda name: loads.append(f"whisper:{name}"))
    monkeypatch.setattr(live_demo, "get_model_and_tokenizer", lambda name: loads.append(f"nllb:{name}"))

    async def fake_synthesize(text, language):
        loads.append("voice")
        return b""

    monkeypatch.setattr(live_demo, "_local_synthesize", fake_synthesize)
    await asyncio.gather(live_demo.warm_up(DEMO), live_demo.warm_up(DEMO))
    nllb = f"nllb:{settings.live_demo_translation_model}"
    assert loads == ["whisper:base", nllb, "voice"]
    assert live_demo.models_ready(DEMO)
    await live_demo.warm_up(dataclasses.replace(DEMO, whisper_model="tiny", voice_enabled=False))
    assert loads[3:] == ["whisper:tiny", nllb]


# ── Admin page ────────────────────────────────────────────────────────────────


@pytest.fixture
async def admin_http(auto_csrf):
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app
    from portal.auth import create_user_token, hash_password
    from portal.database import configure, create_user, dispose, get_session, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as s:
        admin = await create_user(
            s, email="admin@example.com", display_name="Admin", password_hash=hash_password("x" * 12), is_admin=True
        )
        member = await create_user(
            s, email="member@example.com", display_name="Member", password_hash=hash_password("x" * 12)
        )
    tokens = {
        "admin": create_user_token(user_id=admin.id, email="admin@example.com", is_admin=True),
        "member": create_user_token(user_id=member.id, email="member@example.com", is_admin=False),
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"origin": "http://test"}
    ) as http:
        yield http, tokens
    await dispose()


FORM = {
    "enabled": "on",
    "voice_enabled": "on",
    "whisper_model": "small",
    "max_sessions": "3",
    "max_session_seconds": "90",
    "sessions_per_ip_per_hour": "20",
    "max_targets": "2",
    "glossary": "VoxBento\nkeynote = fr: séance plénière",
}


@pytest.mark.anyio
async def test_admin_saves_settings_and_the_demo_uses_them(admin_http):
    http, tokens = admin_http
    http.cookies.set("user_token", tokens["admin"])
    page = await http.get("/admin/live-demo/")
    assert page.status_code == 200
    assert 'name="glossary"' in page.text

    saved = await http.post("/admin/live-demo/", data=FORM)
    assert saved.status_code == 303
    assert saved.headers["location"] == "/admin/live-demo/?saved=1"

    live_demo.forget_cached_settings()
    demo = await live_demo.current_settings()
    assert (demo.whisper_model, demo.max_sessions, demo.max_session_seconds, demo.max_targets) == ("small", 3, 90, 2)
    assert [(e.source_term, e.target_language) for e in demo.glossary] == [("VoxBento", "all"), ("keynote", "fr")]

    config = (await http.get("/api/demo/live/config")).json()
    assert config["max_targets"] == 2
    assert {"term": "keynote", "lang": "fr", "translation": "séance plénière"} in config["glossary"]

    off = await http.post("/admin/live-demo/", data={**FORM, "enabled": ""})
    assert off.status_code == 303
    assert (await http.get("/api/demo/live/config")).json() == {"enabled": False}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_sessions", "0", "Visitors at once must be between 1 and 20."),
        ("max_session_seconds", "soon", "Time limit must be a whole number."),
        ("whisper_model", "large-v3", "Choose one of the listed speech models."),
        ("glossary", "keynote =", "Glossary: Line 1"),
    ],
)
async def test_admin_form_refuses_bad_values_without_saving(admin_http, field, value, message):
    http, tokens = admin_http
    http.cookies.set("user_token", tokens["admin"])
    response = await http.post("/admin/live-demo/", data={**FORM, field: value})
    assert response.status_code == 400
    assert message in response.text
    live_demo.forget_cached_settings()
    assert (await live_demo.current_settings()).max_sessions == settings.live_demo_max_sessions


@pytest.mark.anyio
async def test_only_admins_reach_the_settings(admin_http):
    http, tokens = admin_http
    http.cookies.set("user_token", tokens["member"])
    assert (await http.get("/admin/live-demo/", follow_redirects=False)).status_code in {302, 303, 403}
    assert (await http.post("/admin/live-demo/", data=FORM, follow_redirects=False)).status_code in {302, 303, 403}
    live_demo.forget_cached_settings()
    assert (await live_demo.current_settings()).whisper_model == settings.live_demo_whisper_model


@pytest.mark.anyio
async def test_admin_can_load_the_models_ahead_of_visitors(admin_http, monkeypatch):
    http, tokens = admin_http
    http.cookies.set("user_token", tokens["admin"])
    warmed = asyncio.Event()

    async def fake_warm_up(demo) -> None:
        warmed.set()

    monkeypatch.setattr(live_demo, "warm_up", fake_warm_up)
    monkeypatch.setattr(live_demo, "_warmed", set())
    response = await http.post("/admin/live-demo/warm-up")
    assert response.status_code == 303
    await asyncio.wait_for(warmed.wait(), 1)
