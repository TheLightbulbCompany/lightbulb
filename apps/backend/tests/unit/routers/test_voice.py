"""GET /voice/timeline, POST /voice/calls/{id}/done, GET/PUT /voice/agent against the moto messaging table."""

import pytest
from unittest.mock import AsyncMock

from core.repositories import messaging_repo
from tests.unit.services.conftest import repos  # noqa: F401

WS = "org_test_456"


@pytest.fixture(autouse=True)
def legacy_billing_account(monkeypatch):
    from routers import voice

    monkeypatch.setattr(voice.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(voice.agent_registry, "roster", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "core.repositories.workspace_repo.WorkspaceRepo.resolve_owner_row", AsyncMock(return_value=None)
    )


@pytest.mark.usefixtures("repos")
async def test_paid_usage_returns_exact_utc_provider_period_instead_of_legacy_local_counter(
    async_client_org_admin, monkeypatch
):
    from routers import voice

    account = {"metronome_voice_contract_id": "voice", "voice_plan_status": "pending_payment"}
    monkeypatch.setattr(voice.billing_repo, "get_by_owner_id", AsyncMock(return_value=account))
    measured = {
        "used": 61 / 60,
        "usedSeconds": 61,
        "periodStart": "2026-11-08T00:00:00Z",
        "periodEnd": "2026-12-01T00:00:00Z",
        "timeZone": "UTC",
        "included": 1500,
        "overageRate": 0.35,
    }
    monkeypatch.setattr(voice.voice_plan, "usage", AsyncMock(return_value=measured))
    response = await async_client_org_admin.get("/api/v1/voice/usage")
    assert response.status_code == 200
    assert response.json()["usedSeconds"] == 61 and response.json()["used"] == 61 / 60
    assert response.json()["periodStart"] == measured["periodStart"] and response.json()["timeZone"] == "UTC"


async def _room(room_id: str, kind: str, created: str, owner: str = WS, **extra):
    t = messaging_repo._table()
    t.put_item(
        Item={
            "pk": messaging_repo.owner_pk(owner),
            "sk": messaging_repo.room_pk(room_id),
            "roomId": room_id,
            "kind": kind,
            "createdAt": created,
        }
    )
    t.put_item(
        Item={
            "pk": messaging_repo.room_pk(room_id),
            "sk": "META",
            "roomId": room_id,
            "ownerId": owner,
            "kind": kind,
            "createdAt": created,
            "revision": 1,
            **extra,
        }
    )


@pytest.mark.asyncio
async def test_timeline_lists_calls_and_meetings_newest_first(repos, async_client_org_admin):  # noqa: F811
    await _room(
        "c1",
        "call",
        "2026-11-07T10:00:00+00:00",
        call={"caller": "+14155550133", "outcome": "booked", "startedAt": "2026-11-07T10:00:00+00:00"},
    )
    await _room(
        "m1",
        "channel",
        "2026-11-01T00:00:00+00:00",
        meeting={
            "title": "Team",
            "startAt": 1794070800,
            "notes": "Ship Friday.",
            "decisions": ["Ship Friday"],
            "ticketNonce": "secret",
            "recallBotId": "b",
            "roster": {"p1": {"name": "Ana", "in": True}},
        },
    )
    await _room("ch", "channel", "2026-11-07T11:00:00+00:00")  # an ordinary channel: not on the timeline
    await _room("x", "call", "2026-11-07T12:00:00+00:00", owner="other")  # another workspace's

    items = (await async_client_org_admin.get("/api/v1/voice/timeline")).json()["items"]

    assert [i["roomId"] for i in items] == ["m1", "c1"]  # by when it happened, not when the room was made
    assert items[1] | {"type": "call", "outcome": "booked"} == items[1]
    assert items[0]["notes"] == "Ship Friday." and items[0]["decisions"] == ["Ship Friday"]
    assert items[0]["people"] == ["Ana"] and "ticketNonce" not in items[0] and "recallBotId" not in items[0]


@pytest.mark.asyncio
async def test_done_marks_the_call_resolved(repos, async_client_org_admin):  # noqa: F811
    await _room("c1", "call", "2026-11-07T10:00:00+00:00", call={"outcome": "needs_you"})
    assert (await async_client_org_admin.post("/api/v1/voice/calls/c1/done")).status_code == 200
    call = (await messaging_repo.get_room("c1"))["call"]
    assert call["outcome"] == "needs_you" and call["resolvedAt"]
    assert (await async_client_org_admin.post("/api/v1/voice/calls/nope/done")).status_code == 404


@pytest.mark.asyncio
async def test_agent_config_round_trip_admin_only(repos, async_client_org_admin):  # noqa: F811
    assert (await async_client_org_admin.get("/api/v1/voice/agent")).json()["config"] is None
    cfg = {"name": "Nova", "allow": {"book": True}, "hours": [{"day": "Mon", "open": "09:00"}], "price": 4.5}
    assert (await async_client_org_admin.put("/api/v1/voice/agent", json={"config": cfg})).status_code == 200
    assert (await async_client_org_admin.get("/api/v1/voice/agent")).json()["config"] == cfg
    big = {"x": "a" * 70_000}
    assert (await async_client_org_admin.put("/api/v1/voice/agent", json={"config": big})).status_code == 413


@pytest.mark.asyncio
async def test_agent_config_member_cannot_write(repos, async_client_org_member):  # noqa: F811
    assert (await async_client_org_member.put("/api/v1/voice/agent", json={"config": {}})).status_code == 403


@pytest.mark.asyncio
async def test_a_greeting_style_without_the_disclosure_words_saves(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    # The business owns its opening; the field is only style.
    monkeypatch.setattr("core.config.settings.VOICE_LINE_NUMBER", "+14842079294")  # Go live needs a number
    put = lambda cfg: async_client_org_admin.put("/api/v1/voice/agent", json={"config": cfg})  # noqa: E731
    assert (await put({"live": True, "greeting": "Hi, warm and brief."})).status_code == 200
    assert (await put({"live": True, "greeting": ""})).status_code == 200


@pytest.mark.asyncio
async def test_handoff_number_must_be_e164(repos, async_client_org_admin):  # noqa: F811
    put = lambda n: async_client_org_admin.put("/api/v1/voice/agent", json={"config": {"handoff": {"number": n}}})  # noqa: E731
    for bad in ("555-1234", "+0123456789", "1-900-555-01", "+1555123456789012", 5125550142):
        r = await put(bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "handoff_number_invalid", bad
    for ok in ("+15125550142", "(512) 555-0142", "1 512 555 0142", "+1 416 555 0142"):
        r = await put(ok)
        assert r.status_code == 200, ok
    assert r.json()["config"]["handoff"]["number"] == "+14165550142"  # stored as the worker dials it
    assert (await put("")).status_code == 200  # no handoff


@pytest.mark.asyncio
async def test_handoff_number_must_be_us_or_canada(repos, async_client_org_admin):  # noqa: F811
    """M2: the hand-off (and the run-out forward, which dials it) is a +1 US/CA number: never another country, a
    Caribbean NANP code billed as international, or a premium 900/976 line."""
    put = lambda n: async_client_org_admin.put("/api/v1/voice/agent", json={"config": {"handoff": {"number": n}}})  # noqa: E731
    for bad in ("+44 20 7946 0958", "+1 876 555 0123", "(809) 555-0123", "+1 900 555 0123", "976-555-0123",
                "+1 284 555 0123"):  # fmt: skip
        r = await put(bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "handoff_number_not_us", bad
    from routers.voice import us_number

    assert us_number("+1 340 555 0123") == "+13405550123"  # US territories are US
    assert us_number("+1 155 555 0123") is None  # not a NANP area code


async def _caller(owner: str):
    await messaging_repo.run_in_thread(
        messaging_repo._table().put_item,
        Item={
            "pk": messaging_repo.owner_pk(owner),
            "sk": "VOICECALLER#+14155550133",
            "name": "Priya",
            "facts": ["Prefers Saturdays"],
        },
    )


@pytest.mark.asyncio
async def test_forget_caller(repos, async_client_org_admin):  # noqa: F811
    await _caller(WS)
    await _caller("org_other")
    url = "/api/v1/voice/callers/+14155550133/memory"
    assert (await async_client_org_admin.delete(url)).json() == {"ok": True}
    row = await messaging_repo._get(messaging_repo.owner_pk(WS), "VOICECALLER#+14155550133")
    assert row["facts"] == [] and "name" not in row
    other = await messaging_repo._get(messaging_repo.owner_pk("org_other"), "VOICECALLER#+14155550133")
    assert other["facts"] == ["Prefers Saturdays"]
    assert (await async_client_org_admin.delete("/api/v1/voice/callers/+14155550999/memory")).status_code == 404


async def test_invalid_timezone_rejected_on_save(repos, async_client_org_admin):  # noqa: F811
    put = lambda tz: async_client_org_admin.put("/api/v1/voice/agent", json={"config": {"timezone": tz}})  # noqa: E731
    r = await put("Mars/Olympus")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "timezone_invalid"
    assert (await put("America/Chicago")).status_code == 200


async def _month_of_calls():
    """This month: two calls (3 min) and a held meeting (5 min); last month's call and another workspace's are out."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("America/New_York"))
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this, last = first.isoformat(), (first - timedelta(days=3)).isoformat()
    await _room("c1", "call", this, call={"startedAt": this, "durationSec": 120})
    await _room("c2", "call", this, call={"startedAt": this, "durationSec": 60})
    await _room("c0", "call", last, call={"startedAt": last, "durationSec": 6000})
    await _room("cx", "call", this, owner="other", call={"startedAt": this, "durationSec": 6000})
    t = int(first.timestamp())
    await _room("m1", "channel", last, meeting={"title": "Team", "inCallAt": t + 60, "endedAt": t + 360})


@pytest.mark.asyncio
async def test_usage_used_is_this_months_call_and_meeting_minutes(repos, async_client_org_admin):  # noqa: F811
    await _month_of_calls()
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["used"] == 8


@pytest.mark.asyncio
async def test_usage_included_minutes(repos, async_client_org_admin):  # noqa: F811
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["included"] == 1500


@pytest.mark.parametrize("contract,paid", [(False, False), (True, False), (True, True)])
@pytest.mark.usefixtures("repos")
async def test_usage_only_describes_an_allowance_when_payment_covers_now(
    async_client_org_admin, monkeypatch, contract, paid
):
    from unittest.mock import AsyncMock
    from routers import voice

    account = (
        {
            "metronome_voice_contract_id": "contract",
            "voice_plan_paid_periods": {"invoice": {"start": "2000-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"}}
            if paid
            else {},
        }
        if contract
        else {}
    )
    monkeypatch.setattr(voice.billing_repo, "get_by_owner_id", AsyncMock(return_value=account))
    monkeypatch.setattr(voice, "month_seconds", AsyncMock(return_value=6720))
    monkeypatch.setattr(
        voice.voice_plan,
        "usage",
        AsyncMock(
            return_value={
                "used": 112,
                "included": 1500,
                "overageRate": 0.35,
                "periodStart": "2026-11-01T00:00:00Z",
                "periodEnd": "2026-12-01T00:00:00Z",
            }
        ),
    )
    result = (await async_client_org_admin.get("/api/v1/voice/usage")).json()
    assert result["used"] == 112
    assert result["paidAllowance"] is paid
    assert result["planStatus"] == ("active" if paid else "pending_payment" if contract else "not_enrolled")


@pytest.mark.asyncio
async def test_usage_overage_rate(repos, async_client_org_admin):  # noqa: F811
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["overageRate"] == 0.35


@pytest.mark.asyncio
async def test_usage_run_out_comes_from_the_agent_page(repos, async_client_org_admin):  # noqa: F811
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["runOut"] == "keep"
    await messaging_repo.put_voice_agent(WS, {"runOut": "forward"}, "u")
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["runOut"] == "forward"


@pytest.mark.asyncio
async def test_usage_reads_the_months_counter_row_when_there_is_one(repos, async_client_org_admin):  # noqa: F811
    from datetime import UTC, datetime

    from core.billing import voice_billing

    await _month_of_calls()  # the scan would say 8
    await voice_billing.add_month_seconds(WS, 600, datetime.now(UTC))
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["used"] == 10


@pytest.mark.asyncio
async def test_handoff_number_cannot_be_the_forwarded_line(repos, async_client_org_admin):  # noqa: F811
    cfg = {"forwardFrom": "(415) 555-0177", "handoff": {"number": "+14155550177"}}
    r = await async_client_org_admin.put("/api/v1/voice/agent", json={"config": cfg})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "handoff_is_forwarded_line"


@pytest.mark.usefixtures("repos")
async def test_front_agent_selection_is_scoped_and_preserves_other_fields(async_client_org_admin, monkeypatch):
    from routers import voice

    monkeypatch.setattr(
        voice.agent_registry, "get", AsyncMock(return_value={"agentId": "existing", "status": "active"})
    )
    cfg = {
        "name": "Founder's name",
        "frontAgentId": "existing",
        "voice": "quartz",
        "spanish": True,
        "unknownSetting": {"keep": True},
    }
    response = await async_client_org_admin.put("/api/v1/voice/agent", json={"config": cfg})
    assert response.status_code == 200
    voice.agent_registry.get.assert_awaited_once_with(WS, "existing")
    assert await messaging_repo.get_voice_agent(WS) == cfg
    # An old client omits the selection when saving another setting; retain it.
    response = await async_client_org_admin.put("/api/v1/voice/agent", json={"config": {"name": "Updated"}})
    assert response.json()["config"]["frontAgentId"] == "existing"


@pytest.mark.parametrize("agent", [None, {"status": "paused"}, {"status": "deleted"}])
@pytest.mark.usefixtures("repos")
async def test_unavailable_or_foreign_selection_has_no_saved_effect(async_client_org_admin, monkeypatch, agent):
    from routers import voice

    await messaging_repo.put_voice_agent(WS, {"name": "Unchanged"}, "admin")
    monkeypatch.setattr(voice.agent_registry, "get", AsyncMock(return_value=agent))
    response = await async_client_org_admin.put("/api/v1/voice/agent", json={"config": {"frontAgentId": "foreign"}})
    assert response.status_code == 422
    assert await messaging_repo.get_voice_agent(WS) == {"name": "Unchanged"}


@pytest.mark.usefixtures("repos")
async def test_member_cannot_designate_front_agent(async_client_org_member, monkeypatch):
    from routers import voice

    lookup = AsyncMock()
    monkeypatch.setattr(voice.agent_registry, "get", lookup)
    response = await async_client_org_member.put("/api/v1/voice/agent", json={"config": {"frontAgentId": "someone"}})
    assert response.status_code == 403
    lookup.assert_not_awaited()


@pytest.mark.usefixtures("repos")
async def test_missing_selected_agent_is_visible_without_rewriting_the_selection(async_client_org_admin):
    await messaging_repo.put_voice_agent(WS, {"frontAgentId": "deleted-founder-worker", "name": "Preserved"}, "admin")
    response = await async_client_org_admin.get("/api/v1/voice/agent")
    assert response.status_code == 200
    assert response.json()["config"]["frontAgentId"] == "deleted-founder-worker"
    assert response.json()["frontAgentId"] is None
    assert "Choose an active" in response.json()["frontAgentIssue"]
    assert (await messaging_repo.get_voice_agent(WS))["name"] == "Preserved"


@pytest.mark.parametrize(
    "field,bad",
    [("spanish", "true"), ("spanish", 1), ("spanish", None), ("voice", "alloy"), ("voice", {}), ("voice", None)],
)
async def test_phone_preferences_are_validated(repos, async_client_org_admin, field, bad):  # noqa: F811
    response = await async_client_org_admin.put("/api/v1/voice/agent", json={"config": {field: bad}})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == f"{field}_invalid"


@pytest.mark.parametrize("voice", ["Warm", "marin", "quartz", "willow"])
async def test_phone_preferences_round_trip_without_rewriting_legacy(repos, async_client_org_admin, voice):  # noqa: F811
    cfg = {"name": "Founder assistant", "voice": voice, "spanish": True, "future": {"preserved": True}}
    response = await async_client_org_admin.put("/api/v1/voice/agent", json={"config": cfg})
    assert response.status_code == 200
    assert (await async_client_org_admin.get("/api/v1/voice/agent")).json()["config"] == cfg


@pytest.mark.usefixtures("repos")
async def test_receptionist_can_be_saved_before_phone_setup_without_phone_effects(async_client_org_admin, monkeypatch):
    from routers import voice

    monkeypatch.setattr(
        voice.agent_registry, "get", AsyncMock(return_value={"agentId": "existing", "status": "active"})
    )
    provision = AsyncMock()
    monkeypatch.setattr(voice.voice_onboarding, "provision_at_go_live", provision)
    assert (await async_client_org_admin.get("/api/v1/voice/agent")).json()["config"] is None
    config = {"frontAgentId": "existing", "onboardingStep": 1}
    response = await async_client_org_admin.put("/api/v1/voice/agent", json={"config": config})
    assert response.status_code == 200
    assert response.json()["config"] == config
    assert (await async_client_org_admin.get("/api/v1/voice/agent")).json()["config"] == config
    provision.assert_not_awaited()
