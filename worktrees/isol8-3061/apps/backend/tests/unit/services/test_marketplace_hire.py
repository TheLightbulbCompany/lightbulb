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
    "seller_id": "seller_1",
    "slug": "pulse",
    "name": "Pulse",
    "status": "published",
    "version": 9,
    "manifest_json": {"file_count": 12},
}


@pytest.fixture(autouse=True)
def _stub_listing_install_counter():
    """Keep the listing-row install_count bump off the network — the repo
    builds its own boto3 handle. Tests asserting the bump request this
    fixture by name."""
    with patch(
        "core.repositories.marketplace_repo.increment_listing_install_count",
        new_callable=AsyncMock,
    ) as m:
        yield m


@pytest.fixture(autouse=True)
def approval_install_state():
    """Most approval-creation tests model a new, not-yet-installed agent."""
    with patch(
        "core.services.marketplace_hire._has_committed_marketplace_install",
        new_callable=AsyncMock,
        return_value=False,
    ) as m:
        yield m


@pytest.fixture(autouse=True)
def _stub_marketplace_marker():
    with patch("core.services.agent_provenance.write_marketplace_provenance", new_callable=AsyncMock):
        yield


@pytest.fixture(autouse=True)
def approval_create_claim(monkeypatch):
    """Give approval pre-creation a deterministic, held short-lived claim.

    ``create_hire_approval`` must fail closed when it cannot prove the claim,
    while install tests exercise the repository behavior separately. Keeping the
    ordinary approval tests off DynamoDB makes their Paperclip assertions
    deterministic and lets individual regressions replace either outcome.
    """

    claim = AsyncMock(return_value=("approval_claim", None))
    release = AsyncMock()
    monkeypatch.setattr("core.repositories.container_repo.try_claim_owner_deploy", claim)
    monkeypatch.setattr("core.repositories.container_repo.release_owner_deploy_claim", release)
    return claim, release


def _approval_for(agent_id=_AGENT_ID, listing_id=_LISTING["listing_id"]):
    return {
        "id": "ap_1",
        "payload": {
            "agentId": agent_id,
            "metadata": {LISTING_ID_KEY: listing_id, LISTING_SLUG_KEY: "pulse"},
        },
    }


#: ``deploy_for_approval``'s readiness check reads the owner's containers row directly. It used
#: to resolve a running ECS service and thread the row into ``deploy_from_artifact`` as
#: ``container=``; the install reads the row itself now, and there is no service to resolve. The
#: device-pairing doubles that stood beside this went with the pairing step -- a Hermes agent's
#: "pairing" IS its S3 profile, written by the install.
_PROVISIONED_ROW = {"owner_id": "user_x", "agentcore_runtime_arn": "arn:x"}


@pytest.fixture(autouse=True)
def provisioned_row(monkeypatch):
    """Default every test to a provisioned workspace; the unprovisioned test overrides it."""
    row = AsyncMock(return_value=_PROVISIONED_ROW)
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", row)
    return row


@pytest.mark.asyncio
@patch("core.services.catalog_service.emit_agent_deployed")
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_a_rolled_back_approval_install_is_not_counted_as_a_deployment(
    get_listing, get_catalog, fetch, rollback, emit
):
    """A hire the rollback undid is not a deployment. The funnel event belongs
    after the install commits, not inside ``deploy_from_artifact``."""
    get_listing.return_value = _PUBLISHED
    fetch.return_value = (b"tar", {})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("efs exploded"))
    get_catalog.return_value = catalog

    with pytest.raises(RuntimeError, match="efs exploded"):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    emit.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.catalog_service.emit_agent_deployed")
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_a_committed_approval_install_is_counted_as_a_deployment(
    get_listing, get_catalog, fetch, emit, monkeypatch
):
    get_listing.return_value = _PUBLISHED
    fetch.return_value = (b"tar", {"agent": {"name": "Pulse"}})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": _AGENT_ID})
    get_catalog.return_value = catalog

    await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    emit.assert_called_once()
    assert emit.call_args.kwargs["agent_id"] == _AGENT_ID
    assert emit.call_args.kwargs["owner_id"] == "user_x"


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_install_failure_removes_the_agent_and_reraises(get_listing, get_catalog, fetch, rollback):
    """Never leave a roster entry with no workspace behind it, and never let the
    approving user believe the hire completed."""
    get_listing.return_value = _PUBLISHED
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
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_listing_retired_between_ask_and_decision_rolls_back(get_listing, rollback):
    """A listing can be retired while the approval sits in the inbox."""
    get_listing.return_value = {**_PUBLISHED, "status": "retired"}

    with pytest.raises(RuntimeError, match="no longer published"):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    rollback.assert_awaited_once()


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_an_unprovisioned_workspace_rolls_back_rather_than_half_installing(
    get_listing, rollback, provisioned_row
):
    """Approving before the workspace exists must not leave a roster entry the install never
    reached. (This used to be the COLD-container case -- an idle ECS service scaled to zero;
    a parked microVM is addressable, so what is left to refuse is "no row at all".)"""
    get_listing.return_value = _PUBLISHED
    provisioned_row.return_value = None

    with pytest.raises(RuntimeError, match="no workspace yet"):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    rollback.assert_awaited_once()


@pytest.mark.asyncio
@patch("core.services.paperclip_admin_session.get_admin_session_cookie", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_ask_parks_the_agent_inert_then_files_the_approval(
    resolve_company, create_agent, cookie, approval_create_claim, monkeypatch
):
    """Order is load-bearing: the agent must be pending_approval BEFORE the
    approval exists, so it can never be assigned work or woken while the owner
    is still deciding."""
    resolve_company.return_value = "co_1"
    create_agent.return_value = {"id": _AGENT_ID}
    cookie.return_value = "ck"

    calls = []
    admin = MagicMock()
    admin.list_approvals = AsyncMock(return_value=[])
    admin.update_agent = AsyncMock(side_effect=lambda **kw: calls.append(("patch", kw)))

    async def _create_approval(**kw):
        calls.append(("approval", kw))
        return {"id": "ap_9"}

    admin.create_approval = AsyncMock(side_effect=_create_approval)
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"plan": "standard"}),
    )

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
    assert "idempotency_replay_only" not in create_agent.await_args.kwargs


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.paperclip_admin_session.get_admin_session_cookie", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_a_failed_ask_leaves_no_orphan_in_the_roster(
    resolve_company, create_agent, cookie, rollback, approval_create_claim
):
    resolve_company.return_value = "co_1"
    create_agent.return_value = {"id": _AGENT_ID}
    cookie.return_value = "ck"
    admin = MagicMock()
    admin.list_approvals = AsyncMock(return_value=[])
    admin.update_agent = AsyncMock()
    admin.create_approval = AsyncMock(side_effect=RuntimeError("paperclip down"))

    with patch("core.services.paperclip_admin_client.PaperclipAdminClient", return_value=admin):
        with pytest.raises(RuntimeError, match="paperclip down"):
            await marketplace_hire.create_hire_approval(owner_id="user_x", issue_id="iss_1", listing=_LISTING)

    rollback.assert_awaited_once()
    assert rollback.await_args.kwargs["agent_id"] == _AGENT_ID


@pytest.mark.asyncio
@patch("core.services.paperclip_admin_session.get_admin_session_cookie", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_retry_reuses_the_existing_hire_approval(resolve_company, create_agent, cookie, approval_create_claim):
    """A lost response must not create a second approval for the replayed agent."""

    resolve_company.return_value = "co_1"
    create_agent.return_value = {"id": _AGENT_ID}
    cookie.return_value = "ck"
    admin = MagicMock()
    admin.list_approvals = AsyncMock(
        return_value=[
            {
                "id": "ap_existing",
                "type": "hire_agent",
                "status": "pending",
                "payload": build_hire_approval_payload(listing=_LISTING, agent_id=_AGENT_ID),
            }
        ]
    )
    admin.update_agent = AsyncMock()
    admin.create_approval = AsyncMock()

    with patch("core.services.paperclip_admin_client.PaperclipAdminClient", return_value=admin):
        approval_id = await marketplace_hire.create_hire_approval(
            owner_id="user_x",
            issue_id="iss_1",
            listing=_LISTING,
        )

    assert approval_id == "ap_existing"
    admin.update_agent.assert_not_awaited()
    admin.create_approval.assert_not_awaited()
    assert create_agent.await_args.kwargs["idempotency_key"].startswith(
        "lightbulb-agent-create:v1:marketplace-approval:"
    )


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.paperclip_admin_session.get_admin_session_cookie", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_retry_after_approved_install_does_not_repark_or_refile(
    resolve_company,
    create_agent,
    cookie,
    rollback,
    approval_create_claim,
    approval_install_state,
):
    """Pending-only approval reads must not disable an already-live hire."""

    resolve_company.return_value = "co_1"
    create_agent.return_value = {"id": _AGENT_ID}
    cookie.return_value = "ck"
    approval_install_state.return_value = True
    admin = MagicMock()
    admin.list_approvals = AsyncMock(return_value=[])
    admin.update_agent = AsyncMock()
    admin.create_approval = AsyncMock()

    with patch("core.services.paperclip_admin_client.PaperclipAdminClient", return_value=admin):
        approval_id = await marketplace_hire.create_hire_approval(
            owner_id="user_x",
            issue_id="iss_1",
            listing=_LISTING,
        )

    assert approval_id is None
    approval_install_state.assert_called_once_with(owner_id="user_x", agent_id=_AGENT_ID)
    admin.update_agent.assert_not_awaited()
    admin.create_approval.assert_not_awaited()
    rollback.assert_not_awaited()


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.paperclip_admin_session.get_admin_session_cookie", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_invalid_approval_lookup_response_preserves_replayed_agent(
    resolve_company,
    create_agent,
    cookie,
    rollback,
    approval_create_claim,
):
    """Unknown approval state is not proof that the replayed UUID is disposable."""

    resolve_company.return_value = "co_1"
    create_agent.return_value = {"id": _AGENT_ID}
    cookie.return_value = "ck"
    admin = MagicMock()
    admin.list_approvals = AsyncMock(return_value=None)

    with patch("core.services.paperclip_admin_client.PaperclipAdminClient", return_value=admin):
        with pytest.raises(RuntimeError, match="invalid approvals response"):
            await marketplace_hire.create_hire_approval(
                owner_id="user_x",
                issue_id="iss_1",
                listing=_LISTING,
            )

    rollback.assert_not_awaited()


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.paperclip_admin_session.get_admin_session_cookie", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_ambiguous_approval_lookup_failure_preserves_agent_for_retry(
    resolve_company,
    create_agent,
    cookie,
    rollback,
    approval_create_claim,
):
    """A timeout may hide a prior approval, so deleting its agent is unsafe."""

    import httpx

    resolve_company.return_value = "co_1"
    create_agent.return_value = {"id": _AGENT_ID}
    cookie.return_value = "ck"
    admin = MagicMock()
    admin.list_approvals = AsyncMock(side_effect=httpx.ReadTimeout("lost response"))

    with patch("core.services.paperclip_admin_client.PaperclipAdminClient", return_value=admin):
        with pytest.raises(httpx.ReadTimeout):
            await marketplace_hire.create_hire_approval(
                owner_id="user_x",
                issue_id="iss_1",
                listing=_LISTING,
            )

    rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_precreate_claim_refusal_does_not_mint_or_file(monkeypatch):
    """A held owner claim closes the list-then-create approval race."""

    claim = AsyncMock(return_value=(None, "approval:other"))
    release = AsyncMock()
    mint = AsyncMock()
    monkeypatch.setattr("core.repositories.container_repo.try_claim_owner_deploy", claim)
    monkeypatch.setattr("core.repositories.container_repo.release_owner_deploy_claim", release)
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
        AsyncMock(return_value="company-1"),
    )
    monkeypatch.setattr("core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace", mint)

    assert await marketplace_hire.create_hire_approval(owner_id="owner", issue_id="issue-1", listing=_LISTING) is None
    mint.assert_not_awaited()
    release.assert_not_awaited()
    assert claim.await_args.kwargs["fail_open"] is False


@pytest.mark.asyncio
async def test_approval_precreate_claim_fault_degrades_without_minting(monkeypatch):
    """An unprovable fail-closed claim is not permission to create an approval."""

    claim = AsyncMock(return_value=(None, None))
    release = AsyncMock()
    mint = AsyncMock()
    monkeypatch.setattr("core.repositories.container_repo.try_claim_owner_deploy", claim)
    monkeypatch.setattr("core.repositories.container_repo.release_owner_deploy_claim", release)
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
        AsyncMock(return_value="company-1"),
    )
    monkeypatch.setattr("core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace", mint)

    assert await marketplace_hire.create_hire_approval(owner_id="owner", issue_id="issue-1", listing=_LISTING) is None
    mint.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_hire_approval_recommendations_file_exactly_one_approval(monkeypatch):
    """A real overlap must lose at the durable claim before list-then-create."""

    import asyncio

    first_claimed = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    held = False

    async def _claim(*_args, **_kwargs):
        nonlocal held
        if held:
            return None, "approval:issue-1:listing"
        held = True
        first_claimed.set()
        return "approval_claim", None

    async def _release(*_args, **_kwargs):
        nonlocal held
        held = False

    async def _create_approval(**_kwargs):
        await allow_first_to_finish.wait()
        return {"id": "approval-1"}

    claim = AsyncMock(side_effect=_claim)
    release = AsyncMock(side_effect=_release)
    mint = AsyncMock(return_value={"id": _AGENT_ID})
    admin = MagicMock()
    admin.list_approvals = AsyncMock(return_value=[])
    admin.update_agent = AsyncMock()
    admin.create_approval = AsyncMock(side_effect=_create_approval)

    monkeypatch.setattr("core.repositories.container_repo.try_claim_owner_deploy", claim)
    monkeypatch.setattr("core.repositories.container_repo.release_owner_deploy_claim", release)
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
        AsyncMock(return_value="company-1"),
    )
    monkeypatch.setattr("core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace", mint)
    monkeypatch.setattr(
        "core.services.paperclip_admin_session.get_admin_session_cookie", AsyncMock(return_value="cookie")
    )

    with patch("core.services.paperclip_admin_client.PaperclipAdminClient", return_value=admin):
        first = asyncio.create_task(
            marketplace_hire.create_hire_approval(owner_id="owner", issue_id="issue-1", listing=_LISTING)
        )
        await first_claimed.wait()
        second = await marketplace_hire.create_hire_approval(owner_id="owner", issue_id="issue-1", listing=_LISTING)
        allow_first_to_finish.set()
        assert await first == "approval-1"

    assert second is None
    admin.create_approval.assert_awaited_once()
    assert mint.await_count == 1
    assert release.await_count == 1


@pytest.mark.asyncio
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_owner_with_no_paperclip_company_degrades_instead_of_raising(resolve_company):
    """A recommendation must still return its listings for a degraded owner, so
    a missing Paperclip company returns None rather than raising."""
    resolve_company.return_value = None
    assert await marketplace_hire.create_hire_approval(owner_id="user_x", issue_id="iss_1", listing=_LISTING) is None


# ---------------------------------------------------------------------------
# Paid listings on the approval path. The buyer route 403s an unpurchased
# paid listing; the approval path is the SAME install with a different door,
# so it must refuse at both ends — filing the ask, and (defense in depth)
# installing on approve.
# ---------------------------------------------------------------------------

_PAID = {**_PUBLISHED, "price_cents": 1500, "seller_id": "seller_1"}


@pytest.mark.asyncio
@patch("core.repositories.marketplace_repo.has_unrevoked_purchase", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_approval_install_refuses_an_unpurchased_paid_listing(get_listing, get_catalog, rollback, has_purchase):
    """A paid listing must not install free through the Teams approval door."""
    get_listing.return_value = _PAID
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock()
    get_catalog.return_value = catalog
    has_purchase.return_value = False

    with pytest.raises(RuntimeError, match="purchase required"):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    catalog.deploy_from_artifact.assert_not_awaited()
    rollback.assert_awaited_once()
    assert has_purchase.await_args.kwargs == {"buyer_id": "user_x", "listing_id": _LISTING["listing_id"]}


@pytest.mark.asyncio
@patch("core.repositories.marketplace_repo.has_unrevoked_purchase", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_approval_install_passes_for_an_entitled_paid_listing(
    get_listing, get_catalog, fetch, has_purchase, monkeypatch
):
    get_listing.return_value = _PAID
    fetch.return_value = (b"tar", {"agent": {"name": "Pulse"}})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": _AGENT_ID})
    get_catalog.return_value = catalog
    has_purchase.return_value = True

    await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    catalog.deploy_from_artifact.assert_awaited_once()


@pytest.mark.asyncio
@patch("core.repositories.marketplace_repo.has_unrevoked_purchase", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_approval_install_exempts_the_seller_from_the_purchase_check(
    get_listing, get_catalog, fetch, has_purchase, monkeypatch
):
    """Sellers hold no purchase row for their own listings — same bypass as the
    buyer route."""
    get_listing.return_value = {**_PAID, "seller_id": "user_x"}
    fetch.return_value = (b"tar", {"agent": {"name": "Pulse"}})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": _AGENT_ID})
    get_catalog.return_value = catalog

    await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    has_purchase.assert_not_awaited()
    catalog.deploy_from_artifact.assert_awaited_once()


@pytest.mark.asyncio
@patch("core.repositories.marketplace_repo.has_unrevoked_purchase", new_callable=AsyncMock)
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
async def test_ask_is_not_filed_for_an_unpurchased_paid_listing(create_agent, get_listing, has_purchase):
    """Filing the ask ARMS the post-approve install, so the refusal has to
    happen here too — not only at install time. Degrades to None (listings are
    still worth returning) and never pre-creates an agent."""
    get_listing.return_value = _PAID
    has_purchase.return_value = False

    result = await marketplace_hire.create_hire_approval(
        owner_id="user_x", issue_id="iss_1", listing={**_LISTING, "price_cents": 1500}
    )

    assert result is None
    create_agent.assert_not_awaited()
    assert has_purchase.await_args.kwargs == {"buyer_id": "user_x", "listing_id": _LISTING["listing_id"]}


@pytest.mark.asyncio
@patch("core.repositories.marketplace_repo.has_unrevoked_purchase", new_callable=AsyncMock)
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
@patch("core.services.paperclip_admin_session.get_admin_session_cookie", new_callable=AsyncMock)
@patch(
    "core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace",
    new_callable=AsyncMock,
)
@patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", new_callable=AsyncMock)
async def test_ask_is_filed_for_an_entitled_paid_listing(
    resolve_company, create_agent, cookie, get_listing, has_purchase, approval_create_claim
):
    resolve_company.return_value = "co_1"
    create_agent.return_value = {"id": _AGENT_ID}
    cookie.return_value = "ck"
    get_listing.return_value = _PAID
    has_purchase.return_value = True
    admin = MagicMock()
    admin.list_approvals = AsyncMock(return_value=[])
    admin.update_agent = AsyncMock()
    admin.create_approval = AsyncMock(return_value={"id": "ap_9"})

    with patch("core.services.paperclip_admin_client.PaperclipAdminClient", return_value=admin):
        approval_id = await marketplace_hire.create_hire_approval(
            owner_id="user_x", issue_id="iss_1", listing={**_LISTING, "price_cents": 1500}
        )

    assert approval_id == "ap_9"


@pytest.mark.asyncio
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_approval_install_bumps_listing_install_count(
    get_listing, get_catalog, fetch, monkeypatch, _stub_listing_install_counter
):
    get_listing.return_value = _PUBLISHED
    fetch.return_value = (b"tar", {"agent": {"name": "Pulse"}})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": _AGENT_ID})
    get_catalog.return_value = catalog

    await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    _stub_listing_install_counter.assert_awaited_once_with(listing_id=_LISTING["listing_id"], version=9)


# ---------------------------------------------------------------------------
# Owner-wide deploy claim — this IS a marketplace install and must serialize
# against every other one. The Teams "Needs Attention" band lists several
# approvals at once, so approving three in a row otherwise reproduces the
# 2026-08-06 incident (three concurrent installs on one container, two 500s).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("core.repositories.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("core.repositories.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_approval_install_takes_and_releases_the_owner_deploy_claim(
    get_listing, get_catalog, fetch, claim, release, monkeypatch
):
    get_listing.return_value = _PUBLISHED
    fetch.return_value = (b"tar", {"agent": {"name": "Pulse"}})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": _AGENT_ID})
    get_catalog.return_value = catalog
    claim.return_value = ("tok_1", None)

    await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    assert claim.await_args.kwargs["listing_id"] == _LISTING["listing_id"]
    # Released with the fencing token, and only AFTER the device pairing — that
    # talks to the same container.
    assert release.await_args.kwargs["token"] == "tok_1"


@pytest.mark.asyncio
@patch("core.repositories.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("core.repositories.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_second_concurrent_approval_is_refused_fast_and_rolls_the_agent_back(
    get_listing, get_catalog, rollback, claim, release
):
    """A refusal is immediate — never a server-side wait — and leaves no agent
    that can never be installed.

    The approval is already SPENT when this runs: the route flips the agent
    ``pending_approval`` -> ``idle`` via Paperclip BEFORE calling here. So an
    agent left standing on refusal is stranded — no route will ever install its
    workspace, and the drawer re-hire mints a second agent beside it. Roll it
    back instead; nothing is installed yet, so the rollback carries no ``cause``
    and is a no-op on the container side.
    """
    get_listing.return_value = _PUBLISHED
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock()
    get_catalog.return_value = catalog
    claim.return_value = (None, "lst_other")

    with pytest.raises(RuntimeError, match="already running"):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    catalog.deploy_from_artifact.assert_not_awaited()
    rollback.assert_awaited_once()
    assert rollback.await_args.kwargs == {"owner_id": "user_x", "agent_id": _AGENT_ID}
    release.assert_not_awaited()  # nothing to release — we never held it


@pytest.mark.asyncio
@patch("core.repositories.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("core.repositories.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_claim_is_released_when_the_install_fails(get_listing, get_catalog, fetch, _rollback, claim, release):
    """A leaked claim would wedge every hire this owner tries for the whole lease."""
    get_listing.return_value = _PUBLISHED
    fetch.return_value = (b"tar", {})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("efs exploded"))
    get_catalog.return_value = catalog
    claim.return_value = ("tok_1", None)

    with pytest.raises(RuntimeError, match="efs exploded"):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    release.assert_awaited_once()
    assert release.await_args.kwargs["token"] == "tok_1"


@pytest.mark.asyncio
@patch("core.repositories.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("core.repositories.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._fetch_artifact")
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_a_timed_out_install_hands_the_timeout_to_the_claim_release(
    get_listing, get_catalog, fetch, _rollback, claim, release
):
    """The timeout must reach the release, which HOLDS the claim on it.

    Releasing here would undo the whole point of the unwind's timeout
    carve-out: the container may still be writing the install, and a freed
    claim lets the next approval start a second one on top of it.
    """
    get_listing.return_value = _PUBLISHED
    fetch.return_value = (b"tar", {})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=TimeoutError("pair RPC"))
    get_catalog.return_value = catalog
    claim.return_value = ("tok_1", None)

    with pytest.raises(TimeoutError):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    assert isinstance(release.await_args.kwargs["cause"], TimeoutError)


@pytest.mark.asyncio
@patch("core.repositories.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("core.repositories.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch("core.services.marketplace_hire._rollback_agent", new_callable=AsyncMock)
@patch("core.services.catalog_service.get_catalog_service")
@patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock)
async def test_a_raising_claim_does_not_strand_the_approved_agent(get_listing, get_catalog, rollback, claim, release):
    """The approval is already SPENT when this runs, so an exception that escapes
    before the rollback envelope leaves an idle agent with no workspace and no
    route left to install one. ``try_claim_owner_deploy`` now fails open on any
    fault, but this call site must not depend on that to stay coherent.
    """
    from botocore.exceptions import EndpointConnectionError

    get_listing.return_value = _PUBLISHED
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock()
    get_catalog.return_value = catalog
    claim.side_effect = EndpointConnectionError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")

    with pytest.raises(EndpointConnectionError):
        await marketplace_hire.deploy_for_approval(approval=_approval_for(), owner_id="user_x")

    catalog.deploy_from_artifact.assert_not_awaited()
    rollback.assert_awaited_once()
    assert rollback.await_args.kwargs == {"owner_id": "user_x", "agent_id": _AGENT_ID}
    release.assert_not_awaited()  # never held it


@pytest.mark.asyncio
async def test_malformed_or_wrong_agent_marker_does_not_claim_completion(monkeypatch):
    from core.services import marketplace_hire

    monkeypatch.setattr(
        "core.services.agent_provenance._load_agent_sidecars",
        AsyncMock(
            return_value={
                _AGENT_ID: {
                    "agent_id": "different-agent",
                    "listing_id": "listing_1",
                    "template_slug": "pulse",
                    "template_version": 1,
                    "seller_id": "seller_1",
                }
            }
        ),
    )

    assert not await marketplace_hire._has_committed_marketplace_install(owner_id="user_x", agent_id=_AGENT_ID)
