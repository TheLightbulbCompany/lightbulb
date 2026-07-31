"""Marketplace hires routed through Paperclip's approval surface.

A headless Friday cannot hire; she can only ask. The ask becomes a Paperclip
``hire_agent`` approval over an agent Isol8 pre-created and parked inert, linked
to the issue that prompted it. The roster moves only on approve.

Separate from — and does not disturb — the direct-create flow that seeds
Friday/Atlas and lands marketplace deploys.
"""

import pytest

from core.services import marketplace_hire
from core.services.marketplace_hire import (
    LISTING_ID_KEY,
    LISTING_SLUG_KEY,
    build_hire_approval_payload,
    hired_agent_id_of,
    listing_id_of,
    listing_slug_of,
)

_AGENT_ID = "3f1c9a20-0000-4000-8000-000000000001"
_LISTING = {
    "listing_id": "81ab74f8-efc2-464c-8c62-706274b69639",
    "slug": "pulse",
    "name": "Pulse",
    "summary": "Marketing intel and content research.",
    "tags": ["marketing", "content"],
    "price_cents": 0,
}


def _payload(**over):
    listing = {**_LISTING, **over.pop("listing", {})}
    return build_hire_approval_payload(listing=listing, agent_id=over.pop("agent_id", _AGENT_ID))


def test_payload_carries_agent_id():
    """The load-bearing field. Paperclip's approve RESPONSE never returns the id
    of an agent it created, so an approval without agentId would leave the
    install step unable to say which agent to give a workspace to. With it,
    approve runs activatePendingApproval and reject runs terminate."""
    assert _payload()["agentId"] == _AGENT_ID


def test_payload_omits_the_adapter_config():
    """Read only on the create branch, which agentId bypasses — and it holds a
    private key that should not get a second at-rest copy."""
    payload = _payload()
    assert "adapterConfig" not in payload
    assert "adapterType" not in payload


def test_payload_carries_listing_id_for_the_install():
    """Slugs are non-unique; the install resolves the artifact by listing_id."""
    assert _payload()["metadata"][LISTING_ID_KEY] == _LISTING["listing_id"]


def test_payload_names_and_describes_the_agent_from_the_listing():
    payload = _payload()
    assert payload["name"] == "Pulse"
    assert payload["title"] == "Marketing intel and content research."
    assert payload["capabilities"] == "marketing, content"


def test_payload_title_is_bounded():
    assert len(_payload(listing={"summary": "x" * 400})["title"]) <= 120


def test_payload_tolerates_a_listing_with_no_tags():
    payload = build_hire_approval_payload(listing={"slug": "x", "name": "X"}, agent_id=_AGENT_ID)
    assert payload["capabilities"] == ""
    assert payload["title"] == ""


def test_accessors_find_a_marketplace_hire():
    payload = _payload()
    assert listing_id_of(payload) == _LISTING["listing_id"]
    assert listing_slug_of(payload) == "pulse"
    assert hired_agent_id_of(payload) == _AGENT_ID


def test_accessors_return_none_for_a_non_marketplace_approval():
    """Every other approval type must flow through the approve handler
    untouched — the install is a no-op for them."""
    for payload in ({}, {"metadata": {}}, {"metadata": {"something_else": "x"}}, None):
        assert listing_id_of(payload) is None
        assert listing_slug_of(payload) is None
    assert hired_agent_id_of({"agentId": ""}) is None
    assert hired_agent_id_of({"agentId": 42}) is None


@pytest.mark.asyncio
async def test_deploy_for_approval_is_a_noop_for_other_approval_types():
    """A budget-override or CEO-strategy approval must not touch the container."""
    await marketplace_hire.deploy_for_approval(
        approval={"id": "a1", "payload": {"agentId": _AGENT_ID}}, owner_id="user_x"
    )


@pytest.mark.asyncio
async def test_deploy_for_approval_logs_loudly_on_a_mangled_marketplace_payload(caplog):
    """A hire recognisable as ours but missing what the install needs must NOT
    pass as a silent no-op — the owner would see "approved" and get an agent
    with no workspace. That silent-failure shape has bitten this codebase
    before."""
    await marketplace_hire.deploy_for_approval(
        approval={"id": "a1", "payload": {"metadata": {LISTING_SLUG_KEY: "pulse"}}},
        owner_id="user_x",
    )
    assert "NOT installed" in caplog.text


# ---------------------------------------------------------------------------
# The approve -> install round trip.
#
# This path mutates the roster and the container, so "needs a live click" is
# not adequate coverage. Everything below drives deploy_for_approval and
# create_hire_approval against doubles for Paperclip / ECS / catalog / S3.
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

_PUBLISHED = {
    "listing_id": _LISTING["listing_id"],
    "slug": "pulse",
    "name": "Pulse",
    "status": "published",
    "version": 9,
    "manifest_json": {"file_count": 12},
}


def _approval_for(agent_id=_AGENT_ID, listing_id=_LISTING["listing_id"]):
    return {
        "id": "ap_1",
        "payload": {
            "agentId": agent_id,
            "metadata": {LISTING_ID_KEY: listing_id, LISTING_SLUG_KEY: "pulse"},
        },
    }


def _ecs_double(running=True):
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(
        return_value=({"gateway_token": "gwt"}, "10.0.0.5") if running else (None, None)
    )
    return ecs


@pytest.mark.asyncio
@patch("core.services.paperclip_openclaw_pairing.ensure_operator_device_paired", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.containers.get_ecs_manager")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_install_targets_the_preapproved_agent_and_pairs_its_device(
    get_listing, get_ecs, get_catalog, fetch, pair
):
    """The whole point of payload.agentId: the workspace lands on the agent the
    approval governs, and the operator device is paired — without which the
    agent is rejected NOT_PAIRED on its first relayed connect."""
    get_listing.return_value = _PUBLISHED
    get_ecs.return_value = _ecs_double()
    fetch.return_value = (b"tar", {"agent": {"name": "Pulse"}})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": _AGENT_ID})
    get_catalog.return_value = catalog

    await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    kwargs = catalog.deploy_from_artifact.await_args.kwargs
    assert kwargs["new_agent_id"] == _AGENT_ID
    assert kwargs["owner_id"] == "user_x"
    assert kwargs["container_ip"] == "10.0.0.5"
    assert kwargs["container_token"] == "gwt"
    assert kwargs["tar_bytes"] == b"tar"
    # Manifest comes from the listing ROW, mirroring deploy_listing.
    assert kwargs["manifest"]["version"] == 9
    pair.assert_awaited_once()


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.containers.get_ecs_manager")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_install_failure_removes_the_agent_and_reraises(get_listing, get_ecs, get_catalog, fetch, rollback):
    """Never leave a roster entry with no workspace behind it, and never let the
    approving user believe the hire completed."""
    get_listing.return_value = _PUBLISHED
    get_ecs.return_value = _ecs_double()
    fetch.return_value = (b"tar", {})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("efs exploded"))
    get_catalog.return_value = catalog

    with pytest.raises(RuntimeError, match="efs exploded"):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    rollback.assert_awaited_once()
    assert rollback.await_args.kwargs["agent_id"] == _AGENT_ID


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.containers.get_ecs_manager")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_listing_retired_between_ask_and_decision_rolls_back(get_listing, get_ecs, rollback):
    """A listing can be retired while the approval sits in the inbox."""
    get_listing.return_value = {**_PUBLISHED, "status": "retired"}

    with pytest.raises(RuntimeError, match="no longer published"):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    rollback.assert_awaited_once()
    get_ecs.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.containers.get_ecs_manager")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_cold_container_rolls_back_rather_than_half_installing(get_listing, get_ecs, rollback):
    """Containers scale to zero when idle; approving while cold must not leave a
    roster entry the workspace never reached."""
    get_listing.return_value = _PUBLISHED
    get_ecs.return_value = _ecs_double(running=False)

    with pytest.raises(RuntimeError, match="container is not running"):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    rollback.assert_awaited_once()


@pytest.mark.asyncio
@patch("core.services.paperclip_admin_session.get_admin_session_cookie", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_ask_parks_the_agent_inert_then_files_the_approval(resolve_company, create_agent, cookie):
    """Order is load-bearing: the agent must be pending_approval BEFORE the
    approval exists, so it can never be assigned work or woken while the owner
    is still deciding."""
    resolve_company.return_value = "co_1"
    create_agent.return_value = {"id": _AGENT_ID}
    cookie.return_value = "ck"

    calls = []
    admin = MagicMock()
    admin.update_agent = AsyncMock(side_effect=lambda **kw: calls.append(("patch", kw)))

    async def _create_approval(**kw):
        calls.append(("approval", kw))
        return {"id": "ap_9"}

    admin.create_approval = AsyncMock(side_effect=_create_approval)

    with patch("core.services.paperclip_admin_client.PaperclipAdminClient", return_value=admin):
        approval_id = await marketplace_hire.create_hire_approval(owner_id="user_x", issue_id="iss_1", listing=_LISTING)

    assert approval_id == "ap_9"
    assert [c[0] for c in calls] == ["patch", "approval"]
    assert calls[0][1]["status"] == "pending_approval"
    assert calls[0][1]["agent_id"] == _AGENT_ID
    body = calls[1][1]["body"]
    assert body["type"] == "hire_agent"
    assert body["payload"]["agentId"] == _AGENT_ID
    assert body["issueIds"] == ["iss_1"]


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.paperclip_admin_session.get_admin_session_cookie", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_a_failed_ask_leaves_no_orphan_in_the_roster(resolve_company, create_agent, cookie, rollback):
    resolve_company.return_value = "co_1"
    create_agent.return_value = {"id": _AGENT_ID}
    cookie.return_value = "ck"
    admin = MagicMock()
    admin.update_agent = AsyncMock()
    admin.create_approval = AsyncMock(side_effect=RuntimeError("paperclip down"))

    with patch("core.services.paperclip_admin_client.PaperclipAdminClient", return_value=admin):
        with pytest.raises(RuntimeError, match="paperclip down"):
            await marketplace_hire.create_hire_approval(owner_id="user_x", issue_id="iss_1", listing=_LISTING)

    rollback.assert_awaited_once()
    assert rollback.await_args.kwargs["agent_id"] == _AGENT_ID


@pytest.mark.asyncio
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_owner_with_no_paperclip_company_degrades_instead_of_raising(resolve_company):
    """A recommendation must still return its listings for a degraded owner, so
    a missing Paperclip company returns None rather than raising."""
    resolve_company.return_value = None
    assert await marketplace_hire.create_hire_approval(owner_id="user_x", issue_id="iss_1", listing=_LISTING) is None
