"""lightbulb_board: task counts and lists come from the board itself, never from memory."""
import json
import pathlib
import sys
import types
import urllib.error
import urllib.parse

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402

API = "http://paperclip.internal"
COMPANY = "c0000000-0000-4000-8000-000000000000"
ME = "a0000000-0000-4000-8000-000000000000"
VERA = "a0000000-0000-4000-8000-00000000000v"


@pytest.fixture
def board(monkeypatch):
    """A fake Paperclip board of `rows`, served in pages of `page` like the fork's list route."""
    scope = {"PAPERCLIP_API_URL": API, "PAPERCLIP_API_KEY": "pcp_test_key", "PAPERCLIP_COMPANY_ID": COMPANY}
    fake = types.ModuleType("agent.secret_scope")
    fake.get_secret = scope.get
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    state = {"rows": [], "page": 10, "urls": [], "fail": None}

    def call(method, url, key, body=None, run_id=None):
        assert (method, key) == ("GET", "pcp_test_key")
        if state["fail"]:
            raise state["fail"]
        path, _, query = url.removeprefix(API).partition("?")
        state["urls"].append(url)
        if path == "/api/agents/me":
            return {"id": ME, "companyId": COMPANY}
        if path == f"/api/companies/{COMPANY}/agents":
            return [{"id": ME, "name": "Friday"}, {"id": VERA, "name": "Vera"}]
        assert path == f"/api/companies/{COMPANY}/issues"
        params = dict(urllib.parse.parse_qsl(query))
        limit, offset = min(int(params["limit"]), state["page"]), int(params.get("offset", 0))
        return state["rows"][offset:offset + limit]

    monkeypatch.setattr(lightbulb, "_paperclip_call", call)
    return state


def _rows(n, status="todo", assignee=ME):
    return [{"id": f"i{i}", "identifier": f"WAL-{i}", "title": f"Task {i}", "status": status,
             "assigneeAgentId": assignee} for i in range(n)]


def test_default_counts_every_open_status_across_all_pages(board):
    board["rows"] = _rows(55)
    out = json.loads(lightbulb._board({}))
    assert out["total"] == 55
    assert len(out["issues"]) == 50
    assert out["truncated"] is False
    statuses = urllib.parse.parse_qs(urllib.parse.urlparse(board["urls"][-1]).query)["status"][0].split(",")
    assert "done" not in statuses and "cancelled" not in statuses and "blocked" in statuses


def test_rows_name_the_assignee(board):
    board["rows"] = _rows(1, assignee=VERA)
    [row] = json.loads(lightbulb._board({}))["issues"]
    assert row == {"identifier": "WAL-0", "title": "Task 0", "status": "todo", "assignee": "Vera"}


def test_assignee_me_filters_by_my_agent_id(board):
    lightbulb._board({"assignee": "me"})
    assert any(f"assigneeAgentId={ME}" in u for u in board["urls"])


def test_assignee_by_name_filters_by_that_agents_id(board):
    lightbulb._board({"assignee": "vera"})
    assert any(f"assigneeAgentId={VERA}" in u for u in board["urls"])


def test_unknown_assignee_name_is_refused_without_listing(board):
    out = lightbulb._board({"assignee": "Nobody"})
    assert out == "No agent named Nobody on this board."
    assert not any("/issues" in u for u in board["urls"])


def test_status_outside_the_enum_is_refused(board):
    out = lightbulb._board({"status": ["open"]})
    assert out.startswith("Unknown status: open.")
    assert board["urls"] == []


def test_more_than_the_cap_is_truncated_not_miscounted(board, monkeypatch):
    monkeypatch.setattr(lightbulb, "BOARD_MAX_ROWS", 25)
    board["rows"] = _rows(40)
    out = json.loads(lightbulb._board({}))
    assert out["total"] == 25 and out["truncated"] is True


def test_board_unavailable_returns_a_sentence_not_a_traceback(board):
    board["fail"] = urllib.error.URLError("down")
    assert lightbulb._board({}) == "The board is unavailable right now. Do not state a number."


def test_board_is_registered_and_listed_in_the_manifest(monkeypatch):
    registered = {}
    monkeypatch.setattr(lightbulb, "_register_redaction_default", lambda: 0)

    class Registry:
        def register_hook(self, *_a):
            pass

        def register_tool(self, name, toolset, schema, handler, **_kw):
            registered[name] = (schema, handler)

    lightbulb.register(Registry())
    manifest = (pathlib.Path(lightbulb.__file__).parent / "plugin.yaml").read_text()
    assert registered["lightbulb_board"] == (lightbulb.BOARD_SCHEMA, lightbulb._board)
    assert "  - lightbulb_board\n" in manifest
