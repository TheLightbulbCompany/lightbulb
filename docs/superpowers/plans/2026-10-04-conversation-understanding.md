# Conversation Understanding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement
> Part B task by task. Part A is executed by the controller against the benchmark.

**Goal:** Every spoken line is understood by code; every request gets its own row, owner, run and
visible end; small talk starts no work.

**Architecture:** The backend's `/route` answers every Live line with `ops` (requests with owners,
social, more, cancel, steer) from one Jev call over the line cut into parts. The worker applies `ops`
to its request list for every line, including the floor speaker's, and binds the voice model's
delegations to rows instead of dispatching them.

**Tech stack:** Python 3.12. Backend: FastAPI, `core/services/{messaging,voice_calls,jev,voice_persona}.py`,
pytest. Worker: `apps/huddle-agent` (LiveKit Agents, GPT-Live plugin), pytest with the fakes in
`tests/test_huddle.py`.

**Spec:** `docs/superpowers/specs/2026-10-04-conversation-understanding-design.md`

## Global Constraints

- Two branches off `origin/main`, two PRs: backend `feat/conversation-understanding` (worktree
  `~/Desktop/lb-talk.nosync`), worker `feat/conversation-understanding-worker` (worktree
  `~/Desktop/lb-talk-worker.nosync`). The backend merges and deploys first.
- The backend keeps today's `/route` fields (`agentId`, `agentIds`, `hold`, `requests`) for a worker
  not yet deployed. The worker keeps today's handling for a `/route` answer with no `ops`.
- No new table, flag, queue or dependency. No change under `apps/infra/hermes/` (no image build).
- Never run a whole test suite. Run only the touched test files:
  `cd apps/huddle-agent && uv run pytest tests/<file> -q -p no:cacheprovider`, and
  `cd apps/backend && uv run pytest <files> -q -p no:cacheprovider --no-cov`. Plus `uv run ruff check`
  and `uv run ruff format --check` on touched files.
- Metric dimensions are enums only. Never put transcript text in a metric or a log line that is not
  the existing transcript log.
- A task's text is the speaker's own words. No model rewrites a task.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (implementers) .
- Comments match the file's style: short, say why, cite the spec section ("understanding §4.6").

## Review Focus

1. A delegation that arrives before, during and after its final line's `/route` round trip: exactly
   one run for the floor agent's request, never two, never zero.
2. `/route` down or answering without `ops`: every path behaves exactly as today (the existing suites
   pass unchanged with a backend that returns no `ops`).
3. "Everyone stop" or "<agent>, stop" said while a line is being understood: no row starts afterwards.
4. Two finals from one speaker one second apart, each with requests: rows keep spoken order, one
   acknowledgement, per-agent runs start one at a time.
5. Hang-up with rows queued, working, ready and failed: every row ends delivered, posted, cancelled
   or failed-and-posted; `live.request.open_at_end` is 0.

---

## The `/route` contract (both parts build to this)

Request body gains two optional fields:

```json
{"open": [{"id": "r3", "agentId": "…", "task": "…", "status": "working"}],
 "floor": {"agent": "…", "speaker": "…", "mine": true}}
```

`open`: at most 12 rows: every queued, working and ready row, then the newest delivered rows, three
at most. `floor.mine`: the speaker is the person the floor agent is talking with.

Response gains `ops` (absent only on a backend that predates this plan):

```json
{"agentId": "…", "agentIds": ["…"], "hold": true,
 "ops": {"requests": [{"agentId": "…", "task": "…", "by": "name|fit|floor|chief"}],
         "social": "agentId or null",
         "more": false,
         "cancel": ["r3"],
         "steer": [{"requestId": "r2", "text": "…"}]}}
```

`hold: true` means the line is unfinished: the worker merges it with the speaker's next final, as
today, and ignores `ops`.

---

## Part A: backend (controller, bench-driven)

### Task A1: benchmark and baseline

**Files:** `apps/backend/scripts/speak_gate_bench/understand.py`, `understand.jsonl`,
`results/2026-10-04-understand.md`.

- [ ] `uv run python -m scripts.speak_gate_bench.understand --check` passes on the 100 scenes.
- [ ] Review every scene's gold answer against the spec's policy; fix rows, never the policy.
- [ ] `--runs 3 --tag baseline` against today's `route_live_line`; record the report.

### Task A2: understanding stage and `ops`

**Files:** `core/services/messaging.py` (cut, part questions, assembly), `core/services/voice_calls.py`
(`route_live_line`, owners, `ops`, legacy fields), `routers/huddles.py` (`RouteBody.open`, `floor.mine`),
tests `tests/unit/services/test_voice_understanding.py` and the touched existing tests.

**Produces:** `messaging.understand(latest, members, *, history, cards, floor, open_requests, humans,
setting, wake, unclaimed) -> dict` with keys `agents, humans, hold, anyone, path, parts` where each part
is `{"text", "kind", "who", "p", "named"}`, and `voice_calls.route_live_line(..., open_requests=None)`
returning the contract above.

- [ ] Cut at request cues (spec §4.2 step 1), unit-tested on the spec's examples and the split bench lines.
- [ ] One Jev call: `kind` and `who` per part, `amends`/`drops` per call; hedged as phase 1.
- [ ] Assembly, owner rule (name, fit, floor, chief), meeting and two-people rules, `floor.mine`.
- [ ] Jev failure: one request, whole line, named agent else floor agent else single-responder fallback.
- [ ] Legacy fields derived from `ops`.
- [ ] Gates: understand bench (spec §5.1), split bench ≥ 0.95, multiparty voice gate.

### Task A3: persona and turn note

**Files:** `core/services/voice_persona.py`, `core/services/voice_calls.py` (`TURN_NOTE`), their tests.

- [ ] Persona rules from spec §4.6. `TURN_NOTE` from spec §4.7. Both stay static strings (prompt cache).
- [ ] Tests pin the freshness sentence, the absence of "speed matters more than completeness", and the
      four exits.

---

## Part B: worker (subagent-driven)

Read first, every task: `apps/huddle-agent/src/huddle_agent/huddle.py` (`on_final_transcript`,
`_dispatch`, `_route`, `summon`, `_open`, `_start_requests`, `_run_request`, `on_delegation`, `_drive`,
the RunSink methods, `tick`), `request_list.py`, `backend.py`, and the fakes at the top of
`tests/test_huddle.py` (`FakeBackend`, `FakeLive`, `make`, `say`, `settle`, `answer`) and
`tests/test_request_engine.py` (`three`, `run_of`, `quiet`).

In tests, `/route`'s answer comes from `FakeBackend`: `jev_to` / `route_to` name the routed agent and
`route_extra` is merged into the answer. Every new test builds its answer with a helper
`ops(requests=[(agent, task, by)], social=None, more=False, cancel=(), steer=())` added to
`tests/test_huddle.py` in Task B1, which also fills `agentId` / `agentIds` the way the backend derives
them (distinct owners in order).

### Task B1: the request list holds every request

**Files:**
- Modify: `src/huddle_agent/request_list.py`, `src/huddle_agent/backend.py` (`route`)
- Test: `tests/test_request_list.py`, `tests/test_huddle.py` (the `ops` helper, `FakeBackend.route`
  recording `open` and `floor`)

**Interfaces — Produces:**
- `Request.id: str` (`"r1"`, `"r2"`, … from a counter on the list, never reused) and `Request.by: str = ""`.
- `RequestList.settle(turn, asker, routed, now) -> list[Request]`: one row per entry of `routed`
  (`{agentId, task, by?}`), in order. Several rows for one agent are kept. A row whose `(turn, agent)`
  was stopped while `/route` decided is not made (as today).
- `RequestList.startable() -> list[Request]`: queued routed rows, oldest first, at most one per agent,
  never an agent that has a `working` row, and never more than `MAX_WORKING` working in total.
- `RequestList.get(request_id) -> Request | None`.
- `RequestList.for_route() -> list[dict]`: `{id, agentId, task, status}` for every open routed row,
  then the newest delivered rows (three at most), 12 rows at most, tasks cut to 200 chars.
- `ack_line(rows, name, me=None) -> str`: `me` is the agent that will say it. Its own rows first:
  "You are on A and on B." Then each other agent once: "Forge is on C and on D." At most three
  requests are named; more become "and N more, shown in the room." Ends with
  "Say that in one short sentence, then wait." With `me=None` it behaves as today (first row's agent).
- `Backend.route(..., open: list[dict] = ())` sends `open`; `floor` may carry `mine`.

**Behavior to pin with tests:**
- Three rows for one agent from one turn: all kept, ids differ, `startable()` returns only the first;
  after it is delivered or failed, the second.
- Rows for two agents: both startable at once. Five agents: four.
- A steered row that shares a run (today's `bind` to another run) still holds one slot.
- `for_route()` order and caps. `ack_line` for: one row mine; two mine; one mine and one other; none
  mine; five rows.
- Every existing test in `tests/test_request_list.py` and `tests/test_request_engine.py` still passes;
  where a test pinned "one row per agent, the first ask wins", change it to the new rule and say so in
  the report.

### Task B2: `ops` applied to the lines that reach `/route` today

**Files:**
- Modify: `src/huddle_agent/huddle.py`
- Test: `tests/test_understanding.py` (new)

**Interfaces — Consumes:** Task B1. **Produces:**
- `Huddle._understand(identity, text, mark, humans, unclaimed=False) -> dict | None`: the `/route` call
  alone (body as `_route` builds it today, plus `open=self._requests.for_route()` and `floor.mine`),
  its latency metric, and None on `BackendError`.
- `Huddle._apply(identity, mark, ops) -> list[Request]`: rows made from `ops["requests"]`
  (`settle`), `ops["cancel"]` and `ops["steer"]` left for Task B5 (ignored here).
- `_route` uses them. When the answer has `ops` and no `hold`, the new handling below replaces
  everything from "`agent_ids = …`" down. With no `ops`, the code path is today's, unchanged.

**New handling (answer has `ops`):**
1. Unnamed requests (`by != "name"` for every request) are dropped when someone else spoke since the
   line (today's superseded rule, same metric). Named requests always stand.
2. `rows = self._apply(...)`. No rows:
   - `ops.social` set and no voice open: `summon(identity, ops.social, request=None, path="jev")` with
     a seed line `{name} said: "{text}". Small talk: reply yourself in one short sentence. Do not
     delegate it.` then a nudge. No run starts. With `ops.more`, the seed is `{name} will list several
     requests. Say "go ahead" at most, then stay quiet until they finish.`
   - otherwise: today's "nobody" (`_unrouted`), nothing else.
3. Rows, no voice open: the first row's agent takes the floor (`path` "name" when `by == "name"`,
   else "jev"). Its first row is its **driven run**: `_Driven` as `_open` makes today, but the run's
   request is the row's task, and the row is `start`ed and `bind`-ed to the driven run id before
   `/turns` is posted. Other rows start through `_start_queued()`. When there are two or more rows,
   the voice's seed also carries `ack_line(rows, name, me=agent)`.
4. Rows, a voice open, the speaker is NOT its speaker: rows start through `_start_queued()` at once
   (they no longer wait in `_queue` for the floor); the floor-holder still hears the line as thinking
   (`_other_line` as today, minus its own `/route` call, which is this one). Answers reach the room
   through `_ready_delivery` as phase 1.
5. A driven run that fails to post marks its row failed (`_request_failed`), beside today's
   CANT_REACH line.
6. `switch` (the floor speaker named another agent mid-line): requests owned by another agent start
   as rows; the floor moves to the first such agent only when no request belongs to the floor agent
   (today's switch), with that row as the new voice's driven run.

**Tests (each asserts `backend.turns`, the rows' statuses, and `lives[...]` calls):**
- "Hey guys, how's it going?" with `ops(social="pulse")`: Pulse's voice opens, seed has the small-talk
  line, `backend.turns == []`, no row.
- An unnamed line with three requests for Forge, Forge and Friday, no voice: Forge's voice opens; one
  `/turns` for Forge with the first task and one for Friday; Forge's second row is queued; the seed
  carries the acknowledgement; when Forge's first run ends, its second starts within one tick.
- The same with a voice open for another speaker: no summon, both agents' first rows post `/turns` at
  once, `h._queue == []`.
- A superseded unnamed line starts nothing; a superseded named line still starts.
- `/route` answering without `ops`: the summon, queue and `requests` behavior of today (copy three
  representative assertions from `tests/test_request_engine.py` and `tests/test_huddle.py`).
- A driven row whose `/turns` fails for good: row failed, CANT_REACH said once.

### Task B3: the floor speaker's lines, and delegations bind

**Files:**
- Modify: `src/huddle_agent/huddle.py`
- Test: `tests/test_understanding.py`

**Interfaces — Consumes:** B1, B2. **Produces:** `DELEGATION_WAIT_S = 2.0`.

**Behavior:**
1. In `_dispatch`, a line from the floor voice's own speaker that is real speech, not a pause and not
   a stop command is understood: `humans=len(people)`, `floor.mine=True`. (Today the worker does
   nothing with it.) The call is recorded as that speaker's in-flight understanding; its result
   (rows, ops, time) is kept as the speaker's latest understanding.
2. Its rows: the floor agent's first new row becomes a driven run exactly as `_drive_request` does
   today (thinking: `{name} asked: "{task}" — already handed to your backend self; don't delegate it
   again`), with the row bound to the driven run. A driven run already working for the floor agent
   means the new row waits its turn (per-agent serial); it then starts through `_start_queued()`.
   Other agents' rows start through `_start_queued()`.
3. The barge path (`_barged`) and the bare-"Lightbulb" re-point (`summon` onto the voice that already
   holds the floor with a request) are understood the same way after the re-point, instead of driving
   or typing the whole line. If `/route` fails, each does what it does today.
4. `on_delegation`, after today's stop / no-post checks:
   - wait, at most `DELEGATION_WAIT_S`, for the floor speaker's in-flight understanding; when none is
     in flight and the speaker's latest understanding is older than `BIND_SECONDS`, wait that long for
     a final line to start one;
   - a driven run from that understanding exists: bind (today's branch);
   - that understanding made rows but none for the floor agent: answer the delegation with
     `append_commentary(ack_line(rows, name, me=floor agent), delegation_id=…)`;
   - it made no rows: answer with `append_commentary("Nothing to look up for that. Reply yourself in
     one short sentence.", delegation_id=…)` (for `ops.more`: the "go ahead" line of B2);
   - no final line came: understand `pending_transcript` itself (same handling as 1 and 2), remember
     the time, and when that speech's final arrives later (its start is before that time) add it to
     the transcript only;
   - `/route` failed or answered without `ops`: today's branch (one run, the whole transcript).
5. Meeting setting: the same, because the backend applies the meeting rules.

**Tests:**
- Floor speaker says a line with one request for the floor agent; the delegation arrives (a) before
  the final, (b) while `/route` is in flight (`backend.route_gate`), (c) after: in each, exactly one
  `/turns`, bound to the delegation (`live.driven.bound` emitted), `append_commentary` answers carry
  the delegation id.
- The founder's line 5 with `ops` of three requests (email for the floor agent, two for Forge): one
  driven `/turns` for the floor agent, one `/turns` for Forge, Forge's second row queued.
- All rows for other agents: the delegation is answered with the acknowledgement note; no `/turns`
  for the floor agent.
- Small talk from the floor speaker, model delegates: answered "Nothing to look up…", no `/turns`.
- No final within 2 s of a delegation: its transcript is understood; the late final adds no row.
- No delegation at all (the model answered nothing): the row still runs and its answer is spoken
  (`append_commentary`) when the run ends.
- `/route` raises for a floor line: no rows; the delegation posts today's single run.
- A barge with two requests: re-pointed, two rows, no whole-line driven run.

### Task B4: a named summon opens the voice while the line is understood

**Files:**
- Modify: `src/huddle_agent/huddle.py` (`_dispatch` first branch, `summon`, `_open`)
- Test: `tests/test_understanding.py`

**Behavior:** a line that opens with an agent's name (today: `summon(request=text, path="name")`)
starts `_understand` first and passes it to `summon(..., understood=task)`. `_open` awaits it together
with the window and the session fetch (`asyncio.gather`). Then:
- `ops` with rows: as B2 step 3 (first row of that agent is the driven run with the row's task; other
  rows start; acknowledgement in the seed when two or more rows). When no row belongs to the named
  agent, the voice still opens and its seed is the acknowledgement.
- `ops` with no rows: `social`/`more` seed lines of B2; otherwise the seed is `{name} said: "{text}"`
  and a nudge (a bare summons, as "Forge?" today).
- no answer or no `ops`: today's behavior (driven run with the whole line).
- "Everyone stop" / "<agent>, stop" during the wait: no run starts (use `self._requests.stopped`).
- Latency: the `live.latency.*` metrics keep their meaning; add nothing on the path before
  `live_factory` except the awaited understanding.

**Tests:** "Forge, check CI, then tell me what shipped" with two Forge requests: one `/turns` with the
first task only, second row queued, voice seeded with the first task; "Forge, how's it going?" with
`ops(social="forge")`: no `/turns`; `/route` gated slow: the voice opens only after it answers and
`/turns` carries the row's task; `/route` error: whole-line driven run; a stop while waiting.

### Task B5: acknowledgement, cancel, steer

**Files:**
- Modify: `src/huddle_agent/huddle.py`
- Test: `tests/test_understanding.py`

**Behavior:**
1. **Acknowledgement for an open voice.** Rows made from the floor speaker's lines collect in a
   per-speaker pending acknowledgement. `tick()` sends one note when: the speaker has been quiet for
   `ACK_QUIET_S = 1.0`, no understanding of theirs is in flight, and the collected rows are two or
   more or include a row for another agent. The note is
   `append_thinking(ack_line(rows, name, me=floor agent))` then a nudge. A single row for the floor
   agent sends nothing. A delegation answered with the acknowledgement (B3) clears the pending one.
2. **Cancel.** For each id in `ops.cancel`: the row is cancelled; a working run is aborted (as `_stop`
   does for one run: `_runs.abort`, `_stopped`, the backend abort); its unspoken answer is dropped,
   neither spoken nor posted; the floor voice gets the thinking line `{name} dropped: "{task}".` and
   a nudge. Unknown ids are ignored.
3. **Steer.** For each `{requestId, text}` in `ops.steer`: a queued row has the text appended to its
   task; a working row's run is steered (`backend.turn(..., steer_run_id=row.run_id, steer_only=True)`
   with the text); if the backend says it was not steered (the run just ended), a new row for the
   same agent is made with the text. Unknown ids make a new row for the floor agent, else are ignored.
4. Metrics: `live.request.created{by}`, `live.request.cancelled`, `live.request.steered`.

**Tests:** two finals 0.5 s apart with three rows across two agents: one acknowledgement, after the
quiet second; a single own row: none; cancel of a working row: abort posted, row cancelled, its
later `run_ended` says nothing and posts nothing; steer of a working row: one steer-only `/turns`, no
new row; steer refused: a new row; steer of a queued row: its `/turns` carries both texts.

### Task B6: ends, metrics, and the replay

**Files:**
- Modify: `src/huddle_agent/huddle.py`, `request_list.py`
- Test: `tests/test_understanding.py`, `tests/test_replay_2026_10_04.py` (new)

**Behavior:**
1. An announcement cut off by a person after its audio began (`live.announce.cut`) is also posted to
   the room through `_late_answer`, unless "don't post that" covers it.
2. A failed row whose CANT_REACH line was never said is posted when its news expires or the call
   ends: `_late_answer` posts `I could not finish this on the call. Ask me again here.` for it
   (`postable` no longer blocks a failed row; cancelled and no-post still block). If `/late-answer`
   refuses a run that never started, catch it and log, as today.
3. `live.request.ended{status}` once per row on reaching delivered, cancelled or failed.
   `live.request.open_at_end` (a count) when the worker leaves the room, after the hang-up sweep.
   `live.understand.ops{kind}` per understood line with kind in `request|social|more|cancel|steer|none`.
   `live.delegation.unbound` when a delegation's own transcript had to be understood.
4. The transcript log gains one line per row change: `[request r3 forge working] <task, 120 chars>`
   through the existing transcript flush (it is transcript content, so it stays in the transcript log).
5. `tests/test_replay_2026_10_04.py`: the founder's call, lines 0, 3, 5 and 8 with their delegations
   and `ops` as the backend would answer (owners: Pulse for the application and the email, Forge for
   features and PRs, Forge for the follow-up). Asserts: no `/turns` for the greeting; four `/turns`
   in total for lines 3 and 5, each with one task; one acknowledgement note; Forge's two runs one
   after the other; every row delivered after the answers are injected; `open_at_end == 0`.

---

## Self-review

- Spec §4.1 → B3.1; §4.2, §4.3, §4.4 → A2; §4.5 → B1, B2; §4.6 → B2 (small talk, unnamed), B3
  (floor, delegation), B4 (named), B5 (acknowledgement), A3 (persona); §4.7 → A3; §4.8 → B6;
  §5.1 → A1, A2; §5.2 → B2–B6; §5.3 → B5, B6.
- The `/route` contract is defined once, above, and both parts cite it.
