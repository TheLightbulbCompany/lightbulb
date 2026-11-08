"""The phone receptionist (voice-agents spec §4.1b, §4.3; founder ruling 8 Nov 2026, "Phone, too": the phone joins
the one-Router design as ears and a mouth, shape 1: talker plus delegated brain,
super-agent-design/evidence/voice/voice-brain-architecture-research.md pattern 1). One realtime session per inbound
call (PHONE_ENGINE=gemini: Gemini Live, the flag-only fallback).

Dispatched by a LiveKit SIP dispatch rule: in prod to the one deployed agent, `lightbulb-huddle` (agent.py
routes `phone-` rooms here); on a dev Mac to its own name (`lightbulb-phone-dev`). It opens with the
disclosure (its name, an AI assistant, recorded). The talker's one tool, `delegate`, posts each caller request, with
the call's turns since the last delegation, to the call's room as the caller's line (`/turn`): the Router decides it
with the caller's scope and the Business profile, and its Front desk threads book, move, cancel and take messages
as the caller. The Router's and the Front desk's messages come back through `/events` and are said once, adding
nothing (mouth.py). A wait gets a holding line at once, another at HOLD_S, an offer to take a message at OFFER_S.
The talker still owns the call itself: a hand-off to a person (`delegate(kind="person")`) and a message it took
(`kind="message"`) go to the existing /transfer and /message routes (apps/backend/routers/voice_phone.py), which
also keep the call record.
Run: `AGENT_MODULE=huddle_agent.phone uv run python -m huddle_agent.phone dev`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Literal

import httpx
from livekit import api
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    cli,
    function_tool,
    room_io,
)
from livekit.plugins import google, noise_cancellation, openai
from openai.types.beta.realtime.session import TurnDetection

from huddle_agent import end_after, usage_log
from huddle_agent.backend import BackendError
from huddle_agent.metrics import emit_metric
from huddle_agent.mouth import Mouth, Talker

logger = logging.getLogger("phone")
# One voice vendor (founder, 7 Nov 2026): OpenAI. "gemini" stays a flag-only fallback until it is deleted.
OPENAI = os.environ.get("PHONE_ENGINE", "openai") != "gemini"
# gpt-realtime-mini is deprecated; 2.1-mini is its price ($10/$20 per M audio tokens) with tools and 128k context
MODEL = os.environ.get("PHONE_MODEL", "gpt-realtime-2.1-mini" if OPENAI else "gemini-3.8-live")
VOICE = os.environ.get("PHONE_VOICE", "marin" if OPENAI else "Kore")
AWAY_S = float(
    os.environ.get("PHONE_AWAY_S", "15")
)  # silence before "are you still there?", and again before hanging up
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

HOLD_S = float(os.environ.get("PHONE_HOLD_S", 8))  # no answer yet: one more holding line
OFFER_S = float(os.environ.get("PHONE_OFFER_S", 20))  # still none: offer to take a message
WAIT_S = 90.0  # a delegation's answer may take this long (a Front desk thread: 69-95 s on dev, 8 Nov)
POLL_S = 1.0  # how often the call asks what Lightbulb posted in its room
HOLD_AGAIN = (
    'The team is still checking. Say one short, natural holding line, such as "Thanks for waiting, still '
    'checking on that." Say nothing else and add no facts.'
)
OFFER_MESSAGE = (
    "The answer is taking a while. In one short sentence, offer to take a message so the team can call them back, "
    "or they can keep holding. Add no facts."
)

# Server-side turns on silence (developers.openai.com/api/docs/guides/realtime-vad): talking over the voice stops it.
SERVER_TURNS = TurnDetection(type="server_vad", silence_duration_ms=500, create_response=True, interrupt_response=True)

# The opening is the model's own words (founder, 8 Nov): it must name the assistant, say it is an AI assistant and
# that the call is recorded. The owner's greeting is its style, not a script. DisclosureGuard checks the result.
INSTRUCTIONS = """You are {assistant}, the AI receptionist on the phone for {business}. You speak first. Your opening
line, in your own words, must say all three: your name, {assistant}; that you are an AI assistant; and that this call
is recorded. It is your very first words: no preamble before it. Take its tone from the owner's greeting, which is a guide, not a script: "{greeting}" Greet once and
never again; after it, answer what the caller says. If you are told the disclosure was missed, say the one disclosure
sentence exactly as given, then continue with the caller. Speak briefly and warmly, one or two sentences at a time,
like a good front desk. Today is {today}.
{caller}You own only the conversation; the front desk team behind you owns every fact, booking and decision.
- Greetings, thanks, "are you there?", "can you hear me?": answer yourself, in a few words.
- Everything else -- a question about the business, its hours, prices or services, a booking, a change or cancel,
  a yes or a no -- call delegate with what the caller said, in their own words; a short holding line is said for
  you. Never answer it yourself, never guess a price, time, opening or policy, never say anything is booked, moved
  or done.
- The caller asks for a person: call delegate with kind "person".
- The caller wants to leave a message: once you have it (and their name), call delegate with kind "message" and
  the message as request.
- When a message from the team arrives, say it as you are told, adding nothing.
{can}Never share anything about other customers."""

DELEGATE_DOC = """Hand the caller's request to the front desk team, who answer it on this call: every question, booking,
change, cancel, yes or no. `request` is what they said, in their own words. It returns at once and a short holding
line is said for you: say nothing more; never answer it yourself, the answer arrives later.
kind "person": the caller asked for a person; they are transferred when someone is available.
kind "message": the caller's message for the team (request is the message; name if they gave it)."""


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
    return session.generate_reply(
        instructions=f'The disclosure was missed. Say exactly this, word for word, and nothing else: "{line}" '
        "This is a one-time line: do not say it again later in the call.",
        tool_choice="none",  # the compliance correction is spoken, never a delegate call
    )


def line(session, instructions: str):
    """A phone-only line said from instructions (still there, goodbye, a holding or message offer), tools off: with
    tools on, the model calls delegate instead of speaking it."""
    return session.generate_reply(instructions=instructions, tool_choice="none")


def greet(session) -> None:
    """The opening turn: the model speaks first, from the prompt (docs.livekit.io/agents/build/audio, "generate_reply"
    greeting). No per-response instructions: on Gemini they land in the history as a turn and re-greet (7 Nov).
    Not interruptible, as in LiveKit's greeting example (`allow_interruptions=False`): a caller's "hi" over it cut the
    disclosure off (8 Nov evals). Gemini's server-side turns ignore the flag (LiveKit logs a warning). Tools off: the
    opening is spoken, never a delegate call."""
    return session.generate_reply(allow_interruptions=False, tool_choice="none")


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
        sent = time.monotonic()
        r = await self._http.post(
            path
            if path.startswith("/")
            else f"/internal/voice/calls/{self.call_id}/{path}",
            json=body or {},
        )
        ms = int((time.monotonic() - sent) * 1000)  # a tool turn's backend share (no call content logged)
        if r.is_success:
            logger.info("phone %s: %s -> %s in %d ms", self.call_id, path, r.status_code, ms)
            return r.json()
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        logger.info("phone %s: %s -> %s in %d ms %s", self.call_id, path, r.status_code, ms, str(detail)[:200])
        return {
            "error": (detail or {}).get("code", f"http_{r.status_code}")
            if isinstance(detail, dict)
            else f"http_{r.status_code}"
        }


class Hold:
    """The wait after a delegation: `say(HOLD_AGAIN)` at HOLD_S and `say(OFFER_MESSAGE)` at OFFER_S unless the team
    speaks first (`answered`). `waiting`: the last delegation is under WAIT_S old, so a quiet caller is holding, not
    gone (the Router's ack may come before a Front desk thread's answer); `left()` is how long until it is not."""

    def __init__(self, say, hold_s: float = HOLD_S, offer_s: float = OFFER_S) -> None:
        self._say, self._at = say, ((hold_s, HOLD_AGAIN), (offer_s, OFFER_MESSAGE))
        self._timers: list[asyncio.TimerHandle] = []
        self._since: float | None = None

    def start(self) -> None:
        self.answered()
        loop = asyncio.get_running_loop()
        self._since = time.monotonic()
        self._timers = [loop.call_later(s, self._say, line) for s, line in self._at]

    def answered(self) -> None:
        for t in self._timers:
            t.cancel()
        self._timers = []

    def left(self) -> float:
        return 0.0 if self._since is None else max(0.0, WAIT_S - (time.monotonic() - self._since))

    @property
    def waiting(self) -> bool:
        return self.left() > 0


class Turns:
    """Each delegated request posted to the call's room in order (`/turn`, n numbers them: a retry is the same
    message). A failed post is logged; the hold's message offer still comes."""

    def __init__(self, backend: Backend) -> None:
        self._b, self._n = backend, 0
        self._last: asyncio.Task | None = None

    def add(self, text: str) -> None:
        self._last = asyncio.create_task(self._post(self._last, self._n, text[:4000]))
        self._n += 1

    async def _post(self, prev: asyncio.Task | None, n: int, text: str) -> None:
        if prev:
            await asyncio.gather(prev, return_exceptions=True)  # in the order said
        out = await self._b.post("turn", {"n": n, "text": text})
        if out.get("error"):
            logger.warning("phone %s: line %d not posted (%s)", self._b.call_id, n, out["error"])

    async def flush(self) -> None:
        if self._last:
            await asyncio.gather(self._last, return_exceptions=True)


class Receptionist(Talker):
    """The talker: no tool but `delegate`. Talker (mouth.py) holds its rules in code: an undelegated answer is cut
    and delegated, a result that adds a number or name is said again word for word."""

    where = "phone"

    def __init__(self, backend: Backend, info: dict, transfer, hold: Hold | None = None) -> None:
        super().__init__(INSTRUCTIONS.format(**{
            "assistant": info["assistantName"], "business": info["businessName"], "today": info["today"],
            "greeting": info["greeting"],
            "caller": f"The caller is {info['callerName']}, a returning client.\n" if info.get("callerName") else "",
            "can": (
                'This line is not taking bookings or questions right now: you can only take a message (delegate with '
                'kind "message"), and the owner will call back.\n'
                if info.get("mode") == "message_only" else ""
            ),
        }))  # fmt: skip
        self._b, self._transfer, self._live = backend, transfer, info.get("mode") != "message_only"
        self.allow = tuple(n for n in (info["assistantName"], info["businessName"], info.get("callerName")) if n)
        self.turns, self.hold = Turns(backend), hold
        self.did: list[str] = []  # what the call did itself, the record's summary (the Front desk adds its steps)

    @function_tool(description=DELEGATE_DOC)
    async def delegate(
        self, request: str, kind: Literal["ask", "person", "message"] = "ask", name: str = ""
    ) -> dict:
        if kind != "ask":
            self.asked = True  # the talker's reply to a hand-off or a message is not an undelegated answer
            if kind == "person":
                return await self._hand_off()
            self.did.append(f"Message: {request[:120]}")  # the existing take-message path: always open
            return await self._b.post("message", {"text": request, "name": name, "callbackNumber": ""})
        if not self._live:
            self.asked = True
            return {"error": "not_allowed", "say": "offer to take a message"}
        return self.tool_delegate(request)

    def post(self, request: str) -> None:
        """Talker's send: a task of its own, so an interruption cancels the talker's reply, never the post."""
        if not self._live:
            return  # a paused line only takes messages
        self.turns.add(self.context.take(request))
        if self.hold:
            self.hold.start()

    async def _hand_off(self) -> dict:
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

    if info.get("forwardTo"):  # the month's minutes are used up and the owner chose forwarding
        try:
            await transfer(info["forwardTo"])
        except Exception:  # noqa: BLE001 -- a SIP refusal: answer rather than leave the caller in silence
            logger.warning("phone %s: run-out forward failed", backend.call_id, exc_info=True)
        else:  # only now does the record say forwarded; a failed forward is an ordinary answered call
            await backend.post("end", {"durationSec": 0, "forwarded": True})
            ctx.shutdown("minutes used up: forwarded")
            return
    hold_lines: list = []  # the hold's say, bound once the session exists
    hold = Hold(lambda text: hold_lines[0](text) if hold_lines else None)
    agent = Receptionist(backend, info, transfer, hold)
    session = AgentSession(
        user_away_timeout=AWAY_S,
        **(
            # vad=None (as owner.py): user_state comes from the realtime model's own speech_started/stopped, so
            # speaking -> listening is the end of the turn it answers, before its transcript (Talker.turn_ended
            # depends on that order). server_vad, not semantic_vad: the plugin's semantic_vad took 5-7 s to answer
            # (book eval, 7 Nov). Gemini keeps its own turn-taking.
            dict(llm=openai.realtime.RealtimeModel(model=MODEL, voice=VOICE, turn_detection=SERVER_TURNS), vad=None)
            if OPENAI
            else dict(llm=google.realtime.RealtimeModel(model=MODEL, voice=VOICE, api_version="v1beta"))
        ),
    )
    usage_log(session, f"phone {backend.call_id}")
    items: list[dict] = []
    t0 = time.time()
    guard = DisclosureGuard(
        info["assistantName"],
        lambda text: agent.unchecked(say_disclosure(session, text)),
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
            if ev.item.role == "user":
                agent.heard("Caller", text)
            elif ev.item.role == "assistant" and not ev.item.interrupted:
                agent.context.add(info["assistantName"], text)

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
        """No model left (7 Nov: Gemini's 402 left callers in silence): a callback card in the owner's Needs you, a
        metric, and the line is closed."""
        logger.error("phone %s: voice model failed (%s)", backend.call_id, getattr(err, "error", err))
        emit_metric("phone.voice.failed", 1, "Count", {"engine": "openai" if OPENAI else "gemini"})
        await backend.post("unknown", {"question": "The assistant's voice failed during this call: call them back."})
        agent.did.append("Voice failed; callback needed")
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
            bye = line(session, SILENT_BYE)
            agent.unchecked(bye)
            await asyncio.wait_for(bye, 10)
        except Exception:  # noqa: BLE001 -- a lost goodbye must not keep the line open
            logger.warning(
                "phone %s: silent goodbye failed", backend.call_id, exc_info=True
            )
        silent_end.append(True)
        await hang_up("caller silent")

    def still_away() -> None:
        if session.user_state == "away":
            _away(type("Ev", (), {"new_state": "away"}))

    @session.on("user_state_changed")
    def _away(ev) -> None:
        if ev.new_state == "away" and hold.waiting:  # a caller holding for an answer is not gone: ask after the wait
            asyncio.get_running_loop().call_later(hold.left() + 0.1, still_away)
            return
        if ev.new_state == "away" and not silent:
            agent.unchecked(line(session, STILL_THERE))
            silent.append(asyncio.create_task(hang_up_silent()))
        elif ev.new_state != "away" and silent:
            silent.pop().cancel()

    session.on("user_state_changed", lambda ev: ev.old_state == "speaking" and agent.turn_ended())

    limit = asyncio.create_task(end_after(CALL_MAX_S, session, hang_up, agent.unchecked))

    def idle() -> bool:
        return session.user_state != "speaking" and session.agent_state not in ("speaking", "thinking")

    def hold_line(text: str) -> None:
        if session.user_state != "speaking":  # the caller talking over the wait is answered, not held
            agent.unchecked(line(session, text))  # a holding line, never an answer

    hold_lines.append(hold_line)
    async def fetch(after: int, _handled: int) -> dict:
        out = await backend.post("events", {"after": after})
        if out.get("error"):
            raise BackendError(0, out["error"], None)
        return out

    async def speak_loop(mouth: Mouth) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            await mouth.poll()
            if mouth.pending:
                hold.answered()  # the team spoke: no more holding lines for this ask
            mouth.speak(agent.say, ready=idle(), live=True)

    reports = asyncio.create_task(speak_loop(Mouth(fetch, int(info.get("after") or 0))))

    async def on_end(_reason: str = "") -> None:
        limit.cancel()
        reports.cancel()
        hold.answered()
        await agent.turns.flush()  # a line said just before hang-up still reaches the room
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
    agent.unchecked(greet(session))  # the opening: DisclosureGuard judges it


if __name__ == "__main__":
    cli.run_app(server)
