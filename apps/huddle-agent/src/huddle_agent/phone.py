"""The phone receptionist (voice-agents spec §4.1b, §4.3): one Gemini Live session per inbound call.

Dispatched by a LiveKit SIP dispatch rule: in prod to the one deployed agent, `lightbulb-huddle` (agent.py
routes `phone-` rooms here); on a dev Mac to its own name (`lightbulb-phone-dev`). It greets with the
compliance line, then listens and answers; bookings, messages, transfer and open-ended work go to the
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
from google import genai
from google.genai import types
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    cli,
    function_tool,
    room_io,
)
from livekit.plugins import google

from huddle_agent import end_after

logger = logging.getLogger("phone")
MODEL = os.environ.get("PHONE_MODEL", "gemini-3.8-live")  # 10-voice-models.md §5
VOICE = os.environ.get(
    "PHONE_VOICE", "Kore"
)  # one of Gemini's prebuilt voices; the greeting's TTS uses the same
TTS_MODEL = os.environ.get(
    "PHONE_TTS_MODEL", "gemini-3.1-flash-tts-preview"
)  # streams: first audio in ~0.8 s
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

# TTS slower than this is silence on the line: the Live model greets instead (first audio), or it is cut off (a stall)
FIRST_AUDIO_S, STALL_S = 3.0, 2.0
INSTRUCTIONS = """You are {assistant}, the AI receptionist on the phone for {business}. Your greeting is exactly, word
for word: "{greeting}" If it has already been played to the caller, don't say it again; otherwise your first words
are exactly that greeting. Say it once and never repeat it; after it, answer what the caller says. If you are told
the disclosure was missed, say the one disclosure sentence exactly as given, then continue with the caller. Speak briefly and warmly, one or two sentences at a time, like a good front desk. Today is {today}.
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


def discloses(text: str, greeting: str, assistant: str) -> bool:
    """The greeting as said: the greeting itself (case, punctuation, spacing aside), or a line that still names the
    assistant and says "AI assistant" and "recorded", as whole words ("Ava" isn't in "available"), never denied
    ("not recorded", "isn't being recorded")."""
    t = f" {_norm(text)} "
    # "isn't" normalizes to "isn t"; up to three words between the denial and "recorded" ("not going to be recorded")
    if re.search(
        r" (not|never|no|nothing|t)( \w+){0,3} recorded |\bno part of\b.*\brecorded\b",
        t,
    ):
        return False
    return t.strip() == _norm(greeting) or all(
        f" {_norm(w)} " in t for w in (assistant, "AI assistant", "recorded")
    )


def disclosure_of(items: list[dict], greeting: str, assistant: str) -> str:
    """`ok` / `missing` from the first assistant line spoken before the caller's first line (a cut-off greeting
    counts: it wasn't all said); `unknown` when none was transcribed before the caller spoke."""
    for i in sorted(items, key=lambda i: i["t"]):
        if i["role"] == "user":
            return "unknown"
        if i["role"] == "assistant":
            ok = not i.get("interrupted") and discloses(i["text"], greeting, assistant)
            return "ok" if ok else "missing"
    return "unknown"


class DisclosureGuard:
    """Judges the greeting once it has finished: after the agent's first speaking -> listening change, on final items
    (the agent adds an assistant item only after its audio played, `interrupted` set when cut off). A failed first
    line makes `say` speak the disclosure sentence once. PHONE_TEST_FAIL_DISCLOSURE=1 fails it (scripted runs)."""

    def __init__(self, greeting: str, assistant: str, say) -> None:
        self.greeting, self.assistant, self._say = greeting, assistant, say
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
            verdict = disclosure_of(items, self.greeting, self.assistant)
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


async def greeting_audio(text: str, key: str):
    """The greeting as Gemini TTS audio, streamed: the caller hears exactly this text, never a model's reply to it."""
    client = genai.Client(
        api_key=key, http_options={"api_version": "v1beta"}
    )  # v1 refuses speechConfig (7 Nov)
    voice = types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
    )
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=voice),
    )
    rest = b""
    async for chunk in await client.aio.models.generate_content_stream(
        model=TTS_MODEL, contents=text, config=config
    ):
        for part in [
            p
            for c in chunk.candidates or []
            if c.content
            for p in c.content.parts or []
        ]:
            if part.inline_data and part.inline_data.data:
                pcm, rest = rest + part.inline_data.data, b""
                if len(pcm) % 2:
                    pcm, rest = pcm[:-1], pcm[-1:]
                yield rtc.AudioFrame(
                    pcm, 24000, 1, len(pcm) // 2
                )  # 16-bit mono PCM at 24 kHz


async def greet(session, greeting: str, stream, first: asyncio.Task) -> None:
    """The compliance greeting, word for word, every call. An empty generate_reply() let gemini-3.8 make up its own
    first turn in 3 of 15 simulated calls (8 Nov: "Due to your audio input not being detected…", slots offered
    unasked, a transfer nobody asked for). So the greeting is TTS audio of the exact text, played by say(); the prompt
    says not to repeat a greeting already played. No first audio within FIRST_AUDIO_S: the Live model greets from the
    prompt via an empty generate_reply() (instructions passed there land in the Live history as a user turn and
    re-greet, 7 Nov). A stall of STALL_S mid-stream interrupts the say(): the record and DisclosureGuard then see a
    cut-off greeting (`missing`), and the guard says the disclosure."""
    try:
        head = await asyncio.wait_for(first, FIRST_AUDIO_S)
    except Exception:  # noqa: BLE001 -- no TTS in time: the Live model greets, checked by DisclosureGuard
        logger.warning(
            "phone: greeting TTS failed or slow; the Live model greets", exc_info=True
        )
        session.generate_reply()
        return
    handle = None

    async def audio():
        yield head
        while True:
            try:
                frame = await asyncio.wait_for(anext(stream), STALL_S)
            except StopAsyncIteration:
                return
            except Exception:  # noqa: BLE001 -- a stalled say() would count as played whole; cut it off instead
                logger.warning(
                    "phone: greeting TTS stream broke or stalled", exc_info=True
                )
                handle.interrupt(force=True)
                return
            yield frame

    handle = session.say(greeting, audio=audio())


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
    key = os.environ["GOOGLE_API_KEY"]
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=MODEL, voice=VOICE, api_key=key, api_version="v1beta"
        ),
        user_away_timeout=AWAY_S,
    )
    stream = greeting_audio(info["greeting"], key)
    first = asyncio.ensure_future(
        anext(stream)
    )  # the first audio overlaps the Live connect
    items: list[dict] = []
    t0 = time.time()
    guard = DisclosureGuard(
        info["greeting"],
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
                "disclosure": disclosure_of(
                    items, info["greeting"], info["assistantName"]
                ),
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
            participant_identity=caller.identity, close_on_disconnect=True
        ),
        record=True,  # the greeting says the call is recorded
    )
    await greet(session, info["greeting"], stream, first)


if __name__ == "__main__":
    cli.run_app(server)
