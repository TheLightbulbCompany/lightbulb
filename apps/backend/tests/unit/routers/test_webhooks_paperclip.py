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

import json
from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from core.services.paperclip_admin_client import PaperclipApiError
from core.services import webhook_dedup as _webhook_dedup

_REAL_BEGIN_EVENT_PROCESSING = _webhook_dedup.begin_event_processing
_REAL_COMPLETE_EVENT_PROCESSING = _webhook_dedup.complete_event_processing


@pytest.fixture(autouse=True)
def lifecycle_io(monkeypatch):
    """Isolate canonical revocation and lifecycle storage."""
    revoke = AsyncMock()
    lifecycle = {}

    async def begin_membership(owner_id, alias, *, event_id, sequence, access_state, **kwargs):
        lifecycle[(owner_id, alias)] = {"event_id": event_id, "sequence": sequence, "state": access_state}
        return "membership_claim_test"

    monkeypatch.setattr("core.services.messaging.revoke_human_membership", revoke)
    monkeypatch.setattr(_webhook_dedup, "begin_event_processing", AsyncMock(return_value="claim_test"))
    monkeypatch.setattr(_webhook_dedup, "complete_event_processing", AsyncMock())
    monkeypatch.setattr(_webhook_dedup, "begin_membership_lifecycle", AsyncMock(side_effect=begin_membership))
    monkeypatch.setattr(_webhook_dedup, "renew_membership_lifecycle", AsyncMock())
    monkeypatch.setattr(_webhook_dedup, "complete_membership_lifecycle", AsyncMock())
    return revoke


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


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_failure", [False, True])
async def test_membership_revocation_failure_redelivers_same_svix_and_still_deauthorizes(
    lifecycle_io, monkeypatch, archive_failure
):
    from core.services import webhook_dedup
    from routers import webhooks

    _bypass_svix(monkeypatch)
    monkeypatch.setattr(webhook_dedup, "begin_event_processing", _REAL_BEGIN_EVENT_PROCESSING)
    monkeypatch.setattr(webhook_dedup, "complete_event_processing", _REAL_COMPLETE_EVENT_PROCESSING)
    lifecycle_io.side_effect = [RuntimeError("canonical revocation failed"), None]
    provisioning = AsyncMock()
    if archive_failure:
        provisioning.archive_member.side_effect = PaperclipApiError("retry", 503, "")
    remove_member = AsyncMock()
    monkeypatch.setattr("core.repositories.paperclip_repo.PaperclipRepo.remove_member", remove_member)
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=provisioning))
    monkeypatch.setattr(webhooks, "close_paperclip_provisioning", AsyncMock())
    enqueue = AsyncMock()
    monkeypatch.setattr(webhooks, "_enqueue_paperclip_retry", enqueue)
    request = SimpleNamespace(
        headers=_svix_headers(),
        body=AsyncMock(
            return_value=json.dumps(
                {
                    "type": "organizationMembership.deleted",
                    "data": {"organization": {"id": "org_acme"}, "user_id": "member"},
                }
            ).encode()
        ),
    )
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="clerk-dedup-test",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", "clerk-dedup-test")
        with pytest.raises(RuntimeError, match="canonical revocation failed"):
            await webhooks.handle_clerk_webhook(request)
        provisioning.archive_member.assert_awaited_once_with(user_id="member", org_id="org_acme")
        if archive_failure:
            remove_member.assert_awaited_once_with("org_acme", "member")
            enqueue.assert_awaited_once()
        assert (
            webhook_dedup._table().get_item(Key={"event_id": "msg_test"}).get("Item", {}).get("state") == "processing"
        )
        webhook_dedup._table().update_item(
            Key={"event_id": "msg_test"},
            UpdateExpression="SET lease_until = :expired",
            ExpressionAttributeValues={":expired": 0},
        )
        assert await webhooks.handle_clerk_webhook(request) == {"status": "ok"}
        assert await webhook_dedup.has_event("msg_test")
        assert await webhooks.handle_clerk_webhook(request) == {"status": "duplicate"}
        assert lifecycle_io.await_count == 2


@pytest.mark.asyncio
async def test_membership_delete_revokes_canonical_rooms_before_paperclip(lifecycle_io, monkeypatch):
    from routers import webhooks

    events = []
    lifecycle_io.side_effect = lambda owner_id, user_id: events.append("canonical-revoked")
    provisioning = AsyncMock()
    provisioning.archive_member.side_effect = lambda **kwargs: events.append("paperclip-archived")
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=provisioning))
    monkeypatch.setattr(webhooks, "close_paperclip_provisioning", AsyncMock())

    await webhooks._handle_organization_membership_deleted({"organization": {"id": "org_acme"}, "user_id": "member"})

    lifecycle_io.assert_awaited_once_with("org_acme", "member")
    assert events[:2] == ["canonical-revoked", "paperclip-archived"]


@pytest.mark.asyncio
async def test_membership_delete_wipes_the_members_private_memory_at_once(lifecycle_io, monkeypatch):
    from core.services import workspace_memory
    from routers import webhooks

    wiped = AsyncMock()
    monkeypatch.setattr(workspace_memory, "delete_member_memory", wiped)
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(webhooks, "close_paperclip_provisioning", AsyncMock())

    await webhooks._handle_organization_membership_deleted({"organization": {"id": "org_acme"}, "user_id": "user_a"})
    wiped.assert_awaited_once_with("org_acme", "user_a")

    # Records never expire: a failed wipe must not complete the event, but Paperclip still ran.
    provisioning = AsyncMock()
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=provisioning))
    wiped.side_effect = RuntimeError("memory down")
    with pytest.raises(RuntimeError, match="memory down"):
        await webhooks._handle_organization_membership_deleted(
            {"organization": {"id": "org_acme"}, "user_id": "user_a"}
        )
    provisioning.archive_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_newer_admission_stops_old_delete_during_paperclip_cleanup(
    monkeypatch,
):
    from routers import webhooks

    events = []
    provisioning = AsyncMock()
    provisioning.archive_member.side_effect = lambda **kwargs: events.append("paperclip-archived")

    async def renew(org_id, user_id, claim_id):
        if "paperclip-archived" in events:
            raise _webhook_dedup.MembershipLifecycleSuperseded("newer create owns lifecycle")

    monkeypatch.setattr(webhooks, "_renew_member_lifecycle", AsyncMock(side_effect=renew))
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=provisioning))
    monkeypatch.setattr(webhooks, "close_paperclip_provisioning", AsyncMock())

    with pytest.raises(_webhook_dedup.MembershipLifecycleSuperseded):
        await webhooks._handle_organization_membership_deleted(
            {"organization": {"id": "org_acme"}, "user_id": "member"},
            lifecycle_claim_id="delete-claim",
        )


@pytest.mark.asyncio
async def test_a_failed_membership_revocation_schedules_no_mac_revocation(monkeypatch):
    from core.services import mac_browser
    from routers import webhooks

    scheduled = []
    monkeypatch.setattr(mac_browser, "revoke_member_soon", lambda *a: scheduled.append(a))
    monkeypatch.setattr(
        "core.services.messaging.revoke_human_membership", AsyncMock(side_effect=RuntimeError("ddb down"))
    )
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(webhooks, "close_paperclip_provisioning", AsyncMock())
    with pytest.raises(RuntimeError):
        await webhooks._handle_organization_membership_deleted(
            {"organization": {"id": "org_acme"}, "user_id": "member"}
        )
    assert scheduled == []


@pytest.mark.asyncio
async def test_member_deleted_then_re_added_and_re_paired_keeps_the_pairing_and_the_mac(monkeypatch):
    """Delete, then re-add + re-pair before the background Mac revocation runs: it reads the
    lifecycle epoch, sees the re-add, and touches neither the pairing nor the Mac."""
    from core.services import mac_browser
    from routers import webhooks

    lifecycle = {"lifecycle_event_id": "evt_delete", "access_state": "revoked", "lifecycle_status": "completed"}
    monkeypatch.setattr(_webhook_dedup, "_read_event", AsyncMock(side_effect=lambda key: dict(lifecycle)))
    monkeypatch.setattr("core.services.messaging.revoke_human_membership", AsyncMock())
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(webhooks, "close_paperclip_provisioning", AsyncMock())
    pairing = {"dev_1": {"userId": "member", "clientId": "node-host"}}
    get_row = AsyncMock(return_value={"paired_devices": pairing})
    mark = AsyncMock()
    clear = AsyncMock()
    monkeypatch.setattr(mac_browser.container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr(mac_browser.container_repo, "mark_paired_device_revoking", mark)
    monkeypatch.setattr(mac_browser, "clear_member", clear)

    await webhooks._handle_organization_membership_deleted(
        {"organization": {"id": "org_acme"}, "user_id": "member"},
        lifecycle_claim_id="delete-claim",
        lifecycle_event_id="evt_delete",
    )
    lifecycle.update(lifecycle_event_id="evt_readd", access_state="allowed")  # re-added (+ re-paired)
    loop = asyncio.get_running_loop()
    await asyncio.gather(*[t for t in mac_browser._clears if t.get_loop() is loop])
    get_row.assert_not_awaited()
    mark.assert_not_awaited()
    clear.assert_not_awaited()
    assert pairing == {"dev_1": {"userId": "member", "clientId": "node-host"}}


# ----------------------------------------------------------------------
# organization.created -> provision_new_workspace
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_organization_created_dispatches_the_first_provision():
    """The webhook hands the org to ``provision_new_workspace`` (trial row + gate +
    provision dispatch) and never calls Paperclip itself -- the company is seeded
    inside the provision execution.
    """
    from routers.webhooks import _handle_organization_created

    with (
        patch("routers.webhooks.build_paperclip_provisioning") as mock_factory,
        patch(
            "routers.webhooks._enqueue_paperclip_retry",
            new=AsyncMock(),
        ) as mock_retry,
        patch("core.services.user_provision.provision_new_workspace", new=AsyncMock()) as mock_provision,
        patch("core.repositories.company_slug_repo.claim_hold", new=AsyncMock()) as mock_claim,
    ):
        mock_provision.side_effect = lambda *_, **__: mock_claim.assert_awaited_once()  # the slug lands first
        await _handle_organization_created(
            {
                "id": "org_acme",
                "created_by": "user_owner",
            }
        )

    mock_factory.assert_not_called()
    mock_retry.assert_not_awaited()
    # The creator rides along so the trial events land on their PostHog person.
    mock_provision.assert_awaited_once_with("org_acme", created_by="user_owner")
    # The agent-email slug the owner held at "Create your workspace" moves to the workspace.
    mock_claim.assert_awaited_once_with(user_id="user_owner", owner_id="org_acme")


@pytest.mark.asyncio
async def test_a_failed_organization_created_rolls_back_its_dedup_row_so_svix_redelivers(async_client, monkeypatch):
    """The webhook is the only thing that starts a first provision, so a failure must 5xx AND
    drop the dedup row -- otherwise Svix's redelivery is acked as a duplicate and the
    workspace is never provisioned."""
    from core.services import webhook_dedup

    _bypass_svix(monkeypatch)
    monkeypatch.setattr(
        webhook_dedup, "record_event_or_skip", AsyncMock(return_value=webhook_dedup.WebhookDedupResult.RECORDED)
    )
    delete_event = AsyncMock()
    monkeypatch.setattr(webhook_dedup, "delete_event", delete_event)
    monkeypatch.setattr(
        "core.services.user_provision.provision_new_workspace", AsyncMock(side_effect=RuntimeError("sfn down"))
    )
    monkeypatch.setattr("core.repositories.company_slug_repo.claim_hold", AsyncMock())
    payload = {"type": "organization.created", "data": {"id": "org_acme", "created_by": "user_owner"}}

    with pytest.raises(RuntimeError):
        await async_client.post("/api/v1/webhooks/clerk", json=payload, headers=_svix_headers())
    delete_event.assert_awaited_once_with("msg_test")


# ----------------------------------------------------------------------
# organizationMembership.created -> lifecycle record + idempotent Paperclip JOIN
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_membership_created_does_not_create_company_but_does_join():
    """The membership webhook never creates a company (that stays behind the
    provision gate — no direct ``build_paperclip_provisioning``/
    ``_enqueue_paperclip_retry`` call from this router), but it DOES drive
    the idempotent ``_ensure_paperclip_for_member`` JOIN inline, rather than
    waiting on the member's own ``/container/status`` poll."""
    from routers.webhooks import _handle_organization_membership_created

    with (
        patch("routers.webhooks.build_paperclip_provisioning") as mock_factory,
        patch("routers.webhooks._enqueue_paperclip_retry", new=AsyncMock()) as mock_retry,
        patch("routers.webhooks.billing_repo.get_by_owner_id", new=AsyncMock()) as mock_billing,
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()) as mock_join,
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
    mock_billing.assert_not_awaited()
    mock_join.assert_awaited_once_with(
        org_id="org_x",
        member_user_id="user_member",
        member_email="m@example.com",
    )


@pytest.mark.asyncio
async def test_membership_created_join_error_still_acks_webhook_2xx(lifecycle_io, monkeypatch):
    """A raising Paperclip join must not turn into a non-2xx: the full
    ``handle_clerk_webhook`` route must still return ``{"status": "ok"}`` so
    Clerk does not retry the whole event, while the error is logged + metered."""
    from routers import webhooks

    _bypass_svix(monkeypatch)
    monkeypatch.setattr(
        "core.services.user_provision._ensure_paperclip_for_member",
        AsyncMock(side_effect=RuntimeError("paperclip is down")),
    )
    metric_calls = []
    monkeypatch.setattr(webhooks, "put_metric", lambda name, **kw: metric_calls.append((name, kw)))
    request = SimpleNamespace(
        headers=_svix_headers(),
        body=AsyncMock(
            return_value=json.dumps(
                {
                    "type": "organizationMembership.created",
                    "data": {
                        "organization": {"id": "org_acme", "created_by": "user_owner"},
                        "public_user_data": {"user_id": "user_member", "identifier": "m@example.com"},
                    },
                }
            ).encode()
        ),
    )

    result = await webhooks.handle_clerk_webhook(request)

    assert result == {"status": "ok"}
    assert ("paperclip.member_add", {"dimensions": {"outcome": "webhook_error"}}) in metric_calls


# ----------------------------------------------------------------------
# organizationMembership.deleted -> archive_member
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_membership_deleted_calls_archive_member(async_client, monkeypatch):
    """``organizationMembership.deleted`` should drive
    ``PaperclipProvisioning.archive_member`` scoped to THAT org, so a
    human in two workspaces keeps the other one.
    """
    _bypass_svix(monkeypatch)

    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(return_value=None)
    durable_revoke = AsyncMock()

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
        patch(
            "core.services.messaging.revoke_human_membership",
            new=durable_revoke,
        ),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    durable_revoke.assert_awaited_once()
    mock_provisioning.archive_member.assert_awaited_once_with(user_id="user_member", org_id="org_acme")


@pytest.mark.asyncio
async def test_membership_deleted_retryable_failure_enqueues(async_client, monkeypatch):
    """Paperclip 5xx during ``archive_member`` should enqueue a
    pending-updates retry row keyed on ``op="archive_member"``.

    Auth-bypass guard: the handler MUST also call ``remove_member`` BEFORE
    enqueueing the retry so the user leaves the workspace row's
    ``member_user_ids`` immediately. Otherwise ``resolve_teams_context``
    would keep authorizing the removed user against ``/api/v1/teams/*``
    for the entire retry window (minutes to hours). The retry payload
    carries the org id so the later archive stays org-scoped.
    """
    _bypass_svix(monkeypatch)

    err = PaperclipApiError("server-down", 503, "")
    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(side_effect=err)
    remove_member = AsyncMock(return_value=None)
    monkeypatch.setattr("core.repositories.paperclip_repo.PaperclipRepo.remove_member", remove_member)

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
    mock_provisioning.archive_member.assert_awaited_once_with(user_id="user_member", org_id="org_acme")
    # Auth-bypass guard: DDB membership must be dropped even on retryable.
    remove_member.assert_awaited_once_with("org_acme", "user_member")
    captured_create.assert_awaited_once()
    kwargs = captured_create.call_args.kwargs
    assert kwargs["owner_id"] == "user_member"
    assert kwargs["update_type"] == "paperclip_provision"
    assert kwargs["changes"]["op"] == "archive_member"
    assert kwargs["changes"]["user_id"] == "user_member"
    assert kwargs["changes"]["org_id"] == "org_acme"


@pytest.mark.asyncio
async def test_membership_deleted_non_retryable_failure_swallowed(async_client, monkeypatch):
    """4xx (non-429) on archive_member should NOT enqueue a retry —
    the user is already gone from Clerk, and Clerk redelivery would
    just hit the same 4xx. We still return 200 so Clerk stops retrying.

    Critically (auth-bypass guard): the handler MUST also call
    ``remove_member`` to drop the DDB membership even when
    Paperclip archive failed non-retryably — otherwise the removed
    Clerk-org member can still hit ``/api/v1/teams/*`` because
    ``resolve_teams_context`` only checks DDB state.
    """
    _bypass_svix(monkeypatch)

    err = PaperclipApiError("not-found", 404, "")
    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(side_effect=err)
    remove_member = AsyncMock(return_value=None)
    monkeypatch.setattr("core.repositories.paperclip_repo.PaperclipRepo.remove_member", remove_member)
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
    # Auth-bypass guard: DDB membership must be dropped.
    remove_member.assert_awaited_once_with("org_acme", "user_member")


@pytest.mark.asyncio
async def test_membership_deleted_non_retryable_fence_failure_is_swallowed(async_client, monkeypatch):
    """If the defensive ``remove_member`` call after a non-retryable archive
    failure itself raises, we still return 200 — the webhook must not
    bubble exceptions back to Clerk, which would just redeliver the
    same event. The exception is logged for ops to investigate.
    """
    _bypass_svix(monkeypatch)

    err = PaperclipApiError("not-found", 404, "")
    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(side_effect=err)
    remove_member = AsyncMock(side_effect=RuntimeError("ddb-down"))
    monkeypatch.setattr("core.repositories.paperclip_repo.PaperclipRepo.remove_member", remove_member)
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
    remove_member.assert_awaited_once_with("org_acme", "user_member")
    captured_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_membership_deleted_retryable_fence_failure_is_swallowed(async_client, monkeypatch):
    """On retryable archive failure, the handler tries ``remove_member``
    BEFORE enqueueing the retry to close the auth-bypass window. If
    that defensive ``remove_member`` itself raises (e.g. DDB transient), we
    must still enqueue the retry and return 200 — the webhook must not
    bubble exceptions back to Clerk, and we still want the retry
    pending so the next pass re-attempts archive.
    """
    _bypass_svix(monkeypatch)

    err = PaperclipApiError("server-down", 503, "")
    mock_provisioning = AsyncMock()
    mock_provisioning.archive_member = AsyncMock(side_effect=err)
    remove_member = AsyncMock(side_effect=RuntimeError("ddb-down"))
    monkeypatch.setattr("core.repositories.paperclip_repo.PaperclipRepo.remove_member", remove_member)
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
    remove_member.assert_awaited_once_with("org_acme", "user_member")
    # Even when remove_member raises, the retry row is still enqueued so
    # the next worker pass re-attempts the Paperclip-side archive.
    captured_create.assert_awaited_once()


# ----------------------------------------------------------------------
# user.deleted -> delete the identity row
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_deleted_deletes_identity_row(async_client, monkeypatch):
    """``user.deleted`` sweeps channel_links AND deletes the user's
    Paperclip identity row; no provisioning/company call is made (their
    memberships leave with each org's ``organizationMembership.deleted``).
    """
    _bypass_svix(monkeypatch)

    sweep = AsyncMock(return_value=0)
    repo_delete = AsyncMock(return_value=None)
    factory = AsyncMock()

    payload = {"type": "user.deleted", "data": {"id": "user_x", "deleted": True}}

    with (
        patch("routers.webhooks.build_paperclip_provisioning", new=factory),
        patch("core.repositories.paperclip_repo.PaperclipRepo.delete", new=repo_delete),
        patch(
            "core.repositories.channel_link_repo.sweep_by_member",
            new=sweep,
        ),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers=_svix_headers(),
        )

    assert resp.status_code == 200
    sweep.assert_awaited_once_with("user_x")
    repo_delete.assert_awaited_once_with("user_x")
    factory.assert_not_awaited()


# ----------------------------------------------------------------------
# Stripe customer.subscription.deleted -> no Paperclip call
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
    ``organizationMembership.created`` no longer drives provisioning and
    ``organization.created`` dispatches through ``provision_new_workspace``.
    Membership.deleted still calls ``archive_member`` so the dedupe
    wrapper has something observable to gate.
    """
    _bypass_svix(monkeypatch)

    # First call owns the retryable lease; the completed second call skips.
    claim = "claim_test"
    dedupe_results = [claim, None]

    async def _fake_dedupe(event_id, *, source):
        assert source == "clerk"
        return dedupe_results.pop(0)

    monkeypatch.setattr(
        "core.services.webhook_dedup.begin_event_processing",
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
async def test_stripe_subscription_deleted_makes_no_paperclip_call(async_client, monkeypatch):
    """``customer.subscription.deleted`` runs entitlement teardown but no
    longer touches Paperclip: the company belongs to the workspace, and a
    lapsed entitlement is not a deleted workspace (``organization.deleted``
    disables the workspace row).
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

    factory = AsyncMock()

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
        patch(
            "core.services.paperclip_provisioning.build_paperclip_provisioning",
            new=factory,
        ),
        # Teardown now asks whether ANY provider still pays before running —
        # Stripe ending is no longer the same question as the entitlement
        # ending. Nothing else pays here, so teardown proceeds.
        patch(
            "core.services.entitlement_teardown.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_owner", "subscription_status": "canceled"}),
        ),
    ):
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=b"{}",
            headers={"stripe-signature": "ignored"},
        )

    assert resp.status_code == 200
    factory.assert_not_awaited()


# ----------------------------------------------------------------------
# _lookup_owner_email — Clerk fallback
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


async def test_membership_delete_revokes_before_clearing_macs_and_never_waits_on_them(lifecycle_io, monkeypatch):
    import asyncio

    from core.services import mac_browser
    from routers import webhooks

    events = []
    lifecycle_io.side_effect = lambda owner_id, user_id: events.append("canonical-revoked")

    async def slow_clear(owner, member):
        events.append("mac-clear-started")
        await asyncio.sleep(30)  # a slow Mac

    monkeypatch.setattr(mac_browser, "clear_member", slow_clear)
    monkeypatch.setattr(mac_browser.container_repo, "get_by_owner_id", AsyncMock(return_value={}))
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(webhooks, "close_paperclip_provisioning", AsyncMock())

    await asyncio.wait_for(
        webhooks._handle_organization_membership_deleted({"organization": {"id": "org_acme"}, "user_id": "member"}),
        2,
    )
    # The clear runs in the background under one budget, a few loop turns in.
    for _ in range(20):
        if "mac-clear-started" in events:
            break
        await asyncio.sleep(0)
    assert events[0] == "canonical-revoked" and "mac-clear-started" in events
    loop = asyncio.get_running_loop()
    for task in [t for t in mac_browser._clears if t.get_loop() is loop]:
        task.cancel()
