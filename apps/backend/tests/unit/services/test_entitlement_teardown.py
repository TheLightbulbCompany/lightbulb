"""Teardown keys off the DERIVED entitlement, not off which provider ended.

While Stripe was the only payer those were the same question. With Apple IAP
they are not, and getting it wrong is expensive in both directions: tearing
down someone who still pays takes away what they are paying for, and not
tearing down someone who has stopped leaves a container running whose agents
keep spending on inference.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import entitlement_teardown


@pytest.fixture
def _runtime():
    """Stub the side effects, so tests assert on intent not on AWS."""
    with (
        patch("core.services.owner_facts.write_owner_facts", AsyncMock()) as facts,
        patch("core.containers.get_ecs_manager") as ecs,
        patch(
            "core.services.paperclip_provisioning.build_paperclip_provisioning",
            AsyncMock(side_effect=RuntimeError("paperclip unavailable in tests")),
        ),
        patch("core.billing.bedrock_aip.delete_owner_aips", AsyncMock()) as aips,
        patch("core.billing.bedrock_aip.delete_owner_bedrock_role", AsyncMock()) as role,
    ):
        # Returns the containers row's ALL_OLD image — the only handle on the
        # AIP arns once the row is gone.
        ecs.return_value.delete_user_service = AsyncMock(
            return_value={
                "owner_id": "user_1",
                "bedrock_aip_arns": {"sonnet": "arn:aws:bedrock:us-east-1:1:application-inference-profile/abc"},
                "bedrock_role_arn": "arn:aws:iam::1:role/isol8-prod-owner-bedrock-user_1",
            }
        )
        yield MagicMock(facts=facts, ecs=ecs, aips=aips, role=role)


@pytest.mark.parametrize("status", ["active", "trialing", "trial_local", "past_due"])
@pytest.mark.asyncio
async def test_an_owner_someone_still_pays_for_is_left_alone(_runtime, status):
    """past_due is deliberately in here: a failed card and an Apple billing
    grace period are both recoverable, and destroying a workspace over a
    retryable payment is not a decision to make on someone's behalf."""
    with patch.object(
        entitlement_teardown.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "subscription_status": status}),
    ):
        ran = await entitlement_teardown.teardown_if_unentitled("user_1", trigger="test")

    assert ran is False
    _runtime.facts.assert_not_awaited()
    _runtime.ecs.return_value.delete_user_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_stripe_cancelling_does_not_evict_a_live_apple_subscriber(_runtime):
    """The dual-provider case. Stripe is canceled, Apple is active, so the
    derived status is active — and the owner keeps the container they are still
    paying Apple for."""
    with patch.object(
        entitlement_teardown.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "user_1",
                "stripe_subscription_status": "canceled",
                "apple_subscription_status": "active",
                "subscription_status": "active",
            }
        ),
    ):
        assert await entitlement_teardown.teardown_if_unentitled("user_1", trigger="subscription_deleted") is False

    _runtime.ecs.return_value.delete_user_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_last_provider_ending_tears_the_runtime_down(_runtime):
    with patch.object(
        entitlement_teardown.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "user_1",
                "stripe_subscription_status": "canceled",
                "apple_subscription_status": "canceled",
                "subscription_status": "canceled",
            }
        ),
    ):
        assert await entitlement_teardown.teardown_if_unentitled("user_1", trigger="apple_refund") is True

    _runtime.facts.assert_awaited_once_with("user_1", account_overrides={"subscription_status": "canceled"})
    _runtime.ecs.return_value.delete_user_service.assert_awaited_once_with("user_1")


@pytest.mark.asyncio
async def test_a_missing_row_is_not_entitled(_runtime):
    with patch.object(entitlement_teardown.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        assert await entitlement_teardown.teardown_if_unentitled("ghost", trigger="test") is True


@pytest.mark.asyncio
async def test_teardown_reads_the_row_itself_rather_than_trusting_a_caller(_runtime):
    """The caller has just written ONE provider's status, and the value that
    decides this is derived from both — so a snapshot taken before that write
    cannot answer the question."""
    read = AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "canceled"})
    with patch.object(entitlement_teardown.billing_repo, "get_by_owner_id", read):
        await entitlement_teardown.teardown_if_unentitled("user_1", trigger="test")

    read.assert_awaited_once()
    # Strongly consistent: an eventually consistent read here could observe the
    # pre-cancellation row and skip a teardown that should have happened.
    assert read.await_args.kwargs.get("consistent_read") is True


@pytest.mark.asyncio
async def test_a_failing_step_does_not_abort_the_others(_runtime):
    """These run from webhook handlers, where raising means a 5xx and a provider
    retry — and retrying teardown re-enters calls that already half-succeeded."""
    _runtime.ecs.return_value.delete_user_service = AsyncMock(side_effect=RuntimeError("ECS down"))

    with patch.object(
        entitlement_teardown.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "canceled"}),
    ):
        assert await entitlement_teardown.teardown_if_unentitled("user_1", trigger="test") is True

    # Facts still written despite the ECS failure.
    _runtime.facts.assert_awaited_once()


async def _cancel(owner_id="user_1"):
    with patch.object(
        entitlement_teardown.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": owner_id, "subscription_status": "canceled"}),
    ):
        return await entitlement_teardown.teardown_if_unentitled(owner_id, trigger="test")


@pytest.mark.asyncio
async def test_teardown_reaps_the_per_owner_bedrock_role_and_aips(_runtime):
    """IAM roles are capped at 1,000 per ACCOUNT, and `delete_user_service`
    destroys the containers row that is the only handle on the AIP arns. Before
    this, only the admin account-wipe deleted either, so every cancellation
    leaked a role plus its inference profiles with nothing able to reach them
    again."""
    assert await _cancel() is True

    _runtime.role.assert_awaited_once_with("user_1")
    _runtime.aips.assert_awaited_once()
    assert _runtime.aips.await_args.kwargs["aip_arns"] == [
        "arn:aws:bedrock:us-east-1:1:application-inference-profile/abc"
    ]


@pytest.mark.asyncio
async def test_the_role_is_reaped_even_when_the_row_never_recorded_its_arn(_runtime):
    """The role name derives from owner_id alone and the delete is idempotent,
    so this must NOT be gated on a `bedrock_role_arn` field. Gating on the row
    is precisely what stranded the roles already orphaned in prod: once the row
    is gone, a row-gated delete can never reach them."""
    _runtime.ecs.return_value.delete_user_service = AsyncMock(return_value=None)

    assert await _cancel() is True

    _runtime.role.assert_awaited_once_with("user_1")
    _runtime.aips.assert_not_awaited()  # no arns to work from


@pytest.mark.asyncio
async def test_a_surviving_container_keeps_its_role(_runtime):
    """If the ECS delete failed the task may still be running under this role,
    and pulling it out from under a live container turns a retryable teardown
    into a broken one. The next trigger re-runs this."""
    _runtime.ecs.return_value.delete_user_service = AsyncMock(side_effect=RuntimeError("ECS down"))

    assert await _cancel() is True

    _runtime.role.assert_not_awaited()
    _runtime.aips.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_role_delete_does_not_abort_the_teardown(_runtime):
    """Best-effort like every other step — this runs from a webhook handler
    where raising means a 5xx and a provider retry."""
    _runtime.role.side_effect = RuntimeError("IAM down")
    _runtime.aips.side_effect = RuntimeError("Bedrock down")

    assert await _cancel() is True

    _runtime.facts.assert_awaited_once()
    # Both were attempted: a raising AIP delete must not skip the role delete,
    # which is the half that actually frees the account-wide quota.
    _runtime.aips.assert_awaited_once()
    _runtime.role.assert_awaited_once()
