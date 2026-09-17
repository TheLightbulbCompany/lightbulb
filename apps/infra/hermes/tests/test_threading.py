from bridge import turn
from tests import factories as f

ROOT = "55555555-5555-4555-8555-555555555555"


def test_a_channel_reply_opens_a_thread_under_the_request():
    msg = f.message()
    assert turn.reply_thread_root("channel", msg) == msg["messageId"]


def test_a_reply_inside_an_existing_thread_keeps_that_root():
    msg = f.message(threadRootMessageId=ROOT)
    assert turn.reply_thread_root("channel", msg) == ROOT
    assert turn.reply_thread_root("group-dm", msg) == ROOT


def test_a_dm_stays_linear():
    assert turn.reply_thread_root("dm", f.message()) is None
    assert turn.reply_thread_root("dm", f.message(threadRootMessageId=ROOT)) is None


def test_the_final_reply_carries_the_thread_root(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("done", None, "ack", turn.ZERO_USAGE))
    body = f.ingress()
    turn.run_turn("ceo", body)
    reply = next(b for path, b in posted if path.startswith("agent-messages/"))
    assert reply["threadRootMessageId"] == body["message"]["messageId"]
    assert reply["replyToMessageId"] == body["message"]["messageId"]
    assert reply["eventId"] == f"{body['eventId']}:ceo:reply"
