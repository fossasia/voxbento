"""The translation load-test harness (#1187) in a configuration small enough for CI."""

from __future__ import annotations

import pytest
from translation_load_harness import (
    DROPPED,
    FAILED,
    MISSING,
    TRANSLATED,
    LoadConfig,
    main,
    percentile,
    render,
    run_load_test,
    summarise,
)


@pytest.mark.anyio
async def test_every_translation_is_accounted_for_per_channel():
    """Each room gets its own channels, and every segment sent into one comes back translated."""
    config = LoadConfig(rooms=2, languages=(1, 3), segments=4, interval=0.02, latency=0.01, jitter=0, capacity=0)

    report = await run_load_test(config)

    assert [(c.room, c.language) for c in report.channels] == [
        (1, "fr"),
        (2, "fr"),
        (2, "es"),
        (2, "de"),
    ]
    for channel in report.channels:
        assert channel.sent == 4
        assert channel.outcomes[TRANSLATED] == 4
        assert len(channel.start_lag) == 4
        assert len(channel.inference) == 4
        assert all(value >= 0 for value in channel.start_lag)
    assert report.errors == 0
    assert report.total(MISSING) == 0
    assert report.peak_in_flight >= 1


@pytest.mark.anyio
async def test_queueing_shows_up_as_start_lag():
    """Segments that arrive faster than the engine answers wait, and the wait is reported as lag."""
    latency = 0.05
    config = LoadConfig(rooms=1, languages=(2,), segments=6, interval=0.001, latency=latency, jitter=0, capacity=1)

    report = await run_load_test(config)

    assert [c.outcomes[TRANSLATED] for c in report.channels] == [6, 6]
    # Twelve calls through an engine that serves one at a time take at least twelve latencies.
    assert report.wall_time >= 12 * latency
    # The worker lets two calls per channel start at once, so later segments wait their turn.
    assert max(lag for c in report.channels for lag in c.start_lag) >= latency
    assert report.peak_in_flight <= 4
    assert report.errors == 0


def test_summarise_sorts_each_translation_by_outcome():
    """Translated, dropped (never reached the engine), failed (reached it) and missing (no answer)."""
    rooms = [(7, ["fr", "es"])]
    texts = {(7, 1): "one", (7, 2): "two"}
    arrivals = {(7, 1): 10.0, (7, 2): 11.0}
    calls = {
        ("one", "fr"): (10.5, 11.0),
        ("two", "fr"): (12.0, 12.5),
        ("one", "es"): (10.25, 10.75),
    }
    first_bundles = {
        (7, "fr", 1): None,
        (7, "fr", 2): "pipeline_failed",
        (7, "es", 1): None,
        # (7, "es", 2) never got a bundle.
    }

    channels, reasons = summarise(rooms, 2, texts, arrivals, calls, first_bundles)

    fr, es = channels
    assert (fr.label, es.label) == ("room 1 fr", "room 1 es")
    assert fr.outcomes == {TRANSLATED: 1, FAILED: 1}
    assert fr.start_lag == [0.5, 1.0]
    assert fr.inference == [0.5]
    assert es.outcomes == {TRANSLATED: 1, MISSING: 1}
    assert reasons == {"pipeline_failed": 1}

    first_bundles[(7, "es", 2)] = "pipeline_failed"
    _, es = summarise(rooms, 2, texts, arrivals, calls, first_bundles)[0]
    assert es.outcomes == {TRANSLATED: 1, DROPPED: 1}
    assert es.start_lag == [0.25]  # a dropped translation has no engine call, so no lag


@pytest.mark.anyio
async def test_report_renders_as_text_and_markdown():
    """Rows per channel, then per language over all rooms, then the total."""
    report = await run_load_test(
        LoadConfig(rooms=2, languages=(2, 1), segments=2, interval=0.01, latency=0, jitter=0, capacity=0)
    )

    text = render(report)
    labels = [line.split("  ")[0] for line in text.splitlines() if line.startswith(("room ", "fr,", "es,", "all "))]
    assert labels == ["room 1 fr", "room 1 es", "room 2 fr", "fr, all rooms", "es, all rooms", "all"]

    markdown = render(report, markdown=True)
    assert "| channel | sent | translated | dropped | failed |" in markdown
    assert "| room 1 fr | 2 | 2 | 0 | 0 |" in markdown
    assert "| fr, all rooms | 4 | 4 | 0 | 0 |" in markdown
    assert "| es, all rooms | 2 | 2 | 0 | 0 |" in markdown
    assert "| all | 6 | 6 | 0 | 0 |" in markdown


def test_command_line_prints_a_report(capsys):
    exit_code = main(
        ["--rooms", "1", "--languages", "1", "--segments", "2", "--interval", "0.01", "--latency", "0", "--markdown"]
    )

    assert exit_code == 0
    assert "| room 1 fr | 2 | 2 | 0 | 0 |" in capsys.readouterr().out


@pytest.mark.parametrize(
    "overrides",
    [
        {"rooms": 0},
        {"languages": ()},
        {"languages": (0,)},
        {"languages": (13,)},
        {"segments": 0},
        {"interval": 0},
        {"interval": float("inf")},
        {"latency": -1},
        {"jitter": 1},
        {"capacity": -1},
    ],
)
def test_config_rejects_impossible_values(overrides):
    with pytest.raises(ValueError):
        LoadConfig(**overrides)


def test_percentile_uses_nearest_rank():
    assert percentile([], 0.5) is None
    assert percentile([3.0], 0.95) == 3.0
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.0
    assert percentile([float(n) for n in range(1, 21)], 0.95) == 19.0
