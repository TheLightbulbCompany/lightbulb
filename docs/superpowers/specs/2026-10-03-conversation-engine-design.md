# Conversation engine: one spoken turn, several agents

Status: design, for owner review. Written against `main` at `d581ecac1`. Google Meet facts
come from PR #3638 (`fix/meetings-followups`, not on `main`) and are cited as
`#3638: path:line`. Every other citation is a file on this branch.

## Goal

A person, in Live or in a meeting, says one thing that names several agents. Then:

1. each named agent gets its own part of what was said,
2. all of them start at once,
3. within 1 to 2 s the person knows they were heard,
4. answers come out one voice at a time, each as soon as it is ready,
5. a call starts without a long wait.

Nothing else is in scope. Topic, decision and stance state is out of scope.

## Words

| word | meaning |
|---|---|
| worker | the LiveKit Agents job in `apps/huddle-agent`, one per call |
| turn | one STT final from one speaker |
| request | one ask of one agent: asker, agent, task text, time |
| request list | the requests of the current call, held in the worker's memory |
| voice | one agent's GPT-Live session |

## 1. What happened (2026-10-03, founder's demo channel)

The evidence below comes from prod logs, as reported to this spec. It was not re-read for it.

| event | what we saw | cause in code |
|---|---|---|
| Go live 16:05:09, worker presence 16:05:31 | 22 s to a ready worker | LiveKit Cloud cold start (§3.7). The profile warm-up (`voice_calls.prewake_owner`, called at `apps/backend/routers/huddles.py:159`) fired at 16:05:11-12 and was not the cause |
| "Pulse, an update on the go-to-market strategy. Forge, what our PRs look like. Friday, a summary of our rooms and the last conversation from the last day" | only Pulse was summoned | the worker matches names only in the opening list (`apps/huddle-agent/src/huddle_agent/transcript.py:250-266`). "Pulse" followed by "an" ends that list. Forge and Friday land only in `Address.anywhere` (`transcript.py:267-281`). The worker reads that field only while a voice is open and its own speaker talks (`huddle.py:638-649`) |
| Pulse said "I heard all that" and tried to answer all three | Pulse's run got the whole line | `summon(... request=text ...)` (`huddle.py:585-595`) and the driven run (`huddle.py:1093-1104`) |
| Forge came ~40 s later, Friday ~20 s after that, each only after a later utterance | no parallel runs | a later line named Forge and took the `anywhere` path to a floor switch (`huddle.py:638-649`, `:838-851`) |
| nobody said the others had heard | no acknowledgement | — |
| "A team voice call. Names: Pulse, Friday, Forge, …" three times as the person's line | the STT model repeated its prompt on silence or noise | the worker sets that prompt (`transcript.py:371-380`, `agent.py:347-349`). Every final goes into the transcript and to the router unfiltered (`huddle.py:522-537`, `:618-637`) |
| Friday said it would post in the room after being told not to | "don't post" means nothing to the worker, and the worker asks for that promise | the holding line tells the voice to say it "will post the answer in the room" (`huddle.py:75-79`). Unheard answers are posted by the worker (`huddle.py:1582-1599`) and by the bridge (`apps/infra/hermes/bridge/turn.py:371-374`). The only spoken commands are stop, never mind, cancel that and forget it (`transcript.py:325-345`) |

## 2. The pipeline facts behind the failure

- **One final, one agent.** A turn summons the first name it finds. The run starts with that
  agent's voice, and the whole turn is the task (`huddle.py:578-596`, `:1093-1104`).
- **The queue waits for the first run to finish.** Other named agents go into `_queue`, and
  their runs are not started. The queue advances only when the floor-holder's run has ended,
  the voice is silent, and 1 s has passed (`huddle.py:1526-1528`, `_settled` at
  `:1565-1569`). Any new summon replaces the queue (`huddle.py:1004-1006`). `end_exchange`
  clears it (`huddle.py:1700`).
- **The bridge already runs agents in parallel.** Runs are serialized per agent and room only.
  The microVM runs at most 4 turns at once (`MAX_CONCURRENT_TURNS`, `turn.py:2401-2412`).
- **Ghost lines are not filtered.** The STT prompt is `"A team voice call. Names: …"`
  (`transcript.py:371-380`). Nothing compares a final against it.
- **Late answers ignore "don't post".** The worker posts news nobody heard
  (`huddle.py:1582-1599`). The bridge posts a completed answer whose frames were not delivered
  (`turn.py:371-374`, `:390-398`). Neither has a way to be told not to.
- **Answers of agents without the floor wait as news.** News is spoken only when the room has
  been quiet for 3 s, and at most once per 90 s (`gate.py:7-9`, `huddle.py:1540-1563`).

## 3. Design

### 3.1 Ghost-line filter

At the top of `on_final_transcript` (`huddle.py:522`), the door every final passes, drop a
final when its normalized text (lower case, letters and spaces) equals the current STT prompt,
is 12 or more characters and a substring of it, or starts with `a team voice call`. The worker
already keeps the prompt in `self._vocabulary` (`huddle.py:263`, `:384-386`). Neither the
transcript nor the router sees a dropped line. Metric: `live.stt.ghost_dropped`.

### 3.2 One request per named agent

| the turn names | requests | model calls |
|---|---|---|
| no agent | today's router picks at most one agent (`voice_calls.py:862-904`). The task is the whole turn | as today |
| one agent | one request. The task is the whole turn. The worker's opening-name fast path stays | none |
| two or more | one request per agent that the addressing stage labels addressed or relayed | one Jev call |

The worker counts names with its own `address()`: the opening list plus `anywhere`. A turn
with two or more names goes to `/route`. `/route` already makes one Jev call that labels every
name (`messaging.py:2781-2819`), and the candidates already carry their position
(`messaging.py:2757-2763`). The split adds one more `choice` question per name to that same
call: "which part is the ask for this name". The choices are the turn cut at each name's
position, plus `all` (for "Forge and Friday, check X"). Jev answers only `boolean` and
`choice` questions (`jev.py:130-185`). So the task text is a segment the code cut, and no
model writes free text.

**Router model down:** each named agent gets its own segment, cut at the name positions. This
is the addressing stage's existing fallback shape (`messaging.py:2840-2846`).

### 3.3 All requests start at once

Each request posts its run (`/turns`) as soon as the request exists. It does not wait for a
voice. `/turns` needs only the metering window (`huddle.py:1298-1302`), so the worker opens it
(`_ensure_window`, `huddle.py:1157-1163`) on the call's first request. A second request to an
agent that is still working steers that agent's run, as today (`huddle.py:1209-1237`).

**Cap: 4 working requests per room.** The bridge runs at most 4 turns at once per microVM
(`turn.py:2411`). A fifth run would wait there anyway. Requests over the cap wait as `queued`
and start in order as slots free.

### 3.4 The request list (worker memory only)

Each entry has `agent`, `asker`, `task`, `createdAt`, `status`, `noPost`. The status is one of
`queued`, `working`, `ready`, `delivered`, `cancelled` or `failed`. The list lives only in the
worker for the length of the call. Nothing is stored in the backend. A worker restart loses
the list, and the owner accepts that.

The list travels on calls that already exist:

| carrier | added field |
|---|---|
| `POST /internal/huddles/{room}/route` request | `requests: [{agent, task, status}]`. It replaces `voiceRuns` (`huddle.py:795-798`), which Jev already reads as open lines (`voice_calls.py:798-807`) |
| `/route` response | `requests: [{agentId, task}]` when two or more agents are named |
| `POST /internal/huddles/calls/{callId}/turns` request | `task` (sent as `utterance`, as today) and `requests` (≤ 8 lines). The backend adds them to the run's input bracket (`voice_calls.py:681-682`), so Pulse's run reads "Forge is answering 'what our PRs look like'" |
| the floor voice | list lines appended as thinking, no more often than once every 3 s (`THINKING_EVERY`, `huddle.py:54`) |
| live strip | existing worker attributes (`huddle.py:1601-1646`). `lightbulb.agent.<id>.working` shows the request's task in 60 chars. `queued` and `working` show as `working`, and `ready` shows as `has_news` (`packages/shared/src/voice/words.ts:57-61`) |

### 3.5 Acknowledgement and delivery

**Acknowledgement.** The strip shows every request of the turn as working within 1 s of the
final. For a turn with two or more requests, the first request's agent opens its voice. Its
seed holds one line: "You are on X. Forge is on Y. Friday is on Z. Say that in one short
sentence, then wait." No other voice acknowledges. A GPT-Live voice takes 1.5 to 2.5 s from
summon to first audio (10-02 call, name path), so the spoken acknowledgement lands at 2.5 s
or less. The holding line (`huddle.py:75-79`) is said at most once per turn, and its promise
to post in the room is removed.

**Delivery.** One voice speaks at a time, as today. When a request's run ends, the request
becomes `ready`. The scheduler gives the floor to the oldest ready request first. Ties go to
the request order. It hands over the floor with the existing switch (`huddle.py:838-851`), and
the answer goes in the seed, as the announce seed does today (`huddle.py:1086-1092`). Rules:

- A delivery never starts while a person is speaking. "Quiet" means no human among the SFU's
  active speakers and no human final in the last 1 s (the signals `_proactive` uses,
  `huddle.py:1542-1545`).
- A requested answer is not news. It has no rate limit (`gate.py:7-9` still applies to
  unsolicited news).
- `end_exchange` no longer clears pending requests (`huddle.py:1700`).
- **Barge-in:** a person speaking over a delivery cuts it (`_cut`, `huddle.py:733-738`). That
  request is `delivered`. Other ready requests wait for the next pause.
- **A run fails:** the request is `failed`. In its turn, its agent says one line: "Sorry, I
  couldn't reach my backend for that" (`CANT_REACH`, `huddle.py:81`).
- If the call ends, or the asker has left and no pause comes within 60 s, a ready answer goes
  to the room. The exception is `noPost`.

### 3.6 Commands on the request list

| said | effect |
|---|---|
| "Forge, stop" / "Forge, never mind" | cancel Forge's requests and abort its runs. The abort exists (`huddle.py:542-552`, `:1267-1280`) |
| "everyone stop" / "stop all" | cancel every open request and abort every run. These phrases are added next to `stop_request` (`transcript.py:325-345`) |
| "don't post that" / "Forge, don't post that" | sets `noPost` on the named agent's newest open request, else on the room's newest open request. The worker skips its own late post. The run is steered through the existing steer route (`voice_calls.py:732-745`) with `noPost: true`. The bridge marks the run, and `run_call_turn` skips `_post_late_call_answer` (`turn.py:371-374`) |

### 3.7 Startup

The deployed agent `lightbulb-huddle` is on LiveKit Cloud's Build plan. `lk agent status`
shows it Sleeping, with Replicas 0/1/1. LiveKit says Build plan agents can shut down after
sessions end and take "up to 10 to 20 seconds" to join the next session. Cold start
prevention is included from the Ship plan ($50/month, 5,000 agent session minutes, then
$0.01/min). Sources: https://livekit.com/pricing and
https://docs.livekit.io/deploy/admin/billing.md. The fix is that plan upgrade. It is an owner
decision about money, not code.

### 3.8 Channel profiles

One engine. Only the rows that change behaviour for this goal are listed.

| | Live (channel, DM, group chat) | Meeting (Google Meet via Recall, Zoom later) |
|---|---|---|
| identity source | one STT stream per LiveKit participant. The server sets the identity (`agent.py:422-430`, `:450-453`) | one mixed stream from `meet:<roomId>`. The speaker label comes from Recall speech events ("Priya", "A + B", "Someone") (`#3638: apps/huddle-agent/src/huddle_agent/huddle.py:1566-1595`). Every speaker acts as the inviter (`#3638: apps/backend/core/services/voice_calls.py:368-381`) |
| floor rule | a lone speaker is always answered (`voice_calls.py:892-904`). Delivery as in §3.5 | only named agents, or a question nobody claimed within 3.5 s (`#3638: huddle.py:738-742`, `#3638: voice_calls.py:936-938`). Delivery as in §3.5 |
| where unheard answers land | the channel, as the agent's "From the Live call" message (`voice_calls.py:303-319`), never when `noPost` | the meeting room, at once (`#3638: huddle.py:1516-1520`), never when `noPost` |
| privacy | none. Everyone in the call is a member | internal or external, fixed at invite time (below) |

**Meeting privacy.** The invite parser already has the attendee list
(`#3638: apps/backend/core/services/meeting_invites.py:64`). If every attendee on the invite
is a workspace member, the meeting is internal, and agents behave as in Live. If anyone
outside the workspace is invited, the meeting is external. Agents still answer the question
asked, but they do not bring in workspace-private content unprompted: other rooms, notes or
files. This is decided once, at invite time, and stored on the meeting room's META. The
meeting room shows one line: "Outside guests are invited, so agents won't share
workspace-private details." In an external meeting, that rule is added to the agent's voice
persona (`voice_calls.py:616-643`) and to the run note (`voice_calls.py:292-300`). There is no
switch and no voice command.

## 4. Acceptance

| measure | target | source |
|---|---|---|
| the 2026-10-03 line | 3 requests with the right tasks, all 3 runs started within 2 s of the final | replay test in `apps/huddle-agent/tests`. Bench cases in `apps/backend/scripts/speak_gate_bench/multiparty.py` |
| heard | strip ≤ 1 s, spoken acknowledgement ≤ 2.5 s p50 | new `live.ack.strip_ms`. `live.latency.first_audio` (`huddle.py:905-907`) with `path=ack` |
| one voice at a time | 0 deliveries started over a person, 0 moments with two agent voices | new `live.floor.overlap` |
| ghost lines | 0 across 20 calls | CloudWatch Logs Insights on the `live transcript` log (`huddles.py:284-287`) for `A team voice call` |
| "don't post" | 0 room posts for `noPost` requests | worker log, and the bridge log for skipped posts |
| startup | joins in a few seconds after the plan upgrade | LiveKit Cloud session events |

## 5. Open question

1. **Upgrade the LiveKit agent to the Ship plan ($50/month) to remove the 10 to 20 s cold
   start?** Recommendation: yes. It is the only fix for goal 5, and no code path can replace
   it.
