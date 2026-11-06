from unittest.mock import AsyncMock, MagicMock, call

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle


def _wire_prepare(monkeypatch, agents):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=agents)
    admin.list_routines = AsyncMock(return_value=[])
    admin.list_issues = AsyncMock(return_value=[])
    admin.patch_routine = AsyncMock(return_value={})
    admin.patch_issue = AsyncMock(return_value={})
    http = MagicMock()
    http.aclose = AsyncMock()
    monkeypatch.setattr(lifecycle, "PaperclipRepo", lambda table_name: MagicMock())
    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value="company"))
    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda **_kwargs: http)
    monkeypatch.setattr(lifecycle, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_seeded_agent_ids",
        AsyncMock(side_effect=AssertionError("lifecycle must not depend on seeded ids")),
    )
    return admin


@pytest.mark.asyncio
async def test_specialist_delete_uses_surviving_root_ceo(monkeypatch):
    _wire_prepare(
        monkeypatch,
        [
            {"id": "root", "name": "Arbitrary", "role": "ceo", "reportsTo": None},
            {"id": "specialist", "name": "Friday", "role": "researcher", "reportsTo": "root"},
        ],
    )

    result = await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="specialist")

    assert result["replacement_agent_id"] == "root"


@pytest.mark.asyncio
async def test_renamed_final_root_ceo_cannot_be_deleted(monkeypatch):
    admin = _wire_prepare(
        monkeypatch,
        [
            {"id": "root", "name": "Ada", "role": "ceo", "reportsTo": None},
            {"id": "specialist", "name": "Friday", "role": "researcher", "reportsTo": "root"},
        ],
    )

    with pytest.raises(RuntimeError, match="coordinator cannot be deleted"):
        await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="root")

    admin.list_routines.assert_not_awaited()
    admin.list_issues.assert_not_awaited()


@pytest.mark.asyncio
async def test_root_ceo_can_be_deleted_after_another_root_exists(monkeypatch):
    _wire_prepare(
        monkeypatch,
        [
            {"id": "old-root", "name": "Ada", "role": "ceo", "reportsTo": None},
            {"id": "new-root", "name": "Bea", "role": "ceo", "reportsTo": None},
            {"id": "specialist", "name": "Cid", "role": "engineer", "reportsTo": "old-root"},
        ],
    )

    result = await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="old-root")

    assert result["replacement_agent_id"] == "new-root"


@pytest.mark.asyncio
async def test_replacement_falls_back_to_first_surviving_agent(monkeypatch):
    _wire_prepare(
        monkeypatch,
        [
            {"id": "retiring", "name": "Ada", "role": "general", "reportsTo": None},
            {"id": "first", "name": "Bea", "role": "engineer", "reportsTo": None},
            {"id": "second", "name": "Cid", "role": "researcher", "reportsTo": None},
        ],
    )

    result = await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="retiring")

    assert result["replacement_agent_id"] == "first"


@pytest.mark.asyncio
async def test_paperclip_delete_conditionally_removes_starter_uuid(monkeypatch):
    repo = MagicMock()
    repo.remove_starter_agent_id = AsyncMock(return_value=None)
    repo.delete_agent_key = AsyncMock(return_value=None)
    monkeypatch.setattr(lifecycle, "PaperclipRepo", lambda table_name: repo)
    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value="company"))
    admin = MagicMock()
    admin.delete_agent = AsyncMock(return_value={})
    monkeypatch.setattr(lifecycle, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    http = MagicMock()
    http.aclose = AsyncMock()
    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda **_kwargs: http)

    await lifecycle.delete_paperclip_for_chat(owner_id="owner", agent_id="starter-uuid")
    await lifecycle.delete_paperclip_for_chat(owner_id="owner", agent_id="starter-uuid")

    assert admin.delete_agent.await_count == 2
    assert repo.remove_starter_agent_id.await_args_list == [
        call("owner", "starter-uuid"),
        call("owner", "starter-uuid"),
    ]
    # The deleted agent's Paperclip API key leaves the workspace row with it.
    assert repo.delete_agent_key.await_args_list == repo.remove_starter_agent_id.await_args_list


# ---------------------------------------------------------------------------
# sync_agent_adapter_config -- a re-created containers row mints a new hook_token, but the
# surviving company's hermes_gateway agents still carry the old one as their apiKey
# ---------------------------------------------------------------------------


def _wire_repoint(monkeypatch, *, hook_token="hook-tok"):
    """Paperclip admin behind a real async context manager (``sync_agent_adapter_config`` uses
    ``async with httpx.AsyncClient(...)``), plus the container row the adapter builder reads."""
    admin = MagicMock()
    admin.patch_agent = AsyncMock(return_value={})

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda **_kwargs: _Client())
    monkeypatch.setattr(lifecycle, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(lifecycle.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": hook_token}))
    return admin


def _wire_sync(monkeypatch, *, agents, company_id="co-1", hook_token="new-tok"):
    admin = _wire_repoint(monkeypatch, hook_token=hook_token)
    admin.list_agents = AsyncMock(return_value=agents)
    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value=company_id))
    return admin


@pytest.mark.asyncio
async def test_sync_repatches_every_hermes_agent_with_the_rows_current_token(monkeypatch):
    admin = _wire_sync(
        monkeypatch,
        agents=[
            {"id": "a1", "adapterType": lifecycle.HERMES_GATEWAY_TYPE},
            {"id": "a2", "adapterType": lifecycle.HERMES_GATEWAY_TYPE},
            {"id": "a3", "adapterType": "openclaw_gateway"},
        ],
    )

    assert await lifecycle.sync_agent_adapter_config("org_1") == 2

    patched = {c.kwargs["agent_id"]: c.kwargs["body"] for c in admin.patch_agent.await_args_list}
    assert set(patched) == {"a1", "a2"}  # only hermes_gateway agents carry this apiKey snapshot
    for body in patched.values():
        assert body["adapterConfig"]["apiKey"] == "new-tok"
        assert body["replaceAdapterConfig"] is True


@pytest.mark.asyncio
async def test_sync_reads_the_token_consistently_at_patch_time(monkeypatch):
    """Two concurrent provisions: last writer must win with the row's CURRENT token, so the
    token is re-read (consistent) inside the sync, never captured by a caller earlier."""
    admin = _wire_sync(monkeypatch, agents=[{"id": "a1", "adapterType": lifecycle.HERMES_GATEWAY_TYPE}])
    reads = []

    async def _get(owner_id, *, consistent=False):
        reads.append(consistent)
        return {"hook_token": "fresh-tok"}

    monkeypatch.setattr(lifecycle.container_repo, "get_by_owner_id", _get)

    await lifecycle.sync_agent_adapter_config("org_1")

    assert reads == [True]
    assert admin.patch_agent.await_args.kwargs["body"]["adapterConfig"]["apiKey"] == "fresh-tok"


@pytest.mark.asyncio
async def test_sync_is_a_noop_without_a_company(monkeypatch):
    admin = _wire_sync(monkeypatch, agents=[], company_id=None)

    assert await lifecycle.sync_agent_adapter_config("org_1") == 0

    admin.list_agents.assert_not_awaited()
    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_caps_an_engineers_concurrent_runs_and_keeps_its_runtime_config(monkeypatch):
    hb = {"enabled": True, "intervalSec": 86400}
    admin = _wire_sync(
        monkeypatch,
        agents=[
            {
                "id": "forge",
                "role": "engineer",
                "adapterType": lifecycle.HERMES_GATEWAY_TYPE,
                "runtimeConfig": {"aiConnection": {"x": 1}, "heartbeat": hb},
            },
            {
                "id": "done",
                "role": "engineer",
                "adapterType": lifecycle.HERMES_GATEWAY_TYPE,
                "runtimeConfig": {"heartbeat": {"maxConcurrentRuns": 2}},
            },
            {"id": "ceo", "role": "ceo", "adapterType": lifecycle.HERMES_GATEWAY_TYPE, "runtimeConfig": {}},
        ],
    )

    await lifecycle.sync_agent_adapter_config("org_1")

    patched = {c.kwargs["agent_id"]: c.kwargs["body"] for c in admin.patch_agent.await_args_list}
    assert patched["forge"]["runtimeConfig"] == {"aiConnection": {"x": 1}, "heartbeat": {**hb, "maxConcurrentRuns": 2}}
    assert "runtimeConfig" not in patched["done"]  # already capped: no runtimeConfig rewrite
    assert "runtimeConfig" not in patched["ceo"]


@pytest.mark.asyncio
async def test_sync_raises_on_a_failed_patch(monkeypatch):
    admin = _wire_sync(monkeypatch, agents=[{"id": "a1", "adapterType": lifecycle.HERMES_GATEWAY_TYPE}])
    admin.patch_agent.side_effect = RuntimeError("paperclip 503")

    with pytest.raises(RuntimeError, match="paperclip 503"):
        await lifecycle.sync_agent_adapter_config("org_1")


# ---------------------------------------------------------------------------
# canCreateAgents -- Paperclip 916 defaults a new agent to TRUE; the chat and
# marketplace doors (runtime_rpc agents.create, catalog deploy, marketplace
# hire, listing builder) must reach Paperclip with an explicit false.
# ---------------------------------------------------------------------------


def _wire_real_create(monkeypatch):
    import json

    import httpx

    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.read().decode()))
        return httpx.Response(201, json={"id": "new-agent"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        lifecycle.httpx,
        "AsyncClient",
        lambda **_kw: real_client(transport=httpx.MockTransport(handler), base_url="http://pc.test"),
    )
    monkeypatch.setattr(lifecycle, "PaperclipRepo", lambda table_name: MagicMock())
    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value="company"))
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(lifecycle, "resolve_agent_adapter", AsyncMock(return_value=("hermes_gateway", {})))
    return bodies


@pytest.mark.asyncio
async def test_chat_create_sends_can_create_agents_false(monkeypatch):
    bodies = _wire_real_create(monkeypatch)
    await lifecycle.create_paperclip_for_chat(
        creator_user_id="u", owner_id="owner", name="Helper", role="ceo", model=None
    )
    assert bodies[0]["permissions"] == {"canCreateAgents": False}


@pytest.mark.asyncio
async def test_marketplace_create_sends_can_create_agents_false(monkeypatch):
    bodies = _wire_real_create(monkeypatch)
    await lifecycle.create_paperclip_for_marketplace(
        buyer_user_id="u", owner_id="owner", name="Hire", role=None, idempotency_key="k"
    )
    assert bodies[0]["permissions"] == {"canCreateAgents": False}
