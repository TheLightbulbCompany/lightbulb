"""The Lightbulb huddle state machine: follow along, open the named agent's own voice, run one
exchange, go quiet again. Each agent speaks for itself (Option D).

Everything LiveKit-shaped is injected (`room`, `live_factory`), so this runs against fakes in tests;
agent.py is the thin glue onto the real room and GPT-Live sessions.

State:
- `_window`: the metering window (C1 /summon .. /end). It opens with the first voice and ends when no
  voice is open or joining. It owns no runs.
- `_voice`: the floor-holder's GPT-Live session (`live` is None while it joins).
- `_runs`: every voice run by run id. Closing a voice or the window never stops one.
- `_news`: finished answers of agents that did not hold the floor. Each agent announces its own (D §8):
  proactively once the room is quiet (gate.py), or at once when its has_news tile is tapped.
- `_queue`: (agent, speaker, line) still to answer once the floor settles: the rest of a multi-name request
  in the order named, and requests other people made while a voice was open (C4 `lightbulb.queue`).
- `_held`: a line /route called unfinished (`hold`), merged with that speaker's next final.
"""

from __future__ import annotations

import asyncio
import collections
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
from huddle_agent.request_list import ENDED, LINE_CHARS, OPEN, Request, RequestList, ack_line
from huddle_agent.transcript import (
    Transcript,
    address,
    addressed_by_name,
    clause_openers,
    ghost_line,
    is_question,
    no_post_command,
    pause,
    real_speech,
    soundex,
    stop_all,
    stop_bare,
    stop_request,
    opens_with_person,
    trails_off,
    stop_target,
    strip_wake,
    stt_prompt,
)

logger = logging.getLogger("huddle")

IDLE_SECONDS = 20  # no speech from the speaker and no running run of the floor-holder -> the exchange ends
FIRST_JOIN_GRACE = 60  # join dispatches the worker before the browser connects: wait for the first human
# The last human left: leave after this long, re-checking who is here. The startup snapshot and the
# first participant_connected event run concurrently; the snapshot (empty) finishing second once
# read as "everyone left" and shut a two-person huddle down 1 s after it started (10-02).
EMPTY_ROOM_GRACE = 3
THINKING_EVERY = 3  # other people's lines reach the live session at most this often
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
# ponytail: news limits are not in D (owner decision); live.announce.expired counts every drop
NEWS_PER_AGENT = 5
NEWS_TTL = 10 * 60
# A driven run still working this long after the summon: the voice says so once (Skantze: people
# talk over a silent system that is about to answer).
HOLDING_LINE_S = 12
HOLDING_LINE = (
    "Your backend self is still working on that request. Say in one short sentence that it is taking a "
    "moment; then keep listening."
)
STILL_WAKING = "Your backend is still waking up; say briefly that you're on it and it'll take a moment."
CANT_REACH = "Sorry, I couldn't reach my backend for that."
FAILED_POST = "I could not finish this on the call. Ask me again here."  # understanding §4.8: a failure nobody heard
NOT_STARTED_POST = "I did not get to this on the call. Ask me again here."  # §4.5: still queued when the call ended
LATE_REQUEST_CHARS = 2_000  # LateAnswerBody.request max (backend huddles.py)
REQUEST_QUIET_S = 1  # engine §3.5: a requested answer waits for no human speech this long (no rate limit)
DELIVERY_TRIES = 3  # a requested answer whose voice failed to open this often goes to the room
ASKER_GONE_S = 60  # engine §3.5: its asker left and no pause came this long: the room gets it
# ponytail: inferred (D §5); a delegation the new voice makes this soon after opening is the driven request
BIND_SECONDS = 10
# understanding §4.6: a delegation waits this long for the final of a speech still being transcribed
DELEGATION_WAIT_S = 2.0
SAME_SPEECH = 0.6  # this share of one transcript's content words in another's: the same speech (§4.6)
RECORDS_KEPT = 4  # understood speeches kept per speaker for delegations to match
ALREADY_SAID = "You already gave that answer. Do not repeat it."
IN_HAND = "That is already in hand. Wait for the result."
# for the voice to know, never to read out: "Forge has those results" told the person nothing (10-04 prod test)
REQUESTS_NOTE = "Requests on this call (for you to know; say none of it unless someone asks):\n"
UNFINISHED = 'They have not finished the sentence. Say "go on" at most, then wait.'  # §4.4: its line is held
CHANGED = "That change is made. Say so in a few words."  # a correction or a call-off, already applied (§4.4)
HANDOFF_SECONDS = (
    8  # the floor-holder asked someone by name: they may answer without naming it
)
TRANSCRIPT_FLUSH_S = (
    10  # how often the lines said since the last batch go to the backend
)
QUEUE_SETTLE = 1  # the floor-holder's driven run ended and it stopped speaking this long ago: the next answers
FOLDED = "Folded into your follow-up; the answer comes on that one."
QUEUED_BEHIND = "You're finishing something else in this room first; say so briefly, then answer when it's done."
ANNOUNCE_NOW = "Say it now, briefly, as yourself, then stop."
REPEAT_SECONDS = 10  # live.wake.repeat: the same person names the same agent this soon after a voice_failed
VOICE_NOTE_CHARS = 1_800  # append_instructions stays under ~500 tokens (D §7)
PRESENCE_TIMEOUT = (
    3  # the last (empty) presence must land before LiveKit cancels the job
)
# ponytail: inferred; an unfinished line (/route "hold") merges with the same speaker's next final this soon
HOLD_SECONDS = 15
HOLD_APPLY_S = 4.0  # understanding §4.4: a held line its speaker said nothing more after is applied as it is
ACK_QUIET_S = 1.0  # understanding §4.6: the open voice says who is on what once its speaker is this quiet
SAID_ORDER_WAIT_S = 10.0  # a line waits this long at most for its speaker's earlier line to be applied
CUT_HANDLED_S = 2.0  # a cut answer is posted this long after its cutter's final: /route answered, a call-off applied
CUT_WAIT_S = 10.0  # ...or this long after the cut when no final ever comes
# A Live voice's own speaker cut its answer and it made no reply to them: said once more, in these words.
RESAY = "Your answer was cut off before it was heard, by talk that was not for you. Say it again now: {answer}"
MISSED_SECONDS = 10  # live.route.missed: nobody answered, then the same person names an agent or Lightbulb
UNWANTED_SECONDS = 5  # live.route.unwanted: a Jev-routed voice the asker stops or switches away from this soon
LATE_LINES = "Said in the room since you were asked (may or may not concern you):\n"
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
MEET_SHARE_S = 1.0  # a second speaker is named on a line only when they spoke this long in it
# a meeting voice takes a line with no agent name only from its own speaker, begun this soon after its last word
MEET_FOLLOWUP_S = 10.0
# a ready answer waits for a real pause in a meeting, not a breath between two sentences (Live: REQUEST_QUIET_S)
MEET_QUIET_S = 3.0
# A meeting voice is muted, so the worker stops it for someone who keeps talking over it this long. Shorter is a
# backchannel ("mm-hm", a cough): the agent carries on.
TALK_OVER_S = 1.0
# A meeting voice never hears the question (it is muted), so its seed says what to say once the run is started:
# without it the model greeted ("Yes, I'm here. What's up?", 10-04 prod Meet).
# No example phrase: given "like 'On it.'", it said "On it." every time (10-05 prod Meet). Its words are its own.
TRAIL_S = 8.0  # a meeting line that trails off ("Can one of you tell me...") waits this long for its rest
SAID_LOG_N, SAID_LOG_S = 12, 120.0  # a meeting hand whose words the agent said meanwhile is let go, not said again
MEET_ACK = (
    " Acknowledge it now in a few words of your own that fit what was asked, a different way each time; say "
    "nothing about this note; then wait for the result."
)
# The final POST's backoff and its whole budget. The budget stays under the job's shutdown window
# (agent.py SHUTDOWN_S): the flush usually runs from the shutdown callback.
FINAL_TRANSCRIPT_RETRY_S = (1, 2, 4, 8, 16)
FINAL_TRANSCRIPT_BUDGET_S = 45
UNHEARD_BUDGET_S = 10  # a meeting's unheard answers are posted before its transcript, within this
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


def _said_bags(said: str) -> tuple[collections.Counter, collections.Counter]:
    """What was heard, as bags the sentences of an answer spend in order (`_unsaid_with`): every word, and the
    content words. One set of bags serves every answer of one delivery, in delivery order, so two answers that
    share wording are not both heard for the price of one (Codex on #3792)."""
    words = _words(said)[1]
    return collections.Counter(words), collections.Counter(w for w in words if w not in FUNCTION_WORDS)


def _unsaid_with(answer: str, bags: tuple[collections.Counter, collections.Counter]) -> tuple[str, list[str]]:
    """`unsaid_part` against bags the answers said before this one have already spent: (the rest, the heard
    words the cut sentence spent -- what a boundary carries over so its other half completes it)."""
    all_left, left = bags
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", answer.strip()) if x]
    for i, sentence in enumerate(sentences):
        words = _words(sentence)[1]
        content = [w for w in words if w not in FUNCTION_WORDS]
        spend: list[str] = []  # the heard words this sentence uses up, said to its end or not
        if not content:  # "Yes." / "I can.": said only word for word, each word once
            for w in words:
                if all_left[w] - spend.count(w) > 0:
                    spend.append(w)
            said = len(spend) == len(words)
        else:
            for w in content:
                if left[w] - spend.count(w) > 0:
                    spend.append(w)
                    continue
                key = soundex(w) if len(w) > 2 and w.isascii() else ""
                # misheard by the other transcriber ("peers" for "PRs"): spend the word that sounds like it
                heard = next((x for x in left if len(x) > 2 and x.isascii() and left[x] - spend.count(x) > 0 and soundex(x) == key), None) if key else None
                if heard is not None:
                    spend.append(heard)
            said = len(spend) / len(content) >= 0.7
            for w in words:  # its function words too ("I can deploy." then "I can."), said to its end or cut
                if w in FUNCTION_WORDS and all_left[w] - spend.count(w) > 0:
                    spend.append(w)
        for w in spend:  # also for a cut sentence: its fragment's words are not heard again by a later answer
            all_left[w] -= 1
            if w in left:
                left[w] -= 1
        if not said:
            return (answer if i == 0 else " ".join(sentences[i:])), spend  # the same object when nothing was said
    return "", []


def unsaid_part(answer: str, said: str) -> str:
    """The answer from the sentence it was cut on (owner ruling 10-07: what was heard is not said again). A
    sentence counts as said when most of its words were, in order: the words a sentence used are spent, so a
    later sentence made of the same words is not heard for free; the first sentence that was not said starts
    the rest."""
    return _unsaid_with(answer, _said_bags(said))[0]


def _words_in(a: str, b: str, share: float = SAME_SPEECH) -> bool:
    """Most of `a`'s content words are in `b` (understanding §4.6); a word the other transcriber misheard counts
    when it sounds the same ("peers" for "PRs"). `share` 1: every one, spelled the same, for a match that makes
    the worker do nothing ("the closed PRs" is not "the open PRs"). With none, only the very same words match."""
    (ca, fa), (cb, fb) = _words(a), _words(b)
    if not ca:
        return bool(fa) and not cb and fa == fb
    sounds = {soundex(w) for w in cb if len(w) > 2 and w.isascii()} - {""} if share < 1 else ()
    return sum(w in cb or len(w) > 2 and w.isascii() and soundex(w) in sounds for w in ca) / len(ca) >= share


def _same_speech(said: str, record: str) -> bool:
    """A delegation's transcript and an understood speech's text are one speech: the delegation may be cut short
    of its final, or cover more than one final. A record of fewer than 2 content words is never the part
    contained ("and what" is in every question)."""
    return _words_in(said, record) or (len(_words(record)[0]) >= 2 and _words_in(record, said))


def mixed_meet(identity: str) -> bool:
    """The whole Meet as one participant, `meet:<roomId>` (a Recall bot booked before per-person audio): who
    said a line comes from speaker events (labels). A person of the Meet is `meet:<roomId>:<participantId>`,
    their own microphone and their own name."""
    return identity.startswith("meet:") and identity.count(":") == 1


def in_meet(identity: str) -> bool:
    """Anyone heard through a Google Meet, mixed or per-person. A meeting is people talking to each other with
    the agents in the room: only named asks and its asker's follow-up reach a voice, and a late answer waits
    for a pause."""
    return identity.startswith("meet:")


def mouth(identity: str) -> bool:
    """The voice for this speaker has no ears: the worker listens, never the model. It speaks what it is
    ordered to, hears what was said as text, and is stopped by anyone who keeps talking over it. Every Meet
    voice, mixed or per-person (owner ruling 10-07; rig: a voice that heard its person answered talk meant
    for others and said facts of its own)."""
    return in_meet(identity)


def _retryable(e: BackendError) -> bool:
    """A cold workspace (503 runtime_unavailable), the API gateway's own 503/504, or no answer at all."""
    return e.status in (0, 503, 504)


class Live(Protocol):
    """One agent's GPT-Live session (agent.py `_Live`)."""

    agent_id: str
    track_name: str  # f"agent-{agent_id}" (C7)

    def set_participant(self, identity: str) -> None: ...
    def append_thinking(
        self, text: str, *, delegation_id: str | None = None
    ) -> None: ...
    def append_commentary(
        self, text: str, *, delegation_id: str | None = None
    ) -> None: ...
    def append_instructions(self, text: str) -> None: ...  # a standing rule
    def interrupt(self) -> Any: ...  # AgentSession.interrupt(force=True)
    def mute(self) -> None: ...
    def nudge(self) -> None: ...  # generate_reply(): a request the model may decline
    def say(self, text: str) -> None: ...  # speak the text word for word
    def reply_to(
        self, text: str
    ) -> None: ...  # generate_reply(user_input=text): a typed user turn
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
    used_seconds: float = (
        0.0  # summed GPT-Live seconds of every voice closed in this window
    )


@dataclass
class _Driven:
    """The summoning request's turn, started by the worker in parallel with the voice open (D §5)."""

    run_id: str  # minted once, resent on every retry (C1)
    bind_until: float = math.inf  # opened + BIND_SECONDS once the voice is up
    speaker_name: str | None = None  # who asked, fixed here: the voice may be someone else's by a retry
    posted: bool = False  # /turns accepted it
    started_at: float = 0.0
    held: bool = False  # the HOLDING_LINE was said for it
    pending: str | None = None  # a delegation that arrived before /turns answered


@dataclass
class _Voice:
    agent_id: str
    live: Any  # None while joining
    gen: int  # one per opened session: a run's delegation id is valid only in its own gen
    speaker: str
    last_activity: float
    wake: str | None = None  # the summoning line (None for a tap)
    opened: float = 0.0
    agent_state: str = "listening"  # GPT-Live's AgentState
    naming: bool = False  # a first-time-naming question is open (C4)
    posting: int = 0  # turn POSTs in flight
    lines: list[str] = field(
        default_factory=list
    )  # other people's lines not yet sent as thinking
    last_thinking: float = 0.0
    dropping: bool = False  # its live session failed; a close is on the way
    driven: _Driven | None = None
    driven_run: str | None = (
        None  # the driven run's id, kept after it binds: the queue waits for it
    )
    path: str = (
        "name"  # live.latency.* dimension: name | jev | tap | switch (a queue advance)
    )
    handoff: tuple[str, float] | None = (
        None  # (speaker to return to, until): the agent asked someone by name
    )
    announce: tuple | None = (
        None  # the news item it opened to announce, until its audio starts
    )
    announcing: bool = False  # a proactive announcement's audio is playing
    told: list = field(default_factory=list)  # the news its announcement says: posted if a person cuts it (§4.8)
    said: list[str] = field(default_factory=list)  # what it said, as GPT-Live heard itself: the cut sentence is known
    wake_dims: dict[str, str] | None = (
        None  # live.wake.fired's dimensions, for a voice a name woke
    )
    used: bool = (
        False  # a delegation or pinned-speaker activity: live.wake.unused stays silent
    )
    driven_ok: bool = False  # the driven run (driven_run) was accepted by /turns
    heard_at: float | None = (
        None  # live.latency.*: when the waking line was heard; None once first_audio is out
    )
    list_version: int = 0  # the request list version this voice last heard
    # understanding §4.6: rows its speaker's lines made, acknowledged once they are quiet (tick)
    ack_rows: list = field(default_factory=list)
    # a meeting: the labelled speaker who has its attention ("": nobody may follow up), and when its follow-up
    # window last restarted (it opened, stopped speaking, or took a follow-up)
    label: str = ""
    followed_at: float = 0.0
    saying: str | None = None  # a meeting: the run whose answer it is saying now (`_talked_over`)
    # Live's model stops for its own speaker's sound, so they can cut its answer and the worker never cut it:
    # `answer` is the ended run's (run_id, request, text, at, speaker) until it is known heard; `unheard`: its
    # speech was interrupted, or their sound came before a word of it; `replied`: it then answered them (they
    # were talking to it); `resaid`: it was said again once; `spoke`: its messages since it began to say that
    # run's text box `box` (its sentences are streamed before the run ends; narration before a tool is another
    # box, and its words are not the answer's); `began`: when it began to say the run.
    answer: tuple | None = None
    cut: bool = False  # a message of it was interrupted: stays, whatever plays to its end afterwards
    pending: bool = False  # text handed to it that it has not finished saying (since it last went quiet)
    began: float = 0.0
    box: tuple | None = None
    unheard: bool = False
    replied: bool = False
    resaid: bool = False
    spoke: int = 0


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
    ):
        self.setting = setting
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
        self._meet_roster: list[str] = []  # the humans in the Meet, from the latest packet that had a roster
        self._unclaimed: tuple | None = (
            None  # (deadline, identity, text, mark): an open question
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
        # agent -> [(run_id, request, answer, at, speaker)]
        self._news: dict[str, list[tuple[str, str, str, float, str | None]]] = {}
        # understanding §4.8: agent -> (news a person cut off mid-announcement, who cut it, when); posted once their
        # words are handled, dropped by a "don't post that" or a stop said over it
        self._cut_off: dict[str, tuple[list, str, float]] = {}
        self._rate = gate.RateLimit()
        # run id -> (attempts, last at): a requested delivery whose voice did not open (engine §3.5)
        self._delivery_tries: dict[str, tuple[int, float]] = {}
        self._voice_limit = ""  # why the backend refuses to open a voice (its /summon reason); "" when it does not
        self._trail: dict[str, tuple[str, float]] = {}  # a meeting line that trailed off, and when
        self._said_log: dict[str, list[tuple[float, str]]] = {}  # what each agent said lately, to let a said hand go
        self._rowless: set[str] = set()  # a meeting's ready answers that have no request row (delivered like one)
        self._level: set[str] = set()  # humans in the SFU's active speakers: sound, maybe not speech
        self._voiced: set[str] = set()  # humans the transcriber's speech detector hears speaking
        self._speaking: set[str] = set()  # humans talking now: both of the above
        self._talk_from = 0.0  # when the talking now going on began (a meeting's talk-over, `_talked_over`)
        self._last_speaker: str | None = None
        self._last_agent: dict[
            str, str
        ] = {}  # identity -> the agent they last summoned
        self._queue: list[
            tuple[str, str, str, str]
        ] = []  # (agent_id, speaker, request, the asker's meeting label) still to answer
        # identity -> (line, at, mark, its /route answer, _route's arguments): unfinished, /route said hold
        self._held: dict[str, tuple[str, float, int, dict, dict]] = {}
        self._final_mark: dict[
            str, int
        ] = {}  # identity -> mark of their latest final: a hold older than it is stale
        self._final_at: dict[str, float] = {}  # identity -> when their latest final was heard
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
        self._released: set[str] = (
            set()
        )  # talk_end that landed while a summon was still starting
        self._last_heard = self._started  # the room's last speech (or arrival)
        self._idle_ended = self._left = False
        self._empty_at: float | None = (
            None  # when the room last read as empty (EMPTY_ROOM_GRACE)
        )
        self._presence_v = 0
        self._stopped: set[str] = (
            set()
        )  # runs a stop aborted (a /turns still in flight is aborted again)
        self._told_queued: set[str] = (
            set()
        )  # runs whose floor-holder was told it waits behind another run
        self._cut_by: dict[
            str, str
        ] = {}  # identity -> the agent they cut off this utterance (cleared on their final)
        self._failed_at: dict[
            tuple[str, str], float
        ] = {}  # (identity, agent) -> when its voice_failed
        self._closed_seconds = 0.0  # GPT-Live seconds of every voice this job closed
        # The transcript is persisted by the backend (CloudWatch) in batches, so what people and
        # agents said in a huddle can be read back when something goes wrong in it.
        self._huddle_id = str(uuid.uuid4())
        self._sent_mark = -1  # the newest transcript line the backend has
        self._sent_at = self._started
        self._flush_lock = (
            asyncio.Lock()
        )  # shutdown waits for a batch in flight, then sends the rest
        self._requests = RequestList(self._row_changed)  # conversation engine §3.4: this call's asks, memory only
        self._window_lock = asyncio.Lock()  # one /summon however many requests start at once
        self._marking: set[asyncio.Task] = set()  # "don't post that" marks in flight
        self._closing = False  # the worker is leaving: no new window
        # understanding §4.6: identity -> their /route calls in flight, and their last understood speeches,
        # (rows, ops or None when /route failed or answered without ops, text, at), newest last. A delegation is
        # matched to these by its text, and binds or is answered instead of posting a run.
        self._understanding: dict[str, list[asyncio.Future]] = {}
        self._understood: dict[str, collections.deque] = {}
        self._final_heard: dict[str, asyncio.Event] = {}  # identity -> set by their next final
        # identity -> (when, transcript) of a delegation whose own transcript was understood (no final yet)
        self._unbound: dict[str, tuple[float, str]] = {}
        self._routing: dict[str, list[asyncio.Future]] = {}  # identity -> their lines at /route, in said order

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._done)
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
        """Every call onto a GPT-Live session goes through here. A dead session raises
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
            self._spawn(self.end_exchange(only=v, keep_queue=keep_queue))

    def on_live_closed(self, live: Any) -> None:
        """The GPT-Live session closed on its own (an unrecoverable realtime error)."""
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
            return name if name and not name.startswith("Someone") else "Someone in the meeting"
        return self._name(identity)

    def _with(self, v: _Voice) -> str:
        """Who a voice is with: in a meeting the speaker who has its attention, never whoever spoke last."""
        return v.label or self._talker(v.speaker)

    def _owner_name(self, v: _Voice, row: Request | None = None, mark: int | None = None) -> str | None:
        """The speaker name a turn carries: its request row's asker, else its line's speaker (`mark`), else its
        meeting voice's own speaker; else the person's name a meeting line set (None in a Live call)."""
        if not mixed_meet(v.speaker):
            return self._said_by.get(v.speaker)
        name = row.label if row is not None else self._label(v.speaker, mark) if mark is not None else v.label
        return name or "Someone"

    def _label(self, identity: str, mark: int | None = None) -> str:
        """A mixed meeting line's one known speaker (the line at `mark`, else the latest); "" for anyone else (the
        identity is the person), for "Someone" and for "A + B"."""
        if in_meet(identity) and not mixed_meet(identity):
            return self._said_by.get(identity) or self._name(identity)  # their own microphone: always known
        n = (self._said_at.get(mark) or self._said_by.get(identity, "")) if mixed_meet(identity) else ""
        return "" if n.startswith("Someone") or " + " in n else n

    def _mine(
        self, v: _Voice, identity: str, now: float, began: float | None = None, mark: int | None = None
    ) -> bool:
        """The line is the open voice's own. Live: its pinned speaker's. A meeting's one participant is everyone
        in the Meet: only the labelled speaker who has the agent's attention, begun within MEET_FOLLOWUP_S of
        its last word (10-04 prod Meet: a pinned voice took the whole meeting's talk as its speaker's)."""
        if identity != v.speaker:
            return False
        if not in_meet(identity):
            return True
        label = self._label(identity, mark)
        t = now if began is None else began
        return bool(label) and label == v.label and (
            v.agent_state == "speaking" or t <= v.followed_at + MEET_FOLLOWUP_S
        )

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
        for gone in set(self._people) - set(people):  # their speeches can no longer be delegated (§4.6)
            for d in (self._understood, self._unbound, self._final_heard, self._held, self._final_at):
                d.pop(gone, None)
            self._voiced.discard(gone)  # nobody who left is talking; a rejoin starts clean
            self._level.discard(gone)
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
        self._closing = True  # first: a line routed during the awaits below opens no new window
        if self._marking:  # a "don't post that" said just before hang-up lands first
            await asyncio.wait(set(self._marking), timeout=5)
        if self.setting != "meeting":
            await self._flush_transcript()  # last chance for what was said since the previous batch
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
            await asyncio.wait_for(self._post_unheard(), UNHEARD_BUDGET_S if self.setting == "meeting" else None)
        except asyncio.TimeoutError:
            logger.warning("huddle unheard answers not all posted within %ss", UNHEARD_BUDGET_S)
        open_rows = [r for r in self._requests.open() if r.routed]
        emit_metric("live.request.open_at_end", len(open_rows), "Count")  # understanding §5.3: 0
        await self._flush_transcript()  # the row changes the sweep made; a meeting's one transcript

    async def _post_unheard(self) -> None:
        """The call is ending: every answer nobody heard, and every request never started, goes to the room."""
        if (v := self._voice) and in_meet(v.speaker):
            self._keep_unsaid(v)  # handed to its voice and not said to the end, also before its first word
        held = [(a, n) for a, (items, _, _) in self._cut_off.items() for n in items]  # cut off mid-announcement
        self._cut_off.clear()
        if (v := self._voice) and v.told:  # being said as the call ends: not said to its end (§4.5)
            held += [(v.agent_id, n) for n in v.told]
            v.told = []
        # cut by its own speaker, or a second saying not yet heard; not one a follow-up replaced
        if v and v.answer and (v.unheard or v.resaid) and not self._followed(v):
            held.append((v.agent_id, v.answer))
        if v:
            v.answer = None
        for agent_id, n in held + [(a, n) for a, news in self._news.items() for n in news]:
            await self._late_answer(agent_id, n)  # unheard here: the room gets it
        self._news.clear()
        for r in [r for r in self._requests.open() if r.routed and r.status == "queued"]:
            # understanding §4.5: its agent never got to it; no run will post it, so the room gets one line
            r.run_id = str(uuid.uuid4())
            self._requests.fail(r)  # never done: the post is no delivery
            await self._late_answer(r.agent, (r.run_id, r.task, NOT_STARTED_POST, self._clock(), r.asker))

    async def on_final_transcript(self, identity: str, name: str, text: str) -> None:
        text = text.strip()
        if text and ghost_line(text, self._vocabulary):
            # Conversation engine §3.1: the STT model repeated its prompt on silence; nobody said it.
            emit_metric("live.stt.ghost_dropped", 1, "Count")
            text = ""
        # Talking is not ended here: the detector's stop comes before its sentence's final, and by then the
        # person may be saying the next sentence.
        began = self._utter_from.pop(identity, None)
        if heard := self._final_heard.pop(identity, None):
            heard.set()  # a delegation waiting for this final looks again (understanding §4.6)
        cut = self._cut_by.pop(identity, None)
        v = self._voice
        if v and v.answer and identity == v.speaker and not v.spoke and v.agent_state != "speaking":
            # their sound came before a word of its answer: Live's model drops its reply for it (`_cut_answer`)
            v.unheard = True
        if not text:
            if (
                v and cut == v.agent_id and v.live is not None
            ):  # cut off by noise: carry on
                emit_metric("live.bargein.empty", 1, "Count")
                if not mouth(v.speaker):  # a mouth has nothing to carry on with: it speaks on an order, not a nudge
                    self._live_call(v, "nudge")
            return
        now = self._clock()
        record = text  # what goes on the record: a joined line records only its new piece
        holding = False
        if in_meet(identity):
            # The transcriber ends a line at a short pause, so a thought said in pieces arrives in pieces. One
            # that trails off is not a request yet (10-06 prod Meet: "Can one of you tell me..." was run as one,
            # and the agent asked back what was meant): it is joined to what comes next.
            # The record keeps each piece as it was said; only the understanding sees them joined.
            trail = self._trail.pop(identity, None)
            if trail and self._control(text):
                trail = None  # a stop or "don't post that" stands on its own: the fragment is not its prefix
            if trail and now - trail[1] <= TRAIL_S:
                text = f"{trail[0]} {text}"
                record = text[len(trail[0]) + 1 :]  # the pieces before it are on the record already
            # (a "Forge, stop..." is a stop, not a fragment: held, it would never be applied)
            holding = trails_off(text) and not self._control(text)
            if holding:
                self._trail[identity] = (text, now)
        if self.setting == "meeting":
            self._unclaimed = (
                None  # someone spoke after the question: it was not left unclaimed
            )
            if identity.startswith("meet:") and not mixed_meet(identity):
                # one person of the Meet: their own name; the record still shows they were in the Meet
                self._said_by[identity] = name
                mark = self._transcript.add_person(identity, f"{name} (Meet)", record, at=now)
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
        self._final_at[identity] = now
        self._last_speaker = identity
        self._last_heard = now
        if holding:
            return  # on the record and someone spoke (an unclaimed question is theirs now); its rest is on the way
        mine = v is not None and self._mine(v, identity, now, began, mark)
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
        if stop_request(text, self._team["agents"]):
            # "stop" / "never mind" to an agent aborts its runs (D §8): the agent it names, from anyone, else
            # the floor-holder when its pinned speaker says it. In a mixed meeting, also a stop said over it
            # talking: "never mind" between two people must not abort a working run.
            # ...said with no word before it, or to an agent by name: "I stop" between two people is not one
            over = (
                in_meet(identity)
                and v is not None
                and v.agent_state == "speaking"
                and (stop_bare(text) or stop_target(text, self._team["agents"]) is not None)
            )
            target = stop_target(text, self._team["agents"]) or (
                v.agent_id if mine or over else None
            )
            if target:
                if v and target == v.agent_id:
                    self._unwanted(v, identity)
                self._stop(target, mark)
                return
        held = self._held.pop(identity, None)
        if held and now - held[1] <= HOLD_SECONDS and not _words_in(held[0], text, 1):
            # the rest of an unfinished line: the whole of it is the request. A final that already holds the
            # held words is the whole line (a delegation's transcript was held before its own final came).
            text = f"{held[0]} {text}"
        self._last_line[identity] = text
        a = address(text, self._team["agents"], self._team["people"])
        if in_meet(identity) and v is None and not a.agent_ids:
            # A meeting, nobody on the floor: a name anywhere in the sentence asks that agent ("...all right,
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
        barge = v is not None and v.live is not None and identity != v.speaker
        if (
            barge
            and cut != v.agent_id
            and v.agent_state == "speaking"
            and not mouth(v.speaker)  # a mouth is stopped by `_talked_over` only (see the interim path)
            and (a.agent_ids or a.lightbulb or real_speech(text))
        ):
            self._cut(v, identity)  # no interim got there first: cut on the final
            cut = self._cut_by.pop(identity, None)
        named = self._named(text, a)
        if len(named) >= 2:
            # Conversation engine §3.2: one request per named agent; /route's one Jev call splits the line.
            # Every named agent shows as working while it decides (§3.5: the strip within 1 s). A split of
            # fewer than two requests dispatches the line as today (`_dispatch`), after the round trip.
            self._requests.pending(mark, identity, named, now)
            self._sync_state()
            emit_metric("live.ack.strip_ms", round((self._clock() - now) * 1000), "Milliseconds")
            kw = {"humans": None if a.lightbulb else len(self._people), "heard": (name, a, v, cut)}
            if mine:
                self._understand_floor(identity, text, mark, **kw)  # its voice may delegate it (§4.6)
            else:
                self._spawn(self._route_and_summon(identity, text, mark, **kw))
            return
        self._dispatch(identity, name, text, mark, a, v, cut, barge, began=began)

    def _named(self, text: str, a: Any) -> list[str]:
        """Every agent a line names for the split: wake names, exact mentions, and role names that open a clause."""
        return list(dict.fromkeys(a.agent_ids + a.anywhere + clause_openers(text, self._team["agents"])))

    def _dispatch(
        self,
        identity: str,
        name: str,
        text: str,
        mark: int,
        a: Any,
        v: _Voice | None,
        cut: str | None,
        barge: bool,
        heard_at: float | None = None,
        routed: dict | None = None,
        began: float | None = None,
    ) -> None:
        """Today's handling of an addressed line: a name summons, a barge re-points, the rest routes.
        `heard_at`/`routed`: set after a multi-name line's /route round trip (its start, and its answer).
        `began`: when the line's utterance began (on_final_transcript)."""
        now = self._clock()
        rk = {} if routed is None else {"routed": routed}
        if a.agent_ids and self._requests.stopped(mark, a.agent_ids[0]):
            return  # stopped while this line's /route decided
        own = v is not None and self._mine(v, identity, now, began, mark)
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
                    identity, strip_wake(text), humans=len(self._people), quiet=True, path="name", until=fut, mark=mark
                )
                understood = (self._spawn(route), fut)
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
            self._spawn(self._named_summon(identity, text, mark, summon, understood) if understood else summon)
            return
        if in_meet(identity) and not mixed_meet(identity) and v is not None and own and not a.agent_ids:
            people = self._team["people"] + [{"name": n} for n in (*self._said_by.values(), *self._people.values())]
            if opens_with_person(text, people):
                # its person turned to a colleague ("Leo, did you push that?"): the floor is theirs no more.
                # What the voice had still to say keeps its hand up.
                self._note(f"[{self._agent_name(v.agent_id)}'s voice closes: {name} turned to someone else]")
                self._keep_unsaid(v)
                self._drop(v, keep_queue=True)
                return
        if in_meet(identity) and v is not None and not own:
            # A meeting's line that is not the voice's and names no agent: transcript only. A question may
            # still go unclaimed.
            if a.lightbulb:
                self._spawn(self._lightbulb_over(v, identity, text, mark))
            elif real_speech(text) and not pause(text) and not stop_request(text, self._team["agents"]) and is_question(text):
                self._unclaimed = (now + UNCLAIMED_QUESTION_S, identity, text, mark)
            return
        if (
            len(a.agent_ids) > 1
        ):  # "Forge and Gauge" to Forge, who holds the floor: Gauge answers next
            self._queue = [(aid, identity, text, self._label(identity, mark)) for aid in a.agent_ids[1:]]
            if (
                v.live is not None
            ):  # Forge's answer is a driven run, so the queue waits for it (D §5)
                self._drive_request(v, text, f'{name} asked: "{text}"')
            self._sync_state()
        if (
            barge and cut == v.agent_id and not a.lightbulb
        ):  # a bare Lightbulb takes the floor below
            self._barged(v, identity, name, text, mark)
            return
        if v and identity != v.speaker and a.lightbulb:
            # a bare Lightbulb while a voice is open: this person takes that floor, their line is the request
            self._spawn(self._repoint_and_understand(identity, v.agent_id, text, mark))
            return
        if a.lightbulb and v is None:
            self._spawn(self._route_and_summon(identity, text, mark, **rk))
            return
        if v is None:
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
                return
            # No name in a wake position: the backend routes the line as a chat message (Jev may pick nobody).
            self._spawn(
                self._route_and_summon(identity, text, mark, humans=len(self._people), **rk)
            )
            return
        switch = bool(
            own
            and v.live is not None
            and [x for x in a.anywhere if x != v.agent_id]
        )
        if switch:
            # "Cool, and Friday, what about you?" while Forge holds the floor (10-02: it went to Forge).
            # The backend's addressing stage decides addressed vs mentioned; addressed switches the floor.
            self._spawn(
                self._route_and_summon(
                    identity, text, mark, humans=len(self._people), switch=True, **rk
                )
            )
        if own:
            v.last_activity = v.followed_at = self._clock()  # a meeting follow-up restarts its window
            v.used = True
            v.handoff = None  # the person the agent asked answered: they keep the floor
            u = self._unbound.pop(identity, None)
            # its delegation came first and was understood from its own transcript: transcript only (§4.6). A final
            # that adds an ask has words of its own and is understood.
            late = u is not None and self._clock() - u[0] <= HOLD_SECONDS and _words_in(text, u[1], 1)
            if routed is None and not switch and not late and real_speech(text) and not pause(text):
                # understanding §4.1: the worker's own STT final is the request, not the voice's delegation
                self._understand_floor(identity, text, mark, humans=len(self._people), floor_line=True)
        elif v.handoff and real_speech(
            text
        ):  # someone else spoke first: back to who had the floor
            back = v.handoff[0]
            if self._end_handoff(v) and identity == back:
                self._live_call(
                    v, "reply_to", text
                )  # the session was pinned away from them: it never heard this
            elif identity != back:
                self._other_line(v, identity, name, text, mark, **rk)
        else:
            self._other_line(v, identity, name, text, mark, **rk)

    def _other_line(
        self, v: _Voice, identity: str, name: str, text: str, mark: int, routed: dict | None = None
    ) -> None:
        """Someone other than the pinned speaker, with a voice open (routing v2 §6): thinking for the
        floor-holder (and, batched, a steer into its run), and routed like any line, so an agent Jev picks
        answers them once the floor settles."""
        v.lines.append(f"{name}: {text}")
        self._flush_lines(v)
        if real_speech(text) and not pause(text) and not stop_request(text, self._team["agents"]):
            self._spawn(
                self._route_and_summon(identity, text, mark, humans=len(self._people), routed=routed)
            )

    def _unwanted(self, v: _Voice, identity: str) -> None:
        """live.route.unwanted: the asker stops a Jev-routed voice, or names another agent, right away."""
        if (
            v.path == "jev"
            and self._mine(v, identity, self._clock())
            and self._clock() - (v.opened or self._clock()) <= UNWANTED_SECONDS
        ):
            emit_metric("live.route.unwanted", 1, "Count")

    def on_interim_transcript(self, identity: str, name: str, text: str) -> None:
        """Partial STT text, only to cut the floor-holder off fast (D §9): anyone but its pinned speaker
        (GPT-Live hears them and handles their barge-in itself) who says a name, Lightbulb, a halt or
        real speech over it. Once per utterance; the final decides what happens next."""
        v, text = self._voice, text.strip()
        if text:
            self._utter_from.setdefault(identity, self._clock())
        if v and text and identity == v.speaker:
            v.handoff = None  # the person the agent asked is answering
        if (
            not text
            or v is None
            or v.live is None
            or identity == v.speaker
            or v.agent_state != "speaking"
            # a mouth is stopped by `_talked_over` (tick), which keeps the answer for the next pause; a
            # cut here would leave it "listening", so that path would never run and the answer would be lost
            or mouth(v.speaker)
        ):
            return
        if identity in self._cut_by:
            return
        a = address(text, self._team["agents"], self._team["people"])
        if a.agent_ids or a.lightbulb or real_speech(text):  # real_speech covers a halt
            self._cut(v, identity)

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

    def _cut(self, v: _Voice, identity: str) -> None:
        self._cut_by[identity] = v.agent_id
        if self._live_call(v, "interrupt"):
            v.agent_state = "listening"  # interim and final both arrive; cut once
            emit_metric("live.bargein.cut", 1, "Count")
        self._sync_state()

    def _barged(self, v: _Voice, identity: str, name: str, text: str, mark: int) -> None:
        """The final line of someone who cut the floor-holder off and named no agent."""
        if not real_speech(
            text
        ):  # a backchannel: the agent carries on, still pinned to its speaker
            emit_metric("live.bargein.empty", 1, "Count")
            if not mouth(v.speaker):  # a mouth has nothing to carry on with: it speaks on an order, not a nudge
                self._live_call(v, "nudge")
            return
        if not self._repoint(v, identity):
            return
        v.used = True
        if pause(
            text
        ):  # "hold on": they have the floor, there is nothing to answer yet
            self._live_call(v, "append_thinking", f'{name} cut in: "{text}"')
            return
        # understanding §4.6: understood like the floor speaker's line, never typed in as a user turn
        heard = self._understand_floor(identity, text, mark, humans=len(self._people), floor_line=True)
        self._spawn(self._answer_heard(v, identity, text, heard, f'{name} cut in: "{text}"'))

    async def _answer_heard(self, v: _Voice, identity: str, text: str, heard: asyncio.Future, said: str) -> None:
        """A line the voice did not hear itself (a barge, or any line to a muted meeting voice): once understood,
        it hears what came of it."""
        label = v.label
        rec = await heard
        if v.label != label:
            return  # a meeting: someone else named the agent meanwhile; this line's rows run without its voice
        if rec is not None and rec[1] is not None:
            self._heard(v, identity, text, rec[0])
        # /route down: D §5, their whole line is the request, started by us like a summon's
        elif rec is not None and self._floor_voice(v.agent_id) is v:
            if self._drive_request(v, text, said):
                self._live_call(v, "nudge")

    async def _lightbulb_over(self, v: _Voice, identity: str, text: str, mark: int) -> None:
        """A meeting's "Lightbulb, ..." from someone the open voice is not with: that voice closes (cut off if it
        was talking), then the line routes as it does with no voice open."""
        await self.end_exchange(only=v)
        await self._route_and_summon(identity, text, mark)

    async def _repoint_and_understand(self, identity: str, agent_id: str, text: str, mark: int) -> None:
        """A bare Lightbulb onto the open voice: the floor first, then the line is understood (§4.6)."""
        await self.summon(identity, agent_id, request=None, mark=mark)
        rec = await self._understand_floor(identity, text, mark, floor_line=True)
        if rec is not None and rec[1] is not None:
            self._heard(self._floor_voice(agent_id), identity, text, rec[0])
        elif rec is not None and (v := self._floor_voice(agent_id)) and v.speaker == identity:
            self._live_call(v, "reply_to", text)  # /route down: today's typed user turn

    def _heard(self, v: _Voice | None, identity: str, text: str, rows: list[Request]) -> None:
        """The voice was pinned away while `identity` said `text`: it hears what came of it (understanding §4.6)."""
        if v is None or self._voice is not v or v.speaker != identity:
            return
        v.ack_rows = [r for r in v.ack_rows if r not in rows]  # acknowledged here, not again in tick()
        if not rows:  # small talk: the voice answers it itself
            self._live_call(v, "reply_to", text)
        elif (line := self._ack(rows, v.agent_id, said=True)) and self._live_call(v, "append_thinking", line):
            self._live_call(v, "nudge")

    def _drive_request(self, v: _Voice, text: str, said: str, row: Request | None = None) -> bool:
        """Start `text` as the floor-holder's driven run; its own delegation of it binds instead of posting.
        `row`: the request row it answers, started and bound before /turns is posted (understanding §4.6)."""
        v.driven = d = _Driven(
            str(uuid.uuid4()),
            bind_until=self._clock() + BIND_SECONDS,
            speaker_name=self._owner_name(v, row),
        )
        v.driven_run = d.run_id
        if row is not None:
            self._requests.start(row)
            self._requests.bind(row, d.run_id)
        self._spawn(self._drive(v, d, text, row))
        return self._live_call(
            v,
            "append_thinking",
            f"{said} — already handed to your backend self; don't delegate it again",
        )

    async def _route_and_summon(self, identity: str, text: str, mark: int, **kw) -> tuple | None:
        """Route the line (`_route`, whose result it returns). A multi-name line's strip rows that did not become
        requests are dropped however the routing ends (hold, nobody, error, superseded)."""
        try:
            return await self._route(identity, text, mark, **kw)
        finally:
            if self._requests.drop_pending(mark):
                self._sync_state()

    def _understand_floor(self, identity: str, text: str, mark: int, **kw) -> asyncio.Future:
        """Understand a floor-speaker line now (understanding §4.6). The future, in flight from this call on,
        is the speech's understanding (`_route`'s result); it replaces the last speech's for any delegation."""
        fut = self._in_flight(identity)

        async def run() -> None:
            rec = None
            try:
                rec = await self._route_and_summon(identity, text, mark, **kw)
            finally:
                self._record(identity, fut, rec, text)

        self._spawn(run())
        if kw.get("floor_line") and mouth(identity) and (v := self._voice) is not None:
            # a mouth heard none of it (its follow-up, or a held one applied): told what came of it
            self._spawn(self._answer_heard(v, identity, text, fut, f'{self._talker(identity)} asked: "{text}"'))
        return fut

    def _in_flight(self, identity: str) -> asyncio.Future:
        """A speech of `identity` is being understood (§4.6): `_record` resolves it."""
        fut = asyncio.get_running_loop().create_future()
        self._understanding.setdefault(identity, []).append(fut)
        return fut

    def _record(self, identity: str, fut: asyncio.Future, rec: tuple | None, text: str) -> None:
        """A speech's understanding is known: `fut` resolves to the speaker's record of `text`, appended in arrival
        order (rec (rows, ops); None: a hold, or nothing was understood) (§4.6)."""
        if fut.done():
            return
        if rec is not None:
            rec = (*rec, text, self._clock())
            self._understood.setdefault(identity, collections.deque(maxlen=RECORDS_KEPT)).append(rec)
        fut.set_result(rec)
        flying = self._understanding.get(identity, [])
        if fut in flying:
            flying.remove(fut)

    def _small_talk(self, identity: str, text: str, more: bool, mark: int | None = None) -> str:
        """The seed line of a line that asked nothing (understanding §4.6)."""
        if more:
            return (
                f'{self._talker(identity, mark)} will list several requests. Say "go ahead" at most, then stay quiet '
                "until they finish."
            )
        return f'{self._talker(identity, mark)} said: "{text}". Small talk: reply yourself in one short sentence. Do not delegate it.'

    def _named_line(
        self, identity: str, agent_id: str, text: str, mark: int, r: Any, fut: asyncio.Future, voice: bool = True
    ) -> tuple[Request | None, list[Request] | None, str | None] | None:
        """A named line's /route answer `r`, under the floor lock (understanding §4.6). None: no `ops`, today's
        whole-line run. Else (the row its voice drives, already started; the rows to acknowledge; a seed line).
        `voice` False: no voice will drive a row; every row starts voicelessly."""
        o = r.get("ops") if isinstance(r, dict) else None
        if o is None:
            self._record(identity, fut, None, text)
            return None
        rows = self._apply(identity, mark, o)  # `hold` ignored: the person named the agent
        mine = [x for x in rows if x.agent == agent_id]
        first = mine[0] if voice and mine and mine[0] in self._startable() else None
        if first is not None:
            self._requests.start(first)  # its voice drives it; _start_queued must not post it too
        self._start_queued()
        self._record(identity, fut, (rows, o), text)
        ack = rows if len(rows) >= 2 or (rows and first is None) else None
        said = None
        if not o.get("requests"):  # asked nothing (rows dropped by a stop say nothing)
            said = (
                self._small_talk(identity, text, bool(o.get("more")), mark)
                if o.get("social") or o.get("more")
                else f'{self._talker(identity, mark)} said: "{text}"'
            )
            # /route's word on what the agent owes this line (`ops.say`): called on while its work is still
            # running, it says so, naming the work. A line that only calls on it is never a new request.
            say = o.get("say") or {}
            if say.get("agentId") == agent_id and say.get("kind") == "status" and say.get("task"):
                said += f' You are still working on this for them: "{say["task"]}". Tell them so.'
        return first, ack, said

    async def _named_summon(self, identity: str, text: str, mark: int, summon, understood: tuple) -> None:
        """A named line's summon (understanding §4.6). A re-point applies what the line asked once /route answers,
        the floor lock not held while it waits. The speech's future resolves however this ends."""
        try:
            if (v := await summon) is None:
                return
            r = (await asyncio.gather(understood[0], return_exceptions=True))[0]
            async with self._lock:
                # ...and, in a meeting, still with this line's speaker: someone else may have named the agent since
                here = (
                    self._voice is v
                    and v.speaker == identity
                    and v.live is not None
                    and (not in_meet(identity) or v.label == self._label(identity, mark))
                )
                took = self._named_line(identity, v.agent_id, text, mark, r, understood[1], voice=here)
                if not here:
                    return  # the floor moved meanwhile: the rows still run, without the voice
                if took is None:  # no ops: today's typed line, unless a stop covers it
                    if not (self._requests.stopped(mark) or self._requests.stopped(mark, v.agent_id)):
                        self._live_call(v, "reply_to", text)
                    return
                first, ack, said = took
                if first is not None:
                    task = first.task or text
                    self._drive_request(v, task, f'{self._talker(identity, mark)} asked: "{task}"', row=first)
                if line := self._ack(ack, v.agent_id, said=True):
                    self._live_call(v, "append_thinking", line)
                if said:
                    self._live_call(v, "append_thinking", said)
                if first is not None or line or said:
                    self._live_call(v, "nudge")
        finally:
            self._record(identity, understood[1], None, text)  # nothing understood: resolved all the same

    def _startable(self) -> list[Request]:
        """The rows that may start now, less the floor agent's while its voice's driven run has no row and still
        runs: one run per agent at a time, and that driven run is never overwritten (understanding §4.5)."""
        v = self._voice
        busy = (
            v.agent_id
            if v and not self._requests.by_run(v.driven_run)
            and any(x.run_id == v.driven_run for x in self._runs.running(v.agent_id))
            else None
        )
        return [r for r in self._requests.startable() if r.agent != busy]

    @staticmethod
    def _outcome(r: dict) -> str:
        return "hold" if r.get("hold") else "agent" if r.get("agentIds") or r.get("agentId") else "none"

    async def _understand(self, identity: str, *args, until: asyncio.Future | None = None, **kw) -> dict | None:
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
        try:
            r = await self._backend.route(
                self._said_at.get(mark) or self._said_by.get(identity, identity),
                (text if humans is not None else strip_wake(text))[:MAX_UTTERANCE],
                self._transcript.text()[-MAX_TRANSCRIPT:],
                humans,
                # `mine`: the speaker is already talking with the floor agent; their asks need no name (§4.4)
                floor={"agent": v.agent_id, "speaker": self._with(v), "mine": self._mine(v, identity, heard_at, mark=mark)}
                if v
                else None,
                voice_runs=(
                    [{"agent": x.agent_id, "request": x.request[:200]} for x in self._runs.running()]
                    + [{"agent": r.agent, "request": r.task[:200]} for r in self._requests.queued() if r.routed]
                )[-16:],
                at=self._transcript.times(ROUTE_LINES),
                setting=self.setting,
                unclaimed=unclaimed,
                open=self._requests.for_route(),
            )
            outcome = self._outcome(r)
            return r
        except BackendError as e:
            # spoken to the agents: say it failed (an unclaimed question asked nobody)
            if not quiet and not unclaimed and (humans is None or humans <= 1):
                self._room.publish(ERROR_TOPIC, {"code": e.code, "reason": e.reason}, [identity])
            return None
        finally:
            ms = round((self._clock() - heard_at) * 1000)
            emit_metric("live.latency.jev", ms, "Milliseconds", {"path": path, "outcome": outcome})

    def _apply(self, identity: str, mark: int, ops: dict, later: bool = False, heard: dict | None = None) -> list[Request]:
        """`ops` onto the request list (understanding §4.4, §4.5): earlier rows cancelled and steered first, then
        one row per request. A steer of a row nobody has, while a voice is open, is a new ask of the floor agent.
        `later`: someone else spoke since; a steer that would become a new ask is dropped like an unnamed request."""
        v, extra = self._voice, []
        for rid in ops.get("cancel") or []:
            if (r := self._requests.get(str(rid))) is not None and r.status in OPEN:
                self._cancel(r)
        for s in ops.get("steer") or []:
            text, r = str(s.get("text") or "").strip(), self._requests.get(str(s.get("requestId") or ""))
            if not text or (r is not None and r.status == "cancelled"):
                continue
            # a working row whose run is not bound yet will run its task as it is then (review B5-7)
            if r is not None and (r.status == "queued" or r.status == "working" and r.run_id is None):
                said = f'Added to "{r.task}": {text}'
                self._requests.amend(r, text)
                emit_metric("live.request.steered", 1, "Count", {"how": "queued"})
                if v is not None and v.live is not None:
                    self._live_call(v, "append_thinking", said)
            elif r is not None and r.status == "working":
                self._spawn(self._steer_row(r, identity, mark, text))
            elif (r is not None or v is not None) and not (later and (r is None or r.by != "name")):
                # answered already, or unknown: a new ask of its owner (superseded like any unnamed ask, §4.2)
                extra.append({"agentId": r.agent if r else v.agent_id, "task": text, "by": r.by if r else "floor"})
                emit_metric("live.request.steered", 1, "Count", {"how": "new"})
        rows = self._requests.settle(
            mark, identity, list(ops.get("requests") or []) + extra, self._clock(), self._label(identity, mark)
        )
        for r in rows:
            emit_metric("live.request.created", 1, "Count", {"by": r.by or "none"})
        heard = ops if heard is None else heard  # the line as /route understood it, before supersede (§5.3)
        kind = next((k for k in ("requests", "steer", "cancel", "social", "more") if heard.get(k)), "none")
        emit_metric("live.understand.ops", 1, "Count", {"kind": kind.rstrip("s")})  # §5.3: once per understood line
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
        if r.status in ENDED and r.routed:  # an unrouted strip row was never created (live.request.created)
            emit_metric("live.request.ended", 1, "Count", {"status": r.status})
        if r.routed and self.setting != "meeting":
            line = f"[request {r.id} {self._agent_name(r.agent)} {r.status}] {r.task[:LINE_CHARS]}"
            self._transcript.add_note(line, at=self._clock())

    def _cancel(self, r: Request) -> None:
        """`ops.cancel` (understanding §4.4): its run aborted the way _stop aborts one; its answer never said or posted."""
        v = self._voice
        run = self._runs.get(r.run_id) if r.run_id else None
        # its voice is saying it, or about to (review B5-1): cut it, as a stop does
        saying = bool(v and v.agent_id == r.agent and r.run_id) and (
            r.run_id == v.driven_run
            or (run is not None and run.gen == v.gen)
            or (v.announce is not None and v.announce[0] == r.run_id)
        )
        if r.status == "working" and r.run_id:
            self._runs.abort(r.run_id)  # a run_ended that comes later says nothing
            self._stopped.add(r.run_id)
            self._spawn(self._abort(r.run_id))
        self._requests.drop(r)
        if r.run_id and (news := self._news.get(r.agent)):
            if kept := [n for n in news if n[0] != r.run_id]:
                self._news[r.agent] = kept
            else:
                self._news.pop(r.agent)
        emit_metric("live.request.cancelled", 1, "Count")
        if saying:
            self._live_call(v, "interrupt")
            if v.announce is not None and v.announce[0] == r.run_id:
                v.announce = None
        if v is not None and v.live is not None and self._live_call(
            v, "append_thinking", f'{self._agent_name(r.agent)} dropped: "{r.task}".'
        ):
            if not mouth(v.speaker):  # a mouth has nothing to carry on with: it speaks on an order, not a nudge
                self._live_call(v, "nudge")
        self._start_queued()  # the agent's next row

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
        emit_metric("live.request.steered", 1, "Count", {"how": "run" if steered else "new"})
        v = self._voice
        if steered:
            if v is not None and v.live is not None:
                self._live_call(v, "append_thinking", f'Added to "{r.task}": {text}')
            return
        new = [{"agentId": r.agent, "task": text, "by": r.by}]
        for x in self._requests.settle(mark, speaker, new, self._clock(), self._label(speaker, mark)):
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
        # the floor speaker's line; in a mixed meeting (one participant for everyone) only a line taken as its follow-up
        own = (
            v is not None
            and identity == v.speaker
            # ...and the voice is still with that speaker: someone else may have named the agent while /route decided
            and (not in_meet(identity) or (floor_line and v.label == self._label(identity, mark)))
        )
        if own and in_meet(identity) and not mixed_meet(identity) and ops.get("people") and not (
            reqs or social or ops.get("cancel") or ops.get("steer")
        ):
            # /route understood the line as said to another person ("Monthly, with an annual option. Maya, what
            # is your current tool costing you?"): the floor is theirs no more, and what the voice had still to
            # say keeps its hand up. The understanding decides this, not a pattern on the words (owner 10-07).
            self._note(f"[{self._agent_name(v.agent_id)}'s voice closes: {self._name(identity)} is talking to someone else]")
            self._keep_unsaid(v)
            self._drop(v, keep_queue=True)
            return []
        # Routing v2 §4.2: someone else spoke since; their line routes itself, seeing this one. A named ask stands.
        # The floor speaker's line stands too: its voice heard it and may delegate it (§4.6). Cancel and steer
        # name earlier rows: they stand either way.
        later = not stands and not own and any(
            m > mark for who, m in self._spoke.items() if who != identity
        )
        if later and (reqs or social):
            named = [x for x in reqs if x.get("by") == "name"]
            if social or len(named) < len(reqs):
                emit_metric("live.route.superseded", 1, "Count")
            reqs, social, superseded = named, None, not named
        rows = self._apply(identity, mark, {**ops, "requests": reqs}, later=later, heard=ops)
        if not rows:
            if social and v is None:
                said = self._small_talk(identity, text, bool(ops.get("more")), mark)
                await self.summon(identity, social, request=None, mark=mark, path="jev", heard_at=heard_at, said=said)
            elif not (social or superseded or ops.get("cancel") or ops.get("steer")):
                self._unrouted[identity] = self._clock()
            return rows
        startable = self._startable()
        if own:
            mine = [r for r in rows if r.agent == v.agent_id]
            if mine and mine[0] in startable and v.live is not None:
                # §4.6: the floor speaker's ask is its voice's driven run; its own delegation of it binds
                task = mine[0].task or text
                self._drive_request(v, task, f'{self._talker(identity, mark)} asked: "{task}"', row=mine[0])
            if mine or not switch:
                v.ack_rows += rows  # §4.6: acknowledged once its speaker is quiet (tick)
                self._start_queued()
                return rows
        elif v is not None:
            self._start_queued()  # §4.6: they start now; answers reach the room as ready deliveries
            return rows
        if v is not None:  # a switch: the floor speaker addressed only other agents
            emit_metric("live.route.switch", 1, "Count")
        first = rows[0]
        drive = first in startable  # an agent works on one row at a time (§4.5): else it waits in line
        path = "switch" if v is not None else "name" if first.by == "name" else "jev"
        if drive:
            self._requests.start(first)  # its voice drives it (below); _start_queued must not post it too
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
        if first.status == "working" and first.run_id is None:  # its voice never opened: it runs without one
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
        (its caller, or the voice's delegation, falls back). Returns (rows, ops) once `ops` were acted on,
        ([], None) for a floor line /route failed or answered without `ops`, else None (a hold, today's routing)."""
        heard_at = self._clock()
        r = routed if routed is not None else await self._understand(
            identity, text, humans, unclaimed, quiet=heard is not None or floor_line, mark=mark
        )
        outcome = "error" if r is None else self._outcome(r)
        r = r or {}
        if r.get("ops") is not None and not r.get("hold"):
            # understanding §4.4: the backend listed the line's requests; they replace today's whole-line routing
            if heard is not None and heard[2]:
                self._unwanted(heard[2], identity)
            return await self._act(identity, text, mark, r["ops"], heard_at, switch, stands, floor_line), r["ops"]
        if floor_line and not r.get("hold"):
            return [], None
        if heard is not None and not r.get("hold"):  # a held line waits for the rest, as below
            # `heard`: (name, address, voice, cut) of a line that named 2+ agents, as on_final_transcript saw it
            hname, a, hv, cut = heard
            reqs = r.get("requests") or []
            if len(reqs) < 2:  # one ask (or no router): today's handling of the line, whatever /route said
                if self._requests.drop_pending(mark):
                    self._sync_state()
                named = self._named(text, a)
                if self._requests.stopped(mark) or all(self._requests.stopped(mark, x) for x in named):
                    return  # "everyone stop" / "<agent>, stop" said while /route decided
                cur = self._voice
                barge = cur is not None and cur.live is not None and identity != cur.speaker
                self._dispatch(
                    identity, hname, text, mark, a, cur, cut if cur is hv else None, barge, heard_at,
                    routed=None if outcome == "error" else r,  # a failed /route is asked again
                )
                return
            # Never superseded: named asks stand whoever spoke meanwhile. A voice that opened after this line
            # was heard keeps the floor; the requests still start, without the acknowledgement.
            if hv:
                self._unwanted(hv, identity)
            await self._start_requests(identity, mark, reqs, heard_at, ack=self._voice is None or self._voice is hv)
            return
        if r.get(
            "hold"
        ):  # unfinished ("Friday, we're trying to"): the speaker's next final carries the rest
            if self._final_mark.get(identity, -1) <= mark:
                # understanding §4.4: kept with its answer; applied as it is if nothing more is said (tick). A
                # delegation's own transcript carries a later mark than its speaker's last final: held too.
                kw = {"humans": humans, "switch": switch, "unclaimed": unclaimed, "heard": heard, "floor_line": floor_line}
                self._held[identity] = (text, heard_at, mark, {**r, "hold": False}, kw)
            else:  # that final arrived while /route decided and routed on its own: nothing left to merge
                emit_metric("live.route.hold_stale", 1, "Count")
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
        if not stands and any(m > mark for who, m in self._spoke.items() if who != identity):
            emit_metric("live.route.superseded", 1, "Count")
            return
        if switch and self._voice is not None and set(agent_ids) == {self._voice.agent_id}:
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
            self._queue += [(agent_id, identity, text, self._label(identity, mark)) for agent_id in agent_ids]
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

    def on_agent_said(self, agent_id: str, text: str, interrupted: bool = False) -> None:
        """What a voice said, as GPT-Live heard itself say it: every later seed and since_agent hears it,
        tagged with the person it was talking to ("Forge → Leo: ...", routing v2 §5). `interrupted`: its
        speaker's sound stopped it before the end (the SDK's mark on the message)."""
        v = self._floor_voice(agent_id)
        if v:
            v.said.append(text)
        log = self._said_log.setdefault(agent_id, [])
        log.append((self._clock(), text))
        del log[:-SAID_LOG_N]
        if v and not mouth(v.speaker):
            self._answer_message(v, text, interrupted)
        elif v and not interrupted:
            # a mouth says an answer word for word: one it said to its end is done
            done = {n[0] for n in v.told if not unsaid_part(n[2], text)}
            v.told = [n for n in v.told if n[0] not in done]
            if v.saying in done:
                v.saying = None
        to = self._with(v) if v else ""
        self._transcript.add_agent(
            agent_id, self._agent_name(agent_id), text, at=self._clock(), to=to
        )
        # D §9: the floor-holder asking a present person by name hands them the floor for HANDOFF_SECONDS
        target = addressed_by_name(text, self._people) if v else None
        if target is None or target == v.speaker or in_meet(v.speaker):
            # ...never in a meeting: naming the prospect moved its ears to them and it ran the sales call
            # (10-07 rig). Its ears move only when /route says that person is talking to it.
            return
        back = v.handoff[0] if v.handoff else v.speaker
        if self._repoint(v, target):
            v.handoff = (back, self._clock() + HANDOFF_SECONDS)

    def _end_handoff(self, v: _Voice) -> bool:
        """Return the floor to who had it. False when they left or the session died on the way."""
        back, v.handoff = v.handoff[0], None
        return back in self._people and self._repoint(v, back)

    def _floor_voice(self, agent_id: str | None) -> _Voice | None:
        """The open voice of `agent_id`. One whose session died (`dropping`, its close on the way) says nothing:
        an answer that lands meanwhile is news, not "spoken" into a dead session."""
        v = self._voice
        return v if v and v.live is not None and not v.dropping and v.agent_id == agent_id else None

    def on_speaker_activity(self, agent_id: str) -> None:
        """The live session heard the speaker (it hears no one else)."""
        if v := self._floor_voice(agent_id):
            v.last_activity = self._clock()
            v.used = True

    def on_agent_state(self, agent_id: str, state: str) -> None:
        """GPT-Live's agent state. Speaking is activity: the idle clock restarts when it stops."""
        if v := self._floor_voice(agent_id):
            was_speaking = v.agent_state == "speaking"
            if was_speaking and state != "speaking":
                v.followed_at = self._clock()  # its last word: a meeting follow-up window starts here
                v.pending = False
                if v.answer and not (v.cut or v.unheard):
                    # all it was handed, said with nothing cut: later replies (a "thanks") are not this answer's
                    v.answer = None
            v.agent_state = state
            v.last_activity = self._clock()
            if state == "speaking" and v.heard_at is not None:
                self._latency("first_audio", v)
                v.heard_at = None  # once per voice
            if state == "speaking" and self._speaking and v.path == "announce":
                emit_metric("live.floor.overlap", 1, "Count")  # a delivery's audio began over a person
            if state == "speaking" and v.announce:  # the first audio consumes the news
                v.said = []  # what it says from here is this answer's (a cut is measured against it)
                v.told = list(self._news.get(agent_id, []))
                self._consume_news(agent_id)  # all of it was in the seed
                v.announcing, v.announce = v.path == "announce", None
                emit_metric("live.announce.spoken", 1, "Count")
            elif state != "speaking":
                v.announcing = False
                # said to the end: nothing to post. A mouth also keeps answers handed to it before it
                # spoke (`ended`, tick): those are done only once it has spoken, not on "thinking".
                # A mouth reports each line it finished (`on_agent_said`): what it was handed is done then, not
                # when it stops speaking. It stops after an acknowledgement or a holding line too, and the
                # answer ordered just before that was taken for said (10-07 rig: cut after two words, no hand).
                if not mouth(v.speaker):
                    v.told = []
                    if (run := self._runs.get(v.saying or "")) is None or run.ended:
                        v.saying = None  # its answer was said to the end too
            self._sync_state()

    def on_active_speakers(self, identities: set[str]) -> None:
        """The SFU's speaking set: who makes sound. Who talks is decided in `_talking_changed`."""
        self._level = {i for i in identities if i in self._people}
        self._talking_changed()

    def on_speech(self, identity: str, started: bool) -> None:
        """The transcriber's speech detector: `identity` began or stopped speaking. It fires before the first
        word is transcribed, so nobody is talked over while their sentence is still being heard."""
        if started and identity in self._people:
            self._voiced.add(identity)
        else:
            self._voiced.discard(identity)
        self._talking_changed()

    def _talking_changed(self) -> None:
        """Who is talking: the SFU hears sound from them and the transcriber's speech detector hears speech
        (`on_speech`). Sound alone (typing, a fan, an open laptop microphone) is nobody talking: it neither
        holds an answer back nor drops one (10-04 prod test: three announcements dropped by noise).
        A person talking over a proactive announcement: before its audio, it is dropped and the news kept;
        during it, it is cut and the news stays consumed."""
        humans = self._level & self._voiced
        if humans or self._speaking:
            self._last_heard = self._clock()
        if humans and not self._speaking:
            self._talk_from = self._clock()
        started, self._speaking = humans - self._speaking, humans
        if started and self.setting == "meeting":
            self._unclaimed = None  # someone talks after the question: a person took it
        v = self._voice
        if not started or v is None or v.path != "announce":
            return
        if v.announce:
            if self.setting == "meeting" and (tried := self._delivery_tries.get(v.announce[0])):
                # A meeting talks most of the time: a drop is no failure to open, so it uses up no try. The answer
                # is tried again at each real pause until it is said or expires (NEWS_TTL) onto the record.
                self._delivery_tries[v.announce[0]] = (tried[0] - 1, tried[1])
            v.announce = None
            emit_metric("live.announce.aborted", 1, "Count")
            self._note(f"[announce {self._agent_name(v.agent_id)} dropped before its audio: someone talked]")
            if v.live is not None:
                self._live_call(v, "interrupt")
            self._drop(v)
        elif v.announcing:
            v.announcing = False
            emit_metric("live.announce.cut", 1, "Count")
            self._note(f"[announce {self._agent_name(v.agent_id)} cut off: someone talked]")
            if mouth(v.speaker):
                self._talked_over(v)  # stopped by the worker: what it had not said is kept (`_keep_unsaid`)
                return
            self._live_call(v, "interrupt")
            if v.told:  # understanding §4.8: cut off before its end; the room gets it unless the cutter forbids it
                held = self._cut_off.get(v.agent_id, ([], "", 0.0))[0]
                self._cut_off[v.agent_id] = (held + v.told, min(started), self._clock())
            v.told = []

    async def talk_start(self, identity: str, payload: str = "") -> None:
        """C6: `{agentId}`, or "" for the last agent this caller summoned, else the CEO."""
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
        news = self._news.get(
            agent_id
        )  # a has_news tile announces it now, past the rate limit
        await self.summon(
            identity,
            agent_id,
            request=None,
            path="tap",
            announce=news[0] if news else None,
        )

    async def talk_end(self, identity: str) -> None:
        v = self._voice
        if v and v.live is not None and identity == v.speaker:
            if not mouth(v.speaker):  # a mouth has nothing to carry on with: it speaks on an order, not a nudge
                self._live_call(v, "nudge")
        elif (
            self._lock.locked()
        ):  # released before the summon finished: nudge once it has
            self._released.add(identity)

    # -- the floor --

    async def summon(
        self,
        identity: str,
        agent_id: str,
        request: str | None,
        mark: int | None = None,
        path: str = "name",
        then: list[str] | tuple = (),
        announce: tuple | None = None,
        wake_dims: dict[str, str] | None = None,
        heard_at: float | None = None,
        ack: list[Request] | None = None,
        row: Request | None = None,
        said: str | None = None,
        understood: tuple[asyncio.Future, asyncio.Future] | None = None,
        label: str | None = None,
    ) -> _Voice | None:
        """`then`: the request's other named agents, queued behind this one. Any summon but a queue
        advance (path "switch") replaces a pending queue: a new name wins. `ack`: the rows the voice
        acknowledges (the open ones when it speaks); `row`: the request row `request` is (its driven run binds
        to it); `said`: a seed line that is not a request (small talk). `understood`: a named line's /route call
        in flight and its speech's record (understanding §4.6). Returns the voice it re-pointed for `understood`.
        `label`: a meeting's speaker who gets the agent's attention (default: the summoning line's)."""
        label = self._label(identity, mark) if label is None else label
        async with self._lock:
            self._unclaimed = None  # an agent takes the floor: the open question is not left unclaimed
            self._released.discard(identity)  # only a release during THIS summon counts
            self._last_agent[identity] = agent_id
            if path not in ("switch", "announce"):
                self._queue = [(aid, identity, request or "", label) for aid in then]
            old = self._voice
            if (
                old is not None and old.agent_id == agent_id
            ):  # already holds the floor: re-point, no second session
                old.label, old.followed_at = label, self._clock()  # a meeting: whoever named it has it now
                if self._repoint(old, identity):
                    if understood:  # understanding §4.6: what the line asked is applied once /route answers
                        self._sync_state()
                        return old
                    if request and row is None:  # a row left unbound runs without a voice (_act)
                        self._live_call(old, "reply_to", request)
                    elif (line := self._ack(ack, agent_id, said=True)) and self._live_call(old, "append_thinking", line):
                        self._live_call(old, "nudge")
                self._sync_state()
                return
            now = self._clock()
            self._gen += 1
            v = self._voice = _Voice(
                agent_id,
                None,
                self._gen,
                identity,
                now,
                wake=request,
                last_thinking=now,
                path=path,
                announce=announce,
                wake_dims=wake_dims,
                heard_at=now if heard_at is None else heard_at,
                list_version=self._requests.version,
                label=label,
                followed_at=now,
            )
            self._sync_state()
            # D §9: a switch opens the new voice while the old one closes (same window, no new /summon).
            # Both sessions' seconds land in the window, so the overlap is counted twice (D §4).
            opened, closed = await asyncio.gather(
                self._open(v, identity, request, mark, ack, row=row, said=said, understood=understood),
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
        said: str | None = None,
        understood: tuple[asyncio.Future, asyncio.Future] | None = None,
    ) -> bool:
        """Caller holds the lock and has made `v` the floor. False when it could not open."""
        agent_id = v.agent_id
        # understanding §4.6: a named line's /route answer is awaited beside the window and session, never before
        window, session, *heard = await asyncio.gather(
            self._ensure_window(identity),
            self._backend.session(agent_id),
            *([understood[0]] if understood else []),
            return_exceptions=True,
        )
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
                    self._named_line(identity, agent_id, request, mark, heard[0], understood[1], voice=False)
                return False
        if understood:
            text = request
            took = self._named_line(identity, agent_id, text, mark, heard[0], understood[1])
            if took is not None:
                row, ack, said = took
                request = (row.task or text) if row is not None else None
            elif self._requests.stopped(mark) or self._requests.stopped(mark, agent_id):
                request = None  # a stop covers the line (understanding §4.5): the voice opens, nothing runs
        seed = self._transcript.seed(
            agent_id, before_mark=math.inf if mark is None else mark
        )
        # An acknowledgement says only who is on what ("then wait"): its agent's ready answers stay news, and
        # tick() hands them over once the acknowledgement has been said.
        news = [] if ack is not None else self._news.get(agent_id, [])
        # One turn says them all, in the order they were asked (re-review N5); the announced item in its place
        told = [n for n in news if n is not v.announce] + ([v.announce] if v.announce else [])
        told.sort(key=lambda n: (self._requests.order(n[0]), n[3]))
        if not mouth(identity):  # a mouth says them word for word once it is open (below): not in its seed
            seed += [
                (
                    "developer",
                    self._cant_reach_seed(n)
                    or (
                        f"You finished {n[1]}: {n[2]}. Tell the room now."
                        if n is v.announce
                        else f"You finished {n[1][:80]}. Your answer: {n[2]} Deliver it now, briefly and completely, "
                        "before anything else."
                    ),
                )
                for n in told
            ]
        if row is not None and row.status != "working":
            request = None  # stopped while the voice opened (understanding §4.5): the voice opens, nothing runs
        elif row is not None:
            request = row.task or request  # a steer may have added to it while the voice opened (review B5-7)
        if request:
            # D §5: we start the turn ourselves, in parallel with the voice open. A task, not awaited:
            # a cold workspace retries for up to a minute and must not hold the floor lock.
            v.driven = _Driven(str(uuid.uuid4()), speaker_name=self._owner_name(v, row, mark))
            v.driven_run = v.driven.run_id
            # held now, not in the spawned _drive: a voice that fails before _drive runs still releases it
            self._runs.hold(v.driven_run, agent_id)
            if row is not None:  # understanding §4.6: the row (started by the caller) is this run
                self._requests.bind(row, v.driven.run_id)
            self._spawn(self._drive(v, v.driven, request, row))
            seed.append(
                (
                    "developer",
                    # the voice's own speaker: a queued or delivered voice has no line of its own to read a label from
                    f"{v.label or self._talker(identity, mark)} asked: {request} — already handed to your backend self; don't delegate it again"
                    + (f".{MEET_ACK}" if mouth(identity) else ""),
                )
            )
        # conversation engine §3.5: the one acknowledgement, of the rows still open after the awaits above
        ack_said = self._ack(ack, agent_id, said=True)
        if ack_said:
            seed.append(("developer", ack_said))
        if said:  # understanding §4.6: small talk, answered by the voice itself
            seed.append(("developer", said))
        try:
            live = await self._live_factory(
                agent_id=agent_id,
                persona=session["persona"],
                voice=session["voice"],
                speaker=identity,
                seed=seed,
            )
        except Exception:  # start_live already retried with the fallback voice
            logger.exception("huddle live session failed to start agent=%s", agent_id)
            self._notice({"kind": "voice_failed", "agentId": agent_id})
            self._failed_at[(identity, agent_id)] = self._clock()
            self._runs.release(v.driven_run)  # no voice will say it: its answer is news
            return False
        self._latency("session_started", v)
        if ack is not None and not self._ack(ack, agent_id) and not said and not (row is not None and row.status in OPEN):
            v.live, v.opened = live, self._clock()  # its rows stopped while it joined: closed unsaid
            self._runs.release(v.driven_run)
            return False
        if (
            news and not v.announce and v.path != "announce"
        ):  # an announcement dropped before its audio keeps it
            if mouth(identity):
                # handed over, not yet said: a close before its first word raises the hand again (_keep_unsaid)
                v.told = list(news)
            self._consume_news(agent_id)  # handed to its own agent: consumed
        v.live, v.opened, v.naming = live, self._clock(), bool(session.get("askName"))
        v.followed_at = v.opened
        if mouth(identity):
            # A mouth, not an ear: a voice that heard the Meet answered talk not meant for it (10-04 prod Meet,
            # 10-07 rig). What the meeting says reaches it through the worker's transcript finals only.
            self._live_call(v, "mute")
            if v.announce or v.path != "announce":  # not an announcement dropped while it opened
                for n in told:  # its finished answers, word for word, before anything else
                    self._live_call(v, "say", n[2])
        if v.driven:
            v.driven.bind_until = v.opened + BIND_SECONDS
        if v.announce:  # consumed when its audio starts (on_agent_state)
            if not mouth(identity) and self._live_call(v, "append_instructions", ANNOUNCE_NOW):
                self._live_call(v, "nudge")
        elif v.path != "announce":
            told = bool(request or ack_said or said)
            # A mouth: the seed already says who said what and what to say about it. The "is talking to you"
            # note was read out there ("Meeting is talking to me right now. Go ahead.", 10-04 prod Meet).
            if (mouth(identity) and told) or self._live_call(
                v, "append_thinking", f"{v.label or self._talker(identity, mark)} is talking to you"
            ):
                # a mouth is nudged only for something to acknowledge: its line is written, it cannot decline
                if told or (identity in self._released and not mouth(identity)):
                    # the request is already running (driven): the model acknowledges it, no typed user turn
                    self._live_call(v, "nudge")
        self._released.discard(identity)
        self._runs.release(v.driven_run)  # sentences that came while it opened are said now, none dropped
        self._sync_state()
        return True

    def gptlive_seconds(self) -> float:
        """huddle.gptlive.open_seconds: summed GPT-Live seconds of closed voices and the open one."""
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
                    self._voice_limit = (e.reason or "limit") if e.code == "voice_limit" else ""
                    self._sync_state()
                    raise
                self._voice_limit = ""
                self._window = _Window(r["callId"], float(r["deadline"]), float(r["budgetSeconds"]))
            return self._window

    def _repoint(self, v: _Voice, identity: str) -> bool:
        """False when the live session died on the way (the voice is closing)."""
        v.last_activity = self._clock()
        v.handoff = None
        if identity == v.speaker:
            return True
        v.speaker, v.ack_rows = identity, []
        if in_meet(identity):
            # a meeting voice: its attention moves to this person; no note it could read out (it is told what
            # they asked through `_answer_heard`)
            v.label, v.followed_at = self._label(identity), self._clock()
            return self._live_call(v, "set_participant", identity)
        return self._live_call(v, "set_participant", identity) and self._live_call(
            v, "append_thinking", f"{self._talker(identity)} is talking to you"
        )

    # -- requests (conversation engine §3.3-§3.5) --

    async def _start_requests(
        self, identity: str, mark: int, routed: list[dict], heard_at: float, ack: bool = True
    ) -> None:
        """/route split the line: every request starts now (cap MAX_WORKING), and the first request's agent
        opens its voice to say, once, who is on what. Its seed holds the list, not the line."""
        rows = self._requests.settle(mark, identity, routed, self._clock(), self._label(identity, mark))
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

    def _ack(self, rows: list[Request] | None, me: str, said: bool = False) -> str | None:
        """The acknowledgement of `rows` said by `me`: only those still open (re-review N1); None for none.
        `said`: it is said now, so those rows are acknowledged (understanding §4.6)."""
        rows = [r for r in rows or [] if r.status in OPEN]
        for r in rows if said else ():
            r.acked = True
        return ack_line(rows, self._agent_name, me=me) if rows else None

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
            logger.info("huddle request could not open the window agent=%s", r.agent, exc_info=True)
            self._request_failed([r], run_id)
            return
        if r.status != "working":
            return  # stopped while the window opened
        self._runs.started(run_id, r.agent, speaker=r.asker, request=r.task, delegation_id=None, gen=None)
        # ponytail: a voiceless _Voice carries _post_turn's speaker/posting fields; gen 0 is never a session
        carrier = _Voice(r.agent, None, 0, r.asker, self._clock(), label=r.label)
        resp = await self._post_turn(
            carrier,
            run_id,
            r.task or self._last_line.get(r.asker, ""),
            None,
            requests=self._requests.lines(self._agent_name, skip_agent=r.agent),
        )
        if resp is None and run_id not in self._stopped:
            self._request_failed([r], run_id)

    def _request_failed(self, rows: list[Request], run_id: str) -> None:
        """Spec §3.5: the request is failed; in its turn its agent says CANT_REACH (never posted)."""
        rows = [r for r in rows if r.status in ("queued", "working")]
        if not rows:
            return
        for r in rows:
            self._requests.fail(r)
        r = rows[-1]
        self._news.setdefault(r.agent, []).append((run_id, r.task, CANT_REACH, self._clock(), r.asker))
        self._sync_state()

    # -- runs --

    async def on_delegation(
        self, agent_id: str, delegation_id: str, pending_transcript: str
    ) -> None:
        v = self._floor_voice(agent_id)
        if v is None or self._window is None:
            return
        # a delegated stop covers its own speaker's lines, never another person's still at /route (re-review N2)
        mark = self._final_mark.get(v.speaker)
        if stop_all(pending_transcript):
            self._stop_all(mark)
            return
        if no_post_command(pending_transcript, self._team["agents"])[0]:
            return  # the final already marked it (engine §3.6); never a run
        if stop_request(
            pending_transcript
        ):  # the voice delegated the "stop" it heard: swallow it the same way
            self._stop(agent_id, mark)
            return
        v.used = True
        speaker, asked = v.speaker, self._clock()
        if (held := self._held.get(speaker)) and _words_in(pending_transcript, held[0], 1):
            # understanding §4.4: its line is held for more words and the voice heard nothing the line lacks. The
            # held line makes the run when it is applied; a run here would be a second one.
            self._live_call(v, "append_commentary", UNFINISHED, delegation_id=delegation_id)
            return
        if (
            self._bindable(v, asked)
            and not self._recorded(v.driven)
            and not self._understanding.get(speaker)
            and not any(
                x[1] is not None and x[3] >= v.driven.started_at and _same_speech(pending_transcript, x[2])
                for x in self._understood.get(speaker, ())
            )
        ):
            # a driven run no understood speech made (a named summons' whole line): this delegation's, at once,
            # unless a line of theirs understood since the run began, or still at /route, is what it carries
            heard = None
        else:
            heard = await self._this_speech(v, speaker, pending_transcript)
        if self._floor_voice(agent_id) is not v:
            return  # another voice took the floor while it waited
        d = v.driven
        recorded = self._recorded(d)
        # an empty transcript (a typed request) is never understood: today's run (understanding §4.6)
        if heard is None and pending_transcript.strip() and (recorded or not self._bindable(v, asked)):
            # understanding §4.6: no understood speech matches it, and no driven run without a record (a named
            # summons' whole line) can be it: its own transcript is understood, once
            mark = self._transcript.last_mark
            if mark >= 0 and self._requests.stopped(mark, agent_id):
                return  # a stop said since covers it (§4.5): swallowed
            emit_metric("live.delegation.unbound", 1, "Count")
            self._unbound[speaker] = (asked, pending_transcript)
            heard = await self._understand_floor(
                speaker, pending_transcript, mark, humans=len(self._people), floor_line=True
            )
            if self._floor_voice(agent_id) is not v:
                return
            if heard is None and speaker in self._held:
                # /route held it for more words (§4.4): not understood yet, so a late final is not "late"
                self._unbound.pop(speaker, None)
                self._live_call(v, "append_commentary", UNFINISHED, delegation_id=delegation_id)
                return
        d = v.driven
        # a driven run is this speech's when its understanding made it, or when it has no record of another speech
        own = d is not None and any(r.run_id == d.run_id for r in heard[0]) if heard else False
        if (own or ((heard is None or heard[1] is None) and not recorded)) and self._bindable(v, asked):
            # the new voice delegated the request we already started: bind, never a second POST (D §5)
            if d.posted:
                self._bind_driven(v, d, delegation_id)
            else:
                d.pending = delegation_id  # bound when /turns answers
            v.last_activity = self._clock()
            return
        if heard is not None and heard[1] is not None:
            self._answer_delegation(v, delegation_id, heard[0], heard[1])  # never a run of its own (§4.6)
            return
        # A typed request (wake word) leaves GPT-Live's own audio transcript empty: fall back to the
        # speaker's own latest line, never whoever spoke last.
        utterance = (
            pending_transcript.strip()
            or self._last_line.get(v.speaker, "").strip()
            or "(inaudible)"
        )
        run_id = str(
            uuid.uuid4()
        )  # once per delegation, resent on every retry (C1): a replay never runs twice
        # A follow-up never supersedes: it steers the agent's newest running run (D §8).
        steer = (self._runs.running(agent_id) or [None])[-1]
        self._runs.started(
            run_id,
            agent_id,
            speaker=v.speaker,
            request=utterance,
            delegation_id=delegation_id,
            gen=v.gen,
        )
        r = await self._post_turn(
            v, run_id, utterance, delegation_id, steer.run_id if steer else None
        )
        if r is None:
            if self._voice is v and run_id not in self._stopped:
                self._live_call(
                    v, "append_commentary", CANT_REACH, delegation_id=delegation_id
                )
            return
        if steer and r.get("steered"):
            self._runs.abort(
                run_id
            )  # never runs: the steered run answers this delegation
            old = (
                steer.delegation_id if steer.gen == v.gen else None
            )  # a delegation id never crosses sessions
            self._runs.bind(steer.run_id, delegation_id, v.gen)
            if old and self._voice is v:
                self._live_call(v, "append_thinking", FOLDED, delegation_id=old)

    def _bindable(self, v: _Voice, at: float | None = None) -> bool:
        """`at`: when the delegation came (default now); a wait for its speech does not run the bind window out."""
        d = v.driven
        run = self._runs.get(d.run_id) if d else None
        # a run that already ended was said with no delegation: nothing is left to bind
        at = self._clock() if at is None else at
        return bool(d and at <= d.bind_until and d.pending is None and not (run and run.ended))

    def _recorded(self, d: _Driven | None) -> bool:
        """`d` is the run of a row some understood speech made (understanding §4.6)."""
        return d is not None and any(
            r.run_id == d.run_id for recs in self._understood.values() for rec in recs for r in rec[0]
        )

    def _matching(self, speaker: str, said: str, exact: bool = False) -> tuple | None:
        """The speaker's understood speeches that are the delegation's (understanding §4.6), matched by text: their
        rows in order and the newest one's ops. `exact`: every content word of the delegation is in the speech,
        spelled the same. An empty transcript (a typed request) is the newest speech when it was understood within
        BIND_SECONDS."""
        recs = list(self._understood.get(speaker, ()))
        if said.strip():
            recs = [x for x in recs if (_words_in(said, x[2], 1) if exact else _same_speech(said, x[2]))]
        else:
            recs = [x for x in recs[-1:] if self._clock() - x[3] <= BIND_SECONDS]
        if not recs:
            return None
        return [r for x in recs for r in x[0]], recs[-1][1]

    async def _this_speech(self, v: _Voice, speaker: str, said: str) -> tuple | None:
        """(rows, ops) of the speech a delegation carries (understanding §4.6): an understood speech matching its
        text, else one a final brings within DELEGATION_WAIT_S or an understanding in flight (a /route call, and
        the summon it may start) brings. None: there is none."""
        deadline = self._clock() + DELEGATION_WAIT_S
        while True:
            heard = self._matching(speaker, said)
            # only answered rows: the person may be asking again, and its own final makes a new row
            answered = heard is not None and heard[0] and all(r.status == "delivered" for r in heard[0])
            if heard is not None and not answered or not said.strip():
                return heard
            if flying := self._understanding.get(speaker):
                await flying[0]
                continue
            if self._clock() >= deadline or self._floor_voice(v.agent_id) is not v:
                # an answer already given holds every word of the delegation: an ask one word away, or one
                # with more in it, is a new ask
                return self._matching(speaker, said, exact=True) if answered else heard
            heard = self._final_heard.setdefault(speaker, asyncio.Event())
            waits = [asyncio.ensure_future(heard.wait()), asyncio.ensure_future(self._sleep(deadline - self._clock()))]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waits:
                    w.cancel()

    def _answer_delegation(self, v: _Voice, delegation_id: str, rows: list[Request], ops: dict) -> None:
        """A delegation of speech the worker already understood, with no driven run to bind (understanding §4.6)."""
        live = [r for r in rows if r.status in OPEN]
        fresh = [r for r in live if not r.acked]  # rows no acknowledgement has named yet (§4.6)
        mine = [r for r in fresh if r.agent == v.agent_id]
        if rows and all(r.status == "delivered" for r in rows):
            note = ALREADY_SAID
        elif not fresh and (ops.get("steer") or ops.get("cancel")):
            note = CHANGED  # a correction or a call-off, already applied
        elif rows and not live or not rows and ops.get("requests"):
            return  # stopped since (§4.5): swallowed, as a delegated stop is
        elif live and not fresh:
            note = IN_HAND
        elif mine and fresh == mine[:1] and mine[0].status == "queued":  # else ack_line names every fresh row
            note = f'"{mine[0].task}" is next in line for you. Say so in a few words.'
            mine[0].acked = True
            v.ack_rows = [r for r in v.ack_rows if r not in rows]  # said: tick() does not say it again (§4.6)
        elif fresh:
            note = self._ack(fresh, v.agent_id, said=True)
            v.ack_rows = [r for r in v.ack_rows if r not in rows]
        elif ops.get("more"):
            note = 'They will list several requests. Say "go ahead" at most, then stay quiet until they finish.'
        else:
            note = "Nothing to look up for that. Reply yourself in one short sentence."
        self._live_call(v, "append_commentary", note, delegation_id=delegation_id)

    async def _drive(self, v: _Voice, d: _Driven, request: str, row: Request | None = None) -> None:
        """The driven turn: frames go in with delegation null until a delegation binds to it. `row`: the
        request row it answers (understanding §4.6)."""
        d.started_at = self._clock()
        self._runs.started(
            d.run_id,
            v.agent_id,
            speaker=v.speaker,
            request=request,
            delegation_id=None,
            gen=v.gen,
        )
        lines = self._requests.lines(self._agent_name, skip_agent=v.agent_id) if row is not None else ()
        if await self._post_turn(v, d.run_id, request, None, requests=lines, speaker_name=d.speaker_name) is not None:
            d.posted = True
            v.driven_ok = v.driven_ok or v.driven_run == d.run_id
            if d.pending is not None:
                self._bind_driven(v, d, d.pending)
            return
        if v.driven is d:
            v.driven = None
        voiced = self._voice is v and v.live is not None and d.run_id not in self._stopped
        if voiced and mouth(v.speaker):
            self._live_call(v, "say", CANT_REACH)
        elif voiced:
            self._live_call(v, "append_commentary", CANT_REACH, delegation_id=d.pending)
        if row is not None and voiced:
            self._requests.fail(row)  # its voice just said CANT_REACH: no news item says it again
        elif row is not None and d.run_id not in self._stopped:
            self._request_failed([row], d.run_id)
        if row is not None:
            self._start_queued()  # the agent's next row
            self._sync_state()

    def _bind_driven(self, v: _Voice, d: _Driven, delegation_id: str) -> None:
        self._runs.bind(d.run_id, delegation_id, v.gen)
        if v.driven is d:
            v.driven = None
        emit_metric("live.driven.bound", 1, "Count")

    def _stop(self, agent_id: str, mark: int | None = None) -> None:
        """The only door that stops a voice run: an explicit stop to that agent. Interrupts its voice if it
        holds the floor and aborts every running run of that agent (and no one else's). `mark`: the stop's line
        (default: the newest); a line said at or before it, even one still at /route, starts nothing for it."""
        if v := self._floor_voice(agent_id):
            v.driven = None
            v.answer = None  # a stop said over its answer: neither said again nor posted (§4.8)
            v.saying, v.told = None, []
            v.last_activity = self._clock()
            self._live_call(v, "interrupt")
        aborted = [run.run_id for run in self._runs.running(agent_id)]
        for run_id in aborted:
            self._runs.abort(run_id)  # silent from here on, whatever the backend says
            self._stopped.add(run_id)
        mark = self._transcript.last_mark if mark is None else mark
        self._requests.cancel(agent_id, mark)  # no run_ended follows an abort
        self._cut_off.pop(agent_id, None)  # a stop said over its cut announcement: not posted (§4.8)
        cancelled = {r.run_id for r in self._requests.items if r.agent == agent_id and r.status == "cancelled"}
        if news := [n for n in self._news.get(agent_id, []) if n[0] not in cancelled]:
            self._news[agent_id] = news
        else:
            self._news.pop(agent_id, None)  # a stopped request's answer is neither spoken nor posted
        for run_id in aborted:
            self._spawn(self._abort(run_id))
        self._start_queued()  # the bridge's own semaphore makes a fifth /turns wait for the aborted run
        self._sync_state()

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
        the worker never does; it may still be spoken. Said over a cut announcement (§4.8), it covers that."""
        # a meeting: several people may have talked over it at once, so anyone's unnamed "don't post that" covers it
        cut = [a for a, (_, who, _) in self._cut_off.items() if a == agent_id or agent_id is None and who == asker]
        if not cut and agent_id is None and self.setting == "meeting" and self._cut_off:
            cut = [max(self._cut_off, key=lambda a: self._cut_off[a][2])]  # "that": the newest one cut
        for a in cut:
            del self._cut_off[a]
        if cut:
            emit_metric("live.request.no_post", 1, "Count")
            return
        r = self._requests.no_post(agent_id)
        run_id = r.run_id if r else None
        if r is None:
            runs = self._runs.running(agent_id)
            # named: that agent's newest held answer; unnamed: only one the speaker asked for
            news = [
                n for aid, ns in self._news.items() for n in ns if (aid == agent_id if agent_id else n[4] == asker)
            ]
            run_id = runs[-1].run_id if runs else max(news, key=lambda n: n[3])[0] if news else None
            if run_id is None:
                return  # nothing of that agent's (or anyone's) is open: nothing is marked or counted
            self._requests.suppress(run_id)
        emit_metric("live.request.no_post", 1, "Count")
        if run_id:  # a request not started yet is marked when its /turns lands (_post_turn)
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
        delegation_id: str | None,
        steer_run_id: str | None = None,
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
                            steer_run_id,
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
                        if (
                            attempt == 0 and self._voice is v and v.live is not None
                        ):  # cold start ~1 min: say so once
                            self._live_call(
                                v,
                                "append_thinking",
                                STILL_WAKING,
                                delegation_id=delegation_id,
                            )
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

    # RunSink (frames.Runs): where a run's output goes (D §8). A delegation id never crosses sessions.
    def _same_session(self, run: Run) -> _Voice | None:
        v = self._floor_voice(run.agent_id)
        return v if v and run.gen == v.gen else None

    def progress(self, run: Run, text: str) -> None:
        # not to a mouth: it said each one aloud ("I'm still checking", 10-04 prod Meet); the tile shows it
        if (v := self._same_session(run)) and not mouth(v.speaker):
            self._live_call(
                v,
                "append_thinking",
                f"Working: {text}. Not done yet.",
                delegation_id=run.delegation_id,
            )
        self._sync_state()

    def say(self, run: Run, text: str, final: bool) -> None:
        if v := self._same_session(run):
            if v.saying != run.run_id and (v.answer is None or v.answer[0] != run.run_id):
                self._settle_answer(v)  # the answer before this one: on the record if it was cut (`_followed`)
                v.unheard = v.cut = v.replied = v.resaid = False
                v.began, v.said = self._clock(), []
            if v.box != (run.run_id, run.box):
                # narration before a tool is its own box: its cut, its reply, its words are not the answer's
                v.box, v.spoke = (run.run_id, run.box), 0
                v.unheard = v.cut = v.replied = False
            v.pending = True  # text handed over and not yet said: cleared when the voice next goes quiet
            v.saying = run.run_id
            if mouth(v.speaker):
                # A run speaks a sentence before each lookup, which in a meeting is an agent talking again and
                # again over people. Nothing is said as it streams; its answer is said when it ends.
                return
            self._live_call(
                v, "append_commentary", text, delegation_id=run.delegation_id
            )

    def ended(self, run: Run, answer: str) -> None:
        asked = self._requests.by_run(run.run_id)  # a request-list run (engine §3.5)
        if answer and (news := self._news.get(run.agent_id)):  # §4.8: the real answer replaces its failure line
            news[:] = [n for n in news if n[0] != run.run_id or n[2] != CANT_REACH]
        if v := self._same_session(run):
            if mouth(v.speaker) and answer:
                # the run's last text box (updates before a tool are gone from it), word for word; kept until
                # the voice reports it said (`on_agent_said`), so a cut leaves it for the hand
                self._live_call(v, "say", answer)
                if all(n[0] != run.run_id for n in v.told):
                    v.told.append((run.run_id, run.request, answer, self._clock(), run.speaker))
            if not run.spoken.strip():
                self._live_call(
                    v,
                    "append_thinking",
                    "That request finished without a spoken result.",
                    delegation_id=run.delegation_id,
                )
            elif answer and not mouth(v.speaker) and (v.pending or v.cut or v.unheard or not v.spoke):
                # not known heard yet. `spoke` is kept: sentences said before this end frame were its words.
                # Every part said and the voice quiet with nothing cut: heard, nothing is kept.
                v.answer = (run.run_id, run.request, answer, self._clock(), run.speaker)
            self._requests.delivered({run.run_id})
        elif v := self._floor_voice(run.agent_id):
            if answer and not mouth(v.speaker):
                text = f"Your earlier task on {run.request[:80]} finished: {answer}"
                self._live_call(v, "append_commentary", text, delegation_id=None)
            elif answer:
                # the answer itself, word for word; kept until its audio ends (`_talked_over`, hang-up)
                self._live_call(v, "say", answer)
                # an answer boundary: what it said of the earlier answers counts now, and the words of a
                # sentence cut by the boundary carry over, so the rest of it still completes it
                v.said = self._settle_told(v)
                v.told.append((run.run_id, run.request, answer, self._clock(), run.speaker))
            self._requests.delivered({run.run_id})
        elif (
            answer and self.setting == "meeting" and not asked and not (run.bound and run.speaker in self._people)
        ):  # nobody announces in a meeting, and nobody here asked for it: the room gets it now
            self._spawn(
                self._late_answer(run.agent_id, (run.run_id, run.request, answer))
            )
        elif answer:
            news = self._news.setdefault(run.agent_id, [])
            news.append((run.run_id, run.request, answer, self._clock(), run.speaker))
            self._requests.ready(run.run_id)
            if not asked and self.setting == "meeting":
                # A meeting run with no row (/route gave no `ops`) whose voice has closed: someone asked for it,
                # so it is said at the next pause like a requested answer, not sent to the record unsaid.
                self._rowless.add(run.run_id)
            while len(news) > NEWS_PER_AGENT:
                self._post_news(run.agent_id, news.pop(0))
        elif asked:
            self._requests.delivered({run.run_id})  # finished with nothing to say
        self._start_queued()  # understanding §4.5: the agent's next row starts now, not on the next tick
        self._sync_state()

    def failed(self, run: Run) -> None:
        rows = self._requests.by_run(run.run_id)
        if v := self._same_session(run):
            for r in rows:  # its own voice says CANT_REACH now: failed, with no news to say it again
                self._requests.fail(r)
            if mouth(v.speaker):
                self._live_call(v, "say", CANT_REACH)
            else:
                self._live_call(v, "append_commentary", CANT_REACH, delegation_id=run.delegation_id)
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
                self._tell_queued(payload.get("agentId"))
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
            (n, since, math.inf)
            for n, since in self._meet_speaking.values()
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
            return max((s, n) for n, s, e in spans if n in spoke and s < end and e > start)[1]
        near = [(s, n) for n, s, e in spans if s < end + SPEAKER_SLACK_S and e > start - SPEAKER_SLACK_S]
        if near:
            return max(near)[1]  # the one who began last
        return self._meet_roster[0] if len(self._meet_roster) == 1 else "Someone"

    def stt_stopped(self, identity: str) -> None:
        """That person's STT stream ended (restart or unpublish): its utterance start goes with it."""
        self._utter_from.pop(identity, None)
        self.on_speech(identity, False)

    def _voice_notice(self, p: dict) -> None:
        """C3 notices: tell the agent's open voice, then every person (C5)."""
        agent_id, v = p.get("agentId"), self._voice
        live = v if v and v.agent_id == agent_id and v.live is not None else None
        if p.get("kind") == "renamed":
            if v and v.agent_id == agent_id:
                v.naming = False
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

    def _tell_queued(self, agent_id: str | None) -> None:
        """A floor-holder run waiting on the room key's lock (a chat run holds it): say so once (D §8)."""
        v = self._floor_voice(agent_id)
        for run in self._runs.running(agent_id) if v else []:
            if run.queued and run.run_id not in self._told_queued:
                self._told_queued.add(run.run_id)
                d_id = run.delegation_id if run.gen == v.gen else None
                self._live_call(v, "append_thinking", QUEUED_BEHIND, delegation_id=d_id)

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
        self._expire_news(now)
        self._post_cut(now)
        for r in self._requests.items:
            # a run that lost its run_ended (Runs.running() dropped it as stale): its request failed
            run = self._runs.get(r.run_id) if r.status == "working" and r.run_id else None
            if run is not None and run not in self._runs.running():
                self._request_failed([r], r.run_id)
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
            self._unclaimed = None  # one route, one summon
            if not (self._voice or self._speaking):  # one chance: an open voice or a talker makes it stale
                _, identity, text, mark = u
                self._spawn(
                    self._route_and_summon(
                        identity, text, mark, humans=len(self._people), unclaimed=True
                    )
                )
        v, w = self._voice, self._window
        idle = MEETING_IDLE_SECONDS if meeting else ROOM_IDLE_SECONDS
        if v is None:
            if self._window is not None and not self._lock.locked():
                async with self._lock:
                    await self._end_window()  # kept for open requests; ends after the last (spec §3.3)
            if self._people and now - self._last_heard >= idle:
                await self._end_idle_room()
            elif meeting and self._queue and not self._speaking and now - self._last_heard >= MEET_QUIET_S:
                # the agents named with a meeting voice that has closed: the next one answers at a real pause
                agent_id, speaker, request, label = self._queue.pop(0)
                await self.summon(speaker, agent_id, request, path="switch", label=label)
            else:
                await self._proactive(now)
            return
        if v.live is None:
            return
        if (
            mouth(v.speaker)
            and not (v.announce or v.announcing)  # an announcement is cut at once, in `_talking_changed`
            and v.agent_state == "speaking"
            and self._speaking  # anyone, its asker included: it hears nobody, so nobody stops it but the worker
            and now - self._talk_from >= TALK_OVER_S
        ):
            self._talked_over(v)
            return
        if not mouth(v.speaker):  # a voice that hears: its own speaker's sound cuts it
            self._cut_answer(v, now)
        self._flush_lines(v)
        self._acknowledge(v, now)
        d = v.driven
        if (
            d is not None
            and d.posted
            and not d.held
            and now - d.started_at >= HOLDING_LINE_S
            and not mouth(v.speaker)  # a mouth: no "still checking" over people (10-05 prod Meet)
            and any(r.run_id == d.run_id for r in self._runs.running(v.agent_id))
        ):
            d.held = True
            emit_metric("live.holding_line", 1, "Count")
            self._live_call(v, "append_thinking", HOLDING_LINE, delegation_id=None)
        if self._requests.version != v.list_version and now - v.last_thinking >= THINKING_EVERY:
            v.list_version = self._requests.version
            if lines := self._requests.lines(self._agent_name, skip_agent=v.agent_id):
                v.last_thinking = now
                self._live_call(v, "append_thinking", REQUESTS_NOTE + "\n".join(lines))
        if v.handoff and now >= v.handoff[1]:
            self._end_handoff(v)
        if (
            v.path == "ack"
            and v.heard_at is None  # its acknowledgement's audio is out
            and v.agent_state not in ("speaking", "thinking")
            and self._settled(v, now)
            and self._news.get(v.agent_id)
        ):
            # Its own answers that came in while it opened: handed over as ended() does for the floor-holder.
            for n in self._news[v.agent_id]:
                if mouth(v.speaker):
                    self._live_call(v, "say", n[2])
                    continue
                text = self._cant_reach_seed(n) or f"Your earlier task on {n[1][:80]} finished: {n[2]}"
                self._live_call(v, "append_commentary", text, delegation_id=None)
            if mouth(v.speaker):  # being said: kept until their audio ends (`_talked_over`, hang-up)
                v.told += self._news[v.agent_id]
            self._consume_news(v.agent_id)
            self._sync_state()
            return
        if (
            v.agent_state not in ("speaking", "thinking")
            # quiet, not settled: a teammate's ready answer does not wait for the floor agent's own run to end
            # (10-04 prod test: two answers sat unheard for two minutes). That run goes on without its voice
            # and its answer is said in its turn.
            and self._quiet(v, now)
            # never in the middle of an answer the floor voice is saying sentence by sentence: the rest would
            # be announced later with the part already said (`spoken` is reset when the run goes back to a
            # tool; a run whose end never comes blocks until it is stale, as `_settled` always did)
            and not any(r.run_id == v.driven_run and r.spoken for r in self._runs.running(v.agent_id))
            and not v.posting
            and v.announce is None
            and not self._lock.locked()
            and (ready := self._ready_delivery(now, skip_agent=v.agent_id))
        ):
            await self._deliver(*ready)  # engine §3.5: the oldest ready answer takes the floor
            return
        if self._queue and self._settled(v, now):
            agent_id, speaker, request, label = self._queue.pop(0)
            # its asker's label, not the latest speaker's: the meeting has talked on since the line was said
            await self.summon(speaker, agent_id, request, path="switch", label=label)
            return
        busy = (
            v.posting or self._runs.running(v.agent_id) or v.agent_state == "speaking"
        )
        # The budget is spent in summed GPT-Live seconds, not wall clock (D §4).
        over = w is not None and (
            now >= w.deadline or w.used_seconds + v.live.usage_seconds() >= w.budget
        )
        # A meeting's voice closes once it has said its piece: a working run does not hold it (its answer is
        # a ready row, said by a voice opened for it), a follow-up being said does, up to 3 windows.
        said = (
            in_meet(v.speaker)
            and v.agent_state not in ("speaking", "thinking")
            and not v.posting
            and v.announce is None
            and now - v.followed_at >= MEET_FOLLOWUP_S
            and (not self._speaking or now - v.followed_at >= 3 * MEET_FOLLOWUP_S)
        )
        if over or said or (now - v.last_activity >= IDLE_SECONDS and not busy):
            await self.end_exchange(only=v, keep_queue=said and not over)

    def _answer_message(self, v: _Voice, text: str, interrupted: bool) -> None:
        """A Live voice's message while it says (or has just been handed) a run's answer."""
        run = self._runs.get(v.saying or "")
        ref = v.answer[2] if v.answer else run.text if run else ""
        if v.answer or run:
            v.spoke += 1
        if interrupted:
            v.unheard = v.cut = True
        elif ref and _words_in(text, ref, 0.5):
            # A part of the answer, said to its end. Sound that came before its first word did not stop it
            # after all. A part that was interrupted stays missing, whatever plays afterwards. A long answer
            # comes as several messages and a later one may still be cut, so the answer itself stays until
            # the voice has gone quiet with none of them cut (`_cut_answer`).
            v.unheard = v.cut
        elif v.unheard:
            v.replied = True  # it answered who cut it: they were talking to it

    def _keep_answer(self, v: _Voice) -> None:
        """Its cut answer goes on the record like a cut announcement (§4.8): the room gets it, a meeting keeps
        it unsaid, unless its cutter calls it off."""
        emit_metric("live.answer.cut", 1, "Count")
        held = self._cut_off.get(v.agent_id, ([], "", 0.0))[0]
        self._cut_off[v.agent_id] = (held + [v.answer], v.speaker, self._clock())
        v.answer = None

    def _followed(self, v: _Voice) -> bool:
        """What its speaker said went on from the pending answer's request (a correction, a follow-up: /route's
        `follow`). That request replaces the answer from the moment it is accepted: the stale answer is neither
        said again nor posted beside the new one, also when the follow-up then fails or never speaks."""
        if v.answer is None:
            return False
        # after the answer's own request was made (a rowless run: since its voice began to say it): the
        # follow-up may come while that run is still working, or cut a streamed answer before its end frame
        own = self._requests.by_run(v.answer[0])
        since = min(r.created_at for r in own) if own else v.began
        asker = own[0].asker if own else v.answer[4]
        mine = [r for r in self._requests.items if r.agent == v.agent_id and r.run_id != v.answer[0]]
        # ponytail: /route names no amended request id, so a follow-up counts when its asker is this answer's and
        # no other request of the agent came between; carry the id through `ops` if this misfires
        return any(
            r.by == "follow"
            and r.created_at >= since
            and r.asker == asker
            and not any(o.by != "follow" and since <= o.created_at < r.created_at for o in mine)
            for r in mine
        )

    def _settle_answer(self, v: _Voice) -> None:
        """Its voice moves on (a new answer, a close): a cut answer is kept, a heard one forgotten. Said again
        and not yet known heard counts as cut: the second saying may never have reached anyone."""
        if v.answer and (v.unheard or v.resaid) and not self._followed(v):
            self._keep_answer(v)
        v.answer = None

    def _cut_answer(self, v: _Voice, now: float) -> None:
        """A Live voice's answer that its own speaker's sound cut. The model stops for them by itself, so the
        worker never cut it and nothing kept it (10-04 prod Meet: a noisy microphone's junk lines landed as
        Forge began, and the answer was gone). Once they are quiet and their words are handled: it made no reply,
        so the sound was not for it and it says the answer once more; it replied, or was cut a second time, and
        the answer goes on the record. Never lost, never said a third time."""
        n = v.answer
        if n is not None and self._followed(v):
            v.answer = n = None
        if n is None or v.agent_state in ("speaking", "thinking") or not self._quiet(v, now):
            return
        if not v.unheard:
            if now - n[3] >= CUT_WAIT_S:
                v.answer = None  # nothing cut it: said, maybe in its own words
            return
        who = v.speaker
        if (
            who in self._speaking
            or now - self._final_at.get(who, -math.inf) < CUT_HANDLED_S
            or self._understanding.get(who)
            or who in self._held
            or self._runs.running(v.agent_id)
        ):
            return  # their words are still coming, at /route, or already its next request: a stop may be in them
        if v.replied or v.resaid:
            self._keep_answer(v)
        elif (
            now - n[3] >= CUT_HANDLED_S
            and now - self._last_heard >= (MEET_QUIET_S if self.setting == "meeting" else REQUEST_QUIET_S)
        ):
            v.resaid, v.unheard, v.cut, v.spoke = True, False, False, 0
            v.answer = (*n[:3], now, n[4])
            emit_metric("live.answer.resaid", 1, "Count")
            self._note(f"[{self._agent_name(v.agent_id)} says its cut answer again]")
            self._live_call(v, "append_commentary", RESAY.format(answer=n[2]), delegation_id=None)

    def _talked_over(self, v: _Voice) -> None:
        """A meeting voice hears nobody, so the worker stops it for someone who keeps talking over it, as the
        model's own ear does in a Live call. What it was saying is not lost: a run still working ends as a ready
        answer, said whole at the next pause; an answer already handed to it goes on the record unless the
        talker calls it off, as a cut announcement does (§4.8)."""
        emit_metric("live.meet.talked_over", 1, "Count")
        self._live_call(v, "interrupt")
        self._keep_unsaid(v)
        self._drop(v, keep_queue=True)  # closed: a run still working no longer speaks through it

    def _control(self, text: str) -> bool:
        """A stop, "everyone stop" or "don't post that": applied the moment it is heard, never held."""
        agents = self._team["agents"]
        return stop_all(text) or stop_request(text, agents) or no_post_command(text, agents)[0]

    def _settle_told(self, v: _Voice) -> list[str]:
        """An answer boundary while the voice is still talking: what it has said so far is applied to the
        answers handed to it, so a later turn-away does not raise a hand for words already heard (Codex on
        #3800: a seeded news said to its end came back after a later answer's reset). Returns the heard words
        no sentence accounted for -- a sentence cut by the boundary -- as the said list to go on with, so its
        other half completes it (Codex on #3803)."""
        if not v.told:
            return []  # an earlier Live-call answer's words count for nothing after the boundary
        v.told.sort(key=lambda n: (self._requests.order(n[0]), n[3]))
        bags = _said_bags(" ".join(v.said))
        kept, carry = [], []  # the carry is what each cut sentence spent, from the bags: a word an earlier
        for n in v.told:  # answer used is not carried (Codex on #3803: "Build passed." then "Build failed.")
            rest, fragment = _unsaid_with(n[2], bags)
            if rest:
                kept.append((n[0], n[1], rest, *n[3:]))
                carry += fragment
        v.told = kept
        return [" ".join(carry)] if carry else []

    def _keep_unsaid(self, v: _Voice) -> None:
        """What a meeting voice was saying when it was stopped short (talked over, or closed for another agent
        someone named) goes on the record like a cut announcement (§4.8)."""
        unsaid = list(v.told)  # earlier answers handed to it and not yet said to their end
        run = self._runs.get(v.saying or "")
        if (
            run is not None and run.ended and not (run.aborted or run.failed) and run.text.strip()
            and all(n[0] != run.run_id for n in unsaid)
        ):
            unsaid.append((run.run_id, run.request, run.text.strip(), self._clock(), run.speaker))
        if unsaid and in_meet(v.speaker):
            # a meeting: nothing is lost and nothing is posted yet. Its hand goes back up with what was not heard,
            # from the sentence it was cut on, said at the next pause or when someone turns to it (owner 10-06/07).
            # spent in the seed's delivery order (Codex on #3800): shared wording is not heard twice
            unsaid.sort(key=lambda n: (self._requests.order(n[0]), n[3]))
            bags = _said_bags(" ".join(v.said))
            unsaid = [(n[0], n[1], rest, *n[3:]) for n in unsaid if (rest := _unsaid_with(n[2], bags)[0])]
            have = {n[0] for n in self._news.get(v.agent_id, [])}
            self._news.setdefault(v.agent_id, []).extend(n for n in unsaid if n[0] not in have)
            self._rowless.update(n[0] for n in unsaid)
            self._delivery_tries = {k: t for k, t in self._delivery_tries.items() if k not in {n[0] for n in unsaid}}
            emit_metric("live.meet.hand_back_up", 1, "Count")
        elif unsaid:
            held = self._cut_off.get(v.agent_id, ([], "", 0.0))[0]
            # who talked over it (a per-person Meet: maybe not its asker): their "don't post that" covers it
            talker = v.speaker if v.speaker in self._speaking or not self._speaking else min(self._speaking)
            self._cut_off[v.agent_id] = (held + unsaid, talker, self._clock())
        v.saying, v.told = None, []

    def _apply_held(self, now: float) -> None:
        """understanding §4.4: a held line whose speaker said nothing more for HOLD_APPLY_S is applied as /route
        answered it (never thrown away), once: a later final routes on its own."""
        for identity, (text, at, mark, r, kw) in list(self._held.items()):
            if now - self._final_at.get(identity, at) < HOLD_APPLY_S:
                continue
            del self._held[identity]
            if (u := self._unbound.get(identity)) and u[0] >= at and _words_in(text, u[1], 1):
                continue  # its voice delegated it since, and that transcript was understood (§4.6)
            emit_metric("live.hold.applied", 1, "Count")
            if kw["floor_line"]:  # a delegation of it matches its record
                self._understand_floor(identity, text, mark, routed=r, stands=True, **kw)
            else:  # it stands whoever spoke since (review B5-3): nobody else's line re-makes its asks
                self._spawn(self._route_and_summon(identity, text, mark, routed=r, stands=True, **kw))

    def _acknowledge(self, v: _Voice, now: float) -> None:
        """understanding §4.6: once per turn of speech, after ACK_QUIET_S of quiet from the speaker and with nothing
        of theirs still at /route, who is on what. A lone working row of the floor agent needs none ("On it")."""
        sp = v.speaker
        if not v.ack_rows or sp in self._speaking or self._understanding.get(sp):
            return
        if now - self._final_at.get(sp, -math.inf) < ACK_QUIET_S:
            return
        rows, v.ack_rows = [r for r in v.ack_rows if r.status in OPEN and not r.acked], []
        if len(rows) >= 2 or any(r.agent != v.agent_id or r.status == "queued" for r in rows):
            if self._live_call(v, "append_thinking", self._ack(rows, v.agent_id, said=True)):
                self._live_call(v, "nudge")

    async def _proactive(self, now: float) -> None:
        """No voice is open: an answer someone asked for goes first once the room is quiet (engine §3.5);
        otherwise the oldest news is announced by its own agent once the room is quiet (D §8)."""
        if not self._people or self._speaking or self._lock.locked():
            return
        if ready := self._ready_delivery(now):
            await self._deliver(*ready)
            return
        if self.setting == "meeting":
            return
        if now - self._last_heard < gate.ANNOUNCE_QUIET or not self._rate.ok(now):
            return
        items = [
            (n, aid)
            for aid, news in self._news.items()
            if self._agent(aid)
            for n in news
            if not self._requests.by_run(n[0])  # a requested answer goes through _ready_delivery only
        ]
        if not items:
            return
        item, agent_id = min(items, key=lambda x: x[0][3])
        self._rate.record(
            now
        )  # on the attempt: a refused summon does not retry every tick
        await self._deliver(item, agent_id)

    def _quiet(self, v: _Voice, now: float) -> bool:
        """The floor-holder is not speaking and QUEUE_SETTLE has passed since it last did anything."""
        return v.agent_state != "speaking" and now - v.last_activity >= QUEUE_SETTLE

    def _settled(self, v: _Voice, now: float) -> bool:
        """The floor-holder answered: it is quiet and its driven run ended."""
        return self._quiet(v, now) and not any(r.run_id == v.driven_run for r in self._runs.running(v.agent_id))

    def _expire_news(self, now: float) -> None:
        if self.setting == "meeting":
            for agent_id, news in list(self._news.items()):
                log = self._said_log.get(agent_id, [])
                done = []
                for i, n in enumerate(news):  # only words said since this hand went up count (an earlier answer's do not)
                    said = " ".join(t for at, t in log if at >= n[3] and now - at <= SAID_LOG_S)
                    if not said:
                        continue
                    rest = unsaid_part(n[2], said)
                    if not rest:
                        done.append(n)
                    elif rest != n[2]:  # its first sentences were heard (the cut message lands after the hand went up)
                        news[i] = (n[0], n[1], rest, *n[3:])
                if done:
                    news[:] = [n for n in news if n not in done]
                    self._requests.delivered({n[0] for n in done})
                    emit_metric("live.meet.hand_said_meanwhile", len(done), "Count")
        for agent_id, news in list(self._news.items()):
            kept = [
                n
                for n in news
                if now - n[3] < NEWS_TTL and not self._asker_gone(n, now) and not self._tried_out(n, now)
            ]
            for n in news:
                if n not in kept:
                    self._post_news(agent_id, n)
            if kept:
                self._news[agent_id] = kept
            else:
                del self._news[agent_id]

    def _post_cut(self, now: float) -> None:
        """Cut announcements (§4.8), once the words that cut them were handled. Only speech cuts one, so those
        words are coming: a final lands seconds after the speech ends ("never mind the calendar" was posted over
        1 s after it was said, 10-04 prod test). No words CUT_WAIT_S after they stopped talking: posted all the same."""
        for agent_id, (items, who, at) in list(self._cut_off.items()):
            if who in self._speaking:
                # still talking: a call-off may be in the words to come, and their final lands after the speech ends
                self._cut_off[agent_id] = (items, who, now)
                continue
            heard = self._final_at.get(who, -math.inf)
            handled = heard >= at and now - heard >= CUT_HANDLED_S  # its /route answered and was applied
            # a line of theirs held as unfinished is not handled yet: its continuation may call the answer off
            if (handled or now - at >= CUT_WAIT_S) and not self._understanding.get(who) and who not in self._held:
                del self._cut_off[agent_id]
                for n in items:
                    self._spawn(self._late_answer(agent_id, n))

    def _post_news(self, agent_id: str, n: tuple) -> None:
        """News nobody will hear here (expired, pushed out, or the huddle is ending) goes to the room as
        the agent's own message -- never dropped. 10-02: Forge's answer landed 50 s after hang-up and
        went nowhere; Pulse's board summary never surfaced."""
        emit_metric("live.announce.expired", 1, "Count")
        self._spawn(self._late_answer(agent_id, n))

    def _consume_news(self, agent_id: str) -> None:
        """Its agent said (or was handed) all its news: requested ones are delivered."""
        ids = {n[0] for n in self._news.pop(agent_id, None) or []}
        self._requests.delivered(ids)
        self._rowless -= ids
        for run_id in ids:
            self._delivery_tries.pop(run_id, None)

    def _tried_out(self, n: tuple, now: float) -> bool:
        """A requested delivery whose voice failed to open DELIVERY_TRIES times goes to the room instead."""
        tries, at = self._delivery_tries.get(n[0], (0, 0.0))
        v = self._voice
        return (
            tries >= DELIVERY_TRIES
            and now - at >= gate.ANNOUNCE_QUIET
            and not (v and v.announce is n)  # its voice is open and about to say it
        )

    def _cant_reach_seed(self, n: tuple) -> str | None:
        if not any(r.status == "failed" for r in self._requests.by_run(n[0])):
            return None
        return f'You could not do "{n[1][:80]}". Say exactly this, then stop: {n[2]}'

    def _ready_delivery(self, now: float, skip_agent: str | None = None) -> tuple[tuple, str] | None:
        """The oldest ready answer someone asked for (ties: request order) and its agent, once no human is
        speaking and none has spoken for REQUEST_QUIET_S. A requested answer is not news: no rate limit."""
        if self._speaking or now - self._last_heard < (MEET_QUIET_S if self.setting == "meeting" else REQUEST_QUIET_S):
            return None
        items = [
            (n, aid)
            for aid, news in self._news.items()
            if aid != skip_agent and self._agent(aid)
            for n in news
            if (n[0] in self._rowless or any(r.status in ("ready", "failed") for r in self._requests.by_run(n[0])))
            and now - self._delivery_tries.get(n[0], (0, -math.inf))[1] >= gate.ANNOUNCE_QUIET
        ]
        return min(items, key=lambda x: (x[0][3], self._requests.order(x[0][0])), default=None)

    async def _deliver(self, item: tuple, agent_id: str) -> None:
        """Its own agent says it, pinned to who asked (else the last speaker, else anyone here). Each
        attempt is counted: a refused open waits gate.ANNOUNCE_QUIET, and after DELIVERY_TRIES the room
        gets it (_expire_news), so a backend outage cannot open a session every tick."""
        if self._requests.by_run(item[0]) or item[0] in self._rowless:  # unrequested news is paced by the rate limit
            tries = self._delivery_tries.get(item[0], (0, 0.0))[0]
            self._delivery_tries[item[0]] = (tries + 1, self._clock())
        speaker = next((p for p in (item[4], self._last_speaker) if p in self._people), None) or min(self._people)
        self._note(f"[announce {self._agent_name(agent_id)} opens its voice for: {item[1][:80]}]")
        rows = self._requests.by_run(item[0])  # a meeting: who asked may follow up on the answer
        await self.summon(
            speaker, agent_id, request=None, path="announce", announce=item, label=rows[0].label if rows else ""
        )

    def _asker_gone(self, n: tuple, now: float) -> bool:
        return bool(self._requests.by_run(n[0])) and n[4] not in self._people and now - n[3] >= ASKER_GONE_S

    async def _late_answer(self, agent_id: str, n: tuple) -> None:
        # every caller already took it out of _news: its row closes here, posted or not (failed stays failed)
        self._requests.delivered({n[0]})
        self._delivery_tries.pop(n[0], None)
        if not self._requests.postable(n[0]):
            emit_metric("live.request.post_skipped", 1, "Count")  # "don't post that", or a cancelled request
            return
        try:  # a CANT_REACH line nobody heard is posted as one line (understanding §4.8)
            await self._backend.late_answer(
                agent_id, n[0], n[1][:LATE_REQUEST_CHARS], FAILED_POST if n[2] == CANT_REACH else n[2]
            )
            emit_metric("live.news.posted", 1, "Count")
        except Exception:
            logger.info(
                "huddle late answer post failed agent=%s run=%s",
                agent_id,
                n[0],
                exc_info=True,
            )

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
        for a in self._team["agents"]:
            aid = a["id"]
            if v and v.agent_id == aid:
                status = (
                    "joining"
                    if v.live is None
                    else "speaking"
                    if v.agent_state == "speaking"
                    else "listening"  # C4: the floor-holder is never "working"; QUEUED_BEHIND says it aloud
                )
            elif self._runs.running(aid) or self._requests.working(aid):
                status = "working"
            elif self._news.get(aid):
                status = "has_news"
            else:
                status = "idle"
            state[f"lightbulb.agent.{aid}.status"] = status
            state[f"lightbulb.agent.{aid}.working"] = self._requests.task(aid) or self._runs.working(aid)
            # what a raised hand is about: the oldest answer it has ready (the Meet tile and the Live bar show it)
            state[f"lightbulb.agent.{aid}.hand"] = (
                self._news[aid][0][1][:WORKING_CHARS] if status == "has_news" else ""
            )
            state[f"lightbulb.agent.{aid}.naming"] = (
                "1" if v and v.agent_id == aid and v.naming else ""
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

    def _flush_lines(self, v: _Voice) -> None:
        now = self._clock()
        if v.live is not None and v.lines and now - v.last_thinking >= THINKING_EVERY:
            said = "\n".join(v.lines)
            self._live_call(v, "append_thinking", said)
            v.lines.clear()
            v.last_thinking = now
            if run := (self._runs.running(v.agent_id) or [None])[-1]:
                self._spawn(self._steer_late(run, run.speaker or v.speaker, said))

    async def _steer_late(self, run: Run, speaker: str, said: str) -> None:
        """Routing v2 §4.3: other people's lines reach the floor-holder's working run as a steer, so Hermes
        can see the request was settled meanwhile. steerOnly: a run that already ended starts nothing."""
        if self._window is None:
            return
        try:
            r = await self._backend.turn(
                self._window.call_id,
                speaker,
                run.agent_id,
                str(uuid.uuid4()),
                (LATE_LINES + said)[:MAX_UTTERANCE],
                "",
                steer_run_id=run.run_id,
                steer_only=True,
                speaker_name=self._said_by.get(speaker),
            )
        except BackendError as e:
            logger.info("huddle late-line steer failed: %s", e.code)
            return
        if r.get("steered"):
            emit_metric("live.steer.late_line", 1, "Count")

    async def _end_idle_room(self) -> None:
        """Nobody has spoken for ROOM_IDLE_SECONDS (a meeting: MEETING_IDLE_SECONDS): tell everyone why,
        clear the marker, leave. A meeting first posts its whole transcript and takes the bot out of the Meet."""
        logger.info("huddle idle: leaving")
        self._room.publish(
            ERROR_TOPIC, {"code": IDLE_ENDED, "reason": None}, sorted(self._people)
        )
        self._idle_ended = True
        if self.setting == "meeting":
            await self._last_posts()  # as at hang-up: the unheard answers, then the transcript
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

    async def _close_voice(self) -> None:
        """Caller holds the lock."""
        v, self._voice = self._voice, None
        self._sync_state()
        await self._close_live(v)

    async def _close_live(self, v: _Voice | None) -> None:
        """Close a voice that no longer holds the floor; its GPT-Live seconds go to the window."""
        if v is not None:
            self._settle_answer(v)  # cut and never said again: on the record
            if in_meet(v.speaker):
                # closed with an answer handed to it and not said to its end, also before its first word
                # (someone named another agent): never lost
                self._keep_unsaid(v)
        if v is None or v.live is None:
            return
        if (
            v.wake_dims is not None
            and not v.used
            and not (v.driven_ok and v.driven_run not in self._stopped)
        ):
            # woken by name, then nothing: no delegation, no (unstopped) driven run, the speaker never spoke
            emit_metric("live.wake.unused", 1, "Count", v.wake_dims)
        usage = 0.0
        try:
            v.live.mute()
            await v.live.aclose()  # GPT-Live bills per session second
            usage = (
                v.live.usage_seconds()
            )  # after close: the final usage delta lands during it
        except Exception:
            logger.exception("huddle live session close failed")
        usage = usage or (  # a voice with no opened time bills nothing, never clock - 0
            max(0.0, min(self._clock(), self._window.deadline if self._window else math.inf) - v.opened)
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
