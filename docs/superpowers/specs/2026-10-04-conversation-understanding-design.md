# Conversation understanding: every line heard by code, every request tracked to its end

Status: design, 2026-10-04. Builds on the conversation engine (phase 1: #3663, #3665, #3666, #3667),
which handles one spoken line that names two or more agents. This spec makes that the general case:
any line, named or not, from the person who has the floor or from anyone else.

## 1. Goal

A person talks to the team the way they talk to people. They can greet, think aloud, ask one thing,
ask four things in one breath, correct themselves, and ask a follow-up. The system must:

1. Know what was asked. One row per request, in the speaker's own words.
2. Give each request to the teammate who should do it, and say who has it.
3. Answer from a check made now, never from memory.
4. Bring every request to an end the speaker can see: answered aloud, posted in the room, said to
   have failed, or cancelled by the speaker. Nothing ends in silence.
5. Never start work for small talk.

## 2. What happened (founder's Live test, 2026-10-04 05:44Z)

| # | Said | What the system did | Cause in code |
|---|---|---|---|
| 1 | "Hey guys, how's it going?" | Pulse's voice answered. A backend run on the same line answered again 26 s later ("Good to be back with everyone"). | With no voice open, `/route` picks an agent and `summon(request=text)` always starts a driven run (`huddle.py` `_open`). Nothing asks "is this a request?". |
| 2 | "I got a couple questions. I'm just gonna say them all … I need to know when the update from the application that we made for the money is gonna be in." | No run. No answer. | The speaker had the floor. For the floor speaker the worker does nothing with the final line (`_dispatch`, last branch); only the realtime voice model hears it, and it did not delegate. |
| 3 | "I need you to check Aiden's email … I also want to know what features we've shipped and what PRs are currently open." | One delegation, one run for Pulse with all three asks. | A GPT-Live delegation carries the whole pending transcript and no task text (`GPTLiveDelegation`). The voice cannot split. |
| 4 | (same) | Only the PRs were answered. Email and features were never mentioned again. | One run, `max_iterations: 4`, and `TURN_NOTE` says "speed matters more than completeness". No row existed per ask, so nothing knew two were missing. |
| 5 | (the PR answer) | Stale. "Seven open PRs; the active one is #3638." Wrong on both. | `TURN_NOTE`: "Answer now: if you can answer from what you already know". The run answered from memory in 5 s. |
| 6 | (the PR answer) | "Forge found seven open PRs." Forge did nothing. | The voice model invented the source. No note told it who did the work. |
| 7 | | "[sigh]", two holding lines in a row. | No rule in the persona against either. |

One root cause sits under 2, 3 and 4: **the only thing that hears the floor speaker is the realtime
voice model, and it decides what is a request.** It can only pass a blob, and it may pass nothing.

## 3. What the research says (notes in `.research/`, untracked)

- Splitting a multi-request utterance is the easy part (about 98.8% in DialogUSR). The hard part is a
  later request that leaves out context an earlier one gave. Split first, then give each request its
  context. Accuracy drops with each added intent when a model handles them together, so handle one
  request at a time.
- Goal tracking by a language model across several people is weak (62% in Addlesee et al. 2023). The
  list of open requests must be explicit state that code owns. The classifier proposes moves; code
  applies them (information-state update, Larsson and Traum).
- A request creates an obligation that must be discharged (Traum and Allen). "Nothing silently
  dropped" is a closed set of end states on a table, not a prompt.
- OpenAI's GPT-Live delegation guide: "Live speech and delegated work continue independently. A
  completed backend response does not itself mean the user heard the answer." And: "Delegate before
  giving an answer that depends on backend work. Do not guess the result while waiting."
- Voice agents finish 31 to 51% of tasks that the same model finishes 85% of in text (τ-Voice). Its
  named failure "multi-step amnesia" is our rows 3 and 4. Prompt changes alone gain at most about 15%
  (MAST). The fix is structure.
- Spoken lists hold two to five items (Alexa), best three (Google). Confirm implicitly. One short
  line before work starts, never the same line twice, no sound effects (OpenAI realtime guide).
- Reliability is measured by repeating each scenario and grading the final state, not the wording
  (τ-bench pass^k).

Facts read from the code that bound the design:

- The GPT-Live session has no tools under client delegation, no turn-detection setting, and four
  levers: instructions, thinking ("know this"), commentary ("say this once"), and mute/interrupt.
- A second STT pipeline (`gpt-4o-mini-transcribe`, semantic VAD) already produces a final line for
  every speaker, including the floor speaker.
- Within one agent's room session the bridge runs one turn at a time. A follow-up steers the running
  turn. Four turns run at once per workspace.
- Connector credentials (Google, GitHub, AWS, X) are projected to the whole roster. Every agent has
  the same access, so access cannot pick the owner. Role and "what you do" can.

## 4. Design

### 4.1 One ear

**The worker's STT final is the source of every request, for every speaker.** Each final line that
is real speech and is not a stop command goes to the backend's understanding stage (`/route`, §4.2),
whether or not a voice is open and whoever holds the floor.

The realtime voice still hears the floor speaker. Its delegation is no longer a dispatch. It is a
handle the worker binds to a request row that the final line produced (§4.6).

### 4.2 Understanding a line (backend, one Jev call)

`route_live_line` gains an understanding step built on the phase-1 split (`messaging.address`).

1. **Cut.** The line is cut into parts at agent names and sentence ends (as phase 1), and now also
   where a new request begins inside a sentence. Speech-to-text often gives no punctuation, so the
   cut reads words: after a comma, a joiner (`and`, `also`, `then`, `plus`, `but`, `anyway`), a
   filler (`um`, `uh`, `like`, `I mean`) or an ordinal (`first`, `next`, `one more`), the next words
   must open a request: a question word, an asking verb with its object, or "I need / can you".
   "what features we've shipped | and what PRs are currently open" is two parts. "check the Stripe
   dashboard and the Mercury balance" is one. A step that leans on the one before ("export the list
   and then send it to Dana") is not cut. "can you" alone is never a part.
   At most 8 parts; a line that cuts into more is not split (one request, whole line).
2. **One Jev call** asks, beside today's `complete`, `anyone` and per-name labels:
   - per part, `kind`: one of
     - `request`: asks the AI team to find out, check, do, make or change something;
     - `context`: background or detail for the request next to it;
     - `more`: says requests are coming, or how the speaker wants to go through them;
     - `social`: greeting, thanks, acknowledgement, thinking aloud, talk about the call;
     - `people`: said to another person on the call;
     - `cancel`: calls off an earlier request.
   - per part, `who`: one choice over the agents' cards (name, role, what they do, tools), used
     only when no name in the line assigns the part.
   - per call, only when the speaker has open or just-answered requests: `amends` and `drops`, each
     one choice over those requests plus "none".
3. **Assemble (code).** A `context` part joins the request beside it (the one before, else the one
   after). Each `request` part is one request. Its task is the part in the speaker's words, with its
   context parts in spoken order. No model rewrites it.

The questions and thresholds are tuned on the benchmark (§5). If Jev fails or times out, the line is
one request, whole, for the named agent, else the floor agent, else today's single-responder
fallback. A Jev failure never makes a lone speaker's line vanish.

### 4.3 Who owns a request

In order:

1. **Name.** An agent the line addresses owns the parts the phase-1 rules give it. One addressed
   agent owns every request part in the line.
2. **Fit.** With no name: the `who` choice, when its probability and its margin over the next agent
   pass the fitted thresholds.
3. **Floor.** Else the agent holding the floor, when the speaker is the one it is talking with.
4. **Chief of staff.** Else the CEO agent.

A request that `amends` an earlier one goes to that request's owner whatever `who` says (`follow`).

**Several people, no name.** When two or more people are on a Live call and the line names nobody,
says no wake word and is not the floor conversation, the question "should an agent answer at all?"
is not the part typing's to answer. Chat's room judge (`jev._decide_for_message`: "would a good
teammate answer this?", tuned on the multiparty benchmark) answers it, as it did before this design.
It runs beside the understanding call. When it picks nobody, the line makes no request. When it
picks an agent, the line is understood like any other; a single request goes to the judge's agent
(it read the whole conversation, so a follow-up stays with the teammate already in the exchange),
several requests keep their own fit, and a line with no request part becomes one request for the
judge's agent (`room`).

Each request records which rule chose its owner (`by: name | fit | floor | room | chief | follow`).

### 4.4 The answer of `/route`

Today's fields stay (`agentId`, `agentIds`, `hold`, `requests`) for a worker that has not been
deployed yet, derived from the new result. The new worker reads `ops`:

```json
{"ops": {
  "requests": [{"agentId": "…", "task": "…", "by": "fit"}],
  "social": "agentId or null",
  "more": false,
  "cancel": ["requestId"],
  "steer": [{"requestId": "…", "text": "…"}]
}}
```

- `requests`: in spoken order. Several for one agent are allowed.
- `social`: nothing was asked and one agent may reply in a sentence (a lone speaker's greeting, or
  "go ahead" to an announced list). Null when people are talking to each other, or in a meeting.
- `more`: the speaker announced a list.
- `cancel`: a `cancel` part with a `drops` target.
- `steer`: a single request part whose `amends` target is still working. The text goes into that
  run. If the target is already answered, it is a new request for the same owner instead.

The worker sends its open and last three answered requests (`open`: `id`, `agentId`, `task`,
`status`) with each `/route` call, and `floor.mine: true` when the speaker is the person the floor
agent is talking with.

In a **meeting** (Google Meet) nobody is implied: `ops` holds requests only for a line that names an
agent, says the wake word, or is an unclaimed question, exactly as today. With **two or more people**
in a Live call the room judge decides (§4.3); the default is silence.
One exception covers both: when `floor.mine` is true the speaker is already in a conversation with
the team, so their request parts need no name (today the voice model hears these lines and treats
them the same way). A line that speaks to a person by name stays silent there too.

`hold: true` (an unfinished line) keeps today's meaning: the worker waits for the speaker's next
final and merges. A held line is never thrown away: if the speaker says nothing more for 4 s, the
worker applies the held line's `ops` as they are.

### 4.5 The ledger (worker, code owns it)

`RequestList` becomes the list of every request on the call, not only multi-name splits.

- One row per request: id, asker, owner agent, task, status, run id, `by`.
- Status: `queued → working → ready → delivered`, or `cancelled`, or `failed`. `delivered` means
  spoken to the end or posted in the room.
- **One row is one run is one answer.** An agent works on one row at a time; its next row starts
  when the run before ends. Different agents run together, four at most (as today).
- A new request never steers another request's run. Only `steer` (a correction) does.
- Every row reaches an end state before the call's window closes, or is handed to the room:
  an answer not yet spoken is posted ("From the Live call …", exists); a failure not yet spoken is
  posted in one line; a run still working at hang-up posts its own answer (exists in the bridge).

### 4.6 The voice

The realtime voice is the mouth. It grounds, chats, and reads results. It never decides what is a
request and never states a fact a note did not give it.

- **Small talk.** `social` starts no run. With no voice open, the `social` agent's voice opens with
  the line and "small talk; reply yourself; do not delegate". With a voice open, the model answers
  on its own, as today.
- **Named line, no voice open.** The named agent's voice opens at once, as today, while the line is
  understood in parallel. The seed is built from the result: its first request is its driven run
  (answers stream sentence by sentence, as today); other requests start for their owners.
- **Floor speaker's line.** Rows start as soon as `/route` answers. The floor agent's first new row
  starts as a driven run, with the note "already handed to your backend self; don't delegate it
  again" (the barge path does this today).
- **Delegation.** When the voice delegates, the worker binds the delegation to the floor agent's
  driven row from that speech. If the speech produced rows for other agents only, the delegation is
  answered with the acknowledgement note. If it produced no request, it is answered with "nothing
  to look up; reply yourself". If no final line was heard for that speech within 2 s, the
  delegation's own transcript is understood the same way, and the late final is not understood
  again. If `/route` is down, today's behaviour stands: one run, whole transcript.
- **Acknowledgement.** One note, after the speaker has been quiet for about a second, when a turn
  made two or more rows or a row for another agent: who is on what, at most three named aloud
  ("Forge has the open PRs and what shipped. I'm on the email and the application."). A single row
  for the floor agent needs no note; the model's own "On it" is the acknowledgement. While `more`
  is set and nothing has been asked yet, the note is "they will list several requests; say 'go
  ahead' at most".
- **Persona** (`voice_persona.py`) adds: say only what a note gives you; name a teammate as the
  source only when the note does; start a result with what it is about; one result at a time; never
  the same sentence twice; no sighs, laughs or other sounds; if someone says they have several
  things to ask, let them finish.

### 4.7 The backend turn

`TURN_NOTE` is rewritten:

- The request is one thing the person asked. Other requests from the same breath are with teammates
  or queued; answer only this one.
- "Anything that can change (email, pull requests, deploys, payments, dates, metrics, what shipped)
  must be checked with a tool in this turn before you answer. Your memory and earlier messages tell
  you where to look, never what the answer is. Say in a few words where the answer came from. If
  you cannot check it, say so; never give an old value as current."
- Four ways out: answer (two or three spoken sentences, starting with what it is about), ask one
  question, hand off as a Paperclip task (as today), or say plainly which access or tool is missing.
- "Speed matters more than completeness" is removed.

`CALL_RUN_LIMITS` (four model steps, low effort) is in the agent image and stays. One request per
run fits in it; a request that does not fit is handed off, as today.

### 4.8 Delivery

Mostly as phase 1: an answer waits for a quiet floor, the first ready goes first, the owning agent's
own voice says it, three failed tries post it. Changes:

- A result note for the floor agent names its request ("On the open PRs: …").
- An answer the asker cut off before it finished is posted in the room, so the full text exists.
- At the end of the call, failed rows that were never said are posted in one line.

### 4.9 What does not change

Chat routing. Meeting floor rules. Stop and "don't post that" commands. The floor mechanics (barge,
handoff, queue for bare summons, tap). Metering. The agent image.

## 5. Evaluation

### 5.1 Understanding benchmark (`scripts/speak_gate_bench/understand.py`)

Runs the real understanding stage against live Jev with stub cards. Each row is a scene: roster,
people, transcript so far, floor, open requests, and one or more final lines. It expects a set of
requests (phrases each task must and must not contain, and the allowed owners), plus `social`,
`more`, `cancel` and `steer` outcomes.

Case one is the founder's call, lines 0, 3, 5 and 8, verbatim. The other rows cover: one named
request; one unnamed request; batched requests across domains; a batch for one agent; one verb with
several objects that must not split; an announcement; indirect requests; requests across two finals;
a later request that leans on earlier context; a mid-line repair; a later correction; a
cancellation; a follow-up to an answer; background inside a request; backchannels; small talk;
small talk with a request; talk between people; another person's goal stated for them; a named and
an unnamed request in one line; garbled STT; meeting lines.

Reported per category and overall, over three runs:

| Metric | Gate |
|---|---|
| Request recall (each expected request is exactly one row) | ≥ 0.95 |
| Extra rows (a row nobody asked for) | ≤ 0.03 of rows |
| Small talk or people talk that starts work | ≤ 0.02 |
| Owner is an allowed owner | ≥ 0.90 |
| Scene passes on all three runs (pass^3) | ≥ 0.85 |
| Latency of the stage, p50 / p95 | reported; must not be worse than phase 1 by more than 150 ms p50 |

The two existing benches are regression gates: the phase-1 split bench stays at or above 0.95, and
the multiparty bench keeps its gate (cued recall ≥ 0.90, precision ≥ 0.95).

### 5.2 Worker replay tests

`test_replay_2026_10_04.py` replays the founder's call against the fake backend and fake voice and
asserts: no run for the greeting; four rows from lines 3 and 5; each row its own run; one
acknowledgement; four answers, one voice at a time; nothing left open at the end. Further tests
cover delegation before and after the final line, a delegation with no final, `/route` down, a
correction that steers, a cancel, an answer cut off, and hang-up with rows open.

### 5.3 In production

New metrics, by call: `live.request.created{by}`, `live.request.ended{status}`,
`live.request.open_at_end` (must be 0), `live.understand.kind{kind}`, `live.delegation.unbound`
(the safety net fired), `live.ack_ms`, `live.answer_ms`. The transcript log gains one line per
ledger change, so a call can be read back with its requests.

## 6. Out of scope

- A generative rewrite of a request. The run already receives what was said since the agent last
  spoke; the verbatim part plus that is the context.
- Splitting chat messages. Chat turns have the full step budget and one responder.
- Raising the voice run's step limit (needs an image build and a fleet roll). Revisit if
  `handed off` or step-limit endings are common.
- One shared "Lightbulb" voice for all agents.

## 7. Order of work

1. Benchmark and baseline of today's `/route`.
2. Backend: understanding stage and `ops`, persona, `TURN_NOTE`. Old workers keep working.
3. Worker: ledger for every request, every line understood, delegation binding, acknowledgement,
   delivery changes, metrics, replay tests.
4. Bench gates, whole-branch review, merge, promote the backend, then deploy the worker.
