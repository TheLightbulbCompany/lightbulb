"""A5, A6, A9, A10, A11, A12 (memory) over a fake Ctx: rows are lists, the API is an httpx.MockTransport."""

from __future__ import annotations

import httpx

from scripts.super_agent.acceptance import ctx as ctxmod
from scripts.super_agent.acceptance.a05_approval import a05
from scripts.super_agent.acceptance.a06_go_ahead import a06
from scripts.super_agent.acceptance.a09_landing import a09
from scripts.super_agent.acceptance.a10_a2a import a10
from scripts.super_agent.acceptance.a11_recurring import a11
from scripts.super_agent.acceptance.a12_private_lane import a12

T = "11111111-1111-4111-8111-111111111111"
SAM, PRIYA = "user_sam", "user_priya"


class Fake:
    def __init__(self, handler, rows=None):
        self.ws, self.users = "org", {"sam": SAM, "priya": PRIYA}
        self.state = {"taskIds": [T], "roomId": "r1", "priyaDmRoomId": "dm"}
        self.handler, self.rows = handler, rows if rows is not None else []

    def api(self, user, ws=None):
        def route(request: httpx.Request) -> httpx.Response:
            request.extensions["user"] = user
            return self.handler(request, user)

        return httpx.AsyncClient(base_url=ctxmod.API_ROOT, transport=httpx.MockTransport(route))

    async def tasks_rows(self, ws=None):
        return [dict(r) for r in self.rows]

    async def wait_for(self, pred, timeout_s, *, ws=None):
        return pred(await self.tasks_rows()) or None

    async def sleep(self, s):
        pass


def _json(data, status=200):
    return httpx.Response(status, json=data)


def _view(user, **over):
    sam = user == "sam"
    return {
        "id": "q1",
        "state": "open",
        "canAnswer": sam,
        "waitingLabel": "Waiting on you" if sam else "Waiting on Sam",
        "answerers": [{"id": SAM, "name": "Sam"}],
        "options": [{"id": "approve", "label": "Approve", "style": "primary"}],
        **over,
    }


def _approval_rows(**over):
    return [
        {"type": "task", "taskId": T, "status": "waiting"},
        {
            "type": "question",
            "taskId": T,
            "qid": "q1",
            "kind": "approval",
            "state": "open",
            "relay": {"roomId": "r1", "messageId": "m0"},
            **over,
        },
        {"type": "needs", "taskId": T, "qid": "q1", "userId": SAM},
    ]


async def test_a05_only_sam_can_approve():
    def handler(request, user):
        if request.method == "POST":
            return _json({}, 403 if user == "priya" else 200)
        return _json({"items": [_view(user)]})

    result = await a05(Fake(handler, _approval_rows()))
    assert result.passed, result.evidence
    bad = Fake(
        lambda r, u: _json({} if r.method == "POST" else {"items": [_view(u, canAnswer=True)]}), _approval_rows()
    )
    assert not (await a05(bad)).passed


async def test_a05_without_a_question_fails_with_reason():
    result = await a05(Fake(lambda r, u: _json({"items": []}), []))
    assert not result.passed and "no open approval" in result.evidence["reason"]


async def test_a06_go_ahead_leaves_the_question_open_then_sam_answers():
    rows = _approval_rows()
    ctx = Fake(None, rows)
    ctx.state.update(approvalTaskId=T, approvalQid="q1")
    posted = []

    def handler(request, user):
        path = request.url.path
        if path.endswith("/answer"):
            rows[1].update(state="answered", answeredBy=SAM)
            rows[0]["status"] = "working"
            return _json({})
        if request.method == "POST":
            posted.append(user)
            return _json({"messageId": "m1", "messageSeq": 5})
        if path.endswith("/messages"):
            return _json(
                {"messages": [{"author": {"kind": "agent"}, "messageSeq": 6, "text": "Only Sam can approve this."}]}
            )
        return _json({"items": [_view(user)]})

    ctx.handler = handler
    result = await a06(ctx)
    assert result.passed, result.evidence
    assert posted == ["priya"]


async def test_a09_lands_once_and_ends_idle():
    rows = [
        {"type": "task", "taskId": T},
        {"type": "question", "taskId": T, "qid": "q1", "kind": "review", "state": "open"},
        {
            "type": "effect",
            "taskId": T,
            "idemKey": "k",
            "kind": "mail.send",
            "state": "proposed",
            "operands": {"to": "sa-test-2+clerk_test@thelightbulb.company"},
        },
    ]
    rows[2]["operands"] = dict(rows[2]["operands"])
    ctx = Fake(None, rows)
    seen = {"group": "ready_for_review"}

    def handler(request, user):
        path = request.url.path
        if path.endswith("/answer"):
            rows[2].update(state="done", resultRef="ses:1", doorUsedAt="t")
            return _json({})
        if path.endswith("/questions"):
            return _json({"items": [_view("sam")]})
        detail = _json({"shownGroup": seen["group"]})
        seen["group"] = "idle" if seen["group"] == "ready_for_review" and rows[2]["state"] == "done" else seen["group"]
        return detail

    ctx.handler = handler
    result = await a09(ctx)
    assert result.passed, result.evidence
    assert result.evidence["groups"] == ["ready_for_review", "idle"]


async def test_a09_refuses_to_press_send_for_a_real_recipient():
    rows = [
        {"type": "question", "taskId": T, "qid": "q1", "kind": "review", "state": "open"},
        {
            "type": "effect",
            "taskId": T,
            "idemKey": "k",
            "kind": "mail.send",
            "state": "proposed",
            "operands": {"to": "ann@example.com"},
        },
    ]
    pressed = []
    ctx = Fake(lambda r, u: pressed.append(r.url.path) or _json({}), rows)
    result = await a09(ctx)
    assert not result.passed and result.evidence["reason"].startswith("refusing to press Send")
    assert pressed == []


async def test_a10_reports_why_it_did_not_run():
    result = await a10(Fake(None))
    assert not result.passed and result.evidence["reason"].startswith("blocked")


async def test_a12_canary_stays_with_priya():
    def handler(request, user):
        if request.url.path.endswith("/recall"):
            return _json(
                {"results": [{"text": "CANARY"}], "links": []} if user == "priya" else {"results": [], "links": []}
            )
        return _json({"messageId": "m", "messageSeq": 1})

    ctx = Fake(handler)
    ctx.state["samDmRoomId"] = "sdm"
    seen = {}

    def priya_sees_it(request, user):
        if request.url.path.endswith("/recall"):
            import json

            canary = json.loads(request.content)["question"].split()[2]
            seen["canary"] = canary
            return _json(
                {"results": [{"text": canary}], "links": []} if user == "priya" else {"results": [], "links": []}
            )
        if request.url.path.endswith("/messages") and request.method == "GET":
            return _json(
                {"messages": [{"author": {"kind": "agent"}, "messageSeq": 2, "text": "Two threads are moving."}]}
            )
        return _json({"messageId": "m", "messageSeq": 1})

    ctx.handler = priya_sees_it
    assert (await a12(ctx)).passed
    leak = Fake(
        lambda r, u: (
            _json({"results": [{"text": r.content.decode()}], "links": []})
            if r.url.path.endswith("/recall")
            else _json({"messageId": "m", "messageSeq": 1, "messages": []})
        )
    )
    leak.state["samDmRoomId"] = "sdm"
    assert not (await a12(leak)).passed


async def test_a11_schedule_lifecycle(monkeypatch):
    from scripts.super_agent.acceptance import a11_recurring as mod

    paused = {"v": False}

    async def temporal_paused(ws, sid):
        return paused["v"]

    async def tz(user_id):
        return "UTC"

    async def last_result(ws, task_id):
        return "Signups: 12"

    monkeypatch.setattr(mod, "_temporal_paused", temporal_paused)
    monkeypatch.setattr(mod.user_repo, "get_timezone", tz)
    monkeypatch.setattr(mod.task_conversation, "last_result_text", last_result)
    rows: list[dict] = []
    ctx = Fake(None, rows)

    def handler(request, user):
        path, method = request.url.path, request.method
        if path.endswith("/messages") and method == "POST":
            if "Monday" in request.content.decode():
                rows.append(
                    {"type": "schedule", "scheduleId": "s1", "status": "draft", "cron": ["0 9 * * 1"], "tz": "UTC"}
                )
            return _json({"messageId": "m", "messageSeq": 1})
        if path.endswith("/messages"):
            return _json(
                {
                    "messages": [
                        {
                            "kind": "automation_confirm",
                            "messageSeq": 2,
                            "author": {"kind": "agent"},
                            "text": "Mondays, I'll post signups.",
                            "attrs": {"automationId": "s1"},
                        },
                        {
                            "kind": "result",
                            "messageSeq": 3,
                            "author": {"kind": "agent"},
                            "taskRefs": [{"taskId": "t1"}],
                        },
                    ]
                }
            )
        if path.endswith("/activate"):
            return _json({"nextRuns": ["2026-11-09T09:00:00+00:00"]})
        if path.endswith("/run") and method == "POST":
            if any(r["type"] == "task" for r in rows):
                return _json({}, 409)
            rows.append({"type": "task", "taskId": "t1", "policyRef": "SCHED#s1"})
            return _json({"triggerId": "x"})
        if path.endswith("/runs"):
            return _json({"runs": [{"id": "t2"}, {"id": "t1"}]})
        if method == "PATCH":
            if not any(r.get("taskId") == "t2" for r in rows):  # the first PATCH is the 2-minute one-off
                rows.append(
                    {
                        "type": "task",
                        "taskId": "t2",
                        "policyRef": "SCHED#s1",
                        "previousRunTaskId": "t1",
                        "description": "Previous run (resolved): Signups: 12",
                    }
                )
            return _json({})
        if path.endswith("/pause"):
            paused["v"] = True
            return _json({})
        return _json({"version": 3})

    ctx.handler = handler
    result = await a11(ctx)
    assert result.passed, result.evidence


async def test_a02_two_started_tasks_and_the_estimate_inline():
    from scripts.super_agent.acceptance.a02_many_asks import a02
    from core import tasks_schema as ts

    rows: list[dict] = []
    ctx = Fake(None, rows)

    def handler(request, user):
        if request.url.path.endswith("/rooms/r1") and request.method == "GET":
            return _json({"members": [{"kind": "agent", "id": "front"}]})
        if request.method == "POST":
            trig = ts.trigger_id("chat", "m9")
            rows.extend(
                {"type": "task", "taskId": f"t{i}", "status": "working", "specialist": "s", "triggerId": trig}
                for i in (1, 2)
            )
            rows.append({"type": "trigger", "triggerId": trig, "admission": "respond", "routedTo": {"taskId": "t1"}})
            rows.append({"type": "subscription", "taskId": "t1", "createdBy": "router"})
            return _json({"messageId": "m9", "messageSeq": 1})
        return _json(
            {
                "messages": [
                    {
                        "author": {"kind": "agent", "id": "front"},
                        "taskRefs": [{}, {}],
                        "messageId": "c",
                        "text": "On 200 tickets Stripe takes about $1,200 (2.9% + 30c).",
                    }
                ]
            }
        )

    ctx.handler = handler
    result = await a02(ctx)
    assert result.passed, result.evidence
    assert ctx.state["proposedTaskId"] is None
