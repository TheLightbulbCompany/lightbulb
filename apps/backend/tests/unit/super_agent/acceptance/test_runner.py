"""G1 runner over a fake Ctx: dev-only guards, the registry, wait_for, the report, A0 and the G2 wiring."""

from __future__ import annotations

import json

import httpx
import pytest

from core.services.runtime_transport import session_id_for
from scripts.super_agent.acceptance import ctx as ctxmod
from scripts.super_agent.acceptance import report
from scripts.super_agent.acceptance.a00_parity import a00
from scripts.super_agent.acceptance.ctx import Ctx, Result, parse_appsync_data

DEV_API = "https://api-dev.thelightbulb.company/api/v1"


# ---------------------------------------------------------------------- fakes
class FakeCtx:
    """What a scenario body sees; ``api()`` answers from ``handler`` through httpx.MockTransport."""

    def __init__(self, handler=None, rows: dict[str, list[dict]] | None = None):
        self.ws = "org_on"
        self.users = {"sam": "user_sam", "priya": "user_priya"}
        self.state: dict = {}
        self.handler, self.rows = handler, rows or {}

    def api(self, user: str, ws: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=ctxmod.API_ROOT, transport=httpx.MockTransport(self.handler))

    async def tasks_rows(self, ws: str | None = None) -> list[dict]:
        return list(self.rows.get(ws or self.ws, []))

    async def sleep(self, seconds: float) -> None:
        pass

    async def ensure_dm_rooms(self) -> dict[str, str]:
        return {}


def _ok(data: dict) -> httpx.Response:
    return httpx.Response(200, json=data)


def legacy_handler(*, features: bool = False, reply: bool = True):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/live-config"):
            cfg = {"httpEndpoint": "https://x/event", "realtimeEndpoint": "wss://x/event/realtime", "workspaceId": "w"}
            return _ok({**cfg, "features": {"superAgent": True}} if features else cfg)
        if path.endswith("/messaging/rooms"):
            return _ok({"rooms": [{"roomId": "r1", "name": "general"}]})
        if path.endswith("/messages") and request.method == "POST":
            return _ok({"messageId": "m1", "messageSeq": 5})
        if path.endswith("/messages"):
            # The reply is only on the page after Sam's post: a read without afterSeq=5 gets the room's old head.
            if request.url.params.get("afterSeq") != "5":
                return _ok(
                    {"messages": [{"messageId": "old", "messageSeq": 1, "author": {"kind": "agent", "id": "x"}}]}
                )
            agent = {"messageId": "m2", "messageSeq": 6, "author": {"kind": "agent", "id": "agent1"}}
            return _ok({"messages": [agent] if reply else []})
        return httpx.Response(404)

    return handle


# ---------------------------------------------------------------------- dev-only guards
def _dev(monkeypatch):
    monkeypatch.setattr(ctxmod.settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(ctxmod, "API", DEV_API)
    monkeypatch.setattr(ctxmod, "table_name", lambda short: f"lightbulb-dev-{short}")


def test_runner_refuses_non_dev_environment(monkeypatch):
    _dev(monkeypatch)
    ctxmod.guard()  # dev passes
    monkeypatch.setattr(ctxmod.settings, "ENVIRONMENT", "prod")
    with pytest.raises(SystemExit, match="dev only"):
        ctxmod.guard()


def test_runner_refuses_non_dev_host(monkeypatch):
    _dev(monkeypatch)
    monkeypatch.setattr(ctxmod, "API", "https://api.thelightbulb.company/api/v1")
    with pytest.raises(SystemExit, match="dev only"):
        ctxmod.guard()


def test_runner_refuses_non_dev_table(monkeypatch):
    _dev(monkeypatch)
    monkeypatch.setattr(ctxmod, "table_name", lambda short: f"lightbulb-prod-{short}")
    with pytest.raises(SystemExit, match="lightbulb-dev-"):
        ctxmod.guard()


# ---------------------------------------------------------------------- registry
def test_parse_scenarios():
    assert report.parse_scenarios("A0-A2") == ["A0", "A1", "A2"]
    assert report.parse_scenarios("A7,A1, a5") == ["A7", "A1", "A5"]
    assert report.parse_scenarios("A12-A13,A0") == ["A12", "A13", "A0"]
    with pytest.raises(SystemExit):
        report.parse_scenarios("A99")


async def test_missing_scenario_reports_not_implemented(monkeypatch):
    monkeypatch.setitem(report.SCENARIOS, "A5", ("Q", ("a99_nope:a99",)))
    assert report.resolve("a99_nope:a99") is None
    result = await report.run_scenario("A5", FakeCtx())
    assert result.passed is False
    assert result.evidence["reason"] == "not implemented (lane Q: a99_nope:a99)"
    assert report.status("A5").startswith("not implemented (lane Q")
    assert report.status("A0") == "implemented"


async def test_missing_function_in_present_module_is_not_implemented(monkeypatch):
    monkeypatch.setitem(report.SCENARIOS, "A1", ("O", ("a00_parity:nope",)))
    result = await report.run_scenario("A1", FakeCtx())
    assert result.evidence["reason"] == "not implemented (lane O: a00_parity:nope)"


# ---------------------------------------------------------------------- wait_for
async def test_wait_for_returns_first_truthy_then_none_on_timeout(monkeypatch):
    monkeypatch.setattr(ctxmod, "POLL_S", 0.001)
    ctx = Ctx(ws="org", users={})
    pages = iter([[], [{"type": "task", "status": "queued"}], [{"type": "task", "status": "working"}]])

    async def rows(ws=None):
        return next(pages, [{"type": "task", "status": "working"}])

    monkeypatch.setattr(ctx, "tasks_rows", rows)
    found = await ctx.wait_for(lambda rs: next((r for r in rs if r["status"] == "working"), None), 1)
    assert found == {"type": "task", "status": "working"}
    assert await ctx.wait_for(lambda rs: None, 0.01) is None


def test_parse_appsync_data():
    event = {"type": "task.changed", "payload": {"taskId": "t", "part": "task", "version": 2}}
    assert parse_appsync_data({"type": "data", "event": json.dumps(event)}) == [event]
    assert parse_appsync_data({"type": "data", "event": [json.dumps(event), "not json", 3]}) == [event]
    assert parse_appsync_data({"type": "data"}) == []


# ---------------------------------------------------------------------- report
async def test_report_lists_every_check(monkeypatch):
    async def a1(ctx):
        return Result(True, {"one_agent_member_lightbulb": True, "roomId": "r1", "no_double_reply": True})

    async def a12_router(ctx):
        return Result(False, {"canary_absent_from_reply": False, "sam_got_a_reply": True})

    async def a12(ctx):
        raise RuntimeError("appsync subscribe_error")

    bodies = {"a01_one_coordinator:a01": a1, "a12_router_lanes:a12_router": a12_router, "a12_private_lane:a12": a12}
    monkeypatch.setattr(report, "resolve", lambda part: bodies.get(part))
    results = await report.run_scenarios(["A1", "A12", "A5"], FakeCtx(), log=lambda line: None)
    assert results["A1"].passed and not results["A12"].passed and not results["A5"].passed
    md = report.render_report(results, sha="abc123", workspace="org_on", faults="")
    for check in (
        "one_agent_member_lightbulb",
        "no_double_reply",
        "a12_router_lanes:a12_router.canary_absent_from_reply",
        "a12_router_lanes:a12_router.sam_got_a_reply",
    ):
        assert f"| {check} |" in md, check
    assert '| A1 | PASS | roomId | "r1" |' in md
    assert "a12_private_lane:a12.error" in md and "RuntimeError: appsync subscribe_error" in md
    assert '| A5 | FAIL | reason | "not implemented (lane Q: a05_approval:a05)" |' in md
    assert "NOT GREEN" in md and "dev SHA: abc123" in md
    assert report.failed_checks(results["A12"]) == [
        "a12_router_lanes:a12_router.canary_absent_from_reply",
        "RuntimeError: appsync subscribe_error",
    ]


def test_write_report_keeps_full_evidence_json(tmp_path):
    results = {"A0": Result(True, {"zero_tasks_rows": True, "roomId": "r1"})}
    out = tmp_path / "gates" / "wave2.md"
    report.write_report(out, results, sha="s", workspace="org", faults="")
    assert "| A0 | PASS | zero_tasks_rows | ok |" in out.read_text()
    assert json.loads(out.with_suffix(".md.json").read_text())["A0"]["evidence"]["roomId"] == "r1"


# ---------------------------------------------------------------------- A0
async def test_a00_parity_green_on_legacy_workspace():
    result = await a00(FakeCtx(legacy_handler()))
    assert result.passed, result.evidence
    assert result.evidence["ws"] == "org_on" and result.evidence["replyMessageId"] == "m2"


async def test_a00_parity_fails_when_features_or_rows_leak():
    leaked = FakeCtx(
        legacy_handler(features=True), rows={"org_on": [{"sk": "AGENT#a"}, {"sk": "META"}, {"sk": "TASK#t"}]}
    )
    result = await a00(leaked)
    assert not result.passed
    assert report.failed_checks(result) == ["live_config_has_no_features", "zero_tasks_rows"]
    assert result.evidence["tasksRows"] == 1  # AGENT#/META rows are not task rows
    silent = await a00(FakeCtx(legacy_handler(reply=False)))
    assert report.failed_checks(silent) == ["room_agent_replied_as_today"]


# ---------------------------------------------------------------------- G2 wiring
async def test_gate_wires_vm_evidence_and_skips_a0(monkeypatch):
    tid = "22222222-2222-4222-8222-222222222222"
    rows = {"org_on": [{"type": "attempt", "taskId": tid, "n": 1, "sessionId": session_id_for("org_on")}]}
    from core.services import token_custody

    async def actor(owner_id):
        return False

    monkeypatch.setattr(token_custody, "task_vm_allowed", actor)
    monkeypatch.setattr("scripts.super_agent.acceptance.ctx.load_backend_secrets", lambda: None)  # no Secrets Manager
    ctx = FakeCtx(rows=rows)

    async def scenarios(names, c, *, log):
        c.state["taskIds"] = [tid]
        return {n: Result(True, {"ok": True}) for n in names}

    monkeypatch.setattr(report, "run_scenarios", scenarios)
    results = await report.gate(ctx, log=lambda line: None)
    assert set(results) == {*report.ORDER[1:], "VM", "RULES"}
    assert results["VM"].passed, results["VM"].evidence["failures"]
    assert results["RULES"].evidence["reason"].startswith("no --rules-expect")
    rows["org_on"][0]["sessionId"] = session_id_for("org_on", tid)  # ran on a task VM under actor custody: must fail
    assert not (await report.gate(ctx, log=lambda line: None))["VM"].passed


# ---------------------------------------------------------------------- A12 DM rooms
def dm_handler(created: list[dict], *, lightbulb: bool = True):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/messaging/rooms") and request.method == "GET":
            return _ok({"rooms": [{"roomId": "general-1", "name": "general", "kind": "channel"}]})
        if path.endswith("/messaging/rooms/general-1"):
            agent = {"kind": "agent", "id": "ceo-1", "mentionHandle": "lightbulb" if lightbulb else "growth"}
            return _ok({"roomId": "general-1", "members": [{"kind": "human", "id": "user_sam"}, agent]})
        if path.endswith("/messaging/rooms") and request.method == "POST":
            body = json.loads(request.content)
            created.append(body)
            return _ok({"roomId": f"dm-{len(created)}", "kind": "dm"})
        return httpx.Response(404)

    return handle


async def test_ensure_dm_rooms_opens_one_dm_per_user_with_lightbulb():
    created: list[dict] = []
    ctx = Ctx(ws="org_on", users={"sam": "user_sam", "priya": "user_priya"})
    ctx.api = lambda user, ws=None: httpx.AsyncClient(
        base_url=ctxmod.API_ROOT, transport=httpx.MockTransport(dm_handler(created))
    )  # type: ignore[method-assign]
    rooms = await ctx.ensure_dm_rooms()
    assert rooms == {"sam": "dm-1", "priya": "dm-2"}
    assert ctx.state == {"samDmRoomId": "dm-1", "priyaDmRoomId": "dm-2"}
    assert [c["kind"] for c in created] == ["dm", "dm"]
    assert all(
        c["members"] == [{"kind": "agent", "id": "ceo-1", "role": "member", "listenMode": "active"}] for c in created
    )
    assert created[0]["clientRoomId"] != created[1]["clientRoomId"]  # derived from (ws, user, agent): stable per user
    assert await ctx.ensure_dm_rooms() == rooms and len(created) == 2  # state wins: no second POST


async def test_ensure_dm_rooms_refuses_without_lightbulb_member():
    ctx = Ctx(ws="org_on", users={"sam": "user_sam"})
    ctx.api = lambda user, ws=None: httpx.AsyncClient(
        base_url=ctxmod.API_ROOT, transport=httpx.MockTransport(dm_handler([], lightbulb=False))
    )  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="@lightbulb"):
        await ctx.ensure_dm_rooms()


async def test_a12_runs_the_dm_lookup_before_its_bodies(monkeypatch):
    seen: list[dict] = []

    async def a12_router(ctx):
        seen.append(dict(ctx.state))
        return Result(True, {"ok": True})

    monkeypatch.setattr(report, "resolve", lambda part: a12_router if part == "a12_router_lanes:a12_router" else None)
    ctx = FakeCtx()

    async def ensure_dm_rooms():
        ctx.state["samDmRoomId"], ctx.state["priyaDmRoomId"] = "dm-s", "dm-p"
        return {"sam": "dm-s", "priya": "dm-p"}

    ctx.ensure_dm_rooms = ensure_dm_rooms  # type: ignore[attr-defined]
    result = await report.run_scenario("A12", ctx)
    assert seen == [{"samDmRoomId": "dm-s", "priyaDmRoomId": "dm-p"}]
    assert not result.passed  # M's body is still missing: the report says so, the lookup still ran

    async def broken():
        raise RuntimeError("no #general")

    ctx.ensure_dm_rooms = broken  # type: ignore[attr-defined]
    result = await report.run_scenario("A12", ctx)
    assert result.evidence["dm_rooms"]["error"] == "RuntimeError: no #general"
