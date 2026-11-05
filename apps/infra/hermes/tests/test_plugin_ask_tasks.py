"""ask_person / request_approval on a super-agent task run (wave2-Q Task 7): the question goes to the backend's
internal ask route under the run's identity, never to Paperclip; a blocking ask marks the turn to end
`waiting_approval`; room and Paperclip runs are unchanged (tests/test_plugin_ask_person.py keeps the golden path)."""
import io
import json
import pathlib
import sys
import urllib.error

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
RUN = "0b5c6c39-6a43-5b7e-9d1a-3f0f2d7c1a11"
CTX = {"family": "task", "mode": "worker", "runId": RUN, "taskId": TASK, "attempt": 3, "runSecret": "s3", "agentId": "agent-1",
       "ownerId": "org_test", "apiUrl": "https://api.example/", "hookToken": "tok", "sessionKey": "agent:x:lightbulb:task:y",
       "roomId": None}
URL = "https://api.example/api/v1/internal/runtime/org_test/questions"


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def task_run(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    monkeypatch.setattr(turn_context, "read", lambda sid: dict(CTX) if sid == "s1" else None)
    state = {"posts": [], "answer": {"questionId": "q-1", "state": "open", "waitingLabel": "Waiting on Sam"}}

    def post(url, token, body):
        state["posts"].append((url, token, body))
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return state["answer"]

    monkeypatch.setattr(lightbulb, "_post", post)
    monkeypatch.setattr(lightbulb, "_paperclip_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Paperclip called")))
    state["home"] = tmp_path
    return state


def test_task_run_ask_posts_internal_route(task_run):
    out = lightbulb._ask_person(
        {"question": "Which Stripe account?", "options": ["Live", "Test"], "details": "Both are connected.", "blocked": True},
        session_id="s1",
    )
    [(url, token, body)] = task_run["posts"]
    assert (url, token) == (URL, "tok")
    assert body["taskId"] == TASK and body["runId"] == RUN and body["attempt"] == 3 and body["runSecret"] == "s3"
    assert (body["kind"], body["subkind"], body["blocking"]) == ("question", "question", True)
    assert body["title"] == "Which Stripe account?" and body["text"] == "Both are connected."
    assert body["options"] == [{"id": "o1", "label": "Live"}, {"id": "o2", "label": "Test"}]
    assert body["toolCallId"].startswith("ask-")
    assert "Waiting on Sam" in out and "q-1" in out and "End your turn" in out
    assert turn_context.read_waiting(task_run["home"], RUN) == "q-1"


def test_same_question_twice_is_one_tool_call_id(task_run):
    for _ in range(2):
        lightbulb._ask_person({"question": "Which one?", "options": ["A", "B"]}, session_id="s1")
    ids = {body["toolCallId"] for _, _, body in task_run["posts"]}
    assert len(ids) == 1  # a retried ask replays on the backend instead of opening a second question


def test_non_blocking_question_does_not_end_the_turn(task_run):
    out = lightbulb._ask_person({"question": "Preferred tone?"}, session_id="s1")
    [(_, _, body)] = task_run["posts"]
    assert body["blocking"] is False and body["options"] == []
    assert "End your turn" not in out and "arrives as input" in out
    assert turn_context.read_waiting(task_run["home"], RUN) is None


def test_task_run_request_approval_posts_approval(task_run):
    out = lightbulb._request_approval(
        {"title": "Pay Acme $40 for hosting", "summary": "Needed for the preview.", "subkind": "provider_consent"},
        session_id="s1",
    )
    [(url, _, body)] = task_run["posts"]
    assert url == URL
    assert (body["kind"], body["subkind"], body["blocking"]) == ("approval", "provider_consent", True)
    assert body["options"] == [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}]
    assert body["title"] == "Pay Acme $40 for hosting" and body["text"] == "Needed for the preview."
    assert "End your turn" in out and turn_context.read_waiting(task_run["home"], RUN) == "q-1"


def test_unknown_subkind_falls_back_to_tool(task_run):
    for subkind in ("root", "publish", "a2a_hold"):
        lightbulb._request_approval({"title": "t", "summary": "s", "subkind": subkind}, session_id="s1")
    assert [p[2]["subkind"] for p in task_run["posts"]] == ["tool", "tool", "tool"]


def test_only_stale_run_and_closed_task_stop_the_turn(task_run):
    task_run["answer"] = _http_error(409, {"detail": "not_opened"})
    out = lightbulb._ask_person({"question": "x", "blocked": True}, session_id="s1")
    assert out == "Question not created: HTTP 409 (not_opened)." and turn_context.read_waiting(task_run["home"], RUN) is None
    task_run["answer"] = _http_error(409, {"detail": "task_closed"})
    assert lightbulb._ask_person({"question": "x", "blocked": True}, session_id="s1") == lightbulb.TASK_CLOSED
    task_run["answer"] = _http_error(422, {"detail": [{"msg": "bad"}]})
    assert lightbulb._ask_person({"question": "x"}, session_id="s1") == "Question not created: HTTP 422."


def test_replayed_closed_question_writes_no_marker(task_run):
    task_run["answer"] = {"questionId": "q-1", "state": "answered", "waitingLabel": None}
    out = lightbulb._ask_person({"question": "x", "blocked": True}, session_id="s1")
    assert "already answered" in out and "End your turn" not in out
    assert turn_context.read_waiting(task_run["home"], RUN) is None


def test_task_family_without_the_attempt_identity_takes_todays_path(task_run, monkeypatch):
    """A Router turn, or a task ctx without runSecret, is not a Worker attempt: no internal ask."""
    monkeypatch.setattr(turn_context, "read", lambda sid: {k: v for k, v in CTX.items() if k != "runSecret"})
    assert lightbulb._ask_person({"question": "x"}, session_id="s1") == lightbulb.ASK_IN_REPLY
    monkeypatch.setattr(turn_context, "read", lambda sid: {**CTX, "mode": "router"})
    assert lightbulb._ask_person({"question": "x"}, session_id="s1") == lightbulb.ASK_IN_REPLY
    assert task_run["posts"] == []


def test_internal_ask_stale_run_409(task_run):
    task_run["answer"] = _http_error(409, {"detail": "stale_run"})
    out = lightbulb._ask_person({"question": "Still there?"}, session_id="s1")
    assert "no longer" in out.lower() and "stop" in out.lower()
    assert turn_context.read_waiting(task_run["home"], RUN) is None


def test_backend_down_is_reported_not_raised(task_run):
    task_run["answer"] = OSError("down")
    out = lightbulb._ask_person({"question": "x", "blocked": True}, session_id="s1")
    assert out.startswith("Question not created") and turn_context.read_waiting(task_run["home"], RUN) is None


def test_room_run_never_takes_the_task_path(task_run, monkeypatch):
    """A room or Paperclip turn keeps today's Paperclip path (its own suite is the golden): here, with no
    Paperclip credentials, it answers today's refusal and posts nothing to the task route."""
    room = {k: v for k, v in CTX.items() if k not in ("taskId", "attempt", "runSecret")} | {"family": "room", "roomId": "r1"}
    monkeypatch.setattr(turn_context, "read", lambda sid: room)
    assert lightbulb._ask_person({"question": "x"}, session_id="s1") == lightbulb.ASK_IN_REPLY
    assert lightbulb._request_approval({"title": "t", "summary": "s"}, session_id="s1") == lightbulb.APPROVAL_NO_KEY
    monkeypatch.setattr(turn_context, "read", lambda sid: None)
    assert lightbulb._ask_person({"question": "x"}) == lightbulb.ASK_IN_REPLY
    assert task_run["posts"] == []


def test_waiting_marker_roundtrip(tmp_path):
    assert turn_context.read_waiting(tmp_path, RUN) is None
    turn_context.note_waiting(tmp_path, RUN, "q-9")
    assert turn_context.read_waiting(tmp_path, RUN) == "q-9"
    turn_context.clear_waiting(tmp_path, RUN)
    assert turn_context.read_waiting(tmp_path, RUN) is None


def test_marker_path_is_the_bridges(tmp_path):
    from bridge import effects

    turn_context.note_waiting(tmp_path, RUN, "q-9")
    assert effects.waiting_marker_path(tmp_path, RUN).read_text() == '{"questionId": "q-9"}'
    assert turn_context.read_waiting(tmp_path, RUN) == "q-9"


def test_publish_is_not_a_subkind_the_schema_offers():
    enum = lightbulb.APPROVAL_SCHEMA["parameters"]["properties"]["subkind"]["enum"]
    assert "publish" not in enum and set(enum) == set(lightbulb.TASK_SUBKINDS)
