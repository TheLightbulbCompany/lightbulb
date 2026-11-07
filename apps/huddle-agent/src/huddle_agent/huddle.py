"""The Lightbulb huddle state machine: follow along, open the named agent's own voice, run one
exchange, go quiet again. Each agent speaks for itself (Option D).

Every voice is a mouth, in a Google Meet and in the app alike (owner rulings 10-07 and 10-12): it has no
ears. The worker transcribes, /route decides, the agent's Hermes run answers, and the voice only says the
lines it is ordered to say, one sentence at a time (`_pump`).

Everything LiveKit-shaped is injected (`room`, `live_factory`), so this runs against fakes in tests;
agent.py is the thin glue onto the real room and the Realtime sessions.

State:
- `_window`: the metering window (C1 /summon .. /end). It opens with the first voice and ends when no
  voice is open or joining. It owns no runs.
- `_voice`: the floor-holder's voice (`live` is None while it joins).
- `_runs`: every voice run by run id. Closing a voice or the window never stops one.
- `_ledger`: everything the room's mouths still have to say, by agent (`Utterance`).
- `_queue`: (agent, speaker, line) still to answer once the floor settles: the rest of a multi-name request
  in the order named, and requests other people made while a voice was open (C4 `lightbulb.queue`).
- `_held`: a line /route called unfinished (`hold`), merged with that speaker's next final.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from huddle_agent import gate
from huddle_agent.backend import BackendError
from huddle_agent.frames import WORKING_CHARS, Run, Runs
from huddle_agent.request_list import ENDED, LINE_CHARS, OPEN, Request, RequestList
from huddle_agent.transcript import (
    Transcript,
    address,
    backchannel,
    clause_openers,
    ghost_line,
    is_question,
    no_post_command,
    only_name,
    pause,
    real_speech,
    stop_all,
    stop_bare,
    stop_request,
    opens_with_person,
    trails_off,
    stop_target,
    strip_wake,
    stt_prompt,
    withdraws,
)

logger = logging.getLogger("huddle")

IDLE_SECONDS = 20  # no speech from the speaker and no running run of the floor-holder -> the exchange ends
FIRST_JOIN_GRACE = 60  # join dispatches the worker before the browser connects: wait for the first human
# The last human left: leave after this long, re-checking who is here. The startup snapshot and the
# first participant_connected event run concurrently; the snapshot (empty) finishing second once
# read as "everyone left" and shut a two-person huddle down 1 s after it started (10-02).
EMPTY_ROOM_GRACE = 3
ERROR_TOPIC = "huddle.error"
FRAME_TOPIC = "call.frame"  # C3
NOTICE_TOPIC = "live.notice"  # C5
MAX_UTTERANCE = 4_000  # TurnBody.utterance
MAX_TRANSCRIPT = 12_000  # TurnBody.transcript
ROOM_IDLE_SECONDS = (
    15 * 60
)  # nobody has spoken this long: the worker leaves (and stops paying for STT)
IDLE_ENDED = "idle_ended"  # huddle.error code: the web leaves and says why
TURN_RETRY_BUDGET = (
    75  # seconds a turn keeps retrying a waking workspace before apologising
)
TURN_RETRY_MAX_DELAY = 8
# ponytail: no push on membership change; tiles already follow members client-side
TEAM_REFRESH_S = 60
NEWS_TTL = (
    10 * 60
)  # an answer nobody asked for, unsaid this long, goes to the room in writing
CANT_REACH = "Sorry, I couldn't reach my backend for that."
FAILED_POST = "I could not finish this on the call. Ask me again here."  # understanding §4.8: a failure nobody heard
NOT_STARTED_POST = "I did not get to this on the call. Ask me again here."  # §4.5: still queued when the call ended
LATE_REQUEST_CHARS = 2_000  # LateAnswerBody.request max (backend huddles.py)
DELIVERY_TRIES = (
    3  # a requested answer whose voice failed to open this often goes to the room
)
ASKER_GONE_S = (
    60  # engine §3.5: its asker left and no pause came this long: the room gets it
)
TRANSCRIPT_FLUSH_S = (
    10  # how often the lines said since the last batch go to the backend
)
QUEUE_SETTLE = 1  # the floor-holder's driven run ended and it stopped speaking this long ago: the next answers
REPEAT_SECONDS = 10  # live.wake.repeat: the same person names the same agent this soon after a voice_failed
VOICE_NOTE_CHARS = 1_800  # append_instructions stays under ~500 tokens (D §7)
PRESENCE_TIMEOUT = (
    3  # the last (empty) presence must land before LiveKit cancels the job
)
# ponytail: inferred; an unfinished line (/route "hold") merges with the same speaker's next final this soon
HOLD_SECONDS = 15
HOLD_APPLY_S = 4.0  # understanding §4.4: a held line its speaker said nothing more after is applied as it is
ACK_QUIET_S = 1.0  # understanding §4.6: the open voice says who is on what once its speaker is this quiet
SAID_ORDER_WAIT_S = (
    10.0  # a line waits this long at most for its speaker's earlier line to be applied
)
MISSED_SECONDS = 10  # live.route.missed: nobody answered, then the same person names an agent or Lightbulb
UNWANTED_SECONDS = 5  # live.route.unwanted: a Jev-routed voice the asker stops or switches away from this soon
ROUTE_LINES = 30  # the backend reads the transcript's last 30 lines (voice_calls._transcript_history)
# Meeting mode (a Google Meet bridged in by a Recall bot, one `meet:<roomId>` participant carrying the mix):
MEETING_IDLE_SECONDS = 60 * 60
UNCLAIMED_QUESTION_S = 3.5  # a question that names nobody: a person may answer it first
SPEAKER_TOPIC = "meet.speaker"  # {participantId, name, speaking, at}, relayed by the backend from Recall
# ponytail: fixed slack for Recall's event lag vs our STT final; tune from mislabelled lines
SPEAKER_SLACK_S = 1.0
SPEAKER_WINDOWS = 50
# ponytail: an utterance start older than this is stale (an STT stream that never finalised)
MEET_SPAN_MAX_S = 30
# Recall on Google Meet sends `on` only when the active speaker changes and no `off` for a lone speaker: an open
# span lasts until someone else's `on`, its own `off` or its `leave`, however long that is (a presentation).
MEET_SHARE_S = (
    1.0  # a second speaker is named on a line only when they spoke this long in it
)
# a meeting voice closes this long after its last word or its speaker's last line to it (the session is billed
# by the second). What its agent still has to say is in the room's ledger, not in the voice. A mouth's session
# outlives its voice: agent.py keeps it open for the call, so the next voice of that agent opens at once.
MEET_FOLLOWUP_S = 10.0
# a ready answer waits for a real pause in a meeting, not a breath between two sentences (Live: REQUEST_QUIET_S)
# No wait reserves the silence a sentence needs: a 5.8 s sentence does not fit a 6.8 s pause once 3 s of it
# are spent waiting. Tried instead and not done (10-08, replay, 200 seeds x 15 scenarios, current / fast-eot
# transcriber): the floor being free (every turn resolved, no person given the next one), then a bid of a
# few words ("I have that."), a 1 s opening, and the first sentence. The bid and its opening cost as much of
# the pause as the wait did, so no answer fitted that had not before (sidetalk: still 100% / 100% late),
# and the bids fell into the 2-4 s gaps between people's lines: speech between people's lines went from
# 24-33% to 89-97% of replays (14-22% to 97-100%), failing check-seeds 1587 -> 2355 (749 -> 1497). Without
# the bid, the first sentence as soon as the floor is free: 1587 -> 1779 (749 -> 821), and the answer in
# time in a third of replays with the fast transcriber only. What makes the sentence fit is knowing the
# turn ended sooner (the transcriber) and a shorter first sentence (the answer's writer), not this rule.
MEET_QUIET_S = 3.0
# A mouth hears nobody, so the worker pauses it for someone whose turn over it has lasted this long, counted
# from the first sign of their speech. A shorter sound (a cough, a word) never pauses it. The pause is only
# acoustic: what the talk was is decided from its words once they are known (`Huddle._talked_over`), and
# listening sounds ("mm-hm, yeah") resume it at once.
TALK_OVER_S = 0.5
# The SFU's level for a person drops between their words and comes back late. While the transcriber's speech
# detector still hears them, they are talking for this long after the SFU last did.
LEVEL_FRESH_S = 1.5
# The transcriber's detector hears a person this long after they begin, at most. A sentence whose first
# sound went out less than this before a person was heard (or has not gone out yet) began over them, or
# with them: it yields at once, with no TALK_OVER_S of waiting. Counted from the first frame with sound in
# it (`Huddle.on_unit_audible`): a unit is permitted, and may send quiet frames, a second before that.
# Not for a person whose words so far are only a listening sound (`Huddle._only_listening`).
PREROLL_S = 0.6
# A piece a person begins this soon after their last piece ended (the detector's stop to its next start; the
# transcriber ends a piece at 400 ms of silence) is more of the same turn. Hermes merges a split message the
# same way before it dispatches once. A turn that already made requests is routed again as a whole, and what
# it asks now replaces what it asked (`Huddle._amend_turn`): it never starts a second request.
# ponytail: inferred from the 10-09 prod Meet (gaps of 0.4-0.6 s); tune from `turn` ids in the route log
TURN_GAP_S = 1.0
TRAIL_S = 8.0  # an unfinished turn ("Can one of you tell me...", "Friday.") is joined to a rest that comes this soon
CALL_ON_S = 3.0  # a name said alone, then this much silence from its speaker: the agent was called on
# The room has been without talk this long: sound has settled and speech may start. An answer whose asker is
# not plainly waiting for it (`_hold`) waits MEET_QUIET_S instead.
SETTLE_S = 0.3
# How long a line stays worth saying, by what it is. An answer has no time limit while its asker is here.
EXPIRES_S = {"ack": 10.0, "status": 6.0, "social": 6.0}
# A safety net, and no rule of conversation: an exchange ends when someone else takes a turn that is not
# part of it (`Huddle._engagements`). One that nothing ended, with nothing said between its person and its
# agent this long, is closed so the room does not keep it for the whole call.
ENGAGE_SAFETY_S = 300.0
# A sentence that was cut is not said, however much of it was audible: it is said whole when its utterance
# goes on (`Huddle.on_unit`). A sentence keeps what it is about for its end ("...costs fifty dollars per
# month"), and seconds guessed from characters cannot say its last word was heard (10-08 review: "The
# monthly price is fifty" was counted said and "dollars" never was). What was audible goes on the record
# with this mark, so the record shows a piece and not a sentence that was said.
CUT_MARK = " [cut off]"
# A paused utterance is not said on before the receipt of what was cut: it says where its cursor is. This
# long at most, for a voice that died with the receipt.
RECEIPT_WAIT_S = 1.0
# Characters a second a mouth speaks at, until this call's own units have measured it (`Huddle._audible`).
SPEECH_CPS = 15.0
PLAYBACK_TRIES = 3  # an utterance whose playback ended early this often in a row is not tried aloud again
LATE_RETRY_MAX_S = 30  # a written answer whose post failed is tried again after 1 s, 2 s, 4 s ... up to this
# A person stopped speaking and no transcript of it came this long: none is coming (`Huddle._final_timeout`).
# It bounds only that. It never ends speech still going on, and never stands in for a /route answer.
FINAL_WAIT_S = 3.0
# The detector says a person still speaks and the SFU has heard no sound from them for this long: its stop
# event is taken for lost. Their speech then waits for its transcript like any that ended (FINAL_WAIT_S).
# Long enough for a pause inside a sentence, which the detector may hold through.
DETECTOR_LOST_S = 5.0
# What a mouth says that is not an answer: fixed lines the worker picks, no model writes them. A written
# acknowledgement read out the worker's notes ("Vera's checking...", 10-07 rig) and a model given "like 'On it.'"
# said "On it." every time (10-05 prod Meet). No line reads the task back: fitting a request into a sentence
# took a word list, and still came out wrong.
ACKS = (
    "Sure, I'm on it now.",
    "Okay, let me look into that.",
    "Got it. Give me a moment.",
    "Yes, I'll have that for you shortly.",
    "Understood. I'll come back with it.",
    "Alright, I'm working on it now.",
    "I have it. Back with you shortly.",
    "Right, let me check.",
)
STILL_WORKING = (
    "I'm still working on that.",
    "Still on it, a little longer.",
    "That one is still running.",
    "I'm not done with that yet.",
    "Still checking. A little longer.",
)
QUEUED_LINES = (
    "I'll get to that right after what I'm finishing.",
    "That's next for me, once I finish this one.",
    "I have one thing to finish first, then that.",
)
# A follow-up that went into the run already working on its request: said at once, so its speaker knows it
# was heard. The answer to it comes with that run's.
STEER_ACKS = ("Adding that.", "Got it, that too.", "Okay, that as well.")
HERE = (
    "Yes, I'm here.",
    "I'm here.",
    "Yes, go ahead.",
)  # called on by its name alone, with no open work
COULD_NOT = (
    "Sorry, I could not get that done.",
    "I could not finish that one, sorry.",
    "That did not work. I could not do it.",
)
# A mouth's request whose run is slow or never ends (10-08 Meet: 3 of 17 runs took 45-110 s or sent no end,
# and their askers heard an acknowledgement and then nothing). Why a run is slow is the runtime's question;
# these two bounds are what its asker is owed meanwhile.
# No frame of the run for this long, its asker still waiting on it and the room at a pause: its agent says
# once that it is still working (STILL_WORKING). Never over people who have moved on, never a second time.
RUN_STATUS_S = 20.0
# No `run_ended` this long after /turns accepted the run: its agent tells the asker once that it could not
# get it (COULD_NOT), the request is failed and the agent's next one starts. The slowest run that did answer
# in that call took 110 s. An answer that still comes is written to the room, never said after the apology.
# The two bounds count from different moments on purpose. RUN_STATUS_S counts from the request (the run's
# start here, then its last frame): what a status line answers is the asker's wait, and a workspace that is
# waking is part of it. This one counts from acceptance: it is the time the run is given to do the work, and
# the up to TURN_RETRY_BUDGET seconds a cold workspace spends before it takes the run are not the run's.
# A run never accepted is closed by `_post_turn` (CANT_REACH) at that budget, so it needs no deadline here.
# Nor is the time a run waits on the room lock behind a chat run (`run_queued`): the deadline does not run
# while it is queued, and starts when its first frame after the queue comes.
RUN_DEADLINE_S = 120.0
# The final POST's backoff and its whole budget. The budget stays under the job's shutdown window
# (agent.py SHUTDOWN_S): the flush usually runs from the shutdown callback.
FINAL_TRANSCRIPT_RETRY_S = (1, 2, 4, 8, 16)
FINAL_TRANSCRIPT_BUDGET_S = 45
UNHEARD_BUDGET_S = (
    10  # a meeting's unheard answers are posted before its transcript, within this
)
# TranscriptBody.lines max (backend huddles.py): a rolling flush, and a meeting's one final request
TRANSCRIPT_CHUNK = 500
TRANSCRIPT_FINAL_MAX = 5_000


# understanding §4.6: words two different asks share ("what are the open PRs" / "what are the open issues")
FUNCTION_WORDS = frozenset(
    "a an the and or but so then also of to in on at for from with about by as is are was were be been am do does "
    "did have has had can could would will should may might i you we they he she it me my our your us them this "
    "that these those there here what what's whats when where who which why how if whether yes ok okay um "
    "uh like just please hey hi any some all up out now".split()
)


def _words(text: str) -> tuple[set[str], list[str]]:
    """(content words, all words): lowercase, letters and digits, an apostrophe joins its word."""
    words = re.findall(r"[^\W_]+", text.lower().replace("'", "").replace("\u2019", ""))
    return {w for w in words if w not in FUNCTION_WORDS}, words


def units(text: str) -> list[str]:
    """What a mouth is handed, as the sentences it says one order at a time. A sentence is the unit of delivery:
    said to its end it is done, cut it is said again from its start (owner ruling 10-07). Never split in a word."""
    return [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]


def _words_in(a: str, b: str) -> bool:
    """Every content word of `a` is in `b`, spelled the same: a match that makes the worker do nothing ("the
    closed PRs" is not "the open PRs"). With none, only the very same words match."""
    (ca, fa), (cb, fb) = _words(a), _words(b)
    if not ca:
        return bool(fa) and not cb and fa == fb
    return ca <= cb


def mixed_meet(identity: str) -> bool:
    """The whole Meet as one participant, `meet:<roomId>` (a Recall bot booked before per-person audio): who
    said a line comes from speaker events (labels). A person of the Meet is `meet:<roomId>:<participantId>`,
    their own microphone and their own name."""
    return identity.startswith("meet:") and identity.count(":") == 1


def in_meet(identity: str) -> bool:
    """Anyone heard through a Google Meet, mixed or per-person: their line's speaker is a Meet label."""
    return identity.startswith("meet:")


def _retryable(e: BackendError) -> bool:
    """A cold workspace (503 runtime_unavailable), the API gateway's own 503/504, or no answer at all."""
    return e.status in (0, 503, 504)


class Live(Protocol):
    """One agent's voice, a mouth with no ears (agent.py `_DrivenLive`)."""

    agent_id: str
    track_name: str  # f"agent-{agent_id}" (C7)

    def append_instructions(
        self, text: str
    ) -> None: ...  # a standing rule (a rename, a voice note)
    def interrupt(self) -> Any: ...  # stops the speech and drops what was not started
    # speak the text word for word. `order`: this playback's id. A mouth reports each unit (`Huddle.on_unit`)
    # and then that the order is over (`Huddle.on_playback_ended`).
    def say(self, text: str, order: int | None = None, first: int = 0) -> None: ...
    def usage_seconds(self) -> float: ...
    async def aclose(self) -> None: ...


class Room(Protocol):
    def publish(self, topic: str, payload: dict, to: list[str]) -> None: ...
    def set_attributes(
        self, attrs: dict[str, str]
    ) -> None: ...  # the worker's participant attributes
    def shutdown(self) -> None: ...


def emit_metric(
    name: str, value: float, unit: str = "Seconds", dims: dict[str, str] | None = None
) -> None:
    """One CloudWatch EMF line on stdout (same shape as the backend's put_metric). `dims` are enums only:
    never transcript text."""
    dims = dims or {}
    line = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "LightbulbAI",
                    "Dimensions": [list(dims)],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        **dims,
        name: value,
    }
    print(json.dumps(line), file=sys.stdout, flush=True)


@dataclass
class _Window:
    call_id: str
    deadline: float
    budget: float
    used_seconds: float = 0.0  # summed seconds of every voice closed in this window


@dataclass(eq=False)
class Utterance:
    """One thing an agent has to say in the room, kept in the room's one ledger (`Huddle._ledger`) from the
    moment it is owed until it is delivered or cancelled. It belongs to the agent, not to a voice: a voice
    opening, closing or changing loses nothing of it.

    `units` are its sentences and `cursor` how many were reported said to their end, so it is delivered when
    the cursor reaches the end and never before. `obligation`: the run an answer answers, or the request row
    (else the run) a line is about. `audience`, `label`: who it is for. `over`: who talked over it while it
    was being said. `resume`: that talk was only a backchannel. `write`: small talk still being written, and
    `mark` the line it replies to. `order`, `via`: the playback saying it now and the voice it plays through.
    `speaking` is its permission to play: it holds only while that playback is live, and a pause, a strike
    or a failed playback takes it away (`may_say`). `tries`: playbacks in a row that failed."""

    id: int
    agent: str
    kind: str  # ack | status | social | answer
    units: list[str]
    at: float
    obligation: str | None = None
    request: str = ""
    audience: str | None = None
    label: str = ""
    cursor: int = 0
    state: str = "pending"  # pending | speaking | paused | delivered | cancelled
    over: set = field(default_factory=set)
    resume: bool = False
    write: Any = None
    mark: int = -1
    order: int = 0
    via: Any = None
    tries: int = 0
    # When the first sound of the unit being said went to the track (`on_unit_audible`). None: the unit has
    # its permission and nothing of it is audible yet (`may_say`). 0.0: no unit was asked for.
    sound_at: float | None = 0.0
    sounding: int = -1  # the unit `sound_at` is about
    cut_at: float = 0.0  # when it was paused, until the receipt of the playback that was cut is in (`_hold`)


@dataclass(eq=False)
class Epoch:
    """One stretch of a person's speech, from the transcriber's speech start until what they said has been
    acted on. It is the room's record of an open turn, and the scheduler starts nothing while one is open
    (`Huddle._turn_open`).

    speaking: the transcriber hears them. awaiting-final: they stopped and its transcript is not here.
    awaiting-route: the transcript is here and what it asks is being worked out or applied. Then it is
    resolved and forgotten. `credible`: the SFU heard them too, so it is speech and not the detector alone:
    only a credible one holds the room before it has words. `held`: tasks still applying its line. `words`:
    what the transcriber has made of it so far (its interim text), before the final."""

    id: int
    who: str
    began: float
    state: str = "speaking"  # speaking | awaiting-final | awaiting-route | resolved
    ended: float = 0.0
    credible: bool = False
    held: int = 0
    words: str = ""

    @property
    def listening(self) -> bool:
        """All of it heard so far is a listening sound ("mm-hm", "yeah"): its speaker is following, not asking."""
        return bool(self.words) and backchannel(self.words)


# The speech whose line the running task is handling. asyncio copies it into every task made while it is
# set, so a task that a routing task starts later still knows the speech it works for (`Huddle._routes`).
# An attribute on the room could not say that: it is cleared when the final's handler returns.
_ROUTING: contextvars.ContextVar[Epoch | None] = contextvars.ContextVar(
    "huddle_routing", default=None
)


@dataclass
class Engagement:
    """A person and an agent talking with each other: what the room knows about whether that person wants to
    hear that agent now. `expects`: what comes next between them (the agent's answer, the person's reply to
    the agent's question, or nothing). `why`: what suspended it (the person turned to `people`, an `other`
    exchange came between, a long `gap`, they `hushed` it). `last`, `last_by`: how the agent's last words to
    them ended, and whether they or someone else cut them."""

    person: tuple[str, str]  # (identity, meeting label)
    agent: str
    at: float
    obligation: str | None = None
    state: str = "active"  # active | suspended (ended: it is forgotten)
    expects: str = "nothing"  # answer | reply | nothing
    why: str = ""
    last: str = "none"  # none | speaking | paused | delivered | interrupted
    last_by: str = ""  # speaker | other


@dataclass
class _Voice:
    agent_id: str
    live: Any  # None while joining
    gen: int  # one per opened voice: a run it started is its own (`_same_session`)
    speaker: str
    last_activity: float
    opened: float = 0.0
    agent_state: str = "listening"  # the voice session's AgentState
    posting: int = 0  # turn POSTs in flight
    dropping: bool = False  # its live session failed; a close is on the way
    driven_run: str | None = (
        None  # the run it started for its speaker's request: the queue waits for it
    )
    path: str = "name"  # live.latency.* dimension: name | jev | tap | switch (a queue advance) | announce | ack
    persona: str = ""  # who it is, for the small talk written for it
    writing: Any = None  # its small-talk reply to the line that opened it, begun while /route decided
    wake_dims: dict[str, str] | None = (
        None  # live.wake.fired's dimensions, for a voice a name woke
    )
    used: bool = False  # its speaker spoke to it again: live.wake.unused stays silent
    driven_ok: bool = False  # the driven run (driven_run) was accepted by /turns
    heard_at: float | None = (
        None  # live.latency.*: when the waking line was heard; None once first_audio is out
    )
    # understanding §4.6: rows its speaker's lines made, acknowledged once they are quiet (tick)
    ack_rows: list = field(default_factory=list)
    # a meeting: the labelled speaker it is with ("": nobody known), and when it last had something to do (it
    # opened, stopped speaking, or its speaker said a line to it): it closes MEET_FOLLOWUP_S after that
    label: str = ""
    followed_at: float = 0.0


async def _no_line(persona: str, event: str) -> str:
    return ""


class Huddle:
    def __init__(
        self,
        *,
        room: Room,
        backend: Any,
        live_factory: Callable[
            ..., Awaitable[Live]
        ],  # (*, agent_id, persona, voice, speaker, seed)
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_vocabulary: Callable[
            [str], None
        ] = lambda _prompt: None,  # the STT prompt changed
        setting: str = "live",  # "meeting": a bridged Google Meet
        # (persona, the line said to it and by whom) -> a mouth's one sentence of small talk ("": none)
        writer: Callable[[str, str], Awaitable[str]] | None = None,
    ):
        self.setting = setting
        self._writer = writer or _no_line
        self._room, self._backend, self._live_factory, self._clock = (
            room,
            backend,
            live_factory,
            clock,
        )
        self._sleep = sleep
        self._published: dict[str, str] = {}
        # a meeting keeps every line: the whole transcript is posted once, as the worker leaves
        self._transcript = Transcript(None if setting == "meeting" else 30000)
        self._meet_speaking: dict[
            str, tuple[str, float]
        ] = {}  # participantId -> (name, since)
        self._meet_windows: list[
            tuple[str, float, float]
        ] = []  # (name, start, end), newest last
        self._utter_from: dict[
            str, float
        ] = {}  # identity -> when its current utterance began
        self._said_by: dict[
            str, str
        ] = {}  # identity -> speaker label of its latest meeting line
        # mark -> that meeting line's speaker label. A label read after an await (a /route answer, a voice opening)
        # is read by the line's mark: `_said_by` is by then the label of whoever spoke last.
        self._said_at: dict[int, str] = {}
        self._meet_roster: list[
            str
        ] = []  # the humans in the Meet, from the latest packet that had a roster
        self._steered: set[tuple[int, str]] = (
            set()
        )  # (a line's mark, a request): that line steered it (`_apply`)
        self._status_said: set[str] = (
            set()
        )  # runs whose "still working" line was owed once (`_overdue`)
        self._open_question: tuple | None = (
            None  # (deadline, identity, text, mark): `_unclaimed`
        )
        self._open_question_ep: Epoch | None = (
            None  # the speech that asked it, held while it is open
        )
        self._people: dict[str, str] = {}  # identity -> display name, humans only
        self._team: dict = {"agents": [], "people": [], "ceoId": None}
        self._team_at = -math.inf
        self._refreshing = self._refresh_again = (
            False  # one team refresh in flight; a request during it reruns it
        )
        self._on_vocabulary = on_vocabulary
        self._vocabulary: str | None = None
        self._window: _Window | None = None
        self._voice: _Voice | None = None
        self._gen = 0
        self._runs = Runs(self, clock)
        # run id -> (attempts, last at): a requested delivery whose voice did not open (engine §3.5)
        self._delivery_tries: dict[str, tuple[int, float]] = {}
        self._voice_limit = ""  # why the backend refuses to open a voice (its /summon reason); "" when it does not
        # identity -> (text, when, mark, agent or None): a turn its speaker has not finished. It trailed off, or
        # it is only an agent's name. Their next line completes it.
        self._incomplete: dict[str, tuple[str, float, int, str | None]] = {}
        self._ledger: list[
            Utterance
        ] = []  # everything the room's mouths still have to say (`_pump`)
        self._answered: dict[
            str, list[str]
        ] = {}  # each run's answer as said, sentence by sentence (`_carry_on`)
        self._uid = 0
        self._order = 0  # the last playback started (`_start`): its receipts name it
        # Obligations (a request row, a run) whose speech a person silenced -> their agent. Nothing more
        # about one is said: its answer goes to the room in writing. Its work is not touched (`_hush`).
        self._written: dict[str, str] = {}
        # run id -> (tries, try again at, agent, news): written answers whose post failed (`_late_answer`)
        self._unposted: dict[str, tuple[int, float, str, tuple]] = {}
        self._engaged: dict[
            tuple[tuple[str, str], str], Engagement
        ] = {}  # (person, agent) -> their exchange
        self._selected: str | None = (
            None  # the agent a person just turned to ("go ahead"): it speaks next
        )
        self._heard_at: dict[
            str, float
        ] = {}  # identity -> when they were last heard talking
        # The per-person turn state: identity -> their speech not yet acted on, oldest first (`Epoch`).
        self._turns: dict[str, list[Epoch]] = {}
        self._eid = 0
        self._spoken = [
            0,
            0.0,
        ]  # characters and audible seconds of the units said to their end (`_audible`)
        self._unknown_at = (
            -math.inf
        )  # when speech that never got a transcript last ended (`_final_timeout`)
        self._turn_at: dict[
            tuple[str, str], float
        ] = {}  # (identity, label) -> their last line with real words
        self._level_at: dict[
            str, float
        ] = {}  # identity -> when the SFU last heard sound from them
        self._talk_until = -math.inf  # when the room last went from talk to no talk
        self._gate_timer: Any = (
            None  # the speech gate looks again when the room has been quiet long enough
        )
        self._gate_held: tuple | None = (
            None  # (agent, kind, why, since): the speech the scheduler is holding
        )
        self._delivering: asyncio.Task | None = (
            None  # a voice opening for an utterance (`_open_for`)
        )
        self._line_at: dict[
            tuple, int
        ] = {}  # each list of fixed lines -> the next one to say (`_line`)
        self._level: set[str] = (
            set()
        )  # humans in the SFU's active speakers: sound, maybe not speech
        self._voiced: set[str] = (
            set()
        )  # humans the transcriber's speech detector hears speaking
        self._speaking: set[str] = (
            set()
        )  # humans talking now: both of the above (`_talking_changed`)
        self._last_speaker: str | None = None
        self._last_agent: dict[
            str, str
        ] = {}  # identity -> the agent they last summoned
        self._queue: list[
            tuple[str, str, str, str]
        ] = []  # (agent_id, speaker, request, the asker's meeting label) still to answer
        # identity -> (line, at, mark, its /route answer, _route's arguments): unfinished, /route said hold
        self._held: dict[str, tuple[str, float, int, dict, dict]] = {}
        # A meeting person's turn, which may be said in pieces: identity -> (turn id, when its last piece's
        # speech ended, the whole turn so far). Every /route call for it carries the id; the rows its lines made
        # (`_turn_rows`, by each row's mark) are amended by a later piece of it (`_amend_turn`).
        self._turn_id: dict[str, tuple[int, float | None, str]] = {}
        self._turn_of: dict[
            int, int
        ] = {}  # transcript mark -> the turn its line is part of
        self._tid = 0
        self._final_mark: dict[
            str, int
        ] = {}  # identity -> mark of their latest final: a hold older than it is stale
        self._final_at: dict[
            str, float
        ] = {}  # identity -> when their latest final was heard
        self._spoke: dict[
            str, int
        ] = {}  # identity -> the mark of their latest real-speech final
        self._unrouted: dict[
            str, float
        ] = {}  # identity -> when their line woke nobody (live.route.missed)
        self._lock = asyncio.Lock()  # one summon or close at a time
        self.dropped_frames = 0
        self._tasks: set[asyncio.Task] = set()
        self._started = clock()
        self._seen_human = False
        self._last_line: dict[str, str] = {}  # identity -> their latest final line
        self._last_heard = self._started  # the room's last speech (or arrival)
        self._idle_ended = self._left = False
        self._empty_at: float | None = (
            None  # when the room last read as empty (EMPTY_ROOM_GRACE)
        )
        self._presence_v = 0
        self._stopped: set[str] = (
            set()
        )  # runs a stop aborted (a /turns still in flight is aborted again)
        self._failed_at: dict[
            tuple[str, str], float
        ] = {}  # (identity, agent) -> when its voice_failed
        self._closed_seconds = 0.0  # seconds of every voice this job closed
        # The transcript is persisted by the backend (CloudWatch) in batches, so what people and
        # agents said in a huddle can be read back when something goes wrong in it.
        self._huddle_id = str(uuid.uuid4())
        self._line_seed = uuid.UUID(
            self._huddle_id
        ).int  # where the fixed lines start: another place each call
        self._sent_mark = -1  # the newest transcript line the backend has
        self._sent_at = self._started
        self._flush_lock = (
            asyncio.Lock()
        )  # shutdown waits for a batch in flight, then sends the rest
        self._requests = RequestList(
            self._row_changed
        )  # conversation engine §3.4: this call's asks, memory only
        self._window_lock = (
            asyncio.Lock()
        )  # one /summon however many requests start at once
        self._marking: set[asyncio.Task] = set()  # "don't post that" marks in flight
        self._closing = False  # the worker is leaving: no new window
        # understanding §4.6: identity -> their floor lines still being understood (`_in_flight`): the
        # acknowledgement waits for them (`_acknowledge`)
        self._understanding: dict[str, list[asyncio.Future]] = {}
        self._routing: dict[
            str, list[asyncio.Future]
        ] = {}  # identity -> their lines at /route, in said order

    @property
    def _unclaimed(self) -> tuple | None:
        """A meeting's question that named nobody, left to the people for UNCLAIMED_QUESTION_S:
        (deadline, identity, text, mark). None: there is none, or it was taken.

        While it is open the speech that asked it is not resolved (`_turn_open`): the floor is the
        people's, and no agent starts on a question put to the room. So every way it ends is here: a
        person takes it (they talk, or their line comes), an agent takes the floor, or its time is up and
        the tick has put it to /route, whose task holds the speech from then on (`_routes`)."""
        return self._open_question

    @_unclaimed.setter
    def _unclaimed(self, q: tuple | None) -> None:
        was, self._open_question, self._open_question_ep = (
            self._open_question_ep,
            q,
            None,
        )
        if (
            q is not None
            and (ep := _ROUTING.get()) is not None
            and ep.state == "awaiting-route"
        ):
            ep.held += 1
            self._open_question_ep = ep
        if was is not None:
            self._release(was)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._done)
        return t

    def _routes(self, coro) -> asyncio.Task:
        """A task that works out what a person's line asks and applies it. The speech the line is the
        transcript of stays open until every such task has ended, so nothing is said on a line that is
        still being understood, and its effects are in place before the scheduler looks again.

        The speech is carried by `_ROUTING`, so a routing task that starts another one (a line that named
        two agents is dispatched after its /route round trip) hands it the same speech. The outer task
        still holds it then, so it cannot resolve between the two. A task that raises or is cancelled
        releases it like one that ended."""
        t = self._spawn(coro)
        if (ep := _ROUTING.get()) is not None and ep.state == "awaiting-route":
            ep.held += 1
            t.add_done_callback(lambda _t: self._release(ep))
        return t

    def _done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("huddle task failed", exc_info=t.exception())

    # -- team --

    def _agent(self, agent_id: str | None) -> dict | None:
        return next((a for a in self._team["agents"] if a["id"] == agent_id), None)

    def _agent_name(self, agent_id: str) -> str:
        return (self._agent(agent_id) or {}).get("name") or "the agent"

    async def _refresh_team(self) -> None:
        if (
            self._refreshing
        ):  # the one in flight may have read the team before this request's change
            self._refresh_again = True
            return
        self._refreshing = True
        try:
            while True:
                self._refresh_again = False
                self._team_at = self._clock()
                try:
                    t = await self._backend.team()
                    self._team = {
                        "agents": t.get("agents") or [],
                        "people": t.get("people") or [],
                        "ceoId": t.get("ceoId"),
                    }
                    self._queue = [
                        q for q in self._queue if self._agent(q[0])
                    ]  # a queued agent that left is dropped
                except Exception:
                    logger.exception("huddle team refresh failed")
                if not self._refresh_again:
                    break
        finally:
            self._refreshing = False
        self._update_vocabulary()
        self._sync_state()

    def _update_vocabulary(self) -> None:
        """The STT prompt names everyone, so "Gauge" is not heard as "gauge" (D Addressing)."""
        people = [p["name"] for p in self._team["people"] if p.get("name")] + list(
            self._people.values()
        )
        prompt = stt_prompt(
            [a["name"] for a in self._team["agents"] if a.get("name")], people
        )
        if prompt != self._vocabulary:
            self._vocabulary = prompt
            self._on_vocabulary(prompt)
            emit_metric("live.stt.prompt_updated", 1, "Count")

    # -- live session calls --

    def _live_call(self, v: _Voice, method: str, *args, **kwargs) -> bool:
        """Every call onto a voice session goes through here. A dead session raises
        ("AgentSession isn't running"): close that voice cleanly instead of letting the error
        escape into a person's transcription loop. The next summon starts a fresh one."""
        try:
            getattr(v.live, method)(*args, **kwargs)
            return True
        except Exception:
            logger.exception("huddle live session %s failed; closing the voice", method)
            self._drop(v)
            return False

    def _drop(self, v: _Voice, keep_queue: bool = False) -> None:
        if self._voice is v and not v.dropping:
            v.dropping = True
            self._unsaid(
                (u for u in self._ledger if u.via is v), failed=True
            )  # its session died under it
            self._spawn(self.end_exchange(only=v, keep_queue=keep_queue))

    def on_live_closed(self, live: Any) -> None:
        """The voice session closed on its own (an unrecoverable realtime error)."""
        v = self._voice
        if v and v.live is live:
            logger.warning(
                "huddle live session closed under an open voice agent=%s", v.agent_id
            )
            self._drop(v)

    def _name(self, identity: str) -> str:
        return self._people.get(identity) or "Someone"

    def _talker(self, identity: str, mark: int | None = None) -> str:
        """Who said a line. A meeting arrives as one participant named "Meeting": the labelled speaker of the
        line at `mark` (else of the latest line), else "Someone in the meeting" (10-04 prod Meet: "Meeting is
        talking to me right now"). A person of a per-person Meet is named like anyone in a Live call."""
        if mixed_meet(identity):
            name = self._said_at.get(mark) or self._said_by.get(identity) or ""
            return (
                name
                if name and not name.startswith("Someone")
                else "Someone in the meeting"
            )
        return self._name(identity)

    def _with(self, v: _Voice) -> str:
        """Who a voice is with: in a meeting the speaker who has its attention, never whoever spoke last."""
        return v.label or self._talker(v.speaker)

    def _owner_name(
        self, v: _Voice, row: Request | None = None, mark: int | None = None
    ) -> str | None:
        """The speaker name a turn carries: its request row's asker, else its line's speaker (`mark`), else its
        meeting voice's own speaker; else the person's name a meeting line set (None in a Live call)."""
        if not mixed_meet(v.speaker):
            return self._said_by.get(v.speaker)
        name = (
            row.label
            if row is not None
            else self._label(v.speaker, mark)
            if mark is not None
            else v.label
        )
        return name or "Someone"

    def _label(self, identity: str, mark: int | None = None) -> str:
        """A mixed meeting line's one known speaker (the line at `mark`, else the latest); "" for anyone else (the
        identity is the person), for "Someone" and for "A + B"."""
        if in_meet(identity) and not mixed_meet(identity):
            return self._said_by.get(identity) or self._name(
                identity
            )  # their own microphone: always known
        n = (
            (self._said_at.get(mark) or self._said_by.get(identity, ""))
            if mixed_meet(identity)
            else ""
        )
        return "" if n.startswith("Someone") or " + " in n else n

    def _mine(self, v: _Voice, identity: str, mark: int | None = None) -> bool:
        """The line is the open voice's own: its speaker's, while they are talking with the agent (`Engagement`),
        whoever spoke last and however long ago (10-04 prod Meet: a pinned voice took the whole meeting's talk as
        its speaker's)."""
        if identity != v.speaker:
            return False
        who = self._who(identity, mark)
        e = self._engagement(who, v.agent_id)
        return (
            e is not None and e.state == "active" and who[1] == v.label
        )  # (a mixed meeting: and it is with them)

    def _member_name(self, user_id: str | None) -> str:
        """A workspace member who may not be in the call (a voice note saved from settings)."""
        team = next(
            (p.get("name") for p in self._team["people"] if p.get("userId") == user_id),
            None,
        )
        return self._people.get(user_id or "") or team or "Someone"

    def _notice(self, payload: dict, to: list[str] | None = None) -> None:
        self._room.publish(
            NOTICE_TOPIC, payload, sorted(self._people) if to is None else to
        )

    # -- people --

    async def on_participants(self, people: dict[str, str]) -> None:
        if set(people) - set(self._people):
            self._last_heard = (
                self._clock()
            )  # someone just arrived: the room isn't idle
        for gone in set(self._people) - set(people):
            for d in (self._held, self._final_at):
                d.pop(gone, None)
            self._voiced.discard(
                gone
            )  # nobody who left is talking; a rejoin starts clean
            self._level.discard(gone)
            self._turns[gone] = [
                e for e in self._turns.get(gone, []) if e.state == "awaiting-route"
            ]
        self._talking_changed()
        self._people = dict(people)
        if people:
            self._seen_human = True
        try:
            await self._post_presence(
                people
            )  # first: the last human leaving ends the job right after
        except Exception:
            logger.exception(
                "huddle presence update failed"
            )  # a timeout never holds up the close below
        try:
            v = self._voice
            if (
                v and v.speaker not in people
            ) or not people:  # the pinned speaker left: the voice closes
                await self.end_exchange()
            if people:
                await self._refresh_team()
        except Exception:
            logger.exception("huddle participants update failed")
        finally:
            # An empty room before anyone arrived is the dispatch race, not a huddle ending; an
            # empty one after that is confirmed by tick() after EMPTY_ROOM_GRACE against the latest
            # snapshot, never by this one.
            self._empty_at = self._clock() if not people and self._seen_human else None

    async def _post_presence(self, people: dict[str, str]) -> None:
        if self.setting == "meeting":
            return  # the meeting has its own room marker; the bot's join is not a Live call
        # The version is taken before the first await: these run as concurrent tasks, and the backend
        # drops a stale one.
        self._presence_v = version = max(self._presence_v + 1, time.time_ns())
        await asyncio.wait_for(
            self._backend.presence(sorted(people), version), PRESENCE_TIMEOUT
        )

    async def _flush_transcript(self) -> None:
        """Send the backend every transcript line it has not seen. A failed send is retried on the
        next tick; the lines stay here until one lands."""
        async with self._flush_lock:
            lines = self._transcript.after(self._sent_mark)
            self._sent_at = self._clock()
            final = self.setting == "meeting"
            if final:
                # One request carries the whole transcript: chunks could land on different
                # backend tasks. A meeting never flushes before this, so `lines` is all of it.
                if not lines:
                    return
                if len(lines) > TRANSCRIPT_FINAL_MAX:
                    logger.warning(
                        "meeting transcript: %d lines, keeping the newest %d",
                        len(lines),
                        TRANSCRIPT_FINAL_MAX,
                    )
                    lines = lines[-TRANSCRIPT_FINAL_MAX:]
                parts = [lines]
            else:
                parts = [
                    lines[i : i + TRANSCRIPT_CHUNK]
                    for i in range(0, len(lines), TRANSCRIPT_CHUNK)
                ]
            sent = 0
            for part in parts:
                body = [
                    {
                        "mark": x.mark,
                        "kind": x.kind,
                        "name": x.name,
                        "text": x.text,
                        "at": x.at,
                        "to": x.to,
                        **({"ready": x.ready} if x.ready else {}),
                    }
                    for x in part
                ]
                try:
                    if final:
                        await self._post_final_transcript(body)
                    else:
                        await self._backend.transcript(self._huddle_id, body)
                except Exception:
                    logger.log(
                        logging.ERROR if final else logging.INFO,
                        "huddle transcript post failed; %d lines unsent",
                        len(lines) - sent,
                        exc_info=True,
                    )
                    return
                sent += len(part)
                self._sent_mark = part[-1].mark

    async def _post_final_transcript(self, body: list[dict]) -> None:
        """A meeting's one transcript POST, retried: a transient error must not lose the meeting's
        transcript. Only a 2xx is an acknowledgement (the backend client raises on anything else)."""

        async def post() -> None:
            for attempt, delay in enumerate((*FINAL_TRANSCRIPT_RETRY_S, None), 1):
                try:
                    await self._backend.transcript(self._huddle_id, body, final=True)
                    logger.info(
                        "meeting transcript posted: %d lines, attempt %d",
                        len(body),
                        attempt,
                    )
                    return
                except Exception as e:
                    if delay is None:
                        raise
                    logger.warning(
                        "meeting transcript post failed (%s); retry %d in %ss",
                        type(e).__name__,
                        attempt,
                        delay,
                    )
                    await asyncio.sleep(delay)

        await asyncio.wait_for(post(), FINAL_TRANSCRIPT_BUDGET_S)

    async def clear_presence(self) -> None:
        """The job is shutting down: clear the room's Live marker before anything else, unless people are
        still in the call (a worker restart), whose marker stays theirs. A missed one is healed by the
        clients' refetch and the backend's orphan sweep."""
        self._closing = (
            True  # first: a line routed during the awaits below opens no new window
        )
        if self._marking:  # a "don't post that" said just before hang-up lands first
            await asyncio.wait(set(self._marking), timeout=5)
        if self.setting != "meeting":
            await (
                self._flush_transcript()
            )  # last chance for what was said since the previous batch
        await self._last_posts()
        if self._people and not self._idle_ended:
            return
        try:
            await self._post_presence({})
        except Exception:
            logger.exception("huddle shutdown presence clear failed")

    async def _last_posts(self) -> None:
        """The call is over: the answers nobody heard, then the transcript. A meeting's notes and mail are
        written when its one transcript lands, so the answers must be on the record first; they are bounded, so
        the transcript keeps its budget inside the shutdown window."""
        try:
            await asyncio.wait_for(
                self._post_unheard(),
                UNHEARD_BUDGET_S if self.setting == "meeting" else None,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "huddle unheard answers not all posted within %ss", UNHEARD_BUDGET_S
            )
        open_rows = [r for r in self._requests.open() if r.routed]
        emit_metric(
            "live.request.open_at_end", len(open_rows), "Count"
        )  # understanding §5.3: 0
        await (
            self._flush_transcript()
        )  # the row changes the sweep made; a meeting's one transcript

    async def _post_unheard(self) -> None:
        """The call is ending: every answer nobody heard, and every request never started, goes to the room."""
        # answers not said to their end, also one being said or not yet begun
        held = [(u.agent, self._news_of(u)) for u in self._ledger if u.kind == "answer"]
        self._ledger.clear()
        held += [
            (a, n) for _, _, a, n in self._unposted.values()
        ]  # a post that failed: the last try
        for agent_id, n in held:
            await self._late_answer(agent_id, n)  # unheard here: the room gets it
        for r in [
            r for r in self._requests.open() if r.routed and r.status == "queued"
        ]:
            # understanding §4.5: its agent never got to it; no run will post it, so the room gets one line
            r.run_id = str(uuid.uuid4())
            self._requests.fail(r)  # never done: the post is no delivery
            await self._late_answer(
                r.agent, (r.run_id, r.task, NOT_STARTED_POST, self._clock(), r.asker)
            )

    async def on_final_transcript(self, identity: str, name: str, text: str) -> None:
        """A person's transcribed line. The speech it covers is registered as being routed before anything
        else runs, and is resolved only when the line has been handled and every task applying it has ended."""
        ep = self._final_epoch(identity)
        token = _ROUTING.set(ep)
        try:
            self._heard_final(identity, name, text)
        finally:
            _ROUTING.reset(token)
            self._release(ep, 0)

    def _final_epoch(self, identity: str) -> Epoch:
        """The speech a final covers: the oldest of that person's that ended and has no transcript yet. A
        final resolves only that one, so speech they began since stays open. None waiting: the final stands
        for speech of its own (its detector events were missed)."""
        # ponytail: matched by order, the STT plugin gives a final no segment id. A segment with no words
        # gets no final, so the next final takes its place and the newer one waits out FINAL_WAIT_S. Carry
        # the item id on the speech events if that ever costs a real delay.
        now = self._clock()
        eps = self._turns.setdefault(identity, [])
        ep = next((e for e in eps if e.state == "awaiting-final"), None)
        if ep is None:
            self._eid += 1
            ep = Epoch(self._eid, identity, now, ended=now)
            eps.append(ep)
        ep.state, ep.credible = "awaiting-route", True
        self._turn_log("final", ep)
        return ep

    def _release(self, ep: Epoch, n: int = 1) -> None:
        """A task applying the line of `ep` ended (`n` 0: its final was handled). With none left it is resolved."""
        ep.held -= n
        if ep.held > 0 or ep.state != "awaiting-route":
            return
        ep.state = "resolved"
        self._drop_epoch(ep)
        self._turn_log("resolved", ep)
        self._pump()  # the line is understood and applied: speech that waited for it may start

    def _drop_epoch(self, ep: Epoch) -> None:
        eps = self._turns.get(ep.who, [])
        if ep in eps:
            eps.remove(ep)
        if not eps:
            self._turns.pop(ep.who, None)

    def _final_timeout(self, ep: Epoch) -> None:
        """The degraded path, `final_timeout`: a person stopped speaking FINAL_WAIT_S ago and no transcript
        came. Their speech is closed as unknown human speech: nobody knows what it asked or whom it was for.
        Lines, and answers nobody is plainly waiting for, are held for a real pause on it. An answer its
        asker is waiting for or was turned to for may go (`_hold`). A sound the SFU never heard is dropped."""
        self._drop_epoch(ep)
        if ep.credible:
            self._unknown_at = max(self._unknown_at, ep.ended)
            emit_metric("live.turn.degraded", 1, "Count", {"why": "final_timeout"})
        self._turn_log(
            "degraded:final_timeout" if ep.credible else "dropped:not_credible", ep
        )

    def _detector_lost(self, ep: Epoch, now: float) -> None:
        """The detector-loss policy, `detector_lost`: the detector still says this person speaks, and the
        SFU has heard nothing from them for DETECTOR_LOST_S. Speech that may be going on is never approved
        by a clock alone, so it is not dropped: it ends here and waits for its transcript. One that comes
        is routed as usual. None in FINAL_WAIT_S closes it as unknown human speech (`_final_timeout`)."""
        ep.state, ep.ended = "awaiting-final", now
        emit_metric("live.turn.degraded", 1, "Count", {"why": "detector_lost"})
        self._turn_log("degraded:detector_lost", ep)

    def _turn_log(self, event: str, ep: Epoch) -> None:
        self._log(
            "turn", event=event, epoch=ep.id, who=ep.who, state=ep.state, credible=ep.credible, held=ep.held,
            ageMs=round((self._clock() - ep.began) * 1000),
        )  # fmt: skip

    def _log(self, what: str, **fields) -> None:
        """One controller event on one line: speech evidence, a turn's state, a scheduler decision, a
        permission, a receipt. `t` is monotonic milliseconds, so lines of one call order and subtract."""
        logger.info(
            "huddle %s %s",
            what,
            json.dumps({"t": round(time.monotonic() * 1000), **fields}),
        )

    def _heard_final(self, identity: str, name: str, text: str) -> None:
        text = text.strip()
        if text and ghost_line(text, self._vocabulary):
            # Conversation engine §3.1: the STT model repeated its prompt on silence; nobody said it.
            emit_metric("live.stt.ghost_dropped", 1, "Count")
            text = ""
        # Talking is not ended here: the detector's stop comes before its sentence's final, and by then the
        # person may be saying the next sentence.
        began = self._utter_from.pop(identity, None)
        v = self._voice
        if not text:
            self._final_at[identity] = self._clock()
            self._talked_over(
                identity, ""
            )  # noise, not words: a mouth it paused carries on
            return
        now = self._clock()
        record = (
            text  # what goes on the record: a joined line records only its new piece
        )
        # The transcriber ends a line at a short pause, so a turn said in pieces arrives in pieces. One its
        # speaker has not finished is not a request yet: it trails off (10-06 prod Meet: "Can one of you
        # tell me..." was run as one, and the agent asked back what was meant), or it is only an agent's
        # name (10-07 rig: "Friday." was answered "Yes, I'm here." over the question, which then had no
        # name on it). It is joined to what its speaker says next.
        # The record keeps each piece as it was said; only the understanding sees them joined.
        control = self._control(
            text
        )  # a stop or "don't post that" stands on its own, never held or joined
        piece, joined = (
            text,
            False,
        )  # (`record` stays the piece: the pieces before it are on the record)
        before = self._incomplete.pop(identity, None)
        if before and not control and now - before[1] <= TRAIL_S:
            text, joined = f"{before[0]} {text}", True
        held = None if control or joined else self._held.get(identity)
        if held and now - held[1] <= HOLD_SECONDS and not _words_in(held[0], text):
            # /route said the turn so far is unfinished: this piece is its rest, joined before anything else
            # is decided, also a piece that is only a name (10-09 prod Meet: "A cool" was held, "Hey,
            # Friday." was held apart from it, and "A cool" was then answered as small talk on its own).
            del self._held[identity]
            text, joined = f"{held[0]} {text}", True
        ep, prev = _ROUTING.get(), self._turn_id.get(identity)
        detected = (
            ep is not None and ep.began < ep.ended
        )  # the detector heard this piece (else: a final alone)
        if (
            not joined
            and not control
            and prev is not None
            and not mixed_meet(
                identity
            )  # (one identity for everyone: who goes on is not known here)
            and self._last_speaker == identity
            and prev[1] is not None
            and detected  # (when it began is known only from the detector)
            and (
                ep.began - prev[1] <= TURN_GAP_S
                and self._turn_rows(prev[0])
                or ep.began - prev[1] <= HOLD_SECONDS
                and self._routing.get(identity)
            )
        ):
            # They went straight on after a turn that already asked something, or went on at all while /route
            # still decides on it (its answer may be `hold`, and a hold waits HOLD_SECONDS for the rest): it is
            # the same turn, routed again whole, and what it asks now replaces what it asked (`_amend_turn`); an
            # answer for the piece before comes too late to count (`_regrown`).
            text, joined = f"{prev[2]} {text}", True
        if not (joined and prev):
            self._tid += 1
        tid = prev[0] if joined and prev else self._tid
        self._turn_id[identity] = (tid, ep.ended if detected else None, text)
        # Nothing after a name alone for CALL_ON_S: the agent was called on (`_call_on`). A name that ends
        # an unfinished turn waits the same way for what it is called for.
        called = (
            None
            if control
            else only_name(text, self._team["agents"])
            or (only_name(piece, self._team["agents"]) if held else None)
        )
        holding = not control and (trails_off(text) or called is not None)
        if self.setting == "meeting":
            self._unclaimed = (
                None  # someone spoke after the question: it was not left unclaimed
            )
            if identity.startswith("meet:") and not mixed_meet(identity):
                # one person of the Meet: their own name; the record still shows they were in the Meet
                self._said_by[identity] = name
                mark = self._transcript.add_person(
                    identity, f"{name} (Meet)", record, at=now
                )
            elif identity.startswith("meet:"):
                if began is None or now - began > MEET_SPAN_MAX_S:
                    began = now  # a stale start (an STT stream that never finalised)
                name = self._said_by[identity] = self._meet_label(began, now)
                mark = self._transcript.add_person(
                    identity, f"{name} (Meet)", record, at=now
                )
                self._said_at[mark] = name
                while len(self._said_at) > SPEAKER_WINDOWS:
                    del self._said_at[next(iter(self._said_at))]
            else:
                mark = self._transcript.add_person(identity, name, text, at=now)
        else:
            mark = self._transcript.add_person(identity, name, text, at=now)
        self._final_mark[identity] = mark
        self._turn_of[mark] = (
            tid  # ponytail: one int per line of the call; trim if calls get days long
        )
        self._final_at[identity] = self._heard_at[identity] = now
        if real_speech(text) and not backchannel(text):
            self._turn_at[self._who(identity, mark) or (identity, "")] = (
                now  # they took a turn (`_expire`)
            )
        self._last_speaker = identity
        self._last_heard = now
        self._selected = (
            None  # a new line: whoever was turned to before it had their opening
        )
        self._talked_over(identity, record, self._who(identity, mark))
        if holding:
            self._incomplete[identity] = (text, now, mark, called)
            return  # on the record and someone spoke (an unclaimed question is theirs now); its rest is on the way
        mine = v is not None and self._mine(v, identity, mark)
        if real_speech(text) and not mine:  # a line that routes itself (§4.2)
            self._spoke[identity] = mark
        if stop_all(text):
            # Engine §3.6: "everyone stop" / "stop all": every open request and every run, the floor cut.
            if v:
                self._unwanted(v, identity)
            self._stop_all(mark)
            return
        no_post, target = no_post_command(text, self._team["agents"])
        if no_post:
            self._no_post(target, identity)  # engine §3.6; nothing is posted as a turn
            return
        # A stop is for what it was said over, and stopping an agent's speech is not stopping its work
        # (`_stop_said`): /route decides whether a request is called off. Nobody's: "never mind" between two
        # people stops nothing.
        if stop_request(text, self._team["agents"]) and self._stop_said(
            identity, text, mark
        ):
            return
        held = self._held.pop(identity, None)
        if held and now - held[1] <= HOLD_SECONDS and not _words_in(held[0], text):
            # the rest of an unfinished line: the whole of it is the request. A final that already holds the
            # held words is the whole line.
            text = f"{held[0]} {text}"
        self._last_line[identity] = text
        a = address(text, self._team["agents"], self._team["people"])
        if v is None and not a.agent_ids:
            # Nobody on the floor: a name anywhere in the sentence asks that agent ("...all right,
            # hey Friday, what is on my calendar?"; 10-06 prod Meet, where it got nothing for 30 s).
            for aid in self._named(text, a):
                a.agent_ids.append(aid)
                a.matches.setdefault(aid, "exact")
        unrouted = self._unrouted.pop(identity, None)
        if (
            (a.agent_ids or a.lightbulb)
            and unrouted is not None
            and now - unrouted <= MISSED_SECONDS
        ):
            emit_metric(
                "live.route.missed", 1, "Count"
            )  # they had to say it again, by name
        named = self._named(text, a)
        if len(named) >= 2:
            # Conversation engine §3.2: one request per named agent; /route's one Jev call splits the line.
            # Every named agent shows as working while it decides (§3.5: the strip within 1 s). A split of
            # fewer than two requests dispatches the line as today (`_dispatch`), after the round trip.
            self._requests.pending(mark, identity, named, now)
            self._sync_state()
            emit_metric(
                "live.ack.strip_ms", round((self._clock() - now) * 1000), "Milliseconds"
            )
            kw = {"humans": None if a.lightbulb else len(self._people), "heard": (a, v)}
            if mine:
                self._understand_floor(
                    identity, text, mark, **kw
                )  # its voice says what came of it (§4.6)
            else:
                self._routes(self._route_and_summon(identity, text, mark, **kw))
            return
        self._dispatch(identity, text, mark, a, v)

    def _named(self, text: str, a: Any) -> list[str]:
        """Every agent a line names for the split: wake names, exact mentions, and role names that open a clause."""
        return list(
            dict.fromkeys(
                a.agent_ids + a.anywhere + clause_openers(text, self._team["agents"])
            )
        )

    def _dispatch(
        self,
        identity: str,
        text: str,
        mark: int,
        a: Any,
        v: _Voice | None,
        heard_at: float | None = None,
        routed: dict | None = None,
    ) -> None:
        """Today's handling of an addressed line: a name summons, the rest routes. `heard_at`/`routed`: set after
        a multi-name line's /route round trip (its start, and its answer)."""
        now = self._clock()
        rk = {} if routed is None else {"routed": routed}
        if a.agent_ids and self._requests.stopped(mark, a.agent_ids[0]):
            return  # stopped while this line's /route decided
        own = v is not None and self._mine(v, identity, mark)
        if a.agent_ids and not (own and a.agent_ids[0] == v.agent_id):
            if v:
                self._unwanted(v, identity)
            dims = self._wake_fired(identity, a.agent_ids[0], a.matches[a.agent_ids[0]])
            # understanding §4.6: the line is understood while its voice opens (a fallback line was already asked)
            understood = None
            if heard_at is None:
                fut = self._in_flight(identity)
                # the wake word stripped, as for any Lightbulb line (B4 report)
                route = self._understand(
                    identity,
                    strip_wake(text),
                    humans=len(self._people),
                    quiet=True,
                    path="name",
                    until=fut,
                    mark=mark,
                )
                understood = (self._routes(route), fut)
            summon = self.summon(
                identity,
                a.agent_ids[0],
                request=text,
                mark=mark,
                path="name",
                then=a.agent_ids[1:],
                wake_dims=dims,
                heard_at=heard_at,
                understood=understood,
            )
            # a task: awaiting here would stall this person's STT loop for the whole summon
            self._routes(
                self._named_summon(identity, text, mark, summon, understood)
                if understood
                else summon
            )
            return
        who = self._who(identity, mark)
        # the agents this person is talking with, or was: their line may be for one of them with no name on it
        talking = not a.agent_ids and bool(self._engagements(who))
        if talking:
            people = self._team["people"] + [
                {"name": n} for n in (*self._said_by.values(), *self._people.values())
            ]
            if opens_with_person(text, people):
                # They turned to a colleague by name ("Leo, did you push that?"): they are not talking with the
                # agents now. What an agent had still to say to them keeps its hand up.
                self._suspend(who, "people")
                self._between(who)
                return
        said_to_room = (
            real_speech(text)
            and not pause(text)
            and not stop_request(text, self._team["agents"])
        )
        # Its speaker is talking with no agent and other people are: /route says whether it came between them
        # (`_line_between`). A listening sound comes between nobody and is not read.
        between = (
            said_to_room
            and not talking
            and not backchannel(text)
            and self._others_talking(who)
        )
        if v is not None and not own:
            # A line that is not the voice's and names no agent: transcript only, unless its
            # speaker is talking with an agent. Then /route says, on that evidence, whether it is for the
            # agent. A question may still go unclaimed.
            if a.lightbulb:
                self._routes(self._lightbulb_over(v, identity, text, mark))
                return
            if said_to_room and is_question(text):
                self._unclaimed = (now + UNCLAIMED_QUESTION_S, identity, text, mark)
            if said_to_room and talking:
                self._routes(
                    self._route_and_summon(
                        identity, text, mark, humans=len(self._people), **rk
                    )
                )
            elif between:
                self._routes(self._line_between(identity, text, mark))
            return
        if v is None:
            if a.lightbulb:
                self._routes(self._route_and_summon(identity, text, mark, **rk))
                return
            for agent_id in a.ambiguous[:1]:
                emit_metric("live.wake.ambiguous", 1, "Count")
                self._notice(
                    {"kind": "ambiguous_name", "agentId": agent_id}, [identity]
                )
            if a.ambiguous:
                self._unrouted[identity] = now
            if (
                a.ambiguous
                or not real_speech(text)
                or pause(text)
                or stop_request(text, self._team["agents"])
            ):
                return
            if self.setting == "meeting":
                # The floor rule: a question nobody was named in waits for a person to answer it first;
                # anything else that names no agent wakes nobody.
                if is_question(text):
                    self._unclaimed = (now + UNCLAIMED_QUESTION_S, identity, text, mark)
                if talking:
                    # Its speaker is talking with an agent whose voice has closed by now (10-07 rig: "And
                    # what are they called?" waited out the unclaimed delay, 6.8 s). It goes to /route at
                    # once with that exchange as evidence, and /route decides whether it is for the agent.
                    # Once: a line /route gives nobody leaves the question unclaimed as before.
                    self._routes(
                        self._route_and_summon(
                            identity, text, mark, humans=len(self._people), **rk
                        )
                    )
                elif between:
                    self._routes(self._line_between(identity, text, mark))
                return
            # No name in a wake position: the backend routes the line as a chat message (Jev may pick nobody).
            self._routes(
                self._route_and_summon(
                    identity, text, mark, humans=len(self._people), **rk
                )
            )
            return
        # The voice's own speaker, talking with it.
        if (
            len(a.agent_ids) > 1
        ):  # "Forge and Gauge" to Forge, who holds the floor: Gauge answers next
            self._queue = [
                (aid, identity, text, self._label(identity, mark))
                for aid in a.agent_ids[1:]
            ]
            if (
                v.live is not None
            ):  # Forge's answer is a driven run, so the queue waits for it (D §5)
                self._drive_request(v, text)
                self._say_ack(v, None, driven=True)
            self._sync_state()
        switch = bool(v.live is not None and [x for x in a.anywhere if x != v.agent_id])
        if switch:
            # "Cool, and Friday, what about you?" while Forge holds the floor (10-02: it went to Forge).
            # The backend's addressing stage decides addressed vs mentioned; addressed switches the floor.
            self._routes(
                self._route_and_summon(
                    identity, text, mark, humans=len(self._people), switch=True, **rk
                )
            )
        v.last_activity = v.followed_at = (
            self._clock()
        )  # a follow-up restarts its window
        v.used = True
        if routed is None and not switch and real_speech(text) and not pause(text):
            # understanding §4.1: the worker's own STT final is the request
            named = v.agent_id in self._named(text, a)
            self._understand_floor(
                identity,
                text,
                mark,
                humans=len(self._people),
                floor_line=True,
                named=named,
            )

    def _unwanted(self, v: _Voice, identity: str) -> None:
        """live.route.unwanted: the asker stops a Jev-routed voice, or names another agent, right away."""
        if (
            v.path == "jev"
            and self._mine(v, identity)
            and self._clock() - (v.opened or self._clock()) <= UNWANTED_SECONDS
        ):
            emit_metric("live.route.unwanted", 1, "Count")

    def on_interim_transcript(self, identity: str, name: str, text: str) -> None:
        """Partial STT text: what a person is saying so far, before its final. A mouth they talk over yields once
        it is more than a listening sound (`_yield_check`)."""
        text = text.strip()
        if not text:
            return
        self._utter_from.setdefault(identity, self._clock())
        unheard = [
            e
            for e in self._turns.get(identity, [])
            if e.state in ("speaking", "awaiting-final")
        ]
        if unheard:
            unheard[-1].words = text
            self._listened(unheard[-1])
            self._yield_check()  # more than a listening sound by now: a mouth they talk over yields

    def _listened(self, ep: Epoch) -> None:
        """A person stopped, and all the transcriber has of it so far is a listening sound: it asked nothing,
        so nothing waits for its final transcript (1.2-1.9 s). A mouth it paused goes on now (`_talked_over`),
        and the turn no longer holds the room (`_turn_open`). The final still comes, and is read as any line."""
        if ep.state == "awaiting-final" and ep.listening:
            self._talked_over(ep.who, ep.words)
            self._pump()

    def _wake_fired(self, identity: str, agent_id: str, match: str) -> dict[str, str]:
        """live.wake.fired (D Unknown 5): which kind of name woke an agent, and how well it matched."""
        row = self._agent(agent_id) or {}
        name = (row.get("name") or "").strip().lower()
        people = {
            n.lower()
            for p in self._team["people"]
            for n in ((p.get("name") or "").strip(), *(p.get("name") or "").split()[:1])
        }
        kind = (
            "collision"
            if name in people
            else "default"
            if row.get("defaultName")
            else "custom"
        )
        dims = {"name_kind": kind, "match": match}
        emit_metric("live.wake.fired", 1, "Count", dims)
        failed = self._failed_at.pop((identity, agent_id), None)
        if failed is not None and self._clock() - failed <= REPEAT_SECONDS:
            emit_metric("live.wake.repeat", 1, "Count", dims)
        return dims

    async def _answer_heard(
        self,
        v: _Voice,
        identity: str,
        text: str,
        heard: asyncio.Future,
        writing: asyncio.Task | None = None,
    ) -> None:
        """A line to the voice, which hears nothing itself: once understood, it says what came of it. `writing`:
        its small-talk reply, begun beside /route (`_prewrite`)."""
        label = v.label
        try:
            rec = await heard
            if v.label != label:
                return  # a meeting: someone else named the agent meanwhile; this line's rows run without its voice
            if rec is not None and rec[1] is not None:
                if self._heard(v, identity, text, rec[0], rec[1], writing):
                    writing = None  # it is the reply
            # /route down: D §5, their whole line is the request, started by us like a summon's
            elif rec is not None and self._floor_voice(v.agent_id) is v:
                self._drive_request(v, text)
                self._say_ack(v, None, driven=True)
        finally:
            if writing is not None:
                writing.cancel()  # the line was not small talk: its reply is not wanted

    async def _lightbulb_over(
        self, v: _Voice, identity: str, text: str, mark: int
    ) -> None:
        """A "Lightbulb, ..." from someone the open voice is not with: that voice closes (cut off if it was
        talking), then the line routes as it does with no voice open."""
        await self.end_exchange(only=v)
        await self._route_and_summon(identity, text, mark)

    def _heard(
        self,
        v: _Voice | None,
        identity: str,
        text: str,
        rows: list[Request],
        o: dict,
        written: asyncio.Task | None = None,
    ) -> bool:
        """`identity` said `text` to the voice: it says what came of it (understanding §4.6). `o`: the line's
        `ops`. `written`: a small-talk reply already being written. True: it was used."""
        if v is None or self._voice is not v or v.speaker != identity:
            return False
        v.ack_rows = [
            r for r in v.ack_rows if r not in rows
        ]  # acknowledged here, not again in tick()
        if rows:
            self._say_ack(v, rows)
        elif line := self._owed(o, v.agent_id, text):
            self._say_line(v, line, written if line[0] == "social" else None)
            return line[0] == "social" and written is not None
        return False

    def _reply(self, v: _Voice, text: str, written: asyncio.Task | None = None) -> None:
        """`text` was said to the voice, which heard none of it: a small text model is given the line and who
        said it, and nothing else, and its one sentence joins what the room's mouths have to say. `written`: that
        sentence, already being written (while the voice opened)."""
        write = written or self._spawn(self._writer(v.persona, self._said_to(v, text)))
        u = self._utter(
            v.agent_id, "social", audience=v.speaker, label=v.label, write=write
        )

        def done(t: asyncio.Task) -> None:
            u.write = None
            u.units = [] if t.cancelled() or t.exception() else units(t.result() or "")
            # It took time to write. Its speaker has said something else since, or has turned away from
            # the agent: it would answer a line that is no longer the last one. Silence instead.
            e = self._engagement((u.audience, u.label), u.agent)
            stale = self._final_mark.get(u.audience, -1) != u.mark or (
                e is not None and e.state != "active"
            )
            if not u.units or stale:
                self._strike([u])
            self._pump()

        write.add_done_callback(done)

    def _said_to(self, v: _Voice, text: str) -> str:
        return f'{self._with(v) or "Someone"} just said to you: "{text}"'

    # -- who is talking with which agent (`Engagement`) --

    def _who(
        self, identity: str | None, mark: int | None = None
    ) -> tuple[str, str] | None:
        """The person behind a line, as an engagement knows them. None: a mixed meeting's line with no one
        known speaker, which is nobody's side of an exchange."""
        if identity is None:
            return None
        label = self._label(identity, mark)
        return None if mixed_meet(identity) and not label else (identity, label)

    def _engagements(self, who: tuple[str, str] | None = None) -> list[Engagement]:
        """The exchanges still open (of `who`), the latest first. The rule: an exchange ends when someone
        else takes a turn that is not part of it. Silence ends nothing, however long, and the next thing its
        person says may still be for the agent (/route decides, with the exchange as evidence).

        What a turn of someone else was is /route's to say, never a clock's or a count's. One said among
        people is not part of it and suspends the exchange (`_between`); an endorsement of the request or a
        line to an agent comes between nobody. A suspended exchange with nothing owed is over once anyone
        else has spoken since. One whose answer is still to come stays suspended, so the answer waits for
        an opening. ENGAGE_SAFETY_S closes one that nothing ended, and is only a safety net. One whose
        agent is speaking to its person right now is never ended: each unit it finishes is the exchange
        going on (`_unit_said`)."""
        now = self._clock()
        saying = {
            ((u.audience, u.label), u.agent)
            for u in self._ledger
            if u.state == "speaking"
        }
        for key, e in list(self._engaged.items()):
            if key in saying:
                continue
            old = now - e.at >= ENGAGE_SAFETY_S
            taken = e.state == "suspended" and any(
                at > e.at for other, at in self._turn_at.items() if other != e.person
            )
            if not (old or taken):
                continue
            if e.expects == "answer" and self._owes(e):
                if e.state == "active":
                    e.state, e.why = "suspended", "gap"
            else:
                del self._engaged[key]
        found = [e for e in self._engaged.values() if who is None or e.person == who]
        return sorted(found, key=lambda e: -e.at)

    def _engagement(
        self, who: tuple[str, str] | None, agent_id: str
    ) -> Engagement | None:
        return (
            next((e for e in self._engagements(who) if e.agent == agent_id), None)
            if who
            else None
        )

    def _owes(self, e: Engagement) -> bool:
        """The agent still has something for that person: an answer to say, or work of theirs not ended."""
        identity, label = e.person
        return (
            any(
                u.agent == e.agent
                and u.kind == "answer"
                and (u.audience, u.label) == e.person
                for u in self._ledger
            )
            or any(
                r.agent == e.agent and (r.asker, r.label) == (identity, label)
                for r in self._requests.open()
            )
            or any(run.speaker == identity for run in self._runs.running(e.agent))
        )

    def _engage(
        self,
        who: tuple[str, str] | None,
        agent_id: str,
        obligation: str | None = None,
        expects: str | None = None,
    ) -> None:
        """The person spoke to the agent (a request, its name, "go ahead"): they are talking with each other."""
        if who is None:
            return
        e = self._engaged.setdefault(
            (who, agent_id), Engagement(who, agent_id, self._clock())
        )
        e.state, e.why, e.at = "active", "", self._clock()
        e.obligation = obligation or e.obligation
        e.expects = expects or e.expects
        self._pump()  # what the agent had for them may start now

    def _working_for(self, v: _Voice, row: Request | None) -> None:
        """A mouth's voice drives a request of its speaker: they are waiting for its answer."""
        if v.label or not mixed_meet(v.speaker):
            about = row.id if row is not None else v.driven_run
            self._engage(
                (v.speaker, v.label), v.agent_id, obligation=about, expects="answer"
            )

    def _suspend(
        self, who: tuple[str, str] | None, why: str, agent_id: str | None = None
    ) -> None:
        """The person is no longer talking with the agent(s): what an agent has for them waits for a pause in
        the conversation, or for them to turn back to it. Nothing is cancelled."""
        for e in self._engagements(who) if who else []:
            if e.state == "active" and agent_id in (None, e.agent):
                e.state, e.why = "suspended", why
                self._gate_log("suspend", e.agent, why, e.person[1], 0)

    def _invite(self, who: tuple[str, str] | None, agent_id: str) -> None:
        """The person turned to the agent and asked nothing new ("Friday?", "go ahead"): it speaks next,
        also about what it was told to be quiet about."""
        self._selected = agent_id
        self._unhush(agent_id)
        self._engage(who, agent_id)

    def _others_talking(self, who: tuple[str, str] | None) -> bool:
        """Someone other than `who` is talking with an agent: what `who` says next may come between them."""
        return any(e.person != who and e.state == "active" for e in self._engagements())

    def _between(self, who: tuple[str, str] | None, said: float | None = None) -> None:
        """`who` said a line to a person, or people are settling something among themselves (/route's
        `ops.people`): another exchange has come between every other person and the agent they were talking
        with. What a line is decides this, never how many lines there were: an endorsement of the request,
        said twice or ten times, comes between nobody.
        `said`: when the line was said. An exchange begun or taken up again after it is left alone. The
        line came before it, so it did not come between them."""
        for e in self._engagements():
            if (
                e.person != who
                and e.state == "active"
                and (said is None or e.at <= said)
            ):
                e.state, e.why = "suspended", "other"
                self._gate_log("suspend", e.agent, "other", e.person[1], 0)

    async def _line_between(self, identity: str, text: str, mark: int) -> None:
        """A meeting line of someone who is talking with no agent, said while other people are. /route reads
        it for one thing: whether it was said to people. Nothing on it is started, steered or called off.
        It named no agent, and a request is not this person's to change."""
        said = (
            self._clock()
        )  # /route can answer late. Its answer is about the room as it was now.
        r = await self._understand(
            identity, text, len(self._people), quiet=True, path="between", mark=mark
        )
        if ((r or {}).get("ops") or {}).get("people"):
            self._between(self._who(identity, mark), said)

    def _evidence(self, who: tuple[str, str] | None) -> list[dict]:
        """What /route is told about the speaker and each agent they are talking with."""
        now = self._clock()
        tasks = {r.id: r.task for r in self._requests.items}
        return (
            [
                {
                    "agentId": e.agent,
                    "obligation": e.obligation or "",
                    "task": tasks.get(e.obligation or "", "")[:200],
                    "state": e.state,
                    "expects": e.expects,
                    "why": e.why,
                    "last": e.last,
                    "lastBy": e.last_by,
                    "seconds": round(now - e.at),
                }
                for e in self._engagements(who)[:8]
            ]
            if who
            else []
        )

    def _open_rows(self) -> list[dict]:
        """The request rows /route sees, each with who asked and how far its answer has been said."""
        rows = self._requests.for_route()
        for x in rows:
            r = self._requests.get(x["id"])
            u = next(
                (
                    u
                    for u in self._ledger
                    if u.kind == "answer" and r.run_id and u.obligation == r.run_id
                ),
                None,
            )
            x["asker"] = r.label or self._people.get(r.asker, "")
            x["delivery"] = (
                u.state if u else "delivered" if r.status == "delivered" else "none"
            )
        return rows

    # -- the room's speech. Everything a mouth says is one `Utterance` in the room's one ledger, whoever's it --
    # -- is and whichever voice is open. `_pump` starts it, `on_agent_said` reports it unit by unit.          --

    def _utter(
        self,
        agent_id: str,
        kind: str,
        text: str = "",
        *,
        obligation: str | None = None,
        request: str = "",
        audience: str | None = None,
        label: str = "",
        at: float | None = None,
        write: Any = None,
    ) -> Utterance | None:
        """`agent_id` has `text` to say. None: nothing to say, or what it would say is already superseded.
        A ready answer takes the place of its request's waiting acknowledgement and "still working" line, and
        of an older answer to the same run (a failure line). With the answer ready, neither is added."""
        us = units(text)
        if not us and write is None:
            return None
        if self._hushed(obligation):
            # Silenced, and not withdrawn: a line about it is not said, and its answer is written (`_hush`).
            self._gate_log("hushed", agent_id, kind, obligation or "", 0)
            if kind == "answer":
                when = self._clock() if at is None else at
                self._spawn(
                    self._late_answer(
                        agent_id, (obligation, request, text, when, audience)
                    )
                )
            return None
        mine = [x for x in self._ledger if x.agent == agent_id]
        if kind == "answer":
            self._strike(
                x
                for x in mine
                if x.state != "speaking"
                and (
                    x.obligation == obligation
                    if x.kind == "answer"
                    else self._same(x.obligation, obligation)
                )
            )
        elif any(
            x.kind == "answer"
            and (kind == "status" or self._same(obligation, x.obligation))
            for x in mine
        ):
            # (a "still working" line is about whatever the agent has open: any answer of its own says more)
            self._gate_log("superseded", agent_id, kind, "answer", 0)
            return None
        else:
            # the newer line about the same thing to the same person is the one worth saying
            same = (kind, obligation, audience, label)
            self._strike(
                x
                for x in mine
                if x.state == "pending"
                and (x.kind, x.obligation, x.audience, x.label) == same
            )
        self._uid += 1
        u = Utterance(
            self._uid, agent_id, kind, us, self._clock() if at is None else at, obligation, request, audience, label,
            write=write, mark=self._final_mark.get(audience or "", -1),
        )  # fmt: skip
        self._ledger.append(u)
        self._sync_state()
        self._pump()
        return u

    def _same(self, about: str | None, run_id: str | None) -> bool:
        """What a line is `about` (a run, or a request row) is the request the run `run_id` answers."""
        return bool(about and run_id) and (
            about == run_id or about in {r.id for r in self._requests.by_run(run_id)}
        )

    def _rows_of(self, obligation: str | None) -> list[Request]:
        """The request rows an obligation names: by its run, else by its row id."""
        row = self._requests.get(obligation or "")
        return self._requests.by_run(obligation) or ([row] if row is not None else [])

    def _working_on(self, agent_id: str) -> str | None:
        """What a "still working" line is about: the row the agent works on, else its running run."""
        rows = [
            r
            for r in self._requests.items
            if r.agent == agent_id and r.status in ("working", "queued")
        ]
        rows.sort(key=lambda r: r.status != "working")
        runs = self._runs.running(agent_id)
        return (
            rows[0].run_id or rows[0].id if rows else runs[0].run_id if runs else None
        )

    def _strike(self, us) -> None:
        """Utterances leave the ledger unsaid (cancelled, superseded, stale). One being said is cut."""
        for u in list(us):
            if u not in self._ledger:
                continue
            if u.state == "speaking" and (v := self._floor_voice(u.agent)):
                self._live_call(v, "interrupt")
            self._ledger.remove(u)
            u.state = "cancelled"
            if u.write is not None:
                u.write.cancel()

    def _news_of(self, u: Utterance) -> tuple:
        """An answer's unsaid rest as the room is given it in writing (`_late_answer`)."""
        return (
            u.obligation or f"u{u.id}",
            u.request,
            " ".join(u.units[u.cursor :]),
            u.at,
            u.audience,
        )

    def _expire(self, now: float) -> None:
        """What is no longer worth saying leaves the ledger, each kind by its own rule. An answer never
        expires while its asker is here: gone a minute, or with a voice that will not open or will not play
        it (PLAYBACK_TRIES), it goes to the room in writing.

        A line (an acknowledgement, a status, small talk) is owed to a moment, and is dropped once the moment
        has passed: its person left or is no longer talking with the agent (`turned`), someone else has
        taken a turn since it was owed (`floor`), its request (an acknowledgement) is no longer being worked
        on (`done`), it is older than its kind allows (`EXPIRES_S`), or it will not play. One still being
        written is judged when it is written (`_reply`)."""
        for u in list(self._ledger):
            if u.state == "speaking" or u.write is not None:
                continue
            gone = u.audience is not None and u.audience not in self._people
            if u.kind == "answer":
                tries, at = self._delivery_tries.get(u.obligation or "", (0, 0.0))
                if (
                    (gone and now - u.at >= ASKER_GONE_S)
                    or (u.audience is None and now - u.at >= NEWS_TTL)
                    or (tries >= DELIVERY_TRIES and now - at >= gate.ANNOUNCE_QUIET)
                    or u.tries >= PLAYBACK_TRIES
                ):
                    self._ledger.remove(u)
                    u.state = "cancelled"
                    self._post_news(u.agent, self._news_of(u))
                    self._sync_state()  # its hand is down
                continue
            rows = self._rows_of(u.obligation)
            person = (u.audience, u.label)
            e = self._engaged.get((person, u.agent)) if u.audience else None
            why = (
                "gone" if gone
                else "done" if u.kind == "ack" and rows and not any(r.status in ("queued", "working") for r in rows)
                else "failed" if u.tries >= PLAYBACK_TRIES
                else "turned" if e is not None and e.state != "active"
                else "floor" if any(at > u.at for who, at in self._turn_at.items() if who != person)
                else "old" if now - u.at >= EXPIRES_S[u.kind]
                else ""
            )  # fmt: skip
            if why:
                self._gate_log("expired", u.agent, u.kind, why, 0, u)
                self._strike([u])

    def _rank(self, u: Utterance) -> tuple:
        """Arbitration, the same for every agent: a line owed to someone who just spoke (oldest first), then
        answers in the order their requests were asked, the agent a person just turned to first."""
        if u.kind != "answer":
            return (0, False, 0, u.at, u.id)
        return (
            1,
            u.agent != self._selected,
            self._requests.order(u.obligation or ""),
            u.at,
            u.id,
        )

    def _hold(self, u: Utterance, now: float) -> tuple[str, float | None]:
        """The start rule. ("", None): `u` may start now. Else why it waits, and in how many seconds to look
        again (None: an event will pump).

        Nothing starts while any person has a turn that is not resolved (`_turn_open`). Nor while its own
        person is away, or (a line) another agent has the open voice.
        Then the room only has to settle (SETTLE_S) when the speech is plainly wanted: a line owed to what
        was just said, an utterance a backchannel paused, the agent a person just turned to, or an answer
        whose asker is still talking with the agent and waiting for it. Any other answer keeps its hand up
        until the conversation pauses (MEET_QUIET_S) or someone turns to the agent. After speech nobody
        understood (`_final_timeout`) a line is not plainly wanted either: it waits for that pause too."""
        if u.write is not None:
            return "writing", None
        if u.cut_at and (left := RECEIPT_WAIT_S - (now - u.cut_at)) > 0:
            return (
                "receipt",
                left,
            )  # how much of the cut sentence was audible decides where it goes on from
        if (open_turn := self._turn_open(now)) is not None:
            return open_turn
        if u.audience is not None and u.audience not in self._people:
            return "absent", None
        if (
            u.kind != "answer"
            and (v := self._voice) is not None
            and v.agent_id != u.agent
        ):
            return (
                "voice",
                None,
            )  # a line never takes the voice from another agent. An answer may.
        e = self._engagement((u.audience, u.label), u.agent) if u.audience else None
        wanted = (
            (u.kind != "answer" and self._unknown_at <= u.at)
            or u.resume
            or self._selected == u.agent
            or (
                u.kind == "answer"
                and e is not None
                and e.state == "active"
                and e.expects == "answer"
            )
        )
        if u.resume:
            return (
                "",
                None,
            )  # only a listening sound paused it: nobody took the floor, and no room has to settle
        quiet, since = (
            (SETTLE_S, self._talk_until) if wanted else (MEET_QUIET_S, self._last_heard)
        )
        left = quiet - (now - since)
        return ("quiet", left) if left > 0 else ("", None)

    def _turn_open(self, now: float) -> tuple[str, float | None] | None:
        """The one rule for starting speech: nothing starts while any person has a turn that is not
        resolved. Why, and in how many seconds to look again (None: an event will pump). None: no turn is
        open.

        A turn is open while its person is speaking (`person`: the room hears them, or their speech began
        and the detector has not said it ended, at most DETECTOR_LOST_S after the SFU's last sound of them,
        then `_detector_lost`), while they have stopped and the transcript of it is not here (`final`, at most FINAL_WAIT_S, then `_final_timeout`), while that transcript is
        being routed and its effects applied (`route`, however long: an answer that is not here is never
        taken for a yes; also a meeting's question that named nobody, until a person or /route has taken
        it: `_unclaimed`), and while the line was left unfinished (`turn`: /route said hold, or it trailed
        off and its rest is waited for CALL_ON_S)."""
        if self._speaking:
            return "person", None
        left: float | None = None
        for eps in list(self._turns.values()):
            for ep in list(eps):
                if ep.state == "speaking":
                    # Not in `_speaking`: the SFU's sound of them went stale, which is no sign they stopped,
                    # or has not come yet (it runs up to 2.5 s late). The detector alone holds what has not
                    # begun: holding costs a moment. It never stops a sentence being said (`_yield_check`):
                    # that takes the SFU's sound too, because a false stop costs the sentence.
                    # The other way round was tried and is not done: the SFU's sound alone, held for as long
                    # as the detector might still be about to hear them (PREROLL_S), bought nothing (the
                    # detector is rarely the later of the two) and, with a cough or a key every 10 s on each
                    # silent track, held 4,000 starts in 3,000 replays and put 18% more acknowledgements
                    # past 4 s (test_voice_rules).
                    heard = max(self._level_at.get(ep.who, -math.inf), ep.began)
                    if (lost := DETECTOR_LOST_S - (now - heard)) > 0:
                        return "person", lost
                    self._detector_lost(ep, now)
                if ep.state == "awaiting-route":
                    return "route", None
                if ep.state == "awaiting-final":
                    if (wait := FINAL_WAIT_S - (now - ep.ended)) <= 0:
                        self._final_timeout(ep)
                    elif (
                        ep.credible and not ep.listening
                    ):  # (a listening sound asks nothing: `_listened`)
                        left = max(left or 0.0, wait)
        if left is not None:
            return "final", left
        if self._held:
            return "turn", None  # the tick applies it (`_apply_held`) and pumps
        if (
            rest := max(
                (self._unfinished(w, now) for w in self._incomplete), default=0.0
            )
        ) > 0:
            return "turn", rest
        return None

    def _unfinished(self, identity: str, now: float) -> float:
        """Seconds a turn its speaker has not finished still holds speech back: CALL_ON_S of their silence."""
        if identity in self._speaking:
            return CALL_ON_S
        return CALL_ON_S - (
            now
            - max(
                self._incomplete[identity][1], self._heard_at.get(identity, -math.inf)
            )
        )

    def _pump(self) -> None:
        """The one scheduler for everything the room's mouths say. It runs on every event that can open the
        floor (something to say, a unit said, talk starting or stopping, a line understood, a voice opening
        or closing, its own timer, the tick) and starts at most one utterance:

        - One agent speaks at a time. While an utterance is being said, nothing else starts.
        - Of the utterances that may start now (`_hold`), the first by `_rank`.
        - It is said through its agent's voice. If that voice is not the open one, it is opened for it
          (`_open_for`). The ledger does not change when a voice does.

        Eligibility is read here, at the moment playback starts, never when the utterance was made."""
        now, v = self._clock(), self._voice
        if self._closing or not self._ledger:
            return
        self._expire(now)
        if v is not None and (v.live is None or v.dropping):
            return  # a voice still opening pumps when it is open
        if any(u.state == "speaking" for u in self._ledger):
            return  # the report of its last unit pumps again
        waits = [(self._hold(u, now), u) for u in sorted(self._ledger, key=self._rank)]
        u = next((u for (why, _), u in waits if not why), None)
        if u is not None:
            self._start(u, now)
            return
        if waits:
            (why, _), first = waits[0]
            self._gate(first, why, now)
        if again := [left for (_, left), _ in waits if left is not None]:
            if self._gate_timer is not None:
                self._gate_timer.cancel()
            self._gate_timer = self._later(min(again), self._pump)

    def _start(self, u: Utterance, now: float) -> None:
        """Playback starts, from the cursor, through its agent's voice."""
        v = self._floor_voice(u.agent)
        if v is None:
            # Its agent's voice is not the open one: the voice is a resource, opened for what is to be said.
            # A voice that would not open is tried again after gate.ANNOUNCE_QUIET.
            tried = self._delivery_tries.get(
                u.obligation or f"u{u.id}", (0, -math.inf)
            )[1]
            busy = self._lock.locked() or (
                self._delivering is not None and not self._delivering.done()
            )
            if not busy and self._people and now - tried >= gate.ANNOUNCE_QUIET:
                self._delivering = self._spawn(self._open_for(u))
            return
        self._gate(u, "", now)
        self._order += 1
        u.state, u.over, u.resume, u.order, u.via, u.cut_at = (
            "speaking",
            set(),
            False,
            self._order,
            v,
            0.0,
        )
        u.sound_at, u.sounding = 0.0, -1
        if self._selected == u.agent:
            self._selected = None
        if (
            e := self._engagement((u.audience, u.label), u.agent)
            if u.audience
            else None
        ):
            e.last, e.last_by = "speaking", ""
        # (a dead session: `_drop` returns it)
        self._live_call(v, "say", " ".join(u.units[u.cursor :]), u.order, u.cursor)

    def may_say(self, agent_id: str, order: int | None, index: int) -> bool:
        """Playback permission for one sentence unit, and the only place it is decided. A mouth asks before
        every unit of an order, and again as that unit's first audio is about to go out.

        It is the utterance's `speaking` state: `_start` grants it, and a pause, a strike or a failed
        playback takes it away. A unit begins under the rule an utterance starts under, and no other
        (`_turn_open`): no person may have a turn that is not resolved. Someone talking is one. So is
        someone who said a few words between two units and stopped: what they said is not known yet, and
        it may have been "stop". The utterance pauses here, before the unit, at its cursor. It is not
        dropped and not counted as failed: the scheduler says it on from there once the turn is resolved,
        as after any yield (`_hold`)."""
        u = next(
            (
                u
                for u in self._ledger
                if u.agent == agent_id and u.order == order and u.state == "speaking"
            ),
            None,
        )
        why = (
            "stale"
            if u is None or u.cursor != index
            else (self._turn_open(self._clock()) or ("",))[0]
        )
        if why and why != "stale":
            self._yield(u)
            self._pump()  # the scheduler takes it from here: it notes what holds it and sets its timer
        elif not why and u.sounding != index:
            u.sound_at, u.sounding = (
                None,
                index,
            )  # permitted, and not audible yet (`on_unit_audible`)
        self._log(
            "permit", ok=not why, why=why, agent=agent_id, order=order, unit=index, utterance=u.id if u else None,
            speaking=sorted(self._speaking),
        )  # fmt: skip
        return not why

    def on_unit_audible(self, agent_id: str, order: int | None, index: int) -> None:
        """A mouth's report that the first frame with sound in it, of that unit, went to the track: the
        room begins to hear the sentence now (PREROLL_S is counted from here)."""
        for u in self._ledger:
            if (
                u.agent == agent_id
                and u.order == order
                and u.cursor == index
                and u.state == "speaking"
            ):
                u.sound_at, u.sounding = self._clock(), index

    def on_unit(
        self,
        agent_id: str,
        order: int | None,
        index: int,
        text: str,
        outcome: str,
        evidence: dict,
    ) -> None:
        """A mouth's receipt for one unit it began, by the order and unit it was for, with what went to the
        track. `said`: audible audio played to its end, and only that moves the cursor. `cut`, `revoked`:
        stopped by the worker. `silent`: nothing audible was sent. `long`: far more sound than its words
        need, and the mouth stopped it. Both are a failed playback (the evidence says `why`): the unit is not
        said, and its order's end returns the utterance to be tried again at once, at the same cursor
        (`on_playback_ended`), PLAYBACK_TRIES times at most. `attempt` in the log: which try this was.

        Delivery is counted from `said` alone. What was audible of a cut unit (`_audible`) is an estimate:
        it goes on the record as a marked piece and in the log, and moves nothing."""
        u = next(
            (u for u in self._ledger if u.agent == agent_id and u.order == order), None
        )
        heard, share = (
            (None, 1.0)
            if outcome == "said"
            else self._audible(text, evidence)
            if outcome == "cut"
            else (None, 0.0)
        )
        self._log(
            "receipt", outcome=outcome, agent=agent_id, order=order, unit=index, utterance=u.id if u else None,
            share=round(share, 2), attempt=u.tries + 1 if u else None, **evidence,
        )  # fmt: skip
        if outcome in ("silent", "long"):
            emit_metric(f"live.speech.{outcome}_unit", 1, "Count")
        if outcome == "said":
            self._spoken[0] += len(text)
            self._spoken[1] += evidence.get("loudMs", 0) / 1000
        self.on_agent_said(
            agent_id, text, outcome != "said", order=order, index=index, heard=heard
        )
        if outcome == "cut" and share > 0 and u is not None and u.kind != "answer":
            self._stale(u)

    def _audible(self, text: str, evidence: dict) -> tuple[str, float]:
        """What the room heard of a unit that was cut: its words, and their share of the unit. The mouth
        reports audible seconds, not words: the share is those seconds at the rate this call's units were
        said at, and the words are that share of the text, to a whole word."""
        # ponytail: a rate estimate, right to a word or two. The voice SDK's own played-out transcript
        # would be exact, if a record ever has to be.
        chars, seconds = self._spoken
        rate = chars / seconds if seconds >= 3.0 else SPEECH_CPS
        audible = evidence.get("heardMs", evidence.get("loudMs", 0)) / 1000
        share = min(1.0, audible * rate / max(len(text), 1))
        if share >= 1.0:
            return text, 1.0
        n = int(len(text) * share)
        cut = (
            text[:n] if text[n].isspace() else text[:n].rpartition(" ")[0]
        )  # no half word
        return cut.rstrip(" ,;:-"), share

    def _stale(self, u: Utterance) -> None:
        """A line (an acknowledgement, a status, small talk) that stopped after the room heard some of it is
        over, whether talk stopped it or its playback failed: it was owed to a moment, and its rest said
        later is a new line about nothing. An answer is owed until it is said, and goes on."""
        if u in self._ledger:
            self._gate_log("expired", u.agent, u.kind, "cut", 0, u)
            self._strike([u])

    def _unsaid(self, us, failed: bool = False) -> None:
        """No playback is behind these utterances any more: each one that was being said is pending again at
        its cursor, and the floor is free. `failed`: the playback itself went wrong (it raised, ended before
        its last unit, or its session died), which is counted. After PLAYBACK_TRIES in a row an answer goes
        to the room in writing and a line is dropped (`_expire`). A voice the worker closed or told to be
        quiet for another one is no failure."""
        for u in list(us):
            if u.state != "speaking":
                continue
            u.state, u.via = "pending", None
            u.tries += failed
            self._gate_log(
                "unsaid", u.agent, u.kind, "failed" if failed else "voice", 0, u
            )
            if u.kind != "answer" and u.cursor > 0:
                self._stale(u)  # some of the line was said, then its playback stopped

    def on_playback_ended(
        self, agent_id: str, order: int | None, failed: bool = False
    ) -> None:
        """A mouth's receipt for one order (`_start`): nothing more of it will be said. `failed`: it raised.
        Its last unit reported said, a yield or a strike came first and settled the utterance. One still
        `speaking` here has nothing playing behind it (10-07 review: a failed order was only logged, and the
        room said nothing more), so it is returned and the scheduler runs."""
        us = [
            u
            for u in self._ledger
            if u.order == order and u.agent == agent_id and u.state == "speaking"
        ]
        if us:
            emit_metric(
                "live.speech.playback_ended",
                1,
                "Count",
                {"outcome": "failed" if failed else "early"},
            )
            self._unsaid(us, failed=True)
            self._pump()
        for u in self._ledger:
            if u.order == order and u.agent == agent_id and u.cut_at:
                u.cut_at = 0.0  # every receipt of the playback that was cut is in
                self._pump()

    async def _open_for(self, u: Utterance) -> None:
        """Its own agent's voice opens to say `u`, with whoever it is for (else the last speaker, else anyone
        here). An open that fails is counted: after DELIVERY_TRIES an answer goes to the room (`_expire`), so
        a backend outage cannot open a session every tick."""
        speaker = next(
            (p for p in (u.audience, self._last_speaker) if p in self._people), None
        ) or min(self._people)
        self._note(
            f"[announce {self._agent_name(u.agent)} opens its voice for: {u.request[:80]}]"
        )
        await self.summon(
            speaker, u.agent, request=None, path="announce", label=u.label
        )
        key = u.obligation or f"u{u.id}"
        if self._floor_voice(u.agent) is None:
            self._delivery_tries[key] = (
                self._delivery_tries.get(key, (0, 0.0))[0] + 1,
                self._clock(),
            )
        else:
            self._delivery_tries.pop(key, None)
        self._pump()

    def _later(self, delay: float, fn: Callable[[], Any]) -> Any:
        """`fn` once `delay` has passed: speech starts and stops on its own timer, not on the 1 Hz tick (which
        looks too). None with no loop running."""
        try:
            return asyncio.get_running_loop().call_later(max(delay, 0.0), fn)
        except RuntimeError:
            return None

    def _gate(self, u: Utterance, why: str, now: float) -> None:
        """What the scheduler decided, for the log: one line when speech starts to wait (which rule holds
        it) and one when it starts (how long it waited). 10-07 rig: three answers that started over a
        person or were not stopped could not be explained for lack of it."""
        held = self._gate_held
        same = held is not None and held[:2] == (u.agent, u.kind)
        if why:
            if not same or held[2] != why:
                self._gate_held = (u.agent, u.kind, why, held[3] if same else now)
                emit_metric(
                    "live.speech.held", 1, "Count", {"kind": u.kind, "why": why}
                )
                self._gate_log("hold", u.agent, u.kind, why, 0, u)
            return
        waited = round((now - held[3]) * 1000) if same else 0
        self._gate_held = None
        emit_metric(
            "live.speech.waited",
            waited,
            "Milliseconds",
            {"kind": u.kind, "why": held[2] if same else "none"},
        )
        self._gate_log("start", u.agent, u.kind, held[2] if same else "", waited, u)

    def _gate_log(
        self,
        event: str,
        agent_id: str,
        kind: str,
        why: str,
        waited_ms: int,
        u: Utterance | None = None,
    ) -> None:
        """One decision about speech, with who the room heard talking when it was made: `level` is the SFU's
        sound, `voiced` the transcriber's speech detector, `speaking` what the room takes for talk."""
        self._log(
            "gate", event=event, agent=agent_id, kind=kind, why=why, waitedMs=waited_ms,
            utterance=u.id if u else None, order=u.order if u else None, unit=u.cursor if u else None,
            speaking=sorted(self._speaking), level=sorted(self._level), voiced=sorted(self._voiced),
            turns={w: [e.state for e in eps] for w, eps in self._turns.items()},
        )  # fmt: skip

    # -- yield, hush, withdraw: three different things a person does to an agent that is speaking --

    def _turn_from(self, identity: str) -> float:
        """When the turn `identity` is in began: the first sign of their oldest speech still open."""
        return min(
            (e.began for e in self._turns.get(identity, []) if e.credible),
            default=self._clock(),
        )

    def _yield_check(self) -> bool:
        """Being talked over is the signal to yield. A mouth hears nobody, so the worker pauses it for
        anyone, its asker included, who is talking and whose turn began TALK_OVER_S ago. Run by a timer set
        when the talk began (`_talking_changed`), and by the tick. True: it yielded."""
        u = next((u for u in self._ledger if u.state == "speaking"), None)
        now = self._clock()
        # (the timer may fire a moment early)
        over = [
            w
            for w in self._speaking
            if now - self._turn_from(w) >= TALK_OVER_S - 0.05
            and not self._only_listening(w)
        ]
        if u is None or not over:
            return False
        self._yield(u)
        return True

    def _only_listening(self, identity: str) -> bool:
        """All the transcriber has of what `identity` is saying is a listening sound ("mm-hm", "yeah"): a
        sentence being said is not stopped for it. Words that say more make it yield as they come
        (`on_interim_transcript`); with no words yet it yields as for any talk."""
        speaking = [e for e in self._turns.get(identity, []) if e.state == "speaking"]
        return bool(speaking) and all(e.listening for e in speaking)

    def _yield(self, u: Utterance) -> None:
        """Yield: the utterance loses its permission to play, is paused at its cursor, and the floor is
        free. Its voice stays open and its agent's work goes on. This is only the acoustic pause. Nothing
        is decided here about whether the utterance is still wanted: the talker's words decide that once
        they are known (`_talked_over`), and `_hold` resumes it by them."""
        emit_metric("live.meet.talked_over", 1, "Count")
        now = self._clock()
        u.state, u.cut_at = "paused", now
        # Who it paused for: whoever talks now, and whoever spoke and has not been understood yet. Their
        # words, once known, say what the talk was (`_talked_over`).
        unheard = ("speaking", "awaiting-final")
        u.over |= self._speaking | {
            w
            for w, eps in self._turns.items()
            if any(e.credible and e.state in unheard for e in eps)
        }
        if v := self._floor_voice(u.agent):
            self._live_call(v, "interrupt")
        if (
            e := self._engagement((u.audience, u.label), u.agent)
            if u.audience
            else None
        ):
            e.last, e.last_by = "paused", "speaker" if u.audience in u.over else "other"
        onset = min((self._turn_from(w) for w in self._speaking), default=now)
        # `waitedMs`: first sign of their speech -> the interrupt. The unit's receipt adds interrupt -> silence.
        self._gate_log(
            "revoke",
            u.agent,
            u.kind,
            ",".join(sorted(u.over)),
            round((now - onset) * 1000),
            u,
        )
        if u.kind != "answer" and u.cursor > 0:
            self._stale(u)  # some of the line was said, then talk stopped it

    def _talked_over(
        self, identity: str, text: str, who: tuple[str, str] | None = None
    ) -> None:
        """The words of someone who talked over a mouth are known. The yield already paused it: this decides
        what the talk was. Only listening sounds ("mm-hm, yeah", or noise with no words): it resumes at once
        from its cursor. Real words from someone else, or "hold on" from its own person: the floor was
        taken, and it waits with its hand up. Real words from its own person (`who`) are understood like any
        line of theirs (/route), which may amend it, call it off, or turn them away from the agent."""
        for u in self._ledger:
            if u.state != "paused" or identity not in u.over:
                continue
            listening = backchannel(text)
            if listening:
                u.resume = True
            elif (u.audience, u.label) != who or pause(text):
                why = "people" if (u.audience, u.label) == who else "other"
                self._suspend(
                    (u.audience, u.label) if u.audience else None, why, u.agent
                )
            self._gate_log(
                "resume" if listening else "floor_taken",
                u.agent,
                u.kind,
                identity,
                0,
                u,
            )

    def _hush(
        self, agent_id: str, about: list | tuple = (), owed: list | None = None
    ) -> None:
        """Speech suppression ("stop", "not now", /route's `ops.hush`): what the agent has to say is struck
        and none of it resumes. `about`: the request rows the stop named ("stop talking about the deck").
        None named: everything the agent has open.
        Its work is not touched. The silence is kept on the obligation (`_written`), so it also holds for
        what the work produces later: a status line about it is not said, and its answer, like an answer
        already here, goes to the room in writing. It was asked for and not withdrawn ("don't post that" and
        a cancelled request still stop that). The agent speaks about it again when someone turns to it
        (`_unhush`). `owed`: the caller posts the struck answers itself, once it knows none was withdrawn."""
        rows = [
            r
            for r in map(self._requests.get, map(str, about))
            if r is not None and r.agent == agent_id
        ]
        mine = [u for u in self._ledger if u.agent == agent_id]
        if rows:
            ids = {x for r in rows for x in (r.id, r.run_id) if x}
            mine = [u for u in mine if u.obligation in ids]
        else:
            ids = {u.obligation for u in mine} | {
                run.run_id for run in self._runs.running(agent_id)
            }
            ids |= {r.id for r in self._requests.open() if r.agent == agent_id}
        self._written |= {o: agent_id for o in ids if o}
        self._strike(mine)
        for e in self._engagements():
            if (
                e.agent == agent_id
                and e.state == "active"
                and (not rows or e.obligation in ids)
            ):
                e.state, e.why = "suspended", "hushed"
        for u in mine:
            if (
                e := self._engagement((u.audience, u.label), agent_id)
                if u.audience
                else None
            ):
                e.last, e.last_by = "interrupted", "speaker"
            if u.kind == "answer":
                whole = (
                    *self._news_of(u)[:2],
                    " ".join(u.units),
                    u.at,
                    u.audience,
                )  # it reads on its own
                if owed is None:
                    self._spawn(self._late_answer(agent_id, whole))
                else:
                    owed.append((agent_id, whole))
        if mine:
            emit_metric("live.speech.hushed", 1, "Count")
        self._sync_state()
        self._pump()

    def _hushed(self, obligation: str | None) -> bool:
        """Speech about this obligation was silenced: by its own id, its run's or its request row's."""
        if not obligation or not self._written:
            return False
        return obligation in self._written or any(
            r.id in self._written or r.run_id in self._written
            for r in self._rows_of(obligation)
        )

    def _unhush(self, agent_id: str) -> None:
        """Someone turned to the agent again (called on it, or asked it something new): it may speak, also
        about what it was told to be quiet about."""
        self._written = {o: a for o, a in self._written.items() if a != agent_id}

    async def _withdrawal(
        self, identity: str, text: str, mark: int, owed: list
    ) -> None:
        """A whole-line withdrawal ("never mind", "Forge, cancel that") in a room of mouths. The words say a
        request is taken back and not which one. /route names it (`ops.cancel`), and only then is a run
        aborted. A request /route cannot name is silenced (`ops.hush`) and its work goes on.
        `owed`: the answers the stop cut. One that was not withdrawn goes to the room in writing."""
        try:
            r = await self._understand(
                identity, text, len(self._people), quiet=True, path="stop", mark=mark
            )
            ops = (r or {}).get("ops") or {}
            for rid in ops.get("cancel") or []:
                if (
                    row := self._requests.get(str(rid))
                ) is not None and row.status in OPEN:
                    self._cancel(row)
            for x in ops.get("hush") or []:
                self._hush(str(x.get("agentId") or ""), x.get("requestIds") or ())
        finally:
            for agent_id, whole in owed:
                await self._late_answer(
                    agent_id, whole
                )  # it posts nothing of a cancelled request

    def _stop_said(self, identity: str, text: str, mark: int) -> bool:
        """A line that is nothing but a stop, in a room of mouths (the instant path, no /route). False: it
        stopped nothing, and is people talking.

        Whose it is: the agents it names. Else the agents whose speech this person talked over. Else, for a
        stop with no word before it, the agents speaking to them or talking with them.
        What it does: it silences what the agent has to say, now and when its work ends, and leaves that
        work running (`_hush`). It never aborts a run. An agent that is saying nothing is silenced only by
        its name: silence does not show that its work is unwanted, so "Forge, stop" has Forge's answer
        written, and "stop" changes nothing for it. A withdrawal ("never mind", "cancel that") is also read
        by /route, which alone cancels a request (`_withdrawal`)."""
        agents = self._team["agents"]
        named = stop_target(text, agents)
        names = (
            [named]
            if named
            else self._named(text, address(text, agents, self._team["people"]))
        )
        who = self._who(identity, mark)
        said = [u for u in self._ledger if u.state in ("speaking", "paused")]
        over = [u for u in said if identity in u.over]
        mine = [u for u in said if (u.audience, u.label) == who]
        waiting = (
            [e for e in self._engagements(who) if e.state == "active"]
            if stop_bare(text)
            else []
        )
        targets = (
            names
            or [u.agent for u in over]
            or [u.agent for u in mine if stop_bare(text)] + [e.agent for e in waiting]
        )
        done, withdrawn, owed = False, False, []
        for agent_id in dict.fromkeys(targets):
            theirs = [
                u.obligation
                for u in (said if names else over or mine)
                if u.agent == agent_id
            ]
            if not names:
                theirs += [
                    e.obligation
                    for e in waiting
                    if e.agent == agent_id and e.expects == "answer"
                ]
            if v := self._floor_voice(agent_id):
                self._unwanted(v, identity)
            takes_back = withdraws(text) and bool(names or any(theirs))
            if names or any(u.agent == agent_id for u in self._ledger):
                self._hush(agent_id, owed=owed if takes_back else None)
            elif not takes_back:
                continue
            done, withdrawn = True, withdrawn or takes_back
        if withdrawn:
            self._routes(self._withdrawal(identity, text, mark, owed))
        return done

    def _say_failed(self, v: _Voice, run_id: str, request: str) -> None:
        """A mouth's request failed. That is its outcome, like an answer: it waits its turn and is not dropped
        as stale."""
        self._utter(
            v.agent_id, "answer", self._line(COULD_NOT), obligation=run_id, request=request, audience=v.speaker,
            label=v.label,
        )  # fmt: skip

    def _line(self, lines: tuple[str, ...]) -> str:
        """The call's next fixed line of `lines`, in turn, so no agent says one twice before all were said."""
        at = self._line_at.get(lines, self._line_seed)
        self._line_at[lines] = at + 1
        return lines[at % len(lines)]

    def _say_ack(
        self, v: _Voice, rows: list[Request] | None, driven: bool = False
    ) -> None:
        """A mouth's acknowledgement, a fixed line owed to its voice's speaker. `driven`: its voice drives a
        request of theirs. Else its own row of `rows`: one it works on gets an "on it" line, one still in
        line a "next" line. Rows of other agents get no word from it: their tiles show them."""
        live = [r for r in rows or [] if r.status in ("queued", "working")]
        for r in live:
            r.acked = True
        mine = [r for r in live if r.agent == v.agent_id]
        on = [r for r in mine if r.status == "working"]
        if driven or on:
            about = v.driven_run if driven else on[0].run_id or on[0].id
            self._utter(
                v.agent_id,
                "ack",
                self._line(ACKS),
                obligation=about,
                audience=v.speaker,
                label=v.label,
            )
        elif mine:
            self._utter(
                v.agent_id,
                "ack",
                self._line(QUEUED_LINES),
                obligation=mine[0].id,
                audience=v.speaker,
                label=v.label,
            )

    def _ack_rows(self, rows: list[Request], identity: str) -> None:
        """Requests that start with no voice opened for them are acknowledged all the same, each by its own
        agent: the scheduler finds it a voice."""
        for r in {
            r.agent: r for r in reversed(rows) if r.status in ("queued", "working")
        }.values():
            r.acked = True
            lines = ACKS if r.status == "working" else QUEUED_LINES
            self._utter(
                r.agent,
                "ack",
                self._line(lines),
                obligation=r.run_id or r.id,
                audience=identity,
                label=r.label,
            )

    def _owed(self, o: dict, agent_id: str, text: str) -> tuple[str, str] | None:
        """What a mouth says to a line that asked nothing new: how its open work stands when /route says it was
        called on for it (`ops.say`), a reply when it was small talk. None: nothing."""
        say = o.get("say") or {}
        if (
            say.get("agentId") == agent_id
            and say.get("kind") == "status"
            and say.get("task")
        ):
            return "status", say["task"]
        return ("social", text) if o.get("social") and not o.get("more") else None

    def _say_line(
        self, v: _Voice, line: tuple[str, str], written: asyncio.Task | None = None
    ) -> None:
        """What a mouth owes a line that asked nothing new: ("social", the line) a written reply, ("status",
        the task) how its work stands, ("here", "") it was called on with nothing open."""
        kind, text = line
        if kind == "social":
            self._reply(v, text, written)
        elif kind == "here":
            self._utter(
                v.agent_id,
                "social",
                self._line(HERE),
                audience=v.speaker,
                label=v.label,
            )
        else:
            about = self._working_on(v.agent_id)
            self._status_said.add(
                about or ""
            )  # said because they asked: not said again unasked (`_overdue`)
            self._utter(
                v.agent_id,
                "status",
                self._line(STILL_WORKING),
                obligation=about,
                audience=v.speaker,
                label=v.label,
            )

    def _drive_request(self, v: _Voice, text: str, row: Request | None = None) -> None:
        """Start `text` as the floor-holder's driven run. `row`: the request row it answers, started and bound
        before /turns is posted (understanding §4.6)."""
        run_id = v.driven_run = str(uuid.uuid4())
        if row is not None:
            self._requests.start(row)
            self._requests.bind(row, run_id)
        self._working_for(v, row)
        self._spawn(self._drive(v, run_id, self._owner_name(v, row), text, row))

    async def _route_and_summon(
        self, identity: str, text: str, mark: int, **kw
    ) -> tuple | None:
        """Route the line (`_route`, whose result it returns). A multi-name line's strip rows that did not become
        requests are dropped however the routing ends (hold, nobody, error, superseded)."""
        try:
            return await self._route(identity, text, mark, **kw)
        finally:
            if self._requests.drop_pending(mark):
                self._sync_state()

    def _understand_floor(
        self, identity: str, text: str, mark: int, **kw
    ) -> asyncio.Future:
        """Understand a floor-speaker line now (understanding §4.6). The future, in flight from this call on,
        is the speech's understanding (`_route`'s result)."""
        fut, named = self._in_flight(identity), kw.pop("named", False)

        async def run() -> None:
            rec = None
            try:
                rec = await self._route_and_summon(identity, text, mark, **kw)
            finally:
                self._record(identity, fut, rec)

        self._routes(run())
        if kw.get("floor_line") and (v := self._voice) is not None:
            # the voice heard none of it (its follow-up, or a held one applied): it says what came of it.
            # `named`: the line names the voice's agent, so a small-talk reply is written beside /route.
            writing = self._prewrite(v, identity, text) if named else None
            self._routes(self._answer_heard(v, identity, text, fut, writing))
        return fut

    def _in_flight(self, identity: str) -> asyncio.Future:
        """A speech of `identity` is being understood (§4.6): `_record` resolves it."""
        fut = asyncio.get_running_loop().create_future()
        self._understanding.setdefault(identity, []).append(fut)
        return fut

    def _record(self, identity: str, fut: asyncio.Future, rec: tuple | None) -> None:
        """A speech's understanding is known: `fut` resolves to it (rec (rows, ops); None: a hold, or nothing was
        understood) (§4.6)."""
        if fut.done():
            return
        fut.set_result(rec)
        flying = self._understanding.get(identity, [])
        if fut in flying:
            flying.remove(fut)

    def _named_line(
        self,
        identity: str,
        agent_id: str,
        text: str,
        mark: int,
        r: Any,
        fut: asyncio.Future,
        voice: bool = True,
    ) -> tuple[Request | None, list[Request] | None, tuple[str, str] | None] | None:
        """A named line's /route answer `r`, under the floor lock (understanding §4.6). None: no `ops`, today's
        whole-line run. Else (the row its voice drives, already started; the rows to acknowledge; what its voice
        says to it, `_owed`). `voice` False: no voice will drive a row; every row starts voicelessly."""
        o = r.get("ops") if isinstance(r, dict) else None
        if o is None:
            self._record(identity, fut, None)
            return None
        if self._regrown(identity, mark):
            self._record(identity, fut, None)
            return None, None, None
        if r.get("hold"):
            # Unfinished, though it names the agent (10-09 prod Meet: "Hey, Friday. That's awesome, but can you
            # actually give me a summary of" became a request, and its rest two more). Nothing is asked or said
            # yet: the rest is joined to it (`_heard_final`), or it is applied as it is after a silence.
            self._hold_line(
                identity, text, self._clock(), mark, r, {"humans": len(self._people)}
            )
            self._record(identity, fut, None)
            return None, None, None
        o = self._amend_turn(mark, o)
        rows = self._apply(identity, mark, o)
        mine = [x for x in rows if x.agent == agent_id]
        first = mine[0] if voice and mine and mine[0] in self._startable() else None
        if first is not None:
            self._requests.start(
                first
            )  # its voice drives it; _start_queued must not post it too
        self._start_queued()
        self._record(identity, fut, (rows, o))
        ack = rows if len(rows) >= 2 or (rows and first is None) else None
        # asked nothing: what its agent owes the line (rows dropped by a stop say nothing)
        return first, ack, None if o.get("requests") else self._owed(o, agent_id, text)

    async def _named_summon(
        self, identity: str, text: str, mark: int, summon, understood: tuple
    ) -> None:
        """A named line's summon (understanding §4.6). A re-point applies what the line asked once /route answers,
        the floor lock not held while it waits. The speech's future resolves however this ends."""
        writing = None
        try:
            if (v := await summon) is None:
                return
            writing = self._prewrite(v, identity, text)
            r = (await asyncio.gather(understood[0], return_exceptions=True))[0]
            async with self._lock:
                # ...and still with this line's speaker: in a mixed meeting someone else may have named the agent since
                here = (
                    self._voice is v
                    and v.speaker == identity
                    and v.live is not None
                    and v.label == self._label(identity, mark)
                )
                took = self._named_line(
                    identity, v.agent_id, text, mark, r, understood[1], voice=here
                )
                if not here:
                    return  # the floor moved meanwhile: the rows still run, without the voice
                if (
                    took is None
                ):  # nothing understood (no ops): the whole line is the request, as in `_opened`
                    if not (
                        self._requests.stopped(mark)
                        or self._requests.stopped(mark, v.agent_id)
                    ):
                        self._drive_request(v, text)
                        self._say_ack(v, None, driven=True)
                    return
                first, ack, owed = took
                if first is not None:
                    self._drive_request(v, first.task or text, row=first)
                self._say_ack(v, ack, driven=first is not None)
                if owed:
                    self._say_line(v, owed, writing if owed[0] == "social" else None)
                    writing = None if owed[0] == "social" else writing
        finally:
            if writing is not None:
                writing.cancel()  # the line was not small talk: its reply is not wanted
            self._record(
                identity, understood[1], None
            )  # nothing understood: resolved all the same

    def _prewrite(self, v: _Voice, identity: str, text: str) -> asyncio.Task | None:
        """A small-talk reply to a line that names the voice's agent, written while /route decides what the
        line was (the writer takes 0.5-1.5 s, /route as long). Whoever asked for it uses it if /route says
        small talk, and cancels it if not: most named lines are requests, and their text is thrown away."""
        if v.path == "announce" or not v.persona or not real_speech(text):
            return None
        return self._spawn(self._writer(v.persona, self._said_to(v, text)))

    def _startable(self) -> list[Request]:
        """The rows that may start now, less the floor agent's while its voice's driven run has no row and still
        runs: one run per agent at a time, and that driven run is never overwritten (understanding §4.5)."""
        v = self._voice
        busy = (
            v.agent_id
            if v
            and not self._requests.by_run(v.driven_run)
            and any(x.run_id == v.driven_run for x in self._runs.running(v.agent_id))
            else None
        )
        return [r for r in self._requests.startable() if r.agent != busy]

    @staticmethod
    def _outcome(r: dict) -> str:
        return (
            "hold"
            if r.get("hold")
            else "agent"
            if r.get("agentIds") or r.get("agentId")
            else "none"
        )

    async def _understand(
        self, identity: str, *args, until: asyncio.Future | None = None, **kw
    ) -> dict | None:
        """`_route_call`, answered in the order the speaker's lines were said, whichever call finished first:
        each caller applies its answer before it next waits, so one speaker's requests start in said order
        ("draft the email", then "send it"). `until`: the caller applies it later (a named line, once its voice
        has opened): the lines after it wait for that future. No line waits longer than SAID_ORDER_WAIT_S."""
        mine = asyncio.get_running_loop().create_future()
        lines = self._routing.setdefault(identity, [])
        ahead = list(lines)
        lines.append(mine)

        def applied(_=None) -> None:
            if not mine.done():
                mine.set_result(None)
            if mine in lines:
                lines.remove(mine)
            self._pump()  # the line is understood: speech that waited for what it might change may start

        try:
            r = await self._route_call(identity, *args, **kw)
            if ahead:
                _, stuck = await asyncio.wait(ahead, timeout=SAID_ORDER_WAIT_S)
                for f in stuck:  # never applied: the lines after it stop waiting for it
                    if f in lines:
                        lines.remove(f)
            return r
        finally:
            if until is None or until.done():
                applied()
            else:
                until.add_done_callback(applied)

    async def _route_call(
        self,
        identity: str,
        text: str,
        humans: int | None,
        unclaimed: bool = False,
        quiet: bool = False,
        path: str = "jev",
        mark: int | None = None,
    ) -> dict | None:
        """The line's /route call alone (understanding §4.4): who answers and, from a current backend, `ops`.
        None when it failed; a line spoken to the agents says so, unless `quiet` (its caller has a fallback).
        `mark`: the line's, so a meeting line is sent with its own speaker and not the latest one's."""
        heard_at, outcome, v = self._clock(), "error", self._voice
        # Who the speaker is talking with, as evidence. /route decides on it whether a line with no name is
        # for an agent. With no voice open, the floor it names is their latest exchange.
        talking = self._evidence(self._who(identity, mark))
        try:
            r = await self._backend.route(
                self._said_at.get(mark) or self._said_by.get(identity, identity),
                (text if humans is not None else strip_wake(text))[:MAX_UTTERANCE],
                self._transcript.text()[-MAX_TRANSCRIPT:],
                humans,
                # `mine`: the speaker is already talking with the floor agent; their asks need no name (§4.4)
                floor={
                    "agent": v.agent_id,
                    "speaker": self._with(v),
                    "mine": self._mine(v, identity, mark),
                }
                if v
                else {
                    "agent": talking[0]["agentId"],
                    "speaker": self._label(identity, mark),
                    "mine": talking[0]["state"] == "active",
                }
                if talking
                else None,
                voice_runs=(
                    [
                        {"agent": x.agent_id, "request": x.request[:200]}
                        for x in self._runs.running()
                    ]
                    + [
                        {"agent": r.agent, "request": r.task[:200]}
                        for r in self._requests.queued()
                        if r.routed
                    ]
                )[-16:],
                at=self._transcript.times(ROUTE_LINES),
                setting=self.setting,
                unclaimed=unclaimed,
                open=self._open_rows(),
                engagements=talking,
                turn=self._turn_of.get(mark),
            )
            outcome = self._outcome(r)
            self._log(
                "route",
                turn=self._turn_of.get(mark),
                mark=mark,
                outcome=outcome,
                hold=bool(r.get("hold")),
            )
            return r
        except BackendError as e:
            # spoken to the agents: say it failed (an unclaimed question asked nobody)
            if not quiet and not unclaimed and (humans is None or humans <= 1):
                self._room.publish(
                    ERROR_TOPIC, {"code": e.code, "reason": e.reason}, [identity]
                )
            return None
        finally:
            ms = round((self._clock() - heard_at) * 1000)
            emit_metric(
                "live.latency.jev",
                ms,
                "Milliseconds",
                {"path": path, "outcome": outcome},
            )

    def _apply(
        self,
        identity: str,
        mark: int,
        ops: dict,
        later: bool = False,
        heard: dict | None = None,
    ) -> list[Request]:
        """`ops` onto the request list (understanding §4.4, §4.5): earlier rows cancelled and steered first, then
        one row per request. A steer of a row nobody has, while a voice is open, is a new ask of the floor agent.
        `later`: someone else spoke since; a steer that would become a new ask is dropped like an unnamed request."""
        v, extra = self._voice, []
        for rid in ops.get("cancel") or []:
            if (r := self._requests.get(str(rid))) is not None and r.status in OPEN:
                self._cancel(r)
        for s in ops.get("steer") or []:
            text, r = (
                str(s.get("text") or "").strip(),
                self._requests.get(str(s.get("requestId") or "")),
            )
            if not text or (r is not None and r.status == "cancelled"):
                continue
            if r is not None:
                # One line steers a request once. 10-08 Meet: a nameless question went to /route at once
                # (its speaker was talking with the agent) and again as an unclaimed question 3.5 s later,
                # and the same words went into the run twice.
                if (mark, r.id) in self._steered:
                    continue
                self._steered.add((mark, r.id))
            # a working row whose run is not bound yet will run its task as it is then (review B5-7)
            if r is not None and (
                r.status == "queued" or r.status == "working" and r.run_id is None
            ):
                self._requests.amend(r, text)
                emit_metric("live.request.steered", 1, "Count", {"how": "queued"})
                if not s.get(
                    "quiet"
                ):  # (a turn's own row, amended by more of the turn: acknowledged once)
                    self._steer_ack(r, identity, mark)
            elif r is not None and r.status == "working":
                if not s.get("quiet"):
                    self._steer_ack(r, identity, mark)
                self._spawn(self._steer_row(r, identity, mark, text))
            elif (r is not None or v is not None) and not (
                later and (r is None or r.by != "name")
            ):
                # answered already, or unknown: a new ask of its owner (superseded like any unnamed ask, §4.2)
                extra.append(
                    {
                        "agentId": r.agent if r else v.agent_id,
                        "task": text,
                        "by": r.by if r else "floor",
                    }
                )
                emit_metric("live.request.steered", 1, "Count", {"how": "new"})
        hushed = []
        # Before this line's own requests are rows: a stop silences what was open when it was said.
        for x in ops.get("hush") or []:
            hushed.append(str(x.get("agentId") or ""))
            self._hush(
                hushed[-1], x.get("requestIds") or ()
            )  # nothing was withdrawn: its work goes on
        rows = self._requests.settle(
            mark,
            identity,
            list(ops.get("requests") or []) + extra,
            self._clock(),
            self._label(identity, mark),
        )
        for r in rows:
            emit_metric("live.request.created", 1, "Count", {"by": r.by or "none"})
        heard = (
            ops if heard is None else heard
        )  # the line as /route understood it, before supersede (§5.3)
        who = self._who(identity, mark)
        for r in rows:
            if r.agent not in hushed:
                self._unhush(r.agent)  # asked something new: they want to hear it again
            self._engage(who, r.agent, obligation=r.id, expects="answer")
        if called := heard.get("called"):
            self._invite(
                who, str(called)
            )  # turned to and asked nothing new: what it has is said next
        if heard.get("people") and not any(
            heard.get(k) for k in ("requests", "social", "cancel", "steer")
        ):
            # /route understood the line as said to another person ("Monthly, with an annual option.
            # Maya, what is your current tool costing you?"). The understanding decides this, not a
            # pattern on the words (owner 10-07).
            self._suspend(who, "people")
            self._between(who)
        kind = next(
            (
                k
                for k in ("requests", "steer", "cancel", "social", "more")
                if heard.get(k)
            ),
            "none",
        )
        emit_metric(
            "live.understand.ops", 1, "Count", {"kind": kind.rstrip("s")}
        )  # §5.3: once per understood line
        if (
            (u := self._unclaimed) is not None
            and u[3] == mark
            and (kind != "none" or heard.get("called"))
        ):
            self._unclaimed = None  # an agent took the question: it is not put to the room a second time
        self._sync_state()
        return rows

    def _note(self, text: str) -> None:
        """A line for the call's log only, never read to a model: how a delivery went, so a call reads back
        (the worker's own metrics are not in our logs). A meeting's transcript becomes a file people read: none."""
        if self.setting != "meeting":
            self._transcript.add_note(text, at=self._clock())

    def _row_changed(self, r: Request, was: str | None) -> None:
        """understanding §5.3: a row's end is counted once (RequestList moves only open rows); every change of a
        routed row is a transcript line, so a call reads back with its requests. A meeting's transcript becomes
        its room's transcript.txt: no lines there."""
        if (
            r.status in ENDED and r.routed
        ):  # an unrouted strip row was never created (live.request.created)
            emit_metric("live.request.ended", 1, "Count", {"status": r.status})
        if r.routed and self.setting != "meeting":
            line = f"[request {r.id} {self._agent_name(r.agent)} {r.status}] {r.task[:LINE_CHARS]}"
            self._transcript.add_note(line, at=self._clock())

    def _cancel(self, r: Request) -> None:
        """`ops.cancel` (understanding §4.4): its run aborted the way _stop aborts one; its answer never said or posted."""
        if r.status == "working" and r.run_id:
            self._runs.abort(r.run_id)  # a run_ended that comes later says nothing
            self._stopped.add(r.run_id)
            self._spawn(self._abort(r.run_id))
        self._requests.drop(r)
        emit_metric("live.request.cancelled", 1, "Count")
        # called off: its acknowledgement and its answer are never said, also one being said
        self._strike(
            u
            for u in self._ledger
            if u.agent == r.agent and u.obligation in (r.id, r.run_id)
        )
        for e in self._engagements():
            if e.agent == r.agent and e.obligation == r.id:
                e.expects = "nothing"
        self._start_queued()  # the agent's next row
        self._sync_state()
        self._pump()

    def _steer_ack(self, r: Request, identity: str, mark: int) -> None:
        """A mouth's word for a follow-up that was added to a request still being worked on. Until 10-08 the
        person heard nothing: the words went into the run, and a mouth says nothing for a steer. They spoke
        to the agent about its work, so they are talking with it again and waiting for that answer."""
        self._unhush(r.agent)
        self._engage(
            self._who(identity, mark), r.agent, obligation=r.id, expects="answer"
        )
        self._utter(
            r.agent, "ack", self._line(STEER_ACKS), obligation=r.run_id or r.id, audience=identity,
            label=self._label(identity, mark),
        )  # fmt: skip

    async def _steer_row(self, r: Request, speaker: str, mark: int, text: str) -> None:
        """`ops.steer` of a working row (understanding §4.4): into its run, steer-only. A run that just ended (or a
        steer that failed) makes a new row for the same agent instead: the words are never lost."""
        steered = False
        if self._window is not None and r.run_id:
            try:
                resp = await self._backend.turn(
                    self._window.call_id,
                    speaker,
                    r.agent,
                    str(uuid.uuid4()),
                    text[:MAX_UTTERANCE],
                    "",
                    steer_run_id=r.run_id,
                    steer_only=True,
                    speaker_name=self._said_at.get(mark) or self._said_by.get(speaker),
                )
                steered = bool((resp or {}).get("steered"))
            except BackendError as e:
                logger.info("huddle request steer failed: %s", e.code)
        if r.status == "cancelled":
            return  # cancelled while the steer was in flight (review B5-2): nothing more of it
        emit_metric(
            "live.request.steered", 1, "Count", {"how": "run" if steered else "new"}
        )
        if steered:
            return
        tid = self._turn_of.get(mark)
        twin = (
            next(
                (
                    x
                    for x in self._turn_rows(tid)
                    if x is not r and x.agent == r.agent and x.status == "queued"
                ),
                None,
            )
            if tid is not None
            else None
        )
        if (
            twin is not None
        ):  # another piece's steer of this row already made its row: that one, amended
            self._requests.amend(twin, text)
            self._sync_state()
            return
        new = [{"agentId": r.agent, "task": text, "by": r.by}]
        for x in self._requests.settle(
            mark, speaker, new, self._clock(), self._label(speaker, mark)
        ):
            emit_metric("live.request.created", 1, "Count", {"by": x.by or "none"})
        self._start_queued()
        self._sync_state()

    async def _act(
        self,
        identity: str,
        text: str,
        mark: int,
        ops: dict,
        heard_at: float,
        switch: bool,
        stands: bool = False,
        floor_line: bool = False,
    ) -> list[Request]:
        """A line /route understood (understanding §4.6): each request its own row and run, small talk no run.
        `stands`: a held line applied after its quiet (§4.4), never superseded. Returns the rows it made."""
        reqs, social = list(ops.get("requests") or []), ops.get("social")
        v, superseded = self._voice, False
        # the floor speaker's line, taken as its follow-up
        own = (
            v is not None
            and identity == v.speaker
            # ...and the voice is still with that speaker: someone else may have named the agent while /route decided
            and floor_line
            and v.label == self._label(identity, mark)
        )
        # Routing v2 §4.2: someone else spoke since; their line routes itself, seeing this one. A named ask stands.
        # The floor speaker's line stands too. Cancel and steer name earlier rows: they stand either way.
        later = (
            not stands
            and not own
            and any(m > mark for who, m in self._spoke.items() if who != identity)
        )
        if later and (reqs or social):
            named = [x for x in reqs if x.get("by") == "name"]
            if social or len(named) < len(reqs):
                emit_metric("live.route.superseded", 1, "Count")
            reqs, social, superseded = named, None, not named
        rows = self._apply(
            identity, mark, {**ops, "requests": reqs}, later=later, heard=ops
        )
        if not rows:
            if social and v is None:
                await self.summon(
                    identity,
                    social,
                    request=None,
                    mark=mark,
                    path="jev",
                    heard_at=heard_at,
                    owed=self._owed(ops, social, text),
                )
            elif not (social or superseded or ops.get("cancel") or ops.get("steer")):
                self._unrouted[identity] = self._clock()
            return rows
        startable = self._startable()
        if own:
            mine = [r for r in rows if r.agent == v.agent_id]
            if mine and mine[0] in startable and v.live is not None:
                # §4.6: the floor speaker's ask is its voice's driven run
                self._drive_request(v, mine[0].task or text, row=mine[0])
            if mine or not switch:
                v.ack_rows += (
                    rows  # §4.6: acknowledged once its speaker is quiet (tick)
                )
                self._start_queued()
                return rows
        elif v is not None:
            self._start_queued()  # §4.6: they start now; answers reach the room as ready deliveries
            self._ack_rows(rows, identity)
            return rows
        if v is not None:  # a switch: the floor speaker addressed only other agents
            emit_metric("live.route.switch", 1, "Count")
        first = rows[0]
        drive = (
            first in startable
        )  # an agent works on one row at a time (§4.5): else it waits in line
        path = "switch" if v is not None else "name" if first.by == "name" else "jev"
        if drive:
            self._requests.start(
                first
            )  # its voice drives it (below); _start_queued must not post it too
        self._start_queued()
        await self.summon(
            identity,
            first.agent,
            request=(first.task or text) if drive else None,
            mark=mark,
            path=path,
            heard_at=heard_at,
            ack=rows if len(rows) >= 2 or not drive else None,
            row=first if drive else None,
        )
        if (
            first.status == "working" and first.run_id is None
        ):  # its voice never opened: it runs without one
            self._requests.unstart(first)
            self._start_queued()
        return rows

    async def _route(
        self,
        identity: str,
        text: str,
        mark: int,
        humans: int | None = None,
        switch: bool = False,
        unclaimed: bool = False,
        heard: tuple | None = None,
        routed: dict | None = None,
        floor_line: bool = False,
        stands: bool = False,
    ) -> tuple[list[Request], dict | None] | None:
        """`humans`: people in the call, for a line with no name (None: the Lightbulb wake word, stripped
        so "Lightbulb, Forge, check CI" reaches /route opening with Forge). `routed`: this line's /route answer,
        already fetched by its multi-name round trip: never asked twice. `floor_line`: the floor speaker's own
        line (understanding §4.6): its /route error is not published, and with no `ops` nothing else happens here
        (its caller falls back). Returns (rows, ops) once `ops` were acted on,
        ([], None) for a floor line /route failed or answered without `ops`, else None (a hold, today's routing)."""
        heard_at = self._clock()
        r = (
            routed
            if routed is not None
            else await self._understand(
                identity,
                text,
                humans,
                unclaimed,
                quiet=heard is not None or floor_line,
                mark=mark,
            )
        )
        if not stands and self._regrown(identity, mark):
            return None
        outcome = "error" if r is None else self._outcome(r)
        r = r or {}
        if r.get("ops") is not None and not r.get("hold"):
            # understanding §4.4: the backend listed the line's requests; they replace today's whole-line routing
            if heard is not None and heard[1]:
                self._unwanted(heard[1], identity)
            o = self._amend_turn(mark, r["ops"])
            return await self._act(
                identity, text, mark, o, heard_at, switch, stands, floor_line
            ), o
        if floor_line and not r.get("hold"):
            return [], None
        if heard is not None and not r.get(
            "hold"
        ):  # a held line waits for the rest, as below
            # `heard`: (address, voice) of a line that named 2+ agents, as on_final_transcript saw it
            a, hv = heard
            reqs = r.get("requests") or []
            if (
                len(reqs) < 2
            ):  # one ask (or no router): today's handling of the line, whatever /route said
                if self._requests.drop_pending(mark):
                    self._sync_state()
                named = self._named(text, a)
                if self._requests.stopped(mark) or all(
                    self._requests.stopped(mark, x) for x in named
                ):
                    return  # "everyone stop" / "<agent>, stop" said while /route decided
                self._dispatch(
                    identity,
                    text,
                    mark,
                    a,
                    self._voice,
                    heard_at,
                    routed=None
                    if outcome == "error"
                    else r,  # a failed /route is asked again
                )
                return
            # Never superseded: named asks stand whoever spoke meanwhile. A voice that opened after this line
            # was heard keeps the floor; the requests still start, without the acknowledgement.
            if hv:
                self._unwanted(hv, identity)
            await self._start_requests(
                identity,
                mark,
                reqs,
                heard_at,
                ack=self._voice is None or self._voice is hv,
            )
            return
        if r.get(
            "hold"
        ):  # unfinished ("Friday, we're trying to"): the speaker's next final carries the rest
            kw = {
                "humans": humans,
                "switch": switch,
                "unclaimed": unclaimed,
                "heard": heard,
                "floor_line": floor_line,
            }
            self._hold_line(identity, text, heard_at, mark, r, kw)
            return
        agent_ids = r.get("agentIds") or ([r["agentId"]] if r.get("agentId") else [])
        if agent_ids and self._requests.stopped(mark, agent_ids[0]):
            return  # that agent was stopped while this line's /route decided
        if not agent_ids:
            if outcome == "none":
                self._unrouted[identity] = self._clock()
            return
        # Routing v2 §4.2: someone else spoke since. Their line routes itself, seeing this one; Jev decides on that.
        # A held line applied after its quiet stands (§4.4).
        if not stands and any(
            m > mark for who, m in self._spoke.items() if who != identity
        ):
            emit_metric("live.route.superseded", 1, "Count")
            return
        if (
            switch
            and self._voice is not None
            and set(agent_ids) == {self._voice.agent_id}
        ):
            return  # /route says the floor-holder was addressed: it already heard its own speaker say it
        if switch and self._voice is not None and agent_ids[0] != self._voice.agent_id:
            # The floor-holder's own speaker named another agent mid-sentence and /route says it was
            # addressed: they get the floor now, the way a name at the start of the line would.
            emit_metric("live.route.switch", 1, "Count")
            await self.summon(
                identity,
                agent_ids[0],
                request=text,
                mark=mark,
                path="switch",
                then=agent_ids[1:],
                heard_at=heard_at,
            )
            return
        if humans is not None and self._voice is not None:
            # A voice is open (it was, or one opened while Jev decided): the picked agents answer this person
            # when the floor settles, in order (§6, decision 2). The floor-holder itself is a re-point then.
            self._queue += [
                (agent_id, identity, text, self._label(identity, mark))
                for agent_id in agent_ids
            ]
            emit_metric("live.route.queued", len(agent_ids), "Count")
            self._sync_state()
            return
        await self.summon(
            identity,
            agent_ids[0],
            request=text,
            mark=mark,
            path="jev",
            then=agent_ids[1:],
            heard_at=heard_at,
        )

    def on_agent_said(
        self, agent_id: str, text: str, interrupted: bool = False, order: int | None = None, index: int | None = None,
        heard: str | None = None,
    ) -> None:  # fmt: skip
        """What a voice said: every later seed and since_agent hears it, tagged with the person it was talking
        to ("Forge → Leo: ...", routing v2 §5): one unit it was ordered to say, by its exact text, said to its end
        or not (`on_unit`). `order`, `index`: the playback and unit its receipt is for. `heard`: the unit was cut,
        and this much of it was audible (`_audible`): that goes on the record marked as a piece (CUT_MARK). The
        unit was not said."""
        v = self._floor_voice(agent_id)
        own = (
            self._voice if self._voice and self._voice.agent_id == agent_id else None
        )  # also while it closes
        ready = 0.0  # an answer's first sentence: when that answer was ready (its utterance was owed)
        if own and not interrupted:
            u = self._unit_said(agent_id, text, order, index)
            ready = (
                u.at if u is not None and u.kind == "answer" and u.cursor == 1 else 0.0
            )
        if heard is not None:
            if not heard:
                return  # cut before a word of it was audible: nothing was said
            text = heard + CUT_MARK
        elif interrupted:
            return  # a unit that never played (revoked, silent): nothing was said
        to = self._with(v) if v else ""
        now = self._clock()
        if ready:
            emit_metric(
                "live.answer.wait", now - ready, "Seconds", {"setting": self.setting}
            )
        self._transcript.add_agent(
            agent_id, self._agent_name(agent_id), text, at=now, to=to, ready=ready
        )

    def _unit_said(
        self,
        agent_id: str,
        text: str,
        order: int | None = None,
        index: int | None = None,
    ) -> Utterance | None:
        """A mouth said one unit to its end: the cursor of the utterance waiting for it moves. At the end it is
        delivered, and only then (10-07 rig: an answer cut after two words was taken for said). The person
        it was for has now heard the agent: they are talking with it, and a question it ended on is theirs
        to answer. A receipt that names its order and unit moves only that utterance at that unit."""
        u = next(
            (
                u
                for u in self._ledger
                if u.agent == agent_id
                and u.state
                in (
                    "speaking",
                    "paused",
                )  # (a unit that ended as the yield came is said all the same)
                and u.cursor < len(u.units)
                and u.units[u.cursor] == text
                and (order is None or (u.order, u.cursor) == (order, index))
            ),
            None,
        )
        if u is None:
            return None  # of an utterance since called off
        u.cursor, u.tries = u.cursor + 1, 0
        # Verified playback is the exchange going on: it is refreshed here, unit by unit, read straight from
        # the record. (10-08 rig: looked up after a long answer's last unit, the exchange had just been
        # ended for its age, and the follow-up waited out the unclaimed-question delay.)
        if (e := self._engaged.get(((u.audience, u.label), agent_id))) is not None:
            e.at = self._clock()
        if u.cursor < len(u.units):
            return u
        self._ledger.remove(u)
        u.state = "delivered"
        if u.kind == "answer" and u.obligation:
            # what this run's answer said, for a closing chunk that lands after it (`_carry_on`)
            # ponytail: one entry per answered run, for the call's life
            self._answered[u.obligation] = (
                self._answered.get(u.obligation, []) + u.units
            )
        if u.kind == "answer":
            self._requests.delivered({u.obligation})
            self._delivery_tries.pop(u.obligation or "", None)
        if (
            e := self._engagement((u.audience, u.label), agent_id)
            if u.audience
            else None
        ):
            e.last, e.last_by, e.at = "delivered", "", self._clock()
            if u.kind in ("answer", "social"):
                e.state, e.why = "active", ""
            if text.rstrip().endswith("?"):
                e.expects = "reply"
            elif u.kind == "answer":
                e.expects = "answer" if self._owes(e) else "nothing"
        self._sync_state()
        self._pump()  # what waited behind it starts
        return u

    def _floor_voice(self, agent_id: str | None) -> _Voice | None:
        """The open voice of `agent_id`. One whose session died (`dropping`, its close on the way) says nothing:
        an answer that lands meanwhile is news, not "spoken" into a dead session."""
        v = self._voice
        return (
            v
            if v and v.live is not None and not v.dropping and v.agent_id == agent_id
            else None
        )

    def on_agent_state(self, agent_id: str, state: str) -> None:
        """The voice session's agent state. Speaking is activity: the idle clock restarts when it stops."""
        if v := self._floor_voice(agent_id):
            if v.agent_state == "speaking" and state != "speaking":
                v.followed_at = (
                    self._clock()
                )  # its last word: its follow-up window starts here
            v.agent_state = state
            v.last_activity = self._clock()
            if state == "speaking" and v.heard_at is not None:
                self._latency("first_audio", v)
                v.heard_at = None  # once per voice
            if state == "speaking" and self._speaking:
                emit_metric(
                    "live.floor.overlap", 1, "Count"
                )  # its audio began over a person
            self._sync_state()

    def on_active_speakers(self, identities: set[str]) -> None:
        """The SFU's speaking set: who makes sound. Who talks is decided in `_talking_changed`."""
        level = {i for i in identities if i in self._people}
        for who in level ^ self._level:
            self._log("speech", src="level", who=who, on=who in level)
        self._level = level
        self._talking_changed()

    def on_speech(self, identity: str, started: bool) -> None:
        """The transcriber's speech detector: `identity` began or stopped speaking. It fires before the first
        word is transcribed, so nobody is talked over while their sentence is still being heard. Each
        stretch it hears is one `Epoch` of that person's turn state, and gets one final."""
        now = self._clock()
        eps = self._turns.get(identity, [])
        open_ = next((e for e in eps if e.state == "speaking"), None)
        if started and identity in self._people:
            self._voiced.add(identity)
            if open_ is None:
                self._eid += 1
                self._turns.setdefault(identity, []).append(
                    Epoch(self._eid, identity, now)
                )
        else:
            self._voiced.discard(identity)
            if open_ is not None:
                open_.state, open_.ended = "awaiting-final", now
                self._listened(open_)
        self._log("speech", src="voiced", who=identity, on=started)
        self._talking_changed()

    def _talking_changed(self) -> None:
        """Who is talking: the transcriber's speech detector hears speech from them (`on_speech`) and the SFU
        hears sound, now or within LEVEL_FRESH_S. Sound alone (typing, a fan, an open laptop microphone) is
        nobody talking: it neither holds an answer back nor drops one (10-04 prod test: three announcements
        dropped by noise). The detector alone is nobody talking either, until the SFU has heard them.
        A mouth's speech starts and yields by one rule, whatever it is saying (`_pump`, `_yield_check`)."""
        now = self._clock()
        for who in self._level:
            self._level_at[who] = now
        humans = {
            w
            for w in self._voiced
            if w in self._level
            or now - self._level_at.get(w, -math.inf) < LEVEL_FRESH_S
        }
        if lapse := [
            LEVEL_FRESH_S - (now - self._level_at[w]) for w in humans - self._level
        ]:
            self._later(
                min(lapse), self._talking_changed
            )  # nothing else says when the SFU's sound has gone stale
        for who in humans:
            for ep in self._turns.get(who, []):
                if ep.state == "speaking" and not ep.credible:
                    ep.credible = True
                    self._turn_log("credible", ep)
        if humans or self._speaking:
            self._last_heard = now
        for who in humans | self._speaking:
            self._heard_at[who] = now
        if self._speaking and not humans:
            self._talk_until = now
        if humans != self._speaking:
            self._log(
                "speech",
                src="talking",
                speaking=sorted(humans),
                level=sorted(self._level),
                voiced=sorted(self._voiced),
            )
        started, self._speaking = humans - self._speaking, humans
        if started and self.setting == "meeting":
            self._unclaimed = None  # someone talks after the question: a person took it
        if started:
            for u in self._ledger:
                if u.state == "speaking":
                    u.over |= started  # who talked over it: a stop of theirs is for it, whether or not it yields
                    begun = u.sound_at is None or now - u.sound_at < PREROLL_S
                    if begun and not all(self._only_listening(w) for w in started):
                        self._yield(
                            u
                        )  # its sentence began as they did: they were speaking first, unheard
            # a mouth yields once their turn has lasted TALK_OVER_S, by the clock and not by the next tick
            self._later(
                min(self._turn_from(w) for w in started) + TALK_OVER_S - now,
                self._yield_check,
            )
        self._pump()  # talk began: the scheduler notes what waits. It ended: the room starts to settle.

    async def talk_start(self, identity: str, payload: str = "") -> None:
        """C6: `{agentId}`, or "" for the last agent this caller summoned, else the CEO. Its voice opens and says
        nothing until it is spoken to (owner ruling 10-12: no greeting), unless it has an answer ready: the
        person turned to it, so that is said next (`_invite`)."""
        if (
            identity not in self._people
        ):  # RPC caller_identity is server-set; still, only present humans
            return
        try:
            wanted = json.loads(payload).get("agentId") if payload else None
        except (ValueError, AttributeError):
            return
        if wanted:
            agent_id = (
                wanted if self._agent(wanted) else None
            )  # an unknown agentId is ignored
        else:
            agent_id = next(
                (
                    a
                    for a in (self._last_agent.get(identity), self._team["ceoId"])
                    if self._agent(a)
                ),
                None,
            )
        v = self._voice
        if agent_id is None or (
            v and v.agent_id == agent_id
        ):  # tapping the floor-holder is a no-op
            return
        await self.summon(identity, agent_id, request=None, path="tap")

    # -- the floor --

    async def summon(
        self,
        identity: str,
        agent_id: str,
        request: str | None,
        mark: int | None = None,
        path: str = "name",
        then: list[str] | tuple = (),
        wake_dims: dict[str, str] | None = None,
        heard_at: float | None = None,
        ack: list[Request] | None = None,
        row: Request | None = None,
        understood: tuple[asyncio.Future, asyncio.Future] | None = None,
        label: str | None = None,
        owed: tuple[str, str] | None = None,
    ) -> _Voice | None:
        """`then`: the request's other named agents, queued behind this one. Any summon but a queue
        advance (path "switch") replaces a pending queue: a new name wins. `ack`: the rows the voice
        acknowledges (the open ones when it speaks); `row`: the request row `request` is (its driven run binds
        to it); `owed`: what its voice says to a line that asked nothing (`_owed`). `understood`: a named line's
        /route call in flight and its speech's record (understanding §4.6). Returns the voice it re-pointed for
        `understood`. `label`: a meeting's speaker who gets the agent's attention (default: the summoning line's).
        A tap (path "tap") turns its person to the agent: what it has for them is said next (`_invite`)."""
        label = self._label(identity, mark) if label is None else label
        async with self._lock:
            self._unclaimed = None  # an agent takes the floor: the open question is not left unclaimed
            self._last_agent[identity] = agent_id
            if path != "announce" and (label or not mixed_meet(identity)):
                # they turned to it: the two are talking
                (self._invite if path == "tap" else self._engage)(
                    (identity, label), agent_id
                )
            if path not in ("switch", "announce"):
                self._queue = [(aid, identity, request or "", label) for aid in then]
            old = self._voice
            if (
                old is not None and old.agent_id == agent_id
            ):  # already holds the floor: re-point, no second session
                old.label, old.followed_at = (
                    label,
                    self._clock(),
                )  # a meeting: whoever named it has it now
                self._repoint(old, identity)
                if understood:  # understanding §4.6: what the line asked is applied once /route answers
                    self._sync_state()
                    return old
                driven = (
                    bool(request) and row is None
                )  # (a row left unbound runs without a voice: `_act`)
                if driven:  # the whole line is the request, as a new voice's would be (`_opened`)
                    self._drive_request(old, request)
                self._say_ack(old, ack, driven=driven)
                self._pump()  # what it has for them may start now
                self._sync_state()
                return
            if old is not None and old.live is not None:
                # One agent speaks at a time: it goes quiet now, not when its session has closed beside the
                # new one's opening. What it was saying stays in the ledger from its cursor.
                self._live_call(old, "interrupt")
                self._unsaid(u for u in self._ledger if u.via is old)
            now = self._clock()
            self._gen += 1
            v = self._voice = _Voice(
                agent_id,
                None,
                self._gen,
                identity,
                now,
                path=path,
                wake_dims=wake_dims,
                heard_at=now if heard_at is None else heard_at,
                label=label,
                followed_at=now,
            )
            self._sync_state()
            # D §9: a switch opens the new voice while the old one closes (same window, no new /summon).
            # Both sessions' seconds land in the window, so the overlap is counted twice (D §4).
            opened, closed = await asyncio.gather(
                self._open(
                    v,
                    identity,
                    request,
                    mark,
                    ack,
                    row=row,
                    understood=understood,
                    owed=owed,
                ),
                self._close_live(old),
                return_exceptions=True,
            )
            for e in (opened, closed):
                if isinstance(
                    e, BaseException
                ):  # an open that raised must still end its window
                    logger.error("huddle summon failed agent=%s", agent_id, exc_info=e)
            if opened is not True:
                await self._close_voice()
                await self._end_window()

    async def _open(
        self,
        v: _Voice,
        identity: str,
        request: str | None,
        mark: int | None,
        ack: list[Request] | None = None,
        row: Request | None = None,
        understood: tuple[asyncio.Future, asyncio.Future] | None = None,
        owed: tuple[str, str] | None = None,
    ) -> bool:
        """Caller holds the lock and has made `v` the floor. False when it could not open.

        Three things are waited for, side by side: the call's window (/summon), the agent's session
        (/session: its voice and persona), and for a named line its /route answer. 10-08 Meet: "Hi Friday."
        was answered 4.7 s after the router had read it (`live.latency.route` 5252 ms, /route itself 548 ms):
        all of it was the first two, and which of them cannot be read from the code. Each is one round trip
        to the backend. So how long each took is logged (`open`), and the backend logs its own steps."""
        agent_id, began, took = v.agent_id, self._clock(), {}

        async def timed(what: str, work):
            try:
                return await work
            finally:
                took[what + "Ms"] = round((self._clock() - began) * 1000)

        # understanding §4.6: a named line's /route answer is awaited beside the window and session, never before
        window, session = await asyncio.gather(
            timed("window", self._ensure_window(identity)), timed("session", self._backend.session(agent_id)),
            return_exceptions=True,
        )  # fmt: skip
        warm: list[asyncio.Task] = []
        if understood and not any(
            isinstance(r, BaseException) for r in (window, session)
        ):
            # The voice is prepared while /route decides (10-08 rig: it opened only after, 0.5 s of every
            # acknowledgement). Not before the window is open: a call that is refused opens no voice.
            # It may not speak before: it is not the room's voice until the line is applied (`v.live`
            # below), and the scheduler orders only that one. A mouth is handed each line it says and never
            # reads its seed, so the transcript so far is all it is given.
            seed = self._transcript.seed(
                agent_id, before_mark=math.inf if mark is None else mark
            )
            warm.append(
                self._spawn(
                    timed("voice", self._prepared(agent_id, session, identity, seed))
                )
            )
            v.persona = session["persona"]
            v.writing = self._prewrite(
                v, identity, request or ""
            )  # (taken by `_opened` if it was small talk)
        try:
            heard = (
                list(
                    await asyncio.gather(
                        timed("route", understood[0]), return_exceptions=True
                    )
                )
                if understood
                else []
            )
            return await self._opened(
                v,
                identity,
                request,
                mark,
                ack,
                row,
                understood,
                owed,
                window,
                session,
                heard,
                warm,
            )
        finally:
            for t in warm:  # prepared and not taken (the open failed or was refused on the way): closed unused
                t.add_done_callback(self._unused)
            if v.writing is not None:
                v.writing.cancel()  # the line was not small talk: its reply is not wanted
                v.writing = None
            # Where the time went, each counted from the moment the open began: `waitedMs` is what came
            # before it (the line's hearing to the floor lock).
            waited = (
                round((began - v.heard_at) * 1000) if v.heard_at is not None else None
            )
            self._log(
                "open",
                agent=agent_id,
                path=v.path,
                waitedMs=waited,
                **took,
                totalMs=round((self._clock() - began) * 1000),
            )

    async def _prepared(
        self, agent_id: str, session: dict, identity: str, seed: list
    ) -> Live:
        return await self._live_factory(
            agent_id=agent_id,
            persona=session["persona"],
            voice=session["voice"],
            speaker=identity,
            seed=seed,
        )

    def _unused(self, t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is None:
            self._spawn(t.result().aclose())

    async def _opened(
        self,
        v: _Voice,
        identity: str,
        request: str | None,
        mark: int | None,
        ack: list[Request] | None,
        row: Request | None,
        understood: tuple[asyncio.Future, asyncio.Future] | None,
        owed: tuple[str, str] | None,
        window: Any,
        session: Any,
        heard: list,
        warm: list[asyncio.Task],
    ) -> bool:
        """`_open` once the window, the session and (a named line) its /route answer are here."""
        agent_id = v.agent_id
        self._latency("route", v)
        for r in (window, session):
            if isinstance(r, BaseException):
                code, reason = (
                    (r.code, r.reason)
                    if isinstance(r, BackendError)
                    else ("huddles_upstream", None)
                )
                if code == "agent_not_in_room":
                    self._spawn(self._refresh_team())
                logger.info("huddle summon refused: %s", code)
                if v.path == "announce":  # nobody asked: the badge stays, silently
                    emit_metric("live.announce.refused", 1, "Count")
                else:
                    self._room.publish(
                        ERROR_TOPIC, {"code": code, "reason": reason}, [identity]
                    )
                if understood:  # understanding §4.6: what the line asked still runs, every row voicelessly
                    self._named_line(
                        identity,
                        agent_id,
                        request,
                        mark,
                        heard[0],
                        understood[1],
                        voice=False,
                    )
                return False
        if understood:
            text = request
            took = self._named_line(
                identity, agent_id, text, mark, heard[0], understood[1]
            )
            if took is not None:
                row, ack, owed = took
                request = (row.task or text) if row is not None else None
            elif self._requests.stopped(mark) or self._requests.stopped(mark, agent_id):
                request = None  # a stop covers the line (understanding §4.5): the voice opens, nothing runs
        if row is not None and row.status != "working":
            request = None  # stopped while the voice opened (understanding §4.5): the voice opens, nothing runs
        elif row is not None:
            request = (
                row.task or request
            )  # a steer may have added to it while the voice opened (review B5-7)
        if request:
            # D §5: we start the turn ourselves, in parallel with the voice open. A task, not awaited:
            # a cold workspace retries for up to a minute and must not hold the floor lock.
            run_id = v.driven_run = str(uuid.uuid4())
            # held now, not in the spawned _drive: a voice that fails before _drive runs still releases it
            self._runs.hold(run_id, agent_id)
            if (
                row is not None
            ):  # understanding §4.6: the row (started by the caller) is this run
                self._requests.bind(row, run_id)
            self._working_for(v, row)
            self._spawn(
                self._drive(v, run_id, self._owner_name(v, row, mark), request, row)
            )
        self._ack(
            ack, said=True
        )  # conversation engine §3.5: the rows still open after the awaits above
        v.persona, written = session["persona"], None
        if v.path != "announce" and owed and owed[0] == "social":
            # Small talk is written while the session opens, not after it (10-07 rig: 2 s of every "Hi"),
            # and for a line that names the agent, while /route decides (`_prewrite`).
            written, v.writing = (
                v.writing
                or self._spawn(self._writer(v.persona, self._said_to(v, owed[1]))),
                None,
            )
        seed = self._transcript.seed(
            agent_id, before_mark=math.inf if mark is None else mark
        )
        try:
            live = await (
                warm.pop()
                if warm
                else self._prepared(agent_id, session, identity, seed)
            )
        except Exception:  # start_live already retried with the fallback voice
            logger.exception("huddle live session failed to start agent=%s", agent_id)
            self._notice({"kind": "voice_failed", "agentId": agent_id})
            self._failed_at[(identity, agent_id)] = self._clock()
            self._runs.release(
                v.driven_run
            )  # no voice will say it: its answer is in the ledger
            return False
        self._latency("session_started", v)
        if (
            ack is not None
            and not self._ack(ack)
            and not owed
            and not (row is not None and row.status in OPEN)
        ):
            v.live, v.opened = (
                live,
                self._clock(),
            )  # its rows stopped while it joined: closed unsaid
            self._runs.release(v.driven_run)
            return False
        v.live, v.opened = live, self._clock()
        v.followed_at = v.opened
        # A mouth, not an ear: a voice that heard the Meet answered talk not meant for it (10-04 prod Meet,
        # 10-07 rig). What is said reaches it through the worker's transcript finals only.
        if v.path != "announce":
            # its acknowledgement and what it owes the line: said before any answer (`_rank`). A tap has
            # neither: it says nothing until it is spoken to (owner ruling 10-12).
            self._say_ack(v, ack, driven=bool(request))
            if owed:
                self._say_line(v, owed, written)
        self._pump()  # a voice is a way to say what the ledger holds: what waited for this one starts
        self._runs.release(
            v.driven_run
        )  # sentences that came while it opened are said now, none dropped
        self._sync_state()
        return True

    def gptlive_seconds(self) -> float:
        """huddle.gptlive.open_seconds (a name kept for the metric): summed seconds of closed voices and the open one."""
        v = self._voice
        try:
            return self._closed_seconds + (
                v.live.usage_seconds() if v and v.live is not None else 0.0
            )
        except Exception:
            return self._closed_seconds

    def _latency(self, what: str, v: _Voice) -> None:
        """live.latency.* (D latency gate): from the line that woke the voice (or the tap) to this point."""
        if v.heard_at is not None:
            ms = (self._clock() - v.heard_at) * 1000
            emit_metric(
                f"live.latency.{what}", round(ms), "Milliseconds", {"path": v.path}
            )

    async def _ensure_window(self, speaker: str) -> _Window:
        async with self._window_lock:
            if self._window is None:
                if self._closing:
                    raise RuntimeError("huddle closing: no new window")
                try:
                    r = await self._backend.summon(speaker, self._said_by.get(speaker))
                except BackendError as e:
                    # Published for the tiles: in a Meet nobody sees the refusal, the agents are just silent
                    # (10-04 prod Meet: the day's free voice minutes were used up). Another refusal is not the
                    # limit: an earlier reason must not stay up.
                    self._voice_limit = (
                        (e.reason or "limit") if e.code == "voice_limit" else ""
                    )
                    self._sync_state()
                    raise
                self._voice_limit = ""
                self._window = _Window(
                    r["callId"], float(r["deadline"]), float(r["budgetSeconds"])
                )
            return self._window

    def _repoint(self, v: _Voice, identity: str) -> None:
        """The voice's attention moves to this person. It hears nobody: what they ask reaches it as text
        (`_answer_heard`)."""
        v.last_activity = self._clock()
        if identity != v.speaker:
            v.speaker, v.ack_rows = identity, []
            v.label, v.followed_at = self._label(identity), self._clock()

    # -- requests (conversation engine §3.3-§3.5) --

    async def _start_requests(
        self,
        identity: str,
        mark: int,
        routed: list[dict],
        heard_at: float,
        ack: bool = True,
    ) -> None:
        """/route split the line: every request starts now (cap MAX_WORKING), and the first request's agent
        opens its voice to say, once, who is on what. Its seed holds the list, not the line."""
        rows = self._requests.settle(
            mark, identity, routed, self._clock(), self._label(identity, mark)
        )
        self._sync_state()
        if not rows:
            return  # "everyone stop" while /route decided: nothing to start, nothing to say
        self._start_queued()
        if not ack:
            return
        await self.summon(
            identity,
            rows[0].agent,
            request=None,
            mark=mark,
            path="ack",
            heard_at=heard_at,
            ack=rows,
        )

    def _ack(self, rows: list[Request] | None, said: bool = False) -> bool:
        """Any of `rows` is still open (re-review N1). `said`: they are acknowledged now (understanding §4.6)."""
        rows = [r for r in rows or [] if r.status in OPEN]
        for r in rows if said else ():
            r.acked = True
        return bool(rows)

    def _start_queued(self) -> None:
        for r in self._startable():
            self._requests.start(r)
            self._spawn(self._run_request(r))

    async def _run_request(self, r: Request) -> None:
        """One request's /turns, posted without waiting for a voice. It never steers another run: an agent's
        rows run one at a time (understanding §4.5)."""
        run_id = str(uuid.uuid4())
        self._requests.bind(r, run_id)
        try:
            await self._ensure_window(r.asker)
        except Exception:
            logger.info(
                "huddle request could not open the window agent=%s",
                r.agent,
                exc_info=True,
            )
            self._request_failed([r], run_id)
            return
        if r.status != "working":
            return  # stopped while the window opened
        self._runs.started(run_id, r.agent, speaker=r.asker, request=r.task, gen=None)
        # ponytail: a voiceless _Voice carries _post_turn's speaker/posting fields; gen 0 is never a session
        carrier = _Voice(r.agent, None, 0, r.asker, self._clock(), label=r.label)
        resp = await self._post_turn(
            carrier,
            run_id,
            r.task or self._last_line.get(r.asker, ""),
            requests=self._requests.lines(self._agent_name, skip_agent=r.agent),
        )
        if resp is None and run_id not in self._stopped:
            self._request_failed([r], run_id)

    def _overdue(self, r: Request, run: Run, now: float) -> None:
        """A mouth's request still working (`tick`): what its asker is owed while the run says nothing
        (RUN_STATUS_S), and when it has not ended at all (RUN_DEADLINE_S)."""
        # The deadline is execution time. A run queued behind a chat run (run_queued) has not begun: it is
        # waited for as long as `Runs.running` allows (QUEUED_TURN_SECONDS), and its clock starts as it leaves.
        if run.accepted and not run.queued and now - run.accepted >= RUN_DEADLINE_S:
            self._runs.give_up(run.run_id)
            self._requests.fail(r)
            emit_metric("live.request.overdue", 1, "Count", {"what": "gave_up"})
            # an answer, like any failure (`_say_failed`): it waits its turn, and replaces what was owed before
            self._utter(
                r.agent, "answer", self._line(COULD_NOT), obligation=run.run_id, request=r.task, audience=r.asker,
                label=r.label,
            )  # fmt: skip
            self._start_queued()
            self._sync_state()
            return
        if now - run.seen < RUN_STATUS_S or run.run_id in self._status_said:
            return
        # Its asker is still waiting on it, and has had nothing from it for as long: they are talking with
        # the agent, nobody else has taken a turn since they last spoke, and the room is at a pause.
        # Anything else is a meeting that has moved on, and the agent says nothing into it.
        e = self._engagement((r.asker, r.label), r.agent)
        waiting = (
            e is not None
            and e.state == "active"
            and e.expects == "answer"
            and now - e.at >= RUN_STATUS_S
            and not any(
                at > self._turn_at.get(e.person, 0.0)
                for other, at in self._turn_at.items()
                if other != e.person
            )
        )
        if waiting and not self._speaking and now - self._last_heard >= MEET_QUIET_S:
            self._status_said.add(run.run_id)
            emit_metric("live.request.overdue", 1, "Count", {"what": "status"})
            self._utter(
                # a queued run has not begun: "still working" would be false
                r.agent, "status", self._line(QUEUED_LINES if run.queued else STILL_WORKING), obligation=run.run_id,
                audience=r.asker, label=r.label,
            )  # fmt: skip

    def _request_failed(self, rows: list[Request], run_id: str) -> None:
        """Spec §3.5: the request is failed; in its turn its agent says CANT_REACH (never posted)."""
        rows = [r for r in rows if r.status in ("queued", "working")]
        if not rows:
            return
        for r in rows:
            self._requests.fail(r)
        r = rows[-1]
        self._utter(
            r.agent,
            "answer",
            CANT_REACH,
            obligation=run_id,
            request=r.task,
            audience=r.asker,
            label=r.label,
        )
        self._sync_state()

    # -- runs --

    async def _drive(
        self,
        v: _Voice,
        run_id: str,
        speaker_name: str | None,
        request: str,
        row: Request | None = None,
    ) -> None:
        """The voice's driven turn, posted by the worker. `speaker_name`: who asked, fixed when the run was made.
        `row`: the request row it answers (understanding §4.6)."""
        self._runs.started(
            run_id, v.agent_id, speaker=v.speaker, request=request, gen=v.gen
        )
        lines = (
            self._requests.lines(self._agent_name, skip_agent=v.agent_id)
            if row is not None
            else ()
        )
        if (
            await self._post_turn(
                v, run_id, request, requests=lines, speaker_name=speaker_name
            )
            is not None
        ):
            v.driven_ok = v.driven_ok or v.driven_run == run_id
            return
        voiced = self._voice is v and v.live is not None and run_id not in self._stopped
        if voiced:
            self._say_failed(v, run_id, request)
            if row is not None:
                self._requests.fail(
                    row
                )  # its voice says CANT_REACH: nothing says it again
        elif row is not None and run_id not in self._stopped:
            self._request_failed([row], run_id)
        if row is not None:
            self._start_queued()  # the agent's next row
            self._sync_state()

    def _stop(self, agent_id: str, mark: int | None = None) -> None:
        """Everything of that agent's stops ("everyone stop", `_stop_all`): what it is saying is struck and every
        running run of that agent (and no one else's) is aborted. `mark`: the stop's line (default: the newest);
        a line said at or before it, even one still at /route, starts nothing for it."""
        if v := self._floor_voice(agent_id):
            v.last_activity = self._clock()
        # Called off: nothing of the agent's is said after it, also a run with no row and the rest of an
        # answer it was saying.
        self._strike(u for u in self._ledger if u.agent == agent_id)
        for e in self._engagements():
            if e.agent == agent_id:
                e.expects = "nothing"
        aborted = [run.run_id for run in self._runs.running(agent_id)]
        for run_id in aborted:
            self._runs.abort(run_id)  # silent from here on, whatever the backend says
            self._stopped.add(run_id)
        mark = self._transcript.last_mark if mark is None else mark
        self._requests.cancel(agent_id, mark)  # no run_ended follows an abort
        for run_id in aborted:
            self._spawn(self._abort(run_id))
        self._start_queued()  # the bridge's own semaphore makes a fifth /turns wait for the aborted run
        self._sync_state()
        self._pump()

    def _stop_all(self, mark: int | None = None) -> None:
        """ "everyone stop" / "stop all" (engine §3.6): every open request cancelled (a line /route is still
        splitting starts nothing), every run aborted, the floor cut, and nothing left queued to answer."""
        mark = self._transcript.last_mark if mark is None else mark
        rows = self._requests.cancel(mark=mark)
        agents = {r.agent for r in rows} | {r.agent_id for r in self._runs.running()}
        if self._voice:
            agents.add(self._voice.agent_id)
        self._queue = []
        for agent_id in sorted(agents):
            self._stop(agent_id, mark)
        emit_metric("live.request.stop_all", 1, "Count")

    def _no_post(self, agent_id: str | None, asker: str | None = None) -> None:
        """ "don't post that": the named agent's newest open request, else the room's newest; with no
        request, that agent's (unnamed: anyone's) newest running run. Neither late-answer door posts it, and
        the worker never does; it may still be spoken."""
        r = self._requests.no_post(agent_id)
        run_id = r.run_id if r else None
        if r is None:
            runs = self._runs.running(agent_id)
            # named: that agent's newest held answer; unnamed: only one the speaker asked for
            news = [
                self._news_of(u)
                for u in self._ledger
                if u.kind == "answer"
                and (u.agent == agent_id if agent_id else u.audience == asker)
            ]
            run_id = (
                runs[-1].run_id
                if runs
                else max(news, key=lambda n: n[3])[0]
                if news
                else None
            )
            if run_id is None:
                return  # nothing of that agent's (or anyone's) is open: nothing is marked or counted
            self._requests.suppress(run_id)
        emit_metric("live.request.no_post", 1, "Count")
        if (
            run_id
        ):  # a request not started yet is marked when its /turns lands (_post_turn)
            self._spawn_mark(run_id)

    def _spawn_mark(self, run_id: str) -> None:
        """Marks are tracked: hang-up waits for them (clear_presence)."""
        t = asyncio.create_task(self._mark_no_post(run_id))
        self._marking.add(t)
        t.add_done_callback(self._marking.discard)
        self._tasks.add(t)
        t.add_done_callback(self._done)

    async def _mark_no_post(self, run_id: str) -> None:
        try:
            await self._backend.no_post(run_id)
        except Exception:
            logger.info("huddle no-post mark failed run=%s", run_id, exc_info=True)

    async def _abort(self, run_id: str) -> None:
        try:
            await self._backend.abort(run_id)
        except Exception:
            logger.exception("huddle abort failed run=%s", run_id)

    async def _post_turn(
        self,
        v: _Voice,
        run_id: str,
        utterance: str,
        requests: list[str] | tuple = (),
        speaker_name: str | None = None,
    ) -> dict | None:
        """POST /turns, retrying a waking workspace -> its response. None (and the run aborted) when it never started.
        `speaker_name`: who asked, as fixed when the run was made; read once here otherwise, never per retry."""
        agent_id = v.agent_id
        speaker_name = speaker_name or self._owner_name(v)
        if (
            self._window is None
        ):  # the window ended before this task started: it never runs
            self._runs.abort(run_id)
            return None
        call_id, transcript = (
            self._window.call_id,
            self._transcript.since_agent(agent_id)[-MAX_TRANSCRIPT:],
        )
        v.posting += 1
        started, attempt = self._clock(), 0
        try:
            while True:
                try:
                    r = (
                        await self._backend.turn(
                            call_id,
                            v.speaker,
                            agent_id,
                            run_id,
                            utterance[:MAX_UTTERANCE],
                            transcript,
                            # The raw identity pins the audio; the Meet label is what the backend shows: the
                            # speaker this voice (or this request row) belongs to, not whoever spoke last.
                            speaker_name=speaker_name,
                            requests=list(requests),
                        )
                        or {}
                    )
                    if (
                        run_id in self._stopped
                    ):  # stopped while /turns was in flight: its abort may have missed
                        self._spawn(self._abort(run_id))
                    if self._requests.no_posted(run_id):
                        # "don't post that" said before this /turns landed: a retry rewrites the VOICERUN
                        # row and can lose a mark that landed in its read-then-put window, so mark it again
                        self._spawn_mark(run_id)
                    if run := self._runs.get(run_id):
                        run.accepted = (
                            self._clock()
                        )  # RUN_DEADLINE_S counts from here, not from the first try
                    return r
                except BackendError as e:
                    delay = min(2 ** (attempt + 1), TURN_RETRY_MAX_DELAY)
                    waited = self._clock() - started
                    # A floor switch never drops the request (it never aborts a run): retry it, the answer becomes news.
                    if (
                        _retryable(e)
                        and waited + delay <= TURN_RETRY_BUDGET
                        and run_id not in self._stopped
                    ):
                        attempt += 1
                        await self._sleep(delay)
                        continue
                    if e.code == "agent_not_in_room":
                        self._spawn(self._refresh_team())
                    self._runs.abort(
                        run_id
                    )  # never started: nothing of it may speak later
                    return None
        finally:
            v.posting -= 1
            v.last_activity = self._clock()
            self._sync_state()

    # RunSink (frames.Runs): where a run's output goes (D §8).
    def _same_session(self, run: Run) -> _Voice | None:
        """The open voice that started this run, if it is still open."""
        v = self._floor_voice(run.agent_id)
        return v if v and run.gen == v.gen else None

    def progress(self, run: Run, text: str) -> None:
        # never said: a voice said each one aloud ("I'm still checking", 10-04 prod Meet); the tile shows it
        self._sync_state()

    def ended(self, run: Run, answer: str) -> None:
        """The work is done. Nothing of a run is said as it streams (a run speaks a sentence before each lookup,
        which is an agent talking again and again over people): its answer is said when it ends."""
        if answer and self._carry_on(run, answer):
            return
        asked = self._requests.by_run(run.run_id)  # a request-list run (engine §3.5)
        v = self._floor_voice(run.agent_id)
        sorry = [
            u
            for u in self._ledger
            if u.kind == "answer"
            and u.obligation == run.run_id
            and u.state == "pending"
        ]
        if not answer:
            self._requests.delivered({run.run_id})  # finished with nothing to say
        elif run.gave_up and not sorry:
            # Its asker has been told it could not be done (RUN_DEADLINE_S), and now it is: the room gets
            # it in writing. Said aloud it would take back an apology minutes old, in the middle of
            # whatever the meeting is on by now. (Not yet told: the answer is said in the apology's place.)
            self._spawn(
                self._late_answer(run.agent_id, (run.run_id, run.request, answer))
            )
        elif (
            v is None and not asked and not (run.bound and run.speaker in self._people)
        ):
            # no voice of its agent is open and nobody here asked for it: the room gets it now
            self._spawn(
                self._late_answer(run.agent_id, (run.run_id, run.request, answer))
            )
        else:
            # The work is done and the saying of it begins: two things, each with its own end. The run's
            # last text box (updates before a tool are gone from it) is one utterance, word for word,
            # whichever voice is open or none. Ready, not delivered: delivered when its last unit is
            # reported said (`_unit_said`).
            self._requests.ready(run.run_id)
            label = (
                asked[0].label
                if asked
                else v.label
                if v
                else self._label(run.speaker or "")
            )
            self._utter(
                run.agent_id, "answer", answer, obligation=run.run_id, request=run.request,
                audience=run.speaker, label=label,
            )  # fmt: skip
        self._start_queued()  # understanding §4.5: the agent's next row starts now, not on the next tick
        self._sync_state()

    def _carry_on(self, run: Run, answer: str) -> bool:
        """The run's closing text, landing after its answer began to be said (publishes can swap: `run_ended`,
        then the last chunk). Said again from the start, its first words were heard twice. So when the new
        text goes on from what is being or was said, only the rest is said: a paused answer takes the new
        sentences in place from its cursor; one playing now, or said in full, is followed by the rest as an
        answer of its own. False: nothing of it was said, or the new text does not go on from it (the
        caller's ordinary answer, which replaces it)."""
        new = units(answer)
        prior = next(
            (u for u in self._ledger if u.agent == run.agent_id and u.kind == "answer"
             and u.obligation == run.run_id and u.state in ("speaking", "paused")),
            None,
        )  # fmt: skip
        if (
            prior is not None
            and prior.state == "paused"
            and new[: prior.cursor] == prior.units[: prior.cursor]
        ):
            prior.units = new
        else:
            base = prior.units if prior is not None else self._answered.get(run.run_id)
            if base is None or new[: len(base)] != base:
                return False
            if rest := new[len(base) :]:
                self._utter(
                    run.agent_id, "answer", " ".join(rest), obligation=run.run_id, request=run.request,
                    audience=run.speaker, label=prior.label if prior is not None else "",
                )  # fmt: skip
        self._sync_state()
        self._pump()
        return True

    def failed(self, run: Run) -> None:
        rows = self._requests.by_run(run.run_id)
        if v := self._same_session(run):
            for r in (
                rows
            ):  # its own voice says CANT_REACH now: failed, and nothing says it again
                self._requests.fail(r)
            self._say_failed(v, run.run_id, run.request)
        else:
            self._request_failed(rows, run.run_id)
        self._start_queued()  # its MAX_WORKING slot is free
        self._sync_state()

    def on_data(self, sender: object | None, topic: str | None, data: bytes) -> None:
        if (
            topic == SPEAKER_TOPIC and sender is None
        ):  # only the backend's relay is trusted
            self._on_speaker(data)
            return
        if topic != FRAME_TOPIC:
            return
        if (
            sender is not None
        ):  # only the backend's server-side SendData (no sender) is trusted
            self.dropped_frames += 1
            logger.warning("huddle: dropped a call.frame packet sent by a participant")
            return
        try:
            events = json.loads(data).get("events")
        except (ValueError, AttributeError):
            return
        for ev in events if isinstance(events, list) else []:
            if not isinstance(ev, dict):
                continue
            payload = ev.get("payload") or {}
            if ev.get("type") == "voice.run.frame":
                if v := self._floor_voice(payload.get("agentId")):
                    v.last_activity = self._clock()
                self._runs.on_event(ev)
            elif ev.get("type") == "voice.notice":
                self._voice_notice(payload)
        self._sync_state()

    def _on_speaker(self, data: bytes) -> None:
        """Recall's speech_on/off for one Meet participant: who is speaking now, and recent windows."""
        try:
            p = json.loads(data)
            pid, name = str(p["participantId"]), str(p.get("name") or "Someone")
        except (ValueError, KeyError, TypeError):
            return
        # stamped on arrival: both sides of the overlap use the worker clock; the slack absorbs relay lag
        at = self._clock()
        logger.debug("huddle meet.speaker lag %s", p.get("at"))
        if isinstance(roster := p.get("roster"), list):
            self._meet_roster = [str(x) for x in roster]
        # `kind` (speech_on | speech_off | join | leave) is newer than `speaking`; a join is not speech
        kind = p.get("kind") or ("speech_on" if p.get("speaking") else "speech_off")
        if kind == "speech_on":
            # Meet sends `on` only on a change of speaker and no `off` for the one before: everyone else
            # stopped then. Two people in one line still read "A + B": both spans overlap the line.
            for k in [k for k in self._meet_speaking if k != pid]:
                self._close_span(k, at)
            self._meet_speaking.setdefault(pid, (name, at))
        elif kind != "join":
            self._close_span(pid, at)

    def _close_span(self, pid: str, at: float) -> None:
        if (was := self._meet_speaking.pop(pid, None)) is not None:
            self._meet_windows = [*self._meet_windows, (was[0], was[1], at)][
                -SPEAKER_WINDOWS:
            ]

    def _meet_label(self, start: float, end: float) -> str:
        """Who said the line heard over [start, end]: whoever spoke for most of it, and beside them only someone
        who spoke for a real share of it ("Priya + Dan"). The speaker before is not a second speaker for the half
        second their span runs into the line (Recall's change event lags our transcriber). A line no span covers
        takes the speaker nearest it within the slack; else the one person in the Meet's roster, else "Someone"."""
        spans = self._meet_windows + [
            (n, since, math.inf) for n, since in self._meet_speaking.values()
        ]
        spoke: dict[str, float] = {}
        for n, s, e in spans:
            if (o := min(e, end) - max(s, start)) > 0:
                spoke[n] = spoke.get(n, 0.0) + o
        if spoke:
            most = max(spoke.values())
            names = [n for n, o in spoke.items() if o >= MEET_SHARE_S and o >= most / 3]
            if names:
                return " + ".join(names)
            # A short line ("Forge, status?") across a change of speaker: the change event names who took over,
            # though the one before still has the longer share of it (the event lags our transcriber).
            return max(
                (s, n) for n, s, e in spans if n in spoke and s < end and e > start
            )[1]
        near = [
            (s, n)
            for n, s, e in spans
            if s < end + SPEAKER_SLACK_S and e > start - SPEAKER_SLACK_S
        ]
        if near:
            return max(near)[1]  # the one who began last
        return self._meet_roster[0] if len(self._meet_roster) == 1 else "Someone"

    def stt_stopped(self, identity: str) -> None:
        """That person's STT stream ended (restart or unpublish): its utterance start goes with it, and so does
        speech of theirs no transcript will come for."""
        self._utter_from.pop(identity, None)
        self._turns[identity] = [
            e for e in self._turns.get(identity, []) if e.state == "awaiting-route"
        ]
        self.on_speech(identity, False)

    def _voice_notice(self, p: dict) -> None:
        """C3 notices: tell the agent's open voice, then every person (C5)."""
        agent_id, v = p.get("agentId"), self._voice
        live = v if v and v.agent_id == agent_id and v.live is not None else None
        if p.get("kind") == "renamed":
            if live:
                self._live_call(
                    live, "append_instructions", f"You are now called {p.get('to')}."
                )
            self._spawn(
                self._refresh_team()
            )  # the matcher and the STT vocabulary read the team
            self._notice(
                {
                    k: p.get(k)
                    for k in ("kind", "agentId", "from", "to", "by", "collides")
                }
            )
        elif p.get("kind") == "voice_note_saved":
            if live:
                note = f"Speaking preference from {self._member_name(p.get('by'))}: {p.get('text') or ''}"
                self._live_call(live, "append_instructions", note[:VOICE_NOTE_CHARS])
            self._notice(
                {k: p.get(k) for k in ("kind", "agentId", "name", "text", "by")}
            )

    # -- the clock --

    async def tick(self) -> None:
        """Called about once a second by the glue."""
        if self._idle_ended:
            if not self._left:  # a tick after the notice, so its publish has gone out
                self._left = True
                self._room.shutdown()
            return
        now = self._clock()
        if not self._seen_human and now - self._started >= FIRST_JOIN_GRACE:
            self._seen_human = True  # shut down once
            logger.info("huddle leaving: no human joined within %ss", FIRST_JOIN_GRACE)
            self._room.shutdown()
            return
        if self._empty_at is not None and now - self._empty_at >= EMPTY_ROOM_GRACE:
            if self._people:
                self._empty_at = (
                    None  # someone (re)connected meanwhile: a stale empty read
                )
            else:
                self._empty_at = None
                logger.info("huddle leaving: no humans left")
                self._room.shutdown()
                return
        if self._people and now - self._team_at >= TEAM_REFRESH_S:
            self._spawn(self._refresh_team())
        self._post_again(now)
        for r in self._requests.items:
            # a run that lost its run_ended (Runs.running() dropped it as stale): its request failed
            run = (
                self._runs.get(r.run_id) if r.status == "working" and r.run_id else None
            )
            if run is not None and run not in self._runs.running():
                self._request_failed([r], r.run_id)
            elif run is not None and run.bound:
                self._overdue(r, run, now)
        self._start_queued()
        self._sync_state()
        self._apply_held(now)
        meeting = self.setting == "meeting"
        if (
            not meeting  # a meeting's transcript is posted once, as the worker leaves
            and now - self._sent_at >= TRANSCRIPT_FLUSH_S
            and not self._flush_lock.locked()
        ):
            self._spawn(self._flush_transcript())
        if (u := self._unclaimed) is not None and now >= u[0]:
            if not (
                self._voice or self._speaking
            ):  # one chance: an open voice or a talker makes it stale
                _, identity, text, mark = u
                # Its speech is still open, and the route is a task of it: nothing is said until /route
                # has answered and what it decided is applied, however the task ends (`_routes`).
                token = _ROUTING.set(self._open_question_ep)
                try:
                    self._routes(
                        self._route_and_summon(
                            identity,
                            text,
                            mark,
                            humans=len(self._people),
                            unclaimed=True,
                        )
                    )
                finally:
                    _ROUTING.reset(token)
            self._unclaimed = None  # one route, one summon. After the task holds the speech: no gap between them
        v, w = self._voice, self._window
        idle = MEETING_IDLE_SECONDS if meeting else ROOM_IDLE_SECONDS
        if v is None:
            if self._window is not None and not self._lock.locked():
                async with self._lock:
                    await (
                        self._end_window()
                    )  # kept for open requests; ends after the last (spec §3.3)
            if self._people and now - self._last_heard >= idle:
                await self._end_idle_room()
            elif (
                self._queue
                and not self._speaking
                and now - self._last_heard >= MEET_QUIET_S
            ):
                # the agents named with a voice that has closed: the next one answers at a real pause
                agent_id, speaker, request, label = self._queue.pop(0)
                await self.summon(
                    speaker, agent_id, request, path="switch", label=label
                )
            else:
                self._call_on(now)
                self._pump()  # the room's mouths: whatever may start now starts
            return
        if v.live is None:
            return
        self._call_on(now)
        self._yield_check()
        self._pump()
        self._acknowledge(v, now)
        if self._queue and self._settled(v, now):
            agent_id, speaker, request, label = self._queue.pop(0)
            # its asker's label, not the latest speaker's: the meeting has talked on since the line was said
            await self.summon(speaker, agent_id, request, path="switch", label=label)
            return
        busy = (
            v.posting or self._runs.running(v.agent_id) or v.agent_state == "speaking"
        )
        # The budget is spent in summed voice seconds, not wall clock (D §4).
        over = w is not None and (
            now >= w.deadline or w.used_seconds + v.live.usage_seconds() >= w.budget
        )
        # A voice closes once it has said its piece: a working run does not hold it (its answer is a ready row,
        # said by a voice opened for it), a follow-up being said does, up to 3 windows.
        said = (
            v.agent_state not in ("speaking", "thinking")
            and not v.posting
            and now - v.followed_at >= MEET_FOLLOWUP_S
            and (not self._speaking or now - v.followed_at >= 3 * MEET_FOLLOWUP_S)
        )
        if over or said or (now - v.last_activity >= IDLE_SECONDS and not busy):
            await self.end_exchange(only=v, keep_queue=said and not over)

    def _control(self, text: str) -> bool:
        """A stop, "everyone stop" or "don't post that": applied the moment it is heard, never held."""
        agents = self._team["agents"]
        return (
            stop_all(text)
            or stop_request(text, agents)
            or no_post_command(text, agents)[0]
        )

    def _hold_line(
        self, identity: str, text: str, heard_at: float, mark: int, r: dict, kw: dict
    ) -> None:
        """/route said the turn so far is unfinished (`hold`): nothing of it is asked or said yet. Kept with its
        answer: its speaker's next piece is joined to it (`_heard_final`), or it is applied as it is once they
        are silent (`_apply_held`)."""
        if self._final_mark.get(identity, -1) <= mark:
            # understanding §4.4
            kw = {
                "humans": None,
                "switch": False,
                "unclaimed": False,
                "heard": None,
                "floor_line": False,
                **kw,
            }
            self._held[identity] = (text, heard_at, mark, {**r, "hold": False}, kw)
        else:  # that final arrived while /route decided and routed on its own: nothing left to merge
            emit_metric("live.route.hold_stale", 1, "Count")

    def _regrown(self, identity: str, mark: int) -> bool:
        """A later piece of this line's turn was heard while /route decided on it: that piece is routed with the
        whole turn (`_heard_final`), so this answer, for the turn's start alone, is not acted on."""
        last = self._final_mark.get(identity, -1)
        if (
            last > mark
            and (tid := self._turn_of.get(mark)) is not None
            and self._turn_of.get(last) == tid
        ):
            emit_metric("live.turn.regrown", 1, "Count")
            return True
        return False

    def _turn_rows(self, tid: int) -> list[Request]:
        """The open rows a turn's lines made, whichever path made them (`_apply`, a steer's fallback row). Not a
        multi-name line's strip rows (`routed` False): never started, and its own /route answer replaces them."""
        return [
            r
            for r in self._requests.items
            if r.routed and r.status in OPEN and self._turn_of.get(r.turn) == tid
        ]

    def _amend_turn(self, mark: int, ops: dict) -> dict:
        """A turn said in pieces is routed again as it grows (`_heard_final`): what it asks now REPLACES what it
        asked before, and never adds to it. A request of an agent the turn already asked is that row, amended
        with the turn's words (a queued row's task, a working row's run, `_apply`'s steer); one of another
        agent replaces the turn's rows, which are called off. A steer of the turn's own row is no follow-up.
        The turn's acknowledgement was said once, when it first asked: none again, and no small talk."""
        tid = self._turn_of.get(mark)
        mine = self._turn_rows(tid) if tid is not None else []
        if not mine:
            return ops
        ids = {r.id for r in mine}
        reqs = list(ops.get("requests") or [])
        cancel = list(ops.get("cancel") or [])
        # Each request it makes now amends one live row of the same agent, if one is left; a row the router steers
        # stays; every other row of the turn is called off, also when the whole turn now asks nothing (it was talk to
        # a person, or taken back). A `ready` row's run is over, so words cannot reach it: it is called off (its
        # answer struck) and asked again. A row the turn withdraws (`ops.cancel`) is not amended: its replacement is
        # a request of its own.
        steered = {x.get("requestId") for x in ops.get("steer") or []}
        live = [
            r for r in mine if r.status in ("queued", "working") and r.id not in cancel
        ]
        free, kept, amends, fresh = list(live), [], [], []
        for x in reqs:
            if r := next((r for r in free if r.agent == x.get("agentId")), None):
                free.remove(r)
                kept.append(r)
                amends.append(
                    {"requestId": r.id, "text": x.get("task") or "", "quiet": True}
                )
            else:
                fresh.append(x)
        cancel += [
            r.id
            for r in mine
            if r not in kept
            and not (r in live and r.id in steered)
            and r.id not in cancel
        ]
        steer = [
            {**x, "quiet": True} if x.get("requestId") in ids else x
            for x in ops.get("steer") or []
            if x.get("requestId") not in cancel
        ]
        steer += amends
        reqs = fresh
        emit_metric(
            "live.turn.amended",
            1,
            "Count",
            {
                "how": "replaced"
                if len(cancel) > len(ops.get("cancel") or [])
                else "steered"
            },
        )
        return {
            **ops,
            "requests": reqs,
            "steer": steer,
            "cancel": cancel,
            "social": None,
            "say": None,
            "called": None,
        }

    def _apply_held(self, now: float) -> None:
        """understanding §4.4: a held line whose speaker said nothing more for HOLD_APPLY_S is applied as /route
        answered it (never thrown away), once: a later final routes on its own. Not while they are still saying
        it: a piece being said, or one whose words have not come, is more of the same turn."""
        for identity, (text, at, mark, r, kw) in list(self._held.items()):
            if (
                now - self._final_at.get(identity, at) < HOLD_APPLY_S
                or identity in self._incomplete
            ):
                continue
            if self._saying(identity):
                continue
            del self._held[identity]
            emit_metric("live.hold.applied", 1, "Count")
            if kw["floor_line"]:
                self._understand_floor(
                    identity, text, mark, routed=r, stands=True, **kw
                )
            else:  # it stands whoever spoke since (review B5-3): nobody else's line re-makes its asks
                self._spawn(
                    self._route_and_summon(
                        identity, text, mark, routed=r, stands=True, **kw
                    )
                )

    def _saying(self, identity: str) -> bool:
        """They are still saying their turn: talking now, or a piece of it has ended and its words have not come.
        What they said of it so far is not answered yet (`_apply_held`, `_call_on`)."""
        return identity in self._speaking or any(
            e.state in ("speaking", "awaiting-final")
            for e in self._turns.get(identity, [])
        )

    def _call_on(self, now: float) -> None:
        """Turns their speakers did not finish. A name said alone, then CALL_ON_S of silence from its speaker:
        the agent was called on. It speaks next: an answer it has ready, else how its open work stands, else
        one short line. A line that trailed off and got no rest within TRAIL_S is forgotten."""
        for identity, (_, at, mark, agent_id) in list(self._incomplete.items()):
            silent = now - max(at, self._heard_at.get(identity, -math.inf))
            if self._saying(identity) or silent < (CALL_ON_S if agent_id else TRAIL_S):
                continue
            del self._incomplete[identity]
            if (
                agent_id is None
                or identity not in self._people
                or self._agent(agent_id) is None
            ):
                continue
            self._invite(self._who(identity, mark), agent_id)
            if any(u.agent == agent_id and u.kind == "answer" for u in self._ledger):
                continue
            task = self._requests.task(agent_id) or self._runs.working(agent_id)
            owed = ("status", task) if task else ("here", "")
            if v := self._floor_voice(agent_id):
                self._say_line(v, owed)
            else:
                self._spawn(
                    self.summon(identity, agent_id, request=None, mark=mark, owed=owed)
                )
        self._pump()

    def _acknowledge(self, v: _Voice, now: float) -> None:
        """understanding §4.6: once per turn of speech, after ACK_QUIET_S of quiet from the speaker and with nothing
        of theirs still at /route, who is on what. A lone working row of the floor agent needs none ("On it")."""
        sp = v.speaker
        if not v.ack_rows or sp in self._speaking or self._understanding.get(sp):
            return
        if now - self._final_at.get(sp, -math.inf) < ACK_QUIET_S:
            return
        rows, v.ack_rows = (
            [r for r in v.ack_rows if r.status in OPEN and not r.acked],
            [],
        )
        if len(rows) >= 2 or any(
            r.agent != v.agent_id or r.status == "queued" for r in rows
        ):
            self._say_ack(v, rows)

    def _quiet(self, v: _Voice, now: float) -> bool:
        """The floor-holder is not speaking and QUEUE_SETTLE has passed since it last did anything."""
        return v.agent_state != "speaking" and now - v.last_activity >= QUEUE_SETTLE

    def _settled(self, v: _Voice, now: float) -> bool:
        """The floor-holder answered: it is quiet and its driven run ended."""
        return self._quiet(v, now) and not any(
            r.run_id == v.driven_run for r in self._runs.running(v.agent_id)
        )

    def _post_news(self, agent_id: str, n: tuple) -> None:
        """An answer nobody will hear here (`_expire`) goes to the room as the agent's own message -- never
        dropped. 10-02: Forge's answer landed 50 s after hang-up and went nowhere; Pulse's board summary never
        surfaced."""
        emit_metric("live.announce.expired", 1, "Count")
        self._spawn(self._late_answer(agent_id, n))

    async def _late_answer(self, agent_id: str, n: tuple) -> None:
        """The room gets it in writing, once: the backend keeps one message per run id, so a post sent again
        adds nothing. Its row closes when the post has succeeded, never before (10-07 review: a failed post
        was logged and the answer was gone). One that failed stays owed and is tried again (`tick`, then the
        call's end), each wait twice the last up to LATE_RETRY_MAX_S."""
        tries = self._unposted.pop(n[0], (0,))[0]
        if not self._requests.postable(n[0]):
            self._requests.delivered(
                {n[0]}
            )  # "don't post that", or a cancelled request: nothing is owed
            self._delivery_tries.pop(n[0], None)
            emit_metric("live.request.post_skipped", 1, "Count")
            return
        try:  # a CANT_REACH line nobody heard is posted as one line (understanding §4.8)
            await self._backend.late_answer(
                agent_id,
                n[0],
                n[1][:LATE_REQUEST_CHARS],
                FAILED_POST if n[2] in (CANT_REACH, *COULD_NOT) else n[2],
            )
        except Exception:
            again = min(2**tries, LATE_RETRY_MAX_S)
            self._unposted[n[0]] = (tries + 1, self._clock() + again, agent_id, n)
            logger.info(
                "huddle late answer post failed agent=%s run=%s try=%d",
                agent_id,
                n[0],
                tries + 1,
                exc_info=True,
            )
            return
        self._requests.delivered({n[0]})  # failed stays failed
        self._delivery_tries.pop(n[0], None)
        emit_metric("live.news.posted", 1, "Count")

    def _post_again(self, now: float) -> None:
        """Written answers whose post failed, when their wait is over."""
        for _, at, agent_id, n in list(self._unposted.values()):
            if now >= at:
                self._spawn(self._late_answer(agent_id, n))

    def _state(self) -> dict[str, str]:
        """C4: what the tiles show, as the worker's participant attributes."""
        v = self._voice
        state = {
            "lightbulb.speaking_agent": v.agent_id if v else "",
            "lightbulb.listening_to": self._with(v) if v else "",
            "lightbulb.voice_limit": self._voice_limit,
            # C4: answer order with the floor-holder at the head, while a multi-name request is pending
            "lightbulb.queue": ",".join(
                ([v.agent_id] if v else []) + [q[0] for q in self._queue]
            )
            if self._queue
            else "",
        }
        # a mouth's raised hand: the oldest answer it has still to say
        hands = {
            u.agent: u.request for u in reversed(self._ledger) if u.kind == "answer"
        }
        for a in self._team["agents"]:
            aid = a["id"]
            if v and v.agent_id == aid:
                status = (
                    "joining"
                    if v.live is None
                    else "speaking"
                    if v.agent_state == "speaking"
                    else "listening"  # C4: the floor-holder is never "working"
                )
            elif self._runs.running(aid) or self._requests.working(aid):
                status = "working"
            elif hands.get(aid):
                status = "has_news"
            else:
                status = "idle"
            state[f"lightbulb.agent.{aid}.status"] = status
            state[f"lightbulb.agent.{aid}.working"] = self._requests.task(
                aid
            ) or self._runs.working(aid)
            # what a raised hand is about: the oldest answer it has ready (the Meet tile and the Live bar show it)
            state[f"lightbulb.agent.{aid}.hand"] = (
                hands[aid][:WORKING_CHARS] if status == "has_news" else ""
            )
        return state

    def _sync_state(self) -> None:
        """Writes only what changed; a key that is gone (a departed agent) is written "" (deleted)."""
        state = self._state()
        diff = {k: val for k, val in state.items() if self._published.get(k, "") != val}
        diff.update(
            {k: "" for k, val in self._published.items() if k not in state and val}
        )
        self._published = state
        if diff:
            self._room.set_attributes(diff)

    async def _end_idle_room(self) -> None:
        """Nobody has spoken for ROOM_IDLE_SECONDS (a meeting: MEETING_IDLE_SECONDS): tell everyone why,
        clear the marker, leave. A meeting first posts its whole transcript and takes the bot out of the Meet."""
        logger.info("huddle idle: leaving")
        self._room.publish(
            ERROR_TOPIC, {"code": IDLE_ENDED, "reason": None}, sorted(self._people)
        )
        self._idle_ended = True
        if self.setting == "meeting":
            await (
                self._last_posts()
            )  # as at hang-up: the unheard answers, then the transcript
            try:
                await self._backend.leave_meeting()
            except Exception:
                logger.exception("huddle meeting leave failed")
        try:
            await self._post_presence({})  # every client leaves on that notice
        except Exception:
            logger.exception("huddle idle presence clear failed")

    # -- closing (never stops a run) --

    async def end_exchange(
        self, only: _Voice | None = None, force: bool = False, keep_queue: bool = False
    ) -> None:
        """Close the open voice and, with none left, end the window. `only`: close that voice, not
        one a later summon opened. `force`: the worker is leaving; end the window even for open requests.
        `keep_queue`: a meeting voice closed with its piece said, or talked over: the agents named with it
        still answer, at the next pause (tick)."""
        self._closing = self._closing or force
        async with self._lock:  # a summon in flight finishes first, then is closed here
            if only is not None and self._voice is not only:
                return
            if not keep_queue:
                self._queue = []  # nobody is left to answer to
            await self._close_voice()
            await self._end_window(force)
        self._pump()  # the floor is free: a waiting hand is next

    async def _close_voice(self) -> None:
        """Caller holds the lock."""
        v, self._voice = self._voice, None
        self._sync_state()
        await self._close_live(v)

    async def _close_live(self, v: _Voice | None) -> None:
        """Close a voice that no longer holds the floor; its seconds go to the window."""
        if v is not None:
            # Its voice closes while it speaks (someone named another agent): the voice was only how it was
            # being said. It stays in the ledger from its cursor and is said through the next one.
            self._unsaid(u for u in self._ledger if u.via is v)
        if v is None or v.live is None:
            return
        if (
            v.wake_dims is not None
            and not v.used
            and not (v.driven_ok and v.driven_run not in self._stopped)
        ):
            # woken by name, then nothing: no (unstopped) driven run, the speaker never spoke to it again
            emit_metric("live.wake.unused", 1, "Count", v.wake_dims)
        usage = 0.0
        try:
            await v.live.aclose()
            usage = (
                v.live.usage_seconds()
            )  # after close: the final usage delta lands during it
        except Exception:
            logger.exception("huddle live session close failed")
        usage = usage or (  # a voice with no opened time bills nothing, never clock - 0
            max(
                0.0,
                min(self._clock(), self._window.deadline if self._window else math.inf)
                - v.opened,
            )
            if v.opened
            else 0.0
        )
        self._closed_seconds += usage
        if w := self._window:
            w.used_seconds += usage

    async def _end_window(self, force: bool = False) -> None:
        """Caller holds the lock. Ends the window only when no voice is open or joining."""
        w = self._window
        if w is None or self._voice is not None:
            return
        if not force and self._requests.open() and self._clock() < w.deadline:
            return  # an open request still needs /turns (spec §3.3); tick() ends it after the last
        self._window = None
        try:
            await self._backend.end(w.call_id, max(0, round(w.used_seconds)))
        except Exception:
            logger.exception("huddle end post failed call=%s", w.call_id)
