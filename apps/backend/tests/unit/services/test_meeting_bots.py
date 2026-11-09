"""The Recall adapter: request bodies, join timing, leave fallback, webhook signatures."""

import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from core.services import livekit_huddles, meeting_bots

KEY = "rk_live_SECRET_KEY_VALUE"
URL = "https://meet.google.com/abc-defg-hij"


@pytest.fixture
def recall(monkeypatch):
    calls = []
    responses = []

    def handler(request):
        calls.append(request)
        status, body = responses.pop(0) if responses else (201, {"id": "bot-1"})
        return httpx.Response(status, json=body)

    monkeypatch.setattr(meeting_bots, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(meeting_bots.settings, "RECALL_API_KEY", KEY)
    monkeypatch.setattr(meeting_bots.settings, "RECALL_WEBHOOK_SECRET", "whsec_" + base64.b64encode(b"k" * 24).decode())
    monkeypatch.setattr(meeting_bots.settings, "PUBLIC_API_BASE", "https://api.example")
    monkeypatch.setattr(meeting_bots.settings, "LIVEKIT_API_SECRET", "lk-secret")
    monkeypatch.setattr(meeting_bots.settings, "MEET_BOT_PAGE_URL", "https://app.example/meet-bot")
    monkeypatch.setattr(meeting_bots.settings, "GATEWAY_RELAY_URL", "wss://bv.example/api/v1/gateway/ws")
    return calls, responses


def _meeting(start_in_s):
    start = int(time.time()) + start_in_s
    return {"startAt": start, "endAt": start + 1800, "meetingUrl": URL, "ticketNonce": "nonce-1"}


async def test_create_body(recall, caplog):
    calls, _ = recall
    caplog.set_level(logging.DEBUG)
    assert await meeting_bots.create(room_id="room-1", meeting=_meeting(12 * 60), company="Acme") == "bot-1"
    (req,) = calls
    assert req.method == "POST" and str(req.url) == "https://us-east-1.recall.ai/api/v1/bot/"
    assert req.headers["authorization"] == f"Token {KEY}" and req.headers["idempotency-key"] == "nonce-1"
    body = json.loads(req.content)
    assert body["meeting_url"] == URL and body["bot_name"] == "Acme agents · Lightbulb"
    audio = body["recording_config"]["realtime_endpoints"].pop()
    assert body["recording_config"] == {
        "retention": None,
        "audio_separate_raw": {},
        "realtime_endpoints": [
            {
                "type": "webhook",
                "url": "https://api.example/api/v1/webhooks/recall/realtime",
                "events": [
                    "participant_events.chat_message",
                    "participant_events.speech_on",
                    "participant_events.speech_off",
                    "participant_events.join",
                    "participant_events.leave",
                ],
            }
        ],
    }
    # Each person's audio: the raw-WS door's /meet/ socket, trailing slash before the query.
    assert audio["type"] == "websocket"
    assert audio["events"] == ["audio_separate_raw.data", "participant_events.join", "participant_events.leave"]
    url = urlsplit(audio["url"])
    assert (url.scheme, url.netloc, url.path) == ("wss", "bv.example", "/api/v1/gateway/meet/")
    query = parse_qs(url.query)
    assert query["room"] == ["room-1"]
    assert livekit_huddles.verify_meet_ticket(query["token"][0], livekit_huddles.AUDIO_TICKET) == ("room-1", "nonce-1")
    assert livekit_huddles.verify_meet_ticket(query["token"][0]) is None  # not a page ticket
    # env: Recall's status webhook is account-wide, so the other environment ignores this bot.
    assert body["metadata"] == {"roomId": "room-1", "nonce": "nonce-1", "env": meeting_bots.settings.ENVIRONMENT}
    assert body["variant"] == {"google_meet": "web_4_core"}
    assert body["automatic_leave"] == {
        "waiting_room_timeout": 180,
        "noone_joined_timeout": 1200,
        "everyone_left_timeout": {"timeout": 300},
    }
    assert "google_meet" not in body  # no login group configured
    assert "chat" not in body  # one announcement: voice_meet says it (test_single_announcement)
    page = body["output_media"]["camera"]["config"]["url"]
    assert body["output_media"]["camera"]["kind"] == "webpage"
    assert page.startswith("https://app.example/meet-bot#t=")
    payload, sig = page.split("#t=")[1].split(".")
    assert hmac.compare_digest(
        sig, hmac.new(b"lk-secret", b"meet-ticket|" + payload.encode(), hashlib.sha256).hexdigest()
    )
    room, nonce, _exp = base64.urlsafe_b64decode(payload + "==").decode().split("|")
    assert (room, nonce) == ("room-1", "nonce-1")
    assert "join_at" in body
    assert all(KEY not in r.getMessage() and URL not in r.getMessage() for r in caplog.records)


async def test_create_bot_uses_login_group(recall, monkeypatch):
    calls, _ = recall
    monkeypatch.setattr(meeting_bots.settings, "RECALL_GOOGLE_LOGIN_GROUP_ID", "group-1")
    await meeting_bots.create(room_id="room-1", meeting=_meeting(12 * 60), company="Acme")
    assert json.loads(calls[0].content)["google_meet"] == {"google_login_group_id": "group-1"}


async def test_create_bot_does_not_leave_on_brief_empty_room(recall):
    calls, _ = recall
    await meeting_bots.create(room_id="room-1", meeting=_meeting(12 * 60), company="Acme")
    assert json.loads(calls[0].content)["automatic_leave"]["everyone_left_timeout"] == {"timeout": 300}


async def test_prod_without_login_group_warns(recall, monkeypatch, caplog):
    monkeypatch.setattr(meeting_bots.settings, "ENVIRONMENT", "prod")
    await meeting_bots.create(room_id="room-1", meeting=_meeting(12 * 60), company="Acme")
    assert "signed-out bot" in caplog.text


async def test_a_meeting_a_minute_out_joins_now(recall):
    calls, _ = recall
    await meeting_bots.create(room_id="room-1", meeting=_meeting(60), company="Acme")
    assert "join_at" not in json.loads(calls[0].content)


async def test_errors_are_recall_errors(recall, monkeypatch):
    _, responses = recall
    responses.append((507, {"detail": "out of adhoc bots"}))
    with pytest.raises(meeting_bots.RecallError) as err:
        await meeting_bots.create(room_id="room-1", meeting=_meeting(60), company="Acme")
    assert err.value.code == "507" and "free" in err.value.reason

    def slow(request):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(meeting_bots, "_transport", httpx.MockTransport(slow))
    with pytest.raises(meeting_bots.RecallError) as err:
        await meeting_bots.leave("bot-1")
    assert err.value.code == "timeout"


async def test_reschedule_patches_join_at_and_reticket(recall):
    calls, responses = recall
    responses.append((200, {"id": "bot-1"}))
    await meeting_bots.reschedule("bot-1", room_id="room-1", meeting=_meeting(3600))
    assert calls[0].method == "PATCH" and calls[0].url.path == "/api/v1/bot/bot-1/"
    body = json.loads(calls[0].content)
    assert set(body) == {"join_at", "output_media", "recording_config"}
    # The audio ticket is re-minted too, so a later end does not leave Recall's socket refused.
    url = body["recording_config"]["realtime_endpoints"][1]["url"]
    assert livekit_huddles.verify_meet_ticket(parse_qs(urlsplit(url).query)["token"][0], livekit_huddles.AUDIO_TICKET)


async def test_leave_deletes_then_falls_back_to_leave_call(recall):
    calls, responses = recall
    responses.extend([(204, None), (405, {"detail": "dispatched"}), (200, {})])
    await meeting_bots.leave("bot-1")
    await meeting_bots.leave("bot-2")
    assert [(c.method, c.url.path) for c in calls] == [
        ("DELETE", "/api/v1/bot/bot-1/"),
        ("DELETE", "/api/v1/bot/bot-2/"),
        ("POST", "/api/v1/bot/bot-2/leave_call/"),
    ]


def _signed(body, stamp, key=b"k" * 24):
    sig = base64.b64encode(hmac.new(key, f"msg_1.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": "msg_1", "webhook-timestamp": str(stamp), "webhook-signature": f"v1,{sig}"}


def test_webhook_signature(recall):
    body, now = b'{"event":"bot.done"}', int(time.time())
    assert meeting_bots.verify_webhook(_signed(body, now), body)
    rotated = _signed(body, now)
    rotated["webhook-signature"] = "v1,AAAA " + rotated["webhook-signature"]
    assert meeting_bots.verify_webhook(rotated, body)
    assert not meeting_bots.verify_webhook(_signed(body, now), body + b" ")
    assert not meeting_bots.verify_webhook(_signed(body, now, key=b"x" * 24), body)
    assert not meeting_bots.verify_webhook(_signed(body, now - 600), body)
    assert not meeting_bots.verify_webhook({}, body)


async def test_an_unreadable_create_answer_is_a_recall_error(recall, monkeypatch):
    def garbage(request):
        return httpx.Response(201, content=b"<html>oops</html>")

    monkeypatch.setattr(meeting_bots, "_transport", httpx.MockTransport(garbage))
    with pytest.raises(meeting_bots.RecallError) as err:
        await meeting_bots.create(room_id="room-1", meeting=_meeting(60), company="Acme")
    assert err.value.code == "bad_response"


def test_no_webhook_secret_refuses_everything(recall, monkeypatch):
    body, now = b"{}", int(time.time())
    monkeypatch.setattr(meeting_bots.settings, "RECALL_WEBHOOK_SECRET", "")
    assert not meeting_bots.verify_webhook(_signed(body, now), body)


async def test_a_long_company_name_fits_recalls_bot_name_cap(recall):
    await meeting_bots.create(room_id="room-1", meeting=_meeting(60), company="A" * 120)
    name = json.loads(recall[0][0].content)["bot_name"]
    assert len(name) == 100 and name.endswith(" agents · Lightbulb")


async def test_find_for_room_adopts_only_a_live_bot_of_this_booking(recall):
    calls, responses = recall
    ended = {"id": "bot-0", "status_changes": [{"code": "joining_call"}, {"code": "done"}]}
    scheduled = {"id": "bot-1", "status_changes": []}
    responses.extend([(200, {"results": [ended, scheduled]}), (200, {"results": [ended]})])
    assert await meeting_bots.find_for_room("room-1", "nonce-1") == "bot-1"
    assert await meeting_bots.find_for_room("room-1", "nonce-1") is None
    assert calls[0].method == "GET" and calls[0].url.path == "/api/v1/bot/"
    assert dict(calls[0].url.params) == {"metadata__roomId": "room-1", "metadata__nonce": "nonce-1"}


async def test_audio_mode_reads_what_the_bot_was_booked_with(recall):
    calls, responses = recall
    responses.extend(
        [
            (200, {"id": "bot-1", "recording_config": {"audio_separate_raw": {}}}),
            (200, {"id": "bot-0"}),
            (
                200,
                {"id": "bot-2", "recording_config": {"audio_separate_raw": None}},
            ),  # Recall's default for an older bot
        ]
    )
    assert await meeting_bots.audio_mode("bot-1") == "separate"
    assert await meeting_bots.audio_mode("bot-0") is None
    assert await meeting_bots.audio_mode("bot-2") is None
    assert calls[0].method == "GET" and calls[0].url.path == "/api/v1/bot/bot-1/"


def test_the_audio_ticket_is_redacted_from_the_handshake_log():
    """Uvicorn logs the WebSocket request line; a live ticket in CloudWatch could be replayed."""
    import logging

    from core.observability.logging import RedactTokenFilter

    url = meeting_bots.audio_url("room-1", "abc.def")
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, "", 0, '%s - "WebSocket %s" [accepted]', ("1.2.3.4", url), None
    )
    RedactTokenFilter().filter(record)
    assert "abc.def" not in record.getMessage() and "token=REDACTED" in record.getMessage()


async def test_say_posts_one_chat_line_to_everyone(recall):
    calls, responses = recall
    responses.append((200, {"id": "bot-1"}))
    await meeting_bots.say("bot-1", "x" * 600)
    assert calls[0].method == "POST" and calls[0].url.path == "/api/v1/bot/bot-1/send_chat_message/"
    assert json.loads(calls[0].content) == {"to": "everyone", "message": "x" * 500}
