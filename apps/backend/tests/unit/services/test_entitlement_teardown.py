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


@pytest.fixture(autouse=True)
def _no_real_openrouter_key_retirement():
    with patch("core.repositories.openrouter_key_repo.mark_retiring", new=AsyncMock(return_value=False)):
        yield


@pytest.fixture
def _runtime():
    """Stub the side effects, so tests assert on intent not on AWS."""
    with (
        patch("core.services.owner_facts.write_owner_facts", AsyncMock()) as facts,
        patch("core.services.hermes_provisioning.delete_workspace_runtime", AsyncMock()) as runtime,
        patch("core.billing.bedrock_aip.delete_owner_aips", AsyncMock()) as aips,
        patch("core.billing.bedrock_aip.delete_owner_bedrock_role", AsyncMock()) as role,
        patch(
            "core.services.paperclip_agent_lifecycle.disarm_owner_nightly_automation",
            AsyncMock(return_value={"heartbeats_checked": 1, "issues_cancelled": 2}),
        ) as disarm,
    ):
        # Returns the containers row's ALL_OLD image — the only handle on the
        # AIP arns once the row is gone.
        runtime.return_value = {
            "owner_id": "user_1",
            "bedrock_aip_arns": {"sonnet": "arn:aws:bedrock:us-east-1:1:application-inference-profile/abc"},
            "bedrock_role_arn": "arn:aws:iam::1:role/isol8-prod-owner-bedrock-user_1",
        }
        yield MagicMock(facts=facts, runtime=runtime, aips=aips, role=role, disarm=disarm)


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
    _runtime.runtime.assert_not_awaited()


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

    _runtime.runtime.assert_not_awaited()


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
    _runtime.runtime.assert_awaited_once_with("user_1")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_choice", ["openrouter_managed", "byo_key", "chatgpt_oauth"])
async def test_canceled_owner_retires_any_old_managed_key(_runtime, provider_choice):
    retire = AsyncMock(return_value=True)
    with (
        patch.object(
            entitlement_teardown.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "org_1",
                    "subscription_status": "canceled",
                    "provider_choice": provider_choice,
                }
            ),
        ),
        patch("core.repositories.openrouter_key_repo.mark_retiring", retire),
    ):
        assert await entitlement_teardown.teardown_if_unentitled("org_1", trigger="subscription_deleted") is True
    _runtime.runtime.assert_awaited_once_with("org_1")
    retire.assert_awaited_once_with("org_1")


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

    # Twice, not once: the entry check, then the recheck immediately before the
    # destructive call (see test_a_resubscribe_mid_teardown_aborts...).
    assert read.await_count == 2
    # Strongly consistent: an eventually consistent read here could observe the
    # pre-cancellation row and skip a teardown that should have happened.
    assert all(c.kwargs.get("consistent_read") is True for c in read.await_args_list)


@pytest.mark.asyncio
async def test_a_failing_step_does_not_abort_the_others(_runtime):
    """These run from webhook handlers, where raising means a 5xx and a provider
    retry — and retrying teardown re-enters calls that already half-succeeded."""
    _runtime.runtime = AsyncMock(side_effect=RuntimeError("ECS down"))

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
async def test_the_bedrock_reap_is_delegated_to_the_seam(_runtime):
    """Teardown does not reap anything itself.

    It used to, and that was the bug: it was ONE of four teardown paths, and the
    other three (admin account-wipe, DELETE /debug/provision, DELETE
    /debug/user-data) kept leaking into the same account-wide IAM role quota —
    dev, staging and prod share one AWS account. The reap now lives inside
    ``hermes_provisioning.delete_workspace_runtime``, the seam all four cross, so this path
    gets it by crossing the seam rather than by carrying its own copy."""
    assert await _cancel() is True

    _runtime.runtime.assert_awaited_once_with("user_1")
    _runtime.aips.assert_not_awaited()
    _runtime.role.assert_not_awaited()


def _emitted(metric, name, outcome):
    return any(
        c.args and c.args[0] == name and c.kwargs.get("dimensions", {}).get("outcome") == outcome
        for c in metric.call_args_list
    )


@pytest.mark.asyncio
async def test_teardown_makes_no_paperclip_call(_runtime):
    """The Paperclip company belongs to the workspace, and a lapsed
    entitlement is not a deleted workspace: teardown never disarms Paperclip
    timers (they are gone) and never builds a provisioning client to disable anything
    (``organization.deleted`` owns the workspace disable)."""
    build = AsyncMock()
    with (
        patch("core.services.paperclip_provisioning.build_paperclip_provisioning", build),
        patch.object(
            entitlement_teardown.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_1", "subscription_status": "canceled"}),
        ),
    ):
        assert await entitlement_teardown.teardown_if_unentitled("org_1", trigger="subscription_deleted") is True

    _runtime.disarm.assert_not_awaited()
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_resubscribe_mid_teardown_aborts_before_anything_is_destroyed():
    """THE customer-impacting race. The entitlement read at the top of teardown
    is several awaits stale by the time the destructive call happens, and two
    things land in that gap: a resubscribe, or a cancellation webhook
    redelivered after one (Stripe and Apple both retry on 5xx).

    Losing the container self-heals — the next /container/status poll
    reprovisions. The Bedrock resources reaped behind it do NOT: the
    resubscribe's provision already returned success, so nothing retries and the
    owner's chat hard-fails on Bedrock. So the recheck has to happen BEFORE the
    destructive call, not after."""
    # Unentitled on entry, entitled by the time the recheck runs.
    reads = AsyncMock(
        side_effect=[
            {"owner_id": "user_1", "subscription_status": "canceled"},
            {"owner_id": "user_1", "subscription_status": "active"},
        ]
    )
    runtime = AsyncMock()
    with (
        patch("core.services.owner_facts.write_owner_facts", AsyncMock()),
        patch("core.services.hermes_provisioning.delete_workspace_runtime", runtime),
        patch.object(entitlement_teardown.billing_repo, "get_by_owner_id", reads),
    ):
        ran = await entitlement_teardown.teardown_if_unentitled("user_1", trigger="subscription_deleted")

    assert ran is False
    # Nothing destructive ran — no runtime delete, so no reap behind it.
    runtime.assert_not_awaited()
    assert reads.await_count == 2


@pytest.mark.asyncio
async def test_the_pre_teardown_recheck_is_strongly_consistent(_runtime):
    """An eventually consistent read here would happily return the
    pre-resubscribe row and wave the teardown through — the exact failure the
    recheck exists to prevent."""
    reads = AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "canceled"})
    with patch.object(entitlement_teardown.billing_repo, "get_by_owner_id", reads):
        await entitlement_teardown.teardown_if_unentitled("user_1", trigger="test")

    assert reads.await_count == 2
    assert all(c.kwargs.get("consistent_read") is True for c in reads.await_args_list)


@pytest.mark.asyncio
async def test_a_failing_seam_does_not_abort_the_rest_of_the_teardown(_runtime):
    """Best-effort: this runs from a webhook handler where raising means a 5xx
    and a provider retry, and retrying re-enters calls that half-succeeded. The
    seam raises on a partial reap, so this covers that too."""
    _runtime.runtime = AsyncMock(side_effect=RuntimeError("reap incomplete"))

    assert await _cancel() is True

    _runtime.facts.assert_awaited_once()
