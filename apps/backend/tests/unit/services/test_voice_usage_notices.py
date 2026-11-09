"""Paid voice allowance notices use the frozen Metronome period and send once."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.billing import voice_billing, voice_plan, voice_usage_notices as notices


def _account():
    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start > now:
        start = start.replace(month=start.month - 1)
    return {
        "owner_id": "org-owner",
        "metronome_customer_id": "metronome-customer",
        "metronome_voice_contract_id": "voice-contract-a",
        "voice_plan_usage_key": "voice-usage-a",
        "voice_plan_status": "active",
        "voice_plan_started_at": start.isoformat(),
        "voice_plan_paid_periods": {
            "invoice-current": {
                "start": (now - timedelta(days=1)).isoformat(),
                "end": (now + timedelta(days=30)).isoformat(),
            }
        },
    }


async def _check(account, *, used_seconds, **overrides):
    now = datetime.now(UTC)
    period_start, period_end = voice_plan.period(account, at=now)
    measured = {
        "usedSeconds": used_seconds,
        "periodStart": period_start.isoformat(),
        "periodEnd": period_end.isoformat(),
    }
    with (
        patch.object(notices.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_plan, "usage", AsyncMock(return_value=measured)) as usage,
        patch.object(notices, "_owner_email", AsyncMock(return_value="owner@example.com")),
        patch.object(notices.email_service, "send_email", AsyncMock(return_value="ses-message")) as send,
        patch.object(notices.settings, "FRONTEND_URL", "https://dev.thelightbulb.company"),
    ):
        result = await notices.check_and_notify(
            "org-owner",
            contract_id=account["metronome_voice_contract_id"],
            usage_key=account["voice_plan_usage_key"],
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            **overrides,
        )
    return result, usage, send, measured


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (71_999, ()),
        (72_000, (80,)),
        (89_999, (80,)),
        (90_000, (100,)),
        (150_001, (100,)),
    ],
)
def test_thresholds_use_1500_minutes_as_90000_seconds(seconds, expected):
    assert notices.thresholds_reached(seconds) == expected
    assert voice_billing.INCLUDED_MINUTES * 60 == 90_000


async def test_delayed_provider_read_can_reach_80_percent_on_a_finite_retry(repos):
    account = _account()
    now = datetime.now(UTC)
    period_start, period_end = voice_plan.period(account, at=now)
    measured = {"periodStart": period_start.isoformat(), "periodEnd": period_end.isoformat()}
    usage = AsyncMock(side_effect=[{**measured, "usedSeconds": 71_999}, {**measured, "usedSeconds": 72_000}])
    send = AsyncMock(return_value="ses-message")
    with (
        patch.object(notices.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_plan, "usage", usage),
        patch.object(notices, "_owner_email", AsyncMock(return_value="owner@example.com")),
        patch.object(notices.email_service, "send_email", send),
    ):
        assert (
            await notices.check_and_notify(
                "org-owner",
                contract_id="voice-contract-a",
                usage_key="voice-usage-a",
                period_start=period_start.isoformat(),
                period_end=period_end.isoformat(),
            )
            == "below_threshold"
        )
        assert (
            await notices.check_and_notify(
                "org-owner",
                contract_id="voice-contract-a",
                usage_key="voice-usage-a",
                period_start=period_start.isoformat(),
                period_end=period_end.isoformat(),
            )
            == "sent"
        )
    send.assert_awaited_once()


async def test_100_percent_first_observation_sends_only_highest_and_covers_80(repos):
    account = _account()
    result, usage, send, measured = await _check(account, used_seconds=90_000)
    assert result == "sent"
    assert usage.await_args.kwargs["at"].astimezone(UTC).isoformat() == measured["periodStart"]
    send.assert_awaited_once()
    assert send.await_args.kwargs["to"] == "owner@example.com"
    assert send.await_args.kwargs["kind"] == "voice_allowance"
    content = send.await_args.kwargs["text"]
    assert "1,500 of 1,500 included minutes" in content
    assert "$0.35 per minute" in content
    assert "Calls continue" in content
    assert "Send calls to my phone" in content
    assert "https://dev.thelightbulb.company/voice/settings" in content

    start = voice_plan.period(account, at=datetime.now(UTC))[0]
    table = notices.messaging_repo._table()
    covered = table.get_item(
        Key={
            "pk": notices.messaging_repo.owner_pk("org-owner"),
            "sk": notices._notice_sk("voice-contract-a", start, 80),
        }
    ).get("Item")
    assert covered["status"] == "covered"


async def test_concurrent_crossings_send_once(repos):
    account = _account()
    now = datetime.now(UTC)
    period_start, period_end = voice_plan.period(account, at=now)
    measured = {"periodStart": period_start.isoformat(), "periodEnd": period_end.isoformat(), "usedSeconds": 72_000}
    send = AsyncMock(return_value="ses-message")
    with (
        patch.object(notices.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_plan, "usage", AsyncMock(return_value=measured)),
        patch.object(notices, "_owner_email", AsyncMock(return_value="owner@example.com")),
        patch.object(notices.email_service, "send_email", send),
    ):
        results = await asyncio.gather(
            *[
                notices.check_and_notify(
                    "org-owner",
                    contract_id="voice-contract-a",
                    usage_key="voice-usage-a",
                    period_start=period_start.isoformat(),
                    period_end=period_end.isoformat(),
                )
                for _ in range(8)
            ]
        )
    assert results.count("sent") == 1
    assert results.count("already_claimed") == 7
    send.assert_awaited_once()


async def test_unknown_ses_outcome_is_a_terminal_receipt_and_is_not_resent(repos):
    account = _account()
    now = datetime.now(UTC)
    period_start, period_end = voice_plan.period(account, at=now)
    measured = {"periodStart": period_start.isoformat(), "periodEnd": period_end.isoformat(), "usedSeconds": 72_000}
    send = AsyncMock(return_value=None)
    with (
        patch.object(notices.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_plan, "usage", AsyncMock(return_value=measured)),
        patch.object(notices, "_owner_email", AsyncMock(return_value="owner@example.com")),
        patch.object(notices.email_service, "send_email", send),
    ):
        args = dict(
            contract_id="voice-contract-a",
            usage_key="voice-usage-a",
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )
        assert await notices.check_and_notify("org-owner", **args) == "unknown"
        assert await notices.check_and_notify("org-owner", **args) == "already_claimed"
    send.assert_awaited_once()
    row = notices.messaging_repo._table().get_item(
        Key={
            "pk": notices.messaging_repo.owner_pk("org-owner"),
            "sk": notices._notice_sk("voice-contract-a", period_start, 80),
        }
    )["Item"]
    assert row["status"] == "unknown"


async def test_missing_owner_email_remains_retryable_before_claim(repos):
    account = _account()
    now = datetime.now(UTC)
    period_start, period_end = voice_plan.period(account, at=now)
    measured = {"periodStart": period_start.isoformat(), "periodEnd": period_end.isoformat(), "usedSeconds": 72_000}
    send = AsyncMock(return_value="ses-message")
    with (
        patch.object(notices.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_plan, "usage", AsyncMock(return_value=measured)),
        patch.object(notices, "_owner_email", AsyncMock(side_effect=[None, "owner@example.com"])),
        patch.object(notices.email_service, "send_email", send),
    ):
        args = dict(
            contract_id="voice-contract-a",
            usage_key="voice-usage-a",
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )
        assert await notices.check_and_notify("org-owner", **args) == "owner_email_unavailable"
        assert await notices.check_and_notify("org-owner", **args) == "sent"
    send.assert_awaited_once()


async def test_billing_recipient_is_current_plan_accepting_admin_not_provisioner():
    account = {"voice_plan_accepted_by": "user-payer"}
    members = [{"user_id": "user-payer", "role": "org:admin"}]
    with (
        patch.object(notices.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(notices.clerk_admin, "list_organization_members", AsyncMock(return_value=members)) as list_members,
        patch.object(notices.clerk_admin, "get_primary_email", AsyncMock(return_value="payer@example.com")) as email,
    ):
        assert await notices._owner_email("org_test") == "payer@example.com"
    list_members.assert_awaited_once_with("org_test", strict=True)
    email.assert_awaited_once_with("user-payer")


async def test_removed_or_non_admin_plan_acceptor_fails_closed():
    with (
        patch.object(
            notices.billing_repo, "get_by_owner_id", AsyncMock(return_value={"voice_plan_accepted_by": "user-payer"})
        ),
        patch.object(
            notices.clerk_admin,
            "list_organization_members",
            AsyncMock(return_value=[{"user_id": "user-payer", "role": "org:member"}]),
        ),
        patch.object(notices.clerk_admin, "get_primary_email", AsyncMock()) as email,
    ):
        assert await notices._owner_email("org_test") is None
    email.assert_not_awaited()


async def test_user_owned_billing_recipient_must_be_the_owner():
    with (
        patch.object(
            notices.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"voice_plan_accepted_by": "user-other"}),
        ),
        patch.object(notices.clerk_admin, "list_organization_members", AsyncMock()) as members,
        patch.object(notices.clerk_admin, "get_primary_email", AsyncMock()) as email,
    ):
        assert await notices._owner_email("user-owner") is None
    members.assert_not_awaited()
    email.assert_not_awaited()


async def test_user_owned_billing_recipient_uses_only_the_matching_acceptor():
    with (
        patch.object(
            notices.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"voice_plan_accepted_by": "user-owner"}),
        ),
        patch.object(notices.clerk_admin, "get_primary_email", AsyncMock(return_value="owner@example.com")) as email,
    ):
        assert await notices._owner_email("user-owner") == "owner@example.com"
    email.assert_awaited_once_with("user-owner")


async def test_changed_contract_and_expired_period_never_read_new_period_or_send(repos):
    account = _account()
    now = datetime.now(UTC)
    period_start, period_end = voice_plan.period(account, at=now)
    usage = AsyncMock(
        return_value={
            "usedSeconds": 90_000,
            "periodStart": period_start.isoformat(),
            "periodEnd": period_end.isoformat(),
        }
    )
    send = AsyncMock(return_value="ses-message")
    with (
        patch.object(notices.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_plan, "usage", usage),
        patch.object(notices, "_owner_email", AsyncMock(return_value="owner@example.com")),
        patch.object(notices.email_service, "send_email", send),
    ):
        changed = await notices.check_and_notify(
            "org-owner",
            contract_id="replacement-contract",
            usage_key="voice-usage-a",
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )
        assert changed == "inactive_or_changed_contract"
        old_start = period_start - timedelta(days=65)
        old_end = (old_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        expired = await notices.check_and_notify(
            "org-owner",
            contract_id="voice-contract-a",
            usage_key="voice-usage-a",
            period_start=old_start.isoformat(),
            period_end=old_end.isoformat(),
        )
    assert expired == "expired_period"
    usage.assert_not_awaited()
    send.assert_not_awaited()


async def test_transient_metronome_query_error_does_not_claim_or_send(repos):
    account = _account()
    now = datetime.now(UTC)
    period_start, period_end = voice_plan.period(account, at=now)
    with (
        patch.object(notices.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_plan, "usage", AsyncMock(side_effect=httpx.ConnectError("provider unavailable"))),
        patch.object(notices, "_owner_email", AsyncMock(return_value="owner@example.com")),
        patch.object(notices.email_service, "send_email", AsyncMock()) as send,
    ):
        with pytest.raises(httpx.ConnectError):
            await notices.check_and_notify(
                "org-owner",
                contract_id="voice-contract-a",
                usage_key="voice-usage-a",
                period_start=period_start.isoformat(),
                period_end=period_end.isoformat(),
            )
    send.assert_not_awaited()
    assert (
        not notices.messaging_repo._table()
        .get_item(
            Key={
                "pk": notices.messaging_repo.owner_pk("org-owner"),
                "sk": notices._notice_sk("voice-contract-a", period_start, 80),
            }
        )
        .get("Item")
    )
