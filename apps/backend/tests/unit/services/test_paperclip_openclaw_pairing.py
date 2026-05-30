"""Unit tests for the paperclip <-> openclaw pairing helpers."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.containers.config import build_device_paired_json
from core.crypto.operator_device import generate_operator_device, private_key_pem
from core.services.paperclip_openclaw_pairing import (
    fetch_paperclip_agents_for_owner,
    pair_with_openclaw,
    register_paperclip_agent_devices,
)


def _gw_agent(agent_id, *, pem=None, auth_token="svc", adapter_type="openclaw_gateway", name="Friday"):
    cfg = {
        "url": "wss://ws.isol8.co?token=svc&clientId=paperclip-operator",
        "authToken": auth_token,
        "sessionKeyStrategy": "issue",
        "sessionKey": "user_1",
    }
    if pem is not None:
        cfg["devicePrivateKeyPem"] = pem
    return {"id": agent_id, "name": name, "adapterType": adapter_type, "adapterConfig": cfg}


def _mock_admin_and_repo(agents, monkeypatch):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_x"))
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=agents)
    admin.patch_agent = AsyncMock()
    admin._http = MagicMock()
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie"),
    )
    return admin, repo


@pytest.mark.asyncio
async def test_register_builds_entry_and_binds_auth_token(monkeypatch):
    ident = generate_operator_device()
    agent = _gw_agent("agent_1", pem=private_key_pem(ident))
    admin, repo = _mock_admin_and_repo([agent], monkeypatch)

    entries = await register_paperclip_agent_devices(
        owner_id="user_1", gateway_token="GW_TOKEN", repo=repo, admin_client=admin
    )

    assert len(entries) == 1
    e = entries[0]
    assert e["deviceId"] == ident.device_id
    assert e["publicKey"] == ident.public_key_b64
    assert e["role"] == "operator"
    assert "operator.admin" in e["approvedScopes"]
    assert e["tokens"]["operator"]["token"] == "GW_TOKEN"
    # authToken rebound to the container gateway token; the URL ?token= (service
    # JWT, for the API Gateway authorizer) is untouched.
    admin.patch_agent.assert_awaited_once()
    body = admin.patch_agent.await_args.kwargs["body"]
    assert body["replaceAdapterConfig"] is True
    assert body["adapterConfig"]["authToken"] == "GW_TOKEN"
    assert body["adapterConfig"]["url"] == agent["adapterConfig"]["url"]
    # The paired entry seeds into build_device_paired_json alongside node+operator.
    paired = build_device_paired_json(
        "node_id", "node_pub", operator_entry={"deviceId": "op"}, extra_operator_entries=entries
    )
    import json as _json

    assert ident.device_id in _json.loads(paired)


@pytest.mark.asyncio
async def test_register_skips_agents_without_minted_key_or_wrong_type(monkeypatch):
    agents = [
        _gw_agent("a1", pem=None),  # no minted device key
        _gw_agent("a2", pem="x", adapter_type="claude_local"),  # not openclaw_gateway
    ]
    admin, repo = _mock_admin_and_repo(agents, monkeypatch)
    entries = await register_paperclip_agent_devices(
        owner_id="user_1", gateway_token="GW", repo=repo, admin_client=admin
    )
    assert entries == []
    admin.patch_agent.assert_not_called()


@pytest.mark.asyncio
async def test_register_is_idempotent_when_token_already_bound(monkeypatch):
    agent = _gw_agent("a1", pem=private_key_pem(generate_operator_device()), auth_token="GW")
    admin, repo = _mock_admin_and_repo([agent], monkeypatch)
    entries = await register_paperclip_agent_devices(
        owner_id="user_1", gateway_token="GW", repo=repo, admin_client=admin
    )
    assert len(entries) == 1  # still produces the paired entry
    admin.patch_agent.assert_not_called()  # but no rewrite round-trip


@pytest.mark.asyncio
async def test_fetch_returns_empty_list_for_personal_user_with_no_paperclip_row():
    """Personal user (owner_id starts with 'user_') has no PaperclipCompany
    row -> return []. Container provision falls back to 'main'."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    admin_client = MagicMock()

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
    admin_client.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_for_personal_user_returns_their_company_agents(monkeypatch):
    """Personal user happy path: repo.get(user_id) -> company_id ->
    list_agents -> return list."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(return_value=[{"id": pc_uuid, "name": "Friday", "role": "ceo"}])

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == [{"id": pc_uuid, "name": "Friday", "role": "ceo"}]
    admin_client.list_agents.assert_called_once_with(
        session_cookie="cookie123",
        company_id="co_xyz",
    )


@pytest.mark.asyncio
async def test_fetch_for_org_uses_get_org_company_id(monkeypatch):
    """Org owner (owner_id starts with 'org_') resolves company via
    repo.get_org_company_id, NOT repo.get."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    repo = MagicMock()
    repo.get = AsyncMock()  # should NOT be called
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(return_value=[{"id": pc_uuid, "name": "Friday", "role": "ceo"}])

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="org_xyz",
        repo=repo,
        admin_client=admin_client,
    )

    assert len(result) == 1
    repo.get.assert_not_awaited()
    repo.get_org_company_id.assert_awaited_once_with("org_xyz")


@pytest.mark.asyncio
async def test_fetch_for_org_with_no_company_returns_empty():
    """Org never had Paperclip provisioned -> return []. Container provision
    falls back to 'main'."""
    repo = MagicMock()
    repo.get_org_company_id = AsyncMock(return_value=None)
    admin_client = MagicMock()

    result = await fetch_paperclip_agents_for_owner(
        owner_id="org_xyz",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
    admin_client.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_paperclip_api_error(monkeypatch):
    """If Paperclip is down or returns 5xx, don't fail container provision -
    fall back to legacy 'main' agent. Log the error for ops visibility."""
    from core.services.paperclip_admin_client import PaperclipApiError

    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(side_effect=PaperclipApiError("503", status_code=503, body={}))

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_transient_httpx_error(monkeypatch):
    """Transient httpx errors (connect timeouts, DNS failures, etc.) raised
    BEFORE the request reaches Paperclip must also fall back to []. The
    helper's contract is best-effort: container provision must never fail
    because Paperclip is down or the network is flaky."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_admin_cookie_failure(monkeypatch):
    """If credential loading itself fails (e.g. admin session-cookie fetch
    raises before the list_agents request goes out), still fall back to
    []. The fetch must not abort container provision."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock()  # never reached

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(side_effect=RuntimeError("admin secret missing")),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
    admin_client.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_propagates_unexpected_errors(monkeypatch):
    """The except clause is tightened to ``(PaperclipApiError,
    httpx.HTTPError, RuntimeError)``. Non-network bugs (e.g. KeyError,
    AttributeError, TypeError) inside the helper or the admin client
    must NOT be swallowed as "Paperclip unavailable" — they need to
    surface in CloudWatch so we can fix them.

    A blanket Exception catch would silently mask real programming bugs
    behind a fallback to the legacy 'main' agent path; the resulting
    UX would be "agents disappear from /teams" with no operator signal.
    """
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(side_effect=KeyError("missing-field"))

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    with pytest.raises(KeyError):
        await fetch_paperclip_agents_for_owner(
            owner_id="user_abc",
            repo=repo,
            admin_client=admin_client,
        )


@pytest.mark.asyncio
async def test_pair_calls_agents_create_with_uuid_then_update_for_display_name():
    """pair_with_openclaw sends two RPCs:
    1. agents.create with name=<paperclip_uuid> (so OpenClaw uses the
       UUID as the on-disk agentId).
    2. agents.update with name=<friendly_name> to fix the display."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True, "agentId": pc_uuid})

    await pair_with_openclaw(
        owner_id="user_abc",
        paperclip_agent={
            "id": pc_uuid,
            "name": "Code Reviewer",
            "role": "engineer",
        },
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
    )

    assert pool.send_rpc.await_count == 2

    create_call = pool.send_rpc.await_args_list[0]
    assert create_call.kwargs["method"] == "agents.create"
    assert create_call.kwargs["params"]["name"] == pc_uuid
    assert create_call.kwargs["params"]["workspace"] == (f"/home/node/.openclaw/workspaces/{pc_uuid}")
    # The pool's user_id kwarg is actually owner_id under the hood —
    # the existing gateway pool's signature is what it is.
    assert create_call.kwargs["user_id"] == "user_abc"

    update_call = pool.send_rpc.await_args_list[1]
    assert update_call.kwargs["method"] == "agents.update"
    assert update_call.kwargs["params"]["agentId"] == pc_uuid
    assert update_call.kwargs["params"]["name"] == "Code Reviewer"


@pytest.mark.asyncio
async def test_pair_skips_if_agent_already_exists():
    """If agents.create returns 'already exists' (idempotent reprovision
    path), skip the update — the agent is already paired correctly."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pool = MagicMock()
    # OpenClaw returns INVALID_REQUEST with "agent already exists" in message
    pool.send_rpc = AsyncMock(
        side_effect=RuntimeError('{"code":-32602,"message":"agent \\"' + pc_uuid + '\\" already exists"}')
    )

    # Should not raise — idempotent.
    await pair_with_openclaw(
        owner_id="org_xyz",
        paperclip_agent={"id": pc_uuid, "name": "Friday", "role": "ceo"},
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
    )

    # Only the create attempt; update was skipped because create raised.
    assert pool.send_rpc.await_count == 1


@pytest.mark.asyncio
async def test_pair_with_extra_update_fields_none_preserves_existing_behavior():
    """extra_update_fields=None (default) -> agents.update RPC params
    contain only agentId + name, identical to today. Backwards
    compatibility for the chat-side caller (_intercept_agents_create)
    that doesn't supply extras."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True, "agentId": pc_uuid})

    await pair_with_openclaw(
        owner_id="user_abc",
        paperclip_agent={"id": pc_uuid, "name": "Code Reviewer", "role": "engineer"},
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
        # Note: NOT passing extra_update_fields — exercises default arg.
    )

    update_call = pool.send_rpc.await_args_list[1]
    assert update_call.kwargs["params"] == {
        "agentId": pc_uuid,
        "name": "Code Reviewer",
    }


@pytest.mark.asyncio
async def test_pair_with_extra_update_fields_threads_into_update_rpc():
    """extra_update_fields is merged into the agents.update RPC params
    so marketplace deploys can carry the full slice-derived agent entry
    (identity, skills, reasoningDefault, etc.) through the same call."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True, "agentId": pc_uuid})

    await pair_with_openclaw(
        owner_id="user_abc",
        paperclip_agent={"id": pc_uuid, "name": "Ray", "role": "general"},
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
        extra_update_fields={
            "identity": {"emoji": "🍃", "vibe": "calm"},
            "skills": ["resy"],
            "reasoningDefault": "balanced",
            # These should be overridden by the helper:
            "agentId": "ATTEMPTED_OVERRIDE",
            "name": "ATTEMPTED_OVERRIDE",
        },
    )

    update_call = pool.send_rpc.await_args_list[1]
    params = update_call.kwargs["params"]
    # Caller-supplied agentId/name are clobbered — the helper is the
    # single source of truth for those.
    assert params["agentId"] == pc_uuid
    assert params["name"] == "Ray"
    # extras are threaded through.
    assert params["identity"] == {"emoji": "🍃", "vibe": "calm"}
    assert params["skills"] == ["resy"]
    assert params["reasoningDefault"] == "balanced"
