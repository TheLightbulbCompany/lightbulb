"""RateBucket: sliding-window allow() + idle-key eviction sweep."""

from core.services.rate_bucket import RateBucket


def test_allow_blocks_over_limit_then_resets_after_window(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("core.services.rate_bucket.time.monotonic", lambda: now[0])

    bucket = RateBucket(per_minute=2)
    assert bucket.allow("k") is True
    assert bucket.allow("k") is True
    assert bucket.allow("k") is False  # 3rd within the same minute

    now[0] += 61.0
    assert bucket.allow("k") is True  # window rolled over


def test_sweep_evicts_idle_keys(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("core.services.rate_bucket.time.monotonic", lambda: now[0])

    bucket = RateBucket(per_minute=5)
    bucket.allow("owner-a")
    assert "owner-a" in bucket._buckets

    now[0] += 61.0
    bucket._sweep(now[0])

    assert "owner-a" not in bucket._buckets


def test_periodic_sweep_runs_every_n_calls(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("core.services.rate_bucket.time.monotonic", lambda: now[0])

    bucket = RateBucket(per_minute=1000)
    bucket.allow("idle-owner")
    now[0] += 61.0

    # Drive >1024 calls on a different key so the periodic sweep fires and
    # drops "idle-owner", which hasn't been touched since before the window.
    for _ in range(1025):
        bucket.allow("busy-owner")

    assert "idle-owner" not in bucket._buckets
