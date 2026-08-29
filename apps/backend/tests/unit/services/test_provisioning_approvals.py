import os
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import board_cards  # noqa: E402
from core.services import provisioning_approvals as mod  # noqa: E402

# The filing sequence moved to `board_cards.file_board_card`, so its
# collaborators are patched THERE; `mod` still owns the payloads.


def _admin(existing):
    admin = MagicMock()
    admin._http = object()
    admin.list_approvals = AsyncMock(return_value={"approvals": existing})
    admin.create_approval = AsyncMock(return_value={"id": "apr_new"})
    return admin


@contextmanager
def _board(admin, company_id="cmp_1"):
    # The brief's `with (*_board(admin),):` collapses to a single tuple (no
    # with-item splat in Python's grammar) and raises `TypeError: 'tuple'
    # object does not support the context manager protocol` — verified
    # empirically. `_board` is a context manager itself instead, entered as
    # `with _board(admin):`.
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(board_cards, "resolve_company_id_for_owner", AsyncMock(return_value=company_id))
        )
        stack.enter_context(patch.object(board_cards, "get_admin_session_cookie", AsyncMock(return_value="cookie")))
        stack.enter_context(patch.object(board_cards, "shared_admin_client", return_value=admin))
        stack.enter_context(
            patch.object(board_cards, "resolve_company_name", AsyncMock(return_value="Northwind Ceramics"))
        )
        yield


@pytest.mark.asyncio
async def test_pairing_card_carries_the_url_and_code():
    admin = _admin([])
    with _board(admin):
        approval_id = await mod.request_cli_pairing(
            owner_id="org_a",
            issue_id="iss_1",
            browser_url="https://dashboard.stripe.com/stripecli/confirm?t=abc",
            verification_code="ABCD-EFGH",
        )
    assert approval_id == "apr_new"
    body = admin.create_approval.call_args.kwargs["body"]
    assert body["type"] == "request_board_approval"
    assert body["issueIds"] == ["iss_1"]
    assert body["payload"]["isol8_action"] == "stripe_cli_login"
    assert body["payload"]["title"] == "Connect Stripe CLI for Northwind Ceramics"
    assert body["payload"]["browser_url"] == "https://dashboard.stripe.com/stripecli/confirm?t=abc"
    assert body["payload"]["verification_code"] == "ABCD-EFGH"


@pytest.mark.asyncio
async def test_pairing_card_dedupes_on_a_pending_card():
    admin = _admin(
        [
            {
                "id": "apr_old",
                "type": "request_board_approval",
                "status": "pending",
                "payload": {"isol8_action": "stripe_cli_login"},
            }
        ]
    )
    with _board(admin):
        assert (
            await mod.request_cli_pairing(
                owner_id="org_a", issue_id=None, browser_url="https://x", verification_code="Z"
            )
            == "apr_old"
        )
    admin.create_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_consent_card_carries_provider_service_terms_and_pricing():
    admin = _admin([])
    with _board(admin):
        approval_id = await mod.request_provider_consent(
            owner_id="org_a",
            issue_id=None,
            provider="posthog",
            service="analytics",
            terms_url="https://posthog.com/terms",
            pricing="Free tier — 1M events/mo",
        )
    assert approval_id == "apr_new"
    payload = admin.create_approval.call_args.kwargs["body"]["payload"]
    assert payload["isol8_action"] == "provider_consent"
    assert payload["title"] == "Approve posthog analytics for Northwind Ceramics"
    assert payload["provider"] == "posthog"
    assert payload["service"] == "analytics"
    assert payload["terms_url"] == "https://posthog.com/terms"
    assert payload["pricing"] == "Free tier — 1M events/mo"


@pytest.mark.asyncio
async def test_consent_dedupe_is_per_provider_service_not_global():
    """A pending consent card for Vercel must not suppress a PostHog one."""
    admin = _admin(
        [
            {
                "id": "apr_vercel",
                "type": "request_board_approval",
                "status": "pending",
                "payload": {"isol8_action": "provider_consent", "provider": "vercel", "service": "hosting"},
            }
        ]
    )
    with _board(admin):
        new_id = await mod.request_provider_consent(
            owner_id="org_a",
            issue_id=None,
            provider="posthog",
            service="analytics",
            terms_url="https://posthog.com/terms",
            pricing="free",
        )
        same_id = await mod.request_provider_consent(
            owner_id="org_a",
            issue_id=None,
            provider="vercel",
            service="hosting",
            terms_url="https://vercel.com/legal",
            pricing="paid",
        )
    assert new_id == "apr_new"
    assert same_id == "apr_vercel"
    assert admin.create_approval.await_count == 1


@pytest.mark.asyncio
async def test_no_paperclip_company_is_none_not_an_error():
    admin = _admin([])
    with (
        patch.object(board_cards, "resolve_company_id_for_owner", AsyncMock(return_value=None)),
        patch.object(board_cards, "shared_admin_client", return_value=admin),
    ):
        assert (
            await mod.request_cli_pairing(
                owner_id="org_a", issue_id=None, browser_url="https://x", verification_code="Z"
            )
            is None
        )
    admin.create_approval.assert_not_awaited()
