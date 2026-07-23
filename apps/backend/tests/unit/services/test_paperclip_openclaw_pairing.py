"""Unit tests for the paperclip <-> openclaw pairing helpers."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.containers.config import build_device_paired_json
from core.crypto.operator_device import generate_operator_device, private_key_pem
from core.services.paperclip_openclaw_pairing import (
    ensure_operator_device_paired,
    fetch_paperclip_agents_for_owner,
    pair_with_openclaw,
    register_paperclip_agent_devices,
    resolve_company_id_for_owner,
)


def _gw_agent(
    agent_id,
    *,
    pem=None,
    auth_token="svc",
    adapter_type="openclaw_gateway",
    name="Friday",
    claimed_api_key_path=None,
):
    cfg = {
        "url": "wss://ws.isol8.co?token=svc&clientId=paperclip-operator",
        "authToken": auth_token,
        "sessionKeyStrategy": "issue",
        "sessionKey": "user_1",
    }
    if pem is not None:
        cfg["devicePrivateKeyPem"] = pem
    if claimed_api_key_path is not None:
        cfg["claimedApiKeyPath"] = claimed_api_key_path
    return {"id": agent_id, "name": name, "adapterType": adapter_type, "adapterConfig": cfg}


def _claimed_path(agent_id):
    return f"~/.openclaw/workspace-{agent_id}/paperclip-claimed-api-key.json"


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
    # claimedApiKeyPath points at THIS agent's own per-agent workspace file.
    assert body["adapterConfig"]["claimedApiKeyPath"] == _claimed_path("agent_1")
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
async def test_register_is_idempotent_when_token_and_path_already_bound(monkeypatch):
    agent = _gw_agent(
        "a1",
        pem=private_key_pem(generate_operator_device()),
        auth_token="GW",
        claimed_api_key_path=_claimed_path("a1"),
    )
    admin, repo = _mock_admin_and_repo([agent], monkeypatch)
    entries = await register_paperclip_agent_devices(
        owner_id="user_1", gateway_token="GW", repo=repo, admin_client=admin
    )
    assert len(entries) == 1  # still produces the paired entry
    admin.patch_agent.assert_not_called()  # but no rewrite round-trip


@pytest.mark.asyncio
async def test_register_patches_when_claimed_path_stale_even_if_token_current(monkeypatch):
    """authToken already bound but claimedApiKeyPath missing → still patch so
    the per-agent path lands (otherwise the agent keeps the colliding default)."""
    agent = _gw_agent("a1", pem=private_key_pem(generate_operator_device()), auth_token="GW")
    admin, repo = _mock_admin_and_repo([agent], monkeypatch)
    await register_paperclip_agent_devices(owner_id="user_1", gateway_token="GW", repo=repo, admin_client=admin)
    admin.patch_agent.assert_awaited_once()
    body = admin.patch_agent.await_args.kwargs["body"]
    assert body["adapterConfig"]["claimedApiKeyPath"] == _claimed_path("a1")
    assert body["adapterConfig"]["authToken"] == "GW"


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
async def test_fetch_for_org_uses_list_by_org_id(monkeypatch):
    """Org owner (owner_id starts with 'org_') resolves company via
    repo.list_by_org_id (the org's ACTIVE row), NOT repo.get and NOT
    repo.get_org_company_id (which would return a failed row's id)."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    repo = MagicMock()
    repo.get = AsyncMock()  # should NOT be called
    repo.list_by_org_id = AsyncMock(return_value=[{"status": "active", "company_id": "co_xyz"}])
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
    repo.list_by_org_id.assert_awaited_once_with("org_xyz")


@pytest.mark.asyncio
async def test_fetch_for_org_with_no_company_returns_empty():
    """Org never had Paperclip provisioned (no active row) -> return [].
    Container provision falls back to 'main'."""
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[])
    admin_client = MagicMock()

    result = await fetch_paperclip_agents_for_owner(
        owner_id="org_xyz",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
    admin_client.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_company_id_none_for_personal_failed_marker():
    """A personal owner whose only row is a net-new ``failed`` marker
    (``company_id=""``) resolves to None — NOT the empty string. Every caller
    guards with ``if company_id is None``, so an "" would slip through into a
    doomed Paperclip call with an empty company id."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(status="failed", company_id=""))

    assert await resolve_company_id_for_owner(owner_id="user_abc", repo=repo) is None


@pytest.mark.asyncio
async def test_resolve_company_id_none_for_personal_failed_row_with_company():
    """Even a ``failed`` row that happens to carry a company_id resolves to
    None: a failed provision is treated as "no company" for pairing/lifecycle."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(status="failed", company_id="co_partial"))

    assert await resolve_company_id_for_owner(owner_id="user_abc", repo=repo) is None


@pytest.mark.asyncio
async def test_resolve_company_id_none_for_org_failed_marker_empty():
    """An org whose only row is a net-new ``failed`` marker (empty company_id)
    resolves to None: list_by_org_id has no active row."""
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[{"status": "failed", "company_id": ""}])

    assert await resolve_company_id_for_owner(owner_id="org_abc", repo=repo) is None


@pytest.mark.asyncio
async def test_resolve_company_id_returns_id_for_active_personal_row():
    """Regression guard: a healthy active row still resolves to its company_id."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(status="active", company_id="co_live"))

    assert await resolve_company_id_for_owner(owner_id="user_abc", repo=repo) == "co_live"


@pytest.mark.asyncio
async def test_resolve_company_id_none_for_org_failed_row_with_company():
    """An ORG whose only row is ``failed`` but carries a REAL adopted company_id
    (the reconcile Friday-partial case) must resolve to None — a deliberately-
    failed workspace must not leak its id to the ``if company_id is None``
    callers. ``get_org_company_id`` is mocked to prove the OLD path returned the
    id (the bug this fixes); the fix routes org resolution through
    ``list_by_org_id`` and requires an ACTIVE row, so both are stubbed and the
    result is None regardless of which the implementation calls."""
    repo = MagicMock()
    repo.get_org_company_id = AsyncMock(return_value="co_partial")  # old path returned the id
    repo.list_by_org_id = AsyncMock(return_value=[{"status": "failed", "company_id": "co_partial"}])

    assert await resolve_company_id_for_owner(owner_id="org_abc", repo=repo) is None


@pytest.mark.asyncio
async def test_resolve_company_id_returns_id_for_active_org_row():
    """Regression guard: an ORG with an ``active`` row still resolves to its
    company_id. Green before and after the fix — the happy org path's outcome is
    unchanged, only the lookup mechanism (get_org_company_id → list_by_org_id)."""
    repo = MagicMock()
    repo.get_org_company_id = AsyncMock(return_value="co_live")
    repo.list_by_org_id = AsyncMock(return_value=[{"status": "active", "company_id": "co_live"}])

    assert await resolve_company_id_for_owner(owner_id="org_abc", repo=repo) == "co_live"


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
    assert create_call.kwargs["params"]["workspace"] == (f"/home/node/.openclaw/workspace-{pc_uuid}")
    # The pool's user_id kwarg is actually owner_id under the hood —
    # the existing gateway pool's signature is what it is.
    assert create_call.kwargs["user_id"] == "user_abc"

    update_call = pool.send_rpc.await_args_list[1]
    assert update_call.kwargs["method"] == "agents.update"
    assert update_call.kwargs["params"]["agentId"] == pc_uuid
    assert update_call.kwargs["params"]["name"] == "Code Reviewer"


@pytest.mark.asyncio
async def test_pair_already_exists_still_applies_update_to_heal():
    """If agents.create returns 'already exists' (idempotent reprovision /
    a prior attempt that lost its update response), fall through to the
    agents.update so a stranded name=<uuid> gets renamed to the friendly
    name. The update is itself idempotent."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    def _side(*_args, **kwargs):
        # create raises "already exists"; update succeeds.
        if kwargs.get("method") == "agents.create":
            raise RuntimeError('{"code":-32602,"message":"agent \\"' + pc_uuid + '\\" already exists"}')
        return {"ok": True}

    pool = MagicMock()
    pool.send_rpc = AsyncMock(side_effect=_side)

    # Should not raise — idempotent.
    await pair_with_openclaw(
        owner_id="org_xyz",
        paperclip_agent={"id": pc_uuid, "name": "Friday", "role": "ceo"},
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
    )

    # Create raised "already exists" but the update still ran to heal the name.
    assert pool.send_rpc.await_count == 2
    update_call = pool.send_rpc.await_args_list[1]
    assert update_call.kwargs["method"] == "agents.update"
    assert update_call.kwargs["params"]["name"] == "Friday"


@pytest.mark.asyncio
async def test_pair_retries_once_on_timeout_then_succeeds(monkeypatch):
    """A single dropped RPC response (TimeoutError) is retried and heals —
    the whole Hire no longer fails on one transient gateway hiccup."""
    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", AsyncMock())
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    calls = {"n": 0}

    def _side(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError()  # first create's response is lost
        return {"ok": True}

    pool = MagicMock()
    pool.send_rpc = AsyncMock(side_effect=_side)

    await pair_with_openclaw(
        owner_id="user_abc",
        paperclip_agent={"id": pc_uuid, "name": "Code Reviewer"},
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
    )

    # attempt 1: create (timeout). attempt 2: create + update (ok). = 3 RPCs.
    assert pool.send_rpc.await_count == 3
    assert pool.send_rpc.await_args_list[-1].kwargs["method"] == "agents.update"


@pytest.mark.asyncio
async def test_pair_raises_after_exhausting_timeout_retries(monkeypatch):
    """A persistent timeout still surfaces — the deploy router then unwinds
    the partial deploy and rolls back the Paperclip agent."""
    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", AsyncMock())
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    pool = MagicMock()
    pool.send_rpc = AsyncMock(side_effect=TimeoutError())

    with pytest.raises(TimeoutError):
        await pair_with_openclaw(
            owner_id="user_abc",
            paperclip_agent={"id": pc_uuid, "name": "Code Reviewer"},
            gateway_pool=pool,
            ip="10.0.0.5",
            token="gw_token",
        )

    # Two create attempts (the bounded retry), both timed out.
    assert pool.send_rpc.await_count == 2


@pytest.mark.asyncio
async def test_pair_does_not_retry_non_timeout_error():
    """A genuine protocol/schema RuntimeError (not 'already exists', not a
    timeout) propagates immediately — retrying a deterministic rejection
    would just stall the Hire."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    pool = MagicMock()
    pool.send_rpc = AsyncMock(side_effect=RuntimeError("INVALID_REQUEST: bad workspace"))

    with pytest.raises(RuntimeError, match="bad workspace"):
        await pair_with_openclaw(
            owner_id="user_abc",
            paperclip_agent={"id": pc_uuid, "name": "Code Reviewer"},
            gateway_pool=pool,
            ip="10.0.0.5",
            token="gw_token",
        )

    # No retry: single create attempt.
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


# ---------------------------------------------------------------------------
# ensure_operator_device_paired — live (post-provision) device pairing
# ---------------------------------------------------------------------------


def _mock_live_pairing_deps(monkeypatch):
    """Patch the live-pairing function's self-constructed deps.

    Returns (paired_store, admin, staged) so tests can assert what landed in the
    container's paired.json, whether the authToken rebind ran, and what
    per-agent identity file was staged.
    """
    paired_store: dict = {}

    class _FakeAdapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def mutate_paired_json(self, mutator):
            mutator(paired_store)

    monkeypatch.setattr("core.services.desktop_pair.EfsWorkspaceAdapter", _FakeAdapter)
    monkeypatch.setattr("core.containers.get_workspace", lambda: MagicMock())

    admin = MagicMock()
    admin.patch_agent = AsyncMock()
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.PaperclipAdminClient",
        lambda **kwargs: admin,
    )
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie"),
    )

    # Per-agent identity staging deps (step 3 of ensure_operator_device_paired).
    # ensure_paperclip_api_key + resolve_company_id_for_owner + the staging
    # helper are patched so the test never touches Secrets Manager / boto3 / EFS.
    monkeypatch.setattr(
        "core.services.paperclip_api_key.ensure_paperclip_api_key",
        AsyncMock(return_value="agent-key"),
    )
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
        AsyncMock(return_value="co_x"),
    )
    monkeypatch.setattr(
        "core.repositories.paperclip_repo.PaperclipRepo",
        lambda **kwargs: MagicMock(),
    )

    staged: dict = {}

    async def _fake_stage(*, owner_id, agent_id, token, company_id, api_url, workspace):
        staged.update(
            owner_id=owner_id,
            agent_id=agent_id,
            token=token,
            company_id=company_id,
            api_url=api_url,
        )

    monkeypatch.setattr(
        "core.services.friday_identity.stage_paperclip_identity_for_agent",
        _fake_stage,
    )
    monkeypatch.setattr(
        "core.config.settings.PAPERCLIP_INTERNAL_URL",
        "http://paperclip.internal",
        raising=False,
    )
    return paired_store, admin, staged


@pytest.mark.asyncio
async def test_ensure_operator_device_paired_merges_and_binds(monkeypatch):
    """Happy path: the new agent's device lands in paired.json AND its
    authToken is rebound to the container gateway token."""
    paired, admin, staged = _mock_live_pairing_deps(monkeypatch)
    ident = generate_operator_device()
    agent = _gw_agent("agent_live", pem=private_key_pem(ident), auth_token="service-jwt")

    await ensure_operator_device_paired(owner_id="user_1", paperclip_agent=agent, gateway_token="GW_TOKEN")

    # 1. Device merged into the container trust store as an operator entry.
    assert ident.device_id in paired
    entry = paired[ident.device_id]
    assert entry["publicKey"] == ident.public_key_b64
    assert entry["role"] == "operator"
    assert entry["tokens"]["operator"]["token"] == "GW_TOKEN"
    # 2. authToken rebound to the gateway token (signature is over auth.token)
    #    AND claimedApiKeyPath set to this agent's own per-agent file.
    admin.patch_agent.assert_awaited_once()
    body = admin.patch_agent.await_args.kwargs["body"]
    assert body["adapterConfig"]["authToken"] == "GW_TOKEN"
    assert body["adapterConfig"]["claimedApiKeyPath"] == _claimed_path("agent_live")
    assert body["replaceAdapterConfig"] is True
    # 3. The agent's per-agent Paperclip identity file is minted + staged so the
    #    claimedApiKeyPath above resolves to a real credentials file. Live-create
    #    flows never run write_user_configs, so this seam is the only one that
    #    stages it for them.
    assert staged == {
        "owner_id": "user_1",
        "agent_id": "agent_live",
        "token": "agent-key",
        "company_id": "co_x",
        "api_url": "http://paperclip.internal",
    }


@pytest.mark.asyncio
async def test_ensure_operator_device_paired_stages_identity_even_when_token_already_bound(monkeypatch):
    """The identity staging (step 3) runs unconditionally, even when authToken +
    claimedApiKeyPath are already current and the patch_agent round-trip is
    skipped. Otherwise a re-paired agent whose adapterConfig is already correct
    would never get its credentials file written."""
    paired, admin, staged = _mock_live_pairing_deps(monkeypatch)
    ident = generate_operator_device()
    agent = _gw_agent(
        "agent_live",
        pem=private_key_pem(ident),
        auth_token="GW_TOKEN",
        claimed_api_key_path=_claimed_path("agent_live"),
    )

    await ensure_operator_device_paired(owner_id="user_1", paperclip_agent=agent, gateway_token="GW_TOKEN")

    # No adapterConfig rebind needed, but the identity file is still staged.
    admin.patch_agent.assert_not_awaited()
    assert staged["agent_id"] == "agent_live"
    assert staged["token"] == "agent-key"


@pytest.mark.asyncio
async def test_ensure_operator_device_paired_staging_failure_is_swallowed(monkeypatch):
    """A failure minting the per-agent key (returns None) must not raise — the
    device is still paired; the next reprovision re-stages."""
    paired, admin, staged = _mock_live_pairing_deps(monkeypatch)
    monkeypatch.setattr(
        "core.services.paperclip_api_key.ensure_paperclip_api_key",
        AsyncMock(side_effect=httpx.HTTPError("boom")),
    )
    ident = generate_operator_device()
    agent = _gw_agent("agent_live", pem=private_key_pem(ident), auth_token="service-jwt")

    # Must not raise even though the key mint blew up.
    await ensure_operator_device_paired(owner_id="user_1", paperclip_agent=agent, gateway_token="GW_TOKEN")
    # Device still merged + authToken rebound despite the staging failure.
    assert ident.device_id in paired
    assert staged == {}


@pytest.mark.asyncio
async def test_ensure_operator_device_paired_is_idempotent(monkeypatch):
    """A device already present with the same pubkey is left untouched (its
    metadata/tokens preserved) and no duplicate write occurs when authToken AND
    claimedApiKeyPath are already current."""
    paired, admin, _ = _mock_live_pairing_deps(monkeypatch)
    ident = generate_operator_device()
    agent = _gw_agent(
        "agent_live",
        pem=private_key_pem(ident),
        auth_token="GW_TOKEN",
        claimed_api_key_path=_claimed_path("agent_live"),
    )
    sentinel = {"publicKey": ident.public_key_b64, "role": "operator", "_marker": "keep"}
    paired[ident.device_id] = sentinel

    await ensure_operator_device_paired(owner_id="user_1", paperclip_agent=agent, gateway_token="GW_TOKEN")

    # Existing entry preserved (not clobbered with a fresh entry).
    assert paired[ident.device_id] is sentinel
    # authToken + claimedApiKeyPath already current → no rebind round-trip.
    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_operator_device_paired_skips_non_gateway_adapter(monkeypatch):
    paired, admin, staged = _mock_live_pairing_deps(monkeypatch)
    ident = generate_operator_device()
    agent = _gw_agent("a", pem=private_key_pem(ident), adapter_type="some_other")

    await ensure_operator_device_paired(owner_id="o", paperclip_agent=agent, gateway_token="GW")
    assert paired == {}
    admin.patch_agent.assert_not_awaited()
    # Non-gateway agents short-circuit before staging.
    assert staged == {}


@pytest.mark.asyncio
async def test_ensure_operator_device_paired_skips_without_pem(monkeypatch):
    paired, admin, staged = _mock_live_pairing_deps(monkeypatch)
    agent = _gw_agent("a", pem=None)  # no devicePrivateKeyPem

    await ensure_operator_device_paired(owner_id="o", paperclip_agent=agent, gateway_token="GW")
    assert paired == {}
    admin.patch_agent.assert_not_awaited()
    assert staged == {}


@pytest.mark.asyncio
async def test_ensure_operator_device_paired_token_bind_failure_is_swallowed(monkeypatch):
    """A failing authToken patch must not raise — the device is still paired."""
    paired, admin, _ = _mock_live_pairing_deps(monkeypatch)
    admin.patch_agent = AsyncMock(side_effect=httpx.HTTPError("boom"))
    ident = generate_operator_device()
    agent = _gw_agent("a", pem=private_key_pem(ident), auth_token="service-jwt")

    # Must not raise.
    await ensure_operator_device_paired(owner_id="o", paperclip_agent=agent, gateway_token="GW_TOKEN")
    # Device still merged despite the token-bind failure.
    assert ident.device_id in paired
