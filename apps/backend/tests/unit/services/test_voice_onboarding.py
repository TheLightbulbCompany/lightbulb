"""Onboarding: the website draft (only what the site states, shaped for the Agent page) and the number."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.repositories import messaging_repo
from core.services import link_preview, voice_onboarding as vo
from tests.unit.services.conftest import repos  # noqa: F401

WS = "org_test_456"
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
    monkeypatch.setattr(vo, "read_site", AsyncMock(return_value="t"))
    monkeypatch.setattr(vo, "_bedrock", lambda: _bedrock("no json here"))
    d = await vo.draft("nails.com", "", "")
    assert d["read"] is True and d["services"] == [] and d["hours"] == {}


@pytest.mark.asyncio
async def test_assign_number_gives_dev_its_one_number_once(repos, monkeypatch):  # noqa: F811
    monkeypatch.setattr(vo.settings, "ENVIRONMENT", "dev")
    assert await vo.assign_number(WS, "user_a") == vo.DEV_NUMBER
    assert await vo.assign_number(WS, "user_b") == vo.DEV_NUMBER  # idempotent, keeps the first owner user
    line = await messaging_repo._get(f"PHONE#{vo.DEV_NUMBER}", "LINE")
    assert line["ownerId"] == WS and line["ownerUserId"] == "user_a"
    with pytest.raises(vo.NoNumber, match="taken"):
        await vo.assign_number("org_other", "user_c")
    monkeypatch.setattr(vo.settings, "ENVIRONMENT", "prod")
    with pytest.raises(vo.NoNumber, match="not_available"):
        await vo.assign_number(WS, "user_a")


@pytest.mark.asyncio
async def test_number_route_says_why_there_is_none(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(vo.settings, "ENVIRONMENT", "dev")
    messaging_repo._table().put_item(Item={"pk": f"PHONE#{vo.DEV_NUMBER}", "sk": "LINE", "ownerId": "org_other"})
    r = await async_client_org_admin.post("/api/v1/voice/number")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "taken"
