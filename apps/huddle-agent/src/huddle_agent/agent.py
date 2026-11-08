"""LiveKit glue for the huddle worker. All behaviour lives in huddle.Huddle."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobRequest,
    cli,
    llm,
    room_io,
    stt,
    utils,
)
from livekit.plugins import noise_cancellation, openai
from openai import AsyncOpenAI
from openai.types.realtime import (
    ConversationItemDeleteEvent,
    RealtimeConversationItemUserMessage,
    ResponseCancelEvent,
    ResponseCreateEvent,
)

from huddle_agent import meet, owner, phone, record
from huddle_agent.backend import Backend
from huddle_agent.huddle import Huddle, emit_metric, units

logger = logging.getLogger("huddle")
AGENT_IDENTITY = "agent-ceo"  # C4: every agent voice publishes as this one participant
WORKER_DISPLAY_NAME = "Lightbulb"  # clients render tiles from attributes, not this name
# LiveKit's default (10 s) would kill a meeting's final transcript retry (huddle.FINAL_TRANSCRIPT_BUDGET_S).
SHUTDOWN_S = 60
server = AgentServer(shutdown_process_timeout=SHUTDOWN_S)


def _seeded(seed: list[tuple[str, str]]) -> llm.ChatContext:
    """The huddle so far, as this agent saw it (Transcript.seed)."""
    ctx = llm.ChatContext.empty()
    for role, text in seed:
        ctx.add_message(role=role, content=text)
    return ctx


FALLBACK_VOICE = "marin"
WRITE_TIMEOUT_S = 3.0
# The Realtime model's voices. An agent's voice may be one it lacks (one picked for GPT-Live, which had more:
# "stone", 10-07 rig, a refused session and a second open). That maps to one it has, the same one every time
# for that name, so two agents still sound different.
REALTIME_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")


def realtime_voice(voice: str) -> str:
    return voice if voice in REALTIME_VOICES else REALTIME_VOICES[sum(voice.encode()) % len(REALTIME_VOICES)]
SERVER_VAD = {
    "type": "server_vad",
    "threshold": 0.5,
    "prefix_padding_ms": 600,
    "silence_duration_ms": 700,
}


# Every call, in a Meet or in the app: the plain silence rule, and a short one. semantic_vad gave a line's words
# 1.5-5.8 s after it was said, this gives them in about 1.3 s (rig, 10-06). A sentence it splits at a pause is
# still one turn: the worker joins its pieces (huddle `_heard_final`).
MEET_VAD = {**SERVER_VAD, "silence_duration_ms": 400}


async def _unpublish(room: rtc.Room, track_name: str) -> None:
    # RoomIO never unpublishes its audio track; only this agent's, another agent may be talking.
    lp = room.local_participant
    for pub in list(lp.track_publications.values()):
        if pub.name == track_name:
            try:
                await lp.unpublish_track(pub.sid)
            except rtc.participant.UnpublishTrackError:
                pass  # already gone (the room is closing): nothing left to bill


#: Added to a driven voice's persona: every word it says is ordered by the worker.
DRIVEN = "You read lines aloud. You are given each line, and you say exactly that line and nothing else."
SAY = "Say this line aloud, word for word, and nothing else:\n"
# A reply to small talk is the one line a model writes for a mouth: a small text model, given the line said to
# the agent and who said it, and nothing else. Acknowledgements and status lines are the worker's fixed lines
# (huddle.ACKS): a writer given the worker's notes read them out ("Vera's checking...", 10-07 rig). The speech
# model never words anything: with a question in view it answered from its own head ("it's $1,600", 10-07 rig).
WRITER_MODEL = "gpt-4.1-mini"
WRITER = (
    "You write the one short sentence this teammate says aloud next on a voice call, in the first person, in "
    "their own voice. You are given one line that was just said to them and who said it. It is small talk: "
    "reply the way a person would. This teammate's answers to real questions come from work that takes time "
    "and arrive separately: never answer a question yourself, never promise work, and never state a number, "
    "price, date, name or fact. One sentence, at most 16 words, natural and plain. No filler. Never invite more "
    "questions or offer further help. If the line asks for anything or needs no reply, output exactly NONE. "
    "Output the sentence only."
)


_writer: AsyncOpenAI | None = None  # one client for the whole call, made at the first line: a test has no key


async def write_line(persona: str, event: str) -> str:
    """The one sentence a mouth says to small talk (huddle's `writer`). "": nothing worth saying, or no line
    in time. One client for every voice of the call: a new one per voice paid a new connection for each
    reply (10-07 rig: about 2 s before "Hi")."""
    global _writer
    try:
        _writer = _writer or AsyncOpenAI()
        r = await asyncio.wait_for(
            _writer.chat.completions.create(
                model=WRITER_MODEL,
                max_tokens=48,
                temperature=0.8,
                messages=[
                    {"role": "system", "content": f"{persona}\n\n{WRITER}"},
                    {"role": "user", "content": event},
                ],
            ),
            WRITE_TIMEOUT_S,
        )
        line = (r.choices[0].message.content or "").strip()
        return "" if line.strip(" .\"'").upper() == "NONE" else line  # nothing worth saying: silence
    except Exception:  # noqa: BLE001 -- no line is better than a late or a made-up one
        logger.warning("huddle driven voice: no line written", exc_info=True)
        return ""


# After a cut, the next order waits for the cancelled response's `response.done`, this long at most. A
# `response.create` that raced a `response.cancel` failed outright (10-08 spike, config e). The wait itself was
# not measured: the done normally follows the cancel at once.
CANCEL_WAIT_S = 0.5


class _DrivenSession(openai.realtime.RealtimeSession):
    """A line to say word for word goes to the model without the conversation: its order is the line, in the
    instructions and again as the one user message of that response (`response.input`, which is never added
    to the conversation). With the conversation in view the model answers the last thing a person said
    instead, from its own head (10-07, real model: 5 of 12 lines exact after an open question, "$12 per user
    per month" among the rest; 12 of 12 without). With the line in the instructions alone (`input: []`) a
    short line sometimes came out as silence (10-08 spike, short lines: 5 of 52 silent, first sound p50
    0.88 s; with the user message 0 of 52, 52 of 52 word for word, p50 0.58 s).

    What it said is deleted from the conversation when its response is done: nothing reads it, and a long
    call does not pile it up. An order sent while a cut response is still ending is held until it has ended."""

    _cancelling: asyncio.TimerHandle | None = None  # a cancel is out and its `response.done` is not in
    _held: tuple = ()  # the orders waiting for it
    #: Told as each `response.create` is sent (`_create`): the unit's first-sound time counts from then.
    sent: Callable[[], None] | None = None

    # ponytail: private API (the plugin builds response.create itself), re-check on livekit-agents upgrades
    def send_event(self, event) -> None:
        if isinstance(event, ResponseCancelEvent) and self._cancelling is None and self._current_generation is not None:
            self._cancelling = asyncio.get_running_loop().call_later(CANCEL_WAIT_S, self._release)
        elif isinstance(event, ResponseCreateEvent):
            # every order carries all it needs: the voice never reads the conversation
            _, say, line = (event.response.instructions or "").rpartition(SAY)
            event.response.input = [
                RealtimeConversationItemUserMessage(
                    type="message", role="user", content=[{"type": "input_text", "text": f'Say exactly: "{line}"'}]
                )
            ] if say else []  # fmt: skip
            if self._cancelling is not None:
                self._held += (event,)
                return
            return self._create(event)
        super().send_event(event)

    def _create(self, event) -> None:
        """A `response.create` goes to the model, now: at once, or after its hold. The one place it does."""
        super().send_event(event)
        if self.sent is not None:
            self.sent()

    def _release(self) -> None:
        if self._cancelling is not None:
            self._cancelling.cancel()
        held, self._cancelling, self._held = self._held, None, ()
        for event in held:
            self._create(event)

    def _handle_response_done(self, event) -> None:
        said = list(getattr(self._current_generation, "messages", ()))
        super()._handle_response_done(event)
        for item_id in said:
            super().send_event(ConversationItemDeleteEvent(type="conversation.item.delete", item_id=item_id))
        self._release()

    def truncate(self, **_kw) -> None:
        pass  # a cut unit's item is deleted with the rest: a truncate after that is an error from the server


class _DrivenModel(openai.realtime.RealtimeModel):
    def session(self, *, turn_detection_disabled: bool = False) -> _DrivenSession:
        sess = _DrivenSession(self, turn_detection_disabled=turn_detection_disabled)
        self._sessions.add(sess)
        return sess


# A unit's audio counts as heard when this much of it was above the floor below. A speech handle can end
# uncut with no sound on the track (10-08 rig: "Got it." was reported said twice, and the room heard nothing
# either time). Whether its frames were silent or never came was not recorded then: the receipt says now.
AUDIBLE_S = 0.1
LOUD = 500  # of 32768: about -36 dBFS, well under any spoken word and over a silent frame's dither
# A unit with no audible audio this long after its `response.create` went to the model is stopped and reported
# failed, and the worker says it again at once (`Huddle.on_playback_ended`). Counted from the send itself
# (`_DrivenSession._create`), so a create held behind a cancelled response (CANCEL_WAIT_S: after an interrupt,
# after the unit before stopped itself, or the first order of a mouth that was parked) has the whole time.
# 10-08 spike, the order as it is now: first sound p50 0.58 s, max 0.80 s of 52. It was 3.0 s: a dead unit cost
# the room three seconds of nothing (rig: 3 of 83 units silent).
FIRST_SOUND_S = 1.0
# An order whose create has not gone out this long after it was given is stopped the same way (`unsent`): the
# longest hold there is, and as much again. Nothing else bounds it short of the plugin's own 5 s.
SEND_WAIT_S = 2 * CANCEL_WAIT_S
# A unit with more sound than this is stopped and reported failed (`long`). It is there for audio that runs
# away, not to time normal speech: the rig's units took a median 62 ms of sound a character, and 8 of 83 took
# far more ("I have it." was 4.05 s), most of them lines of 11 characters or fewer, hence the floor. 0.155 is
# 2.5 times the median (it was 1.8 times, of the written characters), per character as spoken (`spoken_chars`).
LONG_MIN_S, LONG_PER_CHAR_S = 1.5, 0.155
# What a written character is worth in spoken ones. A digit or a symbol is a word or more aloud ("$1,249.50"
# is "one thousand two hundred forty-nine dollars and fifty cents": 9 written, 57 said), and a capital may be a
# letter spelled out ("PR", "API"). Both err long: the limit is a ceiling.
SPOKEN_SYMBOL, SPOKEN_CAPITAL = 8, 3


def spoken_chars(unit: str) -> int:
    """About how many characters `unit` is when said aloud. A lower-case letter, a space, an apostrophe, a
    hyphen, a quote or a bracket is 1, and so is punctuation that ends a word. A capital is SPOKEN_CAPITAL.
    Anything else is SPOKEN_SYMBOL: a digit, a currency or other symbol, and punctuation inside a token (the
    dot of "1.5" or of "example.com", a slash)."""
    # ponytail: no number or URL is read out here, a count by character. A text normaliser if a receipt's
    # `overMs` ever shows the count far off.
    n = 0
    for i, c in enumerate(unit):
        if c.islower() or c.isspace() or c in "'’-\"“”()":
            n += 1
        elif c.isupper():
            n += SPOKEN_CAPITAL
        elif c in ".,;:!?" and not unit[i + 1 : i + 2].isalnum():
            n += 1
        else:
            n += SPOKEN_SYMBOL
    return n


def sound_limit(unit: str) -> float:
    """Seconds of sound `unit` may have before it is stopped as `long`."""
    return max(LONG_MIN_S, LONG_PER_CHAR_S * spoken_chars(unit))


@dataclass
class _Unit:
    """One sentence on its way to the track: what the audio node saw of it. Its receipt (`_DrivenLive._speak`)."""

    order: int | None
    index: int
    allowed: Callable[[], bool]  # the controller's permission, asked again as its first audio, and its first audible audio, is about to go out
    audible: Callable[[], None] = lambda: None  # told once, as its first frame with sound in it goes out
    frames: int = 0
    seconds: float = 0.0
    loud: float = 0.0  # seconds of frames with sound in them
    lead: float = 0.0  # seconds of quiet frames before the first one with sound: when the room began to hear it
    sounded: float = 0.0  # when the first frame with sound passed (monotonic)
    revoked: bool = False
    limit: float = float("inf")  # seconds of sound it may have (`sound_limit`)
    stop: Callable[[str], None] = lambda why: None  # ends it as failed: "unsent", "no_sound" or "long"
    why: str = ""  # why it was stopped, if it was


class _Mouth(Agent):
    """A driven voice's agent. The model's audio passes here on its way to the track, so each unit's receipt
    counts what was really sent, and a unit whose permission went while the model was generating sends nothing."""

    unit: _Unit | None = None

    async def realtime_audio_output_node(self, audio, model_settings):
        async for frame in audio:
            if (m := self.unit) is not None:
                data = frame.data
                sound = max(max(data), -min(data)) >= LOUD
                # Asked at its first frame, and again before its first frame with sound in it: the model may
                # lead with quiet frames, and a person may begin in that time. The quiet ones between are let by.
                if m.revoked or ((not m.frames or sound) and not m.loud and not m.allowed()):
                    m.revoked = True
                    return
                m.frames += 1
                m.seconds += frame.duration
                if sound:
                    if not m.loud:
                        m.sounded = time.monotonic()
                        m.audible()
                    m.loud += frame.duration
                    if m.loud > m.limit:
                        m.stop("long")  # no more of it reaches the track
                        return
                elif not m.loud:
                    m.lead += frame.duration
            yield frame


class _DrivenLive:
    """One agent's voice, as huddle.Live: a Realtime model with no ears and no turn detection, so it is silent
    until the worker orders it to speak. A voice that hears answers whatever it hears and cannot be told to
    listen without answering (GPT-Live, 10-07 rig sales call).

    Orders run one after the other on one chain, so two lines never overlap. What it is handed is said one
    sentence at a time. Before each one it asks the worker (`may`), and each ends in a receipt of what went
    to the track (`_speak`). Then the order itself is reported over, also when it failed (`say`): the worker
    never waits on speech that is not coming. `interrupt` stops the speech and drops what was not yet
    started. Whether and when it speaks is the worker's to decide (`Huddle._pump`, `Huddle.may_say`): there
    is no floor rule here."""

    def __init__(
        self,
        agent_id: str,
        session: AgentSession,
        agent: Agent,
        room: rtc.Room,
        # huddle.on_unit for this agent: (order, index, text, outcome, evidence), one per unit it began
        said=lambda order, index, text, outcome, evidence: None,
        ended=lambda order, failed: None,  # huddle.on_playback_ended: one `say` is over, by its order id
        may=lambda order, index: True,  # huddle.may_say: the worker's permission for one unit
        audible=lambda order, index: None,  # huddle.on_unit_audible: a unit's first sound went to the track
    ) -> None:
        self.agent_id, self.track_name = agent_id, f"agent-{agent_id}"
        self._session, self._agent, self._room = session, agent, room
        self._said, self._ended, self._may, self._audible = said, ended, may, audible
        self._tail: asyncio.Task | None = None
        self._epoch = 0  # raised by interrupt: a speech order queued under an older one is dropped
        self._cut_at: float | None = None  # when the last interrupt was sent, until its audio is known stopped
        self._long: str | None = None  # the unit last stopped as `long`, until it is said or another unit is
        # The Realtime plugin reports tokens, not session seconds (`session_duration` is 0): wall clock, open to
        # close, is what is counted against the call's budget.
        self._opened, self._closed = time.monotonic(), None

    def _order(self, do, keep: bool = False) -> None:
        """`do()` after every order before it. `keep`: something to know, which an interrupt does not drop."""
        prior, epoch = self._tail, self._epoch

        async def run() -> None:
            if prior is not None:
                await asyncio.wait([prior])
            if self._closed is not None or (not keep and epoch != self._epoch):
                return
            try:
                await do()
            except Exception:
                logger.exception("huddle driven voice order failed")

        self._tail = asyncio.create_task(run())

    async def _speak(self, order: int | None, index: int, unit: str) -> str:
        """One unit, word for word, then its receipt: `said` (audible audio went to the track and played to its
        end), `cut` (interrupted), `revoked` (permission went before its first audible audio, so none was sent),
        `silent` (nothing audible was sent: a failure) or `long` (far more sound than its words need, and it
        was stopped: a failure, once for a unit). A failure's evidence says `why`. The receipt carries the order and
        unit it is for and what the track was sent, never the model's transcript of itself."""
        # A unit stopped as `long` is not limited on its next try: the limit that cut it is an estimate, and
        # the same one would cut the same unit again, try after try, until it went out in writing.
        free, limit = unit == self._long, sound_limit(unit)
        m = self._agent.unit = _Unit(
            order, index, lambda: self._may(order, index), lambda: self._audible(order, index),
            limit=float("inf") if free else limit,
        )  # fmt: skip
        began, loop, handle = time.monotonic(), asyncio.get_running_loop(), None

        def stop(why: str) -> None:
            # Stopped here, so a dead order costs the room FIRST_SOUND_S and not however long the model goes on.
            if not m.why and handle is not None and not handle.done():
                m.why = why
                handle.interrupt(force=True)

        def sent() -> None:  # its response.create went to the model: the one moment its first sound is timed from
            nonlocal watch
            watch.cancel()
            watch = loop.call_later(FIRST_SOUND_S, lambda: m.loud or stop("no_sound"))

        m.stop = stop
        watch = loop.call_later(SEND_WAIT_S, lambda: stop("unsent"))
        realtime = None
        with contextlib.suppress(RuntimeError):  # (no realtime session: SEND_WAIT_S bounds it)
            realtime = self._agent.realtime_llm_session
            realtime.sent = sent
        handle = self._session.generate_reply(instructions=SAY + unit)
        try:
            await handle  # done when the unit is said, or cut. It never raises.
        finally:
            watch.cancel()
            self._agent.unit = None
            if realtime is not None:
                realtime.sent = None
        if m.revoked:
            outcome = "revoked"
        elif m.why == "long":
            outcome = "long"
        elif m.why or (not handle.interrupted and m.loud < AUDIBLE_S):
            outcome = "silent"
            m.why = m.why or ("quiet" if m.frames else "no_frames")  # it ended uncut: frames with no sound, or none
        else:
            outcome = "cut" if handle.interrupted else "said"
        evidence = {
            "frames": m.frames,
            "audioMs": round(m.seconds * 1000),
            "loudMs": round(m.loud * 1000),
            # what the room can have heard: frames may pass here ahead of their playout, and a cut drops those
            "heardMs": round(min(m.loud, time.monotonic() - m.sounded) * 1000) if m.loud else 0,
            "leadMs": round(m.lead * 1000),
            "tookMs": round((time.monotonic() - began) * 1000),
        }
        if m.why:
            evidence["why"] = m.why
        if free and m.loud > limit:
            evidence["overMs"] = round((m.loud - limit) * 1000)  # past the limit its last try was stopped at
            logger.warning("huddle driven voice: a unit ran %d ms over its sound limit on its second try", evidence["overMs"])
        if outcome == "long":
            self._long = unit
        elif outcome == "said" or not free:
            self._long = None
        if self._cut_at is not None and outcome != "said":
            evidence["stopMs"] = round((time.monotonic() - self._cut_at) * 1000)  # interrupt sent -> audio stopped
        self._cut_at = None
        self._said(order, index, unit, outcome, evidence)
        return outcome

    def say(self, text: str, order: int | None = None, first: int = 0) -> None:
        # One sentence at a time, each with the worker's permission: a cut is known to the sentence, and what
        # follows a cut, an interrupt or a refusal is not said. `first`: the index of its first unit in the
        # worker's utterance. Every order ends in one receipt, whatever happened to it. It is kept through an
        # interrupt for that: the receipt of an order that never started is still owed.
        epoch = self._epoch

        async def play() -> None:
            failed = False
            try:
                for index, unit in enumerate(units(text), first):
                    if epoch != self._epoch or not self._may(order, index):
                        break
                    outcome = await self._speak(order, index, unit)
                    if outcome != "said":
                        failed = outcome in ("silent", "long")
                        break
            except Exception:
                logger.exception("huddle driven voice could not say its line")
                failed = True
            self._ended(order, failed)

        self._order(play, keep=True)

    def append_instructions(self, text: str) -> None:
        # Part of the session's instructions, so a reconnect, which sends them again, keeps the rule.
        self._order(lambda: self._agent.update_instructions(f"{self._agent.instructions}\n\n{text}"), keep=True)

    def interrupt(self):
        self._epoch += 1
        self._cut_at = time.monotonic()
        return self._session.interrupt(force=True)

    def usage_seconds(self) -> float:
        return (self._closed if self._closed is not None else time.monotonic()) - self._opened

    def _stop(self) -> None:
        if self._closed is None:
            self._closed = time.monotonic()

    #: The call's mouths by agent (`start_live`), when this one is kept open for the call.
    kept: dict | None = None

    async def aclose(self) -> None:
        """The worker's voice is over (`Huddle._close_live`), and it is billed to here. A kept mouth's session
        stays open, silent, for the next time its agent speaks: 15 rig calls opened a session 24 times, 0.4-1.0 s
        each, and every silent unit was the first or second after an open (10-08). An idle Realtime session
        costs nothing: it is billed by the token."""
        self._stop()
        if self.kept is None or self.kept.get(self.agent_id) is not self:
            return await self.close()
        with contextlib.suppress(RuntimeError):  # (a session that is not running has nothing to stop)
            self.interrupt()

    def reopen(self) -> _DrivenLive:
        self._opened, self._closed, self._cut_at = time.monotonic(), None, None
        return self

    async def close(self) -> None:
        """The session itself ends: its call is over, or it is not a kept one."""
        self._stop()
        if self.kept is not None and self.kept.get(self.agent_id) is self:
            del self.kept[self.agent_id]
        await self._session.aclose()
        await _unpublish(self._room, self.track_name)


def noise_filter():
    """LiveKit Cloud's background noise model (Krisp NC, no extra cost), on everything a person's microphone
    sends before the transcriber and the voice hear it: music, a fan or a street is not speech, and the
    transcriber invents words for it (10-04 prod Meet: "Hello, guys." from a room with a TV on). It keeps
    all speech, a second person's too; removing other voices is the paid voice-isolation model."""
    return noise_cancellation.NC()


def room_options(agent_id: str, speaker: str) -> room_io.RoomOptions:
    # A voice takes no audio at all (it has no ears) and no text input (people type into the room, not to the
    # voice). Pinned to the speaker all the same, so RoomIO never auto-links whoever comes first.
    return room_io.RoomOptions(
        participant_identity=speaker,
        close_on_disconnect=False,
        text_input=False,
        audio_input=False,
        audio_output=room_io.AudioOutputOptions(track_name=f"agent-{agent_id}"),
    )


async def _open(session: AgentSession, agent: Agent, room, huddle: Huddle, agent_id, speaker) -> None:
    """Start `session` in the room, reporting its state to the huddle. What it said it reports itself, unit by
    unit (`_DrivenLive._speak`)."""
    session.on(
        "agent_state_changed", lambda ev: huddle.on_agent_state(agent_id, ev.new_state)
    )
    try:
        # record=False: a huddle leaves nothing behind, whatever the LiveKit Cloud project's recording default.
        await session.start(
            agent=agent,
            room=room,
            room_options=room_options(agent_id, speaker),
            record=False,
        )
    except BaseException:
        with contextlib.suppress(Exception):
            await session.aclose()
            await _unpublish(room, f"agent-{agent_id}")
        raise


async def _start_driven(room, huddle: Huddle, agent_id, persona, voice, speaker, seed) -> _DrivenLive:
    agent = _Mouth(instructions=f"{persona}\n\n{DRIVEN}", chat_ctx=_seeded(seed))
    # turn_detection=None and no audio input: nothing but an order makes it speak. No vad: nothing to detect.
    session = AgentSession(
        llm=_DrivenModel(model="gpt-realtime-2.1-mini", voice=realtime_voice(voice), turn_detection=None)
    )
    await _open(session, agent, room, huddle, agent_id, speaker)
    live = _DrivenLive(
        agent_id,
        session,
        agent,
        room,
        said=lambda order, index, text, outcome, evidence: huddle.on_unit(
            agent_id, order, index, text, outcome, evidence
        ),
        ended=lambda order, failed: huddle.on_playback_ended(agent_id, order, failed),
        may=lambda order, index: huddle.may_say(agent_id, order, index),
        audible=lambda order, index: huddle.on_unit_audible(agent_id, order, index),
    )

    # An unrecoverable realtime error closes the session under us: end the exchange then, not on the next call.
    def closed(_ev) -> None:
        live._stop()  # billed to here, whoever closed it
        if live.kept is not None and live.kept.get(agent_id) is live:
            del live.kept[agent_id]  # a kept mouth that died: the next one is opened new
        huddle.on_live_closed(live)

    session.on("close", closed)
    return live


async def start_live(
    room, huddle: Huddle, *, agent_id, persona, voice, speaker, seed, mouths: dict | None = None
) -> _DrivenLive:
    """Every voice is a mouth: the worker listens, /route and the agent decide, the voice says it. `mouths`: the
    call's mouths by agent. An agent's mouth opens once in a call and is kept until the call ends
    (`_DrivenLive.aclose`): the worker opens and closes its voice as before, over the same session. Its seed is
    nothing to a mouth, which never reads the conversation."""
    # ponytail: opened at its agent's first line, not at the join. Opening (and one warm-up unit) as the
    # worker joins would take the open off the first line too: a follow-up, it needs a look at the bill.
    if mouths is not None:
        if (live := mouths.get(agent_id)) is not None and live._closed is not None:
            return live.reopen()
        live = mouths[agent_id] = await start_live(
            room, huddle, agent_id=agent_id, persona=persona, voice=voice, speaker=speaker, seed=seed
        )
        live.kept = mouths
        return live
    try:
        return await _start_driven(room, huddle, agent_id, persona, voice, speaker, seed)
    except Exception:
        if voice == FALLBACK_VOICE:
            raise
        # a voice the model no longer accepts must not cost the agent its voice altogether
        logger.warning(
            "huddle voice %s refused; retrying with %s",
            voice,
            FALLBACK_VOICE,
            exc_info=True,
        )
        emit_metric("live.voice.fallback", 1, "Count")
        return await _start_driven(room, huddle, agent_id, persona, FALLBACK_VOICE, speaker, seed)


def person(p: rtc.Participant) -> bool:
    """A human of the call. A Meet person (`meet:<roomId>:<participantId>`) or a mixed Meet (`meet:<roomId>`, an
    older Recall bot) counts; the bot's own page (`meetbot:<roomId>`) publishes no audio and is nobody."""
    if p.identity.startswith("meetbot:"):
        return False
    return p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD or p.identity.startswith("meet:")


class _Room:
    def __init__(self, ctx: JobContext) -> None:
        self._ctx, self._tasks = ctx, set()
        self.closed = False  # the entrypoint loop returns on its own, before LiveKit has to cancel it

    def publish(self, topic: str, payload: dict, to: list[str]) -> None:
        lp = self._ctx.room.local_participant
        t = asyncio.create_task(
            lp.publish_data(json.dumps(payload), topic=topic, destination_identities=to)
        )
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def set_attributes(self, attrs: dict[str, str]) -> None:
        t = asyncio.create_task(self._ctx.room.local_participant.set_attributes(attrs))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def shutdown(self) -> None:
        self.closed = True
        self._ctx.shutdown("no humans left in the huddle")


async def _on_request(req: JobRequest) -> None:
    await req.accept(identity=AGENT_IDENTITY, name=WORKER_DISPLAY_NAME)


# In livekit-agents 1.8.11 this kwarg wins over LIVEKIT_AGENT_NAME (worker.py rtc_session); the Dockerfile sets
# LIVEKIT_AGENT_NAME=lightbulb-huddle too, so both agree.
# HUDDLE_RIG_AGENT_NAME: the multi-voice test rig runs this worker on a laptop under its own dispatch name, so no
# real call reaches it. Unset everywhere else.
@server.rtc_session(agent_name=os.environ.get("HUDDLE_RIG_AGENT_NAME", "lightbulb-huddle"), on_request=_on_request)
async def entrypoint(ctx: JobContext) -> None:
    # A phone call: the plan allows one deployed agent, so the SIP dispatch rules target this one and name their
    # rooms `phone-{env}-_<caller>_<random>`. They send no job metadata.
    if ctx.job.room.name.startswith("phone-"):
        return await phone.entrypoint(ctx)
    meta = json.loads(ctx.job.metadata)
    # A Google Meet: one listening realtime session (meet.py; MEET_ENGINE openai, or gemini). MEET_ENGINE=huddle
    # is the old mouths, until the new one has a live meeting behind it.
    if meta.get("source") == "meet" and os.environ.get("MEET_ENGINE", "openai") != "huddle":
        return await meet.run(ctx, meta)
    # Live in a member's DM with the assistant: the Router session (owner.py), not the mouths below.
    if meta.get("mode") == "owner":
        return await owner.run(ctx, meta)
    started = time.monotonic()
    usage = {"stt": 0.0}
    emitted = {"stt": 0.0, "agent": 0.0, "gptlive": 0.0}
    recorder = record.start(meta["roomId"])  # HUDDLE_RECORD_DIR: this call's inputs, for an offline replay
    backend = recorder.backend(Backend(meta["roomId"], meta["ownerId"], meta["roomToken"]))
    # streaming: interims tell a listening sound from words as they come (huddle `_only_listening`)
    stt_engine = openai.STT(model="gpt-live-transcribe", use_realtime=True, turn_detection=MEET_VAD)

    @stt_engine.on("metrics_collected")
    def _stt_metrics(m) -> None:
        usage["stt"] += m.audio_duration

    mouths: dict[str, _DrivenLive] = {}  # each agent's mouth, open from its first line to the end of the call

    async def live_factory(**kw) -> _DrivenLive:
        return await start_live(ctx.room, huddle, mouths=mouths, **kw)

    def flush_metrics() -> None:
        # deltas since the last flush, so a crashed job loses at most a minute
        now = {
            "stt": usage["stt"],
            "agent": time.monotonic() - started,
            "gptlive": huddle.gptlive_seconds(),
        }
        # a voice mid-close or a failed usage read never sends a negative delta
        now["gptlive"] = max(now["gptlive"], emitted["gptlive"])
        for k in now:
            emit_metric(
                "huddle.gptlive.open_seconds"
                if k == "gptlive"
                else f"huddle.{k}.seconds",
                now[k] - emitted[k],
            )
        emitted.update(now)

    room = _Room(ctx)
    huddle = Huddle(
        room=room,
        backend=backend,
        live_factory=recorder.factory(live_factory),
        on_vocabulary=lambda prompt: stt_engine.update_options(
            prompt=prompt
        ),  # open streams take it in place
        # a Google Meet bridged in by a Recall bot (the meeting dispatch)
        setting="meeting" if meta.get("source") == "meet" else "live",
        writer=recorder.writer(write_line),
    )
    recorder.inputs(huddle)
    tasks: set[asyncio.Task] = set()

    def _done(t: asyncio.Task) -> None:
        tasks.discard(t)
        if not t.cancelled() and (error := t.exception()) is not None:
            logger.error("huddle task failed", exc_info=error)

    def spawn(coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(_done)
        return t

    def humans() -> dict[str, str]:
        return {
            p.identity: p.name or p.identity
            for p in ctx.room.remote_participants.values()
            if person(p)
        }

    async def transcribe(track: rtc.Track, p: rtc.RemoteParticipant) -> None:
        stream = stt_engine.stream()
        audio = rtc.AudioStream.from_track(track=track, noise_cancellation=noise_filter())

        async def push() -> None:
            async for ev in audio:
                stream.push_frame(ev.frame)
            stream.end_input()

        pusher = asyncio.create_task(push())
        try:
            async for ev in stream:
                try:
                    # the transcriber's own speech detector: it answers to speech, not to sound, and it
                    # fires before any word is transcribed (who is talking, for the floor)
                    if ev.type == stt.SpeechEventType.START_OF_SPEECH:
                        huddle.on_speech(p.identity, True)
                    elif ev.type == stt.SpeechEventType.END_OF_SPEECH:
                        huddle.on_speech(p.identity, False)
                    if not ev.alternatives:
                        continue
                    if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                        await huddle.on_final_transcript(p.identity, p.name or p.identity, ev.alternatives[0].text)
                    elif ev.type == stt.SpeechEventType.INTERIM_TRANSCRIPT:  # use_realtime: cumulative, ~0.5 s apart
                        huddle.on_interim_transcript(p.identity, p.name or p.identity, ev.alternatives[0].text)
                except Exception:  # one bad event never ends this person's transcription
                    logger.exception("huddle transcript handling failed")
        except Exception:
            huddle.stt_stopped(p.identity)  # no transcriber now: nobody is held as talking
            raise
        finally:
            await utils.aio.cancel_and_wait(pusher)
            await audio.aclose()
            await stream.aclose()

    stt_tasks: dict[str, asyncio.Task] = {}  # identity -> that person's transcription

    def stop_stt(identity: str) -> None:
        huddle.stt_stopped(identity)
        if (t := stt_tasks.pop(identity, None)) is not None:
            t.cancel()

    def start_stt(track: rtc.Track | None, p: rtc.Participant) -> None:
        if track is None or track.kind != rtc.TrackKind.KIND_AUDIO or not person(p):
            return
        stop_stt(p.identity)
        stt_tasks[p.identity] = spawn(transcribe(track, p))

    def on_track(
        track: rtc.Track, pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant
    ) -> None:
        if (
            not pub.muted
        ):  # a muted mic is not transcribed (or paid for); unmute starts it
            start_stt(track, p)

    def on_muted(p: rtc.Participant, pub: rtc.TrackPublication) -> None:
        if pub.kind == rtc.TrackKind.KIND_AUDIO and isinstance(
            p, rtc.RemoteParticipant
        ):
            stop_stt(p.identity)

    def on_unmuted(p: rtc.Participant, pub: rtc.TrackPublication) -> None:
        if isinstance(p, rtc.RemoteParticipant):
            start_stt(pub.track, p)

    def rpc_talk(data: rtc.RpcInvocationData) -> str:
        # caller_identity is set by the LiveKit server; it is the only speaker identity we trust.
        # Answer at once: a summon outlasts the RPC's 10 s response timeout.
        spawn(huddle.talk_start(data.caller_identity, data.payload))
        return "ok"

    async def on_shutdown(_reason: str = "") -> None:
        await (
            huddle.clear_presence()
        )  # first: a stale "Live" marker outlives everything below
        await huddle.end_exchange(force=True)  # a request still open must not hold the call's window
        for live in list(mouths.values()):  # the last person left: the kept mouths close with the call
            with contextlib.suppress(Exception):
                await live.close()
        flush_metrics()
        await backend.aclose()

    ctx.add_shutdown_callback(on_shutdown)
    ctx.room.on("track_subscribed", on_track)
    ctx.room.on("track_unsubscribed", lambda _t, _pub, p: stop_stt(p.identity))
    ctx.room.on("track_muted", on_muted)
    ctx.room.on("track_unmuted", on_unmuted)
    ctx.room.on(
        "participant_connected", lambda _p: spawn(huddle.on_participants(humans()))
    )
    ctx.room.on(
        "participant_disconnected", lambda _p: spawn(huddle.on_participants(humans()))
    )
    ctx.room.on(
        "data_received",
        lambda pkt: huddle.on_data(pkt.participant, pkt.topic, pkt.data),
    )
    # the SFU's speaking set: sound, which with the transcriber's speech detector is who is talking
    ctx.room.on(
        "active_speakers_changed",
        lambda speakers: huddle.on_active_speakers({p.identity for p in speakers}),
    )
    await ctx.connect()
    # LiveKit sends a participant's attribute changes at once only while it publishes a track; one that does
    # not is batched every 3 s (measured on the rig: a tile went to "working" 3.0 s after the worker wrote it).
    # The worker publishes only while a voice is open, so it holds one silent track for its whole job.
    presence = rtc.LocalAudioTrack.create_audio_track("presence", rtc.AudioSource(48000, 1))
    await ctx.room.local_participant.publish_track(presence, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_UNKNOWN))
    # (no talk_end: the clients stopped sending it, and a voice that only says what it is ordered to has no
    # use for a release)
    ctx.room.local_participant.register_rpc_method("talk_start", rpc_talk)
    await huddle.on_participants(humans())
    for n in itertools.count(1):
        if room.closed:
            return  # shutdown callbacks run next; LiveKit no longer has to cancel this loop
        await asyncio.sleep(1)
        await huddle.tick()
        if n % 60 == 0:
            flush_metrics()


if __name__ == "__main__":
    cli.run_app(server)
