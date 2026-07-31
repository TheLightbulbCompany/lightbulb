import pytest


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
    result = await ac.apply_connection("org_1", "stripe", {"token": "sk_live_X"})
    assert result is None


@pytest.mark.asyncio
async def test_apply_mcp_server_writes_full_block(monkeypatch):
    # stripe's mcp_server wiring installs the full {command, args, env} server
    # block at mcp.servers.stripe in one write — the integration OWNS that key.
    from core.services.credentials import apply_connection as ac

    calls = []

    async def _set(owner, path, value):
        calls.append((owner, path, value))

    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path", _set)
    await ac.apply_connection("org_1", "stripe", {"token": "sk_live_X"})
    assert calls == [
        (
            "org_1",
            ["mcp", "servers", "stripe"],
            {
                "command": "npx",
                "args": ["-y", "@stripe/mcp@0.3.3", "--tools=all"],
                "env": {"STRIPE_SECRET_KEY": "sk_live_X"},
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
