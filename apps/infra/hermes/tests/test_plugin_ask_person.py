"""ask_person: a question (or a blocked report) lands in the person's Needs you and wakes the agent."""
import io
import json
import pathlib
import sys
import types
import urllib.error

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

API = "http://paperclip.internal"
COMPANY = "c0000000-0000-4000-8000-000000000000"
ME = "a0000000-0000-4000-8000-000000000000"
ISSUE = "i0000000-0000-4000-8000-000000000006"
RUN = "r0000000-0000-4000-8000-000000000001"


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def paperclip(tmp_path, monkeypatch):
    """A fake Paperclip behind _paperclip_call, a task run's secret scope, and its facts file."""
    scope = {"PAPERCLIP_API_URL": API + "/", "PAPERCLIP_API_KEY": "pcp_test_key",
             "PAPERCLIP_TASK_ID": ISSUE, "PAPERCLIP_RUN_ID": RUN}
    fake = types.ModuleType("agent.secret_scope")
    fake.get_secret = scope.get
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    (tmp_path / "lightbulb-facts.json").write_text(json.dumps({"app_url": "https://dev.example/"}))

    state = {"calls": [], "fail": {}, "scope": scope}
    issues = {"WAL-6": {"id": ISSUE, "identifier": "WAL-6", "companyId": COMPANY},
              ISSUE: {"id": ISSUE, "identifier": "WAL-6", "companyId": COMPANY}}

    def call(method, url, key, body=None, run_id=None):
        assert key == "pcp_test_key"
        path = url.removeprefix(API)
        state["calls"].append((method, path, body, run_id))
        if (method, path) in state["fail"]:
            raise state["fail"][(method, path)]
        if path == "/api/agents/me":
            return {"id": ME, "companyId": COMPANY}
        if path.startswith("/api/issues/") and method == "GET":
            ref = path.rsplit("/", 1)[1]
            if ref not in issues:
                raise _http_error(404, {"error": "Issue not found"})
            return issues[ref]
        if path == f"/api/companies/{COMPANY}":
            return {"id": COMPANY, "issuePrefix": "WAL"}
        if path == f"/api/issues/{ISSUE}/interactions":
            return {"id": "ix-1", **body}
        return {}

    monkeypatch.setattr(lightbulb, "_paperclip_call", call)
    return state


def _writes(state):
    return [c for c in state["calls"] if c[0] in ("POST", "PATCH")]


def test_question_with_options_creates_one_ask_user_questions_interaction_on_the_runs_task(paperclip):
    out = json.loads(lightbulb._ask_person({
        "question": "Which Stripe account should I use?", "options": ["Live", "Test"]}))
    [(method, path, body, run_id)] = _writes(paperclip)
    assert (method, path) == ("POST", f"/api/issues/{ISSUE}/interactions")
    assert body["kind"] == "ask_user_questions"
    assert body["continuationPolicy"] == "wake_assignee"
    [question] = body["payload"]["questions"]
    assert question["selectionMode"] == "single"
    assert [o["label"] for o in question["options"]] == ["Live", "Test"]
    assert run_id == RUN
    assert out["blocked"] is False
    assert out["interactionId"] == "ix-1"
    assert out["link"].startswith("https://dev.example/")


def test_no_options_is_one_free_text_answer(paperclip):
    lightbulb._ask_person({"question": "What is the client's billing email?"})
    [(_, _, body, _)] = _writes(paperclip)
    [option] = body["payload"]["questions"][0]["options"]
    assert option["freeText"] is True


def test_details_ride_as_the_questions_help_text(paperclip):
    lightbulb._ask_person({"question": "Send now?", "options": ["Yes", "No"], "details": "The draft is in WAL-6."})
    [(_, _, body, _)] = _writes(paperclip)
    assert body["payload"]["questions"][0]["helpText"] == "The draft is in WAL-6."


def test_same_question_twice_uses_the_same_idempotency_key(paperclip):
    args = {"question": "Which Stripe account should I use?", "options": ["Live", "Test"]}
    lightbulb._ask_person(args)
    lightbulb._ask_person(args)
    first, second = (c[2]["idempotencyKey"] for c in _writes(paperclip))
    assert first == second


def test_blocked_sets_the_task_blocked_self_owned_before_asking(paperclip):
    out = json.loads(lightbulb._ask_person({
        "question": "I need Codex review credits. Can you add them?", "options": ["Added", "Skip review"],
        "blocked": True}))
    patch, post = _writes(paperclip)
    assert patch[:2] == ("PATCH", f"/api/issues/{ISSUE}")
    assert patch[2]["status"] == "blocked"
    assert patch[2]["unblockDescriptor"]["owner"] == {"agentId": ME}
    assert post[:2] == ("POST", f"/api/issues/{ISSUE}/interactions")
    assert out["blocked"] is True


def test_blocked_when_already_blocked_still_asks(paperclip):
    paperclip["fail"][("PATCH", f"/api/issues/{ISSUE}")] = _http_error(422, {"error": "already blocked"})
    out = json.loads(lightbulb._ask_person({"question": "Can you unlock it?", "options": ["Done", "No"],
                                            "blocked": True}))
    assert any(c[:2] == ("POST", f"/api/issues/{ISSUE}/interactions") for c in paperclip["calls"])
    assert out["blocked"] is True


def test_room_turn_without_a_task_asks_in_the_reply_and_calls_nothing(paperclip):
    del paperclip["scope"]["PAPERCLIP_TASK_ID"]
    out = lightbulb._ask_person({"question": "Which one?", "options": ["A", "B"]})
    assert paperclip["calls"] == []
    assert "ask in your reply" in out


def test_named_task_is_resolved_by_identifier(paperclip):
    del paperclip["scope"]["PAPERCLIP_TASK_ID"]
    json.loads(lightbulb._ask_person({"question": "Which one?", "options": ["A", "B"], "task": "WAL-6"}))
    assert ("POST", f"/api/issues/{ISSUE}/interactions") in [c[:2] for c in paperclip["calls"]]


def test_unknown_task_is_not_created(paperclip):
    out = lightbulb._ask_person({"question": "Which one?", "options": ["A", "B"], "task": "WAL-99"})
    assert out == "Question not created: task WAL-99 was not found."
    assert _writes(paperclip) == []


def test_refused_create_says_not_created(paperclip):
    paperclip["fail"][("POST", f"/api/issues/{ISSUE}/interactions")] = _http_error(403, {"error": "Forbidden"})
    out = lightbulb._ask_person({"question": "Which one?", "options": ["A", "B"]})
    assert out.startswith("Question not created: HTTP 403")


def test_more_than_four_options_is_refused_before_any_call(paperclip):
    out = lightbulb._ask_person({"question": "Which?", "options": ["A", "B", "C", "D", "E"]})
    assert "2 to 4 options" in out
    assert paperclip["calls"] == []


def test_ask_person_is_registered_and_listed_in_the_manifest(monkeypatch):
    registered = {}
    monkeypatch.setattr(lightbulb, "_register_redaction_default", lambda: 0)

    class Registry:
        def register_hook(self, *_a):
            pass

        def register_tool(self, name, toolset, schema, handler, **_kw):
            registered[name] = (schema, handler)

    lightbulb.register(Registry())
    manifest = (pathlib.Path(lightbulb.__file__).parent / "plugin.yaml").read_text()
    assert registered["ask_person"] == (lightbulb.ASK_PERSON_SCHEMA, lightbulb._ask_person)
    assert "  - ask_person\n" in manifest
