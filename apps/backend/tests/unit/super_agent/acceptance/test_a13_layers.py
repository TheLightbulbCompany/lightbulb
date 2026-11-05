"""A13 over fake rows and stand-ins for the two dev reads (legacy messaging rows, Router runs)."""

from __future__ import annotations

from scripts.super_agent.acceptance import a13_layers, report

T1, T2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def _rows(*, attempt_for=(T1, T2), sub_for=(T1, T2), routed=True) -> list[dict]:
    rows = [
        {"type": "task", "taskId": T1, "triggerId": "chat:m1", "status": "idle"},
        {"type": "task", "taskId": T2, "triggerId": "chat:m1", "status": "working"},
        {"type": "trigger", "triggerId": "chat:m1", "routedTo": {"taskId": T1} if routed else None},
        {"type": "trigger", "triggerId": "chat:m0", "routedTo": None},  # another trigger: ignored
    ]
    rows += [{"type": "attempt", "taskId": t, "n": 1} for t in attempt_for]
    rows += [{"type": "subscription", "taskId": t, "threadKey": "room:r1"} for t in sub_for]
    return rows


class FakeCtx:
    def __init__(self, rows: list[dict]):
        self.ws = "org_on"
        self.state = {"taskIds": [T1, T2], "roomId": "r1", "samDmRoomId": "dm-s", "priyaDmRoomId": "dm-p"}
        self.rows = rows

    async def tasks_rows(self, ws=None):
        return list(self.rows)


def _run(batch: str, decisions: list[str], *, status: str = "completed", tools: list[str] = ()) -> dict:
    return {"batchId": batch, "lane": "shared", "status": status, "decisions": decisions, "tools": list(tools)}


def test_check_rows_names_each_missing_link():
    assert a13_layers.check_rows(_rows(), [T1, T2])["ok"]
    gaps = a13_layers.check_rows(_rows(attempt_for=(T1,), sub_for=(), routed=False), [T1, T2])
    assert not gaps["ok"]
    assert gaps["missing"] == {"task": [], "attempt": [T2], "subscription": [T1, T2], "routedTrigger": [T1, T2]}
    assert not a13_layers.check_rows(_rows(), [])["ok"]  # nothing to show is not evidence


def test_check_router_allows_router_tools_only():
    ok = a13_layers.check_router([_run("b1", ["start_task", "propose_tasks"]), _run("b2", ["reply"], tools=["memory"])])
    assert ok["ok"] and ok["outside"] == [] and ok["notCompleted"] == []
    bad = a13_layers.check_router([_run("b1", ["start_task"], tools=["terminal"]), _run("b2", [], status="failed")])
    assert not bad["ok"]
    assert bad["outside"] == [{"batchId": "b1", "names": ["terminal"]}] and bad["notCompleted"] == ["b2"]
    assert not a13_layers.check_router([])["ok"]


async def test_a13_green_when_rows_link_and_legacy_is_empty(monkeypatch):
    seen: dict = {}

    async def legacy(ws, rooms):
        seen["legacy"] = (ws, rooms)
        return {"runs": [], "followUps": []}

    async def runs(ws, front=None):
        seen["runs"] = ws
        return [_run("b1", ["start_task", "propose_tasks", "reply"])]

    monkeypatch.setattr(a13_layers, "legacy_rows", legacy)
    monkeypatch.setattr(a13_layers, "router_runs", runs)
    result = await a13_layers.a13(FakeCtx(_rows()))
    assert result.passed, result.evidence
    assert seen == {"legacy": ("org_on", ["r1", "dm-s", "dm-p"]), "runs": "org_on"}
    assert report.checks_of(result.evidence) == {
        "every_object_is_a_tasks_row": True,
        "no_legacy_run_rows": True,
        "no_legacy_followup_rows": True,
        "router_ran_only_router_tools": True,
    }
    assert report.status("A13") == "implemented"


async def test_a13_fails_on_legacy_rows_or_an_execution_tool_or_no_temporal(monkeypatch):
    async def legacy(ws, rooms):
        return {"runs": ["RUN#x"], "followUps": ["r1/FOLLOWUP#y"]}

    async def runs(ws, front=None):
        return [_run("b1", ["start_task"], tools=["terminal"])]

    monkeypatch.setattr(a13_layers, "legacy_rows", legacy)
    monkeypatch.setattr(a13_layers, "router_runs", runs)
    result = await a13_layers.a13(FakeCtx(_rows()))
    assert report.failed_checks(result) == [
        "no_legacy_run_rows",
        "no_legacy_followup_rows",
        "router_ran_only_router_tools",
    ]
    assert result.evidence["routerOutside"] == [{"batchId": "b1", "names": ["terminal"]}]

    async def down(ws, front=None):
        raise ConnectionError("temporal unreachable")

    monkeypatch.setattr(a13_layers, "router_runs", down)
    result = await a13_layers.a13(FakeCtx(_rows()))
    assert "router_ran_only_router_tools" in report.failed_checks(result)
    assert result.evidence["routerError"] == "ConnectionError: temporal unreachable"


def test_check_rows_expects_a_subscription_only_for_the_first_started_task():
    rows = _rows(sub_for=(T1,))  # one owner per thread: T2 has none and that is right
    assert a13_layers.check_rows(rows, [T1, T2], [T1])["ok"]
    assert not a13_layers.check_rows(rows, [T1, T2], [T2])["ok"]
