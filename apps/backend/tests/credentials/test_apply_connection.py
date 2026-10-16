from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# There is ONE delivery path: the Hermes-profile projection. The openclaw.json
# rails (wiring.openclaw_paths' direct config writes, the mcp.servers.<key>
# write, and the materializer-onto-EFS dispatch) went with the ECS runtime, so
# the tests that pinned them went with deletion PR-5. What remains below is the
# projection itself plus the doors that must NOT reach it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_mcp_shaped_provider_calls_reconcile_once(monkeypatch):
    """An mcp_server-wiring provider is MCP-shaped -- the projection calls
    reconcile_integrations (which fans out to hermes_profiles.refresh_integrations) ONCE."""
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

    calls = []

    async def fake_reconcile(owner_id, *, pending=None):
        calls.append(owner_id)

    monkeypatch.setattr(mcp_server_mod, "reconcile_integrations", fake_reconcile)

    assert await ac.apply_connection("org_1", "acme", {"token": "sk_x"}) is None
    assert calls == ["org_1"]


@pytest.mark.asyncio
async def test_apply_agentcore_openclaw_paths_provider_with_no_hermes_equivalent_raises(monkeypatch):
    """M6-R31: reconcile_integrations has NO fan-out for an arbitrary openclaw_paths provider
    (it only projects mcp_servers/.env) -- an unlisted one (not in
    HERMES_OPENCLAW_PATHS_PROVIDERS) must raise RuntimeUnsupportedError, not silently no-op via
    reconcile as the pre-M6-R31 behaviour did, so the caller never marks the row connected."""
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

    calls = []

    async def fake_reconcile(owner_id, *, pending=None):
        calls.append(owner_id)

    monkeypatch.setattr(mcp_server_mod, "reconcile_integrations", fake_reconcile)

    with pytest.raises(ac.RuntimeUnsupportedError):
        await ac.apply_connection("org_1", "synth", {"token": "lin_secret"})

    assert calls == []  # never reached -- the unsupported check comes first


@pytest.mark.asyncio
async def test_apply_agentcore_parallel_projects_env_and_config_per_roster_agent(monkeypatch):
    """M6-R31: parallel IS in HERMES_OPENCLAW_PATHS_PROVIDERS -- its key lands in every roster
    agent's .env as PARALLEL_API_KEY, and web.backend is selected deterministically so Hermes's
    keyless ring doesn't pick a different vendor at random. Both land via ONE combined write
    (fix round 1, Important 1) so a partial projection can never be left behind silently."""
    from core.services import messaging as messaging_mod
    from core.services import hermes_profiles as hp_mod
    from core.services.credentials import apply_connection as ac

    monkeypatch.setattr(messaging_mod, "load_agent_roster", AsyncMock(return_value=[{"id": "ceo"}, {"id": "eng"}]))

    calls = []
    monkeypatch.setattr(
        hp_mod,
        "patch_profile_env_and_config",
        lambda owner, agent, env, config: calls.append((owner, agent, env, config)),
    )

    result = await ac.apply_connection("org_1", "parallel", {"token": "par-live-key"})

    assert result is None
    assert calls == [
        ("org_1", "ceo", {"PARALLEL_API_KEY": "par-live-key"}, {"web": {"backend": "parallel"}}),
        ("org_1", "eng", {"PARALLEL_API_KEY": "par-live-key"}, {"web": {"backend": "parallel"}}),
    ]


@pytest.mark.asyncio
async def test_apply_agentcore_parallel_projection_failure_raises_and_is_not_recorded_connected(monkeypatch):
    """Fix round 1, Important 1: the openclaw_paths projection has no later re-assert pass to
    self-heal a missed write (unlike the MCP reconcile rail), so a failed write must propagate --
    connect_paste's existing rollback is what keeps a failed connect from reading "connected"."""
    from core.services import messaging as messaging_mod
    from core.services import hermes_profiles as hp_mod
    from core.services.credentials import apply_connection as ac

    monkeypatch.setattr(messaging_mod, "load_agent_roster", AsyncMock(return_value=[{"id": "ceo"}]))

    def _boom(owner, agent, env, config):
        raise FileNotFoundError("no config.yaml for org_1/hermes/ceo/")

    monkeypatch.setattr(hp_mod, "patch_profile_env_and_config", _boom)

    with pytest.raises(FileNotFoundError):
        await ac.apply_connection("org_1", "parallel", {"token": "par-live-key"})


@pytest.mark.asyncio
async def test_clear_hermes_openclaw_paths_projection_reverts_env_and_config(monkeypatch):
    """The disconnect twin: clears the .env value and reverts web.backend on every profile."""
    from core.services import messaging as messaging_mod
    from core.services import hermes_profiles as hp_mod
    from core.services.credentials import apply_connection as ac

    monkeypatch.setattr(messaging_mod, "load_agent_roster", AsyncMock(return_value=[{"id": "ceo"}]))
    calls = []
    monkeypatch.setattr(
        hp_mod,
        "patch_profile_env_and_config",
        lambda owner, agent, env, config: calls.append((owner, agent, env, config)),
    )

    await ac.clear_hermes_openclaw_paths_projection("org_1", "parallel")

    assert calls == [("org_1", "ceo", {"PARALLEL_API_KEY": ""}, {"web": {"backend": ""}})]


@pytest.mark.asyncio
async def test_clear_hermes_openclaw_paths_projection_failure_is_swallowed(monkeypatch):
    """Unlike connect, a disconnect stays best-effort -- the row has already committed its
    disconnect by the time this runs, so there is no rollback for a raise to trigger."""
    from core.services import messaging as messaging_mod
    from core.services import hermes_profiles as hp_mod
    from core.services.credentials import apply_connection as ac

    monkeypatch.setattr(messaging_mod, "load_agent_roster", AsyncMock(return_value=[{"id": "ceo"}]))

    def _boom(owner, agent, env, config):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(hp_mod, "patch_profile_env_and_config", _boom)

    await ac.clear_hermes_openclaw_paths_projection("org_1", "parallel")  # must not raise


@pytest.mark.asyncio
async def test_clear_hermes_openclaw_paths_projection_no_ops_for_an_unlisted_provider(monkeypatch):
    """A provider _apply_connection_agentcore never projected (RuntimeUnsupportedError on
    connect) has nothing to clear on disconnect -- must not touch any profile."""
    from core.services import messaging as messaging_mod
    from core.services.credentials import apply_connection as ac

    roster = AsyncMock()
    monkeypatch.setattr(messaging_mod, "load_agent_roster", roster)

    await ac.clear_hermes_openclaw_paths_projection("org_1", "some_unlisted_provider")

    roster.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_agentcore_cli_skill_materializer_is_projected(monkeypatch):
    """SUPERSEDES the original "...is_skipped" test. google (gog_cli) is a pure CLI-skill
    materializer with no mcp_remote/mcp_stdio, and the M6-R24 ruling skipped it on AgentCore
    because exec was going to run on the owner's Mac. Since #2749 the shell runs in the
    microVM, so it is projected into every Hermes profile instead (credentials.hermes_cli) and
    the materializer's own return value (gog's connected account email) still surfaces."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials import hermes_cli

    projected = []

    async def fake_project(owner_id, provider_key, materializer, credential):
        projected.append((owner_id, provider_key, materializer, credential))
        return "a@b.co"

    monkeypatch.setattr(hermes_cli, "project", fake_project)

    result = await ac.apply_connection("org_1", "google", {"access_token": "a", "refresh_token": "r"})

    assert result == "a@b.co"
    assert projected == [("org_1", "google", "gog_cli", {"access_token": "a", "refresh_token": "r"})]


@pytest.mark.asyncio
async def test_apply_agentcore_materializer_with_no_projection_is_rejected(monkeypatch):
    """An unsupported materializer cannot produce a connected status row."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials.registry import Provider, Wiring

    provider = Provider(
        key="synth",
        display_name="Synthetic",
        acquisition="paste_token",
        actor="cli",
        refresh_owner="none",
        wiring=Wiring(materializer="toml_cli"),
    )
    monkeypatch.setattr(ac.registry, "get_provider", lambda key: provider)
    with pytest.raises(ac.RuntimeUnsupportedError):
        await ac.apply_connection("org_1", "synth", {"token": "t"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    ("stripe", "notion", "linear", "attio", "intercom", "apollo", "cloudflare"),
)
async def test_prod_brokered_remote_mcp_reconciles_without_retained_credential(monkeypatch, provider):
    """The prod broker promotion (brokered_environments: [dev, prod]) moved these
    actor-owned remote MCP providers onto the workspace bearer broker: in prod the
    refresh grant is backend-owned, so apply_connection reconciles (no
    RuntimeUnsupportedError) even with no retained Secrets-Manager credential."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials import mcp_server as mcp_server_mod

    monkeypatch.setattr(ac.registry.settings, "ENVIRONMENT", "prod")
    calls = []

    async def fake_reconcile(owner_id, *, pending=None):
        calls.append((owner_id, pending))

    monkeypatch.setattr(mcp_server_mod, "reconcile_integrations", fake_reconcile)

    assert await ac.apply_connection("org_1", provider, {"access_token": "fresh", "refresh_token": "rotating"}) is None
    assert calls == [("org_1", provider)]


def test_quickbooks_uses_the_hermes_profile_materializer():
    from core.services.credentials import apply_connection as ac

    provider = ac.registry.get_provider("quickbooks")
    assert ac.runtime_supported(provider)
    assert provider.mcp_stdio is not None
    assert provider.mcp_stdio.command == "/usr/local/bin/lightbulb-quickbooks-mcp"


@pytest.mark.parametrize("provider", ("google", "github", "posthog", "meta_ads", "parallel"))
def test_existing_hermes_rails_remain_supported(provider):
    from core.services.credentials import apply_connection as ac

    assert ac.runtime_supported(ac.registry.get_provider(provider))


@pytest.mark.parametrize("provider", ("stripe", "notion", "linear", "attio", "intercom", "apollo", "cloudflare"))
@pytest.mark.parametrize("env", ("dev", "prod"))
def test_brokered_remote_mcp_is_runtime_supported(monkeypatch, provider, env):
    from core.services.credentials import apply_connection as ac

    monkeypatch.setattr(ac.registry.settings, "ENVIRONMENT", env)
    p = ac.registry.get_provider(provider)
    assert ac.registry.refresh_owner_for(p) == "backend"
    assert ac.runtime_supported(p)


@pytest.mark.asyncio
async def test_apply_agentcore_raises_when_refresh_fails(monkeypatch):
    """A failed profile write must not let the caller mark the provider connected."""
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

    async def fake_reconcile(owner_id, *, pending=None):
        raise FileNotFoundError("no config.yaml yet")

    monkeypatch.setattr(mcp_server_mod, "reconcile_integrations", fake_reconcile)

    with pytest.raises(FileNotFoundError):
        await ac.apply_connection("org_1", "acme", {"token": "x"})


@pytest.mark.asyncio
async def test_apply_backend_only_never_touches_the_runtime(monkeypatch):
    """backend_only wiring never reaches the runtime: Secrets Manager is the credential's
    only custodian, so no projection and no roster read may happen."""
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

    def _no_roster(*a, **kw):
        raise AssertionError("backend_only wiring must not read the roster")

    monkeypatch.setattr("core.services.messaging.load_agent_roster", _no_roster)

    assert await ac.apply_connection("org_1", "backend_only_provider", {}) is None


@pytest.mark.asyncio
async def test_apply_agentcore_first_connect_projects_pending_provider(monkeypatch):
    """M6-R26 fix round 1, Critical 1: apply_connection runs BEFORE the
    router's put_status(..., "connected") write -- the projection must pass
    the connecting provider through as `pending` so the very first connect's
    profile rewrite doesn't rewrite every profile WITHOUT it."""
    from core.services.credentials import apply_connection as ac
    from core.services.credentials import mcp_server as mcp_server_mod

    calls = []

    async def fake_reconcile(owner_id, *, pending=None):
        calls.append((owner_id, pending))

    monkeypatch.setattr(mcp_server_mod, "reconcile_integrations", fake_reconcile)

    await ac.apply_connection("org_1", "posthog", {"access_token": "at"})

    assert calls == [("org_1", "posthog")]


@pytest.mark.asyncio
async def test_apply_never_classifies_the_runtime(monkeypatch):
    """There is one runtime, so a connect owns no transport decision: neither apply_connection
    nor the reconcile it calls may read the containers table to pick a delivery path."""
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

    def _no_reads(*a, **kw):
        raise AssertionError("a connect must not read the containers table")

    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _no_reads)
    monkeypatch.setattr(mcp_server_mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_server_mod.integrations_repo, "get_manual_mcp_servers", AsyncMock(return_value={}))
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=[]))
    refresh = AsyncMock()
    monkeypatch.setattr("core.services.hermes_profiles.refresh_integrations", refresh)

    await ac.apply_connection("org_1", "acme", {"token": "x"})

    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_successful_connect_resumes_waiting_ideas(monkeypatch):
    from core.services.credentials import apply_connection as ac

    seen = []
    monkeypatch.setattr(ac, "_apply_connection_agentcore", AsyncMock(return_value="me@x.test"))
    monkeypatch.setattr(ac.ideas, "resume_after_connect", AsyncMock(side_effect=lambda o, p: seen.append((o, p)) or 1))
    assert await ac.apply_connection("owner-1", "google", {"token": "t"}) == "me@x.test"
    assert seen == [("owner-1", "google")]


@pytest.mark.asyncio
async def test_a_failed_resume_never_fails_the_connect(monkeypatch):
    from core.services.credentials import apply_connection as ac

    monkeypatch.setattr(ac, "_apply_connection_agentcore", AsyncMock(return_value=None))
    monkeypatch.setattr(ac.ideas, "resume_after_connect", AsyncMock(side_effect=RuntimeError("down")))
    assert await ac.apply_connection("owner-1", "google", {"token": "t"}) is None
