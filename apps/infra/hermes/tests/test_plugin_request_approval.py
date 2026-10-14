"""request_approval: a person's decision becomes a real Paperclip approval in one tool call."""
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


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def paperclip(tmp_path, monkeypatch):
    """A fake Paperclip behind _paperclip_call, the profile's secret scope, and its facts file."""
    scope = {"PAPERCLIP_API_URL": API + "/", "PAPERCLIP_API_KEY": "pcp_test_key"}
    fake = types.ModuleType("agent.secret_scope")
    fake.get_secret = scope.get
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    (tmp_path / "lightbulb-facts.json").write_text(json.dumps({"app_url": "https://dev.example/"}))

    state = {"me": {"id": ME, "companyId": COMPANY, "role": "ceo", "reportsTo": None},
             "calls": [], "fail": {}}
    issues = {"WAL-6": {"id": ISSUE, "identifier": "WAL-6"}, ISSUE: {"id": ISSUE, "identifier": "WAL-6"}}

    def call(method, url, key, body=None, run_id=None):
        assert key == "pcp_test_key"
        path = url.removeprefix(API)
        state["calls"].append((method, path, body, run_id))
        if (method, path) in state["fail"]:
            raise state["fail"][(method, path)]
        if path == "/api/agents/me":
            return state["me"]
        if path.startswith("/api/issues/") and method == "GET":
            ref = path.rsplit("/", 1)[1]
            if ref not in issues:
                raise _http_error(404, {"error": "Issue not found"})
            return issues[ref]
        if path == f"/api/companies/{COMPANY}":
            return {"id": COMPANY, "issuePrefix": "WAL"}
        if path == f"/api/companies/{COMPANY}/approvals":
            return {"id": "ap-1", **body}
        return {}

    monkeypatch.setattr(lightbulb, "_paperclip_call", call)
    state["scope"] = scope
    return state


def _created(state):
    return [c for c in state["calls"] if c[0] == "POST"]


def test_the_root_ceos_company_plan_is_an_approve_ceo_strategy_linked_by_identifier(paperclip):
    out = json.loads(lightbulb._request_approval({
        "title": "Approve the launch plan", "summary": "Two weeks: pricing, then ten calls.",
        "tasks": ["WAL-6"], "companyPlan": True}))
    assert out == {"approvalId": "ap-1", "type": "approve_ceo_strategy",
                   "link": "https://dev.example/WAL/approvals/ap-1"}
    [(_, path, body, _)] = _created(paperclip)
    assert path == f"/api/companies/{COMPANY}/approvals"
    assert body["issueIds"] == [ISSUE]  # the identifier, resolved to the UUID the schema needs
    assert body["requestedByAgentId"] == ME
    assert body["payload"]["plan"] == "Two weeks: pricing, then ten calls."
    assert ("PATCH", f"/api/issues/{ISSUE}", {"status": "in_review"}, None) in paperclip["calls"]


@pytest.mark.parametrize("me, company_plan", [
    ({"id": ME, "companyId": COMPANY, "role": "ceo", "reportsTo": None}, False),  # CEO, not the plan
    ({"id": ME, "companyId": COMPANY, "role": "ceo", "reportsTo": "boss"}, True),  # not the ROOT CEO
    ({"id": ME, "companyId": COMPANY, "role": "growth", "reportsTo": "ceo"}, True),  # a specialist
])
def test_everything_else_is_a_request_board_approval(paperclip, me, company_plan):
    paperclip["me"] = me
    out = json.loads(lightbulb._request_approval({
        "title": "Approve pricing", "summary": "Starter $49.", "companyPlan": company_plan,
        "recommendedAction": "Publish the pricing page."}))
    assert out["type"] == "request_board_approval"
    [(_, _, body, _)] = _created(paperclip)
    assert body["payload"] == {"title": "Approve pricing", "summary": "Starter $49.",
                               "recommendedAction": "Publish the pricing page."}
    assert "issueIds" not in body


def test_an_unknown_task_creates_nothing(paperclip):
    out = lightbulb._request_approval({"title": "t", "summary": "s", "tasks": ["WAL-99"]})
    assert out == "Approval not created: task WAL-99 was not found."
    assert _created(paperclip) == []


def test_a_paperclip_error_is_reported_not_raised(paperclip):
    paperclip["fail"][("POST", f"/api/companies/{COMPANY}/approvals")] = _http_error(
        403, {"error": "Status-only recovery runs cannot create or modify approvals"})
    out = lightbulb._request_approval({"title": "t", "summary": "s", "tasks": ["WAL-6"]})
    assert out == ("Approval not created: HTTP 403: Status-only recovery runs cannot create or "
                   "modify approvals.")
    assert not [c for c in paperclip["calls"] if c[0] == "PATCH"]  # no in_review without an approval


def test_a_task_that_cannot_move_still_returns_the_approval(paperclip):
    paperclip["fail"][("PATCH", f"/api/issues/{ISSUE}")] = _http_error(409, {"error": "checked out"})
    paperclip["scope"]["PAPERCLIP_RUN_ID"] = "run-1"
    out = json.loads(lightbulb._request_approval({"title": "t", "summary": "s", "tasks": [ISSUE]}))
    assert out["approvalId"] == "ap-1"
    assert out["notMovedToInReview"] == ["WAL-6 (HTTP 409: checked out)"]
    assert _created(paperclip)[0][3] == "run-1"  # a heartbeat run's id rides along


def test_no_credentials_and_missing_fields_are_refused(paperclip):
    assert "title and summary" in lightbulb._request_approval({"title": "t"})
    paperclip["scope"].pop("PAPERCLIP_API_KEY")
    assert lightbulb._request_approval({"title": "t", "summary": "s"}) == lightbulb.APPROVAL_NO_KEY
