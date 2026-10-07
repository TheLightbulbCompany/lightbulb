import pytest
from unittest.mock import AsyncMock

from core.services.credentials.mcp_server import build_mcp_server_block


@pytest.fixture(autouse=True)
def _no_manual_servers_by_default(monkeypatch):
    """build_connected_integration_servers now also reads the manual-MCP row
    (a real DynamoDB read when unmocked). Default it to empty for every test
    in this file; the manual-servers tests below override it per test."""
    from core.services.credentials import mcp_server as m

    # These assertions pin the brokered bearer shape in production
    # (brokered_environments: [dev, prod]): the refresh grant is backend-owned
    # and the runtime receives a per-turn bearer via ${MCP_<KEY>_ACCESS_TOKEN}.
    monkeypatch.setattr(m.registry.settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(m.integrations_repo, "get_manual_mcp_servers", AsyncMock(return_value={}))


def test_resolve_source_literal_returns_static_string():
    # `literal:` lets a provider set a static env value (e.g. a feature flag)
    # with no dependence on the credential or raw input.
    from core.services.credentials.apply_connection import _resolve_source

    assert _resolve_source("literal:true", {}, {}) == "true"
    # Everything after the prefix is the value verbatim, including a colon.
    assert _resolve_source("literal:a:b", {}, {}) == "a:b"


def test_build_block_mixes_literal_and_credential_env(monkeypatch):
    # A provider can set a static flag alongside a credential-derived value; both
    # resolve through the same _resolve_source the env source-map already uses.
    import core.services.credentials.mcp_server as m
    from core.services.credentials.registry import McpServer, Provider, Wiring

    provider = Provider(
        key="slack",
        display_name="Slack",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(
            mcp_server=McpServer(
                key="slack",
                command="npx",
                env={"FLAG": "literal:true", "TOKEN": "input.token"},
            )
        ),
    )
    monkeypatch.setattr(m.registry, "get_provider", lambda key: provider)

    block = build_mcp_server_block("slack", {"token": "xoxb-123"})
    assert block["env"] == {"FLAG": "true", "TOKEN": "xoxb-123"}


def test_build_block_none_for_non_mcp_provider():
    assert build_mcp_server_block("google", {"access_token": "a"}) is None  # materializer, not mcp_server


@pytest.mark.asyncio
async def test_build_connected_map_skips_actor_and_unretained(monkeypatch):
    # No registry provider rides the stdio mcp_server rail today (stripe moved to
    # its hosted remote MCP), so pin the rail with a synthetic provider: a
    # retained-secret mcp_server row is emitted; the actor-refresh one (google,
    # real registry) is excluded.
    from core.services.credentials import mcp_server as m
    from core.services.credentials.registry import McpServer, Provider, Wiring

    acme = Provider(
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
    real_get = m.registry.get_provider
    monkeypatch.setattr(m.registry, "get_provider", lambda key: acme if key == "acme" else real_get(key))

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {"provider": "acme", "status": "connected"},
                {"provider": "google", "status": "connected"},
            ]
        ),
    )
    monkeypatch.setattr(
        m.integration_secret_store,
        "get",
        AsyncMock(side_effect=lambda o, p: {"token": "sk_X"} if p == "acme" else None),
    )
    servers = await m.build_connected_integration_servers("org_1")
    assert servers == {
        "acme": {
            "command": "npx",
            "args": ["-y", "@acme/mcp@1.0.0"],
            "env": {"ACME_API_KEY": "sk_X"},
            "connectionTimeoutMs": 120000,
        }
    }


@pytest.mark.asyncio
async def test_build_connected_map_emits_remote_block_without_retained_secret(monkeypatch):
    # A brokered mcp_remote provider (attio, stripe) has its refresh grant owned
    # by the backend, so it has NO retained actor credential in Secrets Manager.
    # Its live brokered {url, headers} block must still be emitted from the
    # registry + connected status alone — otherwise a reprovision drops the server
    # entry. The actor-refresh CLI one (google) stays excluded. Uses the real
    # registry entries, so it pins the YAML mcp_remote wiring.
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {"provider": "attio", "status": "connected"},
                {"provider": "stripe", "status": "connected"},
                {"provider": "apollo", "status": "connected"},
                {"provider": "cloudflare", "status": "connected"},
                {"provider": "google", "status": "connected"},
            ]
        ),
    )
    # None of the three has a retained secret; get() must NOT gate a remote block
    # (it is never consulted for one).
    secret_get = AsyncMock(return_value=None)
    monkeypatch.setattr(m.integration_secret_store, "get", secret_get)

    servers = await m.build_connected_integration_servers("org_1")
    # The brokered block carries a per-turn bearer reference so the runtime never
    # holds the refresh grant — identical to what the live materializer write publishes.
    assert servers == {
        "attio": {
            "url": "https://mcp.attio.com/mcp",
            "headers": {"Authorization": "Bearer ${MCP_ATTIO_ACCESS_TOKEN}"},
            "requestTimeoutMs": 60000,
        },
        "stripe": {
            "url": "https://mcp.stripe.com",
            "headers": {"Authorization": "Bearer ${MCP_STRIPE_ACCESS_TOKEN}"},
            "requestTimeoutMs": 60000,
        },
        "apollo": {
            "url": "https://mcp.apollo.io/mcp",
            "headers": {"Authorization": "Bearer ${MCP_APOLLO_ACCESS_TOKEN}"},
            "requestTimeoutMs": 60000,
        },
        "cloudflare": {
            "url": "https://mcp.cloudflare.com/mcp",
            "headers": {"Authorization": "Bearer ${MCP_CLOUDFLARE_ACCESS_TOKEN}"},
            "requestTimeoutMs": 60000,
        },
    }


@pytest.mark.asyncio
async def test_build_connected_map_skips_disconnected_remote_provider(monkeypatch):
    # A disconnected mcp_remote row must not be re-published (no stale server block).
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(return_value=[{"provider": "attio", "status": "disconnected"}]),
    )
    monkeypatch.setattr(m.integration_secret_store, "get", AsyncMock(return_value=None))
    assert await m.build_connected_integration_servers("org_1") == {}


@pytest.mark.asyncio
async def test_build_connected_map_skips_orphan_unknown_provider(monkeypatch):
    # A real owner can have a status row for a provider that was since removed
    # from the registry. The registry lookup KeyErrors for it; the build must
    # skip the orphan row, not crash and drop every server.
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {"provider": "since-removed-provider", "status": "connected"},
                {"provider": "stripe", "status": "connected"},
            ]
        ),
    )
    monkeypatch.setattr(m.integration_secret_store, "get", AsyncMock(return_value=None))
    servers = await m.build_connected_integration_servers("org_1")
    assert servers == {
        "stripe": {
            "url": "https://mcp.stripe.com",
            "headers": {"Authorization": "Bearer ${MCP_STRIPE_ACCESS_TOKEN}"},
            "requestTimeoutMs": 60000,
        }
    }


@pytest.mark.asyncio
async def test_reconcile_refreshes_every_roster_profile(monkeypatch):
    """M4-R11: the rewrite goes through hermes_profiles.refresh_integrations for every one of
    the owner's Paperclip-roster agents. There is no second branch: the openclaw.json rail and
    its per-server/per-path writes went with the ECS runtime."""
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(return_value=[{"id": "a1", "role": "ceo"}, {"id": "a2", "role": "engineering"}]),
    )
    called = {}

    async def fake_refresh(owner_id, agent_ids, *, pending=None):
        called.update(owner_id=owner_id, agent_ids=agent_ids)

    monkeypatch.setattr("core.services.hermes_profiles.refresh_integrations", fake_refresh)
    build = AsyncMock(return_value={})
    monkeypatch.setattr(m, "build_connected_integration_servers", build)

    await m.reconcile_integrations("org_a")

    assert called == {"owner_id": "org_a", "agent_ids": ["a1", "a2"]}
    # refresh_integrations rebuilds the connected set itself; reconcile must not also build it.
    build.assert_not_awaited()


# ---------------------------------------------------------------------------
# Row #6: manual MCP servers (an AgentCore owner's raw-editor entries)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_connected_map_includes_manual_servers(monkeypatch):
    """Manual servers (stored on integrations_repo.MANUAL_MCP_KEY for an
    AgentCore owner -- see routers/integrations.py) are folded into the same
    merged map connected integrations project into."""
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "get_manual_mcp_servers",
        AsyncMock(return_value={"my-custom": {"command": "node", "args": ["x.js"], "env": {}}}),
    )
    monkeypatch.setattr(m.integrations_repo, "list_for_owner", AsyncMock(return_value=[]))

    servers = await m.build_connected_integration_servers("org_1")

    assert servers == {"my-custom": {"command": "node", "args": ["x.js"], "env": {}}}


@pytest.mark.asyncio
async def test_build_connected_map_connected_integration_wins_manual_collision(monkeypatch):
    """A manual server sharing a key with a connected integration must not
    shadow it -- integration ownership always wins, mirroring the ECS
    reconcile's own re-assert-after-write rule."""
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "get_manual_mcp_servers",
        AsyncMock(return_value={"stripe": {"command": "should-be-overwritten"}, "my-custom": {"command": "keep"}}),
    )
    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(return_value=[{"provider": "stripe", "status": "connected"}]),
    )
    monkeypatch.setattr(m.integration_secret_store, "get", AsyncMock(return_value=None))

    servers = await m.build_connected_integration_servers("org_1")

    assert servers["stripe"] == {
        "url": "https://mcp.stripe.com",
        "headers": {"Authorization": "Bearer ${MCP_STRIPE_ACCESS_TOKEN}"},
        "requestTimeoutMs": 60000,
    }
    assert servers["my-custom"] == {"command": "keep"}


@pytest.mark.asyncio
async def test_manual_server_secret_scrubbed_to_env_ref_not_config(monkeypatch):
    """M6-R24/M4-R17: a manual server's header/env secret rides the same
    translate_mcp_servers scrub as any registry mcp_server block -- it never
    lands literally in config.yaml, only as a ${VAR} ref, with the real value
    moved to the profile .env."""
    from core.services import hermes_profiles
    from core.services.credentials import mcp_server as m

    secret = "Bearer sk_live_super_secret"
    monkeypatch.setattr(
        m.integrations_repo,
        "get_manual_mcp_servers",
        AsyncMock(
            return_value={
                "acme": {
                    "transport": "streamable-http",
                    "url": "https://acme.example/mcp",
                    "headers": {"Authorization": secret},
                }
            }
        ),
    )
    monkeypatch.setattr(m.integrations_repo, "list_for_owner", AsyncMock(return_value=[]))

    connected = await m.build_connected_integration_servers("org_1")
    config, secrets = hermes_profiles.translate_mcp_servers(connected)

    assert config["acme"]["headers"]["Authorization"] == "${MCP_ACME_AUTHORIZATION}"
    assert secrets["MCP_ACME_AUTHORIZATION"] == secret
    assert secret not in repr(config)


# ---------------------------------------------------------------------------
# M6-R26 fix round 1, Critical 1: a provider mid-connect (no `connected` row
# yet -- routers/connections.py's three connect doors all call
# apply_connection BEFORE put_status) must still be projected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_connected_map_includes_pending_provider_not_yet_recorded(monkeypatch):
    """A first connect: `list_for_owner` returns [] (the status row isn't
    written yet) -- `pending` must still make the provider show up, using the
    real stripe (mcp_remote) registry entry as the brief's own test case."""
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(m.integrations_repo, "list_for_owner", AsyncMock(return_value=[]))

    servers = await m.build_connected_integration_servers("org_1", pending="stripe")

    assert servers == {
        "stripe": {
            "url": "https://mcp.stripe.com",
            "headers": {"Authorization": "Bearer ${MCP_STRIPE_ACCESS_TOKEN}"},
            "requestTimeoutMs": 60000,
        }
    }


@pytest.mark.asyncio
async def test_build_connected_map_pending_is_a_noop_when_row_already_exists(monkeypatch):
    """A reconnect (or any caller passing pending redundantly) must not
    double-count or otherwise change the outcome for an already-recorded row."""
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(return_value=[{"provider": "stripe", "status": "connected"}]),
    )

    servers = await m.build_connected_integration_servers("org_1", pending="stripe")

    assert list(servers.keys()) == ["stripe"]


@pytest.mark.asyncio
async def test_build_connected_map_no_pending_is_unaffected(monkeypatch):
    """Every existing caller (manual-MCP writes, marketplace deploy) passes no
    `pending` at all -- must behave exactly as before this parameter existed."""
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(m.integrations_repo, "list_for_owner", AsyncMock(return_value=[]))

    assert await m.build_connected_integration_servers("org_1") == {}


@pytest.mark.asyncio
async def test_reconcile_forwards_pending_to_refresh_integrations(monkeypatch):
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(m.integrations_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "a1"}]))
    calls = []

    async def fake_refresh(owner_id, agent_ids, *, pending=None):
        calls.append((owner_id, agent_ids, pending))

    monkeypatch.setattr("core.services.hermes_profiles.refresh_integrations", fake_refresh)

    await m.reconcile_integrations("org_1", pending="stripe")

    assert calls == [("org_1", ["a1"], "stripe")]


# ---------------------------------------------------------------------------
# There is ONE runtime, so reconcile_integrations classifies nothing: no
# `agentcore` parameter, and no containers read at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_never_reads_the_containers_table(monkeypatch):
    """Deletion PR-5 removed the ``agentcore`` parameter that existed only to save a
    classification read. Nothing may put one back: reconcile owns no transport decision."""
    import inspect

    from core.services.credentials import mcp_server as m

    assert "agentcore" not in inspect.signature(m.reconcile_integrations).parameters

    def _no_reads(*a, **kw):
        raise AssertionError("reconcile_integrations must not read the containers table")

    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _no_reads)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.services.hermes_profiles.refresh_integrations", AsyncMock())

    await m.reconcile_integrations("org_1")


# ---------------------------------------------------------------------------
# Ruling M6-R32 — integration_owned_server_keys: the set of mcp.servers keys a
# manual write must not collide with, without touching Secrets Manager.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_owned_server_keys_covers_all_three_shapes(monkeypatch):
    """mcp_remote (stripe), mcp_stdio (quickbooks), and wiring.mcp_server all count as
    'owned' -- and no Secrets Manager read happens for any of them (unlike
    build_connected_integration_servers, which fetches the mcp_server credential)."""
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {"provider": "stripe", "status": "connected"},  # mcp_remote
                {"provider": "quickbooks", "status": "connected"},  # mcp_stdio
            ]
        ),
    )
    secret_get = AsyncMock(side_effect=AssertionError("must not read Secrets Manager"))
    monkeypatch.setattr(m.integration_secret_store, "get", secret_get)

    owned = await m.integration_owned_server_keys("org_1")

    assert owned == {"stripe": "stripe", "quickbooks": "quickbooks"}


@pytest.mark.asyncio
async def test_integration_owned_server_keys_covers_mcp_server_wiring(monkeypatch):
    """No live registry provider uses the mcp_server wiring rail today (MCP providers moved to
    mcp_remote/mcp_stdio) -- exercise it via a synthetic provider, same convention as
    test_apply_openclaw_paths_injects_literal."""
    from core.services.credentials import mcp_server as m
    from core.services.credentials.registry import McpServer, Provider, Wiring

    synthetic = Provider(
        key="fakemcp",
        display_name="Fake MCP",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(mcp_server=McpServer(key="fakemcp", command="npx", args=[], env={})),
    )
    monkeypatch.setattr(m.registry, "get_provider", lambda key: synthetic)
    monkeypatch.setattr(
        m.integrations_repo, "list_for_owner", AsyncMock(return_value=[{"provider": "fakemcp", "status": "connected"}])
    )

    owned = await m.integration_owned_server_keys("org_1")

    assert owned == {"fakemcp": "fakemcp"}


@pytest.mark.asyncio
async def test_integration_owned_server_keys_skips_disconnected_and_unknown(monkeypatch):
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {"provider": "stripe", "status": "revoked"},
                {"provider": "no-such-provider", "status": "connected"},
                {"provider": "", "status": "connected"},
            ]
        ),
    )

    owned = await m.integration_owned_server_keys("org_1")

    assert owned == {}
