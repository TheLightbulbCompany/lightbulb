"""Tests for the Clerk webhook handlers wired to PaperclipProvisioning (T12).

The webhook handler builds a fresh ``PaperclipProvisioning`` per
request via ``build_paperclip_provisioning``. We patch that factory to
hand back an ``AsyncMock`` provisioning instance so the test never
touches a real httpx client / DynamoDB repo.

Svix signature verification is bypassed via monkeypatch on the
private ``_verify_svix_signature`` symbol — same pattern used by the
existing ``test_webhooks_clerk_email_sync.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.services.paperclip_admin_client import PaperclipApiError


def _bypass_svix(monkeypatch):
    monkeypatch.setattr(
        "routers.webhooks._verify_svix_signature",
        lambda body, headers: None,
    )


def _svix_headers() -> dict:
    return {
        "svix-id": "msg_test",
        "svix-timestamp": "1234567890",
        "svix-signature": "ignored",
    }


# ----------------------------------------------------------------------
# organization.created -> provision_org
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_organization_created_does_not_provision():
    """After consolidation (Task 6), the webhook is record-keeping only.
    Should NOT call build_paperclip_provisioning, provision_org, or
    _enqueue_paperclip_retry. Provisioning fires from
    /container/status (Task 8) when readiness is satisfied.
    """
    from routers.webhooks import _handle_organization_created

    with (
        patch("routers.webhooks.build_paperclip_provisioning") as mock_factory,
        patch(
            "routers.webhooks._enqueue_paperclip_retry",
            new=AsyncMock(),
        ) as mock_retry,
    ):
        await _handle_organization_created(
            {
                "id": "org_acme",
                "created_by": "user_owner",
            }
        )

    mock_factory.assert_not_called()
    mock_retry.assert_not_awaited()


# ----------------------------------------------------------------------
# organizationMembership.created -> log + dual-tenancy invariant probe
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_membership_created_does_not_provision():
    """After consolidation (Task 7), the membership webhook is
    record-keeping + dual-tenancy invariant probe only. Should NOT call
    provision_member, build_paperclip_provisioning, or
    _enqueue_paperclip_retry. The new member's paperclip-membership row
    will be added by the first /container/status poll on their session
    via ensure_user_ready (Task 8)."""
    from routers.webhooks import _handle_organization_membership_created

    with (
        patch("routers.webhooks.build_paperclip_provisioning") as mock_factory,
        patch("routers.webhooks._enqueue_paperclip_retry", new=AsyncMock()) as mock_retry,
        patch(
            "routers.webhooks.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),  # no personal billing row -> invariant clean
        ) as mock_billing,
    ):
        await _handle_organization_membership_created(
            {
                "organization": {"id": "org_x", "created_by": "user_owner"},
                "public_user_data": {
                    "user_id": "user_member",
                    "identifier": "m@example.com",
                },
            }
        )

    mock_factory.assert_not_called()
    mock_retry.assert_not_awaited()
    # Invariant probe still runs.
    mock_billing.assert_awaited()


# ----------------------------------------------------------------------
# organizationMembership.deleted -> archive_member
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_membership_deleted_calls_archive_member(async_client, monkeypatch):
    """``organizationMembership.deleted`` should drive
    ``PaperclipProvisioning.archive_member`` (which both calls
    Paperclip's archive endpoint AND marks the DDB row disabled),
    not the bare ``disable`` shortcut.
    """
    _bypass_svix(monkeypatch)

    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(return_value=None)

    payload = {
        "type": "organizationMembership.deleted",
        "data": {
            "id": "orgm_1",
            "organization": {"id": "org_acme"},
            "public_user_data": {"user_id": "user_member"},
        },
    }

    with patch(
        "routers.webhooks.build_paperclip_provisioning",
        new=AsyncMock(return_value=mock_provisioning),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    mock_provisioning.archive_member.assert_awaited_once_with(user_id="user_member")


@pytest.mark.asyncio
async def test_membership_deleted_retryable_failure_enqueues(async_client, monkeypatch):
    """Paperclip 5xx during ``archive_member`` should enqueue a
    pending-updates retry row keyed on ``op="archive_member"``.

    Auth-bypass guard: the handler MUST also call ``disable`` BEFORE
    enqueueing the retry so the DDB row is flipped to
    ``status="disabled"`` immediately. Otherwise
    ``resolve_teams_context`` would keep authorizing the removed user
    against ``/api/v1/teams/*`` for the entire retry window (minutes
    to hours).
    """
    _bypass_svix(monkeypatch)

    err = PaperclipApiError("server-down", 503, "")
    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(side_effect=err)
    mock_provisioning.disable = AsyncMock(return_value=None)

    captured_create = AsyncMock(return_value={"update_id": "upd_archive"})

    payload = {
        "type": "organizationMembership.deleted",
        "data": {
            "id": "orgm_1",
            "organization": {"id": "org_acme"},
            "public_user_data": {"user_id": "user_member"},
        },
    }

    with (
        patch(
            "routers.webhooks.build_paperclip_provisioning",
            new=AsyncMock(return_value=mock_provisioning),
        ),
        patch("core.repositories.update_repo.create", new=captured_create),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    mock_provisioning.archive_member.assert_awaited_once_with(user_id="user_member")
    # Auth-bypass guard: DDB row must be flipped to disabled even on retryable.
    mock_provisioning.disable.assert_awaited_once_with(user_id="user_member")
    captured_create.assert_awaited_once()
    kwargs = captured_create.call_args.kwargs
    assert kwargs["owner_id"] == "user_member"
    assert kwargs["update_type"] == "paperclip_provision"
    assert kwargs["changes"]["op"] == "archive_member"
    assert kwargs["changes"]["user_id"] == "user_member"


@pytest.mark.asyncio
async def test_membership_deleted_non_retryable_failure_swallowed(async_client, monkeypatch):
    """4xx (non-429) on archive_member should NOT enqueue a retry —
    the user is already gone from Clerk, and Clerk redelivery would
    just hit the same 4xx. We still return 200 so Clerk stops retrying.

    Critically (auth-bypass guard): the handler MUST also call
    ``disable`` to mark the DDB row ``status="disabled"`` even when
    Paperclip archive failed non-retryably — otherwise the removed
    Clerk-org member can still hit ``/api/v1/teams/*`` because
    ``resolve_teams_context`` only checks DDB state.
    """
    _bypass_svix(monkeypatch)

    err = PaperclipApiError("not-found", 404, "")
    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(side_effect=err)
    mock_provisioning.disable = AsyncMock(return_value=None)
    captured_create = AsyncMock()

    payload = {
        "type": "organizationMembership.deleted",
        "data": {
            "id": "orgm_1",
            "organization": {"id": "org_acme"},
            "public_user_data": {"user_id": "user_member"},
        },
    }

    with (
        patch(
            "routers.webhooks.build_paperclip_provisioning",
            new=AsyncMock(return_value=mock_provisioning),
        ),
        patch("core.repositories.update_repo.create", new=captured_create),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    mock_provisioning.archive_member.assert_awaited_once()
    captured_create.assert_not_awaited()
    # Auth-bypass guard: DDB row must be flipped to disabled.
    mock_provisioning.disable.assert_awaited_once_with(user_id="user_member")


@pytest.mark.asyncio
async def test_membership_deleted_non_retryable_disable_failure_is_swallowed(async_client, monkeypatch):
    """If the defensive ``disable`` call after a non-retryable archive
    failure itself raises, we still return 200 — the webhook must not
    bubble exceptions back to Clerk, which would just redeliver the
    same event. The exception is logged for ops to investigate.
    """
    _bypass_svix(monkeypatch)

    err = PaperclipApiError("not-found", 404, "")
    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(side_effect=err)
    mock_provisioning.disable = AsyncMock(side_effect=RuntimeError("ddb-down"))
    captured_create = AsyncMock()

    payload = {
        "type": "organizationMembership.deleted",
        "data": {
            "id": "orgm_1",
            "organization": {"id": "org_acme"},
            "public_user_data": {"user_id": "user_member"},
        },
    }

    with (
        patch(
            "routers.webhooks.build_paperclip_provisioning",
            new=AsyncMock(return_value=mock_provisioning),
        ),
        patch("core.repositories.update_repo.create", new=captured_create),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    mock_provisioning.archive_member.assert_awaited_once()
    mock_provisioning.disable.assert_awaited_once_with(user_id="user_member")
    captured_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_membership_deleted_retryable_disable_failure_is_swallowed(async_client, monkeypatch):
    """On retryable archive failure, the handler tries ``disable``
    BEFORE enqueueing the retry to close the auth-bypass window. If
    that defensive ``disable`` itself raises (e.g. DDB transient), we
    must still enqueue the retry and return 200 — the webhook must not
    bubble exceptions back to Clerk, and we still want the retry
    pending so the next pass re-attempts archive.
    """
    _bypass_svix(monkeypatch)

    err = PaperclipApiError("server-down", 503, "")
    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(side_effect=err)
    mock_provisioning.disable = AsyncMock(side_effect=RuntimeError("ddb-down"))
    captured_create = AsyncMock(return_value={"update_id": "upd_archive"})

    payload = {
        "type": "organizationMembership.deleted",
        "data": {
            "id": "orgm_1",
            "organization": {"id": "org_acme"},
            "public_user_data": {"user_id": "user_member"},
        },
    }

    with (
        patch(
            "routers.webhooks.build_paperclip_provisioning",
            new=AsyncMock(return_value=mock_provisioning),
        ),
        patch("core.repositories.update_repo.create", new=captured_create),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    mock_provisioning.archive_member.assert_awaited_once()
    mock_provisioning.disable.assert_awaited_once_with(user_id="user_member")
    # Even when disable raises, the retry row is still enqueued so
    # the next worker pass re-attempts the Paperclip-side archive.
    captured_create.assert_awaited_once()


# ----------------------------------------------------------------------
# user.deleted -> disable
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_deleted_calls_disable(async_client, monkeypatch):
    """``user.deleted`` continues to sweep channel_links AND now
    additionally calls ``PaperclipProvisioning.disable``.
    """
    _bypass_svix(monkeypatch)

    mock_provisioning = AsyncMock()
    mock_provisioning.disable = AsyncMock(return_value=None)

    payload = {"type": "user.deleted", "data": {"id": "user_x", "deleted": True}}

    with (
        patch(
            "routers.webhooks.build_paperclip_provisioning",
            new=AsyncMock(return_value=mock_provisioning),
        ),
        patch(
            "core.repositories.channel_link_repo.sweep_by_member",
            new=AsyncMock(return_value=0),
        ),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    mock_provisioning.disable.assert_awaited_once_with(user_id="user_x")


# ----------------------------------------------------------------------
# Stripe customer.subscription.deleted -> disable
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# user.created -> persists email into users repo
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_created_persists_email(async_client, monkeypatch):
    """``user.created`` should extract the primary email from the Clerk
    payload and persist it via ``user_repo.put`` so subsequent
    organization.created webhooks can resolve the owner's email
    without round-tripping Clerk admin.
    """
    _bypass_svix(monkeypatch)

    captured_put = AsyncMock(return_value={"user_id": "user_new", "email": "new@example.test"})

    payload = {
        "type": "user.created",
        "data": {
            "id": "user_new",
            "email_addresses": [
                {"id": "ea_1", "email_address": "alt@example.test"},
                {"id": "ea_2", "email_address": "new@example.test"},
            ],
            "primary_email_address_id": "ea_2",
        },
    }

    with patch("core.repositories.user_repo.put", new=captured_put):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    captured_put.assert_awaited_once()
    args, kwargs = captured_put.call_args
    # ``put`` accepts a positional user_id + keyword email.
    assert args[0] == "user_new"
    assert kwargs.get("email") == "new@example.test"


@pytest.mark.asyncio
async def test_user_created_persist_failure_is_non_fatal(async_client, monkeypatch):
    """A DDB hiccup on user_repo.put must NOT bubble out as a 5xx —
    Clerk would retry the same event and we'd never make progress."""
    _bypass_svix(monkeypatch)

    failing_put = AsyncMock(side_effect=RuntimeError("ddb-down"))

    payload = {
        "type": "user.created",
        "data": {
            "id": "user_new",
            "email_addresses": [{"id": "ea_1", "email_address": "x@y.test"}],
            "primary_email_address_id": "ea_1",
        },
    }

    with patch("core.repositories.user_repo.put", new=failing_put):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    failing_put.assert_awaited_once()


# ----------------------------------------------------------------------
# Clerk webhook svix dedupe (I1)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clerk_webhook_dedupes_by_svix_id(async_client, monkeypatch):
    """Two retries from svix on the same Clerk event must NOT both
    drive provisioning — verify the second delivery returns early
    without invoking the provisioning factory.

    Uses ``organizationMembership.deleted`` because
    ``organization.created`` and ``organizationMembership.created`` no
    longer drive provisioning after Tasks 6 + 7 (record-keeping only).
    Membership.deleted still calls ``archive_member`` so the dedupe
    wrapper has something observable to gate.
    """
    _bypass_svix(monkeypatch)

    # First call: dedupe says "RECORDED", real handler runs.
    # Second call: dedupe says "ALREADY_SEEN", handler must skip.
    from core.services.webhook_dedup import WebhookDedupResult

    dedupe_results = [WebhookDedupResult.RECORDED, WebhookDedupResult.ALREADY_SEEN]

    async def _fake_dedupe(event_id, *, source):
        assert source == "clerk"
        return dedupe_results.pop(0)

    monkeypatch.setattr(
        "core.services.webhook_dedup.record_event_or_skip",
        _fake_dedupe,
    )

    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(return_value=None)
    factory = AsyncMock(return_value=mock_provisioning)

    payload = {
        "type": "organizationMembership.deleted",
        "data": {
            "id": "orgm_1",
            "organization": {"id": "org_acme"},
            "public_user_data": {"user_id": "user_member"},
        },
    }

    with patch("routers.webhooks.build_paperclip_provisioning", new=factory):
        resp1 = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )
        resp2 = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Provisioning ran exactly once across both retries.
    mock_provisioning.archive_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_stripe_subscription_deleted_calls_disable(async_client, monkeypatch):
    """``customer.subscription.deleted`` should call
    ``PaperclipProvisioning.disable`` for the affected owner with the
    standard 30-day grace.
    """
    # Bypass Stripe signature verification: the Stripe webhook constructs
    # the event from the raw body via ``stripe.Webhook.construct_event``,
    # which signs with STRIPE_WEBHOOK_SECRET. Patching to the canonical
    # event dict is simpler than synthesizing a real signature.
    fake_event = {
        "id": "evt_test",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_123",
                "customer": "cus_existing",
                "status": "canceled",
            }
        },
    }

    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda body, sig, secret: fake_event,
    )

    mock_provisioning = AsyncMock()
    mock_provisioning.disable = AsyncMock(return_value=None)

    fake_account = {
        "owner_id": "user_owner",
        "stripe_customer_id": "cus_existing",
    }

    with (
        patch(
            "core.repositories.billing_repo.list_by_stripe_customer_id",
            new=AsyncMock(return_value=[fake_account]),
        ),
        patch(
            "core.services.webhook_dedup.record_event_or_skip",
            new=AsyncMock(
                return_value=__import__(
                    "core.services.webhook_dedup", fromlist=["WebhookDedupResult"]
                ).WebhookDedupResult.RECORDED
            ),
        ),
        patch(
            "core.services.billing_service.BillingService.cancel_subscription",
            new=AsyncMock(return_value=None),
        ),
        patch("core.containers.get_ecs_manager") as mock_ecs,
        patch(
            "core.services.paperclip_provisioning.build_paperclip_provisioning",
            new=AsyncMock(return_value=mock_provisioning),
        ),
    ):
        mock_ecs.return_value.delete_user_service = AsyncMock(return_value=None)
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=b"{}",
            headers={"stripe-signature": "ignored"},
        )

    assert resp.status_code == 200
    mock_provisioning.disable.assert_awaited_once_with(user_id="user_owner")


# ----------------------------------------------------------------------
# _lookup_owner_email — Codex P1 (round 3): Clerk fallback
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_owner_email_falls_back_to_clerk_when_repo_missing():
    """When ``user_repo.get`` returns no row (a prior ``user.created``
    persistence failed, or the row predates the email field), we must
    fall back to Clerk Backend API. Without the fallback, retries via
    the update-service worker call the same resolver and stay
    permanently ``pending``.
    """
    from routers.webhooks import _lookup_owner_email

    clerk_user = {
        "primary_email_address_id": "idn_primary",
        "email_addresses": [
            {"id": "idn_primary", "email_address": "owner@acme.test"},
        ],
    }

    clerk_get = AsyncMock(return_value=clerk_user)

    with (
        patch(
            "core.repositories.user_repo.get",
            new=AsyncMock(return_value=None),
        ),
        patch("core.services.clerk_admin.get_user", new=clerk_get),
    ):
        email = await _lookup_owner_email(
            org_id="org_acme",
            fallback_user_id="user_owner",
        )

    assert email == "owner@acme.test"
    clerk_get.assert_awaited_once_with("user_owner")


@pytest.mark.asyncio
async def test_lookup_owner_email_falls_back_to_clerk_when_email_field_empty():
    """Same fallback when the row exists but lacks an email field
    (older rows that predate the email column).
    """
    from routers.webhooks import _lookup_owner_email

    clerk_user = {
        "primary_email_address_id": "idn_primary",
        "email_addresses": [
            {"id": "idn_primary", "email_address": "owner@acme.test"},
        ],
    }

    clerk_get = AsyncMock(return_value=clerk_user)

    with (
        patch(
            "core.repositories.user_repo.get",
            new=AsyncMock(return_value={"user_id": "user_owner"}),
        ),
        patch("core.services.clerk_admin.get_user", new=clerk_get),
    ):
        email = await _lookup_owner_email(
            org_id="org_acme",
            fallback_user_id="user_owner",
        )

    assert email == "owner@acme.test"
    clerk_get.assert_awaited_once_with("user_owner")


@pytest.mark.asyncio
async def test_lookup_owner_email_user_repo_fast_path_does_not_hit_clerk():
    """Existing fast path: when the users repo already has the email
    we MUST NOT call Clerk (avoid extra Clerk API hits and rate-limit
    risk on every membership webhook).
    """
    from routers.webhooks import _lookup_owner_email

    clerk_get = AsyncMock()  # spy — should never be awaited

    with (
        patch(
            "core.repositories.user_repo.get",
            new=AsyncMock(return_value={"user_id": "user_owner", "email": "owner@acme.test"}),
        ),
        patch("core.services.clerk_admin.get_user", new=clerk_get),
    ):
        email = await _lookup_owner_email(
            org_id="org_acme",
            fallback_user_id="user_owner",
        )

    assert email == "owner@acme.test"
    clerk_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_owner_email_returns_none_when_clerk_fails():
    """On Clerk error/404 we return None (not raise) so the retry
    worker can try again next cycle without crashing the webhook.
    """
    from routers.webhooks import _lookup_owner_email

    with (
        patch(
            "core.repositories.user_repo.get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.clerk_admin.get_user",
            new=AsyncMock(return_value=None),
        ),
    ):
        email = await _lookup_owner_email(
            org_id="org_acme",
            fallback_user_id="user_owner",
        )

    assert email is None
