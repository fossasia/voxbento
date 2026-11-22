from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token
from portal.config import settings
from portal.database import configure, create_booth, create_event, create_room, dispose, get_session, init_db
from portal.models import Room, User
from portal.routers import admin as admin_router
from portal.routers import translation_models as models_router
from portal.translations import worker as worker_module
from portal.translations.engine import EngineKind, TranslationEngine
from portal.translations.metrics import (
    FAILED,
    FAILING,
    HEALTHY,
    IDLE,
    LAGGING,
    STALE,
    TRANSLATED,
    TranslationMetrics,
    percentile,
    room_state,
)
from portal.translations.worker import TranslationWorker


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_percentile_uses_the_nearest_rank():
    assert percentile([], 0.5) is None
    assert percentile([3.0], 0.95) == 3.0
    values = [float(n) for n in range(1, 21)]
    assert percentile(values, 0.5) == 10.0
    assert percentile(values, 0.95) == 19.0
    assert percentile(reversed(values), 0.95) == 19.0


@pytest.mark.parametrize(
    ("counts", "lag_p95", "expected"),
    [
        ((0, 0, 0), None, IDLE),
        ((10, 0, 0), 0.2, HEALTHY),
        ((10, 1, 0), 0.2, HEALTHY),
        ((1, 0, 0), None, HEALTHY),
        ((9, 0, 1), 0.2, LAGGING),
        ((10, 0, 0), 2.5, LAGGING),
        ((2, 2, 0), 0.1, FAILING),
        ((0, 1, 0), None, HEALTHY),
        ((0, 2, 0), None, FAILING),
        ((3, 2, 5), 3.0, LAGGING),
    ],
)
def test_room_state(counts, lag_p95, expected):
    assert room_state(*counts, lag_p95, 4.0) == expected


def test_a_room_without_a_lag_budget_is_never_lagging_from_its_start_lag():
    assert room_state(5, 0, 0, 30.0, None) == HEALTHY


def test_outcomes_older_than_the_window_are_forgotten():
    clock = Clock()
    metrics = TranslationMetrics(window=60, clock=clock)
    metrics.record("local", "nllb", 1, FAILED)
    clock.now += 30
    metrics.record("local", "nllb", 1, TRANSLATED, start_lag=0.1, latency=0.5)
    clock.now += 31

    assert [outcome.kind for outcome in metrics.recent()] == [TRANSLATED]
    clock.now += 30
    assert metrics.recent() == []
    assert metrics.room(1, 4.0).state == IDLE


def test_stats_are_grouped_by_service_and_model():
    clock = Clock()
    metrics = TranslationMetrics(clock=clock)
    for latency in (0.2, 0.4, 0.6, 0.8):
        metrics.record("local", "nllb", 1, TRANSLATED, start_lag=latency / 2, latency=latency)
    metrics.record("local", "nllb", 2, STALE, start_lag=5.0)
    metrics.record("openai", "gpt-4o-mini", 2, FAILED, start_lag=0.0, latency=12.0)

    local, openai = metrics.by_model()

    assert (local.provider, local.model) == ("local", "nllb")
    assert (local.translated, local.failed, local.stale) == (4, 0, 1)
    assert local.latency_p50 == 0.4
    assert local.latency_p95 == 0.8
    assert local.start_lag_p95 == 5.0
    assert (openai.provider, openai.model, openai.failed) == ("openai", "gpt-4o-mini", 1)


def test_room_health_counts_only_that_room():
    metrics = TranslationMetrics(clock=Clock())
    metrics.record("local", "nllb", 1, TRANSLATED, start_lag=0.1, latency=0.3)
    metrics.record("local", "nllb", 2, FAILED)
    metrics.record("local", "nllb", 2, FAILED)

    assert metrics.room(1, 4.0).state == HEALTHY
    two = metrics.room(2, 4.0)
    assert (two.state, two.translated, two.failed, two.stale) == (FAILING, 0, 2, 0)


def test_every_outcome_in_the_window_is_kept_and_older_ones_are_let_go_while_recording():
    clock = Clock()
    metrics = TranslationMetrics(window=60, clock=clock)
    for _ in range(30_000):
        metrics.record("local", "nllb", 1, TRANSLATED)
    assert metrics.room(1, None).translated == 30_000

    clock.now += 61
    metrics.record("local", "nllb", 1, FAILED)
    assert len(metrics._outcomes) == 1


def test_a_full_window_keeps_the_latest_outcomes_and_says_it_is_incomplete():
    clock = Clock()
    metrics = TranslationMetrics(window=60, max_outcomes=3, clock=clock)
    for kind in (FAILED, FAILED, TRANSLATED):
        metrics.record("local", "nllb", 1, kind)
    assert metrics.complete

    metrics.record("local", "nllb", 1, TRANSLATED)
    metrics.record("local", "nllb", 1, TRANSLATED)

    assert len(metrics.recent()) == 3
    assert metrics.room(1, None).failed == 0
    assert not metrics.complete
    clock.now += 61
    assert metrics.complete


def test_coverage_is_complete_again_once_the_dropped_outcome_would_have_expired():
    clock = Clock()
    metrics = TranslationMetrics(window=60, max_outcomes=3, clock=clock)
    for _ in range(3):
        metrics.record("local", "nllb", 1, TRANSLATED)
    clock.now += 59
    metrics.record("local", "nllb", 1, TRANSLATED)
    assert not metrics.complete

    clock.now += 2
    assert metrics.complete


# ── Through the worker ──


class Engine(TranslationEngine):
    kind = EngineKind.MT_API

    def __init__(self, answer: str | None = "", *, hold: asyncio.Event | None = None, delay: float = 0.0) -> None:
        self.max_concurrency = 1
        self.answer = answer
        self.hold = hold
        self.delay = delay

    async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
        if self.hold is not None:
            await self.hold.wait()
        await asyncio.sleep(self.delay)
        return [[self.answer for _ in targets] for _ in texts]


def fake_room(room_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=room_id,
        floor_ai_vocabulary_enabled=False,
        floor_ai_interpreter_persona=None,
        floor_ai_interpretation_style=None,
    )


@pytest.fixture
def metrics(monkeypatch):
    fresh = TranslationMetrics()
    monkeypatch.setattr(worker_module, "translation_metrics", fresh)
    monkeypatch.setattr("portal.websockets.manager.tts_manager.broadcast_bundle", AsyncMock())
    return fresh


def translate(provider: str, room_id: int = 1, lang: str = "fr"):
    return TranslationWorker(MagicMock())._translate_and_broadcast(
        SimpleNamespace(id=1),
        fake_room(room_id),
        provider,
        "m",
        None,
        lang,
        lang,
        "en",
        "English",
        1,
        "Hello",
        f"event-{room_id}-floor",
        "uuid",
        1,
    )


@pytest.mark.anyio
async def test_a_translated_segment_records_its_start_lag_and_inference_time(monkeypatch, metrics):
    monkeypatch.setitem(worker_module.ENGINES, "metrics-ok", Engine("", delay=0.1))
    monkeypatch.setattr(settings, "translation_max_lag_seconds", 0)

    await asyncio.gather(translate("metrics-ok", 1, "fr"), translate("metrics-ok", 1, "de"))

    first, second = metrics.recent()
    assert (first.provider, first.model, first.room_id, first.kind) == ("metrics-ok", "m", 1, TRANSLATED)
    assert first.start_lag < 0.05
    assert 0.1 <= first.latency < 0.5
    assert second.kind == TRANSLATED
    assert 0.1 <= second.start_lag < 0.5
    assert 0.1 <= second.latency < 0.5


@pytest.mark.anyio
async def test_a_translation_that_cannot_be_saved_is_a_failure_with_its_timings(monkeypatch, metrics):
    monkeypatch.setitem(worker_module.ENGINES, "metrics-unsaved", Engine("Bonjour", delay=0.05))

    def broken_session():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(worker_module, "get_session", broken_session)

    await translate("metrics-unsaved")

    [outcome] = metrics.recent()
    assert outcome.kind == FAILED
    assert outcome.latency >= 0.05
    assert outcome.start_lag is not None


@pytest.mark.anyio
async def test_a_segment_dropped_while_the_model_downloads_is_a_failure(monkeypatch, metrics):
    monkeypatch.setitem(worker_module.ENGINES, "metrics-downloading", Engine(""))
    monkeypatch.setattr(worker_module, "_inference_timeout", lambda provider, model: None)

    await translate("metrics-downloading")

    [outcome] = metrics.recent()
    assert (outcome.kind, outcome.latency) == (FAILED, None)


@pytest.mark.anyio
async def test_an_engine_with_no_answer_is_recorded_as_a_failure(monkeypatch, metrics):
    monkeypatch.setitem(worker_module.ENGINES, "metrics-none", Engine(None))

    await translate("metrics-none")

    [outcome] = metrics.recent()
    assert outcome.kind == FAILED
    assert outcome.latency is not None


@pytest.mark.anyio
async def test_a_timed_out_call_is_recorded_as_a_failure_with_its_time(monkeypatch, metrics):
    release = asyncio.Event()
    monkeypatch.setitem(worker_module.ENGINES, "metrics-slow", Engine("late", hold=release))
    monkeypatch.setattr(worker_module, "_inference_timeout", lambda provider, model: 0.05)

    await translate("metrics-slow")
    release.set()

    [outcome] = metrics.recent()
    assert outcome.kind == FAILED
    assert outcome.latency >= 0.05


@pytest.mark.anyio
async def test_a_stale_segment_is_recorded_apart_from_failures(monkeypatch, metrics):
    release = asyncio.Event()
    monkeypatch.setitem(worker_module.ENGINES, "metrics-held", Engine("", hold=release))
    monkeypatch.setattr(settings, "translation_max_lag_seconds", 0.05)

    first = asyncio.create_task(translate("metrics-held", 1, "fr"))
    for _ in range(5):
        await asyncio.sleep(0)
    await translate("metrics-held", 2, "de")
    release.set()
    await first

    stale = [outcome for outcome in metrics.recent() if outcome.room_id == 2]
    assert [outcome.kind for outcome in stale] == [STALE]
    assert stale[0].start_lag >= 0.05
    assert metrics.room(2, 0.05).state == LAGGING
    assert metrics.room(1, 0.05).state == HEALTHY


@pytest.mark.anyio
async def test_a_segment_dropped_from_a_full_queue_is_a_failure(monkeypatch, metrics):
    monkeypatch.setitem(worker_module.ENGINES, "metrics-full", Engine(""))
    scheduler = worker_module.scheduler_for("metrics-full", "m", 1)
    held = [scheduler.admit(1, "event-1-floor:fr") for _ in range(scheduler.queue_limit)]

    await translate("metrics-full")
    for job in held:
        job.close()

    [outcome] = metrics.recent()
    assert (outcome.kind, outcome.start_lag, outcome.latency) == (FAILED, None, None)


# ── Admins and Mission Control ──


@pytest.fixture
async def world():
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as s:
        admin = User(email="metrics-admin@example.com", display_name="Admin", is_admin=True, email_verified=True)
        outsider = User(email="metrics-outsider@example.com", display_name="Outsider", email_verified=True)
        s.add_all([admin, outsider])
        event = await create_event(s, slug="metricscon", display_name="MetricsCon")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        quiet = await create_room(s, event_id=event.id, display_name="Side Room")
        booth = await create_booth(s, event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
        booth.translation_enabled = True
        await s.flush()
        yield {
            "admin": {"user_token": create_user_token(user_id=admin.id, email=admin.email, is_admin=True)},
            "outsider": {"user_token": create_user_token(user_id=outsider.id, email=outsider.email)},
            "room": room.id,
            "quiet": quiet.id,
        }
    await dispose()


def client(cookies: dict[str, str]) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookies)


@pytest.mark.anyio
async def test_mission_control_reads_a_rooms_translation_health(monkeypatch, world):
    metrics = TranslationMetrics()
    monkeypatch.setattr(admin_router, "translation_metrics", metrics)
    monkeypatch.setattr(settings, "translation_max_lag_seconds", 4.0)
    for _ in range(3):
        metrics.record("local", "nllb", world["room"], TRANSLATED, start_lag=0.1, latency=0.4)
    metrics.record("local", "nllb", world["room"], STALE, start_lag=4.2)

    async with client(world["admin"]) as c:
        lagging = await c.get(f"/api/rooms/{world['room']}/translation-health")
        idle = await c.get(f"/api/rooms/{world['quiet']}/translation-health")

    assert lagging.status_code == 200
    assert lagging.json() == {
        "state": LAGGING,
        "translated": 3,
        "failed": 0,
        "stale": 1,
        "window_seconds": 300,
        "complete": True,
    }
    assert idle.json()["state"] == IDLE


@pytest.mark.anyio
async def test_only_people_who_run_the_room_see_its_translation_health(world):
    async with client(world["outsider"]) as c:
        res = await c.get(f"/api/rooms/{world['room']}/translation-health")

    assert res.status_code == 403


@pytest.mark.anyio
async def test_mission_control_shows_translation_only_for_rooms_that_translate(world):
    async with client(world["admin"]) as c:
        res = await c.get("/mission-control/metricscon/")

    assert res.status_code == 200
    page = res.text
    main = page.split('"name": "Main Hall"')[1].split("}")[0]
    side = page.split('"name": "Side Room"')[1].split("}")[0]
    assert '"translation": true' in main
    assert '"translation": false' in side


@pytest.mark.anyio
async def test_admins_see_translation_health_on_the_models_page(monkeypatch, world):
    metrics = TranslationMetrics()
    monkeypatch.setattr(models_router, "translation_metrics", metrics)
    monkeypatch.setattr(models_router, "loaded_model_ids", lambda: ["nllb-200-distilled-600M"])
    monkeypatch.setitem(worker_module.SCHEDULERS, ("metrics-page", "event-7"), worker_module.TranslationScheduler(3))
    metrics.record("local", "nllb-200-distilled-600M", world["room"], TRANSLATED, start_lag=0.25, latency=1.5)
    metrics.record("local", "nllb-200-distilled-600M", world["room"], STALE, start_lag=4.5)

    async with client(world["admin"]) as c:
        res = await c.get("/admin/translation-models/")

    assert res.status_code == 200
    health = res.text.split('id="translation-health"')[1]
    assert "<code>metrics-page</code>" in health
    assert "0 of 3" in health
    assert "<td>Event 7</td>" in health
    row = health.split("<code>nllb-200-distilled-600M</code>")[1].split("</tr>")[0]
    assert "<td>1</td>" in row
    assert "4.5 s" in row
    assert "1.5 s / 1.5 s" in row
    assert "About 600 MB in total" in health
    assert "Very busy" not in health


@pytest.mark.anyio
async def test_the_models_page_says_when_the_cap_cuts_into_the_window(monkeypatch, world):
    metrics = TranslationMetrics(max_outcomes=2)
    for _ in range(3):
        metrics.record("local", "nllb", world["room"], TRANSLATED, start_lag=0.1, latency=0.2)
    monkeypatch.setattr(models_router, "translation_metrics", metrics)

    async with client(world["admin"]) as c:
        res = await c.get("/admin/translation-models/")

    assert "only the latest 2 segments of the last 5 minutes are counted" in res.text


@pytest.mark.anyio
async def test_the_models_page_says_when_nothing_was_translated(monkeypatch, world):
    monkeypatch.setattr(models_router, "translation_metrics", TranslationMetrics())
    monkeypatch.setattr(models_router, "loaded_model_ids", lambda: [])

    async with client(world["admin"]) as c:
        res = await c.get("/admin/translation-models/")

    health = res.text.split('id="translation-health"')[1]
    assert "No translations in the last 5 minutes." in health
    assert "None. A model loads when a room first translates with it." in health


@pytest.mark.anyio
async def test_a_room_translating_past_the_first_hundred_booths_still_gets_its_status(world):
    async with get_session() as s:
        event_id = (await s.get(Room, world["room"])).event_id
        for index in range(50):
            room = await create_room(s, event_id=event_id, display_name=f"Hall {index}")
            await create_booth(s, event_id=event_id, room_id=room.id, language_code="da", language_name="Danish")
            await create_booth(s, event_id=event_id, room_id=room.id, language_code="de", language_name="German")
        late = await create_room(s, event_id=event_id, display_name="Late Room")
        booth = await create_booth(s, event_id=event_id, room_id=late.id, language_code="es", language_name="Spanish")
        booth.translation_enabled = True

    async with client(world["admin"]) as c:
        page = (await c.get("/mission-control/metricscon/")).text

    assert '"translation": true' in page.split('"name": "Late Room"')[1].split("}")[0]
