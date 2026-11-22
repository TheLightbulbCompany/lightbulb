"""Tests for the Clerk webhook: ``user.*`` events.

Svix signature verification is bypassed via monkeypatch on the
private ``_verify_svix_signature`` symbol — same pattern used by the
existing ``test_webhooks_clerk_email_sync.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import webhook_dedup as _webhook_dedup
from core.services import workspace_memory as _workspace_memory
from tests.unit.services.test_account_wipe import OWNER_ID, wipe_mocks  # noqa: F401

_REAL_BEGIN_EVENT_PROCESSING = _webhook_dedup.begin_event_processing
_REAL_COMPLETE_EVENT_PROCESSING = _webhook_dedup.complete_event_processing
_REAL_PERSON_WIPE = _workspace_memory.delete_person_memory


@pytest.fixture(autouse=True)
def lifecycle_io(monkeypatch):
    """Isolate canonical revocation and lifecycle storage."""
    revoke = AsyncMock()
    monkeypatch.setattr(_webhook_dedup, "begin_event_processing", AsyncMock(return_value="claim_test"))
    monkeypatch.setattr(_webhook_dedup, "complete_event_processing", AsyncMock())
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


# ----------------------------------------------------------------------
# user.deleted -> the person's own tenant is wiped
# ----------------------------------------------------------------------


async def _user_deleted(async_client, user_id: str = OWNER_ID):
    payload = {"type": "user.deleted", "data": {"id": user_id, "deleted": True}}
    return await async_client.post("/api/v1/webhooks/clerk", json=payload, headers=_svix_headers())


@pytest.mark.asyncio
async def test_user_deleted_tears_down_tenant(async_client, monkeypatch, wipe_mocks):  # noqa: F811
    """The person's own tenant goes for good (runtime with profiles and memory, engine state, managed key,
    credentials, rooms, billing, users row), with no Clerk read or delete, no org touched and no phone
    line released."""
    _bypass_svix(monkeypatch)
    wipe_mocks.openrouter_get.return_value = {"owner_id": OWNER_ID, "state": "active"}

    resp = await _user_deleted(async_client)

    assert resp.status_code == 200, resp.text
    wipe_mocks.engine_wipe.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.delete_workspace_runtime.assert_awaited_once_with(OWNER_ID, delete_profiles=True, report={})
    wipe_mocks.openrouter_retire.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.stripe_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.messaging_delete_owner.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.person_memory_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.paperclip_row_delete.assert_awaited_once_with(OWNER_ID)  # identity + the person's workspace row
    wipe_mocks.owner_secrets_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.user_delete.assert_awaited_once_with(OWNER_ID)
    # Clerk already deleted the person: nothing reads or deletes there, and orgs they belonged to are kept.
    wipe_mocks.clerk_list_user_orgs.assert_not_awaited()
    wipe_mocks.paperclip_archive_member.assert_not_awaited()
    wipe_mocks.clerk_delete.assert_not_awaited()
    wipe_mocks.phone_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_deleted_without_a_tenant_is_a_clean_no_op(async_client, monkeypatch, wipe_mocks):  # noqa: F811
    """Most people deleted are Spark-only: no runtime, billing, rooms or users row to remove."""
    _bypass_svix(monkeypatch)
    wipe_mocks.container_get.return_value = None
    wipe_mocks.billing_get.return_value = None
    wipe_mocks.user_get.return_value = None
    wipe_mocks.messaging_list_rooms.return_value = []
    wipe_mocks.identities.pop(OWNER_ID)

    resp = await _user_deleted(async_client)

    assert resp.status_code == 200, resp.text
    # The seam's owner-keyed cleanups still run (idempotent no-ops here); nothing row- or billing-named does.
    wipe_mocks.delete_workspace_runtime.assert_awaited_once_with(OWNER_ID, delete_profiles=True, report={})
    wipe_mocks.stripe_delete.assert_not_awaited()
    wipe_mocks.user_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_deleted_fails_the_event_and_the_redelivery_finishes(async_client, monkeypatch, wipe_mocks):  # noqa: F811
    """A failed step answers 5xx (the lifecycle claim stays open), so Svix redelivers; every step is
    idempotent, so the redelivery completes the wipe."""
    _bypass_svix(monkeypatch)
    wipe_mocks.person_memory_delete.side_effect = [RuntimeError("down"), None]

    first = await _user_deleted(async_client)
    second = await _user_deleted(async_client)

    assert first.status_code == 503 and "person_memory" in first.text
    assert second.status_code == 200, second.text
    assert wipe_mocks.person_memory_delete.await_count == 2
    from core.services import webhook_dedup

    webhook_dedup.complete_event_processing.assert_awaited_once()


def _real_person_wipe(monkeypatch, wipe_mocks, list_sessions_error: str, message: str = f"Actor {OWNER_ID} not found"):  # noqa: F811
    """Run the real person-memory wipe against a fake AgentCore whose ListSessions raises ``list_sessions_error``."""
    from botocore.exceptions import ClientError

    from core.services import evaluator, session_store, workspace_memory

    client = MagicMock()
    client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    client.list_sessions.side_effect = ClientError(
        {"Error": {"Code": list_sessions_error, "Message": message}}, "ListSessions"
    )
    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_SESSIONS_MEMORY_ID", "mem-1", raising=False)
    monkeypatch.setattr(evaluator, "_client", lambda: client)
    monkeypatch.setattr(workspace_memory, "_tail", AsyncMock())
    monkeypatch.setattr(session_store, "delete_sessions", AsyncMock(return_value=0))
    monkeypatch.setattr(evaluator, "delete_drafts", AsyncMock(return_value=0))
    monkeypatch.setattr(workspace_memory, "_forget_keys", AsyncMock())

    async def real(user_id):
        await _REAL_PERSON_WIPE(user_id, resweep_after_s=0)

    wipe_mocks.person_memory_delete.side_effect = real
    return client


@pytest.mark.asyncio
async def test_user_deleted_for_a_person_without_memory_completes(async_client, monkeypatch, wipe_mocks):  # noqa: F811
    """A person who never wrote memory has no AgentCore actor: ListSessions answers not found, which is nothing to delete."""
    _bypass_svix(monkeypatch)
    client = _real_person_wipe(monkeypatch, wipe_mocks, "ResourceNotFoundException")

    resp = await _user_deleted(async_client)

    assert resp.status_code == 200, resp.text
    client.list_sessions.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("ThrottledException", "slow down"),
        ("AccessDeniedException", "denied"),
        ("ResourceNotFoundException", "Memory mem-1 not found"),
    ],
)
async def test_user_deleted_still_redelivers_when_memory_errors_otherwise(
    async_client,
    monkeypatch,
    wipe_mocks,  # noqa: F811
    code,
    message,
):
    _bypass_svix(monkeypatch)
    _real_person_wipe(monkeypatch, wipe_mocks, code, message)

    resp = await _user_deleted(async_client)

    assert resp.status_code == 503 and "person_memory" in resp.text


@pytest.mark.asyncio
async def test_user_deleted_keeps_a_held_phone_line_for_an_admin_wipe(async_client, monkeypatch, wipe_mocks):  # noqa: F811
    """A line is never released silently: the tenant's messaging rows stay so an admin wipe can release it."""
    _bypass_svix(monkeypatch)
    wipe_mocks.held_number.return_value = "+15550000000"

    resp = await _user_deleted(async_client)

    assert resp.status_code == 200, resp.text
    wipe_mocks.phone_release.assert_not_awaited()
    wipe_mocks.messaging_delete_owner.assert_not_awaited()
    wipe_mocks.messaging_delete_room.assert_not_awaited()


# ----------------------------------------------------------------------
# Stripe customer.subscription.deleted -> no Paperclip call
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# user.created -> persists email into users repo
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_created_persists_email(async_client, monkeypatch):
    """``user.created`` should extract the primary email from the Clerk
    payload and persist it via ``user_repo.put`` so later owner-email
    lookups need no round trip to Clerk admin.
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
@pytest.mark.usefixtures("no_evaluator_wipe")
async def test_clerk_webhook_dedupes_by_svix_id(async_client, monkeypatch, wipe_mocks):  # noqa: F811
    """Two deliveries of the same Clerk event must not both wipe: the second returns early."""
    _bypass_svix(monkeypatch)

    # First call owns the retryable lease; the completed second call skips.
    dedupe_results = ["claim_test", None]

    async def _fake_dedupe(event_id, *, source):
        assert source == "clerk"
        return dedupe_results.pop(0)

    monkeypatch.setattr("core.services.webhook_dedup.begin_event_processing", _fake_dedupe)

    resp1 = await _user_deleted(async_client)
    resp2 = await _user_deleted(async_client)

    assert resp1.status_code == 200
    assert resp2.json() == {"status": "duplicate"}
    wipe_mocks.delete_workspace_runtime.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        "organization.created",
        "organization.deleted",
        "organizationMembership.created",
        "organizationMembership.deleted",
    ],
)
async def test_org_webhooks_are_ignored(async_client, monkeypatch, event):
    """The tenant is a person: an organization event is acknowledged and does nothing."""
    _bypass_svix(monkeypatch)
    dedup = AsyncMock()
    monkeypatch.setattr("core.services.webhook_dedup.record_event_or_skip", dedup)
    monkeypatch.setattr("core.services.webhook_dedup.begin_event_processing", dedup)
    payload = {
        "type": event,
        "data": {"id": "org_x", "organization": {"id": "org_x"}, "public_user_data": {"user_id": "user_x"}},
    }

    resp = await async_client.post("/api/v1/webhooks/clerk", json=payload, headers=_svix_headers())

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    dedup.assert_not_awaited()


@pytest.mark.asyncio
async def test_stripe_subscription_deleted_makes_no_paperclip_call(async_client, monkeypatch):
    """``customer.subscription.deleted`` runs entitlement teardown but no
    longer touches Paperclip: the company belongs to the workspace, and a
    lapsed entitlement is not a deleted workspace.
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


@pytest.fixture
def no_evaluator_wipe(monkeypatch):
    """The member wipe's own steps, stubbed: this workspace's sessions and drafts
    (tests/unit/services/test_evaluator.py) and the log forwards for the person's computer keys and
    the entries written on their behalf (tests/unit/services/test_workspace_memory.py). Returns the mocks."""
    from unittest.mock import AsyncMock

    from core.services import evaluator, memory_api, session_store, workspace_memory

    mocks = {}
    for mod, name, value in (
        (session_store, "delete_sessions", 0),
        (evaluator, "delete_drafts", 0),
        (evaluator, "delete_episodes", 0),
        (workspace_memory, "_computer_keys", []),  # no signed-in computer: no forget_author forward
        (workspace_memory, "_forget_keys", None),
        (memory_api, "forget_on_behalf_of", 0),
    ):
        mocks[name] = AsyncMock(return_value=value)
        monkeypatch.setattr(mod, name, mocks[name])
    return mocks


@pytest.mark.asyncio
async def test_user_deleted_keeps_a_sign_in_bound_to_an_org(async_client, monkeypatch, wipe_mocks):  # noqa: F811
    """A ChatGPT sign-in the person made for an org is the org's credential: deleting the person leaves it."""
    _bypass_svix(monkeypatch)
    wipe_mocks.oauth_bound.return_value = "org_x"

    resp = await _user_deleted(async_client)

    assert resp.status_code == 200, resp.text
    wipe_mocks.oauth_revoke.assert_not_awaited()
    wipe_mocks.fast_gate_delete.assert_awaited_once_with(OWNER_ID)


@pytest.mark.asyncio
async def test_user_deleted_redelivery_finishes_cleanup_after_the_row_is_gone(async_client, monkeypatch, wipe_mocks):  # noqa: F811
    """The runtime seam deletes the containers row, then fails deleting the owner's memory: 503. The redelivery
    finds no row and still runs the seam's owner-keyed cleanup (owner memory included), then acknowledges."""
    _bypass_svix(monkeypatch)
    gone = []

    async def seam(owner_id, *, delete_profiles=False, report=None):
        gone.append(owner_id)  # the row is deleted before the memory step
        if len(gone) == 1:
            raise RuntimeError("delete_owner_memory failed")
        return None

    wipe_mocks.delete_workspace_runtime.side_effect = seam
    wipe_mocks.container_get.side_effect = lambda *a, **k: None if gone else {"owner_id": OWNER_ID}

    first = await _user_deleted(async_client)
    second = await _user_deleted(async_client)

    assert first.status_code == 503 and "runtime" in first.text
    assert second.status_code == 200, second.text
    assert gone == [OWNER_ID, OWNER_ID]  # the row-less redelivery ran the cleanup again


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["user_", "user_a/b", "../x", "org_abc"])
async def test_user_deleted_with_a_malformed_id_is_a_400_with_no_deletes(
    async_client,
    monkeypatch,
    wipe_mocks,  # noqa: F811
    user_id,
):
    _bypass_svix(monkeypatch)
    metric = MagicMock()
    monkeypatch.setattr("routers.webhooks.put_metric", metric)

    resp = await _user_deleted(async_client, user_id)

    assert resp.status_code == 400  # Svix does not retry a 4xx: no retry storm
    metric.assert_any_call("clerk.webhook.user_deleted_wipe_failed")
    wipe_mocks.user_get.assert_not_awaited()
    wipe_mocks.delete_workspace_runtime.assert_not_awaited()
    wipe_mocks.user_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_deleted_counts_a_wipe_that_fails_before_its_steps(async_client, monkeypatch, wipe_mocks):  # noqa: F811
    _bypass_svix(monkeypatch)
    metric = MagicMock()
    monkeypatch.setattr("routers.webhooks.put_metric", metric)
    wipe_mocks.user_get.side_effect = RuntimeError("ddb down")

    with pytest.raises(RuntimeError, match="ddb down"):  # the app error reaches the client: no 2xx to ack
        await _user_deleted(async_client)
    metric.assert_any_call("clerk.webhook.user_deleted_wipe_failed")
