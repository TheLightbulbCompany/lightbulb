"""suggest_idea: an idea for this company joins the Ideas list; ids come from the turn, never the model."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

SESSION = "agent:growth:lightbulb:dm:r"
ARGS = {"title": "Answer the 14 open reviews", "description": "Fourteen reviews have no reply.",
        "deliverables": ["Every review answered", " ", "Refunds flagged", "a", "b"], "size": "task",
        "agentId": "evil", "ownerId": "evil"}


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, {"apiUrl": "https://api.invalid/", "ownerId": "org_test",
                                           "hookToken": "h", "agentId": "growth", "runId": "r1"})


def test_posts_to_this_owners_suggestions_as_the_turns_agent(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append((url, token, body))
                        or {"status": "added", "id": "s-1"})
    out = lightbulb._suggest_idea(ARGS, session_id=SESSION)
    assert "Ideas list" in out and "Do not post it in chat" in out
    [(url, token, body)] = calls
    assert url == "https://api.invalid/api/v1/internal/ideas/suggestions/org_test" and token == "h"
    assert body == {"agentId": "growth", "title": "Answer the 14 open reviews",
                    "why": "Fourteen reviews have no reply.",
                    "deliverables": ["Every review answered", "Refunds flagged", "a"], "size": "task"}
    lightbulb._suggest_idea({**ARGS, "emoji": " ⭐ "}, session_id=SESSION)
    assert calls[1][2]["emoji"] == "⭐"


def test_an_app_idea_passes_kind_app_kind_and_origin_through(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append(body) or {"status": "added"})
    lightbulb._suggest_idea({**ARGS, "kind": "app", "app_kind": "dashboard", "origin": " Asked weekly "},
                            session_id=SESSION)
    assert (calls[0]["kind"], calls[0]["appKind"], calls[0]["origin"]) == ("app", "dashboard", "Asked weekly")


@pytest.mark.parametrize("status, words", [("duplicate", "Already in the Ideas list"), ("cap", "10 suggestions"),
                                           ("run_cap", "already filed 3 ideas")])
def test_says_when_nothing_was_added(tmp_path, monkeypatch, status, words):
    _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(lightbulb, "_post", lambda *a: {"status": status})
    assert words in lightbulb._suggest_idea(ARGS, session_id=SESSION)


def test_refused_outside_a_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    monkeypatch.setattr(lightbulb, "_post", lambda *a: pytest.fail("no HTTP call"))
    assert lightbulb._suggest_idea(ARGS, session_id="none") == lightbulb.SUGGEST_NO_TURN


def test_is_registered_and_never_renders_a_card():
    assert lightbulb.SUGGEST_IDEA_SCHEMA["name"] == "suggest_idea"
    assert "not in chat" in lightbulb.SUGGEST_IDEA_SCHEMA["description"]
    assert json.dumps(lightbulb.SUGGEST_IDEA_SCHEMA["parameters"]["properties"]["size"]["enum"]) == '["project", "task", "quick"]'


def test_a_worker_run_sends_its_task_so_one_request_files_at_most_three(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, {"apiUrl": "https://api.invalid/", "ownerId": "org_test", "hookToken": "h",
                                           "agentId": "growth", "taskId": "t-1", "runSecret": "x"})
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append(body) or {"status": "added"})
    lightbulb._suggest_idea({**ARGS, "taskId": "evil"}, session_id=SESSION)
    assert calls[0]["taskId"] == "t-1"
