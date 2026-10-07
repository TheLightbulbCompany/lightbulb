from unittest.mock import AsyncMock, MagicMock, call

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle
from core.services.paperclip_adapter_config import AdapterConfigError


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

    with pytest.raises(RuntimeError, match="final root CEO"):
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


# ---------------------------------------------------------------------------
# M6 task-15 item 1 — repoint_agent_adapter (task-14 row 1, the founder blocker)
# ---------------------------------------------------------------------------


def _wire_repoint(monkeypatch, *, hook_token="hook-tok"):
    """Paperclip admin behind a real async context manager (``repoint_agent_adapter`` uses
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


@pytest.mark.asyncio
async def test_repoint_moves_an_existing_agent_onto_the_native_adapter(monkeypatch):
    """The whole point: an agent CREATED on the old runtime keeps ``openclaw_gateway`` forever,
    and nothing re-picks the adapter, so a migrated workspace dials an operator WebSocket at an
    address that no longer resolves -- for every routine fire, issue run and Teams Run/Retry,
    while room chat (which rides the WORK bus, not the adapter) keeps working. Nothing looks
    broken."""
    admin = _wire_repoint(monkeypatch)
    agent = {"id": "agent-1", "name": "Ada", "adapterType": "openclaw_gateway"}

    assert await lifecycle.repoint_agent_adapter("org_1", agent) is True

    body = admin.patch_agent.await_args.kwargs["body"]
    assert body["adapterType"] == lifecycle.HERMES_GATEWAY_TYPE
    # replaceAdapterConfig, not a merge: the old config carries a device private key and a
    # relay URL, and a merged result would keep both alongside the new fields.
    assert body["replaceAdapterConfig"] is True
    assert body["adapterConfig"]["apiBaseUrl"].endswith("/api/v1/internal/paperclip/hermes/org_1")
    assert body["adapterConfig"]["apiKey"] == "hook-tok"
    assert body["adapterConfig"]["sessionKeyStrategy"] == "issue"


@pytest.mark.asyncio
async def test_repoint_is_idempotent_on_an_agent_already_on_the_target(monkeypatch):
    """It is the only repair path for a stray `openclaw_gateway` row, so it has to be safe to run
    across a whole roster: an agent already on the native adapter is left alone."""
    admin = _wire_repoint(monkeypatch)
    agent = {"id": "agent-1", "adapterType": lifecycle.HERMES_GATEWAY_TYPE}

    assert await lifecycle.repoint_agent_adapter("org_1", agent) is False

    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_repoint_refuses_to_write_a_native_adapter_with_no_hook_token(monkeypatch):
    """An empty token mints an adapter whose every run 401s at the facade -- and Paperclip
    reports that per RUN (``hermes_gateway_auth_failed``), not as a broken agent, so a
    half-re-pointed workspace looks healthy. The guard lives in the shared builder
    (``AdapterConfigError``), so the seed and ``/teams`` create refuse it identically rather than
    this door alone having a private check."""
    admin = _wire_repoint(monkeypatch, hook_token="")
    agent = {"id": "agent-1", "adapterType": "openclaw_gateway"}

    with pytest.raises(AdapterConfigError):
        await lifecycle.repoint_agent_adapter("org_1", agent)

    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_repoint_raises_rather_than_reporting_a_partial_success(monkeypatch):
    """A half-re-pointed workspace looks healthy, which is the entire defect this closes -- so a
    failed PATCH must raise, and the same call must converge on a retry."""
    admin = _wire_repoint(monkeypatch)
    admin.patch_agent.side_effect = [RuntimeError("paperclip 503"), {}]
    agent = {"id": "agent-1", "adapterType": "openclaw_gateway"}

    with pytest.raises(RuntimeError, match="paperclip 503"):
        await lifecycle.repoint_agent_adapter("org_1", agent)

    assert await lifecycle.repoint_agent_adapter("org_1", agent) is True
    assert admin.patch_agent.await_count == 2


@pytest.mark.asyncio
async def test_repoint_refuses_an_agent_row_with_no_id(monkeypatch):
    admin = _wire_repoint(monkeypatch)

    with pytest.raises(ValueError, match="no id"):
        await lifecycle.repoint_agent_adapter("org_1", {"adapterType": "openclaw_gateway"})

    admin.patch_agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# sync_agent_adapter_tokens -- a re-created containers row mints a new hook_token, but the
# surviving company's hermes_gateway agents still carry the old one as their apiKey
# ---------------------------------------------------------------------------


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

    assert await lifecycle.sync_agent_adapter_tokens("org_1") == 2

    patched = {c.kwargs["agent_id"]: c.kwargs["body"] for c in admin.patch_agent.await_args_list}
    assert set(patched) == {"a1", "a2"}  # openclaw_gateway is repoint_agent_adapter's job
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

    await lifecycle.sync_agent_adapter_tokens("org_1")

    assert reads == [True]
    assert admin.patch_agent.await_args.kwargs["body"]["adapterConfig"]["apiKey"] == "fresh-tok"


@pytest.mark.asyncio
async def test_sync_is_a_noop_without_a_company(monkeypatch):
    admin = _wire_sync(monkeypatch, agents=[], company_id=None)

    assert await lifecycle.sync_agent_adapter_tokens("org_1") == 0

    admin.list_agents.assert_not_awaited()
    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_raises_on_a_failed_patch(monkeypatch):
    admin = _wire_sync(monkeypatch, agents=[{"id": "a1", "adapterType": lifecycle.HERMES_GATEWAY_TYPE}])
    admin.patch_agent.side_effect = RuntimeError("paperclip 503")

    with pytest.raises(RuntimeError, match="paperclip 503"):
        await lifecycle.sync_agent_adapter_tokens("org_1")
