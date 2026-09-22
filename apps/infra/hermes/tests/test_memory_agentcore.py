"""PR 3 (AgentCore workspace memory): the bundled provider, its enablement, and health."""
import importlib
import pathlib
import sys
import threading
import types

import pytest

from bridge import server, sync

PLUGINS = pathlib.Path(__file__).resolve().parents[1] / "plugins"
OWNER = "org_test"  # conftest's ISOL8_OWNER_ID
MEMORY_ID = "lightbulb_dev-AbCdEf1234"


class FakeClient:
    def __init__(self, fail=False, records=()):
        self.fail, self.records, self.calls = fail, list(records), []

    def retrieve_memory_records(self, **kw):
        self.calls.append(("retrieve", kw))
        if self.fail:
            raise TimeoutError("read timeout")
        return {"memoryRecordSummaries": [{"content": {"text": t}} for t in self.records]}

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
    fake.spawn_context_thread = lambda target, *, name, daemon=True, args=(): threading.Thread(
        target=target, args=args, name=name, daemon=daemon)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.memory_provider", fake)
    monkeypatch.syspath_prepend(str(PLUGINS / "memory"))
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", MEMORY_ID)
    sys.modules.pop("agentcore", None)
    return importlib.import_module("agentcore")


def provider(mod, client, session="room:ceo"):
    p = mod.AgentCoreMemoryProvider()
    p._client = client
    p.initialize(session, hermes_home="/tmp", platform="lightbulb")
    return p


def test_available_only_with_memory_id_and_owner(mod, monkeypatch):
    assert mod.AgentCoreMemoryProvider().is_available()
    monkeypatch.delenv("AGENTCORE_MEMORY_ID")
    assert not mod.AgentCoreMemoryProvider().is_available()
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", MEMORY_ID)
    monkeypatch.delenv("ISOL8_OWNER_ID")
    assert not mod.AgentCoreMemoryProvider().is_available()


def test_prefetch_uses_the_exact_owner_namespace_and_never_namespace_path(mod):
    client = FakeClient(records=["launch codename is PERIWINKLE-42"])
    out = provider(mod, client).prefetch("x" * 20000)
    (op, kw), = client.calls
    assert op == "retrieve" and kw["memoryId"] == MEMORY_ID
    assert kw["namespace"] == f"/owners/{OWNER}/"
    assert "namespacePath" not in kw and kw["maxResults"] == 5
    assert len(kw["searchCriteria"]["searchQuery"]) == 10000  # botocore max
    assert "- launch codename is PERIWINKLE-42" in out


def test_prefetch_swallows_any_error(mod):
    assert not provider(mod, FakeClient(fail=True)).prefetch("codename?")


def test_prefetch_with_no_records_is_empty(mod):
    assert not provider(mod, FakeClient()).prefetch("codename?")


def test_client_has_short_timeouts_and_no_retries(mod, monkeypatch):
    seen = {}
    fake_boto3 = types.SimpleNamespace(client=lambda service, **kw: seen.update(service=service, **kw) or FakeClient())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    mod.AgentCoreMemoryProvider()._get_client()
    cfg = seen["config"]
    assert seen["service"] == "bedrock-agentcore"
    assert cfg.connect_timeout <= 1 and cfg.read_timeout <= 1.5
    assert cfg.retries == {"total_max_attempts": 1}


def test_sync_turn_writes_one_owner_event_off_thread(mod):
    gate = threading.Event()
    client = FakeClient()
    original = client.create_event
    client.create_event = lambda **kw: (gate.wait(5), original(**kw))
    p = provider(mod, client, session="room:ceo/thread#1")
    p.sync_turn("our codename is PERIWINKLE-42", "noted")
    assert client.calls == []  # returned while the write is still blocked
    gate.set()
    p._last_sync.join(5)
    (op, kw), = client.calls
    assert op == "create" and kw["actorId"] == OWNER and kw["memoryId"] == MEMORY_ID
    assert kw["sessionId"] == "room-ceo-thread-1"
    assert [m["conversational"]["role"] for m in kw["payload"]] == ["USER", "ASSISTANT"]
    assert kw["payload"][0]["conversational"]["content"]["text"] == "our codename is PERIWINKLE-42"


def test_sync_turn_never_raises_and_skips_empty_sides(mod):
    p = provider(mod, FakeClient(fail=True))
    p.sync_turn("hi there", "")
    p._last_sync.join(5)
    client = FakeClient()
    p = provider(mod, client)
    p.sync_turn("", "")
    assert client.calls == []


def test_sync_turn_skips_non_primary_contexts(mod):
    client = FakeClient()
    p = mod.AgentCoreMemoryProvider()
    p._client = client
    p.initialize("s", hermes_home="/tmp", platform="lightbulb", agent_context="subagent")
    p.sync_turn("u", "a")
    assert client.calls == []


def test_sanitized_session_id_fits_the_api_pattern(mod):
    import re
    for raw in ("", "::", "a" * 300, "room:ceo", "_x"):
        sid = mod._session_id(raw)
        assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-_]*", sid) and len(sid) <= 100


def test_no_tools_and_a_short_prompt_line(mod):
    p = mod.AgentCoreMemoryProvider()
    assert p.get_tool_schemas() == []
    assert "shared" in p.system_prompt_block() and "\n" not in p.system_prompt_block()


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
