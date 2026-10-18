from unittest.mock import AsyncMock

import pytest

from routers import agent_text

OWNER, KEY = "org_1", "team@acme.thelightbulb.company/rk1"
LINE = {"address": "+15551230000", "agent_id": "agent_front", "owner_id": OWNER, "kind": "text", "human_threads": {}}
PROVIDER = {"space_id": "sp1", "line_user_id": "lu1", "sender_user_id": "su1", "message_id": "m0"}
ROW = {"owner_id": OWNER, "channel": "text", "from_addr": "+15559870000", "received_key": "rk1", "provider": PROVIDER}


@pytest.fixture
def m(monkeypatch):
    mocks = {
        "get_message": AsyncMock(return_value=dict(ROW)),
        "text_line_for_owner": AsyncMock(return_value=LINE),
        "has_sent_to": AsyncMock(return_value=True),
        "send_text": AsyncMock(return_value={"id": "h1"}),
        "put_sent": AsyncMock(),
        "mail_changed": AsyncMock(),
        "set_thread_mode": AsyncMock(),
        "business_name": AsyncMock(return_value="Acme"),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(agent_text.photon if name == "send_text" else agent_text, name, mock)
    monkeypatch.setattr(agent_text.photon, "enabled", lambda: True)
    return mocks


async def test_reply_sends_from_the_line_to_the_customer_and_records_it(m):
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "sent"
    m["get_message"].assert_awaited_once_with("team@acme.thelightbulb.company", "rk1")
    m["put_sent"].assert_awaited_once_with(
        address="team@acme.thelightbulb.company",
        message_id="h1",
        owner_id=OWNER,
        agent_id="agent_front",
        to="+15559870000",
        subject="",
        text="hi",
        channel="text",
    )
    m["mail_changed"].assert_awaited_once_with(OWNER)
    m["set_thread_mode"].assert_not_awaited()


async def test_reply_sends_to_the_row_space_as_the_line_user(m):
    import hashlib

    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    m["send_text"].assert_awaited_once_with(
        sender_id="lu1",
        space_id="sp1",
        text="hi",
        idempotency_key=f"text-reply:rk1:{hashlib.sha256(b'hi').hexdigest()[:16]}",
    )


async def test_row_without_provider_ids_is_bad_reply_key(m):
    m["get_message"].return_value = {k: v for k, v in ROW.items() if k != "provider"}
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "bad_reply_key"
    m["send_text"].assert_not_awaited()


async def test_member_phone_gets_no_disclosure(m):
    m["has_sent_to"].return_value = False
    m["text_line_for_owner"].return_value = dict(LINE, member_phones={"+15559870000": "u1"})
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    assert m["send_text"].await_args.kwargs["text"] == "hi"


async def test_first_reply_carries_the_disclosure_once(m):
    m["has_sent_to"].return_value = False
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    m["has_sent_to"].assert_awaited_once_with(
        "team@acme.thelightbulb.company", "+15559870000", text_prefix=agent_text.DISCLOSURE_PREFIX
    )
    assert m["send_text"].await_args.kwargs["text"] == "(Automated assistant for Acme) hi"
    assert m["put_sent"].await_args.kwargs["text"] == "(Automated assistant for Acme) hi"


async def test_mail_row_key_is_refused(m):
    m["get_message"].return_value = {"owner_id": OWNER, "from_addr": "a@b.c"}
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "bad_reply_key"
    m["send_text"].assert_not_awaited()


async def test_other_owners_row_is_refused(m):
    m["get_message"].return_value = dict(ROW, owner_id="org_2")
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "bad_reply_key"
    m["send_text"].assert_not_awaited()


async def test_human_reply_marks_the_thread_human(m):
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi", by_human=True) == "sent"
    m["set_thread_mode"].assert_awaited_once_with(LINE["address"], "+15559870000", "human")


def test_hook_send_requires_the_token(client):
    r = client.post(f"/api/v1/agent-text/u/{OWNER}/send", json={"agent_id": "a", "reply_key": KEY, "text": "hi"})
    assert r.status_code == 401


def test_clerk_line_404s_without_a_line(client, monkeypatch):
    monkeypatch.setattr(agent_text, "resolve_owner_id", lambda auth: OWNER)
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=None))
    r = client.get("/api/v1/agent-text/line")
    assert r.status_code == 404
    assert r.json() == {"detail": "no_line"}


def test_clerk_thread_mode_sets_agent(client, monkeypatch):
    set_mode = AsyncMock()
    monkeypatch.setattr(agent_text, "resolve_owner_id", lambda auth: OWNER)
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=LINE))
    monkeypatch.setattr(agent_text, "get_message", AsyncMock(return_value=dict(ROW)))
    monkeypatch.setattr(agent_text, "set_thread_mode", set_mode)
    r = client.post("/api/v1/agent-text/thread", json={"key": KEY, "mode": "agent"})
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    set_mode.assert_awaited_once_with(LINE["address"], "+15559870000", "agent")


async def test_disabled_sends_nothing(m, monkeypatch):
    monkeypatch.setattr(agent_text.photon, "enabled", lambda: False)
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "disabled"
    m["send_text"].assert_not_awaited()


async def test_agent_reply_on_human_thread_is_refused(m):
    m["text_line_for_owner"].return_value = dict(LINE, human_threads={"+15559870000": "t"})
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "human_thread"
    m["send_text"].assert_not_awaited()


async def test_human_reply_on_human_thread_still_sends(m):
    m["text_line_for_owner"].return_value = dict(LINE, human_threads={"+15559870000": "t"})
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi", by_human=True) == "sent"


async def test_human_reply_never_carries_the_disclosure(m):
    m["has_sent_to"].return_value = False
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi", by_human=True)
    assert m["send_text"].await_args.kwargs["text"] == "hi"
    m["has_sent_to"].assert_not_awaited()


def test_business_phone_rejects_non_e164(client, monkeypatch):
    monkeypatch.setattr(agent_text, "_require_hook_token", AsyncMock())
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=LINE))
    save = AsyncMock()
    monkeypatch.setattr(agent_text, "set_business_phone", save)
    for bad in ["555-1234", "+0123456789", "+1555", "call me"]:
        r = client.post(f"/api/v1/agent-text/u/{OWNER}/business-phone", json={"phone": bad})
        assert r.status_code == 400 and r.json() == {"status": "bad_phone"}
    save.assert_not_awaited()
    r = client.post(f"/api/v1/agent-text/u/{OWNER}/business-phone", json={"phone": "+15551234567"})
    assert r.status_code == 200
    save.assert_awaited_once_with(LINE["address"], "+15551234567")
