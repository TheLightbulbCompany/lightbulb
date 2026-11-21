"""Lightbulb Meetings' tools for the agent runtime (`core.engine.hooks.tool`): the bodies of the engine's
meeting-context, meeting-chat and meeting-stop-transcribing routes, after the route's hook-token check. A refusal
raises the HTTPException the route answered with before."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException

from core.engine.hooks import ToolCall


async def meeting_context(call: ToolCall) -> dict:
    from core.voice.meetings import meeting_context as source

    owner_id = call.tenant_id
    body = SimpleNamespace(run_id=call.run_id, trigger_id=call.trigger_ids[0], **call.args)
    try:
        return await source.context(
            owner_id,
            body.run_id,
            body.trigger_id,
            body.section,
            body.cursor,
            grant=body.grant,
        )
    except source.NotAuthorized as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_arguments") from exc


async def meeting_chat(call: ToolCall) -> dict:
    """`routedTo` (when sent): the engine marks the triggers answered by the chat line."""
    from core.voice.meetings import meeting_chat as delivery

    owner_id, body = call.tenant_id, SimpleNamespace(trigger_ids=call.trigger_ids, **call.args)
    try:
        return await delivery.send(owner_id, body.trigger_ids, body.text)
    except delivery.NotCurrentMeeting as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except delivery.meeting_invites.MeetingBusy as exc:
        raise HTTPException(status_code=503, detail="meeting_busy") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_arguments") from exc


async def meeting_stop_transcribing(call: ToolCall) -> dict:
    from core.voice.meetings import meeting_chat as delivery

    owner_id, body = call.tenant_id, SimpleNamespace(trigger_ids=call.trigger_ids, **call.args)
    try:
        return await delivery.stop_transcribing(owner_id, body.trigger_ids, body.participant_id)
    except delivery.NotCurrentMeeting as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except delivery.meeting_invites.MeetingBusy as exc:
        raise HTTPException(status_code=503, detail="meeting_busy") from exc
