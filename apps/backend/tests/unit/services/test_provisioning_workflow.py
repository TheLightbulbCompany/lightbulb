"""The loop's rules. Every Stripe call is patched at the client module, so no
network and no fixtures beyond dicts shaped like spec.txt's objects."""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import provisioning_workflow as mod  # noqa: E402
from core.services.stripe_provisioning import ProvisioningError  # noqa: E402

SERVICE = {
    "object": "v2.provisioning.provider_service",
    "id": "psvc_1",
    "provider_id": "prvdr_1",
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
        "project_id": "proj_1",
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def account(monkeypatch):
    monkeypatch.setattr(
        mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_a", "company_stripe_account_id": "acct_1"}),
    )


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
    monkeypatch.setattr(mod, "write_owner_facts", AsyncMock())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing here may reach DynamoDB or Stripe. Three calls are made by paths
    under test without being their subject: the open-workflow lookup, the
    non-state field writes, and the payment-profile probe behind the consent
    card's editable spending limit. Tests that assert on them patch them."""
    monkeypatch.setattr(mod.repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod.repo, "patch", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.api, "payment_profile", AsyncMock(side_effect=ProvisioningError("not_found", "no profile")))


# --- start ------------------------------------------------------------------


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
async def test_two_concurrent_asks_make_one_workflow_and_one_card():
    """Friday retrying after a timeout must not open a second card — or, once
    approved, provision the same resource twice."""
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
    row = _row(state="awaiting_approval", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
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
        mod.repo, "get", AsyncMock(return_value=_row(state="awaiting_user_action", account_request_id="ar_1"))
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


# --- connecting_provider ----------------------------------------------------


@pytest.mark.asyncio
async def test_active_connection_skips_straight_to_provisioning():
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(
            mod.api, "list_provider_connections", AsyncMock(return_value=[{"provider": "prvdr_1", "status": "active"}])
        ),
        patch.object(mod.api, "create_resource", AsyncMock(return_value={"id": "res_1", "status": "pending"})),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="provisioning", resource_id="res_1"))),
    ):
        out = await mod.advance(row)
    assert out["state"] == "provisioning"


@pytest.mark.asyncio
async def test_pending_auth_files_a_connect_card_and_waits():
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(mod.api, "list_provider_connections", AsyncMock(return_value=[])),
        patch.object(
            mod.api,
            "create_account_request",
            AsyncMock(
                return_value={"id": "ar_1", "status": "pending_auth", "redirect_url": "https://provider.example/oauth"}
            ),
        ),
        patch.object(mod.repo, "patch", AsyncMock(side_effect=lambda wid, f: {**row, **f})) as patched,
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="awaiting_user_action"))) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "awaiting_user_action"
    # The account request id is persisted BEFORE the card opens any redirect.
    assert patched.await_args_list[0].args[1]["account_request_id"] == "ar_1"
    assert move.await_args.kwargs["fields"]["awaiting"] == "provider_connect"
    # The URL is NOT put on the card — the connect-link route mints it on click.
    kwargs = mod.provisioning_approvals.request_provider_connect.await_args.kwargs
    assert "redirect_url" not in kwargs


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
        state="connecting_provider", account_request_id="ar_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG)
    )
    ar = {
        "id": "ar_1",
        "status": "complete",
        "provider_account_details": {
            "id": "prv_acct_1",
            "display_name": "Northwind",
            "primary_email": "founder@example.com",
            "link_action": "linked_existing",
            "active_services": [{"service_id": "svc_analytics", "display_name": "Analytics", "status": "active"}],
        },
    }
    with (
        patch.object(mod.api, "get_account_request", AsyncMock(return_value=ar)),
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
        state="connecting_provider", account_request_id="ar_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG)
    )
    schema = {"type": "object", "properties": {"team": {"type": "string"}}}
    with (
        patch.object(
            mod.api,
            "get_account_request",
            AsyncMock(return_value={"id": "ar_1", "status": "needs_information", "needs_information_schema": schema}),
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
                    "status": "needs_information",
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
async def test_reauth_clears_the_account_request_id_so_a_fresh_one_is_created():
    """Without the clear, `advance` re-reads the OLD (complete) request forever
    and the new one is never created."""
    row = _row(
        state="binding",
        resource_id="res_1",
        account_request_id="ar_old",
        approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG),
    )
    with (
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete"})),
        patch.object(
            mod.api,
            "environment_info",
            AsyncMock(side_effect=ProvisioningError("provider_reauth_required", "reconnect")),
        ),
        patch.object(
            mod.repo, "transition", AsyncMock(return_value=_row(state="connecting_provider", account_request_id=None))
        ) as move,
    ):
        out = await mod.advance(row)
    assert out["state"] == "connecting_provider"
    fields = move.await_args.kwargs["fields"]
    assert fields["account_request_id"] is None
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
            "create_account_request",
            AsyncMock(
                return_value={
                    "id": "ar_1",
                    "status": "error",
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
async def test_complete_resource_binds_the_matching_resource_id_only():
    """ "The current API can scope by all, Provider, or Service, but not by one
    Resource. […] Select only the entry whose resource_id equals the ID stored
    in the […] workflow." """
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    env_info = {
        "resource_access_configurations": [
            {
                "resource_id": "res_OTHER",
                "access_configuration_keys": ["OTHER"],
                "access_configuration": {"OTHER": "nope"},
            },
            {
                "resource_id": "res_1",
                "access_configuration_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
                "access_configuration": {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live"},
            },
        ]
    }
    with (
        patch.object(
            mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete", "name": "analytics"})
        ),
        patch.object(mod.api, "environment_info", AsyncMock(return_value=env_info)),
        patch.object(mod, "land_credentials", AsyncMock()) as land,
        patch.object(mod.repo, "transition", AsyncMock(side_effect=[_row(state="binding"), _row(state="active")])),
    ):
        out = await mod.advance(row)
    assert out["state"] == "active"
    assert land.await_args.kwargs["env"] == {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live"}
    assert land.await_args.kwargs["resource"]["resource_id"] == "res_1"
    assert land.await_args.kwargs["resource"]["env_keys"] == ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]
    assert land.await_args.kwargs["resource"]["level"] == "company"
    mod.write_owner_facts.assert_awaited_once_with("org_a")


@pytest.mark.asyncio
async def test_removed_resource_never_binds_credentials():
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    with (
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "removed"})),
        patch.object(mod.api, "environment_info", AsyncMock()) as env,
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
    thin_service = {**SERVICE, "provider_id": "prvdr_9", "provider_name": "herenow"}
    assert mod.is_offerable(SERVICE, PROVIDER) is True
    assert mod.is_offerable(thin_service, thin_provider) is False
    # A development-only row never shows either.
    assert mod.is_offerable({**SERVICE, "development": True}, PROVIDER) is False


@pytest.mark.asyncio
async def test_a_stripe_5xx_on_a_READ_keeps_the_state_and_retries():
    """Preview flakiness: a 5xx is transient. It must not fail the workflow and
    must not turn into a repeated POST."""
    row = _row(state="provisioning", resource_id="res_1", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    err = ProvisioningError("provisioning_error", "api_error", http_status=500)
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
    err = ProvisioningError("provisioning_error", "api_error", http_status=500)
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


def test_poll_delay_backs_off_with_jitter_and_a_ceiling():
    delays = [mod.poll_delay(n) for n in range(0, 12)]
    assert all(d >= mod.POLL_BASE_S * 0.5 for d in delays)
    assert all(d <= mod.POLL_MAX_S for d in delays)
    assert delays[5] > delays[0]
    # Jitter: two draws at the same attempt are not identical.
    assert len({mod.poll_delay(6) for _ in range(20)}) > 1


# --- the no-repeat rule -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_timed_out_create_is_never_repeated():
    """ "Don't automatically repeat a POST after a timeout or unknown network
    outcome. Mark the workflow requires_review." """
    row = _row(state="connecting_provider", approval=mod.snapshot_of(SERVICE, PROVIDER, CONFIG))
    create = AsyncMock(side_effect=ProvisioningError("transport_error", "Could not reach Stripe; try again"))
    with (
        patch.object(
            mod.api, "list_provider_connections", AsyncMock(return_value=[{"provider": "prvdr_1", "status": "active"}])
        ),
        patch.object(mod.api, "create_resource", create),
        patch.object(mod.repo, "transition", AsyncMock(return_value=_row(state="requires_review"))) as move,
    ):
        out = await mod.advance(row)
        # Advancing the SAME row again must not fire a second create.
        await mod.advance({**row, "state": "requires_review"})
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
    env_info = {
        "resource_access_configurations": [
            {"resource_id": "res_1", "access_configuration_keys": ["K"], "access_configuration": {"K": "phc_SECRET"}}
        ]
    }
    with (
        patch.object(mod.api, "get_resource", AsyncMock(return_value={"id": "res_1", "status": "complete"})),
        patch.object(mod.api, "environment_info", AsyncMock(return_value=env_info)),
        patch.object(mod, "land_credentials", AsyncMock()),
        patch.object(mod.repo, "transition", AsyncMock(side_effect=[_row(state="binding"), _row(state="active")])),
        caplog.at_level("DEBUG"),
    ):
        await mod.advance(row)
    assert "phc_SECRET" not in caplog.text
