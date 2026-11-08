"""The phone receptionist (voice-agents spec §4.1b, §4.3) on GPT-Live (plan 2026-11-08-gpt-live-voice, Task 4;
founder 8 Nov 2026: "use their voice model with our deepseek router"). GPT-Live is the voice: it listens, speaks,
takes turns and decides when a caller's line needs the backend (client delegation, `live.start`). Every delegation
is the caller's line in the call's room (`/turn`, the caller principal: the call's open thread first, else the
Router with the Business profile, whose Front desk threads book, move and cancel as the caller). The room's agent
messages come back through `/events` as results. Our code says nothing of its own: it runs the call (SIP, the
disclosure check, the record, billing at hang-up, the run-out forward) and the two routes only the worker can do:
the SIP transfer (an events item of kind `transfer`) and a paused line's message (`/turn` refuses: `/message`).

Dispatched by a LiveKit SIP dispatch rule: in prod to the one deployed agent, `lightbulb-huddle` (agent.py
routes `phone-` rooms here); on a dev Mac to its own name (`lightbulb-phone-dev`).
Run: `AGENT_MODULE=huddle_agent.phone uv run python -m huddle_agent.phone dev`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import time

import httpx
from livekit import api
from livekit.agents import AgentServer, JobContext, cli, room_io
from livekit.plugins import noise_cancellation

from huddle_agent import GOODBYE, live
from huddle_agent.metrics import emit_metric
from huddle_agent.mouth import speakable

logger = logging.getLogger("phone")
AWAY_S = float(os.environ.get("PHONE_AWAY_S", "15"))  # silence before GPT-Live hears the line is quiet; again: hang up
CALL_MAX_S = float(os.environ.get("PHONE_MAX_S", 20 * 60))  # cost cap: a call ends after 20 minutes
BYE_S = 8.0  # the goodbye's time to play at the cost cap
WAIT_S = 90.0  # a pending delegation keeps a quiet line open this long (a Front desk thread: 69-95 s on dev, 8 Nov)
POLL_S = 1.0  # how often the call asks what Lightbulb posted in its room
# One warm process is plenty for a line; a busy host gets a longer start and its own load ceiling.
server = AgentServer(
    num_idle_processes=1,
    initialize_process_timeout=60,
    load_threshold=float(os.environ.get("PHONE_LOAD_THRESHOLD", "0.7")),
)

#: What the backend does for a caller (GPT-Live's `Backend tools`): the caller-scoped Front desk, nothing more.
TOOLS = [
    "Availability: check open times for the business's services.",
    "Bookings: book, move or cancel the caller's own appointment.",
    "Messages: take a message for the team.",
    "A person: transfer the caller to a person at the business.",
    "Business profile: answer questions about the business's services, prices, hours and policies.",
]
MESSAGE_ONLY_TOOLS = ["Messages: take a message for the team; the owner will call back."]

#: Said once after start, as the docs greet: the same instructions as the prompt's opening, as commentary with
#: delegation_id null (developers.openai.com/api/docs/guides/live-prompting, "Language and pronunciation").
OPENING = (
    "The call has just been answered. Speak first, in one short sentence: say your name, {assistant}; that you are "
    'an AI assistant; and that this call is recorded. Then ask how you can help, and listen. Take your tone from the '
    'owner\'s greeting, a guide, not a script: "{greeting}"'
)

PROMPT = """You are {assistant}, the calm, friendly AI receptionist on the phone for {business}.
Speak warmly and naturally, at an unhurried pace, one or two short sentences at a time, like a good front desk. Be
clear and direct, not overly cheerful. Speak only English, whatever language a line seems to be in.
If the caller is frustrated, acknowledge it briefly and focus on the next helpful step.
{caller}{paused}
Opening: your very first words on the call say your name, {assistant}; that you are an AI assistant; and that this
call is recorded. Then ask how you can help, and listen.

Backchannel policy: Use light backchannels. Acknowledge naturally without competing with the main response.

Interruption policy: Stop speaking when the caller interrupts. Listen to what they say. The one exception is the
opening: finish saying your name, that you are an AI assistant and that this call is recorded, then listen.

{delegation}

Names, dates and numbers: before a booking is made, say the name it goes under back to the caller and ask if you
have it right. If an important name, date, or number is unclear, ask about that part. Use the caller's correction.
Do not guess the missing value.

Wait for the backend's result before stating a price or a time, confirming a booking, or saying that a message was
taken or a transfer is under way. Never share anything about other customers."""

#: Facts the call tells GPT-Live (commentary with delegation_id null): GPT-Live chooses what to say.
QUIET = "The line has been quiet for a while. If the caller stays silent, the call will end."
NOBODY = "Nobody at the business can take the call right now. The team can take a message instead."
PAUSED = "The caller's message was passed on to the team; the owner will call back."


def prompt(info: dict) -> str:
    paused = info.get("mode") == "message_only"
    return PROMPT.format(
        assistant=info["assistantName"],
        business=info["businessName"],
        caller=f"The caller is {info['callerName']}, a returning client.\n" if info.get("callerName") else "",
        paused="This line is not taking bookings or questions right now: it only takes messages.\n" if paused else "",
        delegation=live.delegation_policy(MESSAGE_ONLY_TOOLS if paused else TOOLS),
    )


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
    """`ok` / `missing` from the first assistant line spoken before the caller's first line; `unknown` when none was
    transcribed before the caller spoke. A cut-off line is judged on what played (LiveKit keeps the synchronized
    transcript of an interrupted speech), so an opening cut after its disclosure is ok."""
    for i in sorted(items, key=lambda i: i["t"]):
        if i["role"] == "user":
            return "unknown"
        if i["role"] == "assistant":
            return "ok" if discloses(i["text"], assistant) else "missing"
    return "unknown"


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


class Ending:
    """The call ends once, at the first of: the caller hanging up (their SIP participant left), our own hang-up,
    or the job's shutdown. LiveKit only shuts the job down ~22 s after the caller leaves (8 Nov prod logs), which
    every call billed. `durationSec` runs from answer (the caller joined) to that moment; the backend's `end`
    counts the minutes once per call (#5086), and this posts it once."""

    def __init__(self, answered: float, end) -> None:
        self.answered, self._end = answered, end
        self._task: asyncio.Task | None = None

    def now(self, _reason: str = "") -> asyncio.Task:
        if self._task is None:
            self._task = asyncio.create_task(self._end(int(time.monotonic() - self.answered)))
        return self._task


class DisclosureGuard:
    """Judges the opening once it has finished: after the agent's first speaking -> listening change, on the
    assistant lines transcribed so far (an interrupted one holds only what played). No line that discloses makes
    `say` have the disclosure sentence said once; a caller talking over the opening before its disclosure is such a
    miss (Review Focus 4). PHONE_TEST_FAIL_DISCLOSURE=1 fails it (scripted runs)."""

    def __init__(self, assistant: str, say) -> None:
        self.assistant, self._say = assistant, say
        self.line = f"I'm {assistant}, an AI assistant, and this call is recorded."
        self.corrected = self._done = self._greeted = self._spoke = False

    def state(self, old: str, new: str) -> None:
        if new == "speaking":
            self._spoke = True
        elif self._spoke and old == "speaking":
            self._greeted = True  # the greeting finished (or was cut off) playing

    def seen(self, items: list[dict]) -> None:
        if self._done or not self._greeted:
            return
        said = [i["text"] for i in items if i["role"] == "assistant"]
        if os.environ.get("PHONE_TEST_FAIL_DISCLOSURE") == "1":
            ok = False
        elif not said:
            return  # the opening's line is still on its way
        else:
            ok = any(discloses(t, self.assistant) for t in said)
        self._done = True
        if not ok:
            logger.info("phone: the opening lacked the disclosure; saying it once")
            self.corrected = True
            self._say(self.line)


def say_exactly(line: str) -> str:
    return f'The disclosure was missed. Say this now, word for word, then continue with the caller: "{line}"'


# Standalone (`python -m huddle_agent.phone dev`, a dev line): its own dispatch name; this kwarg wins over
# LIVEKIT_AGENT_NAME in livekit-agents 1.8.11. Deployed, the huddle worker's entrypoint routes `phone-` rooms here.
@server.rtc_session(agent_name=os.environ.get("PHONE_AGENT_NAME", "lightbulb-phone-dev"))
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    try:
        caller = await asyncio.wait_for(ctx.wait_for_participant(), AWAY_S * 2)
        answered = time.monotonic()  # the SIP caller joins when the call is answered: billing starts here
    except TimeoutError:  # 8 Nov sims: a job that started after the caller left sat in the room for 12 minutes
        logger.info("phone: nobody joined job %s; leaving", ctx.job.id)  # the room name holds the caller's number
        ctx.shutdown("no caller")
        return
    meta = json.loads(ctx.job.metadata or "{}")  # a test dispatch (lk dispatch) names the numbers itself
    called = e164(caller.attributes.get("sip.trunkPhoneNumber") or meta.get("calledNumber") or "")
    caller_number = e164(caller.attributes.get("sip.phoneNumber") or meta.get("callerNumber") or "")
    backend = Backend()
    info = await backend.post(
        "/internal/voice/calls", {"calledNumber": called, "callerNumber": caller_number, "livekitRoom": ctx.room.name}
    )
    if info.get("error"):
        logger.error("phone: call refused (%s)", info["error"])
        return
    backend.call_id = info["callId"]
    logger.debug("phone: call %s started", backend.call_id)

    async def transfer(to: str) -> None:
        await ctx.api.sip.transfer_sip_participant(
            api.TransferSIPParticipantRequest(
                room_name=ctx.room.name, participant_identity=caller.identity, transfer_to=f"tel:{to}"
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

    did: list[str] = []  # what the call did itself, the record's summary (the Front desk adds its steps)
    tasks: set[asyncio.Task] = set()
    turn_n, order = itertools.count(), asyncio.Lock()
    asked = [0.0]  # when GPT-Live last delegated (monotonic)
    ls: list[live.LiveSession] = []  # bound once started; post and events run only after

    def spawn(coro) -> None:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    async def post(text: str, turns: list[str]) -> str:
        """One delegation: the caller's line in the call's room, numbered in the order asked (a retry is the same
        message). A paused line refuses it (`not_allowed`): it is a message for the team."""
        asked[0] = time.monotonic()
        said = "\n".join(f"> {t}" for t in turns)
        body = (text.strip() + (f"\n\nSaid on the call since the last ask:\n{said}" if said else ""))[:4000]
        n = next(turn_n)
        async with order:
            out = await backend.post("turn", {"n": n, "text": body})
        if out.get("error") == "not_allowed":
            did.append(f"Message: {text[:120]}")
            await backend.post("message", {"text": body, "name": info.get("callerName") or "", "callbackNumber": ""})
            ls[0].send_result(None, PAUSED)
            return ""
        if out.get("error"):
            logger.warning("phone %s: line %d not posted (%s)", backend.call_id, n, out["error"])
        return out.get("messageId", "")

    async def hand_off() -> None:
        """The backend asked for a person: the existing /transfer route, then the SIP transfer."""
        out = await backend.post("transfer")
        if out.get("transferTo"):
            try:
                await transfer(out["transferTo"])
                did.append("Transferred to a person")
                return
            except Exception:  # noqa: BLE001 -- a SIP refusal: the record says the hand-off was missed
                logger.warning("phone %s: transfer failed", backend.call_id, exc_info=True)
                await backend.post("transfer", {"failed": True})
        ls[0].send_result(None, NOBODY)

    async def events():
        """The room's new agent messages, oldest first, each once (the cursor moves past it)."""
        after = int(info.get("after") or 0)
        while True:
            await asyncio.sleep(POLL_S)
            out = await backend.post("events", {"after": after})
            if out.get("error"):
                logger.warning("phone %s: events failed (%s)", backend.call_id, out["error"])
                continue
            after = max(after, int(out.get("after") or 0))
            for m in out.get("messages", []):
                if m.get("kind") == "transfer":
                    spawn(hand_off())
                    continue
                # a card's Choice said as its options, links and markdown dropped: the caller only hears it
                yield {"kind": "agent", **m, "text": speakable(m.get("text") or "")}

    live_session = await live.start(
        ctx, prompt=prompt(info), surface="phone", post=post, events=events,
        room_options=room_io.RoomOptions(
            participant_identity=caller.identity,
            close_on_disconnect=True,
            # noise cancellation tuned for SIP audio, in the agent and nowhere else on the call (one denoiser:
            # docs.livekit.io/transport/media/enhanced-noise-cancellation)
            audio_input=room_io.AudioInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        ),
    )  # fmt: skip
    ls.append(live_session)
    session = live_session.session
    items: list[dict] = []
    t0 = time.time()
    heard = [time.monotonic()]  # the last sign of life on the line: the caller, the voice, or a pending ask
    guard = DisclosureGuard(info["assistantName"], lambda line: live_session.send_result(None, say_exactly(line)))

    @session.on("conversation_item_added")
    def _item(ev) -> None:
        if text := getattr(ev.item, "text_content", None):
            at = (ev.item.metrics or {}).get("started_speaking_at") or ev.item.created_at
            items.append({"role": ev.item.role, "text": text, "t": at - t0, "interrupted": ev.item.interrupted})
            heard[0] = time.monotonic()
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
        spawn(voice_failed(ev.error))

    async def voice_failed(err) -> None:
        """No model left: a callback card in the owner's Needs you, a metric, and the line is closed."""
        logger.error("phone %s: voice model failed (%s)", backend.call_id, getattr(err, "error", err))
        emit_metric("phone.voice.failed", 1, "Count", {"engine": "gpt-live"})
        await backend.post("unknown", {"question": "The assistant's voice failed during this call: call them back."})
        did.append("Voice failed; callback needed")
        await hang_up("voice failed")

    async def hang_up(reason: str) -> None:
        await ctx.delete_room()  # disconnects the SIP caller too
        # the call ends as we hang up, not at the job's shutdown; no await after it, so on_end (which cancels the
        # silence and max-length tasks that may be running this) starts only once this has returned
        ending.now()
        ctx.shutdown(reason)

    silent_end: list[bool] = []

    def holding() -> bool:
        """GPT-Live is waiting on the team (a delegation is pending, for up to WAIT_S): a quiet caller is holding."""
        return bool(live_session.pending) and time.monotonic() - asked[0] < WAIT_S

    async def watch_silence() -> None:
        """Quiet for AWAY_S: GPT-Live is told (it decides what to say); quiet for AWAY_S more: the call ends
        (8 Nov sims: a silent line stayed open, billed, until the 20-minute cap). live.start sets no
        user_away_timeout, so the line's silence is measured here."""
        told = False
        while True:
            await asyncio.sleep(min(1.0, AWAY_S / 4))
            if session.user_state == "speaking" or session.agent_state == "speaking" or holding():
                heard[0] = time.monotonic()
            quiet = time.monotonic() - heard[0]
            if quiet < AWAY_S:
                told = False
            elif quiet >= 2 * AWAY_S and told:
                silent_end.append(True)
                await hang_up("caller silent")
                return
            elif not told:
                told = True
                live_session.send_result(None, QUIET)

    async def time_cap() -> None:
        await asyncio.sleep(CALL_MAX_S)
        live_session.send_result(None, GOODBYE)
        await asyncio.sleep(BYE_S)
        await hang_up("call max length")

    watchers = [asyncio.create_task(watch_silence()), asyncio.create_task(time_cap())]

    async def on_end(duration: int) -> None:
        for task in (*watchers, *tasks):
            if task is not asyncio.current_task():
                task.cancel()
        await live_session.close()  # GPT-Live bills every open second; a post in flight still reaches the room
        out = await backend.post(
            "end",
            {
                "durationSec": duration,
                "summary": "; ".join(did),
                "transcript": clean_transcript(items),
                "disclosure": disclosure_of(items, info["assistantName"]),
                "disclosureRepeated": guard.corrected,
                "silent": bool(silent_end),
            },
        )
        logger.info("phone: call %s ended outcome=%s", backend.call_id, out.get("outcome", out))

    ending = Ending(answered, on_end)

    async def shutdown(reason: str = "") -> None:
        await ending.now(reason)

    async def hung_up(end: asyncio.Task) -> None:
        await end
        ctx.shutdown("caller hung up")

    left: list[asyncio.Task] = []  # held so the task is not garbage-collected

    @ctx.room.on("participant_disconnected")
    def _left(participant) -> None:
        if participant.identity == caller.identity and not left:  # end now: the job lingers ~22 s, billed
            left.append(asyncio.create_task(hung_up(ending.now())))

    ctx.add_shutdown_callback(shutdown)
    live_session.send_result(None, OPENING.format(assistant=info["assistantName"], greeting=info["greeting"]))


if __name__ == "__main__":
    cli.run_app(server)
