"""The loop's rules. Every Stripe call is patched at the client module, so no
network and no fixtures beyond dicts shaped like spec.txt's objects."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import provisioning_workflow as mod  # noqa: E402
from core.services.stripe_provisioning import ProvisioningError  # noqa: E402

SERVICE = {
    "object": "v2.provisioning.provider_service",
    "id": "psvc_1",
    # Live 2026-09-30.preview shape (verified 2026-10-29): the provider id is `provider`.
    "provider": "prvdr_1",
    "service_id": "svc_analytics",
    "provider_name": "posthog",
    "description": "Product analytics",
    "configuration_schema": {
        "type": "object",
        "properties": {"region": {"type": "string", "enum": ["us", "eu"]}, "organization_name": {"type": "string"}},
        "required": ["region", "organization_name"],
    },
    "pricing": {"type": "free", "paid_pricing": []},
    "kind": "deployable",
    "scope": "project",
    "availability": "PROVIDER_SERVICE_AVAILABILITY_AVAILABLE",
    "constraints": [],
    "allowed_updates": [],
}
PROVIDER = {
    "id": "prvdr_1",
    "name": "posthog",
    "tos_url": "https://posthog.com/terms",
    "website_url": "https://posthog.com",
    "deep_link_purposes": ["dashboard"],
    "capabilities": [],
}
CONFIG = {"region": "us", "organization_name": "Northwind"}


def _approved(**over):
    """A row past the consent card — `_project_for` reads the scope off it."""
    return _row(approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG), **over)


def _row(**over):
    base = {
        "workflow_id": "pwf_1",
        "owner_id": "org_a",
        "action": "create_resource",
        "state": "ask",
        "provider_id": "prvdr_1",
        "provider_name": "posthog",
        "service_ref": "svc_analytics",
        "service_id": "svc_analytics",
        "configuration": CONFIG,
        "issue_id": "iss_1",
        "attempts": 0,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def account(monkeypatch):
    monkeypatch.setattr(
        mod.billing_repo,
        "get_by_owner_id",
        # The company already has its ONE Provisioning Project.
        AsyncMock(
            return_value={
                "owner_id": "org_a",
                "company_stripe_account_id": "acct_1",
                "company_stripe_source": "created",
                "company_stripe_project_id": "proj_1",
            }
        ),
    )


@pytest.fixture(autouse=True)
def standing(monkeypatch):
    monkeypatch.setattr(mod.billing_repo, "set_apps_standing_approval", AsyncMock())


@pytest.fixture(autouse=True)
def cards(monkeypatch):
    for name in (
        "request_provider_consent",
        "request_provider_connect",
        "request_provider_information",
        "request_payment_setup",
    ):
        # raising=False: three of the four land with Task 6's card payloads.
        # This module only ever reaches them through the module attribute, so
        # the loop is testable before they exist.
        monkeypatch.setattr(mod.provisioning_approvals, name, AsyncMock(return_value="apr_1"), raising=False)
    # Through the module attribute, which is how every write site reaches it.
    monkeypatch.setattr(mod.owner_facts, "write_owner_facts", AsyncMock())


def _no_network(*a, **k):
    raise AssertionError("network")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing here may reach DynamoDB, Stripe or Paperclip — a forgotten patch
    fails loudly at the transport instead of hanging on a socket.

    Above that, the calls made by paths under test without being their subject
    get harmless defaults: the open-workflow lookup, the non-state field
    writes, the payment-profile probe behind the consent card's editable
    spending limit, and the two reconcile LISTs (no known projects, no known
    resources). Tests that assert on any of them patch them."""
    monkeypatch.setattr("core.repositories.billing_repo.get_table", _no_network)
    monkeypatch.setattr("core.repositories.provisioning_workflow_repo.get_table", _no_network)
    monkeypatch.setattr("core.repositories.apps_repo.get_table", _no_network)
    monkeypatch.setattr("core.repositories.messaging_repo._table", _no_network)
    # `request`, not `_raw`: `request` turns a raw failure into a TRANSIENT
    # error, which a poll would happily swallow as backoff.
    monkeypatch.setattr(mod.api, "request", _no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_network)
    monkeypatch.setattr(mod.repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod.repo, "patch", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.api, "payment_profile", AsyncMock(side_effect=ProvisioningError("not_found", "no profile")))
    monkeypatch.setattr(mod.work_products, "register", AsyncMock(return_value="wp_1"))


@pytest.fixture(autouse=True)
def catalog_unchanged(monkeypatch):
    """The re-read before the resource write finds the approved entry unless a test says it moved."""
    monkeypatch.setattr(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE]))
    monkeypatch.setattr(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER]))


@pytest.fixture(autouse=True)
def projects_ready(monkeypatch):
    """The account's `projects` capability is active unless a test says not."""
    ready = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.company_stripe.projects_ready", ready)
    return ready


# --- start ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_refuses_until_the_projects_capability_is_active(projects_ready):
    projects_ready.return_value = False
    with patch.object(mod.api, "eligibility", AsyncMock()) as elig:
        with pytest.raises(mod.WorkflowRefused) as exc:
            await mod.start(
                owner_id="org_a",
                provider_name="posthog",
                service_id="svc_analytics",
                configuration=CONFIG,
                issue_id=None,
            )
    assert exc.value.code == "requirements_pending"
    elig.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_snapshots_the_catalog_and_files_a_consent_card():
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))) as move,
    ):
        row = await mod.start(
            owner_id="org_a",
            provider_name="posthog",
            service_id="svc_analytics",
            configuration=CONFIG,
            issue_id="iss_1",
        )
    assert row["state"] == "awaiting_approval"
    # The intent is persisted BEFORE any card or write.
    assert create.await_args.kwargs["service_ref"] == "svc_analytics"
    snapshot = move.await_args.kwargs["fields"]["approval"]
    assert snapshot["service_ref"] == "svc_analytics"
    assert snapshot["pricing_type"] == "free"
    assert snapshot["terms_url"] == "https://posthog.com/terms"
    assert snapshot["configuration"] == CONFIG
    assert snapshot["availability"] == "PROVIDER_SERVICE_AVAILABILITY_AVAILABLE"
    mod.provisioning_approvals.request_provider_consent.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_carries_the_asking_agents_reason_to_the_row_and_the_card():
    """The catalog says WHAT the service is; only Friday can say what it is
    for, and the owner is approving spend on her word. The row keeps it because
    a drift re-file has to say why all over again."""
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))),
    ):
        await mod.start(
            owner_id="org_a",
            provider_name="posthog",
            service_id="svc_analytics",
            configuration=CONFIG,
            issue_id=None,
            reason="The launch needs funnel numbers by Friday.",
        )
    assert create.await_args.kwargs["reason"] == "The launch needs funnel numbers by Friday."
    card = mod.provisioning_approvals.request_provider_consent.await_args
    assert card.kwargs["reason"] == "The launch needs funnel numbers by Friday."


@pytest.mark.asyncio
async def test_a_sequential_re_ask_reuses_the_open_workflow_and_its_card():
    """Friday retrying after a timeout must not open a second card — or, once
    approved, provision the same resource twice. Two SIMULTANEOUS asks can
    still both miss (accepted residual — see the module docstring)."""
    open_row = _row(state="awaiting_approval")
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=[open_row])),
        patch.object(mod.repo, "create", AsyncMock()) as create,
    ):
        row = await mod.start(
            owner_id="org_a", provider_name="posthog", service_id="svc_analytics", configuration=CONFIG, issue_id=None
        )
    assert row["workflow_id"] == "pwf_1"
    # In-memory only, so the caller can say "already asked" instead of claiming
    # it filed a second card. Never written to the row.
    assert row["reused"] is True
    assert "reused" not in open_row
    create.assert_not_awaited()
    mod.provisioning_approvals.request_provider_consent.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_terminal_workflow_does_not_block_a_fresh_ask():
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=[_row(state="rejected")])),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))),
    ):
        await mod.start(
            owner_id="org_a", provider_name="posthog", service_id="svc_analytics", configuration=CONFIG, issue_id=None
        )
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_paperclip_company_fails_the_row_rather_than_leaving_it_open():
    """A card nobody can approve is a workflow nobody can finish."""
    mod.provisioning_approvals.request_provider_consent.return_value = None
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=[])),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="failed"))) as move,
    ):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await mod.start(
                owner_id="org_a",
                provider_name="posthog",
                service_id="svc_analytics",
                configuration=CONFIG,
                issue_id=None,
            )
    assert exc.value.code == "no_company"
    assert move.await_args.kwargs["to_state"] == "failed"


def test_resource_name_is_unique_per_workflow_never_the_bare_service_ref():
    a = mod.resource_name_for(_row(workflow_id="pwf_aaaaaa"))
    b = mod.resource_name_for(_row(workflow_id="pwf_bbbbbb"))
    assert a != b
    assert a != "svc_analytics"
    assert a.startswith("svc_analytics-")


@pytest.mark.asyncio
async def test_start_refuses_an_ineligible_account():
    with patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": False, "requirements": []})):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await mod.start(
                owner_id="org_a",
                provider_name="posthog",
                service_id="svc_analytics",
                configuration=CONFIG,
                issue_id=None,
            )
    assert exc.value.code == "not_eligible"


@pytest.mark.asyncio
async def test_start_with_outstanding_requirements_asks_for_onboarding_not_provisioning():
    """is_eligible=true with requirements means onboarding isn't complete —
    "Don't treat is_eligible=true alone as proof that verification is complete." """
    with patch.object(
        mod.api,
        "eligibility",
        AsyncMock(return_value={"is_eligible": True, "requirements": ["representative.dob", "tos.stripe"]}),
    ):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await mod.start(
                owner_id="org_a",
                provider_name="posthog",
                service_id="svc_analytics",
                configuration=CONFIG,
                issue_id=None,
            )
    assert exc.value.code == "requirements_pending"
    assert "representative.dob" in exc.value.requirements


@pytest.mark.asyncio
async def test_start_rejects_an_unavailable_service():
    unavailable = {**SERVICE, "availability": "PROVIDER_SERVICE_AVAILABILITY_NOT_IN_COUNTRY"}
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[unavailable])),
    ):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await mod.start(
                owner_id="org_a",
                provider_name="posthog",
                service_id="svc_analytics",
                configuration=CONFIG,
                issue_id=None,
            )
    assert exc.value.code == "service_unavailable"


@pytest.mark.asyncio
async def test_start_validates_configuration_against_the_service_schema():
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
    ):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await mod.start(
                owner_id="org_a",
                provider_name="posthog",
                service_id="svc_analytics",
                configuration={"region": "mars"},
                issue_id=None,
            )
    assert exc.value.code == "invalid_configuration"
    # Never the submitted VALUE: this refusal is relayed into Friday's
    # transcript, and a provider field can be a credential.
    assert "mars" not in exc.value.message
    # Still says which field, or she retries blind.
    assert "organization_name" in exc.value.message


@pytest.mark.asyncio
async def test_start_without_a_company_stripe_account_refuses(monkeypatch):
    monkeypatch.setattr(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"}))
    with pytest.raises(mod.WorkflowRefused) as exc:
        await mod.start(
            owner_id="org_a", provider_name="posthog", service_id="svc_analytics", configuration=CONFIG, issue_id=None
        )
    assert exc.value.code == "no_company_stripe_account"


# --- approval ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_with_unchanged_catalog_moves_to_connecting_provider():
    row = _row(state="awaiting_approval", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(mod.repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="connecting_provider"))) as move,
    ):
        out = await mod.on_approved("pwf_1", usage_limit=None, approved_by="user_admin")
    assert out["state"] == "connecting_provider"
    assert move.await_args.kwargs["to_state"] == "connecting_provider"


@pytest.mark.asyncio
async def test_catalog_drift_after_approval_files_a_fresh_card():
    """ "If any approved value changed, require approval again." """
    row = _row(
        state="awaiting_approval",
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
        reason="The launch needs funnel numbers.",
    )
    now_paid = {
        **SERVICE,
        "pricing": {"type": "paid", "paid_pricing": [{"type": "freeform", "freeform": "$20/mo", "is_default": True}]},
    }
    with (
        patch.object(mod.repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[now_paid])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))) as move,
    ):
        out = await mod.on_approved("pwf_1", usage_limit=None, approved_by="user_admin")
    assert out["state"] == "awaiting_approval"
    assert move.await_args.kwargs["to_state"] == "awaiting_approval"
    assert mod.provisioning_approvals.request_provider_consent.await_count == 1
    # The same decision asked again: it must still say why.
    refile = mod.provisioning_approvals.request_provider_consent.await_args
    assert refile.kwargs["reason"] == "The launch needs funnel numbers."


@pytest.mark.asyncio
async def test_a_drifted_consent_card_nobody_can_answer_parks_the_row():
    """The fifth card site: a drift re-file that returns None must park, not
    leave the row in awaiting_approval with no card."""
    row = _row(state="awaiting_approval", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    now_paid = {
        **SERVICE,
        "pricing": {"type": "paid", "paid_pricing": [{"type": "freeform", "freeform": "$20/mo", "is_default": True}]},
    }
    mod.provisioning_approvals.request_provider_consent.return_value = None
    with (
        patch.object(mod.repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[now_paid])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        out = await mod.on_approved("pwf_1", usage_limit=None, approved_by="user_admin")
    assert out["state"] == "requires_review"
    assert move.await_args.kwargs["to_state"] == "requires_review"
    assert move.await_args.kwargs["fields"]["error_code"] == "no_company"


@pytest.mark.asyncio
async def test_paid_service_without_a_payment_profile_files_the_payment_card():
    paid = {
        **SERVICE,
        "pricing": {"type": "paid", "paid_pricing": [{"type": "freeform", "freeform": "$20/mo", "is_default": True}]},
    }
    row = _row(state="awaiting_approval", approval=mod.snapshot_of(paid, PROVIDER, CONFIG))
    with (
        patch.object(mod.repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[paid])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.api, "payment_profile", AsyncMock(side_effect=ProvisioningError("not_found", "no profile"))),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_user_action"))) as move,
    ):
        out = await mod.on_approved(
            "pwf_1",
            usage_limit={"max_amount": 5000, "currency": "usd", "recurring_interval": "month"},
            approved_by="user_admin",
        )
    assert out["state"] == "awaiting_user_action"
    assert move.await_args.kwargs["fields"]["awaiting"] == "payment_setup"
    mod.provisioning_approvals.request_payment_setup.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_declined_card_is_a_terminal_state_not_a_deleted_row():
    with patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="rejected"))) as move:
        out = await mod.on_rejected("pwf_1")
    assert out["state"] == "rejected"
    assert move.await_args.kwargs == {"from_state": "awaiting_approval", "to_state": "rejected"}


@pytest.mark.asyncio
async def test_cancel_refuses_once_anything_exists_on_the_owners_stripe_account():
    """Nothing may be cancelled behind Stripe's back: an account request or a
    resource id means something exists that a row deletion would orphan."""
    with patch.object(
        mod.repo,
        "get",
        AsyncMock(return_value=_row(state="awaiting_user_action", provider_connection_request_id="ar_1")),
    ):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await mod.cancel("pwf_1")
    assert exc.value.code == "cannot_cancel"

    with patch.object(mod.repo, "get", AsyncMock(return_value=_row(state="provisioning", resource_id="res_1"))):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await mod.cancel("pwf_1")
    assert exc.value.code == "cannot_cancel"

    with (
        patch.object(mod.repo, "get", AsyncMock(return_value=_row(state="awaiting_approval"))),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="cancelled"))) as move,
    ):
        out = await mod.cancel("pwf_1")
    assert out["state"] == "cancelled"
    assert move.await_args.kwargs["to_state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_clears_the_pending_question_with_the_state():
    """A cancelled row that still says `awaiting: payment_setup` is one every
    owner route keyed on `awaiting` still treats as live — a withdrawn ask
    could mint a hosted checkout."""
    row = _row(state="awaiting_user_action", awaiting="payment_setup", user_action_since="2026-08-01T00:00:00Z")
    with (
        patch.object(mod.repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="cancelled"))) as move,
    ):
        await mod.cancel("pwf_1")
    assert move.await_args.kwargs["fields"] == {
        "awaiting": None,
        "information_target": None,
        "information_schema": None,
        "user_action_since": None,
    }


# --- connecting_provider ----------------------------------------------------


@pytest.mark.asyncio
async def test_active_connection_skips_straight_to_provisioning():
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(
            mod.api, "list_provider_connections", AsyncMock(return_value=[{"provider": "prvdr_1", "status": "active"}])
        ),
        patch.object(
            mod.api, "create_resource", AsyncMock(return_value={"id": "res_1", "status": "pending"})
        ) as create,
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="provisioning", resource_id="res_1"))),
    ):
        out = await mod.advance(row)
    assert out["state"] == "provisioning"
    # Nothing to reconcile with (no known resources) ⇒ it creates.
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_auth_files_a_connect_card_and_waits():
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(mod.api, "list_provider_connections", AsyncMock(return_value=[])),
        patch.object(
            mod.api,
            "create_provider_connection_request",
            AsyncMock(
                return_value={
                    "id": "ar_1",
                    "request_status": "pending_auth",
                    "redirect_url": "https://provider.example/oauth",
                }
            ),
        ),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})) as patched,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_user_action"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "awaiting_user_action"
    # The account request id is persisted BEFORE the card opens any redirect.
    assert patched.await_args_list[0].args[1]["provider_connection_request_id"] == "ar_1"
    assert move.await_args.kwargs["fields"]["awaiting"] == "provider_connect"
    # The URL is NOT put on the card — the connect-link route mints it on click.
    kwargs = mod.provisioning_approvals.request_provider_connect.await_args.kwargs
    assert "redirect_url" not in kwargs


@pytest.mark.asyncio
async def test_a_later_leg_card_nobody_can_answer_stops_instead_of_re_filing():
    """No Paperclip company ⇒ no Inbox ⇒ the connect card cannot be filed. The
    row must park in requires_review, not re-enter the poll loop and re-file
    the card on every pass."""
    mod.provisioning_approvals.request_provider_connect.return_value = None
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(mod.api, "list_provider_connections", AsyncMock(return_value=[])),
        patch.object(
            mod.api,
            "create_provider_connection_request",
            AsyncMock(return_value={"id": "ar_1", "request_status": "pending_auth"}),
        ),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "requires_review"
    assert move.await_args.kwargs["to_state"] == "requires_review"
    assert move.await_args.kwargs["fields"]["error_code"] == "no_company"


@pytest.mark.asyncio
async def test_two_active_connections_stop_rather_than_guess():
    """ "The current API doesn't let Forge select among several same-Provider
    connections. […] if the returned state is ambiguous, stop." """
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    two = [{"provider": "prvdr_1", "status": "active"}, {"provider": "prvdr_1", "status": "active"}]
    with (
        patch.object(mod.api, "list_provider_connections", AsyncMock(return_value=two)),
        patch.object(mod.api, "create_resource", AsyncMock()) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "requires_review"
    assert move.await_args.kwargs["fields"]["error_code"] == "ambiguous_provider_connection"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_account_request_records_which_provider_account_exists():
    """ "Show returned account details so Ada knows which Provider account will
    receive authentication, billing, security, and operational communications." """
    row = _row(
        state="connecting_provider",
        provider_connection_request_id="ar_1",
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    ar = {
        "id": "ar_1",
        "request_status": "complete",
        "provider_account_details": {
            "id": "prv_acct_1",
            "display_name": "Northwind",
            "primary_email": "founder@example.com",
            "link_action": "linked_existing",
            "active_services": [{"service_id": "svc_analytics", "display_name": "Analytics", "status": "active"}],
        },
    }
    with (
        patch.object(mod.api, "get_provider_connection_request", AsyncMock(return_value=ar)),
        patch.object(
            mod.api, "list_provider_connections", AsyncMock(return_value=[{"provider": "prvdr_1", "status": "active"}])
        ),
        patch.object(mod.api, "create_resource", AsyncMock(return_value={"id": "res_1", "status": "pending"})),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})) as patched,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="provisioning"))),
    ):
        await mod.advance(row)
    saved = patched.await_args_list[0].args[1]["provider_account"]
    assert saved["display_name"] == "Northwind"
    assert saved["primary_email"] == "founder@example.com"
    assert saved["link_action"] == "linked_existing"
    # The provider-side account id is not ours to hand around.
    assert "id" not in saved


@pytest.mark.asyncio
async def test_needs_information_records_which_endpoint_will_receive_it():
    """The account-request and resource submit endpoints take different bodies;
    the row must say which one this schema belongs to."""
    base = _row(
        state="connecting_provider",
        provider_connection_request_id="ar_1",
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    schema = {"type": "object", "properties": {"team": {"type": "string"}}}
    with (
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(
                return_value={"id": "ar_1", "request_status": "needs_information", "needs_information_schema": schema}
            ),
        ),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_user_action"))) as move,
    ):
        await mod.advance(base)
    assert move.await_args.kwargs["fields"]["information_target"] == "account_request"

    res_row = _row(state="provisioning", resource_id="res_1", approval=base["approval"])
    with (
        patch.object(
            mod.api,
            "get_resource",
            AsyncMock(return_value={"id": "res_1", "status": "needs_information", "needs_information_schema": schema}),
        ),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_user_action"))) as move2,
    ):
        await mod.advance(res_row)
    assert move2.await_args.kwargs["fields"]["information_target"] == "resource"


@pytest.mark.asyncio
async def test_an_empty_needs_information_schema_is_a_repoll_not_a_dead_end():
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(
            mod.api,
            "get_resource",
            AsyncMock(
                return_value={
                    "id": "res_1",
                    "request_status": "needs_information",
                    "needs_information_schema": {"type": "object", "properties": {}},
                }
            ),
        ),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})),
        patch.object(mod.repo, "transition", AsyncMock()) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "provisioning"
    move.assert_not_awaited()


@pytest.mark.asyncio
async def test_reauth_clears_the_provider_connection_request_id_so_a_fresh_one_is_created():
    """Without the clear, `advance` re-reads the OLD (complete) request forever
    and the new one is never created."""
    row = _row(
        state="binding",
        resource_id="res_1",
        provider_connection_request_id="ar_old",
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    with (
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete"})),
        patch.object(
            mod.api,
            "reveal_access_configuration",
            AsyncMock(side_effect=ProvisioningError("provider_reauth_required", "reconnect")),
        ),
        patch.object(
            mod.repo,
            "transition",
            AsyncMock(return_value=_row(state="connecting_provider", provider_connection_request_id=None)),
        ) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "connecting_provider"
    fields = move.await_args.kwargs["fields"]
    assert fields["provider_connection_request_id"] is None
    assert fields["error_code"] == "provider_reauth_required"


@pytest.mark.asyncio
async def test_approval_records_who_approved_and_when():
    row = _row(state="awaiting_approval", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(mod.repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="connecting_provider"))) as move,
    ):
        await mod.on_approved("pwf_1", usage_limit=None, approved_by="user_admin")
    fields = move.await_args.kwargs["fields"]
    assert fields["approved_by"] == "user_admin"
    assert fields["approved_at"]


@pytest.mark.asyncio
async def test_account_request_error_fails_the_workflow_with_a_safe_message():
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(mod.api, "list_provider_connections", AsyncMock(return_value=[])),
        patch.object(
            mod.api,
            "create_provider_connection_request",
            AsyncMock(
                return_value={
                    "id": "ar_1",
                    "request_status": "error",
                    "error": {"code": "provider_failure", "message": "provider down"},
                }
            ),
        ),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="failed"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "failed"
    assert move.await_args.kwargs["fields"]["error_code"] == "provider_failure"


# --- provisioning + binding -------------------------------------------------


@pytest.mark.asyncio
async def test_complete_resource_binds_the_revealed_configuration_of_that_resource():
    """The reveal is per RESOURCE (`resources/{id}/reveal_access_configuration`),
    so there is no entry to select out of a service-wide list."""
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    env_info = {"resource": "res_1", "configuration": {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live"}}
    with (
        patch.object(
            mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete", "name": "analytics"})
        ),
        patch.object(mod.api, "reveal_access_configuration", AsyncMock(return_value=env_info)) as reveal,
        patch.object(mod, "land_credentials", AsyncMock()) as land,
        patch.object(mod.repo, "transition", AsyncMock(side_effect=[_row(state="binding"), _row(state="active")])),
    ):
        out = await mod.advance(row)
    assert out["state"] == "active"
    reveal.assert_awaited_once_with("acct_1", "res_1")
    assert land.await_args.kwargs["env"] == {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live"}
    assert land.await_args.kwargs["resource"]["resource_id"] == "res_1"
    assert land.await_args.kwargs["resource"]["env_keys"] == ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]
    assert land.await_args.kwargs["resource"]["level"] == "company"
    mod.owner_facts.write_owner_facts.assert_awaited_once_with("org_a")
    # The work-product receipt rides the bind, scoped to the originating issue.
    receipt = mod.work_products.register.await_args
    assert receipt.args == ("org_a",)
    assert receipt.kwargs["type"] == "artifact"
    assert receipt.kwargs["provider"] == "posthog"
    assert receipt.kwargs["title"] == "Provisioned svc_analytics"
    assert receipt.kwargs["issue_id"] == "iss_1"
    assert receipt.kwargs["external_id"] == "res_1"
    assert receipt.kwargs["metadata"] == {"workflow_id": "pwf_1", "level": "company"}


@pytest.mark.asyncio
async def test_removed_resource_never_binds_credentials():
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "removed"})),
        patch.object(mod.api, "reveal_access_configuration", AsyncMock()) as env,
        patch.object(mod, "land_credentials", AsyncMock()) as land,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="failed"))),
    ):
        out = await mod.advance(row)
    assert out["state"] == "failed"
    env.assert_not_awaited()
    land.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_resource_backs_off_until_the_deadline_then_requires_review():
    row = _row(
        state="provisioning",
        resource_id="res_1",
        attempts=3,
        deadline_at="2000-01-01T00:00:00Z",
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    with (
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "pending"})),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "requires_review"
    # The id is kept: "Stop after a bounded deadline, keep the Resource ID."
    assert move.await_args.kwargs["fields"]["error_code"] == "poll_deadline_exceeded"


@pytest.mark.asyncio
async def test_a_thin_stripe_test_provider_is_never_offered():
    """The live catalog carries `herenow` / `Perplexity` / `Shopify` rows with
    empty categories AND description — Stripe's own test providers."""
    thin_provider = {"id": "prvdr_9", "name": "herenow", "categories": [], "description": ""}
    thin_service = {**SERVICE, "provider": "prvdr_9", "provider_name": "herenow"}
    assert mod.is_offerable(SERVICE, PROVIDER) is True
    assert mod.is_offerable(thin_service, thin_provider) is False
    # A development-only row never shows either.
    assert mod.is_offerable({**SERVICE, "development": True}, PROVIDER) is False


@pytest.mark.asyncio
async def test_a_stripe_5xx_on_a_READ_keeps_the_state_and_retries():
    """Preview flakiness: a 5xx is transient. It must not fail the workflow and
    must not turn into a repeated POST."""
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    err = mod.api.TransientProvisioningError("provisioning_error", "api_error", http_status=500)
    with (
        patch.object(mod.api, "get_resource", AsyncMock(side_effect=err)),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})) as patched,
        patch.object(mod.repo, "transition", AsyncMock()) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "provisioning"
    assert patched.await_args.args[1]["attempts"] == 1
    move.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stripe_5xx_on_a_WRITE_is_an_uncertain_write():
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    err = mod.api.TransientProvisioningError("provisioning_error", "api_error", http_status=500)
    with (
        patch.object(
            mod.api, "list_provider_connections", AsyncMock(return_value=[{"provider": "prvdr_1", "status": "active"}])
        ),
        patch.object(mod.api, "create_resource", AsyncMock(side_effect=err)) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "requires_review"
    assert move.await_args.kwargs["fields"]["error_code"] == "uncertain_write"
    assert create.await_count == 1


# --- the no-repeat rule -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_timed_out_create_is_never_repeated():
    """ "Don't automatically repeat a POST after a timeout or unknown network
    outcome. Mark the workflow requires_review." """
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    create = AsyncMock(
        side_effect=mod.api.TransientProvisioningError("transport_error", "Could not reach Stripe; try again")
    )
    parked = _row(state="requires_review", error_code="uncertain_write")
    with (
        patch.object(
            mod.api, "list_provider_connections", AsyncMock(return_value=[{"provider": "prvdr_1", "status": "active"}])
        ),
        patch.object(mod.api, "create_resource", create),
        patch.object(mod.repo, "transition", AsyncMock(return_value=parked)) as move,
        patch.object(mod.repo, "get", AsyncMock(return_value=parked)),
    ):
        out = await mod.advance(row)
        # Advancing the row AS THE REPO NOW HAS IT must not fire a second create.
        await mod.advance(await mod.repo.get("pwf_1"))
    assert out["state"] == "requires_review"
    assert create.await_count == 1
    assert move.await_args.kwargs["fields"]["error_code"] == "uncertain_write"


@pytest.mark.asyncio
async def test_provider_reauth_required_starts_a_new_account_request():
    row = _row(state="provisioning", resource_id=None, approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(
            mod.api, "get_resource", AsyncMock(side_effect=ProvisioningError("provider_reauth_required", "reconnect"))
        ),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="connecting_provider"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "connecting_provider"
    assert move.await_args.kwargs["fields"]["error_code"] == "provider_reauth_required"


@pytest.mark.asyncio
async def test_needs_information_with_an_unrenderable_schema_requires_review():
    """The constrained renderer takes string/number/boolean/enum. Anything else
    → requires_review rather than a form nobody can fill in (spec §3.2)."""
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    exotic = {"type": "object", "properties": {"blob": {"type": "array", "items": {"type": "object"}}}}
    with (
        patch.object(
            mod.api,
            "get_resource",
            AsyncMock(return_value={"id": "res_1", "status": "needs_information", "needs_information_schema": exotic}),
        ),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "requires_review"
    assert move.await_args.kwargs["fields"]["error_code"] == "unrenderable_schema"


@pytest.mark.asyncio
async def test_no_secret_reaches_the_log(caplog):
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    env_info = {"resource": "res_1", "configuration": {"K": "phc_SECRET"}}
    with (
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete"})),
        patch.object(mod.api, "reveal_access_configuration", AsyncMock(return_value=env_info)),
        patch.object(mod, "land_credentials", AsyncMock()),
        patch.object(mod.repo, "transition", AsyncMock(side_effect=[_row(state="binding"), _row(state="active")])),
        caplog.at_level("DEBUG"),
    ):
        await mod.advance(row)
    assert "phc_SECRET" not in caplog.text


# --- the company Project ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_company_project_is_read_from_the_billing_row():
    """One Project per COMPANY: a second workflow must not mint a second one."""
    with (
        patch.object(mod.api, "create_project", AsyncMock()) as created,
    ):
        assert await mod._project_for("acct_1", _approved()) == ("proj_1", None)
    created.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_account_scoped_service_needs_no_project():
    account_scoped = _row(approval=mod.snapshot_of({**SERVICE, "scope": "account"}, PROVIDER, CONFIG))
    with patch.object(mod.api, "create_project", AsyncMock()) as created:
        assert await mod._project_for("acct_1", account_scoped) == (None, None)
    created.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_project_is_created_then_stored(monkeypatch):
    monkeypatch.setattr(
        mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"company_stripe_account_id": "acct_1", "company_stripe_source": "created"}),
    )
    store = AsyncMock(return_value={"company_stripe_project_id": "proj_new"})
    monkeypatch.setattr(mod.billing_repo, "set_company_stripe_project_id", store)
    with (
        patch("core.services.company_stripe.resolve_company_name", AsyncMock(return_value="Northwind")),
        patch.object(
            mod.api, "create_project", AsyncMock(return_value={"id": "proj_new", "name": "Northwind"})
        ) as created,
    ):
        assert await mod._project_for("acct_1", _approved()) == ("proj_new", None)
    assert created.await_args.kwargs["catalog"] == mod.settings.STRIPE_PROJECTS_CATALOG
    assert store.await_args.kwargs["project_id"] == "proj_new"


@pytest.mark.asyncio
async def test_losing_the_project_race_adopts_the_winners_project(monkeypatch):
    """The conditional write is the arbiter. The loser's Project is inert
    metadata; using it anyway would split the company's credentials."""
    monkeypatch.setattr(
        mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            side_effect=[
                {"company_stripe_account_id": "acct_1", "company_stripe_source": "created"},
                {"company_stripe_project_id": "proj_winner"},
            ]
        ),
    )
    monkeypatch.setattr(mod.billing_repo, "set_company_stripe_project_id", AsyncMock(return_value=None))
    with (
        patch("core.services.company_stripe.resolve_company_name", AsyncMock(return_value="Northwind")),
        patch.object(mod.api, "create_project", AsyncMock(return_value={"id": "proj_mine"})),
    ):
        assert await mod._project_for("acct_1", _approved()) == ("proj_winner", None)
    # Reading back a write that landed in ANOTHER process: eventual consistency
    # would hand us our own losing project.
    assert mod.billing_repo.get_by_owner_id.await_args.kwargs["consistent_read"] is True


@pytest.mark.asyncio
async def test_a_lost_race_with_no_visible_winner_retries_rather_than_using_ours(monkeypatch):
    """Proceeding on our own losing Project would land this company's next
    credential in a Project the winner never sees."""
    monkeypatch.setattr(
        mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"company_stripe_account_id": "acct_1", "company_stripe_source": "created"}),
    )
    monkeypatch.setattr(mod.billing_repo, "set_company_stripe_project_id", AsyncMock(return_value=None))
    row = _approved(state="connecting_provider")
    with (
        patch("core.services.company_stripe.resolve_company_name", AsyncMock(return_value="Northwind")),
        patch.object(mod.api, "create_project", AsyncMock(return_value={"id": "proj_mine"})),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})) as patched,
    ):
        project_id, refused = await mod._project_for("acct_1", row)
    assert project_id is None
    assert refused["state"] == "connecting_provider"
    assert patched.await_args.args[1]["attempts"] == 1


@pytest.mark.asyncio
async def test_an_uncertain_project_create_parks_the_row(monkeypatch):
    """`create_project` is a POST like any other: an unknown outcome must not
    become a second Project on the next pass."""
    monkeypatch.setattr(
        mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"company_stripe_account_id": "acct_1", "company_stripe_source": "created"}),
    )
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch("core.services.company_stripe.resolve_company_name", AsyncMock(return_value="Northwind")),
        patch.object(
            mod.api,
            "create_project",
            AsyncMock(side_effect=mod.api.TransientProvisioningError("transport_error", "unreachable")),
        ),
        patch.object(mod.api, "list_provider_connections", AsyncMock(return_value=[])),
        patch.object(mod.api, "create_provider_connection_request", AsyncMock()) as ar,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "requires_review"
    assert move.await_args.kwargs["fields"]["error_code"] == "uncertain_write"
    ar.assert_not_awaited()


# --- reconcile, races and scope ---------------------------------------------


@pytest.mark.asyncio
async def test_a_row_that_moved_under_us_never_binds_twice():
    """A None from the conditional transition means the row is not ours any
    more — reading credentials or landing them again would be the duplicate."""
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    winner = _row(state="active", resource_id="res_1")
    with (
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete"})),
        patch.object(mod.api, "reveal_access_configuration", AsyncMock()) as env,
        patch.object(mod, "land_credentials", AsyncMock()) as land,
        patch.object(mod.repo, "transition", AsyncMock(return_value=None)),
        patch.object(mod.repo, "get", AsyncMock(return_value=winner)),
    ):
        out = await mod.advance(row)
    assert out["state"] == "active"
    env.assert_not_awaited()
    land.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_a_project_scoped_service_records_a_project_on_the_pointer():
    """ "A project is supplied for scope=project; account-scoped resources are
    placed at account level." Binding never mints one either way."""
    env_info = {"resource": "res_1", "configuration": {"K": "v"}}
    for scope, expected in (("project", "proj_1"), ("account", None)):
        service = {**SERVICE, "scope": scope}
        row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(service, PROVIDER, CONFIG))
        with (
            patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete"})),
            patch.object(mod.api, "reveal_access_configuration", AsyncMock(return_value=env_info)),
            patch.object(mod, "land_credentials", AsyncMock()) as land,
            patch.object(mod.api, "create_project", AsyncMock()) as created,
            patch.object(mod.repo, "transition", AsyncMock(side_effect=[_row(state="binding"), _row(state="active")])),
        ):
            await mod.advance(row)
        assert land.await_args.kwargs["resource"]["project_id"] == expected
        created.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_expired_or_unavailable_access_configuration_parks_for_review():
    """Rotate or relink is a human's call: not a retry, not a failed workflow."""
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    err = ProvisioningError("resource_access_configuration_expired", "expired")
    with (
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete"})),
        patch.object(mod.api, "reveal_access_configuration", AsyncMock(side_effect=err)),
        patch.object(mod, "land_credentials", AsyncMock()) as land,
        patch.object(
            mod.repo, "transition", AsyncMock(side_effect=[_row(state="binding"), _row(state="requires_review")])
        ) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "requires_review"
    assert move.await_args.kwargs["fields"] == {"error_code": "resource_access_configuration_expired"}
    land.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_resource_always_sends_an_explicit_livemode():
    """Stripe's default for an omitted `livemode` is TRUE."""
    row = _approved(state="connecting_provider")
    with (
        patch.object(mod.api, "is_livemode", return_value=False),
        patch.object(mod.api, "create_resource", AsyncMock(return_value={"id": "res_1"})) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="provisioning"))),
    ):
        await mod._create_resource("acct_1", row)
    assert create.await_args.kwargs["livemode"] is False


@pytest.mark.asyncio
async def test_a_dev_row_with_the_old_field_name_still_polls_its_request():
    row = _row(state="connecting_provider", account_request_id="ar_old")
    with (
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(return_value={"id": "ar_old", "request_status": "requested"}),
        ) as get,
        patch.object(mod.repo, "patch", AsyncMock(return_value=row)),
    ):
        await mod.advance(row)
    assert get.await_args.args == ("acct_1", "ar_old")


@pytest.mark.asyncio
async def test_a_requested_connection_polls_until_the_fifteen_minute_cap():
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    row = _row(state="connecting_provider", provider_connection_request_id="pcr_1", deadline_at=past)
    with (
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(return_value={"id": "pcr_1", "request_status": "requested"}),
        ),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        await mod.advance(row)
    assert move.await_args.kwargs["fields"] == {"error_code": "poll_deadline_exceeded"}


@pytest.mark.asyncio
async def test_a_complete_create_response_needs_exactly_one_active_connection():
    """`complete` can arrive on the create response itself; the request stays
    complete after an unlink, so the connection list is the proof."""
    row = _approved(state="connecting_provider", provider_connection_request_id="pcr_1")
    two = [{"provider": "prvdr_1", "status": "active"}, {"provider": "prvdr_1", "status": "active"}]
    with (
        patch.object(mod.api, "list_provider_connections", AsyncMock(return_value=two)),
        patch.object(mod.api, "create_resource", AsyncMock()) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        await mod._handle_connection_request("acct_1", row, {"id": "pcr_1", "request_status": "complete"})
    assert move.await_args.kwargs["fields"] == {"error_code": "ambiguous_provider_connection"}
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_consent_card_says_when_this_approval_creates_the_project():
    creates = mod.snapshot_of(SERVICE, PROVIDER, CONFIG, creates_project=True)
    assert creates["creates_project"] is True and "Project" in creates["action"]
    assert mod.snapshot_of(SERVICE, PROVIDER, CONFIG)["action"] == "creates new"


@pytest.mark.parametrize("attempts,low,high", [(0, 1, 1.25), (3, 8, 10), (5, 30, 37.5), (40, 30, 37.5)])
@pytest.mark.asyncio
async def test_polling_follows_the_guides_backoff_with_jitter(attempts, low, high):
    row = _row(state="provisioning", resource_id="res_1", attempts=attempts)
    with patch.object(mod.repo, "patch", AsyncMock(return_value=row)) as bump:
        await mod._bump(row)
    wait = (
        datetime.fromisoformat(bump.await_args[0][1]["next_poll_after"]) - datetime.now(timezone.utc)
    ).total_seconds()
    assert low - 0.5 <= wait <= high + 0.5


# --- waiting on a person ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_connect_leg_polls_from_the_server_and_waits():
    """ "Keep polling the Account request from the server" — a browser that
    never comes back must not strand the workflow, and must not move it on."""
    row = _row(
        state="awaiting_user_action",
        awaiting="provider_connect",
        provider_connection_request_id="ar_1",
        approval_id="apr_1",
        user_action_since=mod.utc_now_iso(),
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    with (
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(return_value={"id": "ar_1", "request_status": "pending_auth"}),
        ),
        patch.object(mod.repo, "transition", AsyncMock()) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "awaiting_user_action"
    move.assert_not_awaited()
    # The card was filed when the row entered the state, and is not re-filed.
    mod.provisioning_approvals.request_provider_connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_connect_leg_resumes_itself_when_the_owner_comes_back():
    row = _row(
        state="awaiting_user_action",
        awaiting="provider_connect",
        provider_connection_request_id="ar_1",
        user_action_since=mod.utc_now_iso(),
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    ar = {
        "id": "ar_1",
        "request_status": "complete",
        "provider_account_details": {"display_name": "Northwind", "link_action": "created"},
    }
    with (
        patch.object(mod.api, "get_provider_connection_request", AsyncMock(return_value=ar)),
        patch.object(
            mod.api, "list_provider_connections", AsyncMock(return_value=[{"provider": "prvdr_1", "status": "active"}])
        ),
        patch.object(mod.api, "create_resource", AsyncMock(return_value={"id": "res_1", "status": "pending"})),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})) as patched,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="provisioning"))),
    ):
        out = await mod.advance(row)
    assert out["state"] == "provisioning"
    assert patched.await_args_list[0].args[1]["provider_account"]["display_name"] == "Northwind"


@pytest.mark.asyncio
async def test_the_payment_leg_waits_for_the_profile_then_resumes():
    row = _row(
        state="awaiting_user_action",
        awaiting="payment_setup",
        user_action_since=mod.utc_now_iso(),
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    # payment_profile 404s by default — the owner has not paid yet.
    with patch.object(mod.repo, "transition", AsyncMock()) as move:
        assert (await mod.advance(row))["state"] == "awaiting_user_action"
    move.assert_not_awaited()

    with (
        patch.object(mod.api, "payment_profile", AsyncMock(return_value={"card_last4": "4242"})),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="connecting_provider"))) as move2,
    ):
        out = await mod.advance(row)
    assert out["state"] == "connecting_provider"
    assert move2.await_args.kwargs["fields"] == {"awaiting": None, "user_action_since": None}


@pytest.mark.asyncio
async def test_a_week_with_no_answer_parks_the_row_stale_with_its_ids():
    """`updated_at` moves on every transient poll; the owner's seven days are
    measured from `user_action_since` or the row would never go stale."""
    row = _row(
        state="awaiting_user_action",
        awaiting="provider_connect",
        provider_connection_request_id="ar_1",
        approval_id="apr_1",
        user_action_since=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
        updated_at=mod.utc_now_iso(),
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    with (
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(return_value={"id": "ar_1", "request_status": "pending_auth"}),
        ),
        patch.object(
            mod.repo, "transition", AsyncMock(return_value=_row(state="stale", provider_connection_request_id="ar_1"))
        ) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "stale"
    # Non-terminal, ids kept: the next owner click re-mints the link.
    assert out["provider_connection_request_id"] == "ar_1"
    assert move.await_args.kwargs["to_state"] == "stale"
    assert not move.await_args.kwargs.get("fields")


# --- transient reads --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_429_on_a_READ_backs_off_instead_of_failing():
    """The client raises the transient subclass for a 429 too — a rate limit is
    not a dead workflow, and re-deriving transience here would miss it."""
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    err = mod.api.TransientProvisioningError("rate_limit", "slow down", http_status=429, retry_after=2.0)
    with (
        patch.object(mod.api, "get_resource", AsyncMock(side_effect=err)),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})) as patched,
        patch.object(mod.repo, "transition", AsyncMock()) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "provisioning"
    assert patched.await_args.args[1]["attempts"] == 1
    move.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_transient_read_in_connecting_provider_keeps_the_state():
    row = _row(
        state="connecting_provider",
        provider_connection_request_id="ar_1",
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    with (
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(side_effect=mod.api.TransientProvisioningError("rate_limit", "slow", http_status=429)),
        ),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})) as patched,
        patch.object(mod.repo, "transition", AsyncMock()) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "connecting_provider"
    assert patched.await_args.args[1]["attempts"] == 1
    move.assert_not_awaited()


# --- one card per question --------------------------------------------------


@pytest.mark.asyncio
async def test_the_information_leg_asks_once_and_never_restarts_the_clock():
    """The row still carries the provider_connection_request_id from its connect leg. If
    the poll followed that instead of what the row is WAITING for, every tick
    would re-file the card and re-stamp the seven days."""
    since = mod.utc_now_iso()
    schema = {"type": "object", "properties": {"team": {"type": "string"}}}
    row = _approved(
        state="awaiting_user_action",
        awaiting="provider_information",
        information_target="resource",
        approval_id="apr_1",
        resource_id="res_1",
        provider_connection_request_id="ar_1",
        user_action_since=since,
    )
    with (
        patch.object(
            mod.api,
            "get_resource",
            AsyncMock(return_value={"id": "res_1", "status": "needs_information", "needs_information_schema": schema}),
        ),
        patch.object(mod.api, "get_provider_connection_request", AsyncMock()) as ar,
        patch.object(mod.repo, "transition", AsyncMock()) as move,
    ):
        first = await mod.advance(row)
        second = await mod.advance(first)
    assert second["state"] == "awaiting_user_action"
    assert second["user_action_since"] == since
    move.assert_not_awaited()
    ar.assert_not_awaited()
    mod.provisioning_approvals.request_provider_information.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_information_leg_still_goes_stale_after_a_week():
    schema = {"type": "object", "properties": {"team": {"type": "string"}}}
    row = _approved(
        state="awaiting_user_action",
        awaiting="provider_information",
        information_target="resource",
        approval_id="apr_1",
        resource_id="res_1",
        user_action_since=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
    )
    with (
        patch.object(
            mod.api,
            "get_resource",
            AsyncMock(return_value={"id": "res_1", "status": "needs_information", "needs_information_schema": schema}),
        ),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="stale"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "stale"
    assert move.await_args.kwargs["to_state"] == "stale"


# --- re-asking after an uncertain write -------------------------------------


def _parked(**over):
    """A row for the same (provider, service) pair, parked for review."""
    return _row(**{"state": "requires_review", "created_at": "2026-08-29T00:00:00+00:00", **over})


async def _start():
    return await mod.start(
        owner_id="org_a", provider_name="posthog", service_id="svc_analytics", configuration=CONFIG, issue_id=None
    )


def _catalog_reads():
    return (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
    )


@pytest.mark.parametrize(
    "parked",
    [
        pytest.param(_parked(error_code="uncertain_write"), id="uncertain_write"),
        pytest.param(_parked(error_code="credentials_not_found", resource_id="res_1"), id="carries a resource_id"),
        pytest.param(
            _parked(error_code="ambiguous_provider_connection", account_request_id="acr_1"),
            id="carries a dev-row account_request_id",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_re_ask_after_a_parked_uncertain_write_is_refused(parked):
    """The resource may ALREADY EXIST and be billing.

    A fresh workflow gets a fresh `resource_name_for`, so the reconcile-by-name
    read before every create would find nothing and create a second one. The
    only safe answer is to stop and name the parked workflow.
    """
    eligibility, services, providers = _catalog_reads()
    with (
        eligibility,
        services,
        providers,
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=[parked])),
        patch.object(mod.repo, "create", AsyncMock()) as create,
    ):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await _start()
    assert exc.value.code == "needs_review_first"
    assert parked["workflow_id"] in exc.value.message
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_parked_row_with_no_write_behind_it_does_not_block_a_re_ask():
    """`unrenderable_schema` is parked BEFORE anything was created — nothing
    exists to be created twice, so the owner is not stuck."""
    eligibility, services, providers = _catalog_reads()
    with (
        eligibility,
        services,
        providers,
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=[_parked(error_code="unrenderable_schema")])),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))),
    ):
        assert (await _start())["state"] == "awaiting_approval"
    create.assert_awaited_once()


@pytest.mark.parametrize("state", ["cancelled", "rejected"])
@pytest.mark.asyncio
async def test_a_cancelled_or_rejected_row_does_not_block_a_re_ask(state):
    eligibility, services, providers = _catalog_reads()
    with (
        eligibility,
        services,
        providers,
        patch.object(
            mod.repo,
            "list_for_owner",
            AsyncMock(return_value=[_parked(state=state, error_code="uncertain_write")]),
        ),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))),
    ):
        await _start()
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_the_NEWEST_row_for_the_pair_decides():
    """An older parked row behind a newer settled one is history, not a fence."""
    eligibility, services, providers = _catalog_reads()
    rows = [
        _parked(workflow_id="pwf_old", error_code="uncertain_write", created_at="2026-08-01T00:00:00+00:00"),
        _row(workflow_id="pwf_new", state="rejected", created_at="2026-08-28T00:00:00+00:00"),
    ]
    with (
        eligibility,
        services,
        providers,
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=rows)),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))),
    ):
        await _start()
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_board_failure_while_filing_the_consent_card_fails_the_row():
    """A row left in `ask` is a row the dedupe above treats as OPEN, so the
    retry would reuse a workflow nothing is driving. Close it instead."""
    mod.provisioning_approvals.request_provider_consent.side_effect = httpx.ConnectError("board down")
    eligibility, services, providers = _catalog_reads()
    with (
        eligibility,
        services,
        providers,
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="failed"))) as move,
    ):
        with pytest.raises(httpx.ConnectError):
            await _start()
    assert move.await_args.kwargs["to_state"] == "failed"
    assert move.await_args.kwargs["fields"]["error_code"] == "board_unavailable"


# --- the facts file follows every stop --------------------------------------


@pytest.mark.asyncio
async def test_a_rejection_rewrites_the_owner_facts_file():
    """Friday reads `provisioning` to decide whether to wait. A stop she never
    sees is a stop she waits out."""
    with patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="rejected"))):
        await mod.on_rejected("pwf_1")
    mod.owner_facts.write_owner_facts.assert_awaited_once_with("org_a")


# --- Retry-After ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_429_stamps_the_retry_after_stripe_asked_for():
    row = _row(state="provisioning", resource_id="res_1")
    err = mod.api.TransientProvisioningError("rate_limit", "slow down", http_status=429, retry_after=42)
    with (
        patch.object(mod.api, "get_resource", AsyncMock(side_effect=err)),
        patch.object(mod.repo, "patch", AsyncMock(return_value=row)) as bump,
    ):
        await mod.advance(row)
    fields = bump.await_args[0][1]
    assert fields["attempts"] == 1
    stamped = datetime.fromisoformat(fields["next_poll_after"])
    assert 30 < (stamped - datetime.now(timezone.utc)).total_seconds() <= 42


@pytest.mark.asyncio
async def test_a_transient_failure_without_a_retry_after_uses_the_backoff_floor():
    row = _row(state="provisioning", resource_id="res_1")
    err = mod.api.TransientProvisioningError("provisioning_error", "api_error", http_status=500)
    with (
        patch.object(mod.api, "get_resource", AsyncMock(side_effect=err)),
        patch.object(mod.repo, "patch", AsyncMock(return_value=row)) as bump,
    ):
        await mod.advance(row)
    stamped = datetime.fromisoformat(bump.await_args[0][1]["next_poll_after"])
    assert 0 < (stamped - datetime.now(timezone.utc)).total_seconds() <= 1.5


# --- the constrained renderer's contract ------------------------------------


def test_an_empty_or_malformed_enum_is_not_renderable():
    """A `select` with no options is a question the owner cannot answer, and a
    non-list `enum` is not a form we can draw at all — both are `requires_review`,
    not a half-rendered card."""
    assert mod._renderable({"properties": {"region": {"enum": ["us", "eu"]}}})
    assert not mod._renderable({"properties": {"region": {"enum": []}}})
    assert not mod._renderable({"properties": {"region": {"enum": "us"}}})


# --- the read IS the clock --------------------------------------------------


def _admin(approval):
    from unittest.mock import MagicMock

    admin = MagicMock()
    admin._http = object()
    admin.get_approval = AsyncMock(return_value=approval)
    return admin


@pytest.mark.asyncio
async def test_advance_on_read_takes_one_step_and_hands_the_row_back():
    row = _row(state="provisioning", resource_id="res_1")
    moved = _row(state="binding", resource_id="res_1")
    with (
        patch.object(mod.repo, "claim", AsyncMock(return_value=row)) as claim,
        patch.object(mod.repo, "release", AsyncMock()) as release,
        patch.object(mod, "advance", AsyncMock(return_value=moved)) as step,
    ):
        out = await mod.advance_on_read("pwf_1")
    assert out["state"] == "binding"
    # ONE step per read, and the claim is short: a read must not hold a row.
    step.assert_awaited_once()
    assert claim.await_args.kwargs == {"owner": mod.INSTANCE_ID, "ttl_s": mod.READ_LEASE_S}
    release.assert_awaited_once_with("pwf_1", owner=mod.INSTANCE_ID)


@pytest.mark.asyncio
async def test_advance_on_read_leaves_a_row_another_reader_holds_alone():
    """Two readers arriving together (or a rolling deploy's two instances) must
    not both poll Stripe and file the same card. The loser just reports."""
    held = _row(state="provisioning")
    with (
        patch.object(mod.repo, "claim", AsyncMock(return_value=None)),
        patch.object(mod.repo, "get", AsyncMock(return_value=held)),
        patch.object(mod.repo, "release", AsyncMock()) as release,
        patch.object(mod, "advance", AsyncMock()) as step,
    ):
        out = await mod.advance_on_read("pwf_1")
    assert out["state"] == "provisioning"
    step.assert_not_awaited()
    # Not ours to hand back.
    release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", sorted({"active", "failed", "requires_review", "rejected", "cancelled"}))
async def test_advance_on_read_releases_a_terminal_row_without_stepping_it(state):
    done = _row(state=state)
    with (
        patch.object(mod.repo, "claim", AsyncMock(return_value=done)),
        patch.object(mod.repo, "release", AsyncMock()) as release,
        patch.object(mod, "advance", AsyncMock()) as step,
    ):
        out = await mod.advance_on_read("pwf_1")
    assert out["state"] == state
    step.assert_not_awaited()
    release.assert_awaited_once_with("pwf_1", owner=mod.INSTANCE_ID)


@pytest.mark.asyncio
async def test_advance_on_read_honours_a_retry_after_stripe_asked_for():
    """A 429's floor. The read still answers — it just does not spend the call
    Stripe told us to wait for."""
    soon = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
    row = _row(state="provisioning", next_poll_after=soon)
    with (
        patch.object(mod.repo, "claim", AsyncMock(return_value=row)),
        patch.object(mod.repo, "release", AsyncMock()) as release,
        patch.object(mod, "advance", AsyncMock()) as step,
    ):
        out = await mod.advance_on_read("pwf_1")
    assert out["state"] == "provisioning"
    step.assert_not_awaited()
    release.assert_awaited_once_with("pwf_1", owner=mod.INSTANCE_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["2020-01-01T00:00:00+00:00", "not-a-date", None])
async def test_a_past_or_junk_retry_after_is_no_floor_at_all(when):
    """A silly header must not be able to park a workflow nothing else visits."""
    row = _row(state="provisioning", next_poll_after=when)
    with (
        patch.object(mod.repo, "claim", AsyncMock(return_value=row)),
        patch.object(mod.repo, "release", AsyncMock()),
        patch.object(mod, "advance", AsyncMock(return_value=_row(state="binding"))) as step,
    ):
        await mod.advance_on_read("pwf_1")
    step.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_refusal_parks_the_row_on_the_read_that_found_it():
    """A door the state machine will keep finding shut — the owner disconnected
    the company Stripe account. Re-reading it forever is noise."""
    row = _row(state="connecting_provider")
    refusal = mod.WorkflowRefused("no_company_stripe_account", "Set up your company's Stripe account first")
    with (
        patch.object(mod.repo, "claim", AsyncMock(return_value=row)),
        patch.object(mod.repo, "release", AsyncMock()) as release,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
        patch.object(mod, "advance", AsyncMock(side_effect=refusal)),
    ):
        out = await mod.advance_on_read("pwf_1")
    assert out["state"] == "requires_review"
    assert move.await_args.kwargs["fields"]["error_code"] == "no_company_stripe_account"
    # Friday's facts must stop saying "in flight" the moment it is parked.
    mod.owner_facts.write_owner_facts.assert_awaited_once_with("org_a")
    release.assert_awaited_once_with("pwf_1", owner=mod.INSTANCE_ID)


@pytest.mark.asyncio
async def test_a_step_that_blows_up_still_answers_the_read_and_frees_the_row():
    row = _row(state="provisioning")
    with (
        patch.object(mod.repo, "claim", AsyncMock(return_value=row)),
        patch.object(mod.repo, "release", AsyncMock()) as release,
        patch.object(mod.repo, "transition", AsyncMock()) as move,
        patch.object(mod, "advance", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        out = await mod.advance_on_read("pwf_1")
    assert out["state"] == "provisioning"
    # A blip is not a reason to park a workflow; only a refusal is.
    move.assert_not_awaited()
    release.assert_awaited_once_with("pwf_1", owner=mod.INSTANCE_ID)


# --- the consent card is answered in Paperclip, so a read has to look --------


@pytest.mark.asyncio
async def test_a_read_resumes_a_card_the_owner_approved_on_their_phone():
    row = _row(state="awaiting_approval", approval_id="apr_1", usage_limit={"max_amount": 2000})
    with (
        patch.object(mod, "shared_admin_client", return_value=_admin({"id": "apr_1", "status": "approved"})),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock(return_value=_row(state="connecting_provider"))) as resumed,
    ):
        out = await mod.advance(row)
    assert out["state"] == "connecting_provider"
    assert resumed.await_args.kwargs["approved_by"] == "paperclip"
    # An approver cannot edit a card's payload in Paperclip, so this path can
    # only honour the limit the card was filed with.
    assert resumed.await_args.kwargs["usage_limit"] == {"max_amount": 2000}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("card_state", "called"),
    [
        ("awaiting_approval", None),
        ("connecting_provider", "on_approved"),
        ("active", "on_approved"),
        ("rejected", "on_rejected"),
    ],
)
async def test_a_bundled_request_follows_its_apps_card(card_state, called):
    row = _row(workflow_id="pwf_2", state="awaiting_approval", bundled_with="pwf_1")
    card = _row(state=card_state, approved_by="user_1", usage_limit={"max_amount": 100})
    with (
        patch.object(mod.repo, "get", AsyncMock(return_value=card)),
        patch.object(mod, "on_approved", AsyncMock(return_value=_row(state="connecting_provider"))) as approved,
        patch.object(mod, "on_rejected", AsyncMock(return_value=_row(state="rejected"))) as rejected,
    ):
        await mod._reconcile_approval(row)
    assert approved.await_count == (called == "on_approved")
    assert rejected.await_count == (called == "on_rejected")
    if called == "on_approved":
        assert approved.await_args.kwargs == {"usage_limit": {"max_amount": 100}, "approved_by": "user_1"}


@pytest.mark.asyncio
async def test_a_bundled_request_files_no_card_and_posts_no_line():
    with patch.object(mod.apps_room, "post_approval_line", AsyncMock()) as line:
        await mod._approved_line("user_1", _row(bundled_with="pwf_1"))
    line.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_read_records_a_rejection_as_a_terminal_state():
    """A declined card must stop the workflow, not be re-read for a week."""
    row = _row(state="awaiting_approval", approval_id="apr_1")
    with (
        patch.object(mod, "shared_admin_client", return_value=_admin({"id": "apr_1", "status": "rejected"})),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock()) as resumed,
        patch.object(mod, "on_rejected", AsyncMock(return_value=_row(state="rejected"))) as declined,
    ):
        out = await mod.advance(row)
    assert out["state"] == "rejected"
    declined.assert_awaited_once_with("pwf_1")
    resumed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "revision_requested", None])
async def test_a_read_leaves_an_undecided_card_alone(status):
    row = _row(state="awaiting_approval", approval_id="apr_1")
    with (
        patch.object(mod, "shared_admin_client", return_value=_admin({"id": "apr_1", "status": status})),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock()) as resumed,
        patch.object(mod, "on_rejected", AsyncMock()) as declined,
    ):
        assert (await mod.advance(row))["state"] == "awaiting_approval"
    resumed.assert_not_awaited()
    declined.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_paperclip_outage_leaves_the_row_exactly_as_it_was():
    """Transient by construction: the next read looks again."""
    from core.services.paperclip_admin_client import PaperclipApiError

    row = _row(state="awaiting_approval", approval_id="apr_1")
    for error in (PaperclipApiError("GET /api/approvals/apr_1 -> 500", 500, "boom"), httpx.ConnectError("down")):
        admin = _admin(None)
        admin.get_approval = AsyncMock(side_effect=error)
        with (
            patch.object(mod, "shared_admin_client", return_value=admin),
            patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
            patch.object(mod, "on_approved", AsyncMock()) as resumed,
        ):
            assert (await mod.advance(row))["state"] == "awaiting_approval"
        resumed.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_row_with_no_card_to_read_is_left_alone():
    with patch.object(mod, "shared_admin_client") as client:
        out = await mod.advance(_row(state="awaiting_approval"))
    assert out["state"] == "awaiting_approval"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_ask_is_still_a_no_op():
    """`start` is what moves an `ask` row; a read must never file its card."""
    with patch.object(mod, "shared_admin_client") as client:
        assert (await mod.advance(_row(state="ask")))["state"] == "ask"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_start_matches_mixed_case_wire_provider_names():
    """The wire provider_name is mixed-case ("Sentry") while request bodies
    carry lowercase slugs; the match must be case-insensitive or every
    capitalized provider is unfilable (seen live on dev)."""
    svc = {**SERVICE, "provider_name": "Sentry", "service_id": "developer"}
    with (
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[svc])) as cs,
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
    ):
        service, provider = await mod._read_catalog("acct_1", "sentry", "developer")
    assert service["service_id"] == "developer"
    # unfiltered read — no provider_name kwarg reaches the client
    assert cs.await_args.kwargs.get("provider_name") is None


@pytest.mark.asyncio
async def test_read_catalog_none_provider_refuses_cleanly():
    """Schemathesis found a 500: a fuzzed/malformed workflow row can reach
    _read_catalog with provider_name=None; .lower() on it crashed. Must be a
    clean service_unavailable refusal."""
    with (
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
    ):
        with pytest.raises(mod.WorkflowRefused):
            await mod._read_catalog("acct_1", None, "analytics")


def test_a_connection_request_is_complete_when_its_connection_is_active_even_with_no_request_status():
    """`request_status` is "unset once the workflow completes"."""
    done = {"id": "pcr_1", "provider_connection": {"id": "c_1", "provider": "prvdr_1", "status": "active"}}
    assert mod.connection_request_status(done) == "complete"
    assert mod.connection_request_status({"id": "pcr_1", "request_status": "pending_auth"}) == "pending_auth"
    expired = {"request_status": "requested", "provider_connection": {"status": "expired"}}
    assert mod.connection_request_status(expired) == "requested"


def test_resource_status_accepts_both_documented_spellings():
    assert mod.resource_status({"status": "complete"}) == "complete"
    assert mod.resource_status({"status": "resource_status_complete"}) == "complete"
    assert mod.resource_status({}) is None


@pytest.mark.asyncio
async def test_resource_not_complete_on_reveal_polls_again_instead_of_failing():
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    err = ProvisioningError("resource_not_complete", "not yet")
    with (
        patch.object(
            mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "resource_status_complete"})
        ),
        patch.object(mod.api, "reveal_access_configuration", AsyncMock(side_effect=err)),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="binding"))),
        patch.object(mod.repo, "patch", AsyncMock(return_value=row)) as bump,
    ):
        await mod.advance(row)
    assert bump.await_args[0][1]["attempts"] == 1


@pytest.mark.asyncio
async def test_start_refuses_a_pasted_api_key_account(monkeypatch):
    monkeypatch.setattr(
        mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"company_stripe_account_id": "acct_1", "company_stripe_source": "api_key"}),
    )
    with pytest.raises(mod.WorkflowRefused) as exc:
        await mod.start(
            owner_id="org_a", provider_name="posthog", service_id="svc_analytics", configuration=CONFIG, issue_id=None
        )
    assert exc.value.code == "services_account_needed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row, expected",
    [
        ({"company_stripe_account_id": "acct_main", "company_stripe_source": "created"}, "acct_main"),
        (
            {
                "company_stripe_account_id": "acct_main",
                "company_stripe_source": "connected",
                "company_stripe_projects_account_id": "acct_svc",
            },
            "acct_svc",
        ),
        (
            {
                "company_stripe_account_id": "acct_main",
                "company_stripe_source": "api_key",
                "company_stripe_projects_account_id": "acct_svc",
            },
            "acct_svc",
        ),
    ],
)
async def test_account_id_for_resolves_the_account_projects_runs_on(monkeypatch, row, expected):
    monkeypatch.setattr(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))
    assert await mod.account_id_for("org_a") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["connected", "api_key"])
async def test_account_id_for_refuses_without_a_services_account(monkeypatch, source):
    """A connected v1 Standard account is refused by Accounts v2 and by
    Projects (prod 2026-10-27), so it needs a services account."""
    monkeypatch.setattr(
        mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"company_stripe_account_id": "acct_main", "company_stripe_source": source}),
    )
    with pytest.raises(mod.WorkflowRefused) as exc:
        await mod.account_id_for("org_a")
    assert exc.value.code == "services_account_needed"


@pytest.mark.asyncio
async def test_account_id_for_refuses_with_no_binding(monkeypatch):
    monkeypatch.setattr(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={}))
    with pytest.raises(mod.WorkflowRefused) as exc:
        await mod.account_id_for("org_a")
    assert exc.value.code == "no_company_stripe_account"


# --- apps: one Stripe Project per app ---------------------------------------

APP = {"app_id": "app_1", "owner_id": "org_a", "name": "crew-schedule", "stripe_project_id": None}
_READY = {"is_eligible": True, "requirements": []}


def _start_app(**kw):
    return mod.start(
        owner_id="org_a", provider_name="posthog", service_id="svc_analytics", configuration=CONFIG, issue_id=None, **kw
    )


@pytest.mark.asyncio
async def test_app_row_gets_its_own_project_named_after_the_app(monkeypatch):
    set_company = AsyncMock()
    monkeypatch.setattr(mod.billing_repo, "set_company_stripe_project_id", set_company)
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value=APP)),
        patch.object(mod.apps_repo, "set_stripe_project_id", AsyncMock(return_value=True)) as store,
        patch.object(mod.api, "create_project", AsyncMock(return_value={"id": "proj_app"})) as created,
    ):
        assert await mod._project_for("acct_1", _approved(app_id="app_1", environment="prod")) == ("proj_app", None)
    assert created.await_args.kwargs["name"] == "crew-schedule"
    store.assert_awaited_once_with("app_1", "proj_app")
    # The company's Project (proj_1 on the billing row) is neither used nor touched.
    set_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_app_with_a_project_reuses_it():
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value={**APP, "stripe_project_id": "proj_app"})),
        patch.object(mod.api, "create_project", AsyncMock()) as created,
    ):
        assert await mod._project_for("acct_1", _approved(app_id="app_1")) == ("proj_app", None)
    created.assert_not_awaited()


@pytest.mark.asyncio
async def test_losing_the_app_project_race_uses_the_winners():
    reads = AsyncMock(side_effect=[APP, {**APP, "stripe_project_id": "proj_winner"}])
    with (
        patch.object(mod.apps_repo, "get", reads),
        patch.object(mod.apps_repo, "set_stripe_project_id", AsyncMock(return_value=False)),
        patch.object(mod.api, "create_project", AsyncMock(return_value={"id": "proj_mine"})),
    ):
        assert await mod._project_for("acct_1", _approved(app_id="app_1")) == ("proj_winner", None)
    assert reads.await_args.kwargs == {"consistent_read": True}


@pytest.mark.asyncio
async def test_company_row_still_uses_company_project():
    with (
        patch.object(mod.apps_repo, "get", AsyncMock()) as app_read,
        patch.object(mod.api, "create_project", AsyncMock()) as created,
    ):
        assert await mod._project_for("acct_1", _approved()) == ("proj_1", None)
    app_read.assert_not_awaited()
    created.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_records_the_apps_project_not_the_companys():
    row = _approved(state="provisioning", resource_id="res_1", app_id="app_1", environment="prod")
    env_info = {"resource": "res_1", "configuration": {"TOKEN": "t"}}
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value={**APP, "stripe_project_id": "proj_app"})),
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete"})),
        patch.object(mod.api, "reveal_access_configuration", AsyncMock(return_value=env_info)),
        patch.object(mod, "land_credentials", AsyncMock()) as land,
        patch.object(mod.repo, "transition", AsyncMock(side_effect=[_row(state="binding"), _row(state="active")])),
    ):
        await mod.advance(row)
    assert land.await_args.kwargs["resource"]["project_id"] == "proj_app"


@pytest.mark.asyncio
async def test_bind_lands_an_app_credential_under_the_app_not_the_company():
    row = _approved(state="provisioning", resource_id="res_1", app_id="app_1", environment="prod")
    env_info = {"resource": "res_1", "configuration": {"project_id": "p", "token": "t"}}
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value={**APP, "stripe_project_id": "proj_app"})),
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete"})),
        patch.object(mod.api, "reveal_access_configuration", AsyncMock(return_value=env_info)),
        patch.object(mod, "land_credentials", AsyncMock()) as land,
        patch.object(mod.repo, "transition", AsyncMock(side_effect=[_row(state="binding"), _row(state="active")])),
    ):
        await mod.advance(row)
    kw = land.await_args.kwargs
    assert kw["land_in_profiles"] is False
    # One key per RESOURCE: Neon and Vercel both reveal `project_id`.
    assert kw["secret_provider"] == "apps/app_1/posthog/prod"
    assert {k: kw["resource"][k] for k in ("level", "app_id", "environment")} == {
        "level": "app",
        "app_id": "app_1",
        "environment": "prod",
    }
    assert mod.work_products.register.await_args.kwargs["metadata"] == {
        "workflow_id": "pwf_1",
        "level": "app",
        "app_id": "app_1",
        "environment": "prod",
    }


@pytest.mark.asyncio
async def test_create_resource_threads_environment():
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value={**APP, "stripe_project_id": "proj_app"})),
        patch.object(mod.api, "create_resource", AsyncMock(return_value={"id": "res_1"})) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="provisioning"))),
    ):
        await mod._create_resource("acct_1", _approved(state="connecting_provider", app_id="app_1", environment="prod"))
    assert create.await_args.kwargs["environment"] == "prod"
    assert create.await_args.kwargs["project"] == "proj_app"


@pytest.mark.asyncio
async def test_a_company_row_sends_no_environment_as_before():
    with (
        patch.object(mod.api, "create_resource", AsyncMock(return_value={"id": "res_1"})) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="provisioning"))),
    ):
        await mod._create_resource("acct_1", _approved(state="connecting_provider", environment="prod"))
    assert create.await_args.kwargs.get("environment") is None


@pytest.mark.asyncio
async def test_two_apps_same_service_do_not_reuse_a_workflow():
    open_a = _row(state="awaiting_approval", app_id="app_a", environment="prod")
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value={**APP, "app_id": "app_b"})),
        patch.object(mod.api, "eligibility", AsyncMock(return_value=_READY)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=[open_a])),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row(app_id="app_b"))) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))),
    ):
        row = await _start_app(app_id="app_b")
    assert "reused" not in row
    assert create.await_args.kwargs["app_id"] == "app_b"
    assert create.await_args.kwargs["environment"] == "prod"


@pytest.mark.asyncio
async def test_same_app_can_have_prod_and_dev_databases():
    open_prod = _row(state="awaiting_approval", app_id="app_1", environment="prod")
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value=APP)),
        patch.object(mod.api, "eligibility", AsyncMock(return_value=_READY)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=[open_prod])),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row(app_id="app_1", environment="dev"))) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))) as move,
    ):
        row = await _start_app(app_id="app_1", environment="dev")
    assert "reused" not in row
    assert create.await_args.kwargs["environment"] == "dev"
    # The owner's card names the app and which side of it this is for.
    assert move.await_args.kwargs["fields"]["approval"]["environment"] == "crew-schedule — preview"


@pytest.mark.asyncio
async def test_the_same_app_and_environment_still_reuses_the_open_workflow():
    open_prod = _row(state="awaiting_approval", app_id="app_1", environment="prod")
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value=APP)),
        patch.object(mod.api, "eligibility", AsyncMock(return_value=_READY)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=[open_prod])),
        patch.object(mod.repo, "create", AsyncMock()) as create,
    ):
        row = await _start_app(app_id="app_1")
    assert row["reused"] is True
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_app_row_in_a_new_app_says_it_creates_a_project_and_names_the_app():
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value=APP)),
        patch.object(mod.api, "eligibility", AsyncMock(return_value=_READY)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row(app_id="app_1"))),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))) as move,
    ):
        await _start_app(app_id="app_1")
    snapshot = move.await_args.kwargs["fields"]["approval"]
    # The company HAS a Project (proj_1); the app does not, so this one makes it.
    assert snapshot["creates_project"] is True
    assert snapshot["environment"] == "crew-schedule — live"
    assert snapshot["action"] == "creates new, and a Project for crew-schedule"


@pytest.mark.asyncio
async def test_start_refuses_an_unknown_app():
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value=None)),
        patch.object(mod.api, "eligibility", AsyncMock(return_value=_READY)),
        patch.object(mod.repo, "create", AsyncMock()) as create,
    ):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await _start_app(app_id="app_nope")
    assert exc.value.code == "app_not_found"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_owners_app_is_app_not_found():
    theirs = AsyncMock(return_value={**APP, "owner_id": "org_b"})
    with (
        patch.object(mod.apps_repo, "get", theirs),
        patch.object(mod.api, "eligibility", AsyncMock(return_value=_READY)),
        patch.object(mod.repo, "create", AsyncMock()) as create,
    ):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await _start_app(app_id="app_1")
    assert exc.value.code == "app_not_found"
    create.assert_not_awaited()
    with (
        patch.object(mod.apps_repo, "get", theirs),
        patch.object(mod, "_fail", AsyncMock(return_value={"state": "failed"})) as fail,
        patch.object(mod.api, "create_project", AsyncMock()) as mint,
    ):
        assert await mod._app_project_for("acct_1", _approved(app_id="app_1")) == (None, {"state": "failed"})
    assert fail.await_args.args[1] == "app_not_found"
    mint.assert_not_awaited()


# --- catalog re-read before the resource write ------------------------------


def _paid(text):
    return {**SERVICE, "pricing": {"type": "paid", "paid_pricing": [{"type": "freeform", "freeform": text}]}}


@pytest.mark.asyncio
async def test_catalog_drift_before_resource_post_parks_for_review():
    row = _row(state="connecting_provider", approval=mod.snapshot_of(_paid("$20/mo"), PROVIDER, CONFIG))
    with (
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[_paid("$200/mo")])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.api, "_raw", AsyncMock()) as raw,
        patch.object(mod.api, "create_resource", AsyncMock()) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        out = await mod._create_resource("acct_1", row)
    assert out["state"] == "requires_review"
    assert move.await_args.kwargs["fields"]["error_code"] == "catalog_changed"
    create.assert_not_awaited()
    raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_catalog_before_app_resource_post_still_creates():
    app = {**APP, "stripe_project_id": "proj_app"}
    approval = mod.snapshot_of(SERVICE, PROVIDER, CONFIG, environment="crew-schedule — live")
    row = _row(state="connecting_provider", approval=approval, app_id="app_1", environment="prod")
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value=app)),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod.api, "create_resource", AsyncMock(return_value={"id": "res_1"})) as create,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="provisioning"))),
    ):
        await mod._create_resource("acct_1", row)
    create.assert_awaited_once()


# --- R80: the app budget is not a provider limit ---------------------------------


async def _approve_app_row(service):
    approval = mod.snapshot_of(service, PROVIDER, CONFIG, environment="crew-schedule — live")
    row = _row(state="awaiting_approval", approval=approval, app_id="app_1", environment="prod")
    with (
        patch.object(mod.repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.apps_repo, "get", AsyncMock(return_value={**APP, "budget_usd": 10})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[service])),
        patch.object(mod.api, "payment_profile", AsyncMock(return_value={"id": "pp_1"})),
        patch.object(mod.api, "update_limit", AsyncMock(return_value={})) as limit,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="connecting_provider"))) as move,
    ):
        await mod.on_approved("pwf_1", usage_limit=None, approved_by="user_admin")
    return limit, move


@pytest.mark.asyncio
async def test_paid_app_resource_sets_no_provider_limit_from_budget():
    """R80: the provider limit is account-wide; the app's budget_usd never becomes one."""
    limit, move = await _approve_app_row(_paid("$20/mo"))
    limit.assert_not_awaited()
    assert move.await_args.kwargs["to_state"] == "connecting_provider"


@pytest.mark.asyncio
async def test_free_app_resource_sets_no_limit():
    limit, move = await _approve_app_row(SERVICE)
    limit.assert_not_awaited()
    assert move.await_args.kwargs["to_state"] == "connecting_provider"


# --- approval wakes the app's room -----------------------------------------


@pytest.mark.asyncio
async def test_reconcile_approval_posts_line_with_paperclip_user():
    """Paperclip has no Clerk user: "paperclip" is no room member, so the line
    is authored by the room's creator."""
    from core.services import apps_room

    row = _row(state="awaiting_approval", approval_id="apr_1", app_id="app_1", environment="prod")
    moved = {**row, "state": "connecting_provider"}

    async def member(owner_id, user_id, room_id):
        if user_id != "user_creator":
            raise apps_room.messaging.ApiProblem("ROOM_NOT_FOUND", "Room not found.")
        return {}, {}

    with (
        patch.object(mod, "shared_admin_client", return_value=_admin({"id": "apr_1", "status": "approved"})),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod.repo, "get", AsyncMock(return_value=row)),
        patch.object(mod, "_fresh_snapshot", AsyncMock(return_value=row.get("approval") or {})),
        patch.object(mod.repo, "transition", AsyncMock(return_value=moved)),
        patch.object(apps_room.apps_repo, "get", AsyncMock(return_value={**APP, "room_id": "room_1"})),
        patch.object(apps_room.messaging, "require_room_member", member),
        patch.object(
            apps_room.messaging_repo,
            "list_members",
            AsyncMock(return_value=[{"kind": "human", "id": "user_creator", "role": "owner"}]),
        ),
        patch.object(apps_room.messaging, "create_human_message", AsyncMock()) as post,
    ):
        out = await mod.advance(row)
    assert out["state"] == "connecting_provider"
    post.assert_awaited_once()
    auth, room_id, request = post.await_args.args
    assert (auth.user_id, auth.org_id, room_id) == ("user_creator", "org_a", "room_1")
    assert request.text == "Approved posthog/svc_analytics for crew-schedule (live). Setting it up."
    assert post.await_args.kwargs == {"via": "apps", "wake": True}


# --- final review wave ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_removed_app_gets_no_new_workflow():
    """R54: a destroyed app is missing."""
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value={**APP, "state": "destroyed"})),
        patch.object(mod.api, "eligibility", AsyncMock(return_value=_READY)),
        patch.object(mod.repo, "create", AsyncMock()) as create,
    ):
        with pytest.raises(mod.WorkflowRefused) as exc:
            await _start_app(app_id="app_1")
    assert exc.value.code == "app_not_found"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_resource_is_created_for_a_removed_app():
    """R54: a row still stepping after its app was removed fails before the billable create."""
    gone = {**APP, "state": "destroyed", "stripe_project_id": "proj_app"}
    with (
        patch.object(mod.apps_repo, "get", AsyncMock(return_value=gone)),
        patch.object(mod.api, "create_resource", AsyncMock(return_value={"id": "res_1"})) as create,
        patch.object(mod, "_fail", AsyncMock(return_value={"state": "failed"})) as failed,
    ):
        await mod._create_resource("acct_1", _approved(state="connecting_provider", app_id="app_1", environment="prod"))
    create.assert_not_awaited()
    assert failed.await_args.args[1] == "app_not_found"


@pytest.mark.asyncio
async def test_the_winning_approval_drives_its_app_without_a_reader():
    """Run 7: the agent filed the card early and kept building, so nobody read after the approval."""
    from core.services import apps_service

    with (
        patch.object(mod.apps_room, "post_approval_line", AsyncMock()),
        patch.object(apps_service, "drive") as drive,
    ):
        await mod._approved_line("user_1", _row(app_id="app_1"))
        await mod._approved_line("user_1", _row(app_id="app_1", bundled_with="pwf_1"))
        await mod._approved_line("user_1", None)
    drive.assert_called_once_with(_row()["owner_id"], "app_1")


async def test_a_preapproved_free_service_files_no_card_and_posts_no_approved_line():
    """Founder ruling 4 Nov 2026: the workspace approves free hosting and data once; later apps get no card."""
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[SERVICE])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod, "_owned_app", AsyncMock(return_value=True)),
        patch.object(mod, "_creates_project", AsyncMock(return_value=False)),
        patch.object(mod, "_card_lines", AsyncMock(return_value={})),
        patch.object(mod.repo, "list_for_owner", AsyncMock(return_value=[])),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row(app_id="app_2"))),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval", app_id="app_2"))),
        patch.object(mod, "on_approved", AsyncMock(return_value=_row(state="connecting_provider"))) as approved,
    ):
        row = await mod.start(
            owner_id="org_a",
            provider_name="posthog",
            service_id="svc_analytics",
            configuration=CONFIG,
            issue_id=None,
            app_id="app_2",
            preapproved_by="user_owner",
        )
    assert row["state"] == "connecting_provider"
    mod.provisioning_approvals.request_provider_consent.assert_not_awaited()
    assert approved.await_args.kwargs == {"usage_limit": None, "approved_by": "user_owner", "quiet": True}


async def test_a_paid_service_still_gets_its_card_even_with_a_standing_approval():
    paid = {**SERVICE, "pricing": {**(SERVICE.get("pricing") or {}), "type": "paid"}}
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=[paid])),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=[PROVIDER])),
        patch.object(mod, "snapshot_of", lambda *a, **k: {"pricing_type": "paid"}),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_approval"))),
        patch.object(mod, "on_approved", AsyncMock()) as approved,
    ):
        await mod.start(
            owner_id="org_a",
            provider_name="posthog",
            service_id="svc_analytics",
            configuration=CONFIG,
            issue_id="iss_1",
            preapproved_by="user_owner",
        )
    approved.assert_not_awaited()
    mod.provisioning_approvals.request_provider_consent.assert_awaited_once()


async def test_a_person_approving_an_apps_free_card_approves_every_later_app():
    from core.services import apps_service

    free = {"pricing_type": "component"}
    with patch.object(mod.apps_room, "post_approval_line", AsyncMock()), patch.object(apps_service, "drive"):
        await mod._approved_line("user_1", _row(app_id="app_1", approval=free))
        await mod._approved_line("user_1", _row(app_id="app_2", approval=free), quiet=True)  # by the standing one
        await mod._approved_line("user_1", _row(approval=free))  # not an app's card
        await mod._approved_line("user_1", _row(app_id="app_3", approval={"pricing_type": "paid"}))
    (call,) = mod.billing_repo.set_apps_standing_approval.await_args_list
    assert call.args[0] == _row()["owner_id"] and call.args[1]["approved_by"] == "user_1"
