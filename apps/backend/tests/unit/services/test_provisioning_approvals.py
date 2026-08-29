"""The four cards the owner clicks. Only the PAYLOADS live here — the filing
sequence (company gate, cookie, dedupe, create) is `board_cards`' and is tested
in `test_company_stripe_approval.py`, so `file_board_card` is patched out."""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import provisioning_approvals as mod  # noqa: E402

SNAP = {
    "provider_id": "prvdr_1",
    "provider_name": "posthog",
    "service_ref": "svc_analytics",
    "configuration": {"region": "us"},
    "pricing_type": "free",
    "paid_pricing": [],
    "terms_url": "https://posthog.com/terms",
    "website_url": "https://posthog.com",
    "description": "Product analytics",
    "scope": "project",
    "kind": "deployable",
    "configuration_schema": {"type": "object"},
    "availability": "PROVIDER_SERVICE_AVAILABILITY_AVAILABLE",
}


def _filed():
    return patch.object(mod, "file_board_card", AsyncMock(return_value="apr_1"))


def _payload(filer, company="Northwind Ceramics"):
    return filer.await_args.kwargs["build_payload"](company)


@pytest.mark.asyncio
async def test_consent_card_carries_the_snapshot_and_an_editable_limit():
    with _filed() as filer:
        assert (
            await mod.request_provider_consent(owner_id="org_a", issue_id="iss_1", workflow_id="pwf_1", snapshot=SNAP)
            == "apr_1"
        )
    assert filer.await_args.kwargs["marker"] == "provider_consent"
    assert filer.await_args.kwargs["dedupe"] == {"workflow_id": "pwf_1"}
    assert filer.await_args.kwargs["issue_id"] == "iss_1"
    p = _payload(filer)
    assert p["title"] == "Approve posthog svc_analytics for Northwind Ceramics"
    assert p["workflow_id"] == "pwf_1"
    assert p["provider"] == "posthog"
    assert p["service"] == "svc_analytics"
    assert p["terms_url"] == "https://posthog.com/terms"
    assert p["website_url"] == "https://posthog.com"
    assert p["configuration"] == {"region": "us"}
    assert p["pricing_type"] == "free"
    # A free service still carries a limit the owner may edit before approving.
    assert p["usage_limit"] == mod.DEFAULT_USAGE_LIMIT
    assert p["usage_limit_editable"] is True


@pytest.mark.asyncio
async def test_consent_card_carries_the_asking_agents_reason():
    """The owner is approving spend on an agent's word — they get to read the
    sentence. Absent, it is empty, never missing: the card renders the key."""
    with _filed() as filer:
        await mod.request_provider_consent(
            owner_id="org_a",
            issue_id=None,
            workflow_id="pwf_1",
            snapshot=SNAP,
            reason="The launch needs funnel numbers by Friday.",
        )
    assert _payload(filer)["reason"] == "The launch needs funnel numbers by Friday."

    with _filed() as filer:
        await mod.request_provider_consent(owner_id="org_a", issue_id=None, workflow_id="pwf_1", snapshot=SNAP)
    assert _payload(filer)["reason"] == ""


@pytest.mark.asyncio
async def test_a_long_reason_is_capped_like_every_other_card_text():
    with _filed() as filer:
        await mod.request_provider_consent(
            owner_id="org_a", issue_id=None, workflow_id="pwf_1", snapshot=SNAP, reason="x" * 5000
        )
    assert len(_payload(filer)["reason"]) == mod.MAX_TEXT


@pytest.mark.asyncio
async def test_consent_card_carries_every_disclosure_the_guide_requires():
    """Each key here maps to one line of the guide's "Collect informed
    approval" list. A missing one makes the approval uninformed."""
    snap = {
        **SNAP,
        "privacy_policy_url": "https://posthog.com/privacy",
        "categories": ["analytics"],
        "constraints": [{"type": "count", "count": {"at_most": 1}}],
        "environment": "Company — production",
        "action": "creates new",
    }
    with _filed() as filer:
        await mod.request_provider_consent(
            owner_id="org_a", issue_id=None, workflow_id="pwf_1", snapshot=snap, limit_editable=True
        )
    p = _payload(filer)
    assert p["website_url"] == "https://posthog.com"
    assert p["privacy_policy_url"] == "https://posthog.com/privacy"
    assert p["terms_url"] == "https://posthog.com/terms"
    assert p["description"] == "Product analytics"
    assert p["categories"] == ["analytics"]
    assert p["availability"] == "PROVIDER_SERVICE_AVAILABILITY_AVAILABLE"
    assert p["scope"] == "project"
    assert p["constraints"] == [{"type": "count", "count": {"at_most": 1}}]
    assert p["configuration"] == {"region": "us"}
    assert p["environment"] == "Company — production"
    assert p["payment_owner"] == "your card, on your Stripe account"
    assert p["action"] == "creates new"


@pytest.mark.asyncio
async def test_the_limit_is_read_only_once_a_payment_profile_exists():
    with _filed() as filer:
        await mod.request_provider_consent(
            owner_id="org_a", issue_id=None, workflow_id="pwf_1", snapshot=SNAP, limit_editable=False
        )
    assert _payload(filer)["usage_limit_editable"] is False


@pytest.mark.asyncio
async def test_the_card_says_where_the_limit_is_actually_set():
    """Paperclip approvers cannot edit a card's payload, so the card is the
    RECORD of the limit, not the form for it — the copy has to say so or an
    owner types into a field that does not exist."""
    with _filed() as filer:
        await mod.request_provider_consent(owner_id="org_a", issue_id=None, workflow_id="pwf_1", snapshot=SNAP)
    assert "approv" in _payload(filer)["usage_limit_note"].lower()


@pytest.mark.asyncio
async def test_consent_card_shows_the_default_paid_price_verbatim():
    paid = {
        **SNAP,
        "pricing_type": "paid",
        "paid_pricing": [
            {"type": "freeform", "freeform": "$0/mo up to 1M events", "is_default": False},
            {"type": "freeform", "freeform": "$20/mo", "is_default": True, "description": "Team"},
        ],
    }
    with _filed() as filer:
        await mod.request_provider_consent(owner_id="org_a", issue_id=None, workflow_id="pwf_1", snapshot=paid)
    p = _payload(filer)
    # Display text, kept verbatim: "Don't derive a structured cost from
    # freeform price text."
    assert p["price_display"] == "$20/mo"
    assert p["pricing_type"] == "paid"


@pytest.mark.asyncio
async def test_connect_card_stores_no_url_at_all():
    """A card can sit for days; the URL is minted on click by the connect-link
    route. Storing it would be a stale bearer-ish URL in an Inbox."""
    with _filed() as filer:
        await mod.request_provider_connect(
            owner_id="org_a",
            issue_id=None,
            workflow_id="pwf_1",
            snapshot=SNAP,
        )
    assert filer.await_args.kwargs["marker"] == "provider_connect"
    p = _payload(filer)
    assert p["title"] == "Connect posthog for Northwind Ceramics"
    assert p["workflow_id"] == "pwf_1"
    for leaked in ("redirect_url", "checkout_session_url", "access_configuration", "provider_account"):
        assert leaked not in p


@pytest.mark.asyncio
async def test_connect_card_names_the_account_when_one_is_known():
    """Re-connecting after a reauth: the owner already has a provider account,
    and must see WHICH one this authorises before they click."""
    account = {"display_name": "Northwind", "primary_email": "ops@northwind.test", "link_action": "linked_existing"}
    with _filed() as filer:
        await mod.request_provider_connect(
            owner_id="org_a", issue_id=None, workflow_id="pwf_1", snapshot=SNAP, provider_account=account
        )
    assert _payload(filer)["provider_account"] == account


@pytest.mark.asyncio
async def test_information_card_carries_the_schema_and_its_target():
    schema = {"type": "object", "properties": {"team": {"type": "string"}}}
    with _filed() as filer:
        await mod.request_provider_information(
            owner_id="org_a",
            issue_id=None,
            workflow_id="pwf_1",
            snapshot=SNAP,
            schema=schema,
            target="resource",
        )
    assert filer.await_args.kwargs["marker"] == "provider_information"
    p = _payload(filer)
    assert p["title"] == "posthog needs more information for Northwind Ceramics"
    assert p["schema"] == schema
    assert p["target"] == "resource"


@pytest.mark.asyncio
async def test_information_dedupes_per_target_so_both_questions_get_asked():
    """The account request and the resource can each ask for information on the
    same workflow. Deduping on the workflow alone would swallow the second."""
    with _filed() as filer:
        await mod.request_provider_information(
            owner_id="org_a",
            issue_id=None,
            workflow_id="pwf_1",
            snapshot=SNAP,
            schema={"type": "object"},
            target="account_request",
        )
    assert filer.await_args.kwargs["dedupe"] == {"workflow_id": "pwf_1", "target": "account_request"}


@pytest.mark.asyncio
async def test_payment_card_carries_the_limit_and_never_a_checkout_url():
    limit = {"max_amount": 5000, "currency": "usd", "recurring_interval": "month"}
    with _filed() as filer:
        await mod.request_payment_setup(
            owner_id="org_a",
            issue_id=None,
            workflow_id="pwf_1",
            snapshot=SNAP,
            usage_limit=limit,
        )
    assert filer.await_args.kwargs["marker"] == "payment_setup"
    p = _payload(filer)
    assert p["title"] == "Add a card for posthog for Northwind Ceramics"
    assert p["usage_limit"] == limit
    # Minted on click by the owner route, never stored on the card.
    assert "checkout_session_url" not in p


@pytest.mark.asyncio
async def test_every_card_dedupes_per_workflow_not_per_provider():
    for fn, kwargs in (
        (mod.request_provider_consent, {}),
        (mod.request_provider_connect, {}),
        (mod.request_provider_information, {"schema": {"type": "object"}, "target": "resource"}),
        (
            mod.request_payment_setup,
            {"usage_limit": {"max_amount": 1, "currency": "usd", "recurring_interval": "month"}},
        ),
    ):
        with _filed() as filer:
            await fn(owner_id="org_a", issue_id=None, workflow_id="pwf_9", snapshot=SNAP, **kwargs)
        assert filer.await_args.kwargs["dedupe"]["workflow_id"] == "pwf_9"


@pytest.mark.asyncio
async def test_no_paperclip_company_is_none_not_an_error():
    with patch.object(mod, "file_board_card", AsyncMock(return_value=None)):
        assert (
            await mod.request_provider_consent(owner_id="org_a", issue_id=None, workflow_id="pwf_1", snapshot=SNAP)
            is None
        )


@pytest.mark.asyncio
async def test_untrusted_catalog_text_is_length_capped():
    """`llm_context`, descriptions and terms are untrusted catalog data; a
    10KB description must not become a 10KB approval payload."""
    huge = {
        **SNAP,
        "description": "x" * 5000,
        "paid_pricing": [{"type": "freeform", "freeform": "y" * 5000, "is_default": True}],
        "pricing_type": "paid",
    }
    with _filed() as filer:
        await mod.request_provider_consent(owner_id="org_a", issue_id=None, workflow_id="pwf_1", snapshot=huge)
    p = _payload(filer)
    assert len(p["description"]) <= mod.MAX_TEXT
    assert len(p["price_display"]) <= mod.MAX_TEXT
