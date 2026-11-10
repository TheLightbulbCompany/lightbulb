"""The Router's chat delivery into the live occurrence that supplied its triggers.

Recall has no documented idempotency contract for send_chat_message. Claim before sending;
a transport failure is unknown and never automatically sent again. The existing webhook table
holds the bounded delivery receipt; the meeting lease fences the occurrence during the send.
"""

from __future__ import annotations

import hashlib

from core.dynamodb import run_in_thread
from core.repositories import messaging_repo, tasks_repo
from core.services import meeting_bots, meeting_invites, task_conversation, webhook_dedup


class NotCurrentMeeting(ValueError):
    pass


async def send(owner_id: str, trigger_ids: list[str], text: str) -> dict:
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError("meeting chat text must be between 1 and 500 characters")
    triggers = [await tasks_repo.get_trigger(owner_id, tid) for tid in trigger_ids]
    refs = [(t or {}).get("ref") or {} for t in triggers]
    rooms = {r.get("roomId") for r in refs}
    room_id = next(iter(rooms)) if len(rooms) == 1 else None
    if not room_id or any(r.get("origin") != "meeting" for r in refs):
        raise NotCurrentMeeting("not_a_meeting")
    async with meeting_invites.meeting_lease(room_id):
        meta = await messaging_repo.get_room(room_id) or {}
        meeting = meta.get("meeting") or {}
        bot_id, occurrence = meeting.get("recallBotId"), meeting.get("inCallAt")
        if (
            meta.get("ownerId") != owner_id
            or meeting.get("status") != "in_call"
            or not bot_id
            or not occurrence
            or any(r.get("meetingBotId") != bot_id or r.get("meetingInCallAt") != occurrence for r in refs)
        ):
            raise NotCurrentMeeting("not_current_meeting")
        from routers.voice_meet import inviting_member

        member = inviting_member(meeting, await messaging_repo.list_members(room_id))
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
        if status == "sent":
            message_id = await task_conversation.post_as_lightbulb(
                owner_id,
                room_id,
                text=text,
                event_id=key,
                attrs={"voiceDelivery": "context", "meetingChat": {"status": "sent", "botId": bot_id}},
                in_reply_to=refs[0].get("messageId"),
            )
            for trig in triggers:
                await tasks_repo.set_trigger_routed_to(
                    owner_id, trig["triggerId"], {**(trig.get("routedTo") or {}), "meetingChat": message_id}
                )
        return {"status": status, "replayed": fresh is webhook_dedup.WebhookDedupResult.ALREADY_SEEN}


async def stop_transcribing(owner_id: str, trigger_ids: list[str], participant_id: str = "") -> dict:
    """The Router's meeting_stop_transcribing: exclude the person who SPOKE the delegated line. The speaker comes
    from the meeting's own record (`askSpeakers`, written by /ask from the worker's participant id); `participant_id`
    from the model is only checked against it, so nobody can opt out someone else."""
    triggers = [await tasks_repo.get_trigger(owner_id, tid) for tid in trigger_ids]
    refs = [(t or {}).get("ref") or {} for t in triggers]
    rooms = {r.get("roomId") for r in refs}
    room_id = next(iter(rooms)) if len(rooms) == 1 else None
    if not room_id or any(r.get("origin") != "meeting" for r in refs):
        raise NotCurrentMeeting("not_a_meeting")
    async with meeting_invites.meeting_lease(room_id):
        meta = await messaging_repo.get_room(room_id) or {}
        meeting = meta.get("meeting") or {}
        bot_id, occurrence = meeting.get("recallBotId"), meeting.get("inCallAt")
        if (
            meta.get("ownerId") != owner_id
            or meeting.get("status") != "in_call"
            or not bot_id
            or not occurrence
            or any(r.get("meetingBotId") != bot_id or r.get("meetingInCallAt") != occurrence for r in refs)
        ):  # the same occurrence fence send() uses: a previous occurrence's turn never writes into this one
            raise NotCurrentMeeting("not_current_meeting")
        rows = [await messaging_repo.get_message_by_id(room_id, r.get("messageId")) or {} for r in refs]
        speakers = {(meeting.get("askSpeakers") or {}).get(row.get("clientMessageId")) for row in rows}
        if len(speakers) != 1 or not (pid := next(iter(speakers))) or participant_id not in ("", pid):
            raise NotCurrentMeeting("not_the_speaker")
        from routers.voice_meet import record_transcription

        name = ((meeting.get("roster") or {}).get(pid) or {}).get("name") or ""
        return await record_transcription(room_id, meeting, pid, name)
