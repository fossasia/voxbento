"""Load-test harness for the translation worker (#1187).

Drives the real ``TranslationWorker.handle_translation`` path for N rooms x M target
languages, with a fake engine of configurable latency in place of a model, and reports
per channel (one room's translations into one language), per language over all rooms and
in total how long translations waited before inference started, how long inference took,
and how many were dropped, failed or never answered.

Run it locally:

    uv run python tests/translation_load_harness.py --rooms 10 --languages 5

``--languages`` also takes a comma-separated pattern that repeats over the rooms, so
``--rooms 4 --languages 1,8`` gives rooms with 1, 8, 1 and 8 target languages: a quick
way to see whether busy rooms crowd out small ones.

Everything stays in the process: a temporary SQLite database, stub listener and TTS
managers, and no network. The fake engine stands in for the model, so the numbers measure
how the worker queues and schedules translations, not translation quality or a real
model's speed. ``--capacity`` is how many calls the fake engine serves at once (like CPU
cores for a local model); calls beyond it wait inside the engine and count as inference time.

This is a test tool: run it on its own or under pytest, one run at a time, never inside a
process that is serving live events. While it runs it points the worker at its own
database, managers and engine.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Run as a script, the repository root is not on the path yet, and Settings need an
# encryption key to load; the harness never stores a real secret. Both must be in place
# before ``portal`` is imported.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("API_KEY_ENCRYPTION_KEY", "translation-load-harness-only")

from portal import database, youtube_captions  # noqa: E402
from portal.database import get_session, save_transcript_segment  # noqa: E402
from portal.models import Event, Room, RoomTranslationLanguage  # noqa: E402
from portal.translations import worker as worker_module  # noqa: E402
from portal.translations.engine import EngineKind, TranslationEngine  # noqa: E402

#: Target languages in the order rooms get them; English is the source language.
TARGET_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("fr", "French"),
    ("es", "Spanish"),
    ("de", "German"),
    ("pt", "Portuguese"),
    ("it", "Italian"),
    ("nl", "Dutch"),
    ("ja", "Japanese"),
    ("zh", "Chinese"),
    ("ar", "Arabic"),
    ("hi", "Hindi"),
    ("ru", "Russian"),
    ("ko", "Korean"),
)

#: The provider name the rooms select; the harness maps it to the fake engine.
PROVIDER = "load-harness"

#: How long to wait for the worker to answer every segment once the last one is sent.
DRAIN_TIMEOUT = 120.0

#: Per-booth state the current worker keeps between segments, by module attribute name.
WORKER_STATE = ("LANGUAGE_SEMAPHORES", "LANGUAGE_QUEUES")

TRANSLATED = "translated"
DROPPED = "dropped"
FAILED = "failed"
MISSING = "missing"


@dataclass(frozen=True)
class LoadConfig:
    """One load-test run."""

    rooms: int = 10
    #: Target languages per room; the pattern repeats over the rooms.
    languages: tuple[int, ...] = (5,)
    #: Finalised segments each room's speaker produces.
    segments: int = 15
    #: Seconds between two segments of the same room.
    interval: float = 2.0
    #: Seconds the fake engine takes per call, before jitter.
    latency: float = 0.3
    #: Each call's latency varies by up to this fraction either way.
    jitter: float = 0.2
    #: Calls the fake engine serves at once; 0 means no limit.
    capacity: int = 4
    seed: int = 1

    def __post_init__(self) -> None:
        """Reject values a run cannot use, so a bad command line fails before anything starts."""
        if self.rooms < 1:
            raise ValueError("rooms must be at least 1")
        if not self.languages or any(not 1 <= n <= len(TARGET_LANGUAGES) for n in self.languages):
            raise ValueError(f"languages per room must be between 1 and {len(TARGET_LANGUAGES)}")
        if self.segments < 1:
            raise ValueError("segments must be at least 1")
        if not (self.interval > 0 and math.isfinite(self.interval)):
            raise ValueError("interval must be a positive number of seconds")
        if not (self.latency >= 0 and math.isfinite(self.latency)):
            raise ValueError("latency must be zero or a positive number of seconds")
        if not 0 <= self.jitter < 1:
            raise ValueError("jitter must be at least 0 and below 1")
        if self.capacity < 0:
            raise ValueError("capacity must be 0 (no limit) or more")

    def languages_for(self, room_index: int) -> int:
        """How many target languages the room at ``room_index`` (0-based) translates into."""
        return self.languages[room_index % len(self.languages)]


@dataclass
class ChannelReport:
    """What happened to one room's translations into one language."""

    room: int
    language: str
    sent: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    #: Seconds from the segment being finalised to the engine call starting.
    start_lag: list[float] = field(default_factory=list)
    #: Seconds the engine call took, for translations that came back.
    inference: list[float] = field(default_factory=list)

    @property
    def label(self) -> str:
        """The channel's name in the report, e.g. ``room 2 fr``."""
        return f"room {self.room} {self.language}"


@dataclass
class LoadReport:
    """The outcome of one run: every channel, plus what was measured across all of them."""

    config: LoadConfig
    channels: list[ChannelReport]
    wall_time: float
    #: The most engine calls in flight at once, across every room and language.
    peak_in_flight: int
    #: The error codes the worker sent with translations that did not come back.
    reasons: Counter[str]
    #: Exceptions that escaped ``handle_translation``; the worker should catch them all.
    errors: int

    def total(self, outcome: str) -> int:
        """How many translations ended with ``outcome``, over every channel."""
        return sum(channel.outcomes[outcome] for channel in self.channels)

    @property
    def sent(self) -> int:
        """How many translations were asked for, over every channel."""
        return sum(channel.sent for channel in self.channels)


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile, or ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


class FakeEngine(TranslationEngine):
    """Answers after a set delay and records when each call started and ended."""

    kind = EngineKind.CT2_SEQ2SEQ

    def __init__(self, config: LoadConfig) -> None:
        """Take latency, jitter, capacity and seed from the run's config."""
        self._latency = config.latency
        self._jitter = config.jitter
        self._rng = random.Random(config.seed)
        self._hardware = asyncio.Semaphore(config.capacity) if config.capacity else None
        self.in_flight = 0
        self.peak_in_flight = 0
        #: (text, target code) -> (start, end), in ``time.perf_counter`` seconds.
        self.calls: dict[tuple[str, str], tuple[float, float]] = {}

    async def translate_batch(self, source, targets, texts, context, *, model, api_key=None):
        """Wait out the latency (behind ``capacity`` if set) and return a tagged copy of each text."""
        start = time.perf_counter()
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        delay = self._latency * (1 + self._rng.uniform(-self._jitter, self._jitter))
        try:
            if self._hardware is None:
                await asyncio.sleep(delay)
            else:
                async with self._hardware:
                    await asyncio.sleep(delay)
        finally:
            # Also reached when the worker's timeout cancels the call.
            self.in_flight -= 1
            end = time.perf_counter()
            for text in texts:
                for target in targets:
                    self.calls[(text, target.code)] = (start, end)
        return [[f"[{target.code}] {text}" for target in targets] for text in texts]


class StubTTSManager:
    """Stands in for ``tts_manager``: everyone listens, and each translation's first answer is kept."""

    def __init__(self) -> None:
        """Start with no answers recorded."""
        #: (room id, language, seq) -> error code of the first bundle (``None`` = translated).
        self.first: dict[tuple[int, str, int], str | None] = {}

    def has_listeners(self, room_id: int, language_code: str, booth_id: str) -> bool:
        """Every language has a listener, so the worker translates all of them."""
        return True

    async def broadcast_bundle(
        self,
        room_id: int,
        language_code: str,
        booth_id: str,
        audio_bytes: bytes,
        segment_id: str,
        seq: int,
        caption: str = "",
        translation: str = "",
        error: str | None = None,
        audio_pending: bool | None = None,
    ) -> None:
        """Keep the first bundle's error code: it says whether the text was translated."""
        # A second bundle for the same segment carries the audio.
        self.first.setdefault((room_id, language_code, seq), error)


class StubListenerManager:
    """Stands in for ``listener_manager``: no caption listeners, and broadcasts go nowhere."""

    def has_listeners(self, booth_id: str) -> bool:
        """The TTS stub already says everyone listens; captions add nothing to measure."""
        return False

    async def broadcast(self, booth_id: str, message: dict) -> None:
        """Drop caption broadcasts."""


async def seed_rooms(config: LoadConfig, slug: str) -> list[tuple[int, list[str]]]:
    """Create one event with ``config.rooms`` rooms, each translating floor English."""
    rooms: list[tuple[int, list[str]]] = []
    async with get_session() as session:
        event = Event(slug=slug, display_name="Translation load test")
        session.add(event)
        await session.flush()
        for index in range(config.rooms):
            room = Room(
                event_id=event.id,
                display_name=f"Room {index + 1}",
                floor_language_code="en",
                floor_translation_enabled=True,
                floor_translation_provider=PROVIDER,
                floor_translation_model="fake",
            )
            session.add(room)
            await session.flush()
            languages = TARGET_LANGUAGES[: config.languages_for(index)]
            for code, name in languages:
                session.add(
                    RoomTranslationLanguage(room_id=room.id, language_code=code, language_name=name, enabled=True)
                )
            rooms.append((room.id, [code for code, _ in languages]))
    return rooms


def release_worker_state(slug: str) -> None:
    """Remove the per-booth queues and semaphores the worker kept for this run's booths.

    The worker never removes them itself, so repeated runs in one process would pile them up.
    Names the worker no longer has (a scheduler may replace them) are skipped.
    """
    prefix = f"{slug}-"
    for name in WORKER_STATE:
        state = getattr(worker_module, name, None)
        if isinstance(state, dict):
            for key in [key for key in state if key.startswith(prefix)]:
                del state[key]


def summarise(
    rooms: Sequence[tuple[int, Sequence[str]]],
    segments: int,
    texts: dict[tuple[int, int], str],
    arrivals: dict[tuple[int, int], float],
    calls: dict[tuple[str, str], tuple[float, float]],
    first_bundles: dict[tuple[int, str, int], str | None],
) -> tuple[list[ChannelReport], Counter[str]]:
    """Sort every translation into translated, dropped, failed or missing, per channel.

    ``rooms`` is (room id, target languages); ``texts`` and ``arrivals`` are by (room id, seq);
    ``calls`` holds the engine's (start, end) by (text, language); ``first_bundles`` the error
    code of each translation's first bundle by (room id, language, seq), ``None`` if translated.
    """
    reasons: Counter[str] = Counter()
    channels: list[ChannelReport] = []
    for index, (room_id, languages) in enumerate(rooms):
        for code in languages:
            channel = ChannelReport(room=index + 1, language=code)
            for seq in range(1, segments + 1):
                channel.sent += 1
                call = calls.get((texts[(room_id, seq)], code))
                if call is not None:
                    channel.start_lag.append(call[0] - arrivals[(room_id, seq)])
                key = (room_id, code, seq)
                if key not in first_bundles:
                    channel.outcomes[MISSING] += 1
                elif first_bundles[key] is None:
                    channel.outcomes[TRANSLATED] += 1
                    if call is not None:
                        channel.inference.append(call[1] - call[0])
                else:
                    reasons[first_bundles[key]] += 1
                    # Reaching the engine and still failing is a failure (a timeout, or the
                    # result not being saved); never reaching it means the worker dropped it.
                    channel.outcomes[FAILED if call is not None else DROPPED] += 1
            channels.append(channel)
    return channels, reasons


async def run_load_test(config: LoadConfig) -> LoadReport:
    """Run one load test against a fresh temporary database and return what happened."""
    engine = FakeEngine(config)
    tts = StubTTSManager()
    # A fresh event slug per run, so the worker's per-booth state never mixes two runs.
    slug = f"load-{uuid.uuid4().hex[:8]}"
    arrivals: dict[tuple[int, int], float] = {}
    texts: dict[tuple[int, int], str] = {}
    jobs: list[asyncio.Task] = []
    translation_worker = worker_module.TranslationWorker(AsyncMock())

    async def publish(room_id: int, booth: str, text: str, seq: int) -> None:
        """What the caption aggregator does with a finalised floor segment: save it, then translate it."""
        arrivals[(room_id, seq)] = time.perf_counter()
        segment_id = await save_transcript_segment(booth, text, room_id)
        if segment_id is None:
            raise RuntimeError(f"could not save segment {seq} of room {room_id}")
        await translation_worker.handle_translation(room_id, segment_id, text, booth, str(uuid.uuid4()), seq)

    async def speak(index: int, room_id: int, started: float) -> None:
        """Send one room's segments on schedule, each translated in the background as in production."""
        booth = f"{slug}-{room_id}-floor"
        # Rooms are spread over one interval, so their segments do not all land at once.
        offset = config.interval * index / config.rooms
        for seq in range(1, config.segments + 1):
            due = started + offset + (seq - 1) * config.interval
            await asyncio.sleep(max(0.0, due - time.perf_counter()))
            text = f"room {index + 1} segment {seq}"
            texts[(room_id, seq)] = text
            jobs.append(asyncio.create_task(publish(room_id, booth, text, seq)))

    with (
        tempfile.TemporaryDirectory(prefix="voxbento-load-") as directory,
        # The worker reads the process-wide engine; the previous one comes back afterwards.
        patch.object(database, "_engine", None),
        patch.object(database, "_async_session_factory", None),
        # Looked up when the worker runs, so code holding the real managers is unaffected.
        patch("portal.websockets.manager.tts_manager", tts),
        patch("portal.websockets.manager.listener_manager", StubListenerManager()),
        patch.object(youtube_captions, "wants", return_value=False),
        patch.object(youtube_captions, "offer"),
        patch("portal.tts.worker.synthesize", new=AsyncMock(return_value=b"\0\0")),
        patch.dict(worker_module.ENGINES, {PROVIDER: engine}),
        patch.object(worker_module, "KEYLESS_PROVIDERS", worker_module.KEYLESS_PROVIDERS | {PROVIDER}),
    ):
        # A file, not ``:memory:``: concurrent sessions need their own connections.
        database.configure(f"sqlite+aiosqlite:///{Path(directory) / 'load.db'}")
        try:
            await database.init_db()
            rooms = await seed_rooms(config, slug)
            started = time.perf_counter()
            await asyncio.gather(*(speak(index, room_id, started) for index, (room_id, _) in enumerate(rooms)))
            done, pending = await asyncio.wait(jobs, timeout=DRAIN_TIMEOUT)
            for job in pending:
                job.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            wall_time = time.perf_counter() - started
        finally:
            await database.dispose()
            release_worker_state(slug)

    errors = sum(1 for job in done if not job.cancelled() and job.exception() is not None)
    channels, reasons = summarise(rooms, config.segments, texts, arrivals, engine.calls, tts.first)
    return LoadReport(
        config=config,
        channels=channels,
        wall_time=wall_time,
        peak_in_flight=engine.peak_in_flight,
        reasons=reasons,
        errors=errors,
    )


def format_seconds(value: float | None) -> str:
    """Seconds with two decimals, or ``-`` when there was nothing to measure."""
    return "-" if value is None else f"{value:.2f}"


COLUMNS = (
    "channel",
    "sent",
    "translated",
    "dropped",
    "failed",
    "missing",
    "lag p50",
    "lag p95",
    "lag max",
    "infer p50",
    "infer p95",
)


def table_row(label: str, sent: int, outcomes: Counter[str], lag: list[float], inference: list[float]) -> list[str]:
    """One report row: the counts by outcome, then the lag and inference percentiles."""
    return [
        label,
        str(sent),
        str(outcomes[TRANSLATED]),
        str(outcomes[DROPPED]),
        str(outcomes[FAILED]),
        str(outcomes[MISSING]),
        format_seconds(percentile(lag, 0.5)),
        format_seconds(percentile(lag, 0.95)),
        format_seconds(max(lag, default=None)),
        format_seconds(percentile(inference, 0.5)),
        format_seconds(percentile(inference, 0.95)),
    ]


def merged_row(label: str, channels: Sequence[ChannelReport]) -> list[str]:
    """One report row for several channels together, e.g. one language over every room."""
    return table_row(
        label,
        sum(c.sent for c in channels),
        sum((c.outcomes for c in channels), Counter()),
        [value for c in channels for value in c.start_lag],
        [value for c in channels for value in c.inference],
    )


def render(report: LoadReport, *, markdown: bool = False) -> str:
    """The report as a plain-text table, or as Markdown for a CI step summary."""
    config = report.config
    pattern = ",".join(str(n) for n in config.languages)
    capacity = "no limit" if config.capacity == 0 else f"{config.capacity} at once"
    summary = [
        f"Translation load test: {config.rooms} rooms, {pattern} target languages per room "
        f"({len(report.channels)} channels), {config.segments} segments per room every {config.interval:g} s.",
        f"Fake engine: {config.latency:g} s per call (+/- {config.jitter:.0%}), capacity {capacity}.",
        f"Wall time {report.wall_time:.1f} s. Peak engine calls in flight: {report.peak_in_flight}.",
        "Dropped translations never reached the engine, failed ones reached it but did not come back, "
        "missing ones got no answer at all. Lag is seconds from the segment being finalised to its "
        "engine call starting; infer is the engine call's duration.",
    ]
    if report.reasons:
        summary.append(
            "Error codes sent for untranslated segments: "
            + ", ".join(f"{code} x{count}" for code, count in sorted(report.reasons.items()))
            + "."
        )
    if report.errors:
        summary.append(f"{report.errors} segments raised out of handle_translation.")

    channel_rows = [table_row(c.label, c.sent, c.outcomes, c.start_lag, c.inference) for c in report.channels]
    # Each language over every room that translates into it, then everything together.
    languages = list(dict.fromkeys(c.language for c in report.channels))
    language_rows = [
        merged_row(f"{code}, all rooms", [c for c in report.channels if c.language == code]) for code in languages
    ]
    total_row = merged_row("all", report.channels)
    groups = [channel_rows, language_rows, [total_row]]

    if markdown:
        lines = [" ".join(summary), "", "| " + " | ".join(COLUMNS) + " |"]
        lines.append("|" + "|".join(["---"] + ["---:"] * (len(COLUMNS) - 1)) + "|")
        lines += ["| " + " | ".join(row) + " |" for group in groups for row in group]
        return "\n".join(lines) + "\n"

    rows = [row for group in groups for row in group]
    widths = [max(len(column), *(len(row[i]) for row in rows)) for i, column in enumerate(COLUMNS)]

    def line(cells: Sequence[str]) -> str:
        """Labels left-aligned, numbers right-aligned, in the shared column widths."""
        return "  ".join(
            cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i]) for i, cell in enumerate(cells)
        ).rstrip()

    separator = line(["-" * width for width in widths])
    table = [line(COLUMNS)]
    for group in groups:
        table.append(separator)
        table += [line(row) for row in group]
    return "\n".join(summary) + "\n\n" + "\n".join(table) + "\n"


def parse_languages(value: str) -> tuple[int, ...]:
    """``--languages``: one number, or numbers separated by commas."""
    try:
        counts = tuple(int(part) for part in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("use a number, or numbers separated by commas") from None
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    """Run one load test from the command line and print its report.

    Exits 1 when a segment raised out of the worker or a translation got no answer at all.
    """
    parser = argparse.ArgumentParser(description="Load-test the translation worker with a fake engine.")
    defaults = LoadConfig()
    parser.add_argument("--rooms", type=int, default=defaults.rooms, help="rooms translating at once")
    parser.add_argument(
        "--languages",
        type=parse_languages,
        default=defaults.languages,
        help="target languages per room, or a comma-separated pattern repeated over the rooms (e.g. 1,8)",
    )
    parser.add_argument("--segments", type=int, default=defaults.segments, help="segments per room")
    parser.add_argument("--interval", type=float, default=defaults.interval, help="seconds between a room's segments")
    parser.add_argument("--latency", type=float, default=defaults.latency, help="seconds per engine call")
    parser.add_argument("--jitter", type=float, default=defaults.jitter, help="latency variation, as a fraction")
    parser.add_argument(
        "--capacity", type=int, default=defaults.capacity, help="engine calls served at once (0 = no limit)"
    )
    parser.add_argument("--seed", type=int, default=defaults.seed, help="seed for the latency jitter")
    parser.add_argument("--markdown", action="store_true", help="print a Markdown table (for CI step summaries)")
    parser.add_argument("--verbose", action="store_true", help="show the worker's own log lines")
    args = parser.parse_args(argv)

    try:
        config = LoadConfig(
            rooms=args.rooms,
            languages=args.languages,
            segments=args.segments,
            interval=args.interval,
            latency=args.latency,
            jitter=args.jitter,
            capacity=args.capacity,
            seed=args.seed,
        )
    except ValueError as exc:
        parser.error(str(exc))

    # The worker logs every segment, some at error level, which would bury the report.
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run_load_test(config))
    finally:
        if not args.verbose:
            logging.disable(logging.NOTSET)
    print(render(report, markdown=args.markdown), end="")
    return 1 if report.errors or report.total(MISSING) else 0


if __name__ == "__main__":
    sys.exit(main())
