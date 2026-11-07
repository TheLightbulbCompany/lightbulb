"""The phone receptionist (voice-agents spec §4.1b, §4.3): one OpenAI realtime session per inbound call
(PHONE_ENGINE=gemini: Gemini Live, the flag-only fallback).

Dispatched by a LiveKit SIP dispatch rule: in prod to the one deployed agent, `lightbulb-huddle` (agent.py
routes `phone-` rooms here); on a dev Mac to its own name (`lightbulb-phone-dev`). It opens with the
disclosure (its name, an AI assistant, recorded), then listens and answers; bookings, messages, transfer and open-ended work go to the
backend's /internal/voice routes (apps/backend/routers/voice_phone.py), which also keep the call record.
Run: `AGENT_MODULE=huddle_agent.phone uv run python -m huddle_agent.phone dev`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx
from livekit import api, rtc
from livekit.agents import (
    Agent,
    inference,
    AgentServer,
    AgentSession,
    JobContext,
    cli,
    function_tool,
    room_io,
    utils,
)
from livekit.plugins import google, noise_cancellation, openai

from huddle_agent import end_after, usage_log
from huddle_agent.huddle import emit_metric

logger = logging.getLogger("phone")
# One voice vendor (founder, 7 Nov 2026): OpenAI. "gemini" stays a flag-only fallback until it is deleted.
OPENAI = os.environ.get("PHONE_ENGINE", "openai") != "gemini"
# gpt-realtime-mini is deprecated; 2.1-mini is its price ($10/$20 per M audio tokens) with tools and 128k context
MODEL = os.environ.get("PHONE_MODEL", "gpt-realtime-2.1-mini" if OPENAI else "gemini-3.8-live")
VOICE = os.environ.get("PHONE_VOICE", "marin" if OPENAI else "Kore")
AWAY_S = float(
    os.environ.get("PHONE_AWAY_S", "15")
)  # silence before "are you still there?", and again before hanging up
# The voice model is gone mid-call (7 Nov: Gemini's 402 left callers in silence). LiveKit's documented fallback for an
# unrecoverable error: say() with pre-recorded audio, which needs no model (docs.livekit.io/agents/build/events).
SORRY = "I'm sorry, I'm having trouble on this line right now. I've let the team know, and someone will call you back."
SORRY_WAV = os.path.join(os.path.dirname(__file__), "sorry.wav")  # SORRY, spoken
STILL_THERE = 'The line has gone quiet. Ask once, briefly: "Are you still there?"'
SILENT_BYE = "Nobody is answering. In one short sentence, say you'll hang up now and goodbye; don't ask anything."
CALL_MAX_S = float(
    os.environ.get("PHONE_MAX_S", 20 * 60)
)  # cost cap: a call ends after 20 minutes
# One warm process is plenty for a line; a busy host gets a longer start and its own load ceiling.
server = AgentServer(
    num_idle_processes=1,
    initialize_process_timeout=60,
    load_threshold=float(os.environ.get("PHONE_LOAD_THRESHOLD", "0.7")),
)

# The opening is the model's own words (founder, 8 Nov): it must name the assistant, say it is an AI assistant and
# that the call is recorded. The owner's greeting is its style, not a script. DisclosureGuard checks the result.
INSTRUCTIONS = """You are {assistant}, the AI receptionist on the phone for {business}. You speak first. Your opening
line, in your own words, must say all three: your name, {assistant}; that you are an AI assistant; and that this call
is recorded. Take its tone from the owner's greeting, which is a guide, not a script: "{greeting}" Greet once and
never again; after it, answer what the caller says. If you are told the disclosure was missed, say the one disclosure
sentence exactly as given, then continue with the caller. Speak briefly and warmly, one or two sentences at a time,
like a good front desk. Today is {today}.
{caller}What you may tell callers about the business: {facts}
{can}

Times you send to tools are local "YYYY-MM-DDTHH:MM". Offer only slots check_availability returned: offer up to
three open slots, then ask which they prefer. Before calling book or reschedule_or_cancel (to move or cancel), read
back the service, day, time and name, and wait for the caller to say yes. If you don't know something,
call didnt_know with the question, say so, and offer to take a message; never invent prices or policies. If a tool
answers not_allowed, don't retry it: offer to take a message. Never share anything about other customers. You
can't take payments, change prices, or send email; for those take a message for the owner.
{instructions}"""


def e164(number: str) -> str:
    """LiveKit Phone Numbers send the dialed number as digits ("14842079294"); line rows key on E.164."""
    n = number.strip()
    return "+" + n if n.isdigit() else n


def clean_transcript(items: list[dict]) -> list[dict]:
    """The call's transcript for the record: final lines only (an interrupted, cut-off reply is dropped), in spoken
    order (`t`, seconds since the call started), consecutive repeats once. Only what was transcribed; nothing added."""
    out: list[dict] = []
    for i in sorted(items, key=lambda i: i["t"]):
        who = {"user": "caller", "assistant": "assistant"}.get(i["role"])
        if not who or i.get("interrupted") or not i["text"].strip():
            continue
        line = {"who": who, "text": i["text"].strip(), "t": round(max(i["t"], 0.0), 1)}
        if out and (out[-1]["who"], out[-1]["text"]) == (who, line["text"]):
            continue
        out.append(line)
    return out


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def discloses(text: str, assistant: str) -> bool:
    """The opening as said: it names the assistant and says "AI assistant" and "recorded", as whole words ("Ava" isn't in "available"), never denied
    ("not recorded", "isn't being recorded")."""
    t = f" {_norm(text)} "
    # "isn't" normalizes to "isn t"; up to three words between the denial and "recorded" ("not going to be recorded")
    if re.search(
        r" (not|never|no|nothing|t)( \w+){0,3} recorded |\bno part of\b.*\brecorded\b",
        t,
    ):
        return False
    return all(f" {_norm(w)} " in t for w in (assistant, "AI assistant", "recorded"))


def disclosure_of(items: list[dict], assistant: str) -> str:
    """`ok` / `missing` from the first assistant line spoken before the caller's first line (a cut-off greeting
    counts: it wasn't all said); `unknown` when none was transcribed before the caller spoke."""
    for i in sorted(items, key=lambda i: i["t"]):
        if i["role"] == "user":
            return "unknown"
        if i["role"] == "assistant":
            ok = not i.get("interrupted") and discloses(i["text"], assistant)
            return "ok" if ok else "missing"
    return "unknown"


class DisclosureGuard:
    """Judges the greeting once it has finished: after the agent's first speaking -> listening change, on final items
    (the agent adds an assistant item only after its audio played, `interrupted` set when cut off). A failed first
    line makes `say` speak the disclosure sentence once. PHONE_TEST_FAIL_DISCLOSURE=1 fails it (scripted runs)."""

    def __init__(self, assistant: str, say) -> None:
        self.assistant, self._say = assistant, say
        self.line = f"I'm {assistant}, an AI assistant, and this call is recorded."
        self.corrected, self._done, self._greeted, self._spoke = (
            False,
            False,
            False,
            False,
        )

    def state(self, old: str, new: str) -> None:
        if new == "speaking":
            self._spoke = True
        elif self._spoke and old == "speaking":
            self._greeted = True  # the greeting finished (or was cut off) playing

    def seen(self, items: list[dict]) -> None:
        if self._done or not self._greeted:
            return
        if os.environ.get("PHONE_TEST_FAIL_DISCLOSURE") == "1":
            verdict = "missing"
        else:
            verdict = disclosure_of(items, self.assistant)
            if verdict == "unknown" and not any(i["role"] == "user" for i in items):
                return  # the greeting's line may still be on its way; judge when it (or the caller) lands
        self._done = True
        if verdict == "missing":
            self.corrected = True
            self._say(self.line)


def say_disclosure(session, line: str) -> None:
    """One-shot reply with that exact line, in the wording that made Gemini speak verbatim (the greeting tests).
    Gemini Live can't take a turn back out of its history, so the instruction says it is one-time."""
    logger.info("phone: first line lacked the disclosure; saying it once")
    session.generate_reply(
        instructions=f'The disclosure was missed. Say exactly this, word for word, and nothing else: "{line}" '
        "This is a one-time line: do not say it again later in the call."
    )


def greet(session) -> None:
    """The opening turn: the model speaks first, from the prompt (docs.livekit.io/agents/build/audio, "generate_reply"
    greeting). No per-response instructions: on Gemini they land in the history as a turn and re-greet (7 Nov)."""
    session.generate_reply()


class Backend:
    def __init__(self) -> None:
        base = (
            os.environ["LIGHTBULB_API_URL"].rstrip("/").removesuffix("/api/v1")
            + "/api/v1"
        )
        self._http = httpx.AsyncClient(
            base_url=base,
            headers={
                "X-Lightbulb-Huddle-Secret": os.environ["LIGHTBULB_HUDDLE_SECRET"]
            },
            timeout=10,
        )
        self.call_id = ""

    async def post(self, path: str, body: dict | None = None) -> dict:
        r = await self._http.post(
            path
            if path.startswith("/")
            else f"/internal/voice/calls/{self.call_id}/{path}",
            json=body or {},
        )
        if r.is_success:
            return r.json()
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        return {
            "error": (detail or {}).get("code", f"http_{r.status_code}")
            if isinstance(detail, dict)
            else f"http_{r.status_code}"
        }


class Receptionist(Agent):
    def __init__(self, backend: Backend, info: dict, transfer) -> None:
        can = info.get("can") or {}
        off = [
            w
            for k, w in (
                ("book", "book appointments"),
                ("move", "move or cancel appointments"),
            )
            if not can.get(k)
        ]
        super().__init__(instructions=INSTRUCTIONS.format(**{
            "assistant": info["assistantName"], "business": info["businessName"], "today": info["today"],
            "greeting": info["greeting"],
            "facts": info.get("facts") or "nothing; take a message for any question about the business.",
            "caller": f"The caller is {info['callerName']}, a returning client.\n" if info.get("callerName") else "",
            "can": f"On this line you can't {' or '.join(off)}." if off else "",
            "instructions": f"The owner's instructions: {info['instructions']}" if info.get("instructions") else "",
        }))  # fmt: skip
        self._b, self._transfer = backend, transfer
        self.did: list[str] = []  # what the call did, the record's summary

    @function_tool
    async def check_availability(self, date: str, service: str = "") -> dict:
        """Free appointment start times on a date for a service. date: "YYYY-MM-DD" (local)."""
        return await self._b.post("availability", {"date": date, "service": service})

    @function_tool
    async def book(self, start: str, service: str, name: str) -> dict:
        """Book the caller. start: local "YYYY-MM-DDTHH:MM" from check_availability; service; the caller's name."""
        out = await self._b.post(
            "book", {"start": start, "service": service, "name": name}
        )
        if out.get("booked"):
            self.did.append(f"Booked {service} {start} for {name}")
        return out

    @function_tool
    async def reschedule_or_cancel(
        self, start: str, new_start: str | None = None
    ) -> dict:
        """Move (new_start given) or cancel (new_start empty) one of THIS caller's bookings. Call my_bookings first
        if you don't know its start. Times: local "YYYY-MM-DDTHH:MM"."""
        out = await self._b.post(
            "change", {"start": start, "newStart": new_start or None}
        )
        if not out.get("error"):
            self.did.append(
                f"Moved {start} to {new_start}" if new_start else f"Cancelled {start}"
            )
        return out

    @function_tool
    async def my_bookings(self) -> dict:
        """The upcoming bookings made from this caller's phone number."""
        return await self._b.post("my-bookings")

    @function_tool
    async def take_message(
        self, text: str, name: str = "", callback_number: str = ""
    ) -> dict:
        """Leave a message for the owner (anything you can't answer or do)."""
        self.did.append(f"Message: {text[:120]}")
        return await self._b.post(
            "message", {"text": text, "name": name, "callbackNumber": callback_number}
        )

    @function_tool
    async def didnt_know(self, question: str) -> dict:
        """Note a caller's question you couldn't answer, for the owner."""
        return await self._b.post("unknown", {"question": question})

    @function_tool
    async def transfer_to_human(self) -> dict:
        """Hand the caller to a person at the business. If none is available, offer to take a message."""
        out = await self._b.post("transfer")
        if not out.get("transferTo"):
            return {
                "transferred": False,
                "reason": "nobody is available to take calls right now",
            }
        try:
            await self._transfer(out["transferTo"])
        except Exception:  # noqa: BLE001 -- a SIP refusal: the record says the hand-off was missed
            logger.warning("phone %s: transfer failed", self._b.call_id, exc_info=True)
            await self._b.post("transfer", {"failed": True})
            return {"transferred": False, "reason": "the transfer didn't go through"}
        self.did.append("Transferred to a person")
        return {"transferred": True}

    @function_tool
    async def start_work(self, title: str, brief: str) -> dict:
        """Hand an open-ended request to the team to do after the call (a quote, a callback that needs research).
        Tell the caller the team will follow up; don't wait for it."""
        self.did.append(f"Follow-up: {title}")
        return await self._b.post("start-work", {"title": title, "brief": brief})


# Standalone (`python -m huddle_agent.phone dev`, a dev line): its own dispatch name; this kwarg wins over
# LIVEKIT_AGENT_NAME in livekit-agents 1.8.11. Deployed, the huddle worker's entrypoint routes `phone-` rooms here.
@server.rtc_session(
    agent_name=os.environ.get("PHONE_AGENT_NAME", "lightbulb-phone-dev")
)
async def entrypoint(ctx: JobContext) -> None:
    started = time.monotonic()
    await ctx.connect()
    try:
        caller = await asyncio.wait_for(ctx.wait_for_participant(), AWAY_S * 2)
    except TimeoutError:  # 8 Nov sims: a job that started after the caller left sat in the room for 12 minutes
        logger.info(
            "phone: nobody joined job %s; leaving", ctx.job.id
        )  # the room name holds the caller's number
        ctx.shutdown("no caller")
        return
    meta = json.loads(
        ctx.job.metadata or "{}"
    )  # a test dispatch (lk dispatch) names the numbers itself
    called = e164(
        caller.attributes.get("sip.trunkPhoneNumber") or meta.get("calledNumber") or ""
    )
    caller_number = e164(
        caller.attributes.get("sip.phoneNumber") or meta.get("callerNumber") or ""
    )
    backend = Backend()
    info = await backend.post(
        "/internal/voice/calls",
        {
            "calledNumber": called,
            "callerNumber": caller_number,
            "livekitRoom": ctx.room.name,
        },
    )
    if info.get("error"):
        logger.error("phone: call refused (%s)", info["error"])
        return
    backend.call_id = info["callId"]
    logger.debug("phone: call %s started", backend.call_id)

    async def transfer(to: str) -> None:
        await ctx.api.sip.transfer_sip_participant(
            api.TransferSIPParticipantRequest(
                room_name=ctx.room.name,
                participant_identity=caller.identity,
                transfer_to=f"tel:{to}",
            )
        )

    agent = Receptionist(backend, info, transfer)
    session = AgentSession(
        user_away_timeout=AWAY_S,
        **(
            # Client-side turns (docs.livekit.io/agents/logic/turns): LiveKit's VAD and audio end-of-turn model
            # decide when the caller is done, and adaptive interruption ignores "mm-hm". The plugin's default
            # semantic_vad took 5-7 s to answer (book eval, 7 Nov). Gemini keeps its own turn-taking.
            dict(
                llm=openai.realtime.RealtimeModel(model=MODEL, voice=VOICE, turn_detection=None),
                vad=inference.VAD(),
                turn_handling={"turn_detection": inference.TurnDetector(), "interruption": {"mode": "adaptive"}},
            )
            if OPENAI
            else dict(llm=google.realtime.RealtimeModel(model=MODEL, voice=VOICE, api_version="v1beta"))
        ),
    )
    usage_log(session, f"phone {backend.call_id}")
    items: list[dict] = []
    t0 = time.time()
    guard = DisclosureGuard(
        info["assistantName"],
        lambda line: say_disclosure(session, line),
    )

    @session.on("conversation_item_added")
    def _item(ev) -> None:
        text = getattr(ev.item, "text_content", None)
        if text:
            at = (ev.item.metrics or {}).get(
                "started_speaking_at"
            ) or ev.item.created_at
            items.append(
                {
                    "role": ev.item.role,
                    "text": text,
                    "t": at - t0,
                    "interrupted": ev.item.interrupted,
                }
            )
            guard.seen(items)

    @session.on("agent_state_changed")
    def _state(ev) -> None:
        guard.state(ev.old_state, ev.new_state)
        guard.seen(items)

    failed: list[str] = []

    @session.on("error")
    def _error(ev) -> None:
        if getattr(ev.error, "recoverable", True) or failed:
            return  # a retry is under way; a second failure is the same one
        failed.append(type(ev.source).__name__)
        asyncio.create_task(voice_failed(ev.error))

    async def voice_failed(err) -> None:
        """No model left: the apology from a file, a callback card in the owner's Needs you, a metric, and the line
        is closed (a silent line bills nothing but leaves the caller waiting)."""
        logger.error("phone %s: voice model failed (%s)", backend.call_id, getattr(err, "error", err))
        emit_metric("phone.voice.failed", 1, "Count", {"engine": "openai" if OPENAI else "gemini"})
        await backend.post("unknown", {"question": "The assistant's voice failed during this call: call them back."})
        agent.did.append("Voice failed; callback needed")
        try:
            handle = session.say(SORRY, audio=utils.audio.audio_frames_from_file(SORRY_WAV, 24000),
                                 allow_interruptions=False, add_to_chat_ctx=False)  # fmt: skip
            await asyncio.wait_for(handle, 15)
        except Exception:  # noqa: BLE001 -- the card and the hang-up must happen anyway
            logger.warning("phone %s: apology not played", backend.call_id, exc_info=True)
        await hang_up("voice failed")

    async def hang_up(reason: str = "call max length") -> None:
        await ctx.delete_room()  # disconnects the SIP caller too
        ctx.shutdown(reason)

    silent: list[asyncio.Task] = []
    silent_end: list[bool] = []

    async def hang_up_silent() -> None:
        """Still nobody after "are you still there?": a goodbye, then the line is closed (8 Nov sims: a silent line
        stayed open, billing Gemini, until the 20-minute cap)."""
        await asyncio.sleep(AWAY_S)
        try:
            await asyncio.wait_for(session.generate_reply(instructions=SILENT_BYE), 10)
        except Exception:  # noqa: BLE001 -- a lost goodbye must not keep the line open
            logger.warning(
                "phone %s: silent goodbye failed", backend.call_id, exc_info=True
            )
        silent_end.append(True)
        await hang_up("caller silent")

    @session.on("user_state_changed")
    def _away(ev) -> None:
        if ev.new_state == "away" and not silent:
            session.generate_reply(instructions=STILL_THERE)
            silent.append(asyncio.create_task(hang_up_silent()))
        elif ev.new_state != "away" and silent:
            silent.pop().cancel()

    limit = asyncio.create_task(end_after(CALL_MAX_S, session, hang_up))

    async def on_end(_reason: str = "") -> None:
        limit.cancel()
        for task in silent:
            task.cancel()
        out = await backend.post(
            "end",
            {
                "durationSec": int(time.monotonic() - started),
                "summary": "; ".join(agent.did),
                "transcript": clean_transcript(items),
                "disclosure": disclosure_of(items, info["assistantName"]),
                "disclosureRepeated": guard.corrected,
                "silent": bool(silent_end),
            },
        )
        logger.info(
            "phone: call %s ended outcome=%s", backend.call_id, out.get("outcome", out)
        )

    ctx.add_shutdown_callback(on_end)
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=caller.identity,
            close_on_disconnect=True,
            # noise cancellation tuned for SIP audio, in the agent and nowhere else on the call (one denoiser:
            # docs.livekit.io/transport/media/enhanced-noise-cancellation)
            audio_input=room_io.AudioInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        ),
        record=True,  # the greeting says the call is recorded
    )
    greet(session)


if __name__ == "__main__":
    cli.run_app(server)
