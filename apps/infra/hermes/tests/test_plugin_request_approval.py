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


@pytest.mark.parametrize("projects, stamped", [
    ({"WAL-6": "p-1", "WAL-7": "p-1"}, ["p-1"]),  # every linked task in one project
    ({"WAL-6": "p-1", "WAL-7": "p-2"}, None),  # they disagree: name none
    ({"WAL-6": "p-1", "WAL-7": None}, None),  # one has no project: name none
])
def test_a_plan_names_its_project_only_when_its_tasks_agree(paperclip, monkeypatch, projects, stamped):
    real = lightbulb._paperclip_call

    def call(method, url, key, body=None, run_id=None):
        ref = url.rsplit("/", 1)[1]
        if method == "GET" and ref in projects:
            return {"id": f"id-{ref}", "projectId": projects[ref]}
        return real(method, url, key, body, run_id)

    monkeypatch.setattr(lightbulb, "_paperclip_call", call)
    paperclip["me"] = {"id": ME, "companyId": COMPANY, "role": "growth", "reportsTo": "ceo"}
    lightbulb._request_approval({"title": "Plan: CRM", "summary": "s", "tasks": ["WAL-6", "WAL-7"]})
    [(_, _, body, _)] = _created(paperclip)
    assert body["payload"].get("projectIds") == stamped


def test_an_explicit_project_is_stamped_with_or_without_tasks(paperclip):
    paperclip["me"] = {"id": ME, "companyId": COMPANY, "role": "growth", "reportsTo": "ceo"}
    lightbulb._request_approval({"title": "Send the 40 emails", "summary": "s", "projectId": " p-9 "})
    [(_, _, body, _)] = _created(paperclip)
    assert body["payload"]["projectIds"] == ["p-9"]


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


ROOM_KEY = "agent:ceo:lightbulb:channel:11111111-1111-4111-8111-111111111111"


def _binding(tmp_path):
    return tmp_path / "lightbulb-issue-bindings" / f"{ISSUE}.json"


def test_a_room_turn_binds_the_linked_issue_before_the_approval_is_created(paperclip, tmp_path, monkeypatch):
    seen = []
    inner = lightbulb._paperclip_call

    def call(method, url, key, body=None, run_id=None):
        if method == "POST":
            seen.append(json.loads(_binding(tmp_path).read_text()))  # already on disk at the POST
        return inner(method, url, key, body, run_id)

    monkeypatch.setattr(lightbulb, "_paperclip_call", call)
    json.loads(lightbulb._request_approval(
        {"title": "t", "summary": "s", "tasks": ["WAL-6"]}, session_id=ROOM_KEY))
    [binding] = seen
    assert binding["sessionKey"] == ROOM_KEY and "origin" not in binding
    assert isinstance(binding["boundAt"], int)
    from bridge import turn  # the bridge's own reader accepts it
    monkeypatch.setattr(turn, "_profile_dir", lambda agent_id: tmp_path)
    assert turn.issue_conversation("ceo", ISSUE) == ROOM_KEY


def test_a_call_binds_to_its_room_and_records_the_origin(paperclip, tmp_path):
    paperclip["scope"]["LIGHTBULB_ROOM_SESSION_KEY"] = ROOM_KEY
    lightbulb._request_approval({"title": "t", "summary": "s", "tasks": ["WAL-6"]}, session_id="call-1")
    assert json.loads(_binding(tmp_path).read_text())["origin"] == "call-1"


def test_a_failed_create_removes_the_binding_it_wrote(paperclip, tmp_path):
    paperclip["fail"][("POST", f"/api/companies/{COMPANY}/approvals")] = _http_error(500, {})
    lightbulb._request_approval({"title": "t", "summary": "s", "tasks": ["WAL-6"]}, session_id=ROOM_KEY)
    assert not _binding(tmp_path).exists()


def test_a_non_room_session_binds_nothing(paperclip, tmp_path):
    lightbulb._request_approval({"title": "t", "summary": "s", "tasks": ["WAL-6"]}, session_id="issue-session")
    assert not _binding(tmp_path).exists()


def test_the_in_review_move_carries_the_run_attached_to_this_conversation(paperclip, tmp_path):
    run_file = tmp_path / turn_context.DIRNAME / f"paperclip-run.{ISSUE}"
    run_file.parent.mkdir(parents=True)
    run_file.write_text("run-attached\n")
    lightbulb._request_approval({"title": "t", "summary": "s", "tasks": ["WAL-6"]}, session_id=ROOM_KEY)
    assert ("PATCH", f"/api/issues/{ISSUE}", {"status": "in_review"}, "run-attached") in paperclip["calls"]
    assert _created(paperclip)[0][3] is None  # the create is not an issue write


def test_a_failed_create_restores_another_rooms_binding_byte_for_byte(paperclip, tmp_path):
    room_a = '{"sessionKey": "agent:ceo:lightbulb:channel:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "boundAt": 1}\n'
    _binding(tmp_path).parent.mkdir(parents=True)
    _binding(tmp_path).write_text(room_a)
    before = _binding(tmp_path).read_bytes()
    paperclip["fail"][("POST", f"/api/companies/{COMPANY}/approvals")] = _http_error(500, {})
    lightbulb._request_approval({"title": "t", "summary": "s", "tasks": ["WAL-6"]}, session_id=ROOM_KEY)
    assert _binding(tmp_path).read_bytes() == before
