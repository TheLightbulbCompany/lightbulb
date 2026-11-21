"""Lightbulb Phone's tools for the agent runtime (`core.engine.hooks.tool`): the bodies of the engine's front-desk,
transfer-call and business-facts routes. The route has already checked the hook token and the run, and hands over the engine rows the
body needs (`call.triggers`); a refusal raises the HTTPException the route answered with before."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException

from core.engine.hooks import ToolCall
from core.services import task_conversation


async def front_desk(call: ToolCall) -> dict:
    """A phone caller's thread acts for that caller (governance P2, the caller case): the call comes from the task's
    own trigger, never the model, and each action is the phone's own route, which keeps a caller to their own
    bookings and honours the Agent page's allow switches. 403 for any task that is not a caller's."""
    from pydantic import ValidationError

    from core.voice.phone import business_profile
    from routers import voice_phone as vp

    owner_id, body, trig = call.tenant_id, SimpleNamespace(**call.args), (call.triggers or [None])[0]
    call_id = ((trig or {}).get("ref") or {}).get("callId")
    meta = await vp.messaging_repo.get_room(call_id) if call_id else None
    if (meta or {}).get("kind") != "call" or meta.get("ownerId") != owner_id:
        raise HTTPException(status_code=403, detail="not_a_caller_thread")
    try:
        if body.action == "availability":
            return await vp.availability(call_id, vp.DateIn(date=body.date, service=body.service))
        if body.action == "my_bookings":
            return await vp.my_bookings(call_id)
        if body.action == "waitlist":
            # The waitlist has no owner screen (founder 10 Nov: hidden), so the ask is also a message: the owner
            # sees it in Needs you, and the caller is told what was recorded, never a spot nobody sees.
            try:
                await vp.join_waitlist(
                    call_id, vp.WaitlistIn(date=body.date, service=body.service, name=body.name, note=body.note)
                )
            except (HTTPException, ValidationError):
                pass  # off on the Agent page, a bad or past date, no caller number: the message below still goes
            want = " ".join(x for x in (body.service, body.date, body.note) if x)
            text = f"Wants a waitlist spot{': ' + want if want else ''}"[:500]
            await vp.take_message(call_id, vp.MessageIn(name=body.name, text=text))
            return {
                "messageTaken": True,
                "say": "A message was taken for the owner, who will call back about a "
                "spot. Nobody is on a waitlist yet: do not promise one.",
            }
        if body.action == "message":
            if not await vp.callers_own_words(call_id, body.text):
                return {
                    "taken": False,
                    "say": "Not taken: a message is what the caller asked to pass on to the owner, in their own "
                    "words. Answer the caller in your reply; take a message only when they ask to leave one.",
                }
            return await vp.take_message(
                call_id, vp.MessageIn(name=body.name, text=body.text, callbackNumber=body.callback_number)
            )
        try:
            if body.action == "book":
                return await vp.book(call_id, vp.BookIn(start=body.start, service=body.service, name=body.name))
            new = body.new_start if body.action == "move" else None
            return await vp.change_booking(
                call_id, vp.ChangeIn(start=body.start, newStart=new, bookingId=body.booking_id)
            )
        finally:  # the caller's batch and thread see the change at once, not after the 60 s cache
            business_profile.forget(owner_id)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid_arguments") from exc


async def transfer_call(call: ToolCall) -> dict:
    """The Router's transfer_call (a phone caller asked for a person): one Lightbulb line in the call room marked
    `action: "transfer"`, the trigger's answer. The phone worker sees it on /events and runs /transfer and the SIP
    transfer itself (huddle-agent phone.py `hand_off`); nothing is transferred here. Only a caller's triggers, all
    from one call, in that call's own room while the line is live. `routedTo`: the engine marks the triggers answered."""
    from routers import voice_phone as vp

    owner_id, body, triggers = call.tenant_id, SimpleNamespace(trigger_ids=call.trigger_ids, **call.args), call.triggers
    refs = [(t or {}).get("ref") or {} for t in triggers]
    calls = {r.get("callId") if r.get("origin") == "phone_contact" else None for r in refs}
    call_id = next(iter(calls)) if len(calls) == 1 else None
    if not call_id or any(r.get("roomId") != call_id for r in refs):
        raise HTTPException(status_code=403, detail="not_a_caller")
    meta = await vp.messaging_repo.get_room(call_id) if call_id else None
    if not meta or meta.get("kind") != "call" or meta.get("ownerId") != owner_id:
        raise HTTPException(status_code=403, detail="not_a_caller")
    cfg = await vp.agent_config(owner_id)
    if vp.call_mode(cfg, meta.get("call") or {}) != "live":
        raise HTTPException(status_code=403, detail="not_allowed")  # a paused line only takes messages
    if not vp.handoff_to(cfg):  # closed or no number: refuse BEFORE the caller hears "hang on"
        await vp._patch_call(  # the same bookkeeping /transfer does, so /end raises the hand-off Question
            call_id, lambda c: {"handoffMissed": True, "steps": vp._step(c, "Asked for a person; nobody was available")}
        )
        raise HTTPException(status_code=403, detail={"code": "nobody_available"})
    message_id = await task_conversation.post_as_lightbulb(
        owner_id,
        call_id,
        text=vp.redact_cards(body.text),
        event_id=f"call-transfer:{body.trigger_ids[0]}",
        attrs={"action": "transfer"},
        remember=False,
    )
    return {"messageId": message_id, "routedTo": {"transfer": message_id}}


async def business_facts(call: ToolCall) -> dict:
    """A Worker's `business_facts`: its own workspace's Agent page (services, prices, hours, the Knowledge answers),
    without the abbreviated profile's character cap; later saved answers must remain readable."""
    from core.voice.phone import business_profile

    owner_id = call.tenant_id
    return {"facts": await business_profile.facts_text(owner_id)}
