from unittest.mock import AsyncMock

import pytest

from core.services import runtime_transport as _runtime_transport_module

# Captured at collection time, before any test's monkeypatch runs, so this
# stays the REAL implementation regardless of what the autouse fixture below
# (or any test) later does to the module attribute of the same name.
_REAL_IS_AGENTCORE_OWNER = _runtime_transport_module.is_agentcore_owner


@pytest.fixture(autouse=True)
def _ecs_owner_by_default(monkeypatch):
    """Every test below exercises the ECS wiring rails; default the AgentCore
    check to False so none of them need their own mock. The AgentCore-specific
    tests at the bottom of this file override it per test."""
    from core.services.credentials import apply_connection as ac

    monkeypatch.setattr(ac.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_apply_openclaw_paths_injects_literal(monkeypatch):
    from core.services.credentials import apply_connection as ac
    from core.services.credentials.registry import OpenclawPath, Provider, Wiring

    # No live provider uses the openclaw_paths rail anymore (MCP providers moved
    # to mcp_server), so exercise the rail itself via a synthetic provider rather
    # than a registry entry.
    synthetic = Provider(
        key="synth",
        display_name="Synthetic",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(openclaw_paths=[OpenclawPath(path="a.b.c", source="input.token")]),
    )
    monkeypatch.setattr(ac.registry, "get_provider", lambda key: synthetic)

    calls = []

    async def _fake_set(owner, path, value):
        calls.append((owner, path, value))

    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path", _fake_set)
    await ac.apply_connection("org_1", "synth", {"token": "lin_secret"})
    assert calls == [("org_1", ["a", "b", "c"], "lin_secret")]


@pytest.mark.asyncio
async def test_apply_materializer_dispatch_via_thread(monkeypatch):
    # google wiring dispatches to the gog_cli materializer, run off the event loop
    # via run_in_thread (so the blocking subprocess/HTTP doesn't stall the loop).
    from core.services.credentials import apply_connection as ac

    seen = {}

    def _fake_mat(owner, provider, cred):
        seen.update(owner=owner, provider=provider, cred=cred)

    threaded = {}

    async def _fake_run_in_thread(fn, *args, **kwargs):
        threaded["called"] = True
        return fn(*args, **kwargs)

    monkeypatch.setitem(ac.MATERIALIZERS, "gog_cli", _fake_mat)
    monkeypatch.setattr(ac, "run_in_thread", _fake_run_in_thread)
    await ac.apply_connection("org_1", "google", {"access_token": "a", "refresh_token": "r"})
    assert threaded.get("called") is True
    assert seen["owner"] == "org_1" and seen["cred"]["refresh_token"] == "r"


@pytest.mark.asyncio
async def test_apply_returns_materializer_value(monkeypatch):
    # The materializer rail surfaces its return value (gog_cli returns the connected
    # account's email) so the Google connect path can record it without re-fetching.
    from core.services.credentials import apply_connection as ac

    def _fake_mat(owner, provider, cred):
        return "user@example.com"

    async def _fake_run_in_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setitem(ac.MATERIALIZERS, "gog_cli", _fake_mat)
    monkeypatch.setattr(ac, "run_in_thread", _fake_run_in_thread)
    result = await ac.apply_connection("org_1", "google", {"access_token": "a", "refresh_token": "r"})
    assert result == "user@example.com"


@pytest.mark.asyncio
async def test_apply_non_materializer_returns_none(monkeypatch):
    # The other rails deliver into config and have nothing to surface — their
    # return stays None so the generic caller can ignore it.
    from core.services.credentials import apply_connection as ac

    async def _set(owner, path, value):
        return None

    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path", _set)
    result = await ac.apply_connection("org_1", "parallel", {"token": "pk_X"})
    assert result is None


@pytest.mark.asyncio
async def test_apply_mcp_server_writes_full_block(monkeypatch):
    # The mcp_server rail installs the full {command, args, env} server block at
    # mcp.servers.<key> in one write — the integration OWNS that key. No registry
    # provider rides this rail today (stripe moved to its hosted remote MCP), so
    # pin the rail with a synthetic stdio provider.
    from core.services.credentials import apply_connection as ac
    from core.services.credentials import registry
    from core.services.credentials.registry import McpServer, Provider, Wiring

    provider = Provider(
        key="acme",
        display_name="Acme",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(
            mcp_server=McpServer(
                key="acme",
                command="npx",
                args=["-y", "@acme/mcp@1.0.0"],
                env={"ACME_API_KEY": "input.token"},
            )
        ),
    )
    monkeypatch.setattr(registry, "get_provider", lambda key: provider)

    calls = []

    async def _set(owner, path, value):
        calls.append((owner, path, value))

    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path", _set)
    await ac.apply_connection("org_1", "acme", {"token": "sk_live_X"})
    assert calls == [
        (
            "org_1",
            ["mcp", "servers", "acme"],
            {
                "command": "npx",
                "args": ["-y", "@acme/mcp@1.0.0"],
                "env": {"ACME_API_KEY": "sk_live_X"},
                "connectionTimeoutMs": 120000,
            },
        )
    ]


@pytest.mark.asyncio
async def test_apply_x_provider_invokes_xurl_materializer(monkeypatch):
    from core.services.credentials import apply_connection as ac

    calls = []

    def fake_materializer(owner_id, provider_key, credential):
        calls.append((owner_id, provider_key, credential))
        return "isol8"

    async def fake_run_in_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setitem(ac.MATERIALIZERS, "xurl_cli", fake_materializer)
    monkeypatch.setattr(ac, "run_in_thread", fake_run_in_thread)

    credential = {
        "access_token": "at",
        "refresh_token": "rt",
        "client_id": "cid",
        "client_secret": "secret",
        "expires_in": 3600,
    }
    identity = await ac.apply_connection("org_1", "x", credential)

    assert identity == "isol8"
    assert calls == [("org_1", "x", credential)]


# ---------------------------------------------------------------------------
# M6-R24: AgentCore projection (rows #4 + #5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_agentcore_mcp_shaped_provider_calls_reconcile_once(monkeypatch):
    """An mcp_server-wiring provider is MCP-shaped -- the AgentCore projection
    calls reconcile_integrations (which fans out to hermes_profiles.refresh_
    integrations) ONCE, and never touches openclaw.json/EFS."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials import mcp_server as mcp_server_mod
    from core.services.credentials.registry import McpServer, Provider, Wiring

    provider = Provider(
        key="acme",
        display_name="Acme",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(mcp_server=McpServer(key="acme", command="npx", env={"ACME_API_KEY": "input.token"})),
    )
    monkeypatch.setattr(ac.registry, "get_provider", lambda key: provider)
    monkeypatch.setattr(ac.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True))

    calls = []

    async def fake_reconcile(owner_id, *, agentcore=None, pending=None):
        calls.append(owner_id)

    monkeypatch.setattr(mcp_server_mod, "reconcile_integrations", fake_reconcile)
    set_calls = []

    async def fake_set(owner, path, value):
        set_calls.append((owner, path, value))

    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path", fake_set)

    result = await ac.apply_connection("org_1", "acme", {"token": "sk_x"})

    assert result is None
    assert calls == ["org_1"]
    assert set_calls == []  # no EFS write


@pytest.mark.asyncio
async def test_apply_agentcore_openclaw_paths_provider_calls_reconcile_once(monkeypatch):
    """An openclaw_paths (env-var-shaped) provider gets the same one-shot
    reconcile treatment on AgentCore -- no per-path EFS write."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials import mcp_server as mcp_server_mod
    from core.services.credentials.registry import OpenclawPath, Provider, Wiring

    synthetic = Provider(
        key="synth",
        display_name="Synthetic",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(openclaw_paths=[OpenclawPath(path="a.b.c", source="input.token")]),
    )
    monkeypatch.setattr(ac.registry, "get_provider", lambda key: synthetic)
    monkeypatch.setattr(ac.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True))

    calls = []

    async def fake_reconcile(owner_id, *, agentcore=None, pending=None):
        calls.append(owner_id)

    monkeypatch.setattr(mcp_server_mod, "reconcile_integrations", fake_reconcile)
    set_calls = []

    async def fake_set(owner, path, value):
        set_calls.append((owner, path, value))

    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path", fake_set)

    result = await ac.apply_connection("org_1", "synth", {"token": "lin_secret"})

    assert result is None
    assert calls == ["org_1"]
    assert set_calls == []


@pytest.mark.asyncio
async def test_apply_agentcore_cli_skill_materializer_is_skipped(monkeypatch, caplog):
    """google (gog_cli) is a pure CLI-skill materializer with no mcp_remote/
    mcp_stdio -- on AgentCore the materializer never runs (exec is the owner's
    Mac, not the microVM) and the call still returns cleanly."""
    from core.services.credentials import apply_connection as ac

    monkeypatch.setattr(ac.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True))
    mat_called = []
    monkeypatch.setitem(ac.MATERIALIZERS, "gog_cli", lambda *a: mat_called.append(a))

    with caplog.at_level("INFO"):
        result = await ac.apply_connection("org_1", "google", {"access_token": "a", "refresh_token": "r"})

    assert result is None
    assert mat_called == []
    assert "google" in caplog.text


@pytest.mark.asyncio
async def test_apply_agentcore_never_raises_when_refresh_fails(monkeypatch):
    """M6-R24: the AgentCore projection must never raise, even if the profile
    refresh itself blows up (e.g. a profile not written yet)."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials import mcp_server as mcp_server_mod
    from core.services.credentials.registry import McpServer, Provider, Wiring

    provider = Provider(
        key="acme",
        display_name="Acme",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(mcp_server=McpServer(key="acme", command="npx", env={})),
    )
    monkeypatch.setattr(ac.registry, "get_provider", lambda key: provider)
    monkeypatch.setattr(ac.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True))

    async def fake_reconcile(owner_id, *, agentcore=None, pending=None):
        raise FileNotFoundError("no config.yaml yet")

    monkeypatch.setattr(mcp_server_mod, "reconcile_integrations", fake_reconcile)

    result = await ac.apply_connection("org_1", "acme", {"token": "x"})  # must not raise
    assert result is None


@pytest.mark.asyncio
async def test_apply_backend_only_never_checks_runtime(monkeypatch):
    """backend_only wiring is unaffected by runtime -- it must not even pay for
    the is_agentcore_owner lookup."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials.registry import Provider, Wiring

    provider = Provider(
        key="backend_only_provider",
        display_name="X",
        acquisition="paste_token",
        actor="cli",
        refresh_owner="none",
        wiring=Wiring(backend_only=True),
    )
    monkeypatch.setattr(ac.registry, "get_provider", lambda key: provider)
    checked = []

    async def fake_is_agentcore(owner_id):
        checked.append(owner_id)
        return True

    monkeypatch.setattr(ac.runtime_transport, "is_agentcore_owner", fake_is_agentcore)

    result = await ac.apply_connection("org_1", "backend_only_provider", {})

    assert result is None
    assert checked == []


@pytest.mark.asyncio
async def test_apply_agentcore_first_connect_projects_pending_provider(monkeypatch):
    """M6-R26 fix round 1, Critical 1: apply_connection runs BEFORE the
    router's put_status(..., "connected") write -- the projection must pass
    the connecting provider through as `pending` so the very first connect's
    profile rewrite doesn't rewrite every profile WITHOUT it."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials import mcp_server as mcp_server_mod

    monkeypatch.setattr(ac.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True))
    calls = []

    async def fake_reconcile(owner_id, *, agentcore=None, pending=None):
        calls.append((owner_id, agentcore, pending))

    monkeypatch.setattr(mcp_server_mod, "reconcile_integrations", fake_reconcile)

    await ac.apply_connection("org_1", "stripe", {"access_token": "at"})

    assert calls == [("org_1", True, "stripe")]


@pytest.mark.asyncio
async def test_apply_agentcore_reads_is_agentcore_owner_exactly_once(monkeypatch):
    """M6-R26 fix round 1, Important 4: apply_connection resolves the runtime
    once and must not pay for a second read inside reconcile_integrations."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials import mcp_server as mcp_server_mod
    from core.services.credentials.registry import McpServer, Provider, Wiring

    provider = Provider(
        key="acme",
        display_name="Acme",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(mcp_server=McpServer(key="acme", command="npx", env={})),
    )
    monkeypatch.setattr(ac.registry, "get_provider", lambda key: provider)
    reads = []

    async def fake_is_agentcore(owner_id):
        reads.append(owner_id)
        return True

    monkeypatch.setattr(ac.runtime_transport, "is_agentcore_owner", fake_is_agentcore)
    monkeypatch.setattr(mcp_server_mod.runtime_transport, "is_agentcore_owner", fake_is_agentcore)
    monkeypatch.setattr(mcp_server_mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_server_mod.integrations_repo, "get_manual_mcp_servers", AsyncMock(return_value={}))
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.services.hermes_profiles.refresh_integrations", AsyncMock())

    await ac.apply_connection("org_1", "acme", {"token": "x"})

    assert reads == ["org_1"]  # exactly one read for the whole connect


@pytest.mark.asyncio
async def test_apply_ecs_owner_unaffected_by_a_raising_agentcore_check(monkeypatch):
    """M6-R26 fix round 1, Important 3: a DynamoDB throttle inside the
    real is_agentcore_owner (not mocked here) must not turn an ECS owner's
    connect into apply_failed/502 -- it degrades to the ECS path, exactly one
    read attempted."""
    from core.services.credentials import apply_connection as ac

    # Restore the REAL is_agentcore_owner (the autouse fixture above mocked it
    # to a canned False) so its own try/except is what's under test here.
    monkeypatch.setattr(ac.runtime_transport, "is_agentcore_owner", _REAL_IS_AGENTCORE_OWNER)
    reads = []

    async def _raise(owner_id):
        reads.append(owner_id)
        raise Exception("dynamodb throttled")

    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _raise)
    set_calls = []

    async def fake_set(owner, path, value):
        set_calls.append((owner, path, value))

    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path", fake_set)

    result = await ac.apply_connection("org_1", "parallel", {"token": "par-x"})

    assert result is None
    assert reads == ["org_1"]  # exactly one read attempted
    assert set_calls  # the ECS write proceeded
