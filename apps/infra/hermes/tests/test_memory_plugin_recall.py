"""Room agents read memory through the backend's recall and write through ``remember``; the
AgentCore events keep carrying the speaker's name."""
import io
import json
import logging

import pytest

import bridge
from bridge import turn
from tests.test_memory_agentcore import FakeClient, mod, provider  # noqa: F401  (mod is a fixture)

PAGE = {"asOf": "2026-10-27T00:00:00Z", "usedEvidenceTokens": 40, "results": [
    {"rank": 1, "kind": "fact", "content": "Launch is Nov 3 (contested)", "occurredAt": "2026-10-20", "status": "contested",
     "history": [], "sources": []},
    {"rank": 2, "kind": "decision", "content": "Weekly reports", "occurredAt": "2026-10-13", "status": "current",
     "history": [], "sources": []}]}


class FakeBackend:
    """urlopen stand-in for the backend's /api/v1/internal/memory/* endpoints."""

    def __init__(self, reply=None, fail=False):
        self.reply, self.fail, self.calls, self.board = PAGE if reply is None else reply, fail, [], []

    def __call__(self, req, timeout=None):
        if req.data is None:  # the task board's GET
            return io.BytesIO(json.dumps(self.board if "in_progress" in req.full_url else []).encode())
        self.calls.append((req.full_url, req.get_header("Authorization"), json.loads(req.data), timeout))
        if self.fail:
            raise TimeoutError("timed out")
        return io.BytesIO(json.dumps(self.reply).encode())


@pytest.fixture
def backend(mod, monkeypatch):  # noqa: F811
    for k, v in {"PAPERCLIP_API_URL": "http://pc.local/", "PAPERCLIP_API_KEY": "pk-agent",
                 "PAPERCLIP_COMPANY_ID": "co-1"}.items():
        monkeypatch.setenv(k, v)  # the task board's identity; memory uses the bridge's
    monkeypatch.setattr(bridge, "API", "http://api.local")
    monkeypatch.setattr(bridge, "HOOK_TOKEN", "hook-tok")
    mod._BOARD.clear()
    fake = FakeBackend()
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    return fake


def said(text):
    return turn._turn_input({"sender": {"id": "u1", "displayName": "Aden"},
                             "message": {"messageId": "m1", "text": text},
                             "room": {"id": "r1", "kind": "channel"}, "participants": []})


def recalls(backend):
    return [c for c in backend.calls if c[0].endswith("/api/v1/internal/memory/recall/org_test")]


def test_user_text_drops_the_turn_context_line(mod):
    assert mod._user_text(said("hello")) == "Aden: hello"
    assert mod._user_text("no context here") == "no context here"


def test_prefetch_query_is_user_text_only(mod, backend):
    client = FakeClient()
    p = provider(mod, client)
    p.prefetch(said("what is our launch date?"))
    (url, auth, body, _), = recalls(backend)
    assert url == "http://api.local/api/v1/internal/memory/recall/org_test" and auth == "Bearer hook-tok"
    assert body == {"question": "Aden: what is our launch date?", "budget_tokens": 400}
    assert p.prefetch(said("thanks!")) == "" and len(recalls(backend)) == 1  # trivial prompt: no call
    assert client.calls == []  # AgentCore is no longer read


def test_prefetch_injects_page_lines_under_budget(mod, backend):
    p = provider(mod, FakeClient())
    assert p.prefetch(said("launch?")).splitlines() == [
        "Remembered:", "- Launch is Nov 3 (contested)", "- Weekly reports"]
    backend.board = [{"identifier": "ISOA-9", "title": "Launch page"}]
    mod._BOARD.clear()
    recalled, board = p.prefetch(said("launch?")).split("\n\n")
    assert recalled.endswith("- Weekly reports") and "ISOA-9 Launch page" in board


def test_prefetch_backend_failure_returns_board_only(mod, backend, caplog):
    backend.fail = True
    with caplog.at_level(logging.WARNING):
        out = provider(mod, FakeClient()).prefetch(said("secret launch plan?"))
    assert out == "" and "secret launch plan" not in caplog.text
    backend.board = [{"identifier": "ISOA-9", "title": "Launch page"}]
    mod._BOARD.clear()
    assert "ISOA-9" in provider(mod, FakeClient()).prefetch(said("secret launch plan?"))  # the board survives
    assert any("recall failed" in r.message for r in caplog.records)


@pytest.mark.parametrize("reply", [[], {"results": None}, {"results": [None]}, {"results": [{"content": 5}]},
                                   {"results": [{"rank": 1}]}])
def test_prefetch_malformed_page_returns_board_only(mod, backend, reply):
    backend.reply = reply
    backend.board = [{"identifier": "ISOA-9", "title": "Launch page"}]
    out = provider(mod, FakeClient()).prefetch(said("launch?"))
    assert out.startswith("Task board, partial") and "Remembered" not in out


@pytest.mark.parametrize("reply", [[], {"results": [None]}, {"results": "x"}])
def test_search_tool_malformed_page_returns_error_string(mod, backend, reply):
    backend.reply = reply
    out = json.loads(provider(mod, FakeClient()).handle_tool_call("workspace_memory_search", {"query": "x"}))
    assert list(out) == ["error"]


def test_prefetch_caps_injected_block(mod, backend):
    backend.reply = {"results": [{"content": f"fact {i} " + "x" * 90} for i in range(40)]}
    out = provider(mod, FakeClient()).prefetch(said("launch?"))
    facts = out.split("\n", 1)[1]
    assert len(facts) <= 1600 and all(line.startswith("- fact ") and line.endswith("x") for line in facts.splitlines())
    backend.reply = {"results": [{"content": "y" * 1000} for _ in range(40)]}
    big = json.loads(provider(mod, FakeClient()).handle_tool_call("workspace_memory_search", {"query": "x"}))
    assert len(big["page"]) <= mod._SEARCH_MAX and big["page"].endswith("y")  # whole lines only


def test_remember_sends_a_secret_unchanged_and_returns_the_refusal(mod, backend, monkeypatch):
    import urllib.error

    sent = []

    def refuse(req, timeout=None):
        sent.append(json.loads(req.data))
        body = io.BytesIO(json.dumps({"detail": "Not remembered: secret:aws_key"}).encode())
        raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, body)

    monkeypatch.setattr(mod.urllib.request, "urlopen", refuse)
    args = {"subject": "aws key", "fact": "AKIAIOSFODNN7EXAMPLE sk-def456", "kind": "fact"}
    out = json.loads(provider(mod, FakeClient()).handle_tool_call("remember", args))
    assert sent[0]["subject"] == "aws key" and sent[0]["fact"] == "AKIAIOSFODNN7EXAMPLE sk-def456"
    assert out == {"error": "Not remembered: secret:aws_key"}


def test_sync_turn_prefixes_speaker_on_events(mod, tmp_path):
    (tmp_path / "SOUL.md").write_text("# Friday\n")
    client = FakeClient()
    mod._CLIENT = client
    p = mod.AgentCoreMemoryProvider()
    p.initialize("room:ceo", hermes_home=str(tmp_path), user_id="user_2abc", chat_type="dm")
    p.sync_turn(said("I prefer weekly reports"), "Got it.")
    p.sync_turn("plain words with no speaker", "")
    (_, a), (_, b) = client.calls
    assert [m["conversational"]["content"]["text"] for m in a["payload"]] == [
        "Aden: I prefer weekly reports", "Friday: Got it."]
    assert b["payload"][0]["conversational"]["content"]["text"] == "user_2abc: plain words with no speaker"
    assert {c[0] for c in client.calls} == {"create"}  # events only, never a record write


def test_search_tool_reads_through_the_backend(mod, backend):
    out = json.loads(provider(mod, FakeClient()).handle_tool_call("workspace_memory_search", {"query": "launch"}))
    assert out["page"] == "Contested:\nLaunch is Nov 3 (contested) (2026-10-20)\nLog:\nWeekly reports (2026-10-13)"
    assert recalls(backend)[0][2]["question"] == "launch"


def test_remember_tool_forwards(mod, backend):
    backend.reply = {"queued": True, "neighbours": [{"id": "m1", "content": "Launch is Nov 3"}]}
    p = provider(mod, FakeClient())
    desc, = [t["description"] for t in p.get_tool_schemas() if t["name"] == "remember"]
    assert desc.endswith("Team in a room; personal in a direct message.")
    args = {"subject": "launch", "fact": "Launch moved to Nov 10", "kind": "decision", "replaces": ["m1"]}
    out = json.loads(p.handle_tool_call("remember", args))
    assert out == backend.reply
    (url, _, body, _), = backend.calls
    assert url == "http://api.local/api/v1/internal/memory/remember/org_test"
    assert body == {**args, "scope": "team"}  # a room is shared already
    dm = provider(mod, FakeClient(), user_id="user_2abc", chat_type="dm")
    dm.handle_tool_call("remember", {"subject": "s", "fact": "f", "kind": "fact"})
    assert backend.calls[-1][2]["scope"] == "me"
    assert backend.calls[-1][2]["on_behalf_of"] == "user_2abc"  # the person in the DM
    assert "on_behalf_of" not in backend.calls[0][2]  # a room has no one to attach


def test_remember_on_a_task_run_names_the_run(mod, backend, monkeypatch):
    """Gate B2: the backend refuses a phone caller's task, so a Worker run's remember says which run it is."""
    run = {"LIGHTBULB_TASK_ID": "t1", "LIGHTBULB_TASK_ATTEMPT": "2", "LIGHTBULB_RUN_ID": "r1", "LIGHTBULB_RUN_SECRET": "s"}
    monkeypatch.setattr(mod, "_secret", lambda name: run.get(name, ""))
    provider(mod, FakeClient()).handle_tool_call("remember", {"subject": "s", "fact": "f", "kind": "fact"})
    body = backend.calls[-1][2]
    assert (body["task_id"], body["attempt"], body["run_id"], body["run_secret"]) == ("t1", 2, "r1", "s")
    run.clear()  # a room turn: no run to name
    provider(mod, FakeClient()).handle_tool_call("remember", {"subject": "s", "fact": "f", "kind": "fact"})
    assert "task_id" not in backend.calls[-1][2]


def test_recall_query_is_cut_to_the_backends_limit(mod, backend):
    p = provider(mod, FakeClient())
    p.prefetch(said("launch " * 1500))  # ~10,000 characters
    assert 0 < len(recalls(backend)[0][2]["question"]) <= 2000


def test_tools_return_a_short_error_when_the_backend_fails(mod, backend):
    backend.fail = True
    p = provider(mod, FakeClient())
    assert "error" in json.loads(p.handle_tool_call("remember", {"subject": "s", "fact": "f", "kind": "fact"}))
    assert "error" in json.loads(p.handle_tool_call("workspace_memory_search", {"query": "x"}))


def test_dm_recall_names_the_person_and_a_room_does_not(mod, backend):
    provider(mod, FakeClient(), user_id="user_2abc", chat_type="dm").prefetch(said("launch?"))
    provider(mod, FakeClient(), user_id="user_2abc", chat_type="group").prefetch(said("launch?"))
    provider(mod, FakeClient()).handle_tool_call("workspace_memory_search", {"query": "launch"})
    dm, group, room = (c[2] for c in recalls(backend))
    assert dm["member"] == "user_2abc"
    assert "member" not in group and "member" not in room


def test_remember_refusal_is_surfaced_to_the_agent(mod, backend, monkeypatch):
    import urllib.error

    def refuse(req, timeout=None):
        body = io.BytesIO(json.dumps({"detail": "Not remembered: secret:aws_key"}).encode())
        raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, body)

    monkeypatch.setattr(mod.urllib.request, "urlopen", refuse)
    out = json.loads(provider(mod, FakeClient()).handle_tool_call("remember", {"subject": "s", "fact": "f", "kind": "fact"}))
    assert out == {"error": "Not remembered: secret:aws_key"}


def test_search_tool_returns_one_text_page_with_its_turns(mod, backend):
    claims = [{"kind": "claim", "content": f"Ann: topic {i}: value {i}", "occurredAt": "2026-10-20T00:00:00+00:00",
               "status": "current"} for i in range(50)]
    turns = [{"kind": "turn", "content": f"Bo: turn {i}\n" + "w " * 390, "occurredAt": "2026-10-21T00:00:00+00:00",
              "status": "current"} for i in range(30)]
    backend.reply = {"results": claims + turns}
    out = json.loads(provider(mod, FakeClient()).handle_tool_call("workspace_memory_search", {"query": "launch"}))
    assert set(out) == {"page", "counts"} and out["counts"] == {"facts": 50, "turns": 30}
    assert out["page"].startswith("Current:\nAnn: topic 0: value 0 (2026-10-20)")
    assert "Turns:\n2026-10-21, Bo: turn 0 w w" in out["page"]
    assert recalls(backend)[0][2]["budget_tokens"] == 8000
    backend.reply = {"results": claims + turns * 3}  # over the cap: cut at a whole line from the end
    big = json.loads(provider(mod, FakeClient()).handle_tool_call("workspace_memory_search", {"query": "x"}))
    assert len(big["page"]) <= mod._SEARCH_MAX and big["page"].endswith("w") and "Turns:" in big["page"]


def test_search_page_dates_a_claim_by_its_valid_from(mod, backend):
    backend.reply = {"results": [
        {"kind": "claim", "content": "Ann: city: Oslo", "occurredAt": "2026-10-29T01:40:27+00:00",
         "validFrom": "2024-03-15T00:00:00+00:00", "status": "current"},
        {"kind": "claim", "content": "Bo: team: infra", "occurredAt": "2026-10-29T01:40:27+00:00",
         "validFrom": None, "status": "current"}]}
    out = json.loads(provider(mod, FakeClient()).handle_tool_call("workspace_memory_search", {"query": "city"}))
    assert out["page"] == "Current:\nAnn: city: Oslo (2024-03-15)\nBo: team: infra (2026-10-29)"
