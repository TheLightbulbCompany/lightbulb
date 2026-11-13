"""A lapsed voice plan's bought number goes back to Twilio after the 30-day grace, and only ours."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from core.repositories import messaging_repo
from core.services import voice_onboarding as vo
from tests.unit.services.conftest import repos  # noqa: F401

WS = "org_lapsed"
NUM = "+16105550199"
SHARED = "+14842079294"
NOW = datetime(2026, 11, 10, tzinfo=UTC)


def _account(days_ago: int, status: str = "canceled") -> dict:
    return {
        "owner_id": WS,
        "voice_plan_stripe_subscription_id": "sub_1",
        "voice_plan_stripe_status": status,
        "voice_plan_ending_at": (NOW - timedelta(days=days_ago)).isoformat(),
    }


def _setup(monkeypatch, account: dict, *, number: str = NUM, tag: str | None = None) -> AsyncMock:
    monkeypatch.setattr(vo.settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(vo.settings, "VOICE_LINE_NUMBER", SHARED)
    monkeypatch.setattr(vo.settings, "VOICE_INBOUND_TRUNK_ID", "ST_inbound")
    monkeypatch.setattr(vo.billing_repo, "scan_all", AsyncMock(return_value=[account]))
    monkeypatch.setattr(vo.billing_repo, "get_by_owner_id", AsyncMock(return_value=account))
    monkeypatch.setattr(vo.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="tok"))
    monkeypatch.setattr(vo.container_repo, "release_owner_lifecycle_claim", AsyncMock())
    monkeypatch.setattr(vo, "_route_number", AsyncMock())
    twilio = AsyncMock(return_value={})
    monkeypatch.setattr(vo, "_twilio", twilio)
    table = messaging_repo._table()
    table.put_item(Item={"pk": messaging_repo.owner_pk(WS), "sk": "VOICENUMBER", "number": number, "sid": "PN9",
                         "friendlyName": tag or vo._tag(WS)})  # fmt: skip
    table.put_item(Item={"pk": f"PHONE#{number}", "sk": "LINE", "ownerId": WS, "sid": "PN9"})
    return twilio


async def _seed_agent(number: str = NUM) -> None:
    await messaging_repo.put_voice_agent(WS, {"number": number, "live": True}, "user_a")


async def _kept(number: str = NUM) -> bool:
    return bool(await messaging_repo._get(messaging_repo.owner_pk(WS), "VOICENUMBER")) and bool(
        await messaging_repo._get(f"PHONE#{number}", "LINE")
    )


@pytest.mark.asyncio
async def test_releases_after_grace_and_rerun_is_a_noop(repos, monkeypatch):  # noqa: F811
    twilio = _setup(monkeypatch, _account(31))
    await _seed_agent()
    assert await vo.release_lapsed_numbers(NOW) == 1
    twilio.assert_awaited_once_with("DELETE", "IncomingPhoneNumbers/PN9.json")
    vo._route_number.assert_awaited_once_with(NUM, remove=True)
    assert not await messaging_repo._get(f"PHONE#{NUM}", "LINE")
    assert not await messaging_repo._get(messaging_repo.owner_pk(WS), "VOICENUMBER")
    cfg = await messaging_repo.get_voice_agent(WS)
    assert cfg["live"] is False and cfg["number"] == ""
    assert await vo.release_lapsed_numbers(NOW) == 0  # nothing left to release
    assert twilio.await_count == 1


@pytest.mark.asyncio
async def test_keeps_within_grace(repos, monkeypatch):  # noqa: F811
    twilio = _setup(monkeypatch, _account(29))
    assert await vo.release_lapsed_numbers(NOW) == 0
    twilio.assert_not_awaited()
    assert await _kept()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
async def test_keeps_when_renewed(repos, monkeypatch, status):  # noqa: F811
    twilio = _setup(monkeypatch, _account(31, status))
    assert await vo.release_lapsed_numbers(NOW) == 0
    twilio.assert_not_awaited()
    assert await _kept()


@pytest.mark.asyncio
async def test_keeps_when_renewed_after_the_scan(repos, monkeypatch):  # noqa: F811
    twilio = _setup(monkeypatch, _account(31))
    vo.billing_repo.get_by_owner_id.return_value = _account(31, "active")  # the consistent re-read wins
    assert await vo.release_lapsed_numbers(NOW) == 0
    twilio.assert_not_awaited()
    assert await _kept()


@pytest.mark.asyncio
async def test_skips_the_shared_line_and_foreign_tags(repos, monkeypatch):  # noqa: F811
    twilio = _setup(monkeypatch, _account(31), number=SHARED)
    assert await vo.release_lapsed_numbers(NOW) == 0
    twilio.assert_not_awaited()
    assert await _kept(SHARED)
    messaging_repo._table().delete_item(Key={"pk": f"PHONE#{SHARED}", "sk": "LINE"})
    twilio = _setup(monkeypatch, _account(31), tag="someone-elses-number")
    assert await vo.release_lapsed_numbers(NOW) == 0
    twilio.assert_not_awaited()
    assert await _kept()


@pytest.mark.asyncio
async def test_twilio_404_counts_as_released(repos, monkeypatch):  # noqa: F811
    twilio = _setup(monkeypatch, _account(40))
    request = httpx.Request("DELETE", "https://api.twilio.com/x")
    twilio.side_effect = httpx.HTTPStatusError("gone", request=request, response=httpx.Response(404, request=request))
    assert await vo.release_lapsed_numbers(NOW) == 1
    assert not await messaging_repo._get(messaging_repo.owner_pk(WS), "VOICENUMBER")


@pytest.mark.asyncio
async def test_twilio_error_keeps_the_receipt_for_the_next_pass(repos, monkeypatch):  # noqa: F811
    twilio = _setup(monkeypatch, _account(40))
    request = httpx.Request("DELETE", "https://api.twilio.com/x")
    twilio.side_effect = httpx.HTTPStatusError("down", request=request, response=httpx.Response(500, request=request))
    assert await vo.release_lapsed_numbers(NOW) == 0
    assert await _kept()


@pytest.mark.asyncio
async def test_never_deletes_another_workspaces_line(repos, monkeypatch):  # noqa: F811
    _setup(monkeypatch, _account(31))
    messaging_repo._table().put_item(Item={"pk": f"PHONE#{NUM}", "sk": "LINE", "ownerId": "org_other"})
    assert await vo.release_lapsed_numbers(NOW) == 1
    assert (await messaging_repo._get(f"PHONE#{NUM}", "LINE"))["ownerId"] == "org_other"
