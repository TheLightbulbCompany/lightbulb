"""Tests for core/observability/e2e_latency.py — the chat.e2e.latency SLO span.

Covers:
  - START then terminal END for the same run id emits chat.e2e.latency with a
    positive Milliseconds value (alarm W9's metric).
  - A never-completed run is bounded: the map never exceeds its size cap, and
    stale starts age out — no unbounded growth.
  - A terminal END with no matching START is a silent no-op (no metric).
  - An emission failure inside record_end never propagates into the caller.
"""

import time
from unittest.mock import patch

from core.observability.e2e_latency import RunLatencyTracker, _METRIC_NAME


async def test_start_then_end_emits_positive_latency():
    """A recorded START followed by a terminal END emits the SLO metric in ms."""
    tracker = RunLatencyTracker()
    captured: list[tuple] = []

    def fake_put_metric(name, value=1.0, unit="Count", dimensions=None):
        captured.append((name, value, unit, dimensions))

    with patch("core.observability.e2e_latency.put_metric", side_effect=fake_put_metric):
        await tracker.record_start("run-1")
        time.sleep(0.005)  # ensure a measurable, positive elapsed
        await tracker.record_end("run-1")

    assert len(captured) == 1, "exactly one chat.e2e.latency emission"
    name, value, unit, dimensions = captured[0]
    assert name == _METRIC_NAME == "chat.e2e.latency"
    assert unit == "Milliseconds"
    assert value > 0, "elapsed latency must be a positive value"
    # No extra dimensions — alarm W9 queries on {env,service} only, which
    # put_metric auto-injects. A custom dim would create an un-alarmed stream.
    assert dimensions is None
    # Entry consumed on terminal — no leak.
    assert tracker._pending_count() == 0


async def test_end_with_no_start_is_silent_noop():
    """A terminal for an untracked run (cron/channel, aged-out, wrong task) emits nothing."""
    tracker = RunLatencyTracker()
    with patch("core.observability.e2e_latency.put_metric") as mock_put:
        await tracker.record_end("never-started")
    mock_put.assert_not_called()


async def test_never_completed_runs_respect_size_cap():
    """Runs that never terminate must not grow the map past its cap (no leak)."""
    cap = 8
    tracker = RunLatencyTracker(max_pending=cap, max_age_s=10_000)
    with patch("core.observability.e2e_latency.put_metric"):
        for i in range(cap * 5):  # 40 starts, none ever ended
            await tracker.record_start(f"run-{i}")
    # FIFO-bounded: size never exceeds the cap regardless of how many leaked.
    assert tracker._pending_count() == cap
    # The OLDEST starts were evicted; only the most-recent `cap` survive.
    surviving = set(tracker._starts.keys())
    assert surviving == {f"run-{i}" for i in range(cap * 5 - cap, cap * 5)}


async def test_stale_starts_age_out():
    """A start older than max_age_s is swept and never emits a (stale) latency."""
    tracker = RunLatencyTracker(max_pending=1024, max_age_s=0.05)
    with patch("core.observability.e2e_latency.put_metric") as mock_put:
        await tracker.record_start("old-run")
        assert tracker._pending_count() == 1
        time.sleep(0.08)  # exceed max_age_s
        # A fresh record_start triggers the lazy sweep, dropping the stale one.
        await tracker.record_start("new-run")
        assert "old-run" not in tracker._starts
        assert "new-run" in tracker._starts
        # The aged-out run's late terminal emits nothing (start already gone).
        await tracker.record_end("old-run")
    mock_put.assert_not_called()


async def test_emission_failure_never_propagates():
    """If put_metric raises, record_end swallows it — the event path is unharmed."""
    tracker = RunLatencyTracker()
    with patch(
        "core.observability.e2e_latency.put_metric",
        side_effect=RuntimeError("CloudWatch exploded"),
    ):
        await tracker.record_start("run-x")
        # Must NOT raise — best-effort measurement on the chat hot path.
        await tracker.record_end("run-x")
    # The pending entry was still consumed despite the emit failure (no leak).
    assert tracker._pending_count() == 0


async def test_blank_run_id_ignored():
    """A blank/missing run id is a no-op on both ends (defensive)."""
    tracker = RunLatencyTracker()
    with patch("core.observability.e2e_latency.put_metric") as mock_put:
        await tracker.record_start("")
        await tracker.record_end("")
    assert tracker._pending_count() == 0
    mock_put.assert_not_called()


async def test_module_singleton_round_trip(capsys):
    """The public record_chat_start/record_chat_end helpers emit real EMF.

    Exercises the actual put_metric (not mocked) so the emitted line is a
    valid Milliseconds metric named chat.e2e.latency — the shape alarm W9
    consumes.
    """
    import json

    from core.observability import e2e_latency

    # Use a fresh tracker so other tests' state can't bleed in.
    fresh = RunLatencyTracker()
    with patch.object(e2e_latency, "_TRACKER", fresh):
        await e2e_latency.record_chat_start("run-singleton")
        time.sleep(0.003)
        await e2e_latency.record_chat_end("run-singleton")

    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    cw = data["_aws"]["CloudWatchMetrics"][0]
    assert cw["Metrics"][0]["Name"] == "chat.e2e.latency"
    assert cw["Metrics"][0]["Unit"] == "Milliseconds"
    assert data["chat.e2e.latency"] > 0
    # env + service auto-injected; no high-cardinality run/user dims.
    assert set(cw["Dimensions"][0]) == {"env", "service"}
