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

    purchased = []

    async def call(method, path, **params):
        if path.startswith("AvailablePhoneNumbers"):
            return {"available_phone_numbers": [{"phone_number": number}]}
        if method == "GET" and path == "IncomingPhoneNumbers.json":
            return {
                "incoming_phone_numbers": [
                    x
                    for x in purchased
                    if x["friendly_name"] == params.get("FriendlyName")
                    and (not params.get("PhoneNumber") or x["phone_number"] == params["PhoneNumber"])
                ]
            }
        if method == "POST":
            row = {
                "sid": "PN123",
                "phone_number": params["PhoneNumber"],
                "friendly_name": params["FriendlyName"],
                "trunk_sid": params["TrunkSid"],
            }
            purchased.append(row)
            return row
        if method == "DELETE":
            purchased.clear()
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
    monkeypatch.setattr(vo.settings, "VOICE_INBOUND_TRUNK_ID", "ST_inbound" if on else "")
    monkeypatch.setattr(vo.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="lease-token"))
    monkeypatch.setattr(vo.container_repo, "release_owner_lifecycle_claim", AsyncMock())
    monkeypatch.setattr(vo, "_route_number", AsyncMock())
    monkeypatch.setattr(
        vo.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "metronome_voice_contract_id": "contract",
                "voice_plan_status": "active",
                "voice_plan_paid_periods": {
                    "invoice": {"start": "2000-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"}
                },
            }
        ),
    )


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
    lookup, search, buy = twilio.await_args_list
    assert lookup.args == ("GET", "IncomingPhoneNumbers.json")
    assert search.args == ("GET", "AvailablePhoneNumbers/US/Local.json")
    assert search.kwargs == {"AreaCode": "610", "VoiceEnabled": "true", "SmsEnabled": "true"}
    assert buy.args == ("POST", "IncomingPhoneNumbers.json")
    assert buy.kwargs == {"PhoneNumber": NEW, "TrunkSid": "TK123", "FriendlyName": lookup.kwargs["FriendlyName"]}
    line = await messaging_repo._get(f"PHONE#{NEW}", "LINE")
    assert line["ownerId"] == WS and line["sid"] == "PN123"
    assert (await _go_live(async_client_org_admin, {"number": NEW})).status_code == 200
    assert twilio.await_count == 3  # already live: nothing bought again


@pytest.mark.asyncio
async def test_keep_path_buys_in_business_line_area_code(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch, "+14845550199")
    cfg = {"forwardFrom": "(484) 555-0100", "numberChoice": {"path": "keep", "carrier": "att", "mode": "all"}}
    assert await vo.provision_at_go_live(WS, cfg, "user_a") == "+14845550199"
    assert twilio.await_args_list[1].kwargs["AreaCode"] == "484"


async def test_retry_after_config_save_failure_recovers_same_provider_number(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    first = await vo.provision_at_go_live(WS, {}, "user_a")
    # The HTTP config save failed after purchasing/routing; retry still has no config.number.
    assert await vo.provision_at_go_live(WS, {}, "user_b") == first
    assert sum(c.args[0] == "POST" for c in twilio.await_args_list) == 1
    line = await messaging_repo._get(f"PHONE#{first}", "LINE")
    assert line["ownerUserId"] == "user_a"


async def test_line_write_failure_alerts_and_retry_completes_without_second_purchase(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    metric = MagicMock()
    monkeypatch.setattr(vo, "put_metric", metric)
    real = vo.run_in_thread
    calls = {"line": 0}

    async def flaky(fn, *a, **kw):
        if (kw.get("Item") or {}).get("sk") == "LINE":
            calls["line"] += 1
            if calls["line"] == 1:
                raise RuntimeError("ddb down")
        return await real(fn, *a, **kw)

    monkeypatch.setattr(vo, "run_in_thread", flaky)
    with pytest.raises(RuntimeError):
        await vo.provision_at_go_live(WS, {}, "user_a")
    metric.assert_called_once_with("voice.number.orphaned")
    assert await messaging_repo._get(f"PHONE#{NEW}", "LINE") is None
    assert await vo.provision_at_go_live(WS, {}, "user_a") == NEW
    assert sum(c.args[0] == "POST" for c in twilio.await_args_list) == 1
    assert (await messaging_repo._get(f"PHONE#{NEW}", "LINE"))["ownerId"] == WS


async def test_route_failure_compensates_new_purchase(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    monkeypatch.setattr(vo, "_route_number", AsyncMock(side_effect=RuntimeError("LiveKit down")))
    with pytest.raises(vo.NoNumber, match="route_failed"):
        await vo.provision_at_go_live(WS, {}, "user_a")
    assert twilio.await_args_list[-1].args == ("DELETE", "IncomingPhoneNumbers/PN123.json")
    assert await messaging_repo._get(f"PHONE#{NEW}", "LINE") is None
    assert await messaging_repo._get(messaging_repo.owner_pk(WS), "VOICENUMBER") is None


async def test_busy_workspace_cannot_buy_concurrent_second_number(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    monkeypatch.setattr(vo.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value=None))
    with pytest.raises(vo.NoNumber, match="provisioning_in_progress"):
        await vo.provision_at_go_live(WS, {}, "user_a")
    twilio.assert_not_awaited()


async def test_provider_result_for_another_owner_is_never_routed_or_claimed(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = AsyncMock(
        return_value={
            "incoming_phone_numbers": [
                {"friendly_name": "another-owner", "sid": "PNother", "phone_number": NEW, "trunk_sid": "TK123"}
            ]
        }
    )
    monkeypatch.setattr(vo, "_twilio", twilio)
    with pytest.raises(vo.NoNumber, match="provider_ownership_mismatch"):
        await vo.provision_at_go_live(WS, {}, "user_a")
    vo._route_number.assert_not_awaited()
    assert await messaging_repo._get(f"PHONE#{NEW}", "LINE") is None


async def test_ambiguous_purchase_timeout_recovers_exact_number_without_buying_again(repos, monkeypatch):  # noqa: F811
    import httpx

    _buying(monkeypatch, True)
    provider = _fake_twilio(monkeypatch)

    async def uncertain(method, path, **params):
        result = await provider(method, path, **params)
        if method == "POST":
            raise httpx.ReadTimeout("purchase succeeded but response was lost")
        return result

    monkeypatch.setattr(vo, "_twilio", uncertain)
    with pytest.raises(vo.NoNumber, match="buy_failed"):
        await vo.provision_at_go_live(WS, {}, "user_a")
    assert await vo.provision_at_go_live(WS, {}, "user_a") == NEW
    assert sum(c.args[0] == "POST" for c in provider.await_args_list) == 1


async def test_confirmed_unavailable_choice_can_be_replaced_on_next_go_live(repos, monkeypatch):  # noqa: F811
    import httpx

    _buying(monkeypatch, True)
    provider = _fake_twilio(monkeypatch)

    async def collision(method, path, **params):
        if method == "POST":
            response = httpx.Response(
                400, json={"code": 21422}, request=httpx.Request("POST", "https://api.twilio.com")
            )
            raise httpx.HTTPStatusError("unavailable", request=response.request, response=response)
        return await provider(method, path, **params)

    monkeypatch.setattr(vo, "_twilio", collision)
    with pytest.raises(vo.NoNumber, match="none_in_area_code"):
        await vo.provision_at_go_live(WS, {}, "user_a")
    assert await messaging_repo._get(messaging_repo.owner_pk(WS), "VOICENUMBER") is None
    other = "+16105550999"
    new_provider = _fake_twilio(monkeypatch, other)
    assert await vo.provision_at_go_live(WS, {}, "user_a") == other
    assert sum(c.args[0] == "POST" for c in new_provider.await_args_list) == 1


@pytest.mark.parametrize(
    "account",
    [
        None,
        {},
        {"subscription_status": "active", "plan": "standard"},
        {
            "metronome_voice_contract_id": "contract",
            "voice_plan_paid_periods": {"i": {"start": "2000-01-01T00:00:00Z", "end": "2000-02-01T00:00:00Z"}},
        },
        {
            "metronome_voice_contract_id": "contract",
            "voice_plan_unpaid_invoice_ids": ["unpaid"],
            "voice_plan_paid_periods": {"i": {"start": "2000-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"}},
        },
    ],
)
async def test_unpaid_or_expired_plan_cannot_purchase_number(repos, monkeypatch, account):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    monkeypatch.setattr(vo.billing_repo, "get_by_owner_id", AsyncMock(return_value=account))
    with pytest.raises(vo.NoNumber, match="voice_payment_required"):
        await vo.provision_at_go_live(WS, {}, "user_a")
    twilio.assert_not_awaited()


async def test_existing_founder_line_and_idempotent_retrieval_do_not_require_new_payment(repos, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    messaging_repo._table().put_item(Item={"pk": f"PHONE#{LINE}", "sk": "LINE", "ownerId": WS})
    monkeypatch.setattr(vo.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    assert await vo.provision_at_go_live(WS, {"number": LINE}, "user_a") == LINE
    twilio.assert_not_awaited()


async def test_go_live_returns_semantic_payment_error_without_buy(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    _buying(monkeypatch, True)
    twilio = _fake_twilio(monkeypatch)
    monkeypatch.setattr(vo.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    result = await _go_live(async_client_org_admin)
    assert result.status_code == 402 and result.json()["detail"]["code"] == "voice_payment_required"
    twilio.assert_not_awaited()


@pytest.mark.parametrize("remove", [False, True])
async def test_real_sdk_route_patch_preserves_shared_number_and_ip_configuration(monkeypatch, remove):
    from types import SimpleNamespace
    from livekit.api.sip_service import SipService

    sip = SipService.__new__(SipService)  # execute the pinned real SDK wrapper; intercept only its HTTP seam
    sip._client = SimpleNamespace(request=AsyncMock())
    sip._admin_headers = lambda: {}
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=SimpleNamespace(sip=sip))
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(vo.livekit_huddles, "_api", lambda: context)
    monkeypatch.setattr(vo.settings, "VOICE_INBOUND_TRUNK_ID", "ST_owned")
    await vo._route_number(NEW, remove=remove)
    args = sip._client.request.await_args.args
    assert args[1] == "UpdateSIPInboundTrunk"
    request = args[2]
    assert request.sip_trunk_id == "ST_owned"
    assert list(request.update.numbers.remove if remove else request.update.numbers.add) == [NEW]
    assert not request.update.numbers.set
    assert not (request.update.numbers.add if remove else request.update.numbers.remove)
    assert not request.update.allowed_addresses.ListFields()


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
