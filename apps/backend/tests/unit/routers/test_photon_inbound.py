"""Unit tests for POST /api/v1/webhooks/photon-inbound (a customer's iMessage to a business line)."""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services import text_inbound
from core.services.agent_mail_inbound import stranger_key
from core.services.photon import PhotonError
from core.services.webhook_dedup import WebhookDedupResult
from routers import photon_inbound as ph

SECRET_RAW = b"photon-test-secret-bytes"
SECRET = "whsec_" + base64.b64encode(SECRET_RAW).decode()
OWNER_ID, AGENT_ID = "owner_1", "agent_1"
LINE, TEAM, CUSTOMER = "+14155550100", "agents@acme.example.com", "+14155550123"
LINE_USER, CUSTOMER_USER, SPACE = "pho_usr_line", "pho_usr_cust", "pho_spc_1"
LINE_ROW = {
    "address": LINE,
    "owner_id": OWNER_ID,
    "agent_id": AGENT_ID,
    "kind": "text",
    "human_threads": {},
    "photon_user_id": LINE_USER,
}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ph.router, prefix="/api/v1/webhooks")
    return TestClient(app)


@pytest.fixture
def seams(monkeypatch):
    m = {
        "enabled": lambda: True,
        "record_event_or_skip": AsyncMock(return_value=WebhookDedupResult.RECORDED),
        "delete_event": AsyncMock(),
        "get_by_address": AsyncMock(return_value=dict(LINE_ROW)),
        "team_address": AsyncMock(return_value=TEAM),
        "find_by_message_id": AsyncMock(return_value=None),
        "find_sent_by_message_id": AsyncMock(return_value=None),
        "mark_sent_failed": AsyncMock(),
        "put_message": AsyncMock(return_value={"received_key": "2026#m1"}),
        "put_sent": AsyncMock(),
        "mail_changed": AsyncMock(),
        "trial_block_reason": AsyncMock(return_value=None),
        "try_consume_stranger_wake": AsyncMock(return_value=True),
        "release_stranger_wake": AsyncMock(),
        "send_text": AsyncMock(return_value={"id": "msg_out"}),
        "send_typing": AsyncMock(),
        "mark_read": AsyncMock(),
        "deliver_to_agent": AsyncMock(return_value="delivered"),
        "business_name": AsyncMock(return_value="Acme"),
    }
    for name, value in m.items():
        monkeypatch.setattr(ph, name, value)
    monkeypatch.setattr(ph.settings, "PHOTON_WEBHOOK_SECRET", SECRET)
    return m


def _sign(body: bytes, *, secret=SECRET, ts=None, wid="pho_wh_1") -> dict:
    ts = str(int(time.time()) if ts is None else ts)
    key = base64.b64decode(secret.removeprefix("whsec_"))
    mac = hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {
        "webhook-id": wid,
        "webhook-timestamp": ts,
        "webhook-signature": "v1,AAAA v1," + base64.b64encode(mac).decode(),
        "content-type": "application/json",
    }


def _event(**overrides) -> dict:
    """A message.received event in the fixture's shape; keyword overrides replace object fields."""
    obj = {
        "id": "m1",
        "platform": "imessage",
        "status": "received",
        "space": {"id": SPACE, "participantIds": [LINE_USER, CUSTOMER_USER]},
        "sentBy": {"id": CUSTOMER_USER, "handle": CUSTOMER},
        "receivedBy": {"id": LINE_USER, "handle": LINE, "details": {"resourceId": "res_1"}},
        "content": {"type": "text", "text": "do you open at 9?"},
        "timestamps": {"receivedAt": "2026-10-17T10:00:00Z"},
    }
    obj.update(overrides)
    return {
        "id": "pho_evt_1",
        "type": "message.received",
        "apiVersion": "2026-07-01",
        "source": "imessage.photon",
        "timestamp": "2026-10-17T10:00:00Z",
        "data": {"projectId": "pho_prj_1", "object": obj},
    }


def _post(client, event=None, *, headers=None, raw: bytes | None = None):
    raw = raw if raw is not None else json.dumps(event or _event()).encode()
    return client.post("/api/v1/webhooks/photon-inbound", content=raw, headers=headers or _sign(raw))


def test_disabled_without_settings(client, seams, monkeypatch):
    monkeypatch.setattr(ph, "enabled", lambda: False)
    assert _post(client).json() == {"status": "disabled"}


def test_bad_signature_is_401(client, seams):
    raw = json.dumps(_event()).encode()
    r = _post(client, raw=raw, headers=_sign(raw, secret="whsec_" + base64.b64encode(b"other").decode()))
    assert r.status_code == 401 and r.json() == {"status": "bad_signature"}
    seams["record_event_or_skip"].assert_not_called()


def test_body_tampered_after_signing_is_401(client, seams):
    raw = json.dumps(_event()).encode()
    assert _post(client, raw=raw + b" ", headers=_sign(raw)).status_code == 401


def test_stale_timestamp_is_401(client, seams):
    raw = json.dumps(_event()).encode()
    r = _post(client, raw=raw, headers=_sign(raw, ts=int(time.time()) - 301))
    assert r.status_code == 401 and r.json() == {"status": "stale_timestamp"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["data"]["object"].update(content={"type": "read"}),
        lambda e: e["data"]["object"].update(content={"type": "typing"}),
        lambda e: e["data"]["object"].update(content={"type": "group"}),
        lambda e: e.update(type="message.sent"),
        lambda e: e["data"]["object"].update(platform="sms"),
        lambda e: e["data"]["object"].update(status="sent"),
    ],
)
def test_non_message_events_are_ignored(client, seams, mutate):
    event = _event()
    mutate(event)
    assert _post(client, event).json() == {"status": "ignored_event"}
    seams["record_event_or_skip"].assert_not_called()


def test_redelivered_message_id_is_duplicate(client, seams):
    seen = set()

    async def record(event_id, *, source):
        if event_id in seen:
            return WebhookDedupResult.ALREADY_SEEN
        seen.add(event_id)
        return WebhookDedupResult.RECORDED

    seams["record_event_or_skip"].side_effect = record
    assert _post(client).json() == {"status": "delivered"}
    # Photon redelivers the same message under a fresh webhook id.
    raw = json.dumps(_event()).encode()
    assert _post(client, raw=raw, headers=_sign(raw, wid="pho_wh_2")).json() == {"status": "duplicate"}
    seams["put_message"].assert_awaited_once()
    seams["deliver_to_agent"].assert_awaited_once()


def test_replayed_webhook_id_is_duplicate(client, seams):
    seams["record_event_or_skip"].return_value = WebhookDedupResult.ALREADY_SEEN
    assert _post(client).json() == {"status": "duplicate"}
    seams["record_event_or_skip"].assert_awaited_once_with("photon:m1", source="photon_inbound")


@pytest.mark.parametrize("row", [None, {"address": LINE, "owner_id": OWNER_ID, "agent_id": AGENT_ID}])
def test_unknown_line_is_dropped(client, seams, row):
    seams["get_by_address"].return_value = row
    assert _post(client).json() == {"status": "dropped_unknown_line"}
    seams["put_message"].assert_not_called()


def test_a_receiver_without_a_handle_is_an_unknown_line(client, seams):
    assert _post(client, _event(receivedBy={"id": LINE_USER})).json() == {"status": "dropped_unknown_line"}
    seams["get_by_address"].assert_not_called()


def test_stored_and_delivered_with_exact_kwargs(client, seams):
    assert _post(client).json() == {"status": "delivered"}
    seams["put_message"].assert_awaited_once_with(
        address=TEAM,
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        message_id="m1",
        from_addr=CUSTOMER,
        subject="",
        text="do you open at 9?",
        headers={},
        attachments=[],
        received_at="2026-10-17T10:00:00Z",
        channel="text",
        provider_ids={
            "space_id": SPACE,
            "sender_user_id": CUSTOMER_USER,
            "line_user_id": LINE_USER,
            "message_id": "m1",
        },
    )
    key = ph.reply_key(TEAM, "2026#m1")
    seams["deliver_to_agent"].assert_awaited_once_with(
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
        message=text_inbound.build_delivered_text(
            from_number=CUSTOMER, service="iMessage", text="do you open at 9?", reply_key=key, media_url=None
        ),
        idempotency_key="photon:m1",
        session_key=text_inbound.text_session_key(CUSTOMER),
        source="text",
        reply_ref={"provider": "photon", "address": TEAM, "senderId": LINE_USER, "spaceId": SPACE, "to": CUSTOMER},
    )
    assert text_inbound.text_session_key(CUSTOMER).startswith("lightbulb-text:")
    assert f"Reply-Key: {key}" in seams["deliver_to_agent"].await_args.kwargs["message"]
    seams["send_typing"].assert_awaited_once_with(sender_id=LINE_USER, space_id=SPACE)
    seams["mark_read"].assert_awaited_once_with("m1")
    wake = seams["try_consume_stranger_wake"].await_args
    assert wake.args == (OWNER_ID,)
    assert wake.kwargs["attr_prefix"] == "text_wake"
    assert wake.kwargs["sender_key"] == stranger_key(CUSTOMER)
    assert wake.kwargs["owner_cap"] == text_inbound.TEXT_WAKES_PER_OWNER_PER_DAY
    assert wake.kwargs["sender_cap"] == text_inbound.TEXT_WAKES_PER_SENDER_PER_DAY


def test_attachment_is_stored_as_placeholder(client, seams):
    # A caption on the attachment must not replace the placeholder.
    event = _event(content={"type": "attachment", "attachmentId": "att_9", "text": "look"})
    assert _post(client, event).json() == {"status": "delivered"}
    assert seams["put_message"].await_args.kwargs["text"] == "[Attachment: att_9]"


def test_human_thread_is_stored_not_woken(client, seams):
    seams["get_by_address"].return_value = {**LINE_ROW, "human_threads": {CUSTOMER: "2026-10-17T00:00:00Z"}}
    assert _post(client).json() == {"status": "stored_human_thread"}
    seams["put_message"].assert_awaited_once()
    seams["deliver_to_agent"].assert_not_called()


def test_capped_owner_gets_paused_autoreply(client, seams):
    seams["trial_block_reason"].return_value = "daily_cap"
    seams["get_by_address"].return_value = {**LINE_ROW, "business_phone": "+14155550999"}
    assert _post(client).json() == {"status": "capped_notified"}
    text = text_inbound.paused_autoreply("Acme", "+14155550999")
    seams["send_text"].assert_awaited_once_with(
        sender_id=LINE_USER, space_id=SPACE, text=text, idempotency_key="paused:m1"
    )
    kw = seams["put_sent"].await_args.kwargs
    assert kw["channel"] == "text" and kw["to"] == CUSTOMER and kw["message_id"] == "msg_out"
    seams["deliver_to_agent"].assert_not_called()


def test_capped_owner_is_told_once_per_day(client, seams):
    seams["trial_block_reason"].return_value = "daily_cap"
    seen = set()

    async def consume(owner_id, *, sender_key, attr_prefix, **_):
        if (attr_prefix, sender_key) in seen:
            return False
        seen.add((attr_prefix, sender_key))
        return True

    seams["try_consume_stranger_wake"].side_effect = consume
    assert _post(client).json() == {"status": "capped_notified"}
    assert _post(client, _event(id="m2")).json() == {"status": "stored_capped_quiet"}
    seams["send_text"].assert_awaited_once()
    assert seams["try_consume_stranger_wake"].await_args.kwargs["attr_prefix"] == "text_paused"


@pytest.mark.parametrize("exc", [PhotonError(500, "x"), httpx.ConnectError("x")])
def test_a_paused_reply_that_fails_gives_the_slot_back_and_503s(client, seams, exc):
    """Photon redelivers on 503, and the sender's one reply a day must not have been spent on the failure."""
    seams["trial_block_reason"].return_value = "daily_cap"
    seams["send_text"].side_effect = exc
    r = _post(client)
    assert r.status_code == 503 and r.json() == {"status": "paused_reply_failed"}
    taken = seams["try_consume_stranger_wake"].await_args.kwargs
    seams["release_stranger_wake"].assert_awaited_once_with(
        LINE_ROW["owner_id"], sender_key=taken["sender_key"], day=taken["day"], attr_prefix="text_paused"
    )
    seams["delete_event"].assert_awaited_once_with("photon:m1")


def test_empty_sender_is_dropped(client, seams):
    assert _post(client, _event(sentBy={})).json() == {"status": "dropped_no_sender"}
    seams["put_message"].assert_not_called()


def test_sender_without_handle_falls_back_to_user_id(client, seams):
    assert _post(client, _event(sentBy={"id": CUSTOMER_USER})).json() == {"status": "delivered"}
    assert seams["put_message"].await_args.kwargs["from_addr"] == f"photon:{CUSTOMER_USER}"


def test_addressed_to_names_the_line_when_received_by_is_absent(client, seams):
    """Both parties are optional in the spec, and either names our line."""
    event = _event(receivedBy=None, addressedTo={"id": LINE_USER, "handle": LINE})
    assert _post(client, event).json() == {"status": "delivered"}
    assert seams["get_by_address"].await_args_list[0].args == (LINE,)


def test_apple_email_sender_is_kept_verbatim(client, seams):
    event = _event(sentBy={"id": CUSTOMER_USER, "handle": "Jane@iCloud.com"})
    assert _post(client, event).json() == {"status": "delivered"}
    assert seams["put_message"].await_args.kwargs["from_addr"] == "Jane@iCloud.com"


def test_a_send_photon_later_fails_marks_the_sent_row_and_wakes_nobody(client, seams):
    """A 202 send is not delivered until Photon says so; its failure lands on the sent row for the panel."""
    seams["find_sent_by_message_id"].return_value = {
        "address": TEAM,
        "received_key": "2026#msg_out",
        "owner_id": OWNER_ID,
    }
    event = {**_event(id="msg_out", status="failed", failure={"code": "undeliverable"}), "type": "message.failed"}
    assert _post(client, event).json() == {"status": "send_failed_later"}
    seams["mark_sent_failed"].assert_awaited_once_with(TEAM, "2026#msg_out", "undeliverable")
    seams["mail_changed"].assert_awaited_once_with(OWNER_ID)
    seams["put_message"].assert_not_called()
    seams["deliver_to_agent"].assert_not_called()
    # Not a message we sent: nothing to mark.
    seams["find_sent_by_message_id"].return_value = None
    other = {**_event(id="m9", status="failed"), "type": "message.failed"}
    assert _post(client, other).json() == {"status": "ignored_event"}


def test_rate_limited_sender_is_stored_not_woken(client, seams):
    seams["try_consume_stranger_wake"].return_value = False
    assert _post(client).json() == {"status": "stored_not_woken_rate_limited"}
    seams["deliver_to_agent"].assert_not_called()


def test_delivery_error_rolls_back_dedup_and_503s(client, seams):
    seams["deliver_to_agent"].return_value = "error"
    assert _post(client).status_code == 503
    seams["delete_event"].assert_awaited_once_with("photon:m1")
    seams["deliver_to_agent"].return_value = "delivered"
    seams["deliver_to_agent"].side_effect = RuntimeError("boom")
    assert _post(client).status_code == 503
    assert seams["delete_event"].await_count == 2


def test_a_redelivery_the_index_has_not_caught_up_with_lands_on_the_same_row(client, seams):
    """find_by_message_id reads an eventually consistent index; the row's key is Photon's receipt time,
    preserved across replays, so a second put after a 503 is the same row and the same reply key."""
    seams["deliver_to_agent"].return_value = "error"
    assert _post(client).status_code == 503
    seams["deliver_to_agent"].return_value = "delivered"
    assert _post(client).status_code == 200  # find_by_message_id still answers None (the seam's default)
    stamps = [c.kwargs["received_at"] for c in seams["put_message"].await_args_list]
    assert stamps == ["2026-10-17T10:00:00Z", "2026-10-17T10:00:00Z"]


def test_unknown_body_shape_is_422_not_500(client, seams):
    raw = b'{"data": 5, "id": "x"}'
    assert _post(client, raw=raw, headers=_sign(raw)).status_code == 422
    raw = b"not json"
    assert _post(client, raw=raw, headers=_sign(raw)).status_code == 422


def test_delivered_text_matches_the_template():
    got = text_inbound.build_delivered_text(
        from_number=CUSTOMER, service="iMessage", text="hi", reply_key="k/1", media_url=None
    )
    assert got.startswith(f"You received a text message.\n\nFrom: {CUSTOMER}\nVia: iMessage\nReply-Key: k/1\n\n")
    assert got.endswith("---\nhi")


def test_typing_skipped_without_a_line_user(client, seams):
    seams["get_by_address"].return_value = {k: v for k, v in LINE_ROW.items() if k != "photon_user_id"}
    assert _post(client, _event(receivedBy={"handle": LINE})).json() == {"status": "delivered"}
    seams["send_typing"].assert_not_called()
    seams["mark_read"].assert_not_called()


MEMBER_ID = "user_member"


@pytest.fixture
def member(seams, monkeypatch):
    seams["get_by_address"].return_value = {**LINE_ROW, "member_phones": {CUSTOMER: MEMBER_ID}}
    post = AsyncMock()
    monkeypatch.setattr(ph.messaging, "create_human_message", post)
    return post


def test_member_text_is_posted_to_the_default_room_as_the_member(client, seams, member):
    import uuid

    from core.auth import AuthContext
    from core.services import messaging

    assert _post(client).json() == {"status": "member_routed"}
    seams["put_message"].assert_awaited_once()
    seams["mail_changed"].assert_awaited_once_with(OWNER_ID)
    member.assert_awaited_once_with(
        AuthContext(user_id=MEMBER_ID, org_id=OWNER_ID),
        messaging.default_room_id(OWNER_ID),
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb-text|m1"),
            text=f"do you open at 9?\n\n(sent by text from {CUSTOMER}; a reply here is texted back, so keep it short)",
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        via="text",
        text_reply_key=f"{TEAM}/2026#m1",  # where text_relay texts an agent's answer back
    )


def test_member_text_never_wakes_the_front_desk(client, seams, member):
    _post(client)
    seams["deliver_to_agent"].assert_not_called()
    # The member sees typing at once; "read" waits for an agent's run to start on the room message.
    seams["send_typing"].assert_awaited_once_with(sender_id=LINE_USER, space_id=SPACE)
    seams["mark_read"].assert_not_called()
    seams["try_consume_stranger_wake"].assert_not_called()
    seams["send_text"].assert_not_called()


def test_a_refused_member_post_falls_through_to_the_front_desk_and_keeps_the_link(client, seams, member, monkeypatch):
    # A 403 is a member missing from the main room OR from the workspace; the membership webhook unlinks a
    # member who left, so the handler keeps the link and a member merely taken out of the room stays linked.
    from core.services import messaging

    member.side_effect = messaging.ApiProblem("ROOM_FORBIDDEN", "not a room member")
    drop = AsyncMock()
    monkeypatch.setattr("core.repositories.agent_email_repo.set_member_phone", drop)
    r = _post(client)
    assert r.status_code == 200 and r.json() == {"status": "delivered"}
    drop.assert_not_called()
    seams["try_consume_stranger_wake"].assert_awaited_once()
    seams["deliver_to_agent"].assert_awaited_once()
    seams["delete_event"].assert_not_called()


def test_a_missing_main_room_keeps_a_live_members_link(client, seams, member, monkeypatch):
    from core.services import messaging

    member.side_effect = messaging.ApiProblem("ROOM_NOT_FOUND", "no main room")
    drop = AsyncMock()
    monkeypatch.setattr("core.repositories.agent_email_repo.set_member_phone", drop)
    assert _post(client).json() == {"status": "delivered"}  # the text is a customer's this once
    drop.assert_not_called()  # a 404 is the room's fault, not the member's


def test_member_post_transient_failure_is_503_and_retried(client, seams, member):
    member.side_effect = RuntimeError("ddb throttled")
    r = _post(client)
    assert r.status_code == 503
    seams["delete_event"].assert_awaited_once_with("photon:m1")
    seams["deliver_to_agent"].assert_not_called()


def test_unregistered_phone_is_a_customer_again(client, seams, member):
    seams["get_by_address"].return_value = {**LINE_ROW, "member_phones": {"+19990000000": MEMBER_ID}}
    assert _post(client).json() == {"status": "delivered"}
    member.assert_not_called()


CODE = "483921"


@pytest.fixture
def bind(seams, monkeypatch):
    seams["get_by_address"].return_value = {
        **LINE_ROW,
        "member_phones": {"+19990000000": MEMBER_ID, "+18880000000": "user_other"},
        "phone_codes": {CODE: {"user_id": MEMBER_ID, "exp": int(time.time()) + 600}},
    }
    b = AsyncMock(return_value=True)
    monkeypatch.setattr(ph, "bind_member_phone", b)
    return b


def test_a_live_code_links_the_sender_and_replies(client, seams, bind):
    r = _post(client, _event(content={"type": "text", "text": f" link  {CODE}\n"}))  # any case, any spacing
    assert r.status_code == 200 and r.json() == {"status": "member_linked"}
    kwargs = bind.await_args.kwargs
    assert bind.await_args.args == (LINE,)
    assert kwargs["code"] == CODE and kwargs["phone"] == CUSTOMER and kwargs["user_id"] == MEMBER_ID
    assert kwargs["drop"] == ["+19990000000"]
    seams["send_text"].assert_awaited_once_with(
        sender_id=LINE_USER, space_id=SPACE, text=ph.LINKED_REPLY, idempotency_key="link:m1"
    )
    seams["put_message"].assert_not_called()
    seams["deliver_to_agent"].assert_not_called()


def test_a_bare_six_digit_text_never_links(client, seams, bind):
    # A customer's order number is not a link attempt: only "LINK <code>" matches.
    assert _post(client, _event(content={"type": "text", "text": CODE})).json() == {"status": "delivered"}
    bind.assert_not_called()
    seams["put_message"].assert_awaited_once()


def test_a_failed_link_confirmation_still_links(client, seams, bind):
    seams["send_text"].side_effect = PhotonError(503, "http_503")
    assert _post(client, _event(content={"type": "text", "text": f"LINK {CODE}"})).json() == {"status": "member_linked"}


@pytest.mark.parametrize("expired", [False, True])
def test_an_expired_or_used_code_is_a_customer_text(client, seams, bind, expired):
    if expired:
        row = seams["get_by_address"].return_value
        row["phone_codes"] = {CODE: {"user_id": MEMBER_ID, "exp": int(time.time()) - 1}}
    else:
        bind.return_value = False  # used meanwhile
    assert _post(client, _event(content={"type": "text", "text": f"LINK {CODE}"})).json() == {"status": "delivered"}
    assert bind.await_count == (0 if expired else 1)
    seams["put_message"].assert_awaited_once()
    seams["deliver_to_agent"].assert_awaited_once()


def test_a_linked_member_never_relinks_by_code(client, seams, bind, member):
    seams["get_by_address"].return_value = {
        **seams["get_by_address"].return_value,
        "member_phones": {CUSTOMER: MEMBER_ID},
        "phone_codes": {CODE: {"user_id": MEMBER_ID, "exp": int(time.time()) + 600}},
    }
    assert _post(client, _event(content={"type": "text", "text": f"LINK {CODE}"})).json() == {"status": "member_routed"}
    bind.assert_not_called()
