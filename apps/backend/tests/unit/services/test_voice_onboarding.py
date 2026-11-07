"""Onboarding: the website draft (only what the site states, shaped for the Agent page) and the number."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.repositories import messaging_repo
from core.services import link_preview, voice_onboarding as vo
from tests.unit.services.conftest import repos  # noqa: F401

WS = "org_test_456"
LINE = "+14842079294"
HOME = """<html><body><script>x()</script><h1>Maya's Nails</h1><p>Open Mon-Fri 9-7</p>
<a href="/services">Our services</a><a href="https://other.com/services">elsewhere</a><a href="/blog">Blog</a></body></html>"""


def _bedrock(text: str) -> MagicMock:
    client = MagicMock()
    client.converse.return_value = {"output": {"message": {"content": [{"text": text}]}}}
    return client


async def test_read_site_follows_only_useful_same_site_links(monkeypatch):
    fetched = []

    async def safe_get(url, *, accept):
        fetched.append(url)
        return url, (HOME if url.endswith("nails.com") else "<p>Gel $55</p>").encode(), "text/html"

    monkeypatch.setattr(link_preview, "safe_get", safe_get)
    text = await vo.read_site("nails.com")
    assert fetched == ["https://nails.com", "https://nails.com/services"]
    assert "Open Mon-Fri 9-7" in text and "Gel $55" in text and "x()" not in text


async def test_draft_keeps_only_agent_page_shapes(monkeypatch):
    monkeypatch.setattr(vo, "read_site", AsyncMock(return_value="site text"))
    reply = (
        'Here: {"hours": {"Mon": {"open": "09:00", "close": "19:00"}}, "services": [{"name": "Gel manicure", '
        '"minutes": 45, "minutesGuessed": true, "price": "$55"}, {"price": "$1"}], "staff": ["Ana"], '
        '"knowledge": [{"q": "Parking?", "a": "Behind the building"}], "toAdd": ["Walk-ins?"], "evil": 1}'
    )
    monkeypatch.setattr(vo, "_bedrock", lambda: _bedrock(reply))
    d = await vo.draft("nails.com", "Maya's", "Austin")
    assert d["read"] is True and "evil" not in d
    assert d["hours"] == {}  # one day is not a week: the owner sets hours
    assert d["services"] == [
        {"name": "Gel manicure", "minutes": 45, "price": "$55", "staff": "", "minutesGuessed": True}
    ]
    assert d["knowledge"] == [{"q": "Parking?", "a": "Behind the building", "source": "from site"}]
    assert d["toAdd"] == ["Walk-ins?"] and d["staff"] == ["Ana"]


async def test_an_unreadable_site_or_a_bad_reply_is_an_empty_draft(monkeypatch):
    monkeypatch.setattr(vo, "read_site", AsyncMock(side_effect=link_preview.Refused("non-public")))
    assert (await vo.draft("10.0.0.1", "", ""))["read"] is False
    import httpx

    for exc in (httpx.ConnectError("down"), httpx.ReadTimeout("slow")):  # gate 16: not OSError/TimeoutError
        monkeypatch.setattr(vo, "read_site", AsyncMock(side_effect=exc))
        assert (await vo.draft("nails.com", "", ""))["read"] is False
    monkeypatch.setattr(vo, "read_site", AsyncMock(return_value="t"))
    monkeypatch.setattr(vo, "_bedrock", lambda: _bedrock("no json here"))
    d = await vo.draft("nails.com", "", "")
    assert d["read"] is True and d["services"] == [] and d["hours"] == {}


@pytest.mark.asyncio
async def test_assign_number_gives_the_configured_line_once(repos, monkeypatch):  # noqa: F811
    monkeypatch.setattr(vo.settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(vo.settings, "VOICE_LINE_NUMBER", LINE)
    assert await vo.assign_number(WS, "user_a") == LINE
    assert await vo.assign_number(WS, "user_b") == LINE  # idempotent, keeps the first owner user
    line = await messaging_repo._get(f"PHONE#{LINE}", "LINE")
    assert line["ownerId"] == WS and line["ownerUserId"] == "user_a"
    with pytest.raises(vo.NoNumber, match="taken"):
        await vo.assign_number("org_other", "user_c")
    assert await vo.release_number("org_other") == ""  # never another workspace's line
    assert await vo.release_number(WS) == LINE  # the wipe frees it
    assert await messaging_repo._get(f"PHONE#{LINE}", "LINE") is None
    assert await vo.assign_number("org_other", "user_c") == LINE
    monkeypatch.setattr(vo.settings, "VOICE_LINE_NUMBER", "")
    with pytest.raises(vo.NoNumber, match="not_available"):
        await vo.assign_number(WS, "user_a")
    assert await vo.release_number("org_other") == ""  # nothing configured, nothing to release


@pytest.mark.asyncio
async def test_number_route_says_why_there_is_none(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(vo.settings, "VOICE_LINE_NUMBER", LINE)
    messaging_repo._table().put_item(Item={"pk": f"PHONE#{LINE}", "sk": "LINE", "ownerId": "org_other"})
    r = await async_client_org_admin.post("/api/v1/voice/number")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "taken"


def test_bad_model_shapes_skip_the_field_or_item_not_the_draft():
    d = vo._clean(
        {
            "hours": ["Mon 9-5"],
            "services": [{"name": "Gel", "minutes": "about an hour"}, "Pedicure", {"name": "Wax", "minutes": 30}],
            "staff": "Ana",
            "knowledge": {"q": "not a list"},
            "toAdd": "Parking?",
            "phone": {"n": 1},
        }
    )
    assert d["hours"] == {} and d["phone"] == "" and d["knowledge"] == []
    assert [(s["name"], s["minutes"], s["minutesGuessed"]) for s in d["services"]] == [
        ("Gel", 45, True),
        ("Wax", 30, False),
    ]
    assert d["staff"] == ["Ana"] and d["toAdd"] == ["Parking?"]
    assert vo._clean(["not", "an", "object"])["services"] == []


@pytest.mark.asyncio
async def test_a_member_cannot_draft_or_take_a_number(repos, async_client_org_member):  # noqa: F811
    r = await async_client_org_member.post("/api/v1/voice/onboarding/draft", json={"website": "nails.com"})
    assert r.status_code == 403
    assert (await async_client_org_member.post("/api/v1/voice/number")).status_code == 403
    assert await messaging_repo._get(f"PHONE#{LINE}", "LINE") is None


# --- Numbers at onboarding (Task 11): keep-and-forward or a new number, bought at Go live, gated on KYC ---

NEW = "+16105550123"


def _fake_twilio(monkeypatch, number: str = NEW) -> AsyncMock:
    """Every Twilio REST call, mocked: the search finds `number`, the buy returns its PN sid, the release is 204."""

    async def call(method, path, **params):
        if path.startswith("AvailablePhoneNumbers"):
            return {"available_phone_numbers": [{"phone_number": number}]}
        if method == "POST":
            return {"sid": "PN123", "phone_number": params["PhoneNumber"]}
        return {}

    fake = AsyncMock(side_effect=call)
    monkeypatch.setattr(vo, "_twilio", fake)
    return fake


def _buying(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(vo.settings, "VOICE_BUY_NUMBERS", on)
    monkeypatch.setattr(vo.settings, "TWILIO_ACCOUNT_SID", "AC123" if on else "")
    monkeypatch.setattr(vo.settings, "TWILIO_AUTH_TOKEN", "tok" if on else "")
    monkeypatch.setattr(vo.settings, "TWILIO_TRUNK_SID", "TK123" if on else "")
    monkeypatch.setattr(vo.settings, "VOICE_LINE_NUMBER", LINE)


@pytest.mark.asyncio
async def test_reserve_buys_nothing(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    choice = await vo.reserve(WS, "keep", None, "verizon", "missed", user_id="user_a")
    assert choice == {"path": "keep", "areaCode": None, "carrier": "verizon", "mode": "missed"}
    assert (await messaging_repo.get_voice_agent(WS))["numberChoice"] == choice
    twilio.assert_not_awaited()
    assert await messaging_repo._get(f"PHONE#{LINE}", "LINE") is None


@pytest.mark.asyncio
async def test_number_route_stores_the_choice(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    _buying(monkeypatch, False)
    r = await async_client_org_admin.post("/api/v1/voice/number", json={"path": "new", "areaCode": "610"})
    assert r.status_code == 200 and r.json()["numberChoice"]["areaCode"] == "610"
    bad = await async_client_org_admin.post("/api/v1/voice/number", json={"path": "port"})
    assert bad.status_code == 422


async def _go_live(client, cfg: dict | None = None):
    return await client.put("/api/v1/voice/agent", json={"config": {"live": True, **(cfg or {})}})


@pytest.mark.asyncio
async def test_go_live_uses_configured_number_when_buying_off(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    _buying(monkeypatch, False)
    twilio = _fake_twilio(monkeypatch)
    r = await _go_live(async_client_org_admin, {"numberChoice": {"path": "new", "areaCode": "610"}})
    assert r.status_code == 200 and r.json()["config"]["number"] == LINE
    assert (await messaging_repo.get_voice_agent(WS))["number"] == LINE
    assert (await messaging_repo._get(f"PHONE#{LINE}", "LINE"))["ownerId"] == WS
    twilio.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_workspace_409_when_buying_off(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    _buying(monkeypatch, False)
    messaging_repo._table().put_item(Item={"pk": f"PHONE#{LINE}", "sk": "LINE", "ownerId": "org_other"})
    r = await _go_live(async_client_org_admin)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "number_unavailable"
    assert await messaging_repo.get_voice_agent(WS) is None  # nothing saved live without a number


@pytest.mark.asyncio
async def test_go_live_buys_in_area_code_when_on(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    r = await _go_live(async_client_org_admin, {"numberChoice": {"path": "new", "areaCode": "610"}})
    assert r.status_code == 200 and r.json()["config"]["number"] == NEW
    search, buy = twilio.await_args_list
    assert search.args == ("GET", "AvailablePhoneNumbers/US/Local.json")
    assert search.kwargs == {"AreaCode": "610", "VoiceEnabled": "true", "SmsEnabled": "true"}
    assert buy.args == ("POST", "IncomingPhoneNumbers.json")
    assert buy.kwargs == {"PhoneNumber": NEW, "TrunkSid": "TK123"}
    line = await messaging_repo._get(f"PHONE#{NEW}", "LINE")
    assert line["ownerId"] == WS and line["sid"] == "PN123"
    assert (await _go_live(async_client_org_admin, {"number": NEW})).status_code == 200
    assert twilio.await_count == 2  # already live: nothing bought again


@pytest.mark.asyncio
async def test_keep_path_buys_in_business_line_area_code(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch, "+14845550199")
    cfg = {"forwardFrom": "(484) 555-0100", "numberChoice": {"path": "keep", "carrier": "att", "mode": "all"}}
    assert await vo.provision_at_go_live(WS, cfg, "user_a") == "+14845550199"
    assert twilio.await_args_list[0].kwargs["AreaCode"] == "484"


@pytest.mark.asyncio
async def test_release_returns_number(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    number = await vo.provision_at_go_live(WS, {"numberChoice": {"path": "new", "areaCode": "610"}}, "user_a")
    await messaging_repo.put_voice_agent(WS, {"number": number}, "user_a")
    assert await vo.release_number("org_other") == ""  # never another workspace's line
    assert await vo.release_number(WS) == NEW
    assert twilio.await_args_list[-1].args == ("DELETE", "IncomingPhoneNumbers/PN123.json")
    assert await messaging_repo._get(f"PHONE#{NEW}", "LINE") is None
    assert await vo.release_number(WS) == ""
