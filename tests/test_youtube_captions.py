"""Native YouTube caption posting (#1087, plan 006): queue, chunking and delivery."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from portal import youtube_captions as yc

CAPTION_URL = "https://upload.youtube.com/closedcaption?cid=SECRETCID123"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    for store in (yc.targets, yc.health, yc.clock_offset_ms, yc._queues, yc._tasks, yc._last_seq):
        store.clear()
    monkeypatch.setattr(yc, "RETRY_DELAY_S", 0)
    yield
    for task in list(yc._tasks.values()):
        task.cancel()
    for store in (yc.targets, yc.health, yc.clock_offset_ms, yc._queues, yc._tasks, yc._last_seq):
        store.clear()
    yc._transport = None


def mock_youtube(monkeypatch, handler):
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(yc, "_transport", httpx.MockTransport(record))
    return requests


async def drain(room_id: int, expected: int, requests: list, timeout: float = 2.0) -> None:
    """Wait until the sender has made `expected` requests."""
    async with asyncio.timeout(timeout):
        while len(requests) < expected:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)  # let the sender record health after the last response


def test_offer_is_synchronous():
    assert not inspect.iscoroutinefunction(yc.offer)


def test_chunks_split_at_word_boundaries_and_fit():
    pieces = yc.chunks("word " * 100)
    assert len(pieces) > 1
    assert all(len(piece) <= yc.CHUNK_CHARS for piece in pieces)
    assert " ".join(pieces).split() == ["word"] * 100


def test_chunks_collapse_whitespace_and_newlines():
    assert yc.chunks("  hello \n\n world\t again ") == ["hello world again"]


def test_chunks_split_one_unbroken_long_word():
    pieces = yc.chunks("x" * 200)
    assert all(len(piece) <= yc.CHUNK_CHARS for piece in pieces)
    assert "".join(pieces) == "x" * 200


def test_chunks_empty_text():
    assert yc.chunks("   \n ") == []


@pytest.mark.anyio
async def test_offer_without_target_is_a_noop():
    yc.offer(1, "floor", "hello")
    assert yc._queues == {}
    assert yc._tasks == {}


@pytest.mark.anyio
async def test_offer_for_another_language_is_a_noop():
    await yc.activate(1, "de", CAPTION_URL)
    yc.offer(1, "floor", "hello")
    assert yc._queues == {}
    assert yc._tasks == {}


@pytest.mark.anyio
async def test_offer_ignores_blank_text():
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "   ")
    assert yc._queues == {}


@pytest.mark.anyio
async def test_queue_overflow_drops_the_oldest(monkeypatch):
    await yc.activate(1, "floor", CAPTION_URL)
    # Fill the queue without letting the sender run: no task is created for a full pre-seeded queue.
    queue = yc._queues.setdefault(1, asyncio.Queue(maxsize=yc.MAX_QUEUE))
    for index in range(yc.MAX_QUEUE):
        queue.put_nowait((index, f"line {index}"))
    monkeypatch.setattr(yc, "_sender", lambda room_id: asyncio.sleep(3600))
    yc.offer(1, "floor", "newest", received_at_ms=999)
    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert len(items) == yc.MAX_QUEUE
    assert items[0] == (1, "line 1")
    assert items[-1] == (999, "newest")


@pytest.mark.anyio
async def test_sender_posts_in_order_with_incrementing_seq(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "first", received_at_ms=1_700_000_000_000)
    yc.offer(1, "floor", "second", received_at_ms=1_700_000_005_000)
    await drain(1, 2, requests)
    assert [request.url.params["seq"] for request in requests] == ["1", "2"]
    assert requests[0].url.params["cid"] == "SECRETCID123"
    assert all(request.headers["content-type"] == "text/plain; charset=utf-8" for request in requests)
    assert b"first" in requests[0].content
    assert b"second" in requests[1].content
    assert yc.health[1].state == "posting"
    assert yc.health[1].last_ok_at is not None


@pytest.mark.anyio
async def test_seq_uses_question_mark_when_url_has_no_query(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", "https://upload.youtube.com/closedcaption")
    yc.offer(1, "floor", "hello")
    await drain(1, 1, requests)
    assert str(requests[0].url).endswith("/closedcaption?seq=1")


@pytest.mark.anyio
async def test_long_text_is_posted_as_chunks_with_spaced_timestamps(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", CAPTION_URL)
    base = int(time.time() * 1000)  # a recent timestamp: older ones are clamped
    yc.offer(1, "floor", "word " * 40, received_at_ms=base)
    expected = len(yc.chunks("word " * 40))
    assert expected > 1
    await drain(1, expected, requests)
    stamps = [request.content.decode().splitlines()[0] for request in requests]
    assert stamps[0] == yc.format_timestamp(base)
    assert stamps[1] == yc.format_timestamp(base + yc.CHUNK_SPACING_MS)


@pytest.mark.anyio
async def test_rejection_marks_failing_and_continues(monkeypatch):
    statuses = iter([400, 200])
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(next(statuses)))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "rejected")
    yc.offer(1, "floor", "accepted")
    await drain(1, 2, requests)
    assert len(requests) == 2  # a 400 is not retried
    assert yc.health[1].state == "posting"


@pytest.mark.anyio
async def test_rejection_reason_names_the_status(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(403))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "nope")
    await drain(1, 1, requests)
    assert yc.health[1].state == "failing"
    assert "HTTP 403" in yc.health[1].reason


@pytest.mark.anyio
async def test_server_error_is_retried_once_then_dropped(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(503))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "flaky")
    await drain(1, 2, requests)
    await asyncio.sleep(0.05)
    assert len(requests) == 2
    assert yc.health[1].state == "failing"


@pytest.mark.anyio
async def test_network_error_is_retried_once_then_dropped(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")

    requests = mock_youtube(monkeypatch, boom)
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "offline")
    await drain(1, 2, requests)
    await asyncio.sleep(0.05)
    assert len(requests) == 2
    assert yc.health[1].state == "failing"
    assert yc.health[1].reason == yc.UNREACHABLE_REASON


@pytest.mark.anyio
async def test_caption_url_is_never_logged(monkeypatch, caplog):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(500))
    await yc.activate(1, "floor", CAPTION_URL)
    with caplog.at_level(logging.DEBUG):
        yc.offer(1, "floor", "secret test")
        await drain(1, 2, requests)
        await asyncio.sleep(0.05)
    assert "SECRETCID123" not in caplog.text
    assert "upload.youtube.com" not in caplog.text
    assert "secret test" not in caplog.text
    assert "SECRETCID123" not in repr(yc.targets[1])


@pytest.mark.anyio
async def test_caption_url_is_not_logged_when_httpx_normalises_it(monkeypatch, caplog):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    # httpx lower-cases the host and drops the default port before it logs the request.
    await yc.activate(1, "floor", "HTTPS://Upload.YouTube.com:443/closedcaption?cid=SECRETCID123")
    with caplog.at_level(logging.DEBUG):
        yc.offer(1, "floor", "hello")
        await drain(1, 1, requests)
        await asyncio.sleep(0.05)
    assert "SECRETCID123" not in caplog.text
    assert "upload.youtube.com" not in caplog.text.lower()


def test_the_log_filter_uses_secrets_worked_out_once_per_target(monkeypatch):
    target = yc.CaptionTarget(language="floor", url="https://Upload.YouTube.com:443/closedcaption?cid=SECRETCID123")
    yc.targets[1] = target
    calls = []
    monkeypatch.setattr(yc, "_secret_forms", lambda url: calls.append(url) or frozenset())
    record = logging.LogRecord("httpx", logging.INFO, __file__, 0, "HTTP Request: POST %s", (target.url,), None)

    hidden = [not yc._HideCaptionUrls().filter(record) for _ in range(3)]

    assert hidden == [True, True, True]
    assert calls == []  # nothing is recomputed per log record
    assert "SECRETCID123" in target.secrets
    assert "https://upload.youtube.com/closedcaption?cid=SECRETCID123" in target.secrets
    assert "SECRETCID123" not in repr(target)


@pytest.mark.anyio
async def test_activate_upgrades_a_mixed_case_http_scheme():
    await yc.activate(1, "floor", "HTTP://upload.youtube.com/closedcaption?cid=SECRETCID123")
    assert yc.targets[1].url.startswith("https://upload.youtube.com/")


@pytest.mark.anyio
async def test_reactivating_a_stream_keeps_counting_the_seq(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "one")
    yc.offer(1, "floor", "two")
    await drain(1, 2, requests)
    await yc.deactivate(1)
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "three")
    await drain(1, 3, requests)
    assert [r.url.params["seq"] for r in requests] == ["1", "2", "3"]


@pytest.mark.anyio
async def test_a_different_stream_starts_counting_from_one(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "one")
    await drain(1, 1, requests)
    await yc.activate(1, "floor", CAPTION_URL + "&other=1")
    assert yc.targets[1].seq == 0


def test_unusable_target_reason():
    assert yc.unusable_target_reason("floor", False, set()) is None
    assert yc.unusable_target_reason("fr", True, {"fr", "es"}) is None
    assert "Floor translation is off" in yc.unusable_target_reason("fr", False, {"fr"})
    assert "not enabled" in yc.unusable_target_reason("de", True, {"fr"})


@pytest.mark.anyio
async def test_the_final_rejection_is_what_health_reports(monkeypatch):
    skew = "Timestamp skew from local=2026-11-05T11:26:10.685 larger than 60 sec in /closedcaption"
    replies = iter([httpx.Response(400, text=skew), httpx.Response(400, text="bad request")])
    requests = mock_youtube(monkeypatch, lambda request: next(replies))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "late")
    await drain(1, 2, requests)
    assert yc.health[1].state == "failing"
    assert yc.health[1].reason == yc.REJECTED_REASON.format(status=400)


@pytest.mark.anyio
async def test_stale_chunks_of_a_long_caption_are_dropped_when_newer_ones_wait(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", CAPTION_URL)
    long_text = " ".join(["word"] * 100)  # many chunks
    old = int(time.time() * 1000) - yc.MAX_CAPTION_AGE_MS - 1000
    yc.offer(1, "floor", long_text, received_at_ms=old)
    yc.offer(1, "floor", "fresh")
    await drain(1, 2, requests)
    await asyncio.sleep(0.05)
    assert len(requests) == 2  # the first chunk of the old caption, then the fresh one
    assert requests[-1].content.decode().splitlines()[1] == "fresh"


@pytest.mark.anyio
async def test_a_long_caption_is_sent_in_full_when_nothing_is_waiting(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", CAPTION_URL)
    long_text = " ".join(["word"] * 100)
    old = int(time.time() * 1000) - yc.MAX_CAPTION_AGE_MS - 1000
    yc.offer(1, "floor", long_text, received_at_ms=old)
    expected = len(yc.chunks(long_text))
    await drain(1, expected, requests)
    assert len(requests) == expected


@pytest.mark.anyio
async def test_deactivate_stops_the_sender_and_clears_state(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "hello")
    await drain(1, 1, requests)
    await yc.deactivate(1)
    assert 1 not in yc.targets
    assert 1 not in yc._queues
    assert 1 not in yc._tasks
    assert yc.health[1].state == "off"
    yc.offer(1, "floor", "after")
    assert 1 not in yc._queues


@pytest.mark.anyio
async def test_activate_starts_waiting_and_replaces_the_previous_target():
    await yc.activate(1, "floor", CAPTION_URL)
    assert yc.health[1].state == "waiting"
    await yc.activate(1, "de", CAPTION_URL + "&x=1")
    assert yc.targets[1].language == "de"
    assert yc.targets[1].seq == 0


def test_format_timestamp_and_body_match_the_measured_template():
    ms = int(datetime(2026, 11, 5, 11, 24, 52, 978000, tzinfo=UTC).timestamp() * 1000)
    assert yc.format_timestamp(ms) == "2026-11-05T11:24:52.978"
    assert yc.body(ms, "Hello from VoxBento") == "2026-11-05T11:24:52.978\nHello from VoxBento\n"


def test_clamp_keeps_timestamps_inside_youtubes_window():
    now = 1_800_000_000_000
    assert yc.clamp_ms(now, now) == now
    assert yc.clamp_ms(now - 30_000, now) == now - 30_000
    assert yc.clamp_ms(now - 600_000, now) == now - yc.MAX_SKEW_MS
    assert yc.clamp_ms(now + 600_000, now) == now + yc.MAX_SKEW_MS
    assert yc.MAX_SKEW_MS < 60_000


@pytest.mark.anyio
async def test_activate_upgrades_the_http_caption_url_to_https(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", "http://upload.youtube.com/closedcaption?cid=SECRETCID123")
    assert yc.targets[1].url.startswith("https://upload.youtube.com/")
    yc.offer(1, "floor", "hello")
    await drain(1, 1, requests)
    assert requests[0].url.scheme == "https"


@pytest.mark.anyio
async def test_a_caption_that_waited_too_long_is_clamped_before_posting(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(200))
    await yc.activate(1, "floor", CAPTION_URL)
    ten_minutes_ago = int(time.time() * 1000) - 600_000
    yc.offer(1, "floor", "stale", received_at_ms=ten_minutes_ago)
    await drain(1, 1, requests)
    stamp = datetime.fromisoformat(requests[0].content.decode().splitlines()[0]).replace(tzinfo=UTC)
    assert abs(time.time() - stamp.timestamp()) < 60


@pytest.mark.anyio
async def test_clock_skew_rejection_has_its_own_reason(monkeypatch):
    skew = "Timestamp skew from local=2026-11-05T11:26:10.685 larger than 60 sec in /closedcaption"
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(400, text=skew))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "late")
    # The rejection carries YouTube's clock, so the caption is stamped again and tried once more.
    await drain(1, 2, requests)
    assert len(requests) == 2
    assert yc.health[1].state == "failing"
    assert yc.health[1].reason == yc.SKEW_REASON


@pytest.mark.anyio
async def test_other_400_rejections_keep_the_generic_reason(monkeypatch):
    requests = mock_youtube(monkeypatch, lambda request: httpx.Response(400, text="bad request"))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "bad")
    await drain(1, 1, requests)
    assert "HTTP 400" in yc.health[1].reason
    assert len(requests) == 1  # a plain 400 is not a clock problem, so it is not retried


class OffsetClock:
    """Stands in for the time module: this host's clock runs `seconds` ahead of (or behind) real time."""

    def __init__(self, seconds: float):
        self.seconds = seconds

    def time(self) -> float:
        return time.time() + self.seconds


def youtube_server(with_date: bool = True, always_reject: bool = False):
    """A fake caption endpoint that enforces YouTube's rule: 400 when the stamp is >60 s from its own clock."""

    def handle(request: httpx.Request) -> httpx.Response:
        stamp = datetime.fromisoformat(request.content.decode().splitlines()[0]).replace(tzinfo=UTC)
        real_now = datetime.now(UTC)
        headers = {"Date": format_datetime(real_now, usegmt=True)} if with_date else {}
        if always_reject or abs(stamp.timestamp() - real_now.timestamp()) > 60:
            local = real_now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            text = f"Timestamp skew from local={local} larger than 60 sec in /closedcaption"
            return httpx.Response(400, text=text, headers=headers)
        return httpx.Response(200, text=real_now.isoformat(), headers=headers)

    return handle


def stamp_of(request: httpx.Request) -> float:
    return datetime.fromisoformat(request.content.decode().splitlines()[0]).replace(tzinfo=UTC).timestamp()


@pytest.mark.anyio
@pytest.mark.parametrize("host_ahead_s", [90, -90])
@pytest.mark.parametrize("with_date", [True, False])
async def test_a_wrong_host_clock_is_learned_and_the_caption_is_posted(monkeypatch, host_ahead_s, with_date):
    """The first caption is rejected, YouTube's clock is learned from the answer, and the retry is accepted."""
    requests = mock_youtube(monkeypatch, youtube_server(with_date=with_date))
    monkeypatch.setattr(yc, "time", OffsetClock(host_ahead_s))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "hello")
    await drain(1, 2, requests)
    assert len(requests) == 2
    assert yc.health[1].state == "posting"
    assert abs(stamp_of(requests[1]) - time.time()) < 5  # in YouTube's time, not the host's
    assert abs(yc.clock_offset_ms[1] + host_ahead_s * 1000) < 3000


@pytest.mark.anyio
async def test_a_seeded_clock_offset_gets_the_first_caption_through(monkeypatch):
    requests = mock_youtube(monkeypatch, youtube_server())
    monkeypatch.setattr(yc, "time", OffsetClock(90))
    await yc.activate(1, "floor", CAPTION_URL, server_time_ms=int(time.time() * 1000))
    yc.offer(1, "floor", "hello")
    await drain(1, 1, requests)
    assert len(requests) == 1
    assert yc.health[1].state == "posting"
    assert abs(stamp_of(requests[0]) - time.time()) < 5


@pytest.mark.anyio
async def test_once_learned_the_next_caption_needs_no_retry(monkeypatch):
    requests = mock_youtube(monkeypatch, youtube_server())
    monkeypatch.setattr(yc, "time", OffsetClock(90))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "first")
    await drain(1, 2, requests)
    yc.offer(1, "floor", "second")
    await drain(1, 3, requests)
    assert len(requests) == 3
    assert yc.health[1].state == "posting"


@pytest.mark.anyio
async def test_a_skew_that_persists_is_retried_only_once_per_caption(monkeypatch):
    requests = mock_youtube(monkeypatch, youtube_server(always_reject=True))
    await yc.activate(1, "floor", CAPTION_URL)
    yc.offer(1, "floor", "late")
    await drain(1, 2, requests)
    await asyncio.sleep(0.05)
    assert len(requests) == 2
    assert yc.health[1].state == "failing"
    assert yc.health[1].reason == yc.SKEW_REASON


@pytest.mark.anyio
async def test_deactivate_forgets_the_clock_offset(monkeypatch):
    await yc.activate(1, "floor", CAPTION_URL, server_time_ms=int(time.time() * 1000) + 90_000)
    assert 1 in yc.clock_offset_ms
    await yc.deactivate(1)
    assert yc.clock_offset_ms == {}


@pytest.mark.anyio
async def test_wants_is_true_only_for_the_language_being_posted():
    assert not yc.wants(1, "floor")
    await yc.activate(1, "fr", CAPTION_URL)
    assert yc.wants(1, "fr")
    assert not yc.wants(1, "floor")
    assert not yc.wants(2, "fr")
    await yc.deactivate(1)
    assert not yc.wants(1, "fr")


@pytest.mark.anyio
async def test_offer_never_raises_into_the_caption_path(monkeypatch, caplog):
    def broken(task):
        raise RuntimeError("no running loop")

    monkeypatch.setattr(yc, "track_task", broken)
    await yc.activate(1, "floor", CAPTION_URL)
    with caplog.at_level(logging.WARNING):
        yc.offer(1, "floor", "hello")  # must not raise
    assert "youtube_captions offer_failed room_id=1" in caplog.text
    assert "SECRETCID123" not in caplog.text
