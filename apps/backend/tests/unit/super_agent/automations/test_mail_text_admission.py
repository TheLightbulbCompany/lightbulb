"""Mail and text messages as admission sources (lane A, Task 9; contracts-v2 §14.6): in a super-agent workspace
`deliver_to_agent` writes the one `TRIG#` + `WORK#ADMIT#` pair instead of posting to the runtime; replies to a
mail/text trigger go back the way they came (`source_replies.deliver_reply`)."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import tasks_repo
from core.services import agent_mail_delivery, source_replies
from core.services.agent_mail_delivery import deliver_to_agent

WS = "org_1"
AGENT = "agent_ceo"
MAIL_REF = {"address": "ada@example.com", "messageId": "<m1@example.com>", "references": None, "subject": "Invoice 12"}


@pytest.fixture
def env(table, monkeypatch):
    outbox = AsyncMock()
    with (
        patch("core.services.agent_mail_delivery.flags_for", AsyncMock(return_value=True)) as flags,
        patch("core.repositories.messaging_repo.put_admit_trigger", outbox),
        patch("core.services.runtime_transport.request", AsyncMock()) as runtime,
    ):
        yield type("Env", (), {"flags": flags, "outbox": outbox, "runtime": runtime})()


async def _mail(**over):
    return await deliver_to_agent(
        **{
            "owner_id": WS,
            "agent_id": AGENT,
            "message": "From: ada@example.com\nSubject: Invoice 12\n\nPlease pay invoice 12.",
            "idempotency_key": "<m1@example.com>",
            "session_key": "mail-thread-1",
            "source": "mail",
            "reply_ref": MAIL_REF,
        }
        | over
    )


async def test_mail_admitted_once(env):
    """An SES retry after a lost 200 delivers the same message again: one trigger, one Router signal."""
    assert await _mail() == "delivered"
    assert await _mail() == "delivered"
    row = await tasks_repo.get_trigger(WS, "mail:<m1@example.com>")
    assert row["source"] == "mail" and row["admission"] == "respond" and row["lane"] == "shared"
    assert row["ref"]["agentId"] == AGENT and row["ref"]["sessionKey"] == "mail-thread-1"
    assert row["ref"]["reply"] == MAIL_REF and row["ref"]["text"].startswith("From: ada@example.com")
    assert row["version"] == 1  # the second delivery did not rewrite the row
    # the outbox put is unconditional by design (O6): a MODIFY of the same row is ignored by the pipe
    assert env.outbox.await_count == 2 and env.outbox.await_args.args == (WS, "mail:<m1@example.com>")
    assert not env.runtime.called


async def test_text_admitted_with_text_trigger_id(env):
    ref = {"provider": "sendblue", "address": "team@x", "fromNumber": "+1555", "toNumber": "+1666"}
    assert (
        await _mail(idempotency_key="sendblue:h1", source="text", reply_ref=ref, message="do you open at 9?")
        == "delivered"
    )
    row = await tasks_repo.get_trigger(WS, "text:sendblue:h1")
    assert row["source"] == "text" and row["ref"]["reply"] == ref


async def test_text_is_capped_to_32k(env):
    await _mail(message="x" * 40_000)
    assert len((await tasks_repo.get_trigger(WS, "mail:<m1@example.com>"))["ref"]["text"]) == 32_768


async def test_outbox_failure_returns_error_and_the_retry_admits(env):
    env.outbox.side_effect = [RuntimeError("ddb down"), None]
    assert await _mail() == "error"  # the caller rolls back its dedup row and asks the provider to retry
    assert await _mail() == "delivered"
    assert await tasks_repo.get_trigger(WS, "mail:<m1@example.com>") is not None


async def test_switch_off_posts_to_runtime_unchanged(env, monkeypatch):
    env.flags.return_value = False
    env.runtime.return_value = type("R", (), {"status_code": 202})()
    with (
        patch(
            "core.services.agent_mail_delivery.container_repo.get_by_owner_id",
            AsyncMock(return_value={"agentcore_runtime_arn": "arn", "gateway_token": "g"}),
        ),
        patch("core.services.agent_mail_delivery.trial_block_reason", AsyncMock(return_value=None)),
    ):
        assert await _mail() == "delivered"
    body = env.runtime.await_args.kwargs["json_body"]
    assert body["eventKind"] == "agent_mail" and body["eventId"] == "<m1@example.com>"
    assert body["message"] == {"text": "From: ada@example.com\nSubject: Invoice 12\n\nPlease pay invoice 12."}
    assert "source" not in body and "reply" not in body  # today's body, byte for byte
    assert await tasks_repo.get_trigger(WS, "mail:<m1@example.com>") is None
    assert not env.outbox.called


@pytest.mark.parametrize("router", ["ses_inbound", "sendblue_inbound", "photon_inbound"])
def test_stranger_caps_stay_in_the_callers_before_admission(router):
    """§14.6: today's stranger caps run in the routers; a capped message never reaches `deliver_to_agent`."""
    src = (Path(__file__).parents[4] / "routers" / f"{router}.py").read_text()
    assert src.index("try_consume_stranger_wake(") < src.index("await deliver_to_agent(")


# --- replies back to mail / text ---------------------------------------------------------------------------


def _trigger(source, reply):
    return {"triggerId": f"{source}:x", "source": source, "ref": {"agentId": AGENT, "reply": reply}}


async def test_deliver_reply_mail_uses_thread():
    with patch("core.services.agent_mail_reply.send_reply", AsyncMock(return_value="sent")) as send:
        await source_replies.deliver_reply(WS, _trigger("mail", MAIL_REF), "Paid: invoice 12 went out today.")
    ctx = send.await_args.kwargs["ctx"]
    assert ctx == {
        "owner_id": WS,
        "agent_id": AGENT,
        "reply_to": "ada@example.com",
        "subject": "Invoice 12",
        "in_reply_to": "<m1@example.com>",
        "references": None,
    }
    assert send.await_args.kwargs["body_text"] == "Paid: invoice 12 went out today."


async def test_deliver_reply_text_sends_sms():
    ref = {"provider": "sendblue", "address": "team@x", "fromNumber": "+1555", "toNumber": "+1666"}
    with (
        patch("core.services.sendblue.send_text", AsyncMock(return_value={})) as send,
        patch("core.repositories.agent_mail_inbox_repo.put_sent", AsyncMock()) as put_sent,
    ):
        await source_replies.deliver_reply(WS, _trigger("text", ref), "Yes, 9 to 5.")
    send.assert_awaited_once_with(from_number="+1555", to_number="+1666", content="Yes, 9 to 5.")
    kw = put_sent.await_args.kwargs
    assert (kw["address"], kw["to"], kw["text"], kw["channel"]) == ("team@x", "+1666", "Yes, 9 to 5.", "text")
    assert kw["owner_id"] == WS and kw["agent_id"] == AGENT


async def test_deliver_reply_text_photon():
    from core.services.webhook_dedup import WebhookDedupResult

    ref = {"provider": "photon", "address": "team@x", "senderId": "u_line", "spaceId": "sp_1", "to": "+1666"}
    with (
        patch("core.services.photon.send_text", AsyncMock(return_value={"id": "p9"})) as send,
        patch("core.repositories.agent_mail_inbox_repo.put_sent", AsyncMock()) as put_sent,
        patch("core.services.source_replies.record_event_or_skip", AsyncMock(return_value=WebhookDedupResult.RECORDED)),
    ):
        await source_replies.deliver_reply(WS, _trigger("text", ref), "Yes, 9 to 5.", idempotency_key="evt-9")
    # Photon's own idempotency key is per reply (the Router's event id), not per trigger
    assert send.await_args.kwargs == {
        "sender_id": "u_line",
        "space_id": "sp_1",
        "text": "Yes, 9 to 5.",
        "idempotency_key": "reply:evt-9",
    }
    assert put_sent.await_args.kwargs["message_id"] == "p9"


async def test_deliver_reply_is_idempotent_on_the_routers_event_id():
    """A Router Activity retry carries the same event id: one text goes out, not two; a failed send frees it."""
    ref = {"provider": "sendblue", "address": "team@x", "fromNumber": "+1555", "toNumber": "+1666"}
    seen: set[str] = set()

    async def record(event_id, *, source, ttl_seconds=None):
        from core.services.webhook_dedup import WebhookDedupResult

        if event_id in seen:
            return WebhookDedupResult.ALREADY_SEEN
        seen.add(event_id)
        return WebhookDedupResult.RECORDED

    async def forget(event_id):
        seen.discard(event_id)

    send = AsyncMock(side_effect=[RuntimeError("sendblue 503"), {}, {}])
    with (
        patch("core.services.source_replies.record_event_or_skip", side_effect=record),
        patch("core.services.source_replies.delete_event", side_effect=forget),
        patch("core.services.sendblue.send_text", send),
        patch("core.repositories.agent_mail_inbox_repo.put_sent", AsyncMock()),
    ):
        with pytest.raises(RuntimeError):
            await source_replies.deliver_reply(WS, _trigger("text", ref), "Yes.", idempotency_key="evt-1")
        await source_replies.deliver_reply(WS, _trigger("text", ref), "Yes.", idempotency_key="evt-1")  # the retry
        await source_replies.deliver_reply(WS, _trigger("text", ref), "Yes.", idempotency_key="evt-1")  # a second retry
    assert send.await_count == 2  # the failed attempt and the one that went out; the last was a no-op
    assert seen == {"reply:org_1:evt-1"}  # scoped per workspace


async def test_a_mail_reply_that_was_not_sent_frees_the_key():
    """`send_reply` never raises: a `not_sent` answer must still free the key, or the retry is a no-op."""
    from core.services.webhook_dedup import WebhookDedupResult

    freed = []
    with (
        patch("core.services.source_replies.record_event_or_skip", AsyncMock(return_value=WebhookDedupResult.RECORDED)),
        patch("core.services.source_replies.delete_event", AsyncMock(side_effect=lambda k: freed.append(k))),
        patch("core.services.agent_mail_reply.send_reply", AsyncMock(return_value="not_sent")),
    ):
        with pytest.raises(RuntimeError, match="not_sent"):
            await source_replies.deliver_reply(WS, _trigger("mail", MAIL_REF), "hello", idempotency_key="evt-2")
    assert freed == ["reply:org_1:evt-2"]


async def test_deliver_reply_without_a_reply_ref_is_a_noop():
    with patch("core.services.agent_mail_reply.send_reply", AsyncMock()) as send:
        await source_replies.deliver_reply(WS, _trigger("mail", None), "hello")
        await source_replies.deliver_reply(WS, {"triggerId": "chat:m", "source": "chat", "ref": {}}, "hello")
    assert not send.called


def test_module_never_imports_paperclip():
    src = Path(agent_mail_delivery.__file__).read_text() + Path(source_replies.__file__).read_text()
    assert "paperclip" not in src.lower()
