"""spec 11 §3.7 integration: 100 ticks of scan + act with orphans, a refusing start and cap pressure leave the
Task row count unchanged (the watchdog never creates tasks)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from temporalio.service import RPCError, RPCStatusCode

from core import tasks_schema as ts
from core.services import watchdog as wd
from core.temporal.watchdog_types import WatchdogConfig

from .conftest import WS


def _tasks(table):
    items = table.scan()["Items"]
    return [i for i in items if i.get("type") == "task"]


async def test_100_faulty_ticks_task_count_unchanged(table, metrics, monkeypatch):
    for i in range(5):
        tid = ts.new_task_id()
        table.put_item(
            Item=ts.Task(
                ws=WS,
                taskId=tid,
                identifier=f"LB-{i}",
                title="t",
                status="working",
                audience={"kind": "workspace"},
                lane="shared",
                triggerId=ts.trigger_id("chat", tid),
                attemptCount=1,
                createdAt="2026-11-04T00:00:00+00:00",
                updatedAt="2026-11-04T00:00:00+00:00",
            ).to_item()
        )
    before = len(_tasks(table))
    monkeypatch.setattr(wd, "_scope", (0.0, []))

    # The fleet cap is bucketed by UTC hour; on the wall clock a loop that crosses :00 gets a fresh
    # bucket (3 + 3 starts) and fails. One frozen instant keeps all 100 ticks in one bucket.
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 12, 12, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(wd, "datetime", FrozenDatetime)

    class Gone:
        async def describe(self):
            raise RPCError("gone", RPCStatusCode.NOT_FOUND, b"")

    start = AsyncMock(side_effect=RuntimeError("refused"))
    cfg = WatchdogConfig(reconcile="act", fleet_actions_per_hour=3)
    with (
        patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=[{"owner_id": WS}])),
        patch("core.temporal.client.task_handle", lambda ws, t: Gone()),
        patch("core.temporal.client.start_task_workflow", start),
    ):
        for _ in range(100):
            scan = await wd.watchdog_scan(cfg)
            for p in scan.proposals:
                await wd.watchdog_act(cfg, p)
    assert len(_tasks(table)) == before
    assert start.await_count <= 3  # the fleet cap held within the hour
    assert all(c.args[2] == "reconcile" for c in start.await_args_list)
