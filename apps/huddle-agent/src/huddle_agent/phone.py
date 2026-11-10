"""The phone receptionist (voice-agents spec §4.1b, §4.3) on GPT-Live (plan 2026-11-08-gpt-live-voice, Task 4;
founder 8 Nov 2026: "use their voice model with our deepseek router"). GPT-Live is the voice: it listens, speaks,
takes turns and decides when a caller's line needs the backend (client delegation, `live.start`). Every delegation
is the caller's line in the call's room (`/turn`, the caller principal: the call's open thread first, else the
Router with the Business profile, whose Front desk threads book, move and cancel as the caller). The room's agent
messages come back through `/events` as results. Our code says nothing of its own: it runs the call (SIP, the
record, billing at hang-up, the run-out forward) and the two routes only the worker can do:
the SIP transfer (the Router's transfer_call line, an events item with `action: "transfer"`) and a paused line's
message (`/turn` refuses: `/message`).

The owner calling in (board row 3.29): keypad digits (LiveKit SIP's `sip_dtmf_received`) go to `/owner-pin`, which
alone decides. Once it says yes, the same `/turn` and `/events` carry the member's own lines in their DM with the
assistant (the orb's owner path, the same Router), and GPT-Live is told it now talks to the owner.

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
import time

import httpx
from livekit import api
from livekit.agents import AgentServer, JobContext, cli, room_io
from livekit.plugins import noise_cancellation

from huddle_agent import GOODBYE, live
from huddle_agent.metrics import emit_metric

logger = logging.getLogger("phone")
AWAY_S = float(os.environ.get("PHONE_AWAY_S", "15"))  # caller silence before GPT-Live hears it; again: hang up
CALL_MAX_S = float(os.environ.get("PHONE_MAX_S", 20 * 60))  # cost cap: a call ends after 20 minutes
BYE_S = 8.0  # the goodbye's time to play at the cost cap
CONNECT_S = 3.0  # the transfer line's time to play before the SIP transfer
WAIT_S = 90.0  # a pending delegation keeps a quiet line open this long (a Front desk thread: 69-95 s on dev, 8 Nov)
POLL_S = 1.0  # how often the call asks what Lightbulb posted in its room
# One warm process is plenty for a line; a busy host gets a longer start and its own load ceiling.
server = AgentServer(
    num_idle_processes=1,
    initialize_process_timeout=60,
    load_threshold=float(os.environ.get("PHONE_LOAD_THRESHOLD", "0.7")),
)

#: What the backend does for a caller (GPT-Live's `Backend tools`): the Router's Business profile and transfer_call,
#: and the caller-scoped Front desk thread's actions (front_desk: availability, book, my_bookings, move, cancel,
#: waitlist, message), nothing more.
TOOLS = [
    "Business profile: the business's actual description, what it does, services, prices, durations, hours and policies.",
    "Availability: find open times for a service on a day.",
    "Bookings: book a time, or list, move or cancel the caller's own bookings.",
    "Messages: take a message for the owner, with the caller's name and a callback number.",
    "A person: hand the call to a person at the business, when someone is available.",
]
MESSAGE_ONLY_TOOLS = ["Messages: take a message for the team; the owner will call back."]

#: Said once after start, as the docs greet: the same instructions as the prompt's opening, as commentary with
#: delegation_id null (developers.openai.com/api/docs/guides/live-prompting, "Language and pronunciation").
OPENING = (
    "The call has just been answered. Speak first in English, in one short sentence, and then listen. You are "
    '{assistant}. Open the way the business\'s own greeting does, a guide, not a script: "{greeting}"'
)

PROMPT = """You are {assistant}, the calm, friendly AI receptionist on the phone for {business}.
Speak warmly and naturally, at an unhurried pace, one or two short sentences at a time, like a good front desk. Be
clear and direct, not overly cheerful. {language}
If the caller is frustrated, acknowledge it briefly and focus on the next helpful step.
{caller}{paused}{owner}
Opening: your very first words follow the business's greeting, then you listen.
If a caller sincerely asks whether they are speaking to a person, answer truthfully that this is an AI assistant.

Backchannel policy: Use light backchannels. Acknowledge naturally without competing with the main response.

Interruption policy: Stop speaking when the caller interrupts. Listen to what they say.

{delegation}

Names, dates and numbers: before a booking is made, say the name it goes under back to the caller and ask if you
have it right. If an important name, date, or number is unclear, ask about that part. Use the caller's correction.
Do not guess the missing value.

Wait for the backend's result before describing this business or its services, stating a price or a time, confirming a booking, or saying that a message was
taken or a transfer is under way. Never share anything about other customers.
If the caller objects to being recorded, answer honestly that the business may keep a transcript of the call, and
offer a person or a message."""

#: Facts the call tells GPT-Live (commentary with delegation_id null): GPT-Live chooses what to say.
QUIET = "The line has been quiet for a while. If the caller stays silent, the call will end."
NOBODY = "Nobody at the business can take the call right now. The team can take a message instead."
PAUSED = "The caller's message was passed on to the team; the owner will call back."
#: The number is a member's saved mobile with a phone PIN (start's `ownerPin`). Caller ID is no proof: only the PIN,
#: typed on the keypad and checked by the backend, is. The model never hears the digits or decides if they are right.
OWNER_PIN = (
    "This number belongs to someone at the business. If they say they are the owner or want to talk as the owner, "
    "they can type their phone PIN on the keypad, then the pound key. You never hear the PIN and never decide "
    "whether it is right: you are told the result. Until then they are a caller like any other.\n"
)
#: The backend's answer to the typed PIN, told to GPT-Live (commentary): it chooses the words.
PIN_OK = (
    "The PIN typed on the keypad was right: this is the business's owner. From now on they talk as the owner, "
    "with the team's full help, as in the Lightbulb app."
)
PIN_WRONG = "The PIN typed on the keypad was not accepted."
OWNER_ENDED = "Owner mode has ended: this person is no longer a member of the business. They are a caller again."
ANSWER_WITHIN_S = 60.0  # owner.py's: a line this soon after a Worker's question answers it; later, a new ask
#: The prompt from then on (owner.py's, on the phone): the team's brain, not the front desk.
OWNER_NOW = """The caller has proven they are the business's owner. They are no longer a caller.
Backend tools now: the team's brain, which knows the team's work across email, calendar and docs, remembers what
the team said and decided, and starts work for the team's agents. Delegate their requests as before.
Approvals happen on the card in the Lightbulb app, never by voice: when something needs approval, say it is waiting
on the card in the app. A spoken yes approves nothing."""


def prompt(info: dict) -> str:
    paused = info.get("mode") == "message_only"
    return PROMPT.format(
        language=(
            "Open in English. Speak English or Spanish, following the caller's choice. Switch naturally when the "
            "caller speaks or asks for Spanish or English; keep all permissions rules in either language."
            if info.get("spanish") is True
            else "Speak only English, whatever language a line seems to be in."
        ),
        assistant=info["assistantName"],
        business=info["businessName"],
        caller=f"The caller is {info['callerName']}, a returning client.\n"
        if info.get("callerName")
        else ""
        if info.get("callerVerified")
        else "This caller's number is not verified: you cannot look up, move or cancel existing bookings; offer "
        "to take a message for the team instead.\n",
        paused="This line is not taking bookings or questions right now: it only takes messages.\n"
        if paused
        else "" if info.get("transfer") else "No person is available to transfer a call to; take a message instead.\n",
        owner=OWNER_PIN if info.get("ownerPin") else "",
        delegation=live.delegation_policy(
            MESSAGE_ONLY_TOOLS if paused else TOOLS if info.get("transfer") else TOOLS[:-1]  # no number: no person tool
        ),
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
        self.call_token = ""  # start's callToken: every later route for this call needs it, not just the secret

    async def post(self, path: str, body: dict | None = None) -> dict:
        sent = time.monotonic()
        if path.startswith("/"):
            r = await self._http.post(path, json=body or {})
        else:
            r = await self._http.post(
                f"/internal/voice/calls/{self.call_id}/{path}",
                json=body or {},
                headers={"X-Lightbulb-Call-Token": self.call_token},
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


END_TRIES, END_BACKOFF_S = 3, 1.0
CANT_TAKE = "We can't handle this call right now. Tell the caller briefly, in your own words, and suggest calling back later."
SORRY_S = 7.0  # the model's time to say it before the hang-up


async def post_end(backend, body: dict) -> dict:
    """`end` writes the record, the minutes and the owner's card, and the backend repeats it safely: retry a refusal
    or a dropped request a few times before giving up (a call left live is closed by the backend's sweep)."""
    out: dict = {}
    for n in range(END_TRIES):
        try:
            out = await backend.post("end", body)
            if not out.get("error"):
                return out
        except Exception:  # noqa: BLE001 -- a network error is a failed try like any other
            logger.warning("phone %s: end failed", backend.call_id, exc_info=True)
            out = {"error": "unreachable"}
        if n < END_TRIES - 1:
            await asyncio.sleep(END_BACKOFF_S * (n + 1))
    logger.error("phone %s: end not delivered (%s)", backend.call_id, out.get("error"))
    return out


async def say_sorry_and_hang_up(ctx: JobContext, caller) -> None:
    """The backend refused to start the call: the model tells the caller (we give it the fact, not the words), then we hang up, instead of dead air."""

    async def no_post(*_a):
        return None

    async def no_events():
        return
        yield

    try:
        voice = await live.start(
            ctx, prompt="You answer the phone for a business. The call cannot be handled right now; the caller hears you say so.", surface="phone",
            post=no_post, events=no_events,
            room_options=room_io.RoomOptions(participant_identity=caller.identity, close_on_disconnect=True),
        )  # fmt: skip
        voice.send_result(None, CANT_TAKE)
        await asyncio.sleep(SORRY_S)
        await voice.close()
    except Exception:  # noqa: BLE001 -- the hang-up below is the point
        logger.warning("phone: could not say the refusal line", exc_info=True)
    await ctx.delete_room()
    ctx.shutdown("call refused")


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
        "/internal/voice/calls",
        {
            "calledNumber": called,
            "callerNumber": caller_number,
            "livekitRoom": ctx.room.name,
            # Twilio's X-Twilio-VerStat, mapped by the inbound trunk (scripts/provision_phone_telephony.py)
            "verstat": caller.attributes.get("sip.verstat") or "",
        },
    )
    if info.get("error"):
        logger.error("phone: call refused (%s)", info["error"])
        await say_sorry_and_hang_up(ctx, caller)
        return
    backend.call_id, backend.call_token = info["callId"], info.get("callToken") or ""
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
            await post_end(backend, {"durationSec": 0, "forwarded": True})
            ctx.shutdown("minutes used up: forwarded")
            return

    did: list[str] = []  # what the call did itself, the record's summary (the Front desk adds its steps)
    tasks: set[asyncio.Task] = set()
    turn_n = itertools.count()
    asked: dict[str, float] = {}  # each posted ask's message id -> when it was posted (monotonic)
    question: list = [None, 0.0]  # the last Worker question {taskId, qid} the voice was given, and when
    ls: list[live.LiveSession] = []  # bound once started; post and events run only after

    def spawn(coro) -> None:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    def owner_ended(out: dict) -> None:
        """The member was removed mid-call (`/events` or `/turn` says so once): a caller again, from the call room's
        cursor, with the caller prompt."""
        cursor[0] = int(out.get("after") or 0)
        question[:] = [None, 0.0]
        ls[0].transport.instructions(prompt(info))
        ls[0].send_result(None, OWNER_ENDED)

    async def post(text: str, turns: list[str]) -> str | live.Posted | None:
        """One delegation: the caller's line in the call's room, numbered in the order asked (live.py posts one at a
        time, in order; a retry is the same message). Its message id, or None: not posted. A paused line refuses it
        (`not_allowed`): it is a message for the team."""
        said = "\n".join(f"> {t}" for t in turns)
        body = (text.strip() + (f"\n\nSaid on the call since the last ask:\n{said}" if said else ""))[:4000]
        n = next(turn_n)
        q, at = question  # owner mode: the Worker question just said is answered by this line (owner.py's rule)
        answers = q if q and time.monotonic() - at <= ANSWER_WITHIN_S else None
        out = await backend.post("turn", {"n": n, "text": body, **({"answers": answers} if answers else {})})
        if answers is not None and question[0] is q:
            question[:] = [None, 0.0]
        if out.get("ownerModeEnded"):  # /turn found the member removed before any poll did
            owner_ended(out)
        if out.get("error") == "not_allowed":
            did.append(f"Message: {text[:120]}")
            await backend.post("message", {"text": body, "name": info.get("callerName") or "", "callbackNumber": ""})
            ls[0].send_result(None, PAUSED)
            return None
        if out.get("error"):
            logger.warning("phone %s: line %d not posted (%s)", backend.call_id, n, out["error"])
        if mid := out.get("messageId") or None:
            asked[mid] = time.monotonic()
        return live.Posted(mid, (out["taskId"],)) if mid and out.get("taskId") else mid

    async def hand_off() -> None:
        """The Router's transfer_call: its line plays first, then the existing /transfer route and the SIP
        transfer."""
        await asyncio.sleep(CONNECT_S)
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

    transferred: set[str] = set()  # the events iterator may restart; an admitted SIP action must not replay
    silence_checks: list = []  # bound after startup; events can arrive while the model is opening

    cursor = [int(info.get("after") or 0)]  # owner mode moves it to the member's DM (pin_typed)

    async def events():
        """The room's new agent messages, oldest first, each once (the cursor moves past it)."""
        while True:
            await asyncio.sleep(POLL_S)
            after = cursor[0]
            out = await backend.post("events", {"after": after})
            if out.get("error"):
                logger.warning("phone %s: events failed (%s)", backend.call_id, out["error"])
                continue
            if cursor[0] != after:
                continue  # owner mode began while this was in flight: these were the call room's
            if out.get("ownerModeEnded"):
                owner_ended(out)
                continue
            cursor[0] = max(after, int(out.get("after") or 0))
            for m in out.get("messages", []):
                if m.get("question"):
                    question[:] = [m["question"], time.monotonic()]
                if m.get("action") == "transfer" and (mid := m.get("id")) and mid not in transferred:
                    transferred.add(mid)
                    spawn(hand_off())
                yield {"kind": "agent", **m}
                if m.get("waiting") is False and silence_checks:
                    silence_checks[0]()  # return an answered quiet caller to the ordinary silence timer

    try:
        live_session = await live.start(
            ctx, prompt=prompt(info), surface="phone", post=post, events=events,
            voice=info.get("voice") if info.get("voice") in ("marin", "quartz", "willow") else "marin",
            opening=OPENING.format(assistant=info["assistantName"], greeting=info["greeting"]),
            user_away_timeout=AWAY_S,  # caller silence only: the voice's own words never reset it
            record=True,
            room_options=room_io.RoomOptions(
                participant_identity=caller.identity,
                close_on_disconnect=True,
                # noise cancellation tuned for SIP audio, in the agent and nowhere else on the call (one denoiser:
                # docs.livekit.io/transport/media/enhanced-noise-cancellation)
                audio_input=room_io.AudioInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
            ),
        )  # fmt: skip
    except Exception:  # the paid call already exists, but the normal shutdown hooks are not registered yet
        logger.exception("phone %s: voice session could not start", backend.call_id)
        emit_metric("phone.voice.failed", 1, "Count", {"engine": "gpt-live"})
        try:
            await backend.post("unknown", {"question": "The assistant's voice failed during this call: call them back."})
        finally:
            try:
                await post_end(backend, {"durationSec": int(time.monotonic() - answered),
                                         "summary": "Voice failed; callback needed"})
            finally:
                await ctx.delete_room()
                ctx.shutdown("voice failed")
        return
    ls.append(live_session)
    session = live_session.session
    items: list[dict] = []
    t0 = time.time()
    @session.on("conversation_item_added")
    def _item(ev) -> None:
        if text := getattr(ev.item, "text_content", None):
            at = (ev.item.metrics or {}).get("started_speaking_at") or ev.item.created_at
            items.append({"role": ev.item.role, "text": text, "t": at - t0, "interrupted": ev.item.interrupted})

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
    silent: list[asyncio.Task] = []

    def holding() -> float:
        """How long GPT-Live may still wait on the team: the youngest pending ask's time left under WAIT_S (one being
        posted counts as new). 0: none pending, or all older than WAIT_S (a lost answer never holds the line)."""
        now, left = time.monotonic(), 0.0
        for mid in live_session.awaiting.values():
            left = max(left, WAIT_S - (now - asked.get(mid, now)) if mid else WAIT_S)
        return max(left, 0.0)

    async def hang_up_silent() -> None:
        """Still no caller AWAY_S after GPT-Live heard the line is quiet: the call ends (8 Nov sims: a silent line
        stayed open, billed, until the 20-minute cap)."""
        await asyncio.sleep(AWAY_S)
        silent_end.append(True)
        await hang_up("caller silent")

    def still_away() -> None:
        if session.user_state == "away":
            _away(type("Ev", (), {"new_state": "away"}))

    silence_checks.append(still_away)

    @session.on("user_state_changed")
    def _away(ev) -> None:
        """The caller's state only (user_away_timeout): the voice's own "are you still there?" never resets it."""
        if ev.new_state == "away" and (wait := holding()):  # holding for the team's answer is not gone
            asyncio.get_running_loop().call_later(wait + 0.01, still_away)
            return
        if ev.new_state == "away" and not silent:
            live_session.send_result(None, QUIET)
            silent.append(asyncio.create_task(hang_up_silent()))
        elif ev.new_state != "away" and silent:
            silent.pop().cancel()

    async def time_cap() -> None:
        await asyncio.sleep(CALL_MAX_S)
        live_session.send_result(None, GOODBYE)
        await asyncio.sleep(BYE_S)
        await hang_up("call max length")

    limit = asyncio.create_task(time_cap())

    async def on_end(duration: int) -> None:
        for task in (limit, *silent, *tasks):
            task.cancel()
        try:
            await live_session.close()  # GPT-Live bills every open second; a post in flight still reaches the room
        except Exception:  # noqa: BLE001 -- the record and the minutes are posted whatever the close did
            logger.warning("phone %s: voice session close failed", backend.call_id, exc_info=True)
        out = await post_end(
            backend,
            {
                "durationSec": duration,
                "summary": "; ".join(did),
                "transcript": clean_transcript(items),
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

    digits: list[str] = []

    async def pin_typed(pin: str) -> None:
        """The typed PIN goes to the backend, which alone decides (owner-pin); never to the model or the log."""
        out = await backend.post("owner-pin", {"pin": pin})
        if out.get("ok"):
            cursor[0] = int(out.get("after") or 0)
            live_session.transport.instructions(OWNER_NOW)
            live_session.send_result(None, PIN_OK)
            did.append("The owner proved it was them with the PIN")
        else:
            live_session.send_result(None, PIN_WRONG)

    @ctx.room.on("sip_dtmf_received")
    def _dtmf(ev) -> None:
        """Keypad digits from the caller: `#` sends them, `*` starts over. Only on a number start called ownerPin."""
        if not info.get("ownerPin") or getattr(ev.participant, "identity", None) != caller.identity:
            return
        if ev.digit == "#":
            pin, digits[:] = "".join(digits), []
            if 4 <= len(pin) <= 8:
                spawn(pin_typed(pin))
            else:
                live_session.send_result(None, PIN_WRONG)
        elif ev.digit == "*":
            digits.clear()
        elif ev.digit.isdigit() and len(digits) < 8:
            digits.append(ev.digit)

    ctx.add_shutdown_callback(shutdown)
    live_session.send_result(None, OPENING.format(assistant=info["assistantName"], greeting=info["greeting"]))


if __name__ == "__main__":
    cli.run_app(server)
