"""The Router's chat delivery into the live occurrence that supplied its triggers.

Recall has no documented idempotency contract for send_chat_message. Claim before sending;
a transport failure is unknown and never automatically sent again. The existing webhook table
holds the bounded delivery receipt; the meeting lease fences the occurrence during the send.
"""

from __future__ import annotations

import hashlib

from core.dynamodb import run_in_thread
from core.engine import client as engine
from core.engine.contract import LineIn, Principal
from core.services import webhook_dedup
from core.voice.meetings import meeting_bots, meeting_invites


class NotCurrentMeeting(ValueError):
    pass


async def _current(owner_id: str, trigger_ids: list[str]) -> tuple[list[dict | None], list[dict], str]:
    """The triggers, their refs and their room, when every trigger came from one meeting's room."""
    triggers = [await engine.get_trigger(owner_id, tid) for tid in trigger_ids]
    refs = [(t or {}).get("ref") or {} for t in triggers]
    rooms = {r.get("roomId") for r in refs}
    room_id = next(iter(rooms)) if len(rooms) == 1 else None
    if not room_id or any(r.get("origin") != "meeting" for r in refs):
        raise NotCurrentMeeting("not_a_meeting")
    return triggers, refs, room_id


async def _live(owner_id: str, room_id: str, refs: list[dict], *, with_members: bool = False):
    """The room and its meeting while that meeting's bot is in the call the refs came from (under the lease)."""
    conv = await engine.get_conversation(owner_id, room_id, with_members=with_members)
    meeting = (conv.annotations.get("meeting") if conv else None) or {}
    bot_id, occurrence = meeting.get("recallBotId"), meeting.get("inCallAt")
    if (
        meeting.get("status") != "in_call"
        or not bot_id
        or not occurrence
        or any(r.get("meetingBotId") != bot_id or r.get("meetingInCallAt") != occurrence for r in refs)
    ):  # a previous occurrence's turn never writes into this one
        raise NotCurrentMeeting("not_current_meeting")
    return conv, meeting


async def send(owner_id: str, trigger_ids: list[str], text: str) -> dict:
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError("meeting chat text must be between 1 and 500 characters")
    triggers, refs, room_id = await _current(owner_id, trigger_ids)
    async with meeting_invites.meeting_lease(room_id):
        conv, meeting = await _live(owner_id, room_id, refs, with_members=True)
        bot_id = meeting["recallBotId"]
        from core.voice.meetings.records import inviting_member

        member = inviting_member(meeting, conv.members)
        if not member or any(t.get("actorUserId") != member for t in triggers):
            raise NotCurrentMeeting("no_current_inviter")
        digest = hashlib.sha256(("\0".join(sorted(set(trigger_ids))) + "\0" + text).encode()).hexdigest()
        key = f"meeting-chat:{owner_id}:{bot_id}:{digest}"
        fresh = await webhook_dedup.record_event_or_skip(key, source="meeting_chat")
        status = "unknown"
        if fresh is webhook_dedup.WebhookDedupResult.RECORDED:
            try:
                await meeting_bots.say(bot_id, text)
                status = "sent"
            except meeting_bots.RecallError as exc:
                # A rejected request did not send; a lost response may have sent. Neither retries itself.
                status = (
                    "refused" if exc.code.isdigit() and 400 <= int(exc.code) < 500 and exc.code != "408" else "unknown"
                )
            except Exception:  # the provider may have accepted before its response became unreadable
                status = "unknown"
            await run_in_thread(
                webhook_dedup._table().update_item,
                Key={"event_id": key},
                UpdateExpression="SET chat_status = :status",
                ExpressionAttributeValues={":status": status},
            )
        else:
            status = (await webhook_dedup._read_event(key)).get("chat_status", "unknown")
        out = {"status": status, "replayed": fresh is webhook_dedup.WebhookDedupResult.ALREADY_SEEN}
        if status == "sent":
            line = await engine.append_line(
                LineIn(
                    tenant_id=owner_id,
                    conversation_id=room_id,
                    principal=Principal(kind="system", id="lightbulb"),
                    text=text,
                    idempotency_key=key,
                    system_attrs={"voiceDelivery": "context", "meetingChat": {"status": "sent", "botId": bot_id}},
                    in_reply_to=refs[0].get("messageId"),
                )
            )
            out["routedTo"] = {"meetingChat": line.message_id}  # the triggers' answer: the engine marks them
        return out


async def stop_transcribing(owner_id: str, trigger_ids: list[str], participant_id: str = "") -> dict:
    """The Router's meeting_stop_transcribing: exclude the person who SPOKE the delegated line. The speaker comes
    from the meeting's own record (`askSpeakers`, written by /ask from the worker's participant id); `participant_id`
    from the model is only checked against it, so nobody can opt out someone else."""
    _, refs, room_id = await _current(owner_id, trigger_ids)
    async with meeting_invites.meeting_lease(room_id):
        _, meeting = await _live(owner_id, room_id, refs)
        rows = [row or {} for row in await engine.get_lines(owner_id, room_id, [r.get("messageId") for r in refs])]
        speakers = {(meeting.get("askSpeakers") or {}).get(row.get("clientMessageId")) for row in rows}
        if len(speakers) != 1 or not (pid := next(iter(speakers))) or participant_id not in ("", pid):
            raise NotCurrentMeeting("not_the_speaker")
        from core.voice.meetings.records import record_transcription

        name = ((meeting.get("roster") or {}).get(pid) or {}).get("name") or ""
        return await record_transcription(owner_id, room_id, meeting, pid, name)
