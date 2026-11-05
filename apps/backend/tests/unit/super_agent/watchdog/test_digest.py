"""`#watchdog` digest and `report()` (wave2-W Task 5)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core import tasks_schema as ts
from core.config import settings
from core.repositories import watchdog_repo
from core.services import watchdog as wd
from core.temporal.watchdog_types import WatchdogConfig

from .conftest import WS

CFG = WatchdogConfig()
NOW = datetime.now(timezone.utc)
TODAY, YDAY = NOW.strftime("%Y%m%d"), (NOW - timedelta(days=1)).strftime("%Y%m%d")


async def _finding(day, i, check="C2", **extra):
    item, _ = await watchdog_repo.put_finding(
        {
            "pk": ts.wd_finding_pk(day),
            "sk": ts.wd_finding_sk(WS, f"t{i:03d}", check, 1),
            "ws": WS,
            "taskId": f"t{i:03d}",
            "attemptN": 1,
            "check": check,
            "mode": "observe",
            "proposed": "none",
            **extra,
        }
    )
    return item


@pytest.fixture
def post(table, monkeypatch):
    monkeypatch.setattr(settings, "WATCHDOG_OWNER_ID", "org_lightbulb")
    monkeypatch.setattr(settings, "WATCHDOG_ROOM_ID", "11111111-1111-1111-1111-111111111111")
    sent = AsyncMock(return_value="m1")
    with (
        patch("core.services.task_conversation.post_as_lightbulb", sent),
        patch.object(wd.activity, "info", lambda: SimpleNamespace(workflow_id="watchdog-tick-2026-11-04T12:00:00Z")),
    ):
        yield sent


async def test_digest_silent_when_nothing_new(post):
    await wd.watchdog_digest(CFG, [], False)
    await wd.watchdog_digest(CFG, [], True)  # daily on a healthy day: nothing yesterday
    post.assert_not_awaited()


async def test_digest_lists_at_most_20_with_ids(post):
    ids = [(await _finding(TODAY, i))["findingId"] for i in range(25)]
    await wd.watchdog_digest(CFG, ids, False)
    text = post.await_args.kwargs["text"]
    lines = text.split("\n")
    assert lines[0] == "Watchdog: 25 new findings"
    assert len(lines) == 1 + 20 + 1 and lines[-1] == "…and 5 more"
    assert lines[1].startswith(ids[0] + " C2 proposed=none mode=observe")


async def test_digest_event_id_idempotent(post):
    ids = [(await _finding(TODAY, 1))["findingId"]]
    await wd.watchdog_digest(CFG, ids, False)
    await wd.watchdog_digest(CFG, ids, False)
    events = {c.kwargs["event_id"] for c in post.await_args_list}
    assert events == {"watchdog:digest:watchdog-tick-2026-11-04T12:00:00Z"}


async def test_daily_digest_counts_and_sample(post):
    for i in range(3):
        await _finding(YDAY, i)
    await _finding(YDAY, 9, "judge", verdict="looping", p=0.9)
    await wd.watchdog_digest(CFG, [], True)
    (call,) = post.await_args_list
    assert call.kwargs["event_id"] == f"watchdog:daily:{YDAY}"
    text = call.kwargs["text"]
    assert f"Watchdog daily {YDAY}: 4 findings, 4 unlabelled" in text
    assert "C2=3 looping=1" in text


async def test_digest_without_room_posts_nothing(post, monkeypatch):
    monkeypatch.setattr(settings, "WATCHDOG_ROOM_ID", "")
    await wd.watchdog_digest(CFG, ["x"], True)
    post.assert_not_awaited()


async def test_report_precision_latest_100_excludes_unsure(table):
    for i in range(110):  # tier2: 110 labelled; the latest 100 decide
        f = await _finding(TODAY, i, "judge")
        label = "fp" if i < 10 else ("unsure" if i % 10 == 0 else "tp")
        await watchdog_repo.label(f["pk"], f["sk"], label, "user_admin")
    for i, label in enumerate(["tp", "fp", "unsure"]):
        f = await _finding(YDAY, 200 + i, "C1")
        await watchdog_repo.label(f["pk"], f["sk"], label, "user_admin")
    r = await wd.report()
    assert r["reconcile"] == {"n": 2, "precision": 0.5}
    assert r["tier1"] == {"n": 0, "precision": None}
    assert r["tier2"]["n"] <= 100 and r["tier2"]["n"] == 100 - sum(1 for i in range(10, 110) if i % 10 == 0)
    assert r["tier2"]["precision"] == 1.0  # the 10 oldest (all fp) fell out of the window
