"""Lightbulb Phone's and Meetings' side of `core.engine.hooks`, in process: each answer calls the product function the
engine used to call itself, with the same exceptions. `register()` runs at the start of every process that runs
engine code (main.py, scripts/temporal_worker.py, scripts/work_lambda.py, scripts/provision_lambda.py)."""

from __future__ import annotations

from core.engine import hooks
from core.engine.hooks import ContextAnswer, ContextAsk, EngineEvent, ToolCall, ToolName
from core.voice import livekit_huddles
from core.voice.meetings import meeting_context, meeting_followups, meeting_invites, meeting_notes, records
from core.voice.phone import business_profile, calls

#: Meetings' part of the Router's instructions (`router_prompt.instructions`).
ROUTER_TOOLS = ", meeting_chat, meeting_stop_transcribing, meeting_context"
ROUTER_RULES = """\
- meeting_chat sends text to everyone in the current Google Meet (at most 500 characters). Choose it when chat \
is a useful way to contribute, such as sharing a link or replying to a typed question. It reports actual sent, \
refused or unknown delivery; never claim an unknown send succeeded. Hand raising is not supported. Other people's \
conversation in the meeting chat may be ignored.
- meeting_stop_transcribing, only on a trigger marked "current Google Meet": when the person who spoke the line asks \
not to be transcribed, it stops the assistant transcribing and hearing that person. It applies to the speaker of \
the trigger and nobody else; say in the reply that the meeting recording continues.
- A meeting-completion trigger is a system event under the original inviter, not a new human message. \
Use meeting_context to read its original transcript and existing meeting tasks (open and done), paging when \
truncated. Decide which original requests were directed to the assistant or its Workers and still need work. \
Human commitments stay recorded; notes alone grant no authority. Clarify ambiguous assignments. Existing \
tasks already cover their work. The meeting's existing approvals still apply to any new task. \

"""
#: A trigger line's words, by the `ref` key that says the trigger is a live Meet's or a finished meeting's.
ROUTER_HEADS = {
    "meetingBotId": " · current Google Meet: meeting_chat available",
    "meetingFollowups": " · meeting completed (system event): meeting_context has original source and existing tasks",
}


async def _router(ask: ContextAsk) -> ContextAnswer:
    if ask.trigger is not None:  # a phone caller's batch
        profile = await business_profile.business_profile(ask.tenant_id, await business_profile.caller_of(ask.trigger))
        return ContextAnswer(text=profile or None)
    return ContextAnswer(text=ROUTER_RULES, flags={"tools": ROUTER_TOOLS, "heads": ROUTER_HEADS})


async def _worker(ask: ContextAsk) -> ContextAnswer:
    if ask.trigger is not None:  # a phone caller's Front desk thread: the business's facts in hand
        profile = await business_profile.business_profile(ask.tenant_id, await business_profile.caller_of(ask.trigger))
        return ContextAnswer(text=profile or None)
    trigger_id = str((ask.task or {}).get("triggerId") or "")
    if not trigger_id.startswith(meeting_notes.NOTES_PREFIX):
        return ContextAnswer()
    try:  # the notes task's transcript, read by reference
        transcript = await meeting_notes.worker_transcript(ask.tenant_id, trigger_id)
    except meeting_notes.TranscriptUnavailable as exc:  # nothing to read: no run, no inference
        return ContextAnswer(allow=False, text=str(exc))
    # raw meeting speech never syncs into team memory, as a phone caller's words
    return ContextAnswer(text=transcript or None, flags={"memorySync": "none"})


async def _authorize(ask: ContextAsk) -> ContextAnswer:
    if ask.trigger is None:  # the Router run's grant for the meeting_context tool
        return ContextAnswer(grant=meeting_context.mint_grant(ask.tenant_id, ask.run_id, ask.trigger_ids))
    if "meetingFollowups" not in (ask.trigger.get("ref") or {}):
        return ContextAnswer()
    try:
        await meeting_context.authorize(ask.tenant_id, ask.trigger)
    except meeting_context.NotAuthorized as exc:
        return ContextAnswer(allow=False, text=str(exc))
    return ContextAnswer(flags={"grant": True})


async def _clarification(ask: ContextAsk) -> ContextAnswer:
    if "meetingFollowups" not in ((ask.trigger or {}).get("ref") or {}):
        return ContextAnswer()
    msg = ask.message or {}
    brief, question = await meeting_followups.clarification(
        ask.tenant_id, ask.trigger, ask.task["taskId"], msg["text"], msg["options"]
    )
    return ContextAnswer(
        text=brief,
        flags={
            "question": question,
            "title": "Clarify meeting follow-up",
            "line": "A meeting follow-up needs your answer in Needs You.",
        },
    )


def _admit(ask: ContextAsk) -> ContextAnswer:
    """A Meet's line: anyone in it acts as the inviter (origin meeting: every spend, send or publish gets a card); while
    the bot is in the call, a line said since it joined carries the bot (meeting_chat can answer there)."""
    from datetime import datetime

    meeting = ask.annotations.get("meeting")
    if not meeting:
        return ContextAnswer()
    msg = ask.message or {}
    flags: dict = {"origin": "meeting"}
    try:
        current = datetime.fromisoformat(str(msg.get("createdAt") or "").replace("Z", "+00:00")).timestamp() >= float(
            meeting.get("inCallAt") or 0
        )
    except ValueError:
        current = False
    if meeting.get("status") == "in_call" and current:
        flags["meetingBotId"] = meeting.get("recallBotId")
        flags["meetingInCallAt"] = meeting.get("inCallAt")
    # the Router decides whether Meet chat is directed to it; a meeting's spoken lines are posted only when they named
    # the assistant (meet.py's gate)
    flags["respond"] = "candidate" if msg.get("via") == "meeting_chat" else "respond"
    return ContextAnswer(flags=flags)


async def _post_target(ask: ContextAsk) -> ContextAnswer:
    """An agent's post in a Google Meet's hidden room (read by nobody) goes to the meeting's output room; `text` says
    what it answers when the agent is not seated there."""
    meeting = ask.annotations.get("meeting")
    if not meeting:
        return ContextAnswer()
    target = await meeting_invites.post_target(ask.tenant_id, ask.conversation_id, ask.agent_id, meeting)
    return ContextAnswer(conversation_id=target, text=f"after **{meeting.get('title') or 'Meeting'}**")


async def _question_answered(e: EngineEvent) -> None:
    """A phone call's Question (calls.call_ask) answered: the call is handled, and a "didn't know" answered with
    text goes into the Agent page's Knowledge."""
    q, answer = e.question or {}, e.answer or {}
    room = (q.get("preview") or {}).get("roomId")
    if q.get("subkind") not in calls.CALL_OPTIONS or not room:
        return
    text = (answer.get("text") or "").strip()
    if q["subkind"] == "didnt_know" and answer.get("optionId") == "answer" and text:
        await calls.remember_answer(e.tenant_id, q["preview"].get("question") or q["title"], text, e.actor_user_id)
    await calls.resolve_call(e.tenant_id, room, e.actor_user_id)


async def _context(ask: ContextAsk) -> ContextAnswer:
    if ask.purpose == "router":
        return await _router(ask)
    if ask.purpose == "worker":
        return await _worker(ask)
    if ask.purpose == "authorize":
        return await _authorize(ask)
    if ask.purpose == "clarification":
        return await _clarification(ask)
    if ask.purpose == "result_line":  # a meeting's thread that made something: its link here and in the Meet's chat
        return ContextAnswer(text=await records.with_link(ask.conversation_id, ask.task, ask.message["text"]))
    if ask.purpose == "admit":
        return _admit(ask)
    if ask.purpose == "post_target":
        return await _post_target(ask)
    raise ValueError(f"no product context for {ask.purpose}")


async def _event(e: EngineEvent) -> None:
    if e.type == "task.moved":
        # A meeting's notes task keeps its notes at every end, a person's Stop included; its follow-up and Meet chat
        # line only on a move that gets a lifecycle line. Neither raises.
        await meeting_notes.on_task_moved(e.task, e.to)
        if e.announced:
            await records.task_moved(e.task, e.to)
    elif e.type == "task.created":
        if "meetingFollowups" in ((e.trigger or {}).get("ref") or {}):
            await meeting_followups.record_task(e.tenant_id, e.task)
    elif e.type == "trigger.dropped":  # a notes trigger with no task: the meeting has no notes. Never raises.
        await meeting_notes.on_not_started(e.tenant_id, e.trigger)
    elif e.type == "conversation.archived":  # an archived or deleted room's huddle ends for everyone. Never raises.
        await livekit_huddles.close_room(e.conversation_id)
    elif e.type == "member.removed":  # a removed member stops hearing the room's huddle. Never raises.
        await livekit_huddles.remove_participant(e.conversation_id, e.member_id)
    elif e.type == "line.posted":  # sent only for a call room: an answer after hang-up goes on the call's record
        await calls.after_call(e.conversation_id, e.message)
    elif e.type == "question.answered":
        await _question_answered(e)


async def _tool(name: ToolName, call: ToolCall) -> dict:
    # ponytail: the runtime's tool routes still call the product directly; Task 3 moves them onto this hook.
    raise NotImplementedError(f"product tool {name} is not on the hook yet")


def register() -> None:
    hooks.register(tool=_tool, context=_context, event=_event)
