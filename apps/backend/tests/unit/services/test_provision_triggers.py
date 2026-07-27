"""``ensure_owner_provisioned`` — the auth-free, owner-scoped provisioning trigger.

This is the single entry point the Clerk and Stripe webhooks call. Its whole
contract is: evaluate the gate, dispatch if clear, never raise, and report
honestly which of those happened via ``provision.trigger{source,result}``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.services.provision_gate import Gate
from core.services.user_provision import (
    ProvisionTriggerResult,
    ensure_owner_provisioned,
)


def _gate(code: str = "signup_required") -> Gate:
    return Gate(
        code=code,
        title="t",
        message="m",
        action_label="a",
        action_href="/onboarding",
        action_admin_only=True,
        owner_role="owner",
    )


@pytest.mark.asyncio
async def test_clear_gate_dispatches_once_for_that_owner():
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ) as mock_gate,
        patch(
            "core.services.provision_dispatch.dispatch_provision",
            new=AsyncMock(return_value="dispatched"),
        ) as mock_dispatch,
    ):
        result = await ensure_owner_provisioned("user_abc", trigger="clerk.user_created")

    assert result is ProvisionTriggerResult.DISPATCHED
    mock_dispatch.assert_awaited_once_with("user_abc")
    # is_admin=True is correct BY CONSTRUCTION — this entry point is
    # owner-scoped, never member-scoped.
    mock_gate.assert_awaited_once_with(owner_id="user_abc", is_admin=True)


@pytest.mark.asyncio
async def test_gate_up_does_not_dispatch():
    """A gate being up is a normal return, not an error — and must have no
    side effects at all."""
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=_gate("team_billing_required")),
        ),
        patch(
            "core.services.provision_dispatch.dispatch_provision",
            new=AsyncMock(),
        ) as mock_dispatch,
    ):
        result = await ensure_owner_provisioned("org_acme", trigger="stripe.subscription_live")

    assert result is ProvisionTriggerResult.GATED
    mock_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_lost_reports_not_claimable():
    """The single-flight refusing is the fence working, not a failure. It must
    be distinguishable from a real dispatch in the metric, which is the whole
    reason dispatch_provision returns an outcome."""
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.provision_dispatch.dispatch_provision",
            new=AsyncMock(return_value="claim_lost"),
        ),
    ):
        result = await ensure_owner_provisioned("user_abc", trigger="clerk.user_created")

    assert result is ProvisionTriggerResult.NOT_CLAIMABLE


@pytest.mark.asyncio
async def test_gate_failure_never_escapes_into_the_webhook():
    """A raise here would 5xx a webhook; Svix counts that as a delivery failure
    and silently disables the endpoint after 5 days of them."""
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(side_effect=RuntimeError("dynamo down")),
        ),
        patch(
            "core.services.provision_dispatch.dispatch_provision",
            new=AsyncMock(),
        ) as mock_dispatch,
    ):
        result = await ensure_owner_provisioned("user_abc", trigger="clerk.user_created")

    assert result is ProvisionTriggerResult.ERROR
    mock_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_metric_carries_source_and_result():
    """``provision.trigger{source=status_poll, result=dispatched}`` falling to
    ~0 for fresh signups is the single number that proves the event trigger is
    live, so both dimensions have to actually be emitted."""
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.provision_dispatch.dispatch_provision",
            new=AsyncMock(return_value="dispatched"),
        ),
        patch("core.services.user_provision.put_metric") as mock_metric,
    ):
        await ensure_owner_provisioned("user_abc", trigger="clerk.user_created")

    mock_metric.assert_any_call(
        "provision.trigger",
        dimensions={"source": "clerk.user_created", "result": "dispatched"},
    )


@pytest.mark.asyncio
async def test_poll_path_does_not_re_evaluate_the_gate():
    """``ensure_user_ready`` evaluates the gate once at its top and only reaches
    ``_provision_or_reconcile`` when it was clear. Re-gating in the dispatch
    would re-read the billing row on every ~3s poll of every pre-ready owner —
    the exact load cost this change exists to remove.
    """
    from core.services.user_provision import _provision_or_reconcile

    auth = type("A", (), {"user_id": "user_abc", "is_org_context": False, "is_org_admin": True, "email": ""})()

    with (
        patch(
            "core.services.user_provision.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),  # no row -> dispatch branch
        ),
        patch(
            "core.services.user_provision.wake_policy.is_parked_trial",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ) as mock_gate,
        patch(
            "core.services.provision_dispatch.dispatch_provision",
            new=AsyncMock(return_value="dispatched"),
        ) as mock_dispatch,
    ):
        await _provision_or_reconcile(auth, "user_abc")
        # The dispatch is fire-and-forget so the nightly-CEO arm doesn't queue
        # behind a StartExecution round trip — drain the task before asserting.
        import asyncio

        await asyncio.sleep(0)

    mock_gate.assert_not_awaited()
    mock_dispatch.assert_awaited_once_with("user_abc")


@pytest.mark.asyncio
async def test_parked_trial_box_is_never_woken_by_a_trigger():
    """Eligibility != intent. A parked trial container is `stopped`, which
    `try_claim_provision` ADMITS — so a dispatch against it is a wake. Explicit
    wakes go through `container_wake.request_wake`; an eligibility event must
    not burn free-tier budget on a box nobody asked for.
    """
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "stopped", "stop_reason": "trial_idle"}),
        ),
        patch(
            "core.services.user_provision.wake_policy.is_parked_trial",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "core.services.provision_dispatch.dispatch_provision",
            new=AsyncMock(),
        ) as mock_dispatch,
    ):
        result = await ensure_owner_provisioned("user_parked", trigger="trial.bedrock_restored")

    assert result is ProvisionTriggerResult.PARKED
    mock_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unparked_container_still_dispatches():
    """The suppression is narrow: only a healthy, deliberately parked trial box.
    Everything else still provisions."""
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "stopped", "stop_reason": "admin"}),
        ),
        patch(
            "core.services.user_provision.wake_policy.is_parked_trial",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "core.services.provision_dispatch.dispatch_provision",
            new=AsyncMock(return_value="dispatched"),
        ) as mock_dispatch,
    ):
        result = await ensure_owner_provisioned("user_x", trigger="clerk.user_created")

    assert result is ProvisionTriggerResult.DISPATCHED
    mock_dispatch.assert_awaited_once_with("user_x")


@pytest.mark.asyncio
async def test_parked_check_failure_falls_through_to_dispatch():
    """`is_parked_trial` fails OPEN on every uncertainty by design; the row read
    guarding it must too. Self-heal never depends on the predicate."""
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.container_repo.get_by_owner_id",
            new=AsyncMock(side_effect=RuntimeError("dynamo down")),
        ),
        patch(
            "core.services.provision_dispatch.dispatch_provision",
            new=AsyncMock(return_value="dispatched"),
        ) as mock_dispatch,
    ):
        result = await ensure_owner_provisioned("user_x", trigger="clerk.user_created")

    assert result is ProvisionTriggerResult.DISPATCHED
    mock_dispatch.assert_awaited_once_with("user_x")
