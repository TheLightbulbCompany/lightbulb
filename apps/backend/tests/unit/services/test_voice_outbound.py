"""The forwarding check (Task 10): one outbound SIP call to the owner's business line, mocked LiveKit."""

from unittest.mock import AsyncMock, MagicMock


from core.config import settings
from core.repositories import messaging_repo
from core.services import livekit_huddles, voice_outbound
from routers import voice_phone
from tests.unit.services.conftest import repos  # noqa: F401

WS = "org_test_456"  # the conftest org admin's workspace
CFG = {"name": "Nova", "number": "+14155550100", "forwardFrom": "(415) 555-0177"}


def fake_livekit(monkeypatch) -> MagicMock:
    lk = MagicMock()
    lk.sip.create_sip_participant = AsyncMock()
    lk.room.delete_room = AsyncMock()
    cm = MagicMock(__aenter__=AsyncMock(return_value=lk), __aexit__=AsyncMock(return_value=False))
    monkeypatch.setattr(livekit_huddles, "_api", lambda: cm)
    for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.setattr(settings, k, "x")
    return lk


async def test_forward_check_unconfigured_returns_reason(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    lk = fake_livekit(monkeypatch)
    monkeypatch.setattr(settings, "VOICE_OUTBOUND_TRUNK_ID", "")
    assert await voice_outbound.forward_check(WS, CFG) == {"ok": False, "reason": "outbound_not_configured"}
    r = await async_client_org_admin.post("/api/v1/voice/forward-check")
    assert r.status_code == 200 and r.json() == {"ok": False, "reason": "outbound_not_configured"}
    lk.sip.create_sip_participant.assert_not_called()
    assert await messaging_repo.get_voice_agent(WS) is None


async def test_forward_check_dials_business_line(repos, monkeypatch):  # noqa: F811
    lk = fake_livekit(monkeypatch)
    monkeypatch.setattr(settings, "VOICE_OUTBOUND_TRUNK_ID", "ST_out")
    await messaging_repo.put_voice_agent(WS, {**CFG, "forwardingOk": True}, "u")
    assert (await voice_outbound.forward_check(WS, CFG))["reason"] == "no_line"  # no line row: nothing dialed
    lk.sip.create_sip_participant.assert_not_called()
    await voice_phone.put_line("+14155550100", {"ownerId": WS, "ownerUserId": "u"})
    assert await voice_outbound.forward_check(WS, {**CFG, "forwardingOk": True}) == {"ok": True}
    req = lk.sip.create_sip_participant.call_args.args[0]
    assert (req.sip_trunk_id, req.sip_call_to, req.sip_number) == ("ST_out", "+14155550177", "+14155550100")
    assert req.room_name.startswith("phone-out-") and req.ringing_timeout.seconds == 60
    assert req.max_call_duration.seconds == 60
    cfg = await messaging_repo.get_voice_agent(WS)
    assert cfg["forwardingCheck"]["callId"] == req.room_name.removeprefix("phone-out-")
    assert cfg["forwardingCheck"]["startedAt"] and "forwardingOk" not in cfg  # a new check clears the old tick


async def test_forward_check_owner_only(repos, async_client_org_member, monkeypatch):  # noqa: F811
    lk = fake_livekit(monkeypatch)
    monkeypatch.setattr(settings, "VOICE_OUTBOUND_TRUNK_ID", "ST_out")
    await messaging_repo.put_voice_agent(WS, CFG, "u")
    assert (await async_client_org_member.post("/api/v1/voice/forward-check")).status_code == 403
    lk.sip.create_sip_participant.assert_not_called()
