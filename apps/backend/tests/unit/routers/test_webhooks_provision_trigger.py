"""Clerk ``user.created`` as a provisioning trigger, + the signature posture.

Two things are under test here and they are related: the webhook endpoint is now
a provisioning trigger, which makes it a cost-amplification primitive, which is
why the signature check had to stop failing open first.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest

_SECRET = "whsec_" + base64.b64encode(b"super-secret-key-for-tests").decode()


def _signed_headers(body: bytes, *, secret: str = _SECRET, msg_id: str = "msg_1") -> dict:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    ts = "1700000000"
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}


def _user_created(*, verified: bool = True, primary_id: str | None = "idn_1") -> dict:
    return {
        "type": "user.created",
        "data": {
            "id": "user_new",
            "primary_email_address_id": primary_id,
            "email_addresses": [
                {
                    "id": "idn_1",
                    "email_address": "new@example.com",
                    "verification": {"status": "verified" if verified else "unverified"},
                }
            ],
            "first_name": "Ada",
        },
    }


def _quiet_user_created_side_effects(monkeypatch):
    """Stub the non-trigger work in the user.created branch (users-table write,
    welcome email) so these tests only exercise the trigger."""
    monkeypatch.setattr("core.repositories.user_repo.put", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "core.services.welcome_email.send_welcome_email",
        AsyncMock(return_value=None),
    )


# ----------------------------------------------------------------------
# Signature verification — must fail CLOSED
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_signature_accepted(async_client, monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", _SECRET)
    _quiet_user_created_side_effects(monkeypatch)
    body = json.dumps(_user_created()).encode()

    with (
        patch(
            "core.services.user_provision.ensure_personal_trial_default_for_user",
            new=AsyncMock(),
        ),
        patch("core.services.user_provision.ensure_owner_provisioned", new=AsyncMock()),
        patch("core.services.webhook_dedup.record_event_or_skip", new=AsyncMock(return_value=None)),
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body))

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_invalid_signature_rejected(async_client, monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", _SECRET)
    body = json.dumps(_user_created()).encode()
    headers = _signed_headers(body)
    headers["svix-signature"] = "v1," + base64.b64encode(b"wrong").decode()

    with patch("routers.webhooks.put_metric") as mock_metric:
        resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers=headers)

    assert resp.status_code == 400
    mock_metric.assert_any_call("webhook.clerk.sig_fail")


@pytest.mark.asyncio
async def test_missing_signature_headers_rejected(async_client, monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", _SECRET)
    body = json.dumps(_user_created()).encode()

    resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers={})

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_malformed_secret_rejects_rather_than_skipping(async_client, monkeypatch):
    """THE regression this locks down. The old inline verifier logged
    'not valid base64' and RETURNED — a typo'd secret silently reopened the
    endpoint to any unsigned body on the internet. The shared verifier fails
    closed; do not 'fix' it back.
    """
    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", "whsec_!!!not-base64!!!")
    body = json.dumps(_user_created()).encode()

    with patch("core.services.user_provision.ensure_owner_provisioned", new=AsyncMock()) as mock_trigger:
        resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body))

    assert resp.status_code == 400
    mock_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_unset_secret_skips_verification_for_local_dev(async_client, monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", None)
    _quiet_user_created_side_effects(monkeypatch)
    body = json.dumps(_user_created()).encode()

    with (
        patch(
            "core.services.user_provision.ensure_personal_trial_default_for_user",
            new=AsyncMock(),
        ),
        patch("core.services.user_provision.ensure_owner_provisioned", new=AsyncMock()),
        patch("core.services.webhook_dedup.record_event_or_skip", new=AsyncMock(return_value=None)),
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers={"svix-id": "msg_local"})

    assert resp.status_code == 200


# ----------------------------------------------------------------------
# user.created -> trial row + provisioning trigger
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verified_user_created_seeds_trial_then_triggers(async_client, monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", None)
    _quiet_user_created_side_effects(monkeypatch)
    body = json.dumps(_user_created(verified=True)).encode()

    with (
        patch(
            "core.services.user_provision.ensure_personal_trial_default_for_user",
            new=AsyncMock(),
        ) as mock_trial,
        patch("core.services.user_provision.ensure_owner_provisioned", new=AsyncMock()) as mock_trigger,
        patch("core.services.webhook_dedup.record_event_or_skip", new=AsyncMock(return_value=None)),
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers={"svix-id": "msg_v"})

    assert resp.status_code == 200
    # Order matters: the trial_local row is what CLEARS the gate, so it must
    # land before the trigger evaluates it.
    mock_trial.assert_awaited_once_with("user_new")
    mock_trigger.assert_awaited_once_with("user_new", trigger="clerk.user_created")


@pytest.mark.asyncio
async def test_unverified_primary_email_does_not_trigger(async_client, monkeypatch):
    """Clerk matches org invitations to VERIFIED emails only, so firing before
    verification would hand an about-to-be-invited user a personal container,
    billing row and throwaway Paperclip company. Deferring to the poll is free.
    """
    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", None)
    _quiet_user_created_side_effects(monkeypatch)
    body = json.dumps(_user_created(verified=False)).encode()

    with (
        patch(
            "core.services.user_provision.ensure_personal_trial_default_for_user",
            new=AsyncMock(),
        ) as mock_trial,
        patch("core.services.user_provision.ensure_owner_provisioned", new=AsyncMock()) as mock_trigger,
        patch("core.services.webhook_dedup.record_event_or_skip", new=AsyncMock(return_value=None)),
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers={"svix-id": "msg_u"})

    assert resp.status_code == 200
    mock_trigger.assert_not_awaited()
    mock_trial.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_primary_pointer_does_not_trigger(async_client, monkeypatch):
    """No ``primary_email_address_id`` means we cannot tell WHICH address is
    primary. ``_primary_email_verified`` is strict on purpose — never assume
    ``email_addresses[0]``."""
    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", None)
    _quiet_user_created_side_effects(monkeypatch)
    body = json.dumps(_user_created(primary_id=None)).encode()

    with (
        patch("core.services.user_provision.ensure_owner_provisioned", new=AsyncMock()) as mock_trigger,
        patch("core.services.webhook_dedup.record_event_or_skip", new=AsyncMock(return_value=None)),
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers={"svix-id": "msg_p"})

    assert resp.status_code == 200
    mock_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_failure_still_returns_200(async_client, monkeypatch):
    """A non-2xx makes Svix retry, and 5 days of failures silently auto-disable
    the endpoint. The poll fallback covers the owner, so swallow and ack."""
    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", None)
    _quiet_user_created_side_effects(monkeypatch)
    body = json.dumps(_user_created()).encode()

    with (
        patch(
            "core.services.user_provision.ensure_personal_trial_default_for_user",
            new=AsyncMock(side_effect=RuntimeError("clerk down")),
        ),
        patch("core.services.webhook_dedup.record_event_or_skip", new=AsyncMock(return_value=None)),
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers={"svix-id": "msg_boom"})

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_duplicate_svix_id_triggers_only_once(async_client, monkeypatch):
    """``svix-id`` is stable across Svix's retries, which is what makes it the
    correct dedup key for at-least-once delivery."""
    from core.services.webhook_dedup import WebhookDedupResult

    monkeypatch.setattr("core.config.settings.CLERK_WEBHOOK_SECRET", None)
    _quiet_user_created_side_effects(monkeypatch)
    body = json.dumps(_user_created()).encode()

    seen: set[str] = set()

    async def _dedup(event_id: str, *, source: str):
        if event_id in seen:
            return WebhookDedupResult.ALREADY_SEEN
        seen.add(event_id)
        return WebhookDedupResult.RECORDED

    with (
        patch(
            "core.services.user_provision.ensure_personal_trial_default_for_user",
            new=AsyncMock(),
        ),
        patch("core.services.user_provision.ensure_owner_provisioned", new=AsyncMock()) as mock_trigger,
        patch("core.services.webhook_dedup.record_event_or_skip", new=_dedup),
    ):
        for _ in range(2):
            resp = await async_client.post("/api/v1/webhooks/clerk", content=body, headers={"svix-id": "msg_same"})
            assert resp.status_code == 200

    assert mock_trigger.await_count == 1
