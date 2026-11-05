"""`can_view` is the one visibility rule (contracts §5); `summarize` is the one TaskSummary (C15, §4).
Review focus 4: Priya (room member) never sees Sam's DM-born task; an ex-member loses a room task."""

import pytest

from core import tasks_schema as ts
from core.services import tasks_access as ta

WS = "org_1"
TID = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


def _task(audience, **over):
    return {
        "pk": ts.ws_pk(WS),
        "sk": ts.task_sk(TID),
        "type": "task",
        "taskId": TID,
        "identifier": "LB-7",
        "title": "Pay the invoice",
        "status": "working",
        "audience": audience,
        "lane": "shared",
        "ownerUserId": "sam",
        "triggerId": "chat:m1",
        "version": 4,
        "updatedAt": "2026-11-04T10:00:00Z",
    } | over


@pytest.fixture
def world(monkeypatch):
    """Room r1 has priya now (ex was removed); org_1 has sam and priya; ann is an answerer (in audience.userIds)."""
    room = {("r1", "priya"), ("r1", "sam")}
    orgs = {"sam": {WS}, "priya": {WS}, "ex": set()}

    async def get_member(room_id, kind, member_id):
        assert kind == "human"
        return {"id": member_id} if (room_id, member_id) in room else None

    async def orgs_of(user_id):
        return orgs.get(user_id, set())

    monkeypatch.setattr(ta.messaging_repo, "get_member", get_member)
    monkeypatch.setattr(ta, "_user_org_ids", orgs_of)


@pytest.mark.parametrize(
    "audience,user,expected",
    [
        ({"kind": "person", "userIds": ["sam"]}, "sam", True),
        ({"kind": "person", "userIds": ["sam"]}, "priya", False),
        ({"kind": "person", "userIds": ["sam", "ann"]}, "ann", True),  # answerer added by Q when a question opens
        ({"kind": "room", "roomId": "r1", "userIds": []}, "priya", True),
        ({"kind": "room", "roomId": "r1", "userIds": []}, "ex", False),
        ({"kind": "workspace", "userIds": []}, "priya", True),
        ({"kind": "workspace", "userIds": []}, "outsider", False),
        ({"kind": "workspace", "userIds": []}, "", False),
    ],
)
async def test_can_view_matrix(world, audience, user, expected):
    assert await ta.can_view(_task(audience), user) is expected


async def test_personal_workspace_member_is_its_owner(monkeypatch):
    task = _task({"kind": "workspace"}, pk=ts.ws_pk("user_sam"))
    assert await ta.can_view(task, "user_sam") is True


def _q(kind, answerers, state="open", qid="q1"):
    return {
        "type": "question",
        "taskId": TID,
        "qid": qid,
        "kind": kind,
        "subkind": None,
        "title": "Pay $40?",
        "answerers": answerers,
        "state": state,
        "options": [{"id": "yes", "label": "Approve"}],
    }


@pytest.mark.parametrize(
    "status,questions,viewer,task_over,effects,group",
    [
        ("working", [_q("approval", ["sam"])], "sam", {}, [], "waiting_on_you"),
        ("waiting", [_q("approval", ["sam"])], "priya", {}, [], "working"),
        ("waiting", [_q("review", ["sam"])], "sam", {}, [], "ready_for_review"),
        ("waiting", [_q("approval", ["sam"], state="answered")], "sam", {}, [], "working"),
        ("working", [], "sam", {"landingEffect": "idem1"}, [], "landing"),
        ("working", [], "sam", {}, [{"idemKey": "e", "state": "intent"}], "landing"),
        ("idle", [], "sam", {"watchUntil": "2026-11-11T00:00:00Z"}, [], "idle"),
        ("idle", [], "sam", {}, [], "idle"),
        ("resolved", [], "sam", {}, [], "resolved"),
        ("failed", [], "sam", {}, [], "waiting_on_you"),
        ("failed", [], "priya", {}, [], "idle"),
        ("proposed", [], "sam", {}, [], None),
        ("queued", [], "sam", {}, [], "working"),
    ],
)
def test_summarize_shown_group(status, questions, viewer, task_over, effects, group):
    tree = {
        "task": _task({"kind": "room", "roomId": "r1"}, status=status, **task_over),
        "attempts": [],
        "subscriptions": [],
        "questions": questions,
        "effects": effects,
    }
    assert ta.summarize(tree, viewer)["shownGroup"] == group


def test_summarize_open_question_for_someone_else():
    tree = {
        "task": _task({"kind": "room", "roomId": "r1"}, status="waiting"),
        "attempts": [],
        "subscriptions": [],
        "questions": [_q("approval", ["sam"])],
        "userNames": {"sam": "Sam"},
    }
    s = ta.summarize(tree, "priya")
    assert s["openQuestion"]["answerers"] == ["sam"]
    assert s["openQuestion"]["answererNames"] == ["Sam"]
    assert s["openQuestion"]["canAnswer"] is False
    assert ta.summarize(tree, "sam")["openQuestion"]["canAnswer"] is True


def test_summarize_derived_fields():
    tree = {
        "task": _task(
            {"kind": "room", "roomId": "r1"},
            status="idle",
            watchUntil="2026-11-11T00:00:00Z",
            attemptCount=2,
            budgetUsd=5.0,
            usage={"inputTokens": 10, "outputTokens": 5, "costUsd": 0.4},
        ),
        "attempts": [
            {"n": 1, "status": "ended", "outcome": "completed", "sessionId": "lb-org_1-t-abc", "inputTriggerIds": []},
            {
                "n": 2,
                "status": "ended",
                "outcome": "completed",
                "sessionId": "lb-org_1-t-abc",
                "inputTriggerIds": ["chat:m9"],
                "lastHeartbeatAt": "2026-11-04T10:05:00Z",
            },
        ],
        "subscriptions": [{"threadKey": "room:r1:m1", "createdBy": "router", "active": True}],
        "questions": [],
        "effects": [
            {
                "idemKey": "e1",
                "state": "proposed",
                "questionId": "q2",
                "output": {"kind": "mail", "title": "Reply to Acme", "ref": {}},
            }
        ],
    }
    s = ta.summarize(tree, "sam")
    assert s["watching"] is True and s["shownGroup"] == "idle"
    assert s["originRoomId"] == "r1"
    assert s["taskRoomId"] == ts.task_room_id(TID)
    assert s["runsOn"] == "task"
    assert s["recentInputs"] == ["chat:m9"]
    assert s["activity"]["n"] == 2
    assert s["costUsd"] == 0.4 and s["budgetUsd"] == 5.0 and s["audienceKind"] == "room"
    assert s["output"] == {"idemKey": "e1", "kind": "mail", "title": "Reply to Acme", "nextStep": "approve"}


def test_summarize_fields_match_schema_export():
    tree = {"task": _task({"kind": "person", "userIds": ["sam"]}), "attempts": [], "subscriptions": []}
    s = ta.summarize(tree, "sam")
    exported = ts.export_schema()["views"]["TaskSummary"]["properties"]
    assert set(s) == set(exported)
    ts.TaskSummary.model_validate(s)
