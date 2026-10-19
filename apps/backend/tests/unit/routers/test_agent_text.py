from unittest.mock import AsyncMock

import pytest

from routers import agent_text

OWNER, KEY = "org_1", "team@acme.thelightbulb.company/rk1"
LINE = {
    "address": "+15551230000",
    "agent_id": "agent_front",
    "owner_id": OWNER,
    "kind": "text",
    "human_threads": {},
    "disclosed": {"+15559870000": "2026-10-17T00:00:00+00:00"},
}
NEW_CUSTOMER_LINE = {**LINE, "disclosed": {}}
PROVIDER = {"space_id": "sp1", "line_user_id": "lu1", "sender_user_id": "su1", "message_id": "m0"}
ROW = {"owner_id": OWNER, "channel": "text", "from_addr": "+15559870000", "received_key": "rk1", "provider": PROVIDER}


@pytest.fixture
def m(monkeypatch):
    mocks = {
        "record_event_or_skip": AsyncMock(return_value=agent_text.WebhookDedupResult.RECORDED),
        "get_message": AsyncMock(return_value=dict(ROW)),
        "text_line_for_owner": AsyncMock(return_value=LINE),
        "set_disclosed": AsyncMock(),
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
        idempotency_key=f"text-reply:rk1:agent:{hashlib.sha256(b'hi').hexdigest()[:16]}",
    )


async def test_reply_prefers_the_lines_photon_user_over_the_rows(m):
    m["text_line_for_owner"].return_value = dict(LINE, photon_user_id="line_usr")
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    assert m["send_text"].await_args.kwargs["sender_id"] == "line_usr"


async def test_row_without_provider_ids_is_bad_reply_key(m):
    m["get_message"].return_value = {k: v for k, v in ROW.items() if k != "provider"}
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "bad_reply_key"
    m["send_text"].assert_not_awaited()


async def test_member_phone_gets_no_disclosure(m):
    m["text_line_for_owner"].return_value = dict(NEW_CUSTOMER_LINE, member_phones={"+15559870000": "u1"})
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    assert m["send_text"].await_args.kwargs["text"] == "hi"
    m["set_disclosed"].assert_not_awaited()


async def test_first_reply_carries_the_disclosure_once(m):
    m["text_line_for_owner"].return_value = NEW_CUSTOMER_LINE
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    assert m["send_text"].await_args.kwargs["text"] == "(Automated assistant for Acme) hi"
    assert m["put_sent"].await_args.kwargs["text"] == "(Automated assistant for Acme) hi"


async def test_disclosure_is_recorded_on_the_row_after_the_first_reply(m):
    m["text_line_for_owner"].return_value = NEW_CUSTOMER_LINE
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    m["set_disclosed"].assert_awaited_once_with(LINE["address"], "+15559870000")
    # A customer already on the row hears no disclosure, and nothing is written.
    m["set_disclosed"].reset_mock()
    m["text_line_for_owner"].return_value = LINE
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi again")
    assert m["send_text"].await_args.kwargs["text"] == "hi again"
    m["set_disclosed"].assert_not_awaited()


async def test_a_retry_after_the_disclosure_was_recorded_keeps_the_same_idempotency_key(m):
    """The key is the reply as written, so a retry whose text has lost the prefix replays the first send."""
    m["text_line_for_owner"].return_value = NEW_CUSTOMER_LINE
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    first = m["send_text"].await_args.kwargs["idempotency_key"]
    m["text_line_for_owner"].return_value = LINE  # the disclosure is on the row now
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    assert m["send_text"].await_args.kwargs["idempotency_key"] == first
    assert m["send_text"].await_args.kwargs["text"] == "hi"


async def test_a_person_sending_the_agents_words_is_a_new_send_not_a_replay(m):
    """The key carries who sends: a human taking over with the same text must not replay the agent's send."""
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi")
    agent_key = m["send_text"].await_args.kwargs["idempotency_key"]
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi", by_human=True)
    human_key = m["send_text"].await_args.kwargs["idempotency_key"]
    assert agent_key != human_key
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi", by_human=True)
    assert m["send_text"].await_args.kwargs["idempotency_key"] == human_key  # a human's retry still replays


async def test_a_failed_send_records_no_disclosure(m):
    m["text_line_for_owner"].return_value = NEW_CUSTOMER_LINE
    m["send_text"].side_effect = agent_text.photon.PhotonError(500, "http_500")
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "send_failed"
    m["set_disclosed"].assert_not_awaited()


async def test_mail_row_key_is_refused(m):
    m["get_message"].return_value = {"owner_id": OWNER, "from_addr": "a@b.c"}
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "bad_reply_key"
    m["send_text"].assert_not_awaited()


async def test_other_owners_row_is_refused(m):
    m["get_message"].return_value = dict(ROW, owner_id="org_2")
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "bad_reply_key"
    m["send_text"].assert_not_awaited()


async def test_another_agent_cannot_send_as_the_front_desk(m):
    """Every agent holds the hook token; the send names its agent and only the line's agent passes."""
    status = await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi", agent_id="agent_other")
    assert status == "not_front_desk"
    m["send_text"].assert_not_awaited()
    status = await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi", agent_id=LINE["agent_id"])
    assert status == "sent"


async def test_a_retried_send_is_recorded_once(m):
    """A retry is Photon's replay of the first send (same message id): one sent row, not two."""
    m["record_event_or_skip"].return_value = agent_text.WebhookDedupResult.ALREADY_SEEN
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi") == "sent"
    m["send_text"].assert_awaited_once()
    m["put_sent"].assert_not_awaited()
    m["record_event_or_skip"].assert_awaited_once_with("photon-sent:h1", source="agent_text")


async def test_human_reply_marks_the_thread_human(m):
    assert await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi", by_human=True) == "sent"
    m["set_thread_mode"].assert_awaited_once_with(LINE["address"], "+15559870000", "human")


def test_hook_send_requires_the_token(client):
    r = client.post(f"/api/v1/agent-text/u/{OWNER}/send", json={"agent_id": "a", "reply_key": KEY, "text": "hi"})
    assert r.status_code == 401


def test_clerk_line_404s_without_a_line(client, monkeypatch):
    monkeypatch.setattr(agent_text, "resolve_owner_id", lambda auth: OWNER)
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_text.text_lines, "complete_pending", AsyncMock(return_value="absent"))
    r = client.get("/api/v1/agent-text/line")
    assert r.status_code == 404
    assert r.json() == {"status": "absent"}


def test_line_read_completes_pending(client, monkeypatch):
    monkeypatch.setattr(agent_text, "resolve_owner_id", lambda auth: OWNER)
    reads = AsyncMock(side_effect=[None, LINE])
    monkeypatch.setattr(agent_text, "text_line_for_owner", reads)
    complete = AsyncMock(return_value="provisioned")
    monkeypatch.setattr(agent_text.text_lines, "complete_pending", complete)
    r = client.get("/api/v1/agent-text/line")
    complete.assert_awaited_once_with(OWNER)
    assert r.status_code == 200 and r.json()["number"] == LINE["address"]


def test_line_read_settles_a_pending_row_left_beside_a_line(client, monkeypatch):
    """A surplus line whose release is not done keeps its pending row; a panel open retries it."""
    monkeypatch.setattr(agent_text, "resolve_owner_id", lambda auth: OWNER)
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=LINE))
    monkeypatch.setattr(agent_text, "pending_line_for_owner", AsyncMock(return_value={"kind": "text_pending"}))
    complete = AsyncMock(return_value="provisioned")
    monkeypatch.setattr(agent_text.text_lines, "complete_pending", complete)
    r = client.get("/api/v1/agent-text/line")
    complete.assert_awaited_once_with(OWNER)
    assert r.status_code == 200 and r.json()["number"] == LINE["address"]


def test_line_read_still_pending_is_404_for_the_panel(client, monkeypatch):
    # useTextLine maps 404 to null; a 202 body without `number` would read as a line.
    monkeypatch.setattr(agent_text, "resolve_owner_id", lambda auth: OWNER)
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_text.text_lines, "complete_pending", AsyncMock(return_value="pending"))
    r = client.get("/api/v1/agent-text/line")
    assert r.status_code == 404 and r.json() == {"status": "pending"}


@pytest.mark.parametrize(
    "error", [agent_text.photon.PhotonError(503, "http_503"), agent_text.httpx.ConnectError("down")]
)
def test_line_read_photon_error_is_404(client, monkeypatch, error):
    monkeypatch.setattr(agent_text, "resolve_owner_id", lambda auth: OWNER)
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_text.text_lines, "complete_pending", AsyncMock(side_effect=error))
    r = client.get("/api/v1/agent-text/line")
    assert r.status_code == 404 and r.json() == {"status": "pending"}  # the pending row is still there


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
    m["text_line_for_owner"].return_value = NEW_CUSTOMER_LINE
    await agent_text.send_reply(owner_id=OWNER, reply_key=KEY, text="hi", by_human=True)
    assert m["send_text"].await_args.kwargs["text"] == "hi"
    m["set_disclosed"].assert_not_awaited()


def test_business_phone_rejects_non_e164(client, monkeypatch):
    monkeypatch.setattr(agent_text, "_require_hook_token", AsyncMock())
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=LINE))
    save = AsyncMock()
    monkeypatch.setattr(agent_text, "set_business_phone", save)
    for bad in ["555-1234", "+0123456789", "+1555", "call me"]:
        r = client.post(
            f"/api/v1/agent-text/u/{OWNER}/business-phone", json={"agent_id": LINE["agent_id"], "phone": bad}
        )
        assert r.status_code == 400 and r.json() == {"status": "bad_phone"}
    save.assert_not_awaited()
    good = {"agent_id": LINE["agent_id"], "phone": "+15551234567"}
    r = client.post(f"/api/v1/agent-text/u/{OWNER}/business-phone", json=good)
    assert r.status_code == 200
    save.assert_awaited_once_with(LINE["address"], "+15551234567")


def test_business_phone_refuses_another_agent(client, monkeypatch):
    monkeypatch.setattr(agent_text, "_require_hook_token", AsyncMock())
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=LINE))
    save = AsyncMock()
    monkeypatch.setattr(agent_text, "set_business_phone", save)
    body = {"agent_id": "agent_other", "phone": "+15551234567"}
    r = client.post(f"/api/v1/agent-text/u/{OWNER}/business-phone", json=body)
    assert r.status_code == 403 and r.json() == {"detail": "not_front_desk"}
    save.assert_not_awaited()


def _me(monkeypatch, line, set_phone=None):
    monkeypatch.setattr(agent_text, "resolve_owner_id", lambda auth: OWNER)
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=line))
    set_phone = set_phone or AsyncMock()
    monkeypatch.setattr("core.repositories.agent_email_repo.set_member_phone", set_phone)
    return set_phone


def _as(client, user_id):
    from core.auth import AuthContext

    client.app.dependency_overrides[agent_text.get_current_user] = lambda: AuthContext(user_id=user_id, org_id=OWNER)


def test_phone_code_is_issued_and_stored(client, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(agent_text.time, "time", lambda: now)
    monkeypatch.setattr(agent_text.secrets, "randbelow", lambda n: 4821)
    codes = {
        "111111": {"user_id": "x", "exp": now - 1},  # expired: pruned
        "222222": {"user_id": "y", "exp": now + 60},  # another member's live code: kept
        "333333": {"user_id": "me", "exp": now + 60},  # my earlier live code: replaced (one live code per member)
    }
    _me(monkeypatch, {**LINE, "phone_codes": codes})
    put = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_text, "put_phone_code", put)
    _as(client, "me")
    r = client.post("/api/v1/agent-text/me/phone/code")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "004821" and body["number"] == LINE["address"]
    assert agent_text.datetime.fromisoformat(body["expiresAt"]).timestamp() == now + 15 * 60
    put.assert_awaited_once_with(LINE["address"], "004821", "me", now + 15 * 60, prune=["111111", "333333"])


def test_phone_code_redraws_once_on_a_live_collision(client, monkeypatch):
    draws = iter(range(1, 10))
    monkeypatch.setattr(agent_text.secrets, "randbelow", lambda n: next(draws))
    _me(monkeypatch, LINE)
    put = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(agent_text, "put_phone_code", put)
    _as(client, "me")
    assert client.post("/api/v1/agent-text/me/phone/code").json()["code"] == "000002"
    assert [c.args[1] for c in put.await_args_list] == ["000001", "000002"]
    put.reset_mock(side_effect=True)
    put.side_effect = [False, False]
    assert client.post("/api/v1/agent-text/me/phone/code").status_code == 503


def test_phone_code_without_a_line_is_404(client, monkeypatch):
    _me(monkeypatch, None)
    assert client.post("/api/v1/agent-text/me/phone/code").status_code == 404


def test_me_phone_delete_removes_the_callers_phone(client, monkeypatch):
    set_phone = _me(monkeypatch, {**LINE, "member_phones": {"+15550001111": "other", "+15551234567": "me"}})
    _as(client, "me")
    r = client.delete("/api/v1/agent-text/me/phone")
    assert r.json() == {"status": "ok"}
    set_phone.assert_awaited_once_with(LINE["address"], "+15551234567", None)


def test_line_read_has_my_phone(client, monkeypatch):
    _me(monkeypatch, {**LINE, "member_phones": {"+15551234567": "me"}})
    monkeypatch.setattr(agent_text, "pending_line_for_owner", AsyncMock(return_value=None))
    _as(client, "me")
    assert client.get("/api/v1/agent-text/line").json()["myPhone"] == "+15551234567"
    _as(client, "you")
    assert client.get("/api/v1/agent-text/line").json()["myPhone"] is None


@pytest.mark.parametrize(("again", "status"), [("pending", "pending"), ("absent", "absent")])
def test_line_read_after_a_failed_operation_rechecks_the_subscription(client, monkeypatch, again, status):
    monkeypatch.setattr(agent_text, "resolve_owner_id", lambda auth: OWNER)
    monkeypatch.setattr(agent_text, "text_line_for_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_text.text_lines, "complete_pending", AsyncMock(return_value="failed"))
    monkeypatch.setattr(
        agent_text.billing_repo, "get_by_owner_id", AsyncMock(return_value={"stripe_subscription_id": "sub_1"})
    )
    sync = AsyncMock(return_value=again)
    monkeypatch.setattr(agent_text.text_lines, "sync_front_desk", sync)
    r = client.get("/api/v1/agent-text/line")
    assert r.status_code == 404 and r.json() == {"status": status}
    sync.assert_awaited_once_with(OWNER, subscription_id="sub_1")
