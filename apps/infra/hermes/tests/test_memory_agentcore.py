"""PR 3 (AgentCore workspace memory): the bundled provider, its enablement, and health."""
import importlib
import json
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from bridge import server, sync

PLUGINS = pathlib.Path(__file__).resolve().parents[1] / "plugins"
OWNER = "org_test"  # conftest's ISOL8_OWNER_ID
MEMORY_ID = "lightbulb_dev-AbCdEf1234"


class FakeClient:
    def __init__(self, fail=False, records=(), by_ns=None):
        # by_ns: {namespace: [texts] or an exception}, overriding records/fail per namespace.
        self.fail, self.records, self.by_ns, self.calls = fail, list(records), by_ns or {}, []

    def retrieve_memory_records(self, **kw):
        self.calls.append(("retrieve", kw))
        got = self.by_ns.get(kw["namespace"], self.records)
        if self.fail or isinstance(got, Exception):
            raise got if isinstance(got, Exception) else TimeoutError("read timeout")
        return {"memoryRecordSummaries": [{"content": {"text": t}} for t in got]}

    def create_event(self, **kw):
        self.calls.append(("create", kw))
        if self.fail:
            raise RuntimeError("boom")


@pytest.fixture
def mod(monkeypatch):
    """Import the provider against a stand-in for Hermes's agent.memory_provider (the tests run
    outside the image, where Hermes is not installed)."""
    fake = types.ModuleType("agent.memory_provider")
    fake.MemoryProvider = type("MemoryProvider", (), {})
    # Stand-in for upstream's is_trivial_prompt (agent/memory_provider.py): bare acknowledgements.
    fake.is_trivial_prompt = lambda text: (text or "").strip().lower().rstrip("!.") in ("", "thanks", "ok")
    redact = types.ModuleType("agent.redact")
    # Stand-in for upstream's redact_for_egress: masks anything shaped like an sk- key.
    redact.redact_for_egress = lambda text: re.sub(r"sk-[A-Za-z0-9_-]+", "sk-***", text)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.memory_provider", fake)
    monkeypatch.setitem(sys.modules, "agent.redact", redact)
    monkeypatch.syspath_prepend(str(PLUGINS / "memory"))
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", MEMORY_ID)
    sys.modules.pop("agentcore", None)
    m = importlib.import_module("agentcore")
    m._CLIENT = None
    return m


def provider(mod, client, session="room:ceo", **kw):
    mod._CLIENT = client
    p = mod.AgentCoreMemoryProvider()
    p.initialize(session, hermes_home="/tmp", platform="lightbulb", **kw)
    return p


MEMBER = "user_2abcXYZ"
DM = {"user_id": MEMBER, "chat_type": "dm"}
TEAM_NS, PRIVATE_NS = f"/owners/{OWNER}/", f"/owners/{OWNER}:{MEMBER}/"


def test_available_only_with_memory_id_and_owner(mod, monkeypatch):
    assert mod.AgentCoreMemoryProvider().is_available()
    monkeypatch.delenv("AGENTCORE_MEMORY_ID")
    assert not mod.AgentCoreMemoryProvider().is_available()
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", MEMORY_ID)
    monkeypatch.delenv("ISOL8_OWNER_ID")
    assert not mod.AgentCoreMemoryProvider().is_available()


def test_client_has_short_timeouts_and_one_retry(mod, monkeypatch):
    seen = {}
    fake_boto3 = types.SimpleNamespace(client=lambda service, **kw: seen.update(service=service, **kw) or FakeClient())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    assert mod._client() is mod._client()  # built once per process, not per turn
    cfg = seen["config"]
    assert seen["service"] == "bedrock-agentcore"
    assert cfg.connect_timeout <= 1 and cfg.read_timeout <= 1.5
    # Two attempts, standard mode: the client is cached per process, and its pooled connection
    # goes stale between turns (dev acceptance 2026-09-23: the first recall after ~16 min idle
    # failed "Connection was closed before we received a valid response"). One retry on a fresh
    # connection keeps the worst case (~5 s) under Hermes's 8 s prefetch ceiling.
    assert cfg.retries == {"total_max_attempts": 2, "mode": "standard"}


def test_sync_turn_writes_one_owner_event_inline(mod):
    """Hermes already runs sync_turn on its own serialized background worker, so the write is
    inline there: ordering is kept and nothing escapes Hermes's bounded shutdown drain."""
    client = FakeClient()
    p = provider(mod, client, session="room:ceo/thread#1")
    p.sync_turn("our codename is PERIWINKLE-42", "noted")
    (op, kw), = client.calls
    assert op == "create" and kw["actorId"] == OWNER and kw["memoryId"] == MEMORY_ID
    assert kw["sessionId"] == "room-ceo-thread-1"
    assert [m["conversational"]["role"] for m in kw["payload"]] == ["USER", "ASSISTANT"]
    assert kw["payload"][0]["conversational"]["content"]["text"] == "our codename is PERIWINKLE-42"


def test_sync_turn_names_both_speakers_and_drops_the_turn_context_line(mod, tmp_path):
    """One actorId serves the whole workspace, so the names in the text are how extraction
    tells Aden from Leo from the agent."""
    from bridge import turn
    assert mod._TURN_CONTEXT_PREFIX == turn.TURN_CONTEXT_PREFIX
    (tmp_path / "SOUL.md").write_text("<!-- generated -->\n\n# Friday\n\nYou are Friday.\n")
    client = FakeClient()
    mod._CLIENT = client
    p = mod.AgentCoreMemoryProvider()
    p.initialize("room:ceo", hermes_home=str(tmp_path), platform="lightbulb")
    user = turn._turn_input({"sender": {"id": "u1", "displayName": "Aden"},
                             "message": {"messageId": "m1", "text": "I prefer weekly reports"},
                             "room": {"id": "r1", "kind": "channel"}, "participants": []})
    p.sync_turn(user, "Got it.")
    (_, kw), = client.calls
    texts = [m["conversational"]["content"]["text"] for m in kw["payload"]]
    assert texts == ["Aden: I prefer weekly reports", "Friday: Got it."]


def test_sync_turn_never_raises_and_skips_empty_sides(mod):
    p = provider(mod, FakeClient(fail=True))
    p.sync_turn("hi there", "")  # must not raise
    client = FakeClient()
    p = provider(mod, client)
    p.sync_turn("", "")
    assert client.calls == []


def test_sync_turn_redacts_secrets_before_they_reach_shared_memory(mod):
    """Memory is shared by every agent: a key pasted in one DM must not resurface in #general."""
    client = FakeClient()
    provider(mod, client).sync_turn("here is the key sk-live-abc123", "stored sk-live-abc123")
    (_, kw), = client.calls
    texts = [m["conversational"]["content"]["text"] for m in kw["payload"]]
    assert all("abc123" not in t for t in texts)


def test_sanitized_session_id_fits_the_api_pattern(mod):
    import re
    for raw in ("", "::", "a" * 300, "room:ceo", "_x"):
        sid = mod._session_id(raw)
        assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-_]*", sid) and len(sid) <= 100


def test_two_tools_and_a_short_prompt_line(mod):
    p = mod.AgentCoreMemoryProvider()
    remember, search = p.get_tool_schemas()
    assert search["name"] == "workspace_memory_search" and search["parameters"]["required"] == ["query"]
    assert remember["name"] == "remember"
    block = p.system_prompt_block()
    assert "shared" in block and "workspace_memory_search" in block and "\n" not in block


# ---- the search tool: recall the agent asks for by topic --------------------------------
@pytest.mark.parametrize("args", [{}, {"query": "  "}, None])
def test_search_tool_needs_a_query(mod, args):
    client = FakeClient()
    assert "error" in json.loads(provider(mod, client).handle_tool_call("workspace_memory_search", args))


def test_search_tool_answers_an_error_not_an_exception_when_memory_is_off(mod, monkeypatch):
    p = provider(mod, FakeClient())
    monkeypatch.delenv("AGENTCORE_MEMORY_ID")
    p._memory_id = ""
    assert "error" in json.loads(p.handle_tool_call("workspace_memory_search", {"query": "demos"}))


# ---- the task board: only the backend's (tasks_board, test_memory_ctx.py); no Paperclip read ----------
def test_a_turn_without_an_audience_reads_no_board(mod, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_KEY", "pk-old")  # an old profile .env still carries it
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no Paperclip board read"))
    p = provider(mod, FakeClient())
    assert p.on_pre_compress([{"role": "user", "content": "hi"}]) == mod.KEEP_NOTE


def test_a_trivial_prompt_reads_no_memory(mod):
    client = FakeClient()
    assert provider(mod, client).prefetch("thanks") == "" and client.calls == []


# ---- scopes: a 1:1 DM with a person is private to them; everything else is team -------------
def test_a_dm_turn_from_a_person_writes_their_private_actor(mod):
    client = FakeClient()
    provider(mod, client, **DM).sync_turn("my salary is 90k", "noted")
    (_, kw), = client.calls
    assert kw["actorId"] == f"{OWNER}:{MEMBER}"


@pytest.mark.parametrize("kw", [
    {"user_id": MEMBER, "chat_type": "group"},          # channel and group DM
    {"user_id": "agent-ceo", "chat_type": "dm"},        # an agent speaking in a DM
    {"user_id": "", "chat_type": "dm"},                 # no sender
    {"chat_type": "dm"},
    {"user_id": "user_x:y", "chat_type": "dm"},         # never a ":" inside the actor's member half
    {},                                                 # mail, Paperclip runs, routines
])
def test_every_other_turn_writes_the_team_actor(mod, kw):
    client = FakeClient()
    p = provider(mod, client, **kw)
    p.sync_turn("our codename is PERIWINKLE-42", "noted")
    assert [c[1].get("actorId") for c in client.calls] == [OWNER]
    assert "private" not in p.system_prompt_block()


def test_dm_prompt_says_recall_is_private_and_not_to_save_it(mod):
    block = provider(mod, FakeClient(), **DM).system_prompt_block()
    assert "private" in block and "memory tool" in block and "\n" not in block


# ---- enablement --------------------------------------------------------------------------
def _write(memory):
    sync.write_profile_config("ceo", {"memory": memory, "model": {"default": "m"}})


@pytest.mark.parametrize("before", [{"provider": ""}, {}, None])
def test_normalize_enables_agentcore_when_configured_and_unset(home, monkeypatch, before):
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", MEMORY_ID)
    _write(before)
    assert sync.normalize_profile_backends("ceo")["memory"]["provider"] == "agentcore"
    assert sync.read_profile_config("ceo")["memory"]["provider"] == "agentcore"


def test_normalize_disables_agentcore_when_env_is_gone(home, monkeypatch):
    monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)
    _write({"provider": "agentcore", "memory_char_limit": 2200})
    assert sync.normalize_profile_backends("ceo")["memory"] == {"provider": "", "memory_char_limit": 2200}


@pytest.mark.parametrize("env", [MEMORY_ID, None])
def test_normalize_never_overrides_another_provider(home, monkeypatch, env):
    if env:
        monkeypatch.setenv("AGENTCORE_MEMORY_ID", env)
    else:
        monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)
    _write({"provider": "honcho"})
    assert sync.normalize_profile_backends("ceo")["memory"]["provider"] == "honcho"


def test_normalize_leaves_builtin_alone_without_env(home, monkeypatch):
    monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)
    _write({"provider": ""})
    assert sync.normalize_profile_backends("ceo")["memory"] == {"provider": ""}


# ---- health ------------------------------------------------------------------------------
AUTH = {"authorization": "Bearer gw-test"}


@pytest.mark.parametrize("env,expected", [(MEMORY_ID, 1), (None, 0)])
def test_health_reports_memory_protocol(home, monkeypatch, env, expected):
    if env:
        monkeypatch.setenv("AGENTCORE_MEMORY_ID", env)
    else:
        monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)
    _, obj = server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", AUTH, {"method": "health"})
    assert obj["payload"]["memoryProtocol"] == expected


def test_health_advertises_the_three_way_browser_state_merge(home):
    # The backend writes a paired Mac's sign-ins back only to a runtime that says this.
    _, obj = server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", AUTH, {"method": "health"})
    assert obj["payload"]["browserStateMerge"] == 1


def test_memory_events_use_the_sessions_scoped_credentials(mod, monkeypatch):
    """With credentials from the invoke, the memory client is built on them, not the execution role."""
    import bridge

    builds = []
    fake = types.ModuleType("boto3")
    fake.client = lambda service, **kw: (builds.append((service, kw.get("aws_access_key_id"))), object())[1]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(bridge, "AWS_SESSION", {})
    mod._client()
    monkeypatch.setattr(bridge, "AWS_SESSION", {"keys": ("ASIA1", "s", "t"), "expires": datetime.now(timezone.utc) + timedelta(hours=1)})
    mod._client()
    mod._client()
    assert builds == [("bedrock-agentcore", None), ("bedrock-agentcore", "ASIA1")]
