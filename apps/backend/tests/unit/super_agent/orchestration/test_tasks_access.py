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
        # A DM task carries the DM's id (Tasks pane); priya, now a member of r1 (the DM became a group chat), still
        # never sees sam's DM task.
        ({"kind": "person", "userIds": ["sam"], "roomId": "r1"}, "sam", True),
        ({"kind": "person", "userIds": ["sam"], "roomId": "r1"}, "priya", False),
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
        ("proposed", [], "sam", {}, [], "resolved"),  # a row from before 5 Nov 2026 reads as Closed
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
                "startedAt": "2026-11-04T10:04:00Z",
                "endedAt": "2026-11-04T10:05:02Z",
            },
        ],
        "subscriptions": [{"threadKey": "room:r1:m1", "createdBy": "router", "active": True}],
        "questions": [],
        "effects": [
            {
                "idemKey": "e1",
                "kind": "mail.send",
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
    # The chip's timer: the latest run's start and end (dev walk 12, W12-1).
    assert (s["activity"]["startedAt"], s["activity"]["endedAt"]) == ("2026-11-04T10:04:00Z", "2026-11-04T10:05:02Z")
    assert s["costUsd"] == 0.4 and s["budgetUsd"] == 5.0 and s["audienceKind"] == "room"
    assert s["output"] == {"idemKey": "e1", "kind": "mail", "title": "Reply to Acme", "nextStep": "send"}


def test_summarize_fields_match_schema_export():
    tree = {"task": _task({"kind": "person", "userIds": ["sam"]}), "attempts": [], "subscriptions": []}
    s = ta.summarize(tree, "sam")
    exported = ts.export_schema()["views"]["TaskSummary"]["properties"]
    assert set(s) == set(exported)
    ts.TaskSummary.model_validate(s)


# --- UX slice 5: what a task made stays on it for every status ----------------------------------------------------


def test_summarize_carries_outputs_for_a_finished_task():
    made = [{"id": "app:a1", "kind": "app", "title": "Booking", "ref": {"appId": "a1"}, "createdAt": "t1"}]
    tree = {"task": _task({"kind": "person", "userIds": ["sam"]}, status="resolved", outputs=made), "attempts": []}
    s = ta.summarize(tree, "sam")
    assert s["outputs"] == made and s["output"] is None  # `output` stays the draft waiting on a review
    assert ta.summarize({"task": _task({"kind": "person", "userIds": ["sam"]})}, "sam")["outputs"] == []  # older row


def _effect(**over):
    return {
        "idemKey": "t:1:c1",
        "taskId": TID,
        "kind": "mail.send",
        "state": "done",
        "operands": {"to": "a@b.c", "subject": "Q3"},
        "resultRef": "msg_1",
    } | over


@pytest.mark.parametrize(
    "effect,expected",
    [
        (
            _effect(),
            {
                "id": "effect:t:1:c1",
                "kind": "mail",
                "title": "Send 'Q3' to a@b.c",
                "ref": {"idemKey": "t:1:c1", "to": "a@b.c", "subject": "Q3", "resultRef": "msg_1"},
            },
        ),
        (
            _effect(kind="pr.merge", operands={"repo": "o/r", "number": 7}, resultRef="pr:o/r#7:abc"),
            {
                "id": "effect:t:1:c1",
                "kind": "pr",
                "title": "Merge PR #7 in o/r",
                "ref": {
                    "idemKey": "t:1:c1",
                    "repo": "o/r",
                    "number": "7",
                    "url": "https://github.com/o/r/pull/7",
                    "resultRef": "pr:o/r#7:abc",
                },
            },
        ),
        (_effect(resultRef="refused:rate_limited"), None),  # didn't happen
        (_effect(state="unknown"), None),
        (_effect(state="intent"), None),
        (_effect(kind="purchase", operands={"merchant": "Porkbun"}), None),  # an approval kind: nothing made
    ],
)
def test_landed_effect_output(effect, expected):
    assert ta.effect_output(effect) == expected


def test_file_output_is_a_document_or_a_file_in_the_task_room():
    room = ts.task_room_id(TID)
    doc = ta.file_output(TID, {"attachmentId": "f1", "fileName": "agenda.docx", "contentType": ta.DOC_TYPES[0]})
    assert doc == {
        "id": "file:f1",
        "kind": "document",
        "title": "agenda.docx",
        "ref": {"roomId": room, "attachmentId": "f1", "fileName": "agenda.docx", "contentType": ta.DOC_TYPES[0]},
    }
    assert (
        ta.file_output(TID, {"attachmentId": "f2", "fileName": "a.png", "contentType": "image/png"})["kind"] == "file"
    )


def test_effect_refs_are_capped():
    out = ta.effect_output(_effect(operands={"to": "a" * 500, "subject": "s" * 500}, resultRef="r" * 500))
    assert all(len(v) <= 200 for v in out["ref"].values())


async def test_output_writes_never_fail_their_caller(monkeypatch):
    """Review fix: an effect's done and a run's end matter more than the output row."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(ta.tasks_repo, "add_task_output", AsyncMock(side_effect=RuntimeError("ddb down")))
    await ta.record_effect_output(WS, _effect())
    await ta.record_output(WS, TID, {"id": "file:f1", "kind": "file", "title": "a"})
    await ta.record_effect_output(WS, {"state": "done"})  # malformed row: logged, not raised
    assert ta.tasks_repo.add_task_output.await_count == 2


# --- Dev walk 10: the main chat's one-line result (W10-1, W10-2) -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("## Taglines\n\n- [Linear](https://linear.app) `ships` _fast_.", "Linear ships fast."),  # heading skipped
        ("# Three taglines", "Three taglines"),  # a heading alone is the line
        ("```python\nprint(1)\n```\nIt prints 1.", "It prints 1."),  # a leading code block says nothing
        ("| Brand | Tagline |\n|---|---|\n| Linear | Fast |", "Brand · Tagline Linear · Fast"),  # no pipes
        ("5 * 3 = 15, and 2 * 4 = 8.", "5 * 3 = 15, and 2 * 4 = 8."),  # arithmetic, not italics
        ("See [the docs (v2)](https://x.dev/a_(b)) now.", "See the docs (v2) now."),  # nested parentheses
        ("Read <https://x.dev> and [the guide][1].\n[1]: https://x.dev/g", "Read https://x.dev and the guide."),
        ("<p>Done <b>today</b>.</p>", "Done today."),  # raw HTML
        ("Use snake_case names.", "Use snake_case names."),
        ("![chart](https://x.dev/c.png)", ""),  # an image says nothing: the caller falls back
    ],
)
def test_plain_line_strips_markdown(text, line):
    assert ta.plain_line(text) == line


def test_plain_line_ends_at_a_sentence_within_the_limit():
    walk = (
        '**"Your competitors say fast."** Linear: "Built for speed." Notion: "One workspace." Asana leads with teams.'
    )
    line = ta.plain_line(walk)
    assert "*" not in line and len(line) <= ta.LINE_MAX and line.endswith(".")
    assert line.startswith('"Your competitors say fast."')
    cut = ta.plain_line("word " * 60)  # one sentence longer than the line: cut at a word, never mid-word
    assert len(cut) <= ta.LINE_MAX and cut.endswith("word…")
    cjk = "我们找到了三条竞争对手的标语。" * 12  # CJK: its own sentence ends, and no space needed to cut
    assert ta.plain_line(cjk).endswith("。") and len(ta.plain_line(cjk)) <= ta.LINE_MAX
    long_cjk = "竞" * 200
    assert len(ta.plain_line(long_cjk)) == ta.LINE_MAX and ta.plain_line(long_cjk).endswith("…")


def test_result_line_is_the_first_sentence_of_the_reply():
    """Walk 14 (W14-6): the finish line read "Drafted the launch email …, with an 8% seven-day signup target. It
    features …": the whole reply. It is the first sentence only, what was made; the rest stays in the task."""
    reply = "Drafted the welcome email (cafe-menu.md). Suggestion: aim for 8% signups in seven days."
    att = [{"n": 1, "status": "ended", "outcome": "completed", "output": reply}]
    tree = {"task": _task({"kind": "room", "roomId": "r1"}, status="idle"), "attempts": att}
    assert ta.result_line(tree, "r1") == "Drafted the welcome email (cafe-menu.md)."
    one = [{**att[0], "output": "Fees on a $50.25 ticket are $1.76 (2.9% + 30¢)"}]  # no sentence end: all of it
    assert ta.result_line({**tree, "attempts": one}, "r1") == "Fees on a $50.25 ticket are $1.76 (2.9% + 30¢)"


def test_result_line_follows_the_privacy_rule():
    done = {"kind": "room", "roomId": "r1"}
    att = [{"n": 1, "status": "ended", "outcome": "completed", "output": "**Paid** Acme.\n\nDetails…"}]
    tree = {"task": _task(done, status="idle"), "attempts": att}
    assert ta.result_line(tree, "r1") == "Paid Acme."
    assert ta.result_line({**tree, "task": _task(done)}, "r1") is None  # still working
    # Someone in the room can't open it: no words there; a person-only task never shows any.
    assert ta.result_line({**tree, "task": _task({"kind": "room", "roomId": "r2"}, status="idle")}, "r1") is None
    assert ta.result_line({**tree, "task": _task({"kind": "person", "userIds": ["sam"]}, status="idle")}, "r1") is None
    # A person task in its own DM: the line is whole while that person is everyone there, bare once someone joins.
    dm_task = {**tree, "task": _task({"kind": "person", "userIds": ["sam"], "roomId": "r1"}, status="idle")}
    assert ta.result_line(dm_task, "r1", ["sam"]) == "Paid Acme."
    assert ta.result_line(dm_task, "r1", ["sam", "priya"]) is None
    assert ta.result_line(dm_task, "r1") is None  # membership unknown: never
    assert ta.result_line(dm_task, "r2", ["sam"]) is None  # another room
    failed = _task({"kind": "workspace"}, status="failed", statusReason="refused", watchUntil="2026-11-10")
    assert ta.result_line({**tree, "task": failed}, "r1") == "Didn't finish: the computer it runs on refused it."
    image = [{**att[0], "output": "![chart](https://x.dev/c.png)"}]
    assert ta.result_line({"task": _task(done, status="idle"), "attempts": image}, "r1") == "Pay the invoice"


def test_a_short_result_output_carries_its_words_and_an_email_draft_is_mail():
    """Walk 16: a library item opened its task and was named after it; an email draft was filed as Doc."""
    task = {"taskId": TID, "title": "Draft parent email"}
    mail = ta.text_output(task, 1, "m1", "Drafted the parent email.\n\nSubject: Story-time\n\nHi parents,\n\n[Sign up]")
    assert mail["kind"] == "mail" and mail["ref"]["text"] == "Subject: Story-time\n\nHi parents,\n\n[Sign up]"
    post = ta.text_output(task, 2, "m2", "Saturday story-time is back!")
    assert (
        post["id"] == "text:2" and post["kind"] == "document" and post["ref"]["text"] == "Saturday story-time is back!"
    )
