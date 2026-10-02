# Voice Option D Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to carry out this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each agent speaks for itself in Live, in its own voice and persona. A voice request is an ordinary run on that agent's room session key. It runs in the background, posts nothing to the room timeline, and is remembered in the room conversation. The only state Live owns is the window that meters GPT-Live minutes.

**Architecture:**
- Backend (`apps/backend`):
  - meters one voice window per room;
  - dispatches voice runs on `agent:{agentId}:lightbulb:channel:{roomId}` with the room's `recentHistory`;
  - relays the bridge's run frames to the LiveKit worker by room;
  - serves persona, team, routing, rename and voice-notes routes;
  - deletes the `/calls` orb (Live is in every room, DMs included, so D replaces it).
- Bridge (`apps/infra/hermes/bridge`):
  - accepts a voice envelope on the room key (the only call envelope left; the `/calls` orb's `callId` envelope is deleted);
  - runs it through the room turn's own history path, so it persists into the room key's Hermes session (`state.db`) like any chat turn. That session is the memory voice and chat share;
  - fixes queued and late steers;
  - injects `VOICE_NOTES.md`.
- Worker (`apps/huddle-agent`):
  - opens one GPT-Live session per agent: its own persona, voice and track;
  - matches names locally;
  - drives the first turn, routes frames by run, queues and switches the floor, handles barge-in, announces news;
  - publishes per-agent attributes and `live.notice`.
- Clients:
  - web and iPhone render per-agent tiles from those attributes and the room member list;
  - both send `talk_start {agentId}`.

**Tech Stack:** FastAPI + DynamoDB (backend); Python bridge on Hermes 0.21.3 in AgentCore; livekit-agents 1.8.3 + `gpt-live-1` + `gpt-4o-mini-transcribe` (worker); Next.js 16 + `@livekit/components-react` 2.9.24 (web); Expo SDK 57 + livekit-client (iPhone); shared code in `packages/shared/src/voice/words.ts`.

**Spec:**
- `docs/superpowers/specs/2026-10-02-voice-agent-is-the-voice-design.md` ("D"), branch `docs/voice-option-d` @ 656974ca5.
- `docs/superpowers/specs/2026-10-02-live-ui-ux-design.md` ("UI").
- Reviews: `.option-d-grounding-review.md` ("GR") and `.option-d-recheck.md` ("recheck").

Code is cited at origin/main c16a4c6fc; the orb-deletion facts (Task 5) were re-checked at ad01bd813.

**Owner ruling (2026-10-02): fix forward, build nothing intermediate.** No compatibility staging, no merge holds, no split promotions, no keep-alongside legacy. A few minutes of skew while prod rolls is accepted.

**Preconditions (assumed, built elsewhere):**
- Hermes sessions are in their **end state** (PR #3561, being extended in parallel): every turn passes `session_db` in `_construct_agent`, and room turns pass the room key's Hermes transcript (`state.db`) as `conversation_history` plus a "room messages since your last run" block built from `recentHistory` past a `messageSeq` cursor kept in session metadata. This plan calls that "the room-turn history path". A voice run on the room key goes through it, so what is said by voice is in the room session for the next chat or voice run with no voice-specific storage. That is D's shared memory.
- PRs #3543 (cut-ins/hand-offs) and #3559 (proactive) are NOT merged. Tasks 24, 25 and 26 rework them. Close both PRs with a pointer to this plan when those tasks merge.

---

## Global Constraints

- **Nothing reaches the room timeline from Live.** No message, notice, error or transcript is posted (owner ruling). Notices travel on LiveKit data topics only.
- **Paperclip keeps its own keys** (`paperclip:company:…`, D §3a). No task changes the Paperclip session path.
- **No Live-specific session key.** The voice envelope's `sessionId` must equal `turn.lightbulb_session_key(agentId, roomId)`. `call_session_key` and the `…:call:{callId}` shape are deleted (D §3), together with the `/calls` orb (Task 5).
- **Runs are never stopped by Live.** Leaving, closing the window, the worker exiting or a floor switch never aborts a run. Only an explicit "stop/never mind" to that agent aborts one (D §4, §8). D's "or anyone's Stop" has no surface in this plan: voice runs are not in the `RUN#` ledger, so no client lists them (see Owner decisions).
- Each task gets a fresh worktree off clean `origin/main` (never stack on `codex/*` or another agent's branch) and its own PR.
- Testing:
  - CI is the full test gate. Write the tests in the task and push; do not run full suites locally (owner rule).
  - Merge after CI is green and Codex's findings are handled. Read Codex output in a separate step from the merge.
- Any change under `apps/infra/lib/`: run `cd apps/infra && npx cdk synth` and commit the regenerated `.github/workflows/deploy.yml` in the same PR.
- Never commit `apps/infra/hermes/skills/` (it is generated).
- Every commit ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. PR bodies end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Lines marked **(inferred)** in D keep their `ponytail:` comment naming the ceiling.

## Order

Merge tasks in dependency order; dev deploys on merge. Ship to prod once, when the set is done. No holds, no compatibility shims.

1. **First:** Task 5 (orb deletion; nothing mounts the orb), Task 2 (shared), Task 1 (metrics baseline).
2. **Backend:** Tasks 3, 4, 12, 14, 15, then 10, 11, then 13 (it uses Task 11's VOICERUN helpers).
3. **Bridge:** Tasks 6, 7, 8 (plus Task 3's bridge half). Then Task 9 builds the image and rolls **dev**, after Tasks 10 and 11 are merged, so dev's backend and dev's runtimes switch envelopes together.
4. **Worker:** Tasks 16–19, then Task 20 once Tasks 10–12 are on dev, then Tasks 21–28.
5. **Web:** Tasks 29–33 after Task 2. Prod frontend ships only with the dispatch below.
6. **One prod promotion** (Task 39), with 0 deploy runs in flight:
   - PR flipping `hermes-version.json` `prod.tag` to the dev-validated tag; merge.
   - `gh workflow run deploy.yml --ref main` (backend, web, and `AGENTCORE_HERMES_IMAGE` from the pin, service-stack.ts:45/:1450).
   - Once `aws ecs describe-task-definition` shows the new backend image: `gh workflow run huddle-agent.yml --ref main` (prod deploys the worker on `workflow_dispatch` only; needs `vars.LIVEKIT_PROD_ENABLED`), then `roll_hermes_runtimes.py --all` and `reapply_hermes_skills_dirs.py` on prod. Verify `containerUri` with `aws bedrock-agentcore-control get-agent-runtime`.
   - **Accepted skew** while this runs: an owner whose runtime is not yet rolled gets `503 runtime_unavailable` on Live requests (the old bridge answers the voice envelope `400 invalid_call_run`), and the old worker cannot start Live against the new `/summon` (it reads `persona`/`voice`, huddle.py:245) until the worker dispatch lands. Chat is unaffected.
7. **iPhone:** Tasks 34–38 merge after the promotion, because an OTA reaches prod phones on merge (`mobile-ota.yml`). No TestFlight build: every mobile task is JS-only and adds no native module. Each mobile PR shows that `npx expo-updates runtimeversion:resolve --platform ios` gives the same output on main and on the branch.

---

## Canonical Cross-Area Contract (every task below conforms; this section wins over any task text)

### C1 Backend routes for the worker (header `x-lightbulb-huddle-secret`, all bodies carry `ownerId`, `roomToken`)

| Route | Body (beyond ownerId, roomToken) | Response | Errors |
|---|---|---|---|
| `POST /api/v1/internal/huddles/{roomId}/summon` | `speakerUserId` | `{callId, deadline, budgetSeconds, joined: bool}` | 402 `voice_limit {reason: free_minutes_used\|credits_low\|plan_inactive}`, 409 `voice_busy`, 409 `no_agents`, 409 `workspace_not_ready`, 404 `room_not_found` |
| `POST /api/v1/internal/huddles/{roomId}/team` | — | `{agents: [{id, name, listenMode, defaultName: bool}], people: [{userId, name}], ceoId: str\|null}` | 404 `room_not_found` |
| `POST /api/v1/internal/huddles/{roomId}/agents/{agentId}/session` | — | `{agentId, name, voice, persona, askName: bool}` | 409 `agent_not_in_room` |
| `POST /api/v1/internal/huddles/{roomId}/route` | `speakerUserId, utterance, transcript` | `{agentId}` | — |
| `POST /api/v1/internal/huddles/calls/{callId}/turns` | `speakerUserId, agentId` (**required**), `runId` (**required**, uuid, minted by the worker once per delegation and resent on every retry), `utterance` (1–4,000), `transcript` (≤12,000), `steerRunId: str\|null` | `{runId, agentId, steered: bool}` | 404 `call_not_found`, 409 `agent_not_in_room`, 503 `runtime_unavailable` |
| `POST /api/v1/internal/huddles/{roomId}/runs/{runId}/abort` | — | `{aborted: bool}` | — |
| `POST /api/v1/internal/huddles/calls/{callId}/end` | `usageSeconds` | `{closed}` (unchanged; no longer stops runs) | — |
| `POST /api/v1/internal/huddles/{roomId}/presence` | unchanged | unchanged | — |

Notes on C1:
- `/summon` opens or joins the window. `joined` is true only when an open window already existed for the room. The worker calls it only when it holds no window. `budgetSeconds` is `deadline - startedAt` on open and `deadline - now` on a join (a restarted worker never gets the full budget back).
- `/team` lists only agents whose `listenMode` is not `observe`.
- `/route` is called only when the worker matched no name in a wake position. It first runs `voice_routing.named_agent` over the whole utterance (D: a name inside the request > Jev > CEO), then Jev with a 3 s timeout, then `ceoId`, then the first agent.
- A `/turns` retry with the same `runId` is answered by the bridge as `200 replayed` (`register_call_run`, server.py:184-186), so a delivered-but-timed-out invoke never runs twice.
- `agent_not_in_room` is 409 on every route; the worker refreshes `/team` on it.

### C2 Bridge → backend frame relay (hook token of `ownerId`)
- `POST /api/v1/internal/huddles/{ownerId}/rooms/{roomId}/frames`, body `{events: [Event]}`.
- It does **not** 404 after the window closes.
- `Event = {schemaVersion: 1, eventId, type: "voice.run.frame", occurredAt, payload: {roomId, runId, agentId, frame: {type, …, runId, agentId}}}`.
- Relayed `frame.type` values (bridge `HUDDLE_FRAMES`): `tool_start`, `chunk`, `error`, `run_ended`, `run_queued`.

### C3 Backend → worker LiveKit data
- Topic `call.frame` (`livekit_huddles.FRAME_TOPIC`), `destination_identities=["agent-ceo"]`, sent server-side (sender None).
- Packet `{"events": [...]}`, where each event is one of:
  - a `voice.run.frame` (C2);
  - `{type: "voice.notice", payload: {kind: "renamed", agentId, from, to, by, collides: bool}}`;
  - `{type: "voice.notice", payload: {kind: "voice_note_saved", agentId, name, text, by}}`.
- `renamed` is sent from the shared rename seam (`refresh_agent_soul`, Task 13) for every rename door: the voice route, the Teams PATCH (and so Undo) and `runtime_rpc agents.update`. `voice_note_saved` is sent by the voice route and the C9 PUT.
- The worker drops any packet whose sender is not None.

### C4 Worker participant (identity stays `agent-ceo`) attributes
- `lightbulb.speaking_agent`: **the floor-holder**, i.e. the agent whose voice is joining or open, from the moment the worker starts opening it until it closes; `""` when none. During a concurrent switch it is the incoming agent. It does not mean "making sound now": the key name is historical.
- `lightbulb.agent.<id>.status`: one of `joining | speaking | listening | working | has_news | idle`. **Status alone decides the bars and the state line**; the floor-holder's status is `joining`, `speaking` or `listening`. Everything that asks "who holds the floor" (the tap no-op, the queue head, the iPhone stage, the header, the floor-holder sink) reads `speaking_agent`.
- `lightbulb.agent.<id>.working`: ≤60 chars.
- `lightbulb.agent.<id>.naming`: `"1"` while a first-time-naming question is open, else `""`.
- `lightbulb.queue`:
  - While a multi-name request is pending, it holds that request's agent ids in answer order, **with the current floor-holder at the head**. Otherwise it is `""`.
  - Badge = the 1-based index, shown on every entry except the floor-holder. So "Forge and Gauge" → `forge,gauge`, and Gauge shows `2`.
- `lightbulb.listening_to`: the pinned speaker's display name.
- `""` deletes a key. Keys of departed agents are written `""`. The single-Lightbulb keys `lightbulb.status`, `lightbulb.speaker` and `lightbulb.working` are no longer written at all.

### C5 Worker → people
- Data topic `live.notice`, sender `agent-ceo`, JSON body one of:
  - `{kind: "renamed", agentId, from, to, by, collides}` to all people;
  - `{kind: "voice_note_saved", agentId, name, text, by}` to all people;
  - `{kind: "ambiguous_name", agentId}` to the speaker only;
  - `{kind: "voice_failed", agentId}` to all people.
- The existing `huddle.error` topic is unchanged.

### C6 RPC
- `talk_start` to `agent-ceo`, payload `JSON.stringify({agentId})`, or `""` meaning "the last agent this caller spoke to, else `ceoId`".
- An unknown agentId is ignored.

### C7 Audio
- Each agent session publishes a Microphone-source track named `agent-<agentId>` on `agent-ceo`.
- During a switch two such tracks can coexist, so clients filter by name.

### C8 Agent hook-token route (plugin → backend)
- `POST /api/v1/internal/agents/voice-profile/{ownerId}`
- Body `{agentId, runId, action: "rename"|"add_note"|"replace_notes"|"clear_notes", name?, text?}` → `{ok, agentId, name, notes}`.
- Errors: 401, 403 `not_a_member`, 404 `voice_run_not_found`, 409 `name_taken`, 429 `rate_limited`.

### C9 Clerk route (web + iPhone settings)
- `GET /api/v1/teams/agents/{agentId}/voice-notes` → `{name, notes, voice}`.
- `PUT` on the same path with `{notes: str ≤1000}` → `{name, notes, voice}`; 422 when over 1,000.
- Any workspace member may call it.
- Undo of a rename uses the existing `PATCH /api/v1/teams/agents/{agentId} {name}`.

### C10 Join flag
- `POST /huddles/{roomId}/join` returns `lightbulb: true` iff the room has a non-`observe` agent member. That is already true on main (`voice_calls.listening_agents`), so no change is needed.

---

## Review Focus

1. **Contract drift (C1–C10).** Grep each attribute name, topic, route path and JSON key across producer and consumer in every PR. The most fragile points:
   - `voice.run.frame` / `payload.runId`, which replaces `call.turn.frame` / `turnId`;
   - the `lightbulb.queue` head semantics;
   - `voice.notice` arriving on `call.frame`, not on a new topic;
   - `/turns` `agentId` and `runId` being required (one `runId` per delegation, reused on retries);
   - the `/voice-notes` path.
2. **Nothing posted.** The plugin refusal (Task 7), the TURN_NOTE rewrite (Task 11) and the absence of any `post_message` in the voice path.
3. **Run ownership.** No code path from window close, worker leave or a switch reaches `/abort` or `_stop_all_turns` for a huddle row.
4. **One session per room key.** A voice run takes the room key's turn lock and the room-turn history path (Task 6); nothing voice-specific is stored in the profile.
5. **Billing.** A window takes exactly one free claim or credit hold. Summed usage stays ≤ `deadline - startedAt`.
6. **Trust boundaries.**
   - Notices are accepted only from sender None (worker) or `agent-ceo` (clients).
   - The voice-profile route resolves the speaker from the VOICERUN row, never from agent input.
   - A person cannot forge a `developer` "(agent) said" line.

---

## Task 1: Worker latency and GPT-Live concurrency metrics (baseline)

Implements D "Latency gate (F6)" and Unknowns 1 and 6; D §11.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py`, `apps/huddle-agent/src/huddle_agent/agent.py`
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Consumes: today's `summon` / `start_live` / `on_agent_state` (wh:229-272, wa:82-94).
- Produces:
  - `emit_metric(name, value, unit="Seconds", dims: dict[str,str] | None = None)`, which puts dimension keys in EMF `Dimensions [[...]]`;
  - `live.latency.route`, `live.latency.session_started` and `live.latency.first_audio` (Milliseconds), with dimension `path ∈ today|name|jev|switch|tap`;
  - `huddle.gptlive.open_seconds` (a per-job delta flushed in `flush_metrics`).

- [ ] Add `dims` to `emit_metric`.
- [ ] In `Huddle.summon`, take `heard_at` from `on_final_transcript` (default: now). Emit `route` after `backend.summon` and `session_started` after `_live_factory`. Store t0 on `_Exchange` and emit `first_audio` once, on the first `agent_state == "speaking"`.
- [ ] In `flush_metrics`, add a `gptlive` key: the summed `usage_seconds` of open and closed sessions, as a delta, like the `stt` key.
- [ ] Tests:
  - `test_summon_emits_route_session_and_first_audio_latency`: fake clock; final at t, summon returns at t+0.4, speaking at t+2.1 → three EMF lines with `path=today` and values 400 / … / 2100.
  - `test_first_audio_emitted_once_per_exchange`.
  - `test_emit_metric_dims_shape`.
- [ ] Commit `feat(huddle-agent): latency and GPT-Live seconds metrics`. Merge and let dev deploy. Dispatch `huddle-agent.yml` to prod once, before Task 20 merges, so path (a) collects about 20 prod samples: that is the latency gate's baseline (D owner decision 1) measured on today's worker, not a staging step.

---

## Task 2: Shared Live readers and copy (`words.ts`)

Implements UI §0, §1, §3, §4, §6, §7, §9; D §10. This task merges the web and iPhone area plans; both clients consume it.

**Files:**
- Modify: `packages/shared/src/voice/words.ts`
- Test: `packages/shared/src/voice/words.test.ts`

**Interfaces:**
- Consumes: C4 and C5 shapes; `RoomMember {kind, id, displayName, listenMode}` (`packages/shared/src/messaging/types.ts`).
- Produces (re-exported via `index.ts`):
  - Constants: `LIVE_AGENT_IDENTITY = "agent-ceo"` (unchanged), `LIVE_NOTICE_TOPIC = "live.notice"`, `SPEAKING_AGENT`, `QUEUE`, `LISTENING_TO`, `statusKey(id)`, `workingKey(id)`, `namingKey(id)`.
  - `type AgentLiveStatus = "joining"|"speaking"|"listening"|"working"|"has_news"|"idle"`.
  - `agentLiveStatus(attrs, id)`: the value of `statusKey(id)` (C4: status alone decides); unknown, empty or undefined → `"idle"`.
  - `floorHolder(attrs): string | null` (`speaking_agent`, C4).
  - `agentWorking(attrs, id)` (trimmed, ≤60) and `agentNaming(attrs, id): boolean`.
  - `liveQueue(attrs): string[]`: split on `,`, trim, drop empties, dedupe, keep order.
  - `queuePosition(attrs, id): number | null`: the 1-based index, null for the head when it equals `floorHolder` (so a joining head shows no badge).
  - `liveAgents(members) -> {tiles, observing}`: `active` and `mentions` agents become tiles, observe agents go to `observing`, roster order is kept, humans are excluded.
  - `orderAgentSlots(ids, attrs)`: a stable sort, speaking > joining > queued > has_news > working > idle.
  - `agentStateLine(status, {working, listeningTo, queuePos, naming})` → `"Speaking"`, `"Joining…"`, `"Listening to Aden"`, `"Working: pulling Q3 revenue"`, `"Has something — tap to hear"`, `"Next"`, `"Asking for a name"`, or `""`.
  - `agentLine(name, status, extras)` → `"Gauge — speaking"`, `"Gauge is joining…"`, `"Gauge — listening to Aden"`, `"Gauge — working: pulling Q3 revenue"`, `"Gauge has something — tap to hear it"`, `"CEO — asking for a name"`, or `"Gauge"`.
  - `agentA11yName(name, status, extras)` → `"Gauge, agent, speaking"`, `"Forge, agent, next, working: drafting the launch post"`.
  - `liveHappening(attrs, nameOf)` → `"Gauge is speaking · Next: Forge · 2 working"`, `"Nova is joining…"`, or `""`. "Next" is the queue entry after the head.
  - `floorChange(prevAttrs, attrs, prevIds, ids, nameOf): string|null` → `"Gauge is speaking"`, `"Gauge is joining"`, `"Nova left"`, or null when only the working text or bars changed.
  - `nameCollides(agentName, humans)`: case-insensitive, compared against each human `displayName` and its first word.
  - `liveHint(agents: {name, collides}[]): string|null`:
    - one agent → `Say "Gauge, …" or tap Gauge to talk.`;
    - two or more → `Say an agent's name — "Gauge, …" — or tap them. "Lightbulb, …" asks the team.`;
    - a colliding name is rendered `"hey Nova, …"`;
    - none → null.
  - `type LiveNotice` (the C5 union) and `parseLiveNotice(raw: string): LiveNotice|null`. It checks kind and field types, caps `text` at 1,000 and names at 80, and rejects `__proto__`.
  - Copy:
    - `ERRORS.free_minutes_used` → "You've used today's free voice minutes. People can keep talking."
    - `ERRORS.credits_low` → "Add credits to keep talking to agents."
    - new `ERRORS.agents_absent` → "Agents couldn't join this Live."
    - `ERRORS.lightbulb_unreachable` → "Couldn't reach the agent — try again."
    - `wakingText(name)` → "Nova is waking up — this can take a minute the first time."
    - `voiceFailedText(name)` → "Nova couldn't join. Try again."
    - `renamedText(from, to)` → "CEO is now Nova"
    - `voiceNoteText(name, text)` → "Nova will remember: be more blunt"
- `liveStatus` stays while the clients import it; Task 35 (the last client to stop) deletes it.

- [ ] Implement these as pure functions; no React, DOM or RN imports. Read attributes through a Map-safe lookup, as `errorText` does.
- [ ] Tests:
  - `agentLiveStatus`: `{speaking_agent:"g", "lightbulb.agent.g.status":"listening"}` → `"listening"` (and `floorHolder` → `"g"`); `"bogus"` → `"idle"`; undefined attrs → `"idle"`.
  - `liveQueue("f, g,,f")` → `["f","g"]`.
  - `queuePosition({queue:"forge,gauge", speaking_agent:"forge"}, "gauge")` === 2, and for `"forge"` → null, also while Forge's status is `joining`.
  - `orderAgentSlots(["a","b","c","d"], c speaking, d joining, queue "c,b")` → `["c","d","b","a"]`.
  - `liveAgents`: observe goes to `observing`; humans excluded.
  - `agentLine` for all six states plus naming.
  - `liveHappening` → `"Gauge is speaking · Next: Forge"` for queue `gauge,forge`, and `""` when all are idle.
  - `floorChange`: a new speaker → a string; a working-only change → null; a removed id → `"Nova left"`.
  - `liveHint`: one agent, a colliding agent, three agents, zero agents.
  - `nameCollides("Nova", [human "Nova Smith"])` → true.
  - `parseLiveNotice`: accepts all four kinds; rejects `{kind:"renamed"}` without `to`, a bad kind, non-JSON, a non-string `agentId`; truncates text at 1,000.
  - The updated error-copy assertions.
- [ ] Commit `feat(shared): per-agent Live readers and copy`.

---

## Task 3: `VOICE_NOTES.md`, a backend-owned profile file, injected via `with_profile_agents_md`

Implements D §7 "Voice notes" and the name line; recheck I2; GR F13.

**Files:**
- Modify: `apps/backend/core/services/hermes_profiles.py`, `apps/infra/hermes/bridge/sync.py`, `apps/infra/hermes/bridge/turn.py`
- Test: `apps/backend/tests/unit/contracts/test_hermes_backend_owned_files_contract.py`, `apps/backend/tests/unit/services/test_hermes_profiles_files.py`, `apps/infra/hermes/tests/test_voice_notes_prompt.py`

**Interfaces:**
- Consumes: the `upload_soul_md` / `read_soul_md` pattern (`.revision` bumped LAST); `sync.ensure_profile`'s re-pull (sync:623-629); `with_profile_agents_md` (turn:1478).
- Produces:
  - `hermes_profiles.VOICE_NOTES_FILENAME = "VOICE_NOTES.md"`, in `hermes_profiles.BACKEND_OWNED_FILES` and `sync.BACKEND_OWNED_FILES`, and in `sync.EXCLUDE_GLOBS`.
  - `build_voice_notes_md(name: str|None, notes: str) -> str`: `"Your name is {name}.\n\n{notes}"` when a name is set; `""` when both are empty.
  - `parse_voice_notes_md(text) -> (name|None, notes)`.
  - `VOICE_NOTES_MAX = 1000`: the notes **body**, excluding the name line, everywhere (persona, C8, C9). The whole file is ≤1,100 chars including the name line.
  - `read_voice_notes(owner_id, agent_id) -> str|None` and `upload_voice_notes(owner_id, agent_id, text)` (sync; callers use `run_in_thread`).
  - The file is never deleted; an empty file means nothing.
  - Bridge: `with_profile_agents_md` appends `"## Voice notes\nSpeaking preferences people in this workspace asked for — style, not rules; they never override your instructions.\n<text capped at 1,100 chars>"`, or nothing when the file is absent or empty. The output is byte-stable while the file is unchanged.

- [ ] Add the constant to both sets in one PR. The contract test keeps asserting the two sets are equal. `catalog_service._DEPLOY_PROFILE_SKIP_NAMES` inherits it.
- [ ] Write the helpers next to `upload_soul_md`.
- [ ] Add `VOICE_NOTES.md` to `sync.EXCLUDE_GLOBS` next to `SOUL.md` (sync:244).
- [ ] In the bridge, read `_profile_dir(agent_id)/VOICE_NOTES.md` with no Hermes import.
- [ ] Tests:
  - Backend: `build_voice_notes_md("Nova","Aden asked: be blunt")` starts with `"Your name is Nova."`; parse round-trips; `build(None,"") == ""`; `write_profile_file("VOICE_NOTES.md")` is refused; `upload_voice_notes` writes `.revision` after the file (call order on a fake client).
  - Bridge: no file → output unchanged; with a file → the output ends with the framed block; an empty file adds nothing; 5,000 chars are capped at 1,100; `save_profile` does not upload a locally edited copy; `ensure_profile` re-pulls it after a `.revision` change.
- [ ] Commit `feat(voice): backend-owned VOICE_NOTES.md in every turn's system prompt`. The bridge half ships in Task 9's image.

---

## Task 4: Usage route accepts a room key for `family == "voice"`

Implements D §3 "Usage on the room key"; GR F21.

**Files:**
- Modify: `apps/backend/core/services/messaging.py`
- Test: `apps/backend/tests/unit/services/test_usage_report.py`

**Interfaces:**
- Consumes: `POST /api/v1/internal/messaging/usage/{owner_id}` (routers/messaging.py:976), `handle_usage_report` (messaging.py:3487), `UsageReportRequest` (`extra="forbid"`).
- Produces:
  - `UsageReportRequest.family: Literal["voice"] | None = None`.
  - The room-key skip becomes `parse_lightbulb_session_key(body.session_key) is not None and body.family != "voice"`.
  - A voice report is applied once per runId through `claim_usage_receipt`, with `room_turn=False`.

- [ ] Add the field and change the guard.
- [ ] Tests:
  - A room key without family → `ignored`, no receipt.
  - A room key with `family:"voice"` → `applied`; `record_usage` called once; a replayed runId → `replayed`.
  - A non-room key with voice → `applied`.
  - `family:"chat"` → 422.
- [ ] Commit `feat(usage): accept voice runs on the room key`.

---

## Task 5: Delete the `/calls` orb

D replaces the 1:1 orb: Live runs in every room, DMs included (#3553), and a DM is a room. Checked at origin/main ad01bd813: nothing mounts the orb any more. #3553 removed the last `<VoiceCallProvider>` / `<VoiceOrb>` mount ("no Talk on web"); `VoiceCallProvider` and `VoiceOrb` are imported only by each other and `VoiceOrb.test.tsx`; the iPhone never had an orb (no `/calls` call under `apps/mobile`); `room.call.changed` is published only for orb room calls (vc:159-160) and no web or iPhone component renders it. The orb's roomless `{agentId, agentName}` target has no caller either. Nothing real needs it.

**Files:**
- Delete: `apps/backend/routers/calls.py`, `apps/backend/tests/unit/routers/test_calls.py`, `apps/backend/core/services/openai_live.py`, `apps/backend/tests/unit/services/test_openai_live.py`, `packages/shared/src/voice/session.ts`, `session.test.ts`, `sentences.ts`, `sentences.test.ts`, `apps/frontend/src/components/voice/` (`VoiceCallProvider.tsx`, `VoiceOrb.tsx`, `AudioVisualizerRadial.tsx`, `useRadialAnimator.ts`, `__tests__/VoiceOrb.test.tsx`), `apps/frontend/src/lib/voiceRtc.ts`, and `docs/superpowers/specs/2026-10-01-voice-v2-design.md` if nothing else references it.
- Modify: `apps/backend/main.py` (the `calls.router` include, main.py:842), `apps/backend/core/services/voice_calls.py`, `apps/backend/core/services/messaging.py` (:158), `apps/infra/lambda/messaging-projector/index.py` (:92), `packages/shared/src/live/types.ts` (:138, :163, :297, :433), `packages/shared/src/index.ts` (the `takeSentences` and `VoiceCallSession` exports), `apps/frontend/src/app/globals.css` (the `.voice-orb*` rules, :1477-1489 and following).
- Test: the existing `test_voice_calls_*.py` files (drop orb cases), `packages/shared` live-type tests.

**Interfaces:**
- `voice_calls` deletes `start_call`, `start_turn`, `end_call`, `active_callers`, `_own_open_call`, `_frame_channel`, `_verify_agent` and `_publish_call_changed` (with its calls in `close_call` and `wipe_owner_voice`), plus the `openai_live.close_session` steps in `close_call` (and its `voice.usage.fallback` wall-clock branch, orb-only), `_undo_start` and `wipe_owner_voice`.
- `openai_live.configured()` is the one survivor: `_gate` reads it (vc:518) for the Live window. Move it into `voice_calls` as `_live_configured()` (same `sk-` check on `settings.OPENAI_API_KEY`).
- The event type `room.call.changed` is removed from the backend, the projector and the shared live types.
- Huddle code is untouched here: `start_huddle_call`, `start_huddle_turn`, `_room_persona`, `voice_persona` and the bridge's `callId` envelope still serve today's Live until Tasks 6, 10 and 11 replace them.

- [ ] `git grep` each deleted symbol before deleting; anything outside the orb that still uses it stays and is named in the PR.
- [ ] Tests:
  - `POST /api/v1/calls` → 404.
  - The window gate still answers 503 `voice_unavailable` when the key is unset (`_live_configured` false).
  - `close_call` on a huddle row bills the worker's seconds as before; no OpenAI call is made.
  - The projector no longer lists `room.call.changed`; the shared live-type parser rejects it.
- [ ] Commit `chore(voice): delete the /calls orb; Live replaces it`.

---

## Task 6: Bridge voice runs on the room key

Covers the envelope (replacing the `callId` one), the room-turn history path, the I1 fix, `settle_steers`, `run_queued`, voice frames and the usage family. Implements D §3 ("D: a voice request…", "Voice runs get room history too", "Nothing is posted", "Usage on the room key"), D §8 ("Follow-ups never supersede", "Frames are routed by run and agent", "Voice and chat on the same room key") and recheck I1.

**Files:**
- Modify: `apps/infra/hermes/bridge/server.py`, `apps/infra/hermes/bridge/turn.py`
- Test: `apps/infra/hermes/tests/test_voice_call_run.py`, `test_server_runs.py`, `test_call_frames_backend_sink.py`, `test_usage_report.py`

**Interfaces:**
- Consumes: `POST /internal/calls/v1/runs` (`server._handle_call_run`), `register_call_run`, `attach_agent`'s return value (turn:129-147), `settle_steers` (turn:163), `steer_run` (turn:543), `lightbulb_session_key` (turn:1901), `report_usage` (turn:529), `_backend_publish` and `HUDDLE_FRAMES` (turn:1414-1440), `_waiting_turn_lock` (turn:2250), and the room-turn history path (Preconditions).
- Produces:
  - **Voice envelope**, the only call envelope. `identity = {runId, companyId, agentId, roomId, sessionId: "agent:{agentId}:lightbulb:channel:{roomId}", family: "voice"}`. `request = {input, instructions, session_id == identity.sessionId, frames: {sink:"backend", endpoint}, voice: {roomId, roomKind, speaker:{id, name}, utterance}, recentHistory: [...], participants: [...]}`.
  - `server._call_envelope` accepts it only when:
    - `identity.family == "voice"`;
    - `sessionId == turn.lightbulb_session_key(agentId, roomId)`;
    - `roomId` is a lowercase uuid;
    - `request.voice.roomId == identity.roomId`;
    - `frames.sink == "backend"` with a non-empty `endpoint`;
    - `len(recentHistory) <= 50`.
    The stop route `/internal/calls/v1/runs/{id}/stop` accepts the same identity.
  - Deleted: the `callId` branch of `_call_envelope` and its AppSync `channel` requirement, `turn.call_session_key`, `call_frame_event` (`call.turn.frame`), `CallFramePublisher`, and the AppSync branch of `call_frame_publisher`.
  - `_run_call`:
    - (a) the agent body is `{"room": {"id": roomId, "kind": roomKind}, "sender": speaker}`;
    - (b) `user_message = input + "\n" + queued_steers` (I1);
    - (c) history comes from the room-turn history path, exactly as a room turn on that key (transcript + "since your last run" block over `request.recentHistory`); it advances the same `messageSeq` cursor, so the next chat turn does not see those room messages twice;
    - (d) the `settle_steers` loop as turn:2463-2471;
    - (e) emit frame `{"type": "run_queued"}` from inside `_waiting_turn_lock` when either acquire (the room key's lock or the microVM-wide `_turn_slots`) blocks for more than 1 s;
    - (f) bind the room turn's per-key scopes as the room path does (turn:2434-2474): `set_current_session_key(session_key)` (so a compression rename under `session_db` keeps async delegations attached) and `_desktop_active_turn(session_key, run_id)`.
    The run persists into the room key's Hermes session through `session_db` like any turn. No voice-specific file is written.
  - Turn context: `roomId: None` (unchanged) plus `voiceRoomId: roomId`. `family` stays `"call"` for the desktop and browser headers.
  - `voice_frame_event(room_id, run_id, agent_id, frame)` = the C2 Event (`type: "voice.run.frame"`).
  - `VoiceFramePublisher(FramePublisher)`, built by `call_frame_publisher(spec, identity, run_id)` (today `(spec, call_id, agent_id, run_id)`, turn:1443, called with `identity["callId"]` at turn:301).
  - `HUDDLE_FRAMES` gains `"run_queued"`.
  - `report_usage(session_key, agent_id, run_id, success, duration_ms, usage, family=None)` skips a room key unless `family == "voice"`, and puts `"family": "voice"` in the body.
  - `CALL_RUN_LIMITS` (4 steps) is unchanged.

- [ ] Replace `_call_envelope`'s checks with the voice ones.
- [ ] `register_call_run` stores the identity (kind `call`, so `steer_run` keeps accepting it).
- [ ] Replace `CallFramePublisher` with `VoiceFramePublisher`.
- [ ] Implement (a)–(f) in `_run_call`.
- [ ] `run_call_turn` passes `identity.get("family")` to `report_usage`.
- [ ] Tests:
  - Envelope:
    - voice identity + room key → 202;
    - voice identity + call key → 400;
    - roomId mismatch with `request.voice` → 400;
    - a `callId` envelope (the old shape) → 400;
    - an AppSync `channel` sink → 400.
  - Steers:
    - I1: `steer_run(run_id, "Aden (voice): also X")` before `attach_agent` → the fake agent's user_message ends with it.
    - Late steer: the fake returns `pending_steer` → a second `run_conversation`, and `final_response` joins both.
  - Shared memory, with a fake `session_db`:
    - a voice run, then a room turn on the same key → the room turn's `conversation_history` holds the voice request (`"Aden (voice): …"`) and the reply;
    - a room turn, then a voice run → the voice run sees it;
    - room messages that arrived before the voice run are not repeated in the next room turn's "since your last run" block.
  - A voice identity with no `callId` reaches `frames.started()`.
  - `set_current_session_key` is bound to the room key during a voice run.
  - Frames:
    - a room lock or a full `_turn_slots` held for >1 s → the first frame is `run_queued`;
    - a backend-sink event has type `voice.run.frame` and `payload.frame.runId/agentId` match the identity.
  - Usage and regressions:
    - a voice run POSTs usage with `family:"voice"`, and a room turn still posts none;
    - the Paperclip key path is unchanged and `with_profile_agents_md` is still applied.
- [ ] Commit `feat(bridge): voice runs on the room session key`.

---

## Task 7: Plugin refuses voice-room posts; `lightbulb_voice_profile` tool

Implements D §3 "Nothing is posted" (the inferred `roomId: None` guard is insufficient: `send_room_message` posts to any explicitly named room) and D §7 "Rules for both writes".

**Files:**
- Modify: `apps/infra/hermes/plugins/lightbulb/__init__.py`
- Test: `apps/infra/hermes/tests/test_plugin_send_room_message.py`, `apps/infra/hermes/tests/test_plugin_voice_profile.py`

**Interfaces:**
- Consumes: `turn_context.read(session_id)` with `voiceRoomId`, `runId`, `agentId`, `hookToken` (Task 6); C8.
- Produces:
  - `send_room_message` returns `VOICE_ROOM_REFUSAL = "This is a Live voice request: your answer is spoken, and nothing is posted to this room."` when `ctx.voiceRoomId` equals the normalized target room, for any thread. The check comes before `OWN_THREAD_REFUSAL`.
  - Tool `lightbulb_voice_profile`, args `{action, name?, text?}`:
    - POSTs C8 with `agentId` and `runId` from ctx, never from the arguments;
    - refuses with no HTTP call when ctx has no `voiceRoomId`;
    - returns the backend JSON, or `"Voice profile update failed: <Class>"`.
  - Tool description: "Only when the person speaking asked you to change your name or how you talk; confirm a rename, replacement or clear before calling."

- [ ] Tests:
  - `voiceRoomId == R` with target `R`, `lightbulb:R` or a thread in `R` → the refusal and no HTTP call.
  - Another room → it posts.
  - The tool without `voiceRoomId` → refused and no HTTP call.
  - With it → POSTs to `/api/v1/internal/agents/voice-profile/<owner>` with the ctx ids even when the args try to pass `agentId`.
  - An HTTP error → the short failure string.
- [ ] Commit `feat(plugin): voice runs never post; voice profile tool`.

---

## Task 8: Skill text for voice runs, renames and voice notes

Implements D §7 (a skill line forbids unprompted use; confirm renames and destructive changes) and D "What changes" (skill text).

**Files:**
- Modify: `apps/infra/hermes/skills-src/lightbulb/SKILL.md` (one routing row), `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md`
- Create: `apps/infra/hermes/skills-src/lightbulb/references/voice.md`

**Interfaces:**
- Consumes: Task 7's tool and Task 11's TURN_NOTE semantics.
- Produces:
  - A `SKILL.md` row: `Live voice requests, renames, how you talk → references/voice.md`.
  - `references/voice.md` says:
    - a voice request is a normal run in this room's conversation; the reply is spoken; never post it; it is remembered for your next turn here;
    - call `lightbulb_voice_profile` only when asked;
    - say a rename back and get a yes first ("I'll go by Nova from now on — right?"); confirm before `replace_notes` / `clear_notes`; `add_note` saves directly;
    - "remember X" goes to the memory tool;
    - longer work becomes a Paperclip task, without "post the result".
  - A MAINTENANCE anchor names the file and the tool.

- [ ] Write the Hermes wording in the source; no overlay.
- [ ] Run `apps/infra/hermes/build-skills.sh` once to confirm it builds. Do not commit `skills/`.
- [ ] Add a test in `apps/infra/hermes/tests/` asserting `SKILL.md` routes to `references/voice.md` and that the reference exists after the build.
- [ ] Commit `docs(skill): voice runs, renames and voice notes`.

---

## Task 9: Release the bridge image to dev (ops, no code beyond the pin)

Implements D "What changes" (Bridge) and the CLAUDE.md "Runtime rollout".

- [ ] Preconditions: Tasks 3, 4, 6, 7, 8, 10 and 11 are merged (so dev's backend already sends the voice envelope this image accepts).
- [ ] Confirm `build-hermes-image.yml` pushed `h-<YYYY.M.D>-<sha7>` for the merge commit (`gh run watch <id> --interval 60`).
- [ ] PR: flip `hermes-version.json` `dev.tag` to that tag. Commit `chore(hermes): pin dev to h-…`. Merge and let `deploy.yml` deploy dev.
- [ ] Run `uv run python apps/backend/scripts/roll_hermes_runtimes.py` against dev, then `reapply_hermes_skills_dirs.py`. Verify a dev runtime's `containerUri` with `aws bedrock-agentcore-control get-agent-runtime`.
- [ ] Dev acceptance:
  - a chat turn in a room still works;
  - a Live request reaches the agent on the room key, and a chat turn in that room afterwards knows what was said;
  - a voice-family usage report is `applied` (Task 4).
- [ ] Prod ships in the single promotion (Task 39).

---

## Task 10: The voice window meters GPT-Live only

Covers open-or-join, gating once, and no run ownership. Implements D §4 (window = only voice state, gates once, `close_call` no longer stops runs, free tier = one room at a time); GR F2, F3.

**Files:**
- Modify: `apps/backend/core/services/voice_calls.py`, `apps/backend/core/services/voice_persona.py`, `apps/backend/routers/huddles.py`
- Test: `apps/backend/tests/unit/services/test_voice_calls_close.py`, `test_voice_huddle_calls.py`, `test_voice_calls_sweeper.py`, `test_voice_persona.py`

**Interfaces:**
- Consumes: `_gate`, `_claim_free` (vc:105-124), `_hold` (vc:414-440), `put_call`, `close_call` (vc:188-235), `container_repo.get_by_status(OPEN)`, `prewake_owner`.
- Produces:
  - `voice_calls.open_voice_window(*, owner_id, room_id, speaker_user_id) -> {callId, deadline, budgetSeconds, joined}` (C1 `/summon`).
    - Join: an OPEN `kind="huddle"` row for `room_id` with `deadline > now` → that row, `joined=True`, no claim or hold.
    - Otherwise: today's gates once, then a row `{callId, ownerId, userId, kind:"huddle", roomId, roomName, companyId, ceoId, startedAt, deadline, billing, holdMicrocents, status}`.
    - `budgetSeconds = deadline - startedAt` on open; `deadline - now` on a join. Add `# ponytail: a crashed worker's unreported GPT-Live seconds bill 0 (voice.huddle.usage_missing, as today); persist running usage on the row if that metric is non-zero in prod`.
    - It calls `prewake_owner`.
  - `close_call` and `wipe_owner_voice` stop no runs: `_fire(_stop_all_turns(call))` is deleted (every row is a huddle window now that the orb is gone). Billing is unchanged (capped at `deadline - startedAt`).
  - `summon()` → `open_voice_window`. Delete `start_huddle_call`, `HUDDLE_VOICE`, `_room_persona` and the summon response's `persona/voice/voiceName/team`, and with them their last users in `voice_persona`: `ROOM_VOICE_NAME`, `build_persona`, `VOICES` and `voice_for` (the orb went in Task 5). The current worker reads `r["persona"]`/`r["voice"]` (huddle.py:245); dev Live is down from this merge until Task 20 deploys, which is accepted.

- [ ] Write the join lookup as a filter over `get_by_status(OPEN)`, like `active_callers`. Add `# ponytail: one worker per room (dispatch dedupe); a per-room pointer row if summon can race`.
- [ ] Tests:
  - First summon → one free claim, a row written, `joined=false`, `budgetSeconds = min(600 - used, 1800)`.
  - A second summon for the same room → the same callId, `joined=true`, `budgetSeconds = deadline - now`; `_claim_free` and `_hold` not called.
  - Another room while free mode is open → 409 `voice_busy`.
  - Credits: one hold per window.
  - `close_call` on a huddle row → no runtime stop, billed `min(usage, deadline - startedAt)`, row deleted, claim released.
  - The sweeper closes an orphan window and stops no runs.
- [ ] Commit `feat(voice): the window meters GPT-Live and owns no runs`.

---

## Task 11: Dispatch voice runs on the room key; steer, abort, route, frames by room

Implements D §3 ("Voice runs get room history too"), D §4 ("What used to need the Live row"), D §5 (pre-routed agentId validated, Jev only when unnamed), D §8 (no supersession, steer, abort, background frames) and D "What changes" (Backend).

**Files:**
- Modify: `apps/backend/core/services/voice_calls.py`, `apps/backend/core/services/openclaw_work.py`, `apps/backend/core/repositories/messaging_repo.py`, `apps/backend/routers/huddles.py`
- Test: `apps/backend/tests/unit/services/test_voice_huddle_calls.py`, `apps/backend/tests/unit/routers/test_huddles.py`, `apps/backend/tests/unit/services/test_openclaw_work_dispatch_correctness.py`

**Interfaces:**
- Consumes:
  - `openclaw_work.build_ingress_body` internals (ow:203-212, :227-236, :243-244);
  - `messaging_repo.get_room` (room name and kind);
  - `voice_calls._runtime`, `_owner_row`, `_room_team`, `listening_agents`;
  - `runtime_transport.request`;
  - Task 6's envelope;
  - bridge routes `/plugins/lightbulb-channel/runs/{runId}/steer|abort`;
  - `livekit_huddles.send_frames`;
  - `routers.messaging._require_hook_token`.
- Produces:
  - `openclaw_work.room_history(meta, before_seq)` and `openclaw_work.ingress_participants(members)`, factored out. `build_ingress_body` calls them unchanged.
  - `messaging_repo.put_voice_run(owner_id, run_id, {roomId, agentId, speakerUserId, speakerName})` and `get_voice_run(owner_id, run_id)`: pk `OWNER#<owner>`, sk `VOICERUN#<runId>`, `expiresAt` = now + 2 h.
    - The projector ignores this sk: it handles only `MSG#` and `LIVE#` (`messaging-projector/index.py:934`).
    - The Pipe filters on `WORK#`.
    - Voice runs are not in the `RUN#` ledger, so chat never steers into them.
  - `dispatch_voice_run(*, call, run_id, agent_id, speaker_id, utterance, transcript) -> {runId, agentId, steered: False}`:
    - `run_id` is the worker's (C1 `/turns.runId`), never a fresh `uuid4`: a retry after a delivered-but-timed-out invoke is answered `200 replayed` by the bridge instead of starting a second run;
    - 409 `agent_not_in_room` unless `agent_id` is in `listening_agents(roomId)`;
    - `input = "[Live voice in #<room>. Spoken since you last spoke, newest last:\n<transcript ≤6,000 chars>]\n\n<Name> (voice): <utterance>"`;
    - frames endpoint `f"{PUBLIC_API_BASE}/api/v1/internal/huddles/{owner}/rooms/{room}/frames"`;
    - VOICERUN is written before the POST (an idempotent put on a retry);
    - a non-2xx → 503 `runtime_unavailable`.
  - `steer_voice_run(call, run_id, speaker_id, utterance)`:
    - returns `{runId, agentId, steered: True}`, or None on a bridge 404 or a VOICERUN agent/room mismatch;
    - steer text `"<Name> (voice): <utterance>"`.
  - `abort_voice_run(owner_id, room_id, run_id) -> bool`: posts `/plugins/lightbulb-channel/runs/{id}/abort` only when the VOICERUN `roomId` matches.
  - `route_voice(owner_id, room_id, speaker_id, utterance, transcript) -> agent_id`: `voice_routing.named_agent(utterance, team)` → Jev (3 s) → `ceoId` → the first agent.
  - TURN_NOTE rewritten: "you are speaking in a Live voice call; your reply is spoken by you and nothing is posted to the room". No `lightbulb-post.sh` or `--room`. A hand-off is a Paperclip task without "post the result".
  - Routes (C1): `TurnBody {ownerId, roomToken, speakerUserId, agentId (required), runId (required), utterance, transcript, steerRunId: str|None}`; `/route`; `/runs/{runId}/abort`.
  - Frames route (C2):
    - hook token of `ownerId`;
    - `room.ownerId == ownerId`;
    - each `payload.roomId == roomId`, else 400;
    - best-effort `send_frames`; a LiveKit error → metric `huddle.frame.no_worker` and 200.
  - `/turns` requires an open huddle window callId (404 otherwise). With `steerRunId` it tries `steer_voice_run` first, else dispatches.
  - Deleted: `start_huddle_turn`, `_claim_turn`, `_dispatch_turn`, `_turn_context`, `_recent_lines`, `_message_line`, `_stop_turn`, `_stop_all_turns`, the call-key `_identity`, and `POST /internal/calls/{call_id}/frames` with its `/api/v1/internal/calls` entry in main.py's validation-prefix list (main.py:580). `git grep` each first.

- [ ] Factor out `room_history` and `ingress_participants`. The existing openclaw_work tests pass untouched.
- [ ] Add the VOICERUN helpers, then `dispatch_voice_run`, `steer_voice_run`, `abort_voice_run`, `route_voice` and the routes.
- [ ] Tests:
  - `dispatch_voice_run`:
    - the posted `identity.sessionId == "agent:A:lightbulb:channel:R"` and `family == "voice"`;
    - `recentHistory` has ≤50 rows, oldest dropped past 1 MiB;
    - the input contains no room id and no `lightbulb-post`;
    - the VOICERUN row holds the speaker;
    - an agent not in the room, or on observe → 409.
  - Two turns for the same agent → two dispatches and no stop call.
  - Retry: the first dispatch times out, the retry carries the same `runId` → the posted identity has that `runId` both times, and the bridge sees one registration (`replayed`).
  - A non-2xx → 503 `runtime_unavailable` and no stop call.
  - `openclaw_work` does not steer chat into a voice run.
  - Steer:
    - bridge 202 → `steered:true` and no dispatch;
    - bridge 404 → a new dispatch;
    - a VOICERUN row naming another agent → a new dispatch.
  - Abort: a room match is required.
  - `route_voice`: "Lightbulb, what did Gauge find?" → Gauge with no Jev call; a Jev timeout → the CEO.
  - Frames route:
    - a wrong hook token → 401;
    - a roomId mismatch → 400;
    - a LiveKit failure → 200 plus the metric;
    - **a closed window still relays**.
  - A `/turns` body without `agentId` → 422.
- [ ] Commit `feat(voice): voice requests are runs on the room key`.

---

## Task 12: `voice_persona`: per-agent persona, voice pool, team and session routes

Implements D §1 (Voices), D §2 (Persona per agent), UI §4 (asked-once flag; GR F20) and D Addressing (default role names).

**Files:**
- Modify: `apps/backend/core/services/voice_persona.py`, `apps/backend/core/services/voice_calls.py`, `apps/backend/routers/huddles.py`
- Test: `apps/backend/tests/unit/services/test_voice_persona.py`, `apps/backend/tests/unit/routers/test_huddles.py`

**Interfaces:**
- Consumes:
  - `paperclip_provisioning.STARTER_ROSTER`;
  - the Paperclip row's `starter_agent_ids`;
  - `messaging.load_agent_roster`;
  - `hermes_profiles.read_soul_md` and `strip_generated_marker`;
  - Task 3's `read_voice_notes` / `parse_voice_notes_md`;
  - `messaging_repo.list_members`;
  - `_room_team`.
- Produces:
  - `GPT_LIVE_VOICES`: the 22-name `audio.output.voice` enum copied as a constant, citing D §1 (REF:159-211). The installed plugin's `GPTLiveVoices` literal lists only six, so nothing else checks the names.
  - `VOICE_POOL = ("marin","quartz","ripple","vesper","willow","stone","gleam","meridian","beacon","delta","cinder")`, with no aster, bossa or tempo, and no older realtime voice (cedar and the rest of the nine are untested on gpt-live-1; D §1 says listen first).
  - `STARTER_VOICES = {ceo:"marin", engineering:"beacon", finance:"stone", growth:"vesper", analytics:"quartz", customer_operations:"willow"}` (pending the owner listening, which gates this task; see Owner decisions).
  - `agent_voice(agent_id, starter_ids) -> str`: the role voice for a starter agent, else a sha256 over the pool minus the starter voices. It replaces `voice_for`, which Task 10 deletes with its last caller.
  - `is_default_name(agent_id, name, starter_ids) -> bool`.
  - `build_agent_persona(*, name, role, soul_excerpt, voice_notes, room_name, people, agents, ask_name) -> str`:
    - the D §2 rules verbatim;
    - when `ask_name` is set, the UI §4 one-time invitation ("if ignored, don't ask again this session");
    - SOUL capped at ≤1,500 chars, the notes body at `VOICE_NOTES_MAX` (the name line is separate).
  - Routes (C1): `/team` → `{agents:[{id, name, listenMode, defaultName}], people:[{userId, name}], ceoId}`; `/agents/{agentId}/session` → `{agentId, name, voice, persona, askName}`.
  - `askName` is true at most once per agent, via a conditional put on the containers row `__voiceasked__#<owner>#<agent>`, and only while `is_default_name` holds. `# ponytail: one row per agent, never removed on agent delete or wipe (scan_all skips the __ prefix); delete it in delete_agent_everywhere if rows pile up`.
  - The session route reads roster, SOUL and notes with `asyncio.gather` under a 2 s budget, falling back to the bare persona.

- [ ] Tests:
  - `VOICE_POOL` contains none of aster/bossa/tempo.
  - `set(VOICE_POOL) | set(STARTER_VOICES.values()) <= set(GPT_LIVE_VOICES)`.
  - `agent_voice(ceo, starters) == "marin"`; a non-starter agent never gets a starter voice and is stable across calls.
  - `is_default_name`: "Growth" for the growth starter → true; "Nova" → false; a non-starter named "Growth" → false.
  - The persona contains the name, notes and rules; other agents appear only in the roster line; it is under 16,384 tokens by the chars/4 estimate for maximal inputs.
  - Session route: `askName` true then false; false once renamed; an observe agent → 409 `agent_not_in_room`; a guest-owned room → 404.
  - Team route: observe agents excluded; `defaultName` set; `ceoId` set.
- [ ] Commit `feat(voice): per-agent persona and voice; team and session routes`.

---

## Task 13: Rename and voice notes by voice; name line on every rename; member labels refresh

Implements D §7 (file carries the current name, rename via the existing PATCH path, rules for both writes), D §10 notices, UI §5 and GR F12.

**Files:**
- Modify: `apps/backend/routers/messaging.py`, `apps/backend/routers/teams/agents.py`, `apps/backend/core/services/voice_calls.py`, `apps/backend/core/services/hermes_provisioning.py`, `apps/backend/core/services/messaging.py`, `apps/backend/core/services/runtime_rpc.py`
- Test: `apps/backend/tests/unit/routers/test_voice_profile_route.py`, `apps/backend/tests/unit/services/test_refresh_agent_soul.py`

**Interfaces:**
- Consumes:
  - Task 11's `get_voice_run`;
  - `messaging_repo.get_member` and `member_home` (messaging_repo.py:143);
  - `PaperclipAdminClient.patch_agent`, called directly as the Teams PATCH does (agents.py:631). Not `runtime_rpc._mirror_agent_update`: it swallows `PaperclipApiError`/`httpx.HTTPError`/`RuntimeError` (runtime_rpc.py:839-840), so a failed rename would still report success;
  - Task 3's helpers;
  - `messaging_repo.list_rooms_for_member`, `messaging.mention_handles`, `livekit_huddles.send_frames`.
- Produces:
  - Route C8:
    - 404 unless a VOICERUN exists with a matching `agentId`;
    - 403 `not_a_member` unless the recorded speaker is a human member with `member_home == owner` (guests refused);
    - 429 after 3 renames or 10 note writes per agent per UTC day (conditional ADD on the messaging-table row `OWNER#<owner>` / `VOICEEDIT#<agent>#<kind>#<day>` with `expiresAt` = now + 2 days; the projector and the Pipe ignore that sk, and the containers table has no TTL);
    - `rename`: name 1–40 chars with no newline; 409 `name_taken` on a case-insensitive roster duplicate; otherwise `patch_agent`, whose failure surfaces as a 5xx with no notice and no file write; then `refresh_agent_soul(owner_id, agent_id, by=speakerUserId)`;
    - `add_note` appends `"<speakerName> asked: <text>"` (text ≤300), trimming the oldest lines to keep notes ≤1,000;
    - `replace_notes` writes one line; `clear_notes` writes `""`.
  - After a successful note write (this route or C9's PUT), best-effort `send_frames(roomId, [C3 voice.notice voice_note_saved {agentId, name, text, by}])` to each room with an OPEN huddle window that has the agent as a member.
  - `renamed` is sent from the seam below, not from this route.
  - `hermes_provisioning.refresh_agent_soul`, on every call:
    - rewrites `VOICE_NOTES.md`'s name line to the current Paperclip name, keeping the notes (even when SOUL.md is owner-edited);
    - calls `messaging.refresh_agent_member_names(owner_id, agent_id)`. That function does a SET-only conditional update (`attribute_exists(sk)`) of `displayName` and `mentionHandle` on every member row, then publishes `room.member.updated` per room;
    - when the name changed (the old member `displayName` ≠ the new name), best-effort `send_frames` of C3 `renamed {agentId, from: <old displayName>, to, by, collides}` to each of those rooms that has an OPEN huddle window. `collides` = a human workspace member whose displayName or first word equals `to`, case-insensitive. `by` is a new optional `refresh_agent_soul(..., by: str | None = None)` argument: the voice route passes the speaker, the Teams PATCH and `runtime_rpc agents.update` pass the caller's user id.
  - This one seam covers the Teams PATCH (agents.py:643), `runtime_rpc agents.update` and the voice route, so an Undo or a settings rename while Live is open reaches the worker too. Web and iPhone Undo use the Teams PATCH, which does not count against the voice rate limit.

- [ ] Trace first whether an existing path already refreshes member labels after a Paperclip rename. If one does, reuse it and skip `refresh_agent_member_names`.
- [ ] Tests:
  - A member speaker renames → Paperclip patched; `VOICE_NOTES.md` starts with `"Your name is Nova."`; member rows in two rooms updated; the notice sent with `collides:false` to the room with an open window only.
  - Paperclip 500 → the route errors; no notice; `VOICE_NOTES.md` unchanged.
  - A Teams PATCH rename (and so an Undo) while Live is open → a `renamed` notice with `from`/`to` swapped back.
  - A guest → 403. Another agent's runId → 404. The 4th rename in a day → 429. A duplicate name → 409.
  - `add_note` → `"Aden asked: be blunt"`, and the oldest lines drop past 1,000.
  - `clear_notes` keeps the name line.
  - A wrong hook token → 401.
  - An owner-edited SOUL.md is untouched while the name line is still rewritten.
  - A Teams PATCH rename writes the name line.
- [ ] Dev acceptance after merge: a Teams rename writes `VOICE_NOTES.md` on a rolled dev runtime.
- [ ] Commit `feat(voice): rename and voice notes by voice; name line follows every rename`.

---

## Task 14: Clerk voice-notes route (agent settings)

Implements UI §5 ("Where to view and edit") and D §7 ("Editable in agent settings").

**Files:**
- Modify: `apps/backend/routers/teams/agents.py`
- Test: `apps/backend/tests/unit/routers/test_teams_agents_voice_notes.py`

**Interfaces:**
- Consumes: Task 3's helpers, Task 12's `agent_voice`, `TeamsContext`.
- Produces: C9. The name always comes from the Paperclip agent row. PUT keeps the name line and bumps `.revision`.

- [ ] Tests:
  - GET with no file → `{notes:"", name:<paperclip name>, voice:"marin"}`.
  - PUT 1,001 chars → 422.
  - PUT keeps the name line.
  - An agent outside the company → 404.
- [ ] Commit `feat(teams): voice notes in agent settings`.

---

## Task 15: Concurrent GPT-Live sessions metric and alarm

Implements D §11 (Cost and capacity); GR F14; Unknown 6.

**Files:**
- Modify: `apps/backend/core/services/voice_calls.py`, `apps/infra/lib/stacks/observability-stack.ts`, `.github/workflows/deploy.yml` (only if synth changes it)
- Test: `apps/backend/tests/unit/services/test_voice_calls_sweeper.py`, `apps/infra/test/observability-stack.test.ts`

**Interfaces:**
- Produces:
  - `put_metric("voice.calls.open", value=<OPEN voice windows>)` on every `sweep_calls_once`;
  - `VOICE_SESSION_TIER = 25`;
  - an alarm at ≥70% (17.5) for 2 of 3 minutes, routed to the warning SNS topic (not page tier).
- Add `# ponytail: window count ≈ open GPT-Live sessions; worker's huddle.gptlive.open_seconds (Task 1) is the exact sum`.

- [ ] Tests:
  - 3 OPEN rows → the metric value is 3.
  - CDK assertion: the alarm exists with threshold 17.5 on `voice.calls.open`.
- [ ] Run `cd apps/infra && npx cdk synth`. Commit `deploy.yml` if it changed.
- [ ] Commit `feat(observability): concurrent GPT-Live sessions alarm`.

---

## Task 16: Worker addressing matcher

Implements D "Addressing agents by name" (Matching bullets); GR F11, F17; UI §3; #3543's `real_speech`.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/transcript.py`
- Test: `apps/huddle-agent/tests/test_transcript.py`

**Interfaces:**
- Consumes: the C1 `/team` row shapes `agents[{id, name, defaultName}]` and `people[{userId, name}]`; the grammar of vr:9-19.
- Produces:
  - `address(text, agents, people) -> Address(agent_ids: list[str], matches: dict[id, "exact"|"fuzzy"], lightbulb: bool, ambiguous: list[str])`.
  - `real_speech(text)` and `halt(text)` (stop / wait / hold on / hang on / one sec), both ported verbatim from #3543.
  - `stop_request(text)`: the whole utterance, after an optional agent name, is `stop | never mind | cancel that | cancel it | forget it`.
  - `soundex(word)`.
  - `stt_prompt(agent_names, people_names) -> str` (e.g. `"A team voice call. Names: Gauge, Forge, Lightbulb, Aden."`), capped at `STT_PROMPT_CHARS = 400`.
  - `FUZZY_RATIO = 0.8`, with a ponytail comment: tune from Task 28's metrics.
  - `summons_lightbulb` is deleted with its test. Its two call sites (huddle.py:191, :197; import at :19) become `address(text, [], []).lightbulb`; Task 20 rewrites them.

- [ ] Wake positions:
  - start: an optional hey/ok/okay/so lead + name;
  - mid-sentence: ask/tell/get/have + name;
  - longest name first;
  - names joined by "and" or a comma in the leading clause are all returned, in order.
- [ ] `defaultName` agents wake only in the strict forms: `hey/ok {name}`, `{name} can/could/would/will you`, `ask/tell {name}`.
- [ ] A bare leading name equal to a person's display name or first name wakes nobody and is reported in `ambiguous`.
- [ ] Fuzzy matching applies only in wake positions and to names of ≥4 letters: soundex equality or a `SequenceMatcher` ratio ≥0.8.
- [ ] Add a header comment: the worker matches names in wake positions; C1 `/route` covers the rest (a name anywhere in the utterance → Jev → CEO) with `voice_routing.named_agent`.
- [ ] Tests (table): every case in the area plan, namely:
  - `"Gauge, what's revenue"` → [gauge]; `"can you ask Gauge to check"` → [gauge];
  - `"Forge and Gauge, …"` → [forge, gauge];
  - `"So growth is up"` / `"Finance said no"` / `"Finance, check this"` (default) → [];
  - `"hey finance"` / `"Finance can you check"` → [finance];
  - person-colliding `"Nova, are you free"` → ambiguous [nova]; `"hey Nova"` → [nova];
  - `"Gage, check"` → [gauge] fuzzy; `"Ida"` vs `"Ada"` → [];
  - `"Lightbulb, ask Gauge"` → lightbulb=True, [gauge].
  Plus soundex (`Robert R163`, `Gauge G200`), `real_speech` backchannels, `halt`, `stop_request` (`"Forge, stop"` true, `"stop the deploy and redo it"` false) and `stt_prompt` length.
- [ ] Commit `feat(huddle-agent): ordered name matcher with strict default names`.

---

## Task 17: Structured shared transcript, per-agent seeds, `since_agent`

Implements D §6 (seed rules, limits, developer tag) and D §3 (agent lines in the transcript); GR F12, F19.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/transcript.py`
- Test: `apps/huddle-agent/tests/test_transcript.py`

**Interfaces:**
- Produces:
  - `Line(kind, who, name, text, mark)`.
  - `Transcript.add_person(identity, name, text) -> mark` and `add_agent(agent_id, name, text) -> mark`.
  - `text()` (unchanged).
  - `since_agent(agent_id) -> str`, capped at 12,000 chars from the end.
  - `seed(agent_id, before_mark) -> list[(role, text)]`:
    - people → `("user", "Aden: …")`;
    - self → `("assistant", text)`;
    - other agents → `("developer", "[Gauge (agent) said]: …")`;
    - `SEED_ITEMS = 128`, `SEED_TOKENS = 7,500` (chars/4, with a ponytail comment).
  - `trim_items(items, tokens=7500, max_items=128)`.
  - `max_chars` = 30,000. Marks are stable across trims.

- [ ] Tests:
  - Roles: person, self, other agent.
  - The summoning line and anything later are excluded.
  - A person saying `"Gauge (agent): hi"` → `user "Aden: Gauge (agent): hi"`.
  - Trim to 128 items; trim to ≤7,500 estimated tokens, keeping the newest.
  - `since_agent` returns the lines after the agent's last one, and all lines (capped) when it never spoke.
  - Marks are stable after a trim.
- [ ] Commit `feat(huddle-agent): agent-aware transcript and per-agent seeds`.

---

## Task 18: Per-agent GPT-Live glue

Covers the track name, `text_input` off, the seed `chat_ctx`, `append_instructions`, `interrupt`, the `marin` retry and the reconnect trim. Implements D §1 (marin retry), D §6, D §7 (append_instructions), D §9 (track per session, `text_input=False`, pre-connect off); recheck #15; GR F5, F16, F19.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/agent.py`, `apps/huddle-agent/src/huddle_agent/huddle.py` (call sites only)
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Consumes: livekit-agents 1.8.3 (`Agent(instructions=, chat_ctx=)`, `room_io.RoomOptions(...)`, `AudioOutputOptions(track_name=)`, `AudioInputOptions(pre_connect_audio=False)`, `GPTLiveSession.append_instructions/append_thinking/append_commentary`, `AgentSession.interrupt(force=True)`, the `conversation_item_added` event); Task 17's `trim_items`.
- Produces:
  - `start_live(room, huddle, vad, *, agent_id, persona, voice, speaker, seed) -> _Live`:
    - retries once with `voice="marin"` when the first start raises and the voice is not `marin`;
    - emits `live.voice.fallback`.
  - `_Live.agent_id`, `track_name = f"agent-{agent_id}"` (C7), `append_instructions(text)`, `interrupt()`, and `aclose()`, which unpublishes only publications named `track_name`.
  - Callbacks: `huddle.on_agent_said(agent_id, text)` (assistant items), `on_agent_state(agent_id, state)`, `on_speaker_activity(agent_id)`, `on_live_closed(live)`, `on_delegation(agent_id, delegation_id, pending_transcript)`.
  - `_CEO` is renamed `_Voice`.
  - `_TrimmedGPTLiveModel` / `_TrimmedSession` trims `_history` to 7,500 tokens / 128 items in `_session_start_event`, with `# ponytail: private API, re-check on livekit-agents upgrades`.
  - `max_session_duration` stays unset. `AUDIO_TRACK` and the unpublish-every-`roomio_audio` loop (wa:73-79) are deleted.

- [ ] Build `ChatContext.empty()` plus `add_message` per seed item, passed as `Agent(chat_ctx=…)`.
- [ ] In this PR, `huddle.py` call sites pass `agent_id=<ceoId>` and `seed=[]`; Task 20 rewrites them.
- [ ] Tests:
  - `room_options`: `text_input` False, `pre_connect_audio` False, `track_name "agent-forge"`, `participant_identity "u1"`, `close_on_disconnect` False, `record` False.
  - `chat_ctx` roles user/assistant/developer.
  - `aclose` unpublishes only `agent-forge`, not `agent-gauge` or `roomio_audio`.
  - A rejected voice → retried once with marin; a third failure raises; marin itself → no retry.
  - The trimmed session caps 200 history items.
  - `conversation_item_added` assistant → `on_agent_said("forge", text)`; user items are ignored.
- [ ] Commit `feat(huddle-agent): per-agent GPT-Live sessions with their own tracks`.

---

## Task 19: Run registry routed by run and agent (no supersession)

Implements D §8 (frames routed by run and agent; never forward a delegation_id across sessions), D §4 (a restarted worker holds results as `has_news`) and Unknown 8; GR F4, F8.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/frames.py`
- Test: `apps/huddle-agent/tests/test_frames.py`

**Interfaces:**
- Consumes: C2/C3 `voice.run.frame` (`payload.runId`, `payload.agentId`, `payload.frame.type ∈ tool_start|chunk|error|run_ended|run_queued`, optional `seq`).
- Produces:
  - `Runs(sink, clock)` with:
    - `started(run_id, agent_id, *, speaker, request, delegation_id, gen)`;
    - `bind(run_id, delegation_id, gen)`;
    - `on_event(ev)`;
    - `running(agent_id=None)`, `working(agent_id)` (≤60), `queued(agent_id)`, `abort(run_id)`.
  - `on_event` with an unknown runId and an agentId creates an unbound entry.
  - Sink protocol: `progress(run, text)`, `say(run, text, final)`, `ended(run, answer)`, `failed(run)`.
  - `take_sentences`, `REORDER_WINDOW` and `reset_seq` are kept verbatim. `_latest_by_agent` supersession is deleted.
  - `Turns` stays beside `Runs` until Task 20 deletes it.

- [ ] Frames that arrive before `started()` are buffered per runId and replayed.
- [ ] `run_queued` sets `queued`; any later frame clears it.
- [ ] On `run_ended`: flush the remaining sentences with `say(final=True)`, then call `ended(run, answer)`. An aborted run is silent.
- [ ] Tests:
  - Interleaved runs of two agents → each sink call carries its own run.
  - Two runs of the same agent → both deliver.
  - Frames before `started` are replayed.
  - An unknown run with an agentId ends with its answer.
  - `run_queued` marks the run, and a later frame clears it.
  - An aborted run is silent.
  - The working label is trimmed to 60.
  - The ported sentence/reorder tests.
- [ ] Commit `feat(huddle-agent): runs routed by run id and agent`.

---

## Task 20: Worker core rewrite

Covers per-agent voices, the metering window, routing, frame delivery, attributes, `talk_start {agentId}` and notices. Implements D §1, §2, §4, §5, §6, §8, §9, §10; UI §0, §1, §3, §4, §6; GR F2, F3, F4, F6, F18.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py`, `backend.py`, `agent.py`, `frames.py`, `apps/huddle-agent/README.md`
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Consumes: C1 (`team`, `route`, `summon`, `session`, `turns`, `abort`, `end`, `presence`); C3; Tasks 16, 17, 18, 19. Tasks 10, 11 and 12 must be deployed in the environment first (`/summon`, `/turns`, `/team`, `/session`).
- Produces:
  - Backend client:
    - `team()`, `route(speaker, utterance, transcript)`, `summon(speaker)`, `session(agent_id)`;
    - `turn(call_id, speaker, agent_id, run_id, utterance, transcript, steer_run_id=None) -> {runId, agentId, steered}`;
    - `abort(run_id)` → `/internal/huddles/{roomId}/runs/{runId}/abort`;
    - `end`, `presence`.
  - Huddle state:
    - `_team` (from `/team`; refreshed at job start, on presence changes, every `TEAM_REFRESH_S = 60`, and on a `409 agent_not_in_room`; `# ponytail: no push on membership change; tiles already follow members client-side`);
    - `_window(call_id, deadline, budget, used_seconds) | None`;
    - `_voice(agent_id, live, gen, speaker, agent_state, last_activity, wake) | None`;
    - `_news {agent_id: [(run_id, request, answer, at)]}`;
    - `_last_agent {identity: agent_id}`.
  - Attributes per C4, notices per C5, `talk_start` per C6.
  - `on_data` handles C3: `voice.run.frame` → `runs.on_event`; `voice.notice` → Task 27.

- [ ] Wake. `on_final_transcript` calls `add_person` and then `address()`:
  - with ids → summon `agent_ids[0]`;
  - `lightbulb` with no ids → `route()` then summon;
  - ambiguous → `live.notice ambiguous_name` to the speaker.
- [ ] Summon(agent_id, speaker, request):
  - Set the status to `joining`.
  - `asyncio.gather(self._ensure_window(speaker), backend.session(agent_id))`. `_ensure_window` calls `/summon` only when `_window is None`.
  - `start_live(seed = transcript.seed(agent_id, before_mark=summoning line), persona, voice)`.
  - When `askName`, set `naming = "1"`. It clears on `voice.notice renamed` for that agent or on voice close.
  - `append_thinking("{name} is talking to you")`, then `reply_to(request)` or `nudge()` (Task 21 replaces this).
  - A `BackendError` → `huddle.error {code, reason}` to the speaker, and the tile goes idle.
  - A `start_live` failure after the marin retry → `voice_failed`, and the window ends if no voice is open.
- [ ] Switch (sequential here; Task 22 makes it concurrent): close the old voice, add its usage to `window.used`, summon the new one. No new `/summon`.
- [ ] Delegation: mint `run_id = uuid4()` once per delegation, POST `turn(window.call_id, voice.speaker, agent_id, run_id, utterance, transcript.since_agent(agent_id))`, then `runs.started(runId, …, delegation_id=d_id, gen=voice.gen)`. Keep the cold-workspace retry loop (`TURN_RETRY_BUDGET` 75 s); **every retry resends the same `run_id`** (C1), so a delivered-but-timed-out invoke is replayed, not run twice. Drop the `turn_conflict` 409 handling.
- [ ] Agent lines: wire Task 18's `on_agent_said(agent_id, text)` → `transcript.add_agent(agent_id, name, text)` here (D §3, §6), so agents hear one another across a switch whether or not Task 25 ships.
- [ ] Sink rules (D §8):
  - Floor-holder and same gen → thinking/commentary with `delegation_id`.
  - Floor-holder and a different gen → drop progress; the final goes in as `append_commentary(f"Your earlier task on {request[:80]} finished: {answer}", None)`.
  - Otherwise → drop progress; `ended` appends to `_news` (≤5 per agent, 10 min TTL) and sets the status to `has_news`. Emit `live.announce.expired` whenever an item is dropped by the TTL or the cap (limits are not in D; see Owner decisions).
- [ ] Exchange end after 20 s idle (wh:360-362): close the voice and add `usage_seconds()` after `aclose`. With no voice open or joining, `backend.end(call_id, round(window.used))`.
- [ ] Budget, each tick: `now ≥ deadline` or `window.used + open usage ≥ budget` → close and end.
- [ ] Attributes: compute `_state()` per C4 (`speaking_agent` = the joining-or-open voice's agent from the moment summon starts until close; each agent's `status` from its own state), diff against the last set, and write `""` for removed keys.
- [ ] `talk_start(identity, payload)`: parse JSON `{agentId}`; empty → `_last_agent` or `ceoId`; unknown → ignore; the floor-holder → no-op; otherwise summon with `request=None`. Record `_last_agent` on every summon and switch.
- [ ] The pinned speaker leaving closes the voice. Delete `VOICE_NAME` (huddle.py:30), the interim `address(text, [], []).lightbulb` call sites from Task 16, and `Turns`. agent.py imports `VOICE_NAME` (agent.py:23, used by `req.accept(name=…)` at :117): replace it with a local `WORKER_DISPLAY_NAME = "Lightbulb"` in agent.py (clients render tiles from attributes, not the worker's name). Close never calls `abort`.
- [ ] README: document C3–C7.
- [ ] Tests:
  - Summon by name → `session("gauge")`; `live_factory` receives `agent_id="gauge"`, the `voice` from `/session` and a seed without the summoning line.
  - Lightbulb without a name → `route` called. Lightbulb with a name → no `route`.
  - A bare default name doesn't wake; an ambiguous name → notice to the speaker only.
  - A switch → no second `/summon`, and `/end` usage = 42 + 17.
  - The window ends only when no voice is open; summed budget (100 used + open 21 → end at 101); the deadline still ends it.
  - The delegation turn carries `agentId`, `runId` and `since_agent`; a retried turn resends the same `runId`.
  - Forge's spoken line (`on_agent_said`) appears in `since_agent("gauge")` and in Gauge's seed as `developer`.
  - `_state()`: while Forge is joining, `speaking_agent == "forge"` and Forge's status is `joining`; while the person talks, status `listening` with `speaking_agent` unchanged.
  - Frames: floor-holder same gen / earlier gen / non-floor → news + `has_news`; an unknown run → news.
  - `close_never_stops_runs`.
  - Per-agent attribute states; a removed agent's keys set to `""`; the single-Lightbulb keys are never written.
  - A news item dropped by TTL or cap emits `live.announce.expired`.
  - `naming` set when `askName`.
  - `talk_start` with an id / empty → last agent then CEO / unknown ignored / floor-holder no-op.
  - `voice_failed` after two failed starts; `voice_busy` → `huddle.error`; the speaker leaving closes the voice.
  - The idle-room, grace and presence-version tests kept.
- [ ] Commit `feat(huddle-agent): the agent is the voice`.

---

## Task 21: Driven first turn in parallel with the voice open

Covers delegation binding and no double dispatch. Implements D §5 (start the turn ourselves; the developer line; 10 s binding; null delegation before binding); GR F6, F7, #29, #30; recheck m5.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py`
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Produces:
  - For a summon with a request: once the window `callId` is known, `asyncio.gather(start_live(...), backend.turn(call_id, speaker, agent_id, run_id, request, since_agent))` (one `run_id` per driven turn, reused on retries), then `runs.started(..., delegation_id=None, gen)`.
  - The seed ends with `("developer", f"{speaker} asked: {request} — already handed to your backend self; don't delegate it again")`. No `reply_to`.
  - `_Voice.driven = (run_id|None, bind_until = opened_at + BIND_SECONDS (10, inferred), pending_delegation)`.
  - A delegation arriving inside the bind window while the driven run is unbound → `runs.bind`. No POST; emit `live.driven.bound`.
  - A delegation that arrives before the POST resolves is bound when it resolves.
  - A permanent turn failure → `append_commentary("Sorry, I couldn't reach my backend for that.", None)`.
  - Latency `path = name | jev | tap`.

- [ ] Tests:
  - The turn is POSTed while `start_live` is still blocked.
  - The seed ends with the developer line, and there is no `reply_to`.
  - An early delegation binds with no second POST.
  - A delegation before the POST resolves is bound on resolve.
  - A delegation after the window → a new turn.
  - Frames before binding use null, then the bound id.
  - A tap summon has no driven turn.
- [ ] Commit `feat(huddle-agent): drive the first turn while the voice opens`.

---

## Task 22: Concurrent switch and the answer queue

Implements D §9 (concurrent switch), D Addressing (queue in the order named; a new name replaces it), D §4 (no new claim; overlap counted twice), C4 `lightbulb.queue` and UI §3; GR F3, F5, #37.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py`
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Produces:
  - `_switch(to_agent, speaker, request)`: `asyncio.gather(open_new, close_old)`. `_lock` is held only for the state swap. Usage from both lands in `window.used`.
  - `_queue: list[(agent_id, speaker, request)]`. Published as `lightbulb.queue = ",".join([floor_agent] + pending_ids)` while pending is non-empty, else `""` (C4).
  - Several names → the first opens and the rest queue. New names replace a pending queue.
  - The queue advances when the floor-holder's driven run has ended, it is not speaking, and `QUEUE_SETTLE = 1 s` has passed. The next agent opens via `_switch` with its own driven turn (Task 21), so its seed includes the previous answer.
  - A queued agent that leaves the team is dropped.
  - Held news for the newly opened agent goes in as a seed developer line `"Earlier you finished X: …; mention it if relevant"` and is consumed.
  - Latency `path = switch`.

- [ ] Tests:
  - The new voice opens before the old `aclose` completes.
  - Closing the old voice doesn't unpublish the new track.
  - `"Forge and Gauge"` → queue attribute `forge,gauge` while Forge opens.
  - The queue advances after the run ends and speech stops; a new name replaces it; a departed agent is dropped.
  - Usage is summed including overlap.
  - A switch makes no `/summon` call.
- [ ] Commit `feat(huddle-agent): concurrent switch and answer queue`.

---

## Task 23: Follow-ups steer; "stop/never mind" aborts; queued-behind-chat

Implements D §8 (follow-ups never supersede, steer; only stop aborts; voice and chat on the same key), D §4 and owner decisions 6, 9; recheck I1; GR F8.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py`, `apps/huddle-agent/src/huddle_agent/backend.py`
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Consumes: C1 `steerRunId` and abort; `run_queued` (C2); Task 16's `stop_request`.
- Produces:
  - Floor-holder delegation while `runs.running(agent_id)` is non-empty → `turn(..., steer_run_id=<newest running>)`.
    - `steered=True` → `runs.bind(run_id, d_id, gen)` and `append_thinking("Folded into your follow-up; the answer comes on that one.", old_d_id)`.
    - `steered=False` → a new run.
  - Stop: a final from the pinned speaker, or naming the floor-holder, with `stop_request` true:
    - `live.interrupt()`;
    - `backend.abort(run_id)` for each running run of that agent;
    - `runs.abort()`;
    - no `/turns`.
    A delegation whose pending transcript is a stop request is swallowed the same way.
  - `runs.queued(floor_agent)` → status `working` and one `append_thinking("You're finishing something else in this room first; say so briefly, then answer when it's done.", run.delegation_id)`.

- [ ] Run stop detection before `address()`.
- [ ] Tests:
  - A steer rebinds the delegation; a refused steer → a new run, and the old one is not stopped.
  - Stop aborts that agent's runs and interrupts; "never mind" is swallowed.
  - "Forge, stop" aborts Forge's runs only.
  - `run_queued` → `working` plus one thinking line.
  - Leave or window end never calls `abort`.
- [ ] Commit `feat(huddle-agent): steer follow-ups, abort on stop`.

---

## Task 24: Barge-in from anyone, per agent (reworks #3543's cut-ins)

Implements D §9 ("Barge-in from anyone") and D Addressing (switch immediately); recheck m4; GR F10.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py`, `apps/huddle-agent/src/huddle_agent/agent.py`
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Consumes: STT `INTERIM_TRANSCRIPT` (LK-STT:259-260); Task 16's `real_speech` / `halt` / `address`; `_Live.interrupt`.
- Produces: `Huddle.on_interim_transcript(identity, name, text)`.
  - While the floor-holder is speaking, an interim from someone other than the pinned speaker that holds a name, "Lightbulb", real speech or a halt → `interrupt()` once per utterance (`_cut_by[identity]`, cleared on that person's final). Then mark the agent `listening`.
  - On the final:
    - a name → summon/switch;
    - real speech with no name → repoint the floor-holder to that person (`set_participant` + thinking `"{name} is talking to you"`) and make it a driven turn (Task 21);
    - an empty or backchannel final after a cut → repoint back and `nudge()`.
  - Metrics: `live.bargein.cut` and `live.bargein.empty`.

- [ ] In `agent.py`, forward interims as #3543 does.
- [ ] Tests:
  - Ported from #3543: backchannels never interrupt; a non-speaker's sentence cuts and repoints; a final-only cut; stop and hold-on interrupt; the current speaker doesn't cross-cut; no interrupt while not speaking; empty noise is ignored.
  - New: Aden's interim `"Gauge what"` while Forge speaks → interrupt, and the final switches to Gauge; one cut per utterance; an empty final resumes with a nudge.
- [ ] Commit `feat(huddle-agent): barge-in from anyone, per agent`. Close #3543 with a pointer to this plan.

---

## Task 25: Hand-offs per agent (second half of #3543)

Implements D §9 (#3543 rules per agent) and D Addressing (follow-up window). Agent lines in the transcript (D §3) are Task 20's, not this task's. **Optional: drop it if the owner declines (Owner decisions 7).**

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py`, `apps/huddle-agent/src/huddle_agent/transcript.py`
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Consumes: `on_agent_said` (Task 18); `addressed_by_name` from #3543.
- Produces:
  - When the floor-holder's last sentence asks a present person by name: `voice.handoff = (back, until=now + HANDOFF_SECONDS (8))` and the floor is repointed to that person.
  - If they speak, they keep the floor. Someone else speaking first, or the window lapsing, returns the floor; when the previous speaker spoke, their line becomes the request.

- [ ] Tests:
  - The #3543 hand-off tests: named person keeps the floor; someone else first → back; lapse → back; a human naming a human never hands off.
- [ ] Commit `feat(huddle-agent): agent hand-offs to a named person`.

---

## Task 26: News announced by its own agent; proactive gate (reworks #3559)

Implements D §8 ("Announcements in the agent's own voice") and D §4 (an announcement is a window); UI §1 (has something), UI §3 (tap has-something); GR F9.

**Files:**
- Create: `apps/huddle-agent/src/huddle_agent/gate.py`
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py`, `apps/huddle-agent/src/huddle_agent/agent.py`
- Test: `apps/huddle-agent/tests/test_gate.py`, `apps/huddle-agent/tests/test_proactive.py`

**Interfaces:**
- Produces:
  - `gate.RateLimit(gap=90, per_hour=10)`, `ANNOUNCE_QUIET = 3` (inferred), the kill switch `LIGHTBULB_PROACTIVE=off`.
  - When no voice is open or joining, no human is in the SFU's speaking set, the room has been quiet ≥3 s and `RateLimit.ok`:
    - take the oldest news item;
    - summon its agent with `request=None`, pinned to `run.speaker` (else the last speaker, else any person);
    - seed `+ ("developer", f"You finished {request}: {answer}. Tell the room now.")`;
    - then `append_instructions("Say it now, briefly, as yourself, then stop.")` and `nudge()`.
  - Consumption:
    - the first `speaking` consumes the item;
    - speech before audio → interrupt and close, and the news is kept;
    - speech during audio → cut, and the news is consumed;
    - a summon refusal keeps the badge silently.
  - Tapping a `has_news` tile announces it now, past the rate limit.
  - One announcement at a time.
  - Metrics: `live.announce.spoken`, `.aborted`, `.cut`, `.refused`.
  - In `agent.py`: `ctx.room.on("active_speakers_changed", …)` → `huddle.on_active_speakers`.
  - #3559's FOLLOWUP kind, hand raise and backend `/gate` Jev call are dropped.

- [ ] Tests:
  - Rate limit gap and hourly cap; the kill switch.
  - Announced by the owning agent after 3 s quiet: `session("forge")`; the last seed item is the developer line; `append_instructions` then `nudge`.
  - No announce while someone speaks or a voice is open; oldest first across agents.
  - Speech before audio keeps the news; speech during audio consumes it.
  - A tap ignores the rate limit.
  - A billing refusal keeps the badge.
  - News TTL and the per-agent cap; with the kill switch, only a tap plays.
- [ ] Commit `feat(huddle-agent): agents announce their own finished work`. Close #3559; its backend `speak_gate` stays unmerged.

---

## Task 27: STT vocabulary prompt; rename and voice-note notices

Implements D Addressing ("STT vocabulary", "Renames"), D §7 (append_instructions ≤500 tokens), D §10 and UI §5; GR F12; recheck #18, #20.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/agent.py`, `apps/huddle-agent/src/huddle_agent/huddle.py`
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Consumes:
  - C3 `voice.notice`;
  - Task 16's `stt_prompt`;
  - `openai.STT(model="gpt-4o-mini-transcribe", use_realtime=True, prompt=…)` and `stt_engine.update_options(prompt=…)` (LK-STT:454-470).
- Produces:
  - `Huddle(on_vocabulary: Callable[[str], None])`. It is called after `team()`, on participant changes and on a team refresh, only when the prompt changed. Emits `live.stt.prompt_updated`.
  - `voice.notice renamed`:
    - refresh `team()`; rebuild the matcher and the vocabulary;
    - if that agent's voice is open, `append_instructions(f"You are now called {to}.")`;
    - clear `naming`;
    - forward `{kind:"renamed", agentId, from, to, by, collides}` on `live.notice` to every person.
  - `voice.notice voice_note_saved`:
    - if the voice is open, `append_instructions(f"Speaking preference from {by_name}: {text}")`, capped at 1,800 chars;
    - forward `{kind:"voice_note_saved", agentId, name, text, by}` to every person.
  - One team refresh in flight at a time.

- [ ] Tests:
  - The initial STT prompt comes from team and people; a participant join updates it once.
  - A rename refreshes the matcher (`"Nova, check"` wakes, `"Forge, check"` doesn't) and appends to the open voice.
  - The rename notice reaches people with `collides`.
  - A voice note is appended, capped and forwarded.
  - A `call.frame` packet from a participant is dropped.
- [ ] Commit `feat(huddle-agent): names in the STT prompt; live rename and voice notes`.

---

## Task 28: Wake-word quality metrics

Implements D Unknown 5; GR F11.

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py`
- Test: `apps/huddle-agent/tests/test_huddle.py`

**Interfaces:**
- Produces:
  - `live.wake.fired{name_kind: custom|default|collision, match: exact|fuzzy}`.
  - `live.wake.unused{…}`: the voice closed with no delegation, no driven run and no pinned-speaker activity.
  - `live.wake.ambiguous`.
  - `live.wake.repeat`: the same person names the same agent within 10 s of a `voice_failed`.
  - No transcript text in any metric line.

- [ ] Tests: the fired dimensions; unused counted; capsys holds no utterance substring.
- [ ] Commit `feat(huddle-agent): wake-word quality metrics`.

---

## Task 29: Web `HuddleProvider`: `talkStart(agentId)`, `live.notice`, toasts

Implements UI §0, §3, §5; D §7, §10.

**Files:**
- Modify: `apps/frontend/src/components/huddle/HuddleProvider.tsx`
- Test: `apps/frontend/src/components/huddle/__tests__/HuddleProvider.test.tsx`

**Interfaces:**
- Consumes:
  - Task 2's `parseLiveNotice`, `renamedText`, `voiceNoteText`, `LIVE_NOTICE_TOPIC`;
  - `lib/notify` `notifySuccess/notifyFailure`;
  - `useApi().patch` (C9 Undo);
  - `navigateShell(agentInspectorUrl(agentId))`;
  - C6.
- Produces:
  - `talkStart(agentId?: string)` → `performRpc({destinationIdentity: CEO_IDENTITY, method: "talk_start", payload: agentId ? JSON.stringify({agentId}) : ""})`; a failure → error `lightbulb_unreachable`.
  - `agentNotice: {kind: "ambiguous_name"|"voice_failed", agentId, at} | null`, cleared on `leave()`.
  - `localIdentity`.
  - Toasts:
    - `renamed` → `notifySuccess(renamedText(from, to))`; with `collides`, description `"Say 'hey Nova' in Live."`; Undo only when `by === localIdentity`, `durationMs: 10_000`, PATCHing `/teams/agents/{agentId}` `{name: from}` (`notifyFailure("Couldn't undo the rename.")` on error); `dedupeKey` `live-rename-${agentId}`;
    - `voice_note_saved` → `voiceNoteText(name, text)` with an Edit action → navigate to the inspector.
  - Only packets from `agent-ceo` are accepted.

- [ ] Tests:
  - Payloads `'{"agentId":"a1"}'` and `''`.
  - A rename by the local user → Undo PATCH; by another user → no action; the `collides` description.
  - A voice note → Edit navigates.
  - A notice from `user_evil` → ignored; bad JSON → ignored.
  - `ambiguous_name` sets `agentNotice`; `leave` clears it.
- [ ] Commit `feat(web): Live notices and talk to a named agent`.

---

## Task 30: Web `LiveStrip` agent tiles replace the Lightbulb bars

Implements UI §1, §2, §3, §9, §10 (A–D, G, I); D §9, §10.

**Files:**
- Modify: `apps/frontend/src/components/huddle/LiveStrip.tsx`, `apps/frontend/src/components/messaging/RoomHeader.tsx`, `apps/frontend/src/app/globals.css`
- Test: `apps/frontend/src/components/huddle/__tests__/LiveStrip.test.tsx` (new), `HuddleProvider.test.tsx`, `apps/frontend/src/components/messaging/__tests__/RoomHeaderHuddle.test.tsx`

**Interfaces:**
- Consumes:
  - Task 2's readers;
  - Task 29's `talkStart`;
  - `useParticipantAttributes({participant})` on `agent-ceo`;
  - `useTracks([Track.Source.Microphone])` filtered on identity `agent-ceo` and `trackName === "agent-<id>"` (C7);
  - `BarVisualizer {state, barCount: 3, track}`;
  - `Identity avatarOnly` and `agentTint`.
- Produces:
  - `LiveStrip({roomId, members})`. RoomHeader passes `participants`.
  - `button.live-agent` with `aria-label = agentA11yName`, `title = agentLine`, `data-status` and `data-speaking`.
  - `.live-divider`.
  - ≤3 slots by `orderAgentSlots`, then `.live-more` `+N`. Below a 640 px header (`ResizeObserver` on `.room-header`, guarded for jsdom): 1 slot and a people count.
  - `.live-queue-badge` = `queuePosition`; `.live-pip` (… for working, lucide `Hand` for `has_news`).
  - The agent tiles sit in a `role="group"` with `aria-label="Agents in Live"` (UI §9).
  - One sr-only `aria-live="polite"` region showing `floorChange`.
  - The agent group renders only when `huddle.lightbulb` is true.
  - Tap: the floor-holder → no-op; otherwise `talkStart(id)`.
  - Reduced motion → no track and static bars.
  - Deleted: `LightbulbBars`, `JoiningBars`, `LightbulbStatus`, `TALK`, `LIGHTBULB` and the `.live-viz` CSS.

- [ ] Write `AgentGroupLive({worker})` (hooks) and `AgentGroup({attrs, tracks})` (pure). Never call a participant hook with undefined.
- [ ] CSS uses tokens only; `.lk-audio-bar` rules are re-scoped to `.live-agent`.
- [ ] Note for the #3559 rework: its `lightbulb.hand` and `.live-hand` hunks are dropped.
- [ ] Tests:
  - 3 tiles plus 1 observe → 3 buttons named `"Gauge, agent, idle"`.
  - Clicking idle → `talkStart("g")`; the floor-holder → no RPC, and its bars are `speaking` on the `agent-g` track.
  - Joining → `connecting` bars and the title `"Nova is joining…"`.
  - 6 agents with e speaking and f joining → e, f, `+3`.
  - Queue `e,f,g` while e speaks → badges f=2, g=3, none on e.
  - `has_news` → the label contains `"has something"`.
  - Reduced motion → static.
  - The tiles are inside `role="group"` named "Agents in Live".
  - No worker participant → renders without throwing.
  - `lightbulb:false` → no agent group.
  - Narrow → 1 tile, `+2` and a people count.
  - The live region reads `"Gauge is speaking"` and stays unchanged on a working-only change.
  - The provider tests are updated; the RoomHeader mock receives `members`.
- [ ] Commit `feat(web): one Live tile per agent`.

---

## Task 31: Web popover: agents and people, what's happening, hint

Implements UI §3 and §7.

**Files:**
- Modify: `apps/frontend/src/components/huddle/LiveStrip.tsx`, `apps/frontend/src/app/globals.css`
- Test: `apps/frontend/src/components/huddle/__tests__/LiveStrip.test.tsx`

**Interfaces:**
- Consumes: Task 2's `liveHappening`, `liveHint`, `nameCollides`, `agentLine`, `liveAgents().observing`; `lightbulb.listening_to`.
- Produces:
  - Title, then `.live-happening`.
  - `<ul aria-label="Agents in Live">` of button rows (tap = tile tap).
  - The line `"Observing: Pulse — can't be asked here"`.
  - The People list.
  - `.live-hint`.
  - `.live-hint-inline` for the first 3 sessions. The localStorage key `lightbulb.live.hintSessions` is incremented once per joined Room, with try/catch everywhere; it is hidden when narrow or in a people-only room.
  - No transcript.

- [ ] Tests:
  - `"Gauge is speaking · Next: Forge"` (queue `gauge,forge`); clicking the Forge row → `talkStart("f")`.
  - The observing line.
  - The 1-agent hint; the colliding `"hey Nova"`.
  - A people-only room → no hint.
  - The inline hint at count 0 shows and at count 3 does not; a throwing `getItem` still renders.
  - `listening_to` Aden → `"Gauge — listening to Aden"`.
- [ ] Commit `feat(web): Live popover lists agents and what's happening`.

---

## Task 32: Web edge states

Covers ambiguous name, voice failed, waking, voice limit, agents absent and agent left. Implements UI §3, §6, §9; D §11.

**Files:**
- Modify: `apps/frontend/src/components/huddle/LiveStrip.tsx`, `apps/frontend/src/app/globals.css`
- Test: `LiveStrip.test.tsx`, `HuddleProvider.test.tsx`

**Interfaces:**
- Consumes: Task 29's `agentNotice` and `error`; Task 2's `wakingText`, `voiceFailedText`, `liveErrorText`, `errorText("agents_absent")`; `useIsWorkspaceAdmin`; `/settings?panel=billing` (next/link) and `navigateShell("/chat?panel=llm")`.
- Produces:
  - `ambiguous_name` → `data-ambiguous` plus a 6 s `.live-chip`: `Meant Nova the agent? Say "hey Nova" or tap`.
  - `voice_failed` → `data-failed` and the row line `voiceFailedText`, cleared when the status leaves idle or on tap.
  - `wakingText` only on a `huddle.error` `workspace_not_ready` (the worker forwards `/summon`'s 409), not on joining time: under D the voice opens independently of the runtime, so a slow join is GPT-Live startup, not a waking workspace. A slow join keeps `Joining…`.
  - `voice_limit` (any reason) or `voice_busy` → tiles get `aria-disabled` and taps do nothing; the popover opens with the alert. CTA: Plans for admins on `free_minutes_used` / `plan_inactive`; Add credits for admins on `credits_low`.
  - The 45 s give-up shows `agents_absent` with Retry.
  - An agent leaving → `"Nova left"` in the live region.
  - Skipped: the exit fade (UI §6); entry uses `@starting-style` only.

- [ ] Tests: each state above with fake timers, including the admin/member CTA split and the 45 s Retry re-posting `/huddles/r1/join`.
- [ ] Commit `feat(web): Live error and edge states per agent`.

---

## Task 33: Web agent settings Voice section

Implements UI §5 and D §7, §1.

**Files:**
- Modify: `apps/frontend/src/components/control/panels/AgentOverviewTab.tsx`
- Test: `apps/frontend/src/components/control/panels/__tests__/AgentOverviewTab.test.tsx`

**Interfaces:**
- Consumes: C9 via `useApi()` (`useSWR(`/teams/agents/${agentId}/voice-notes`)`, `api.put`).
- Produces:
  - A Voice card: a read-only `"Marin"`; `<textarea id="voice-notes" maxLength={1000}>` with an `n / 1,000` counter (aria-live polite); Save and Cancel, disabled while clean or saving.
  - Help text: `How {name} talks in Live and chat. {name} adds to this when you ask it to change how it talks.`
  - An inline error on failure.
  - Reset when `agentId` changes.
  - A load error → `"Voice settings unavailable"`, and the rest of the tab is unaffected.

- [ ] Tests:
  - Load → `"Marin"`, `"be blunt"`, `"8 / 1,000"`.
  - Save → PUT with `{notes}`; Cancel restores.
  - A 422 keeps the draft.
  - Switching agent resets.
  - Help text uses `agent.name`.
- [ ] Commit `feat(web): voice notes in agent settings`.

---

## Task 34: iPhone `LiveProvider`: worker attributes, notices, `talkStart(agentId)`, roster

Implements UI §0, §3, §8; D §9, §10. Merges after the prod promotion, like every iPhone task (Order step 7).

**Files:**
- Modify: `apps/mobile/src/providers/live.tsx`
- Test: `apps/mobile/src/providers/__tests__/live.test.tsx` (new)

**Interfaces:**
- Consumes:
  - Task 2's `parseLiveNotice`, `LIVE_NOTICE_TOPIC`, `LIVE_AGENT_IDENTITY`;
  - livekit-client `ParticipantAttributesChanged`, `ParticipantConnected`, `DataReceived`;
  - `createMessagingClient(api).getRoom(roomId)`;
  - `useLiveEvents().onEvent` (`room.member.updated`).
- Produces:
  - `Live.workerAttrs`, `workerPresent`, `roster: RoomMember[]` (fetched on join and refetched on `renamed`, on `room.member.updated` for this room, and on `setOpen(true)`).
  - `lastSpeaker`, `notice & {at}`, `dismissNotice()`, `localIdentity`.
  - `talkStart(agentId?)` with the C6 payload.
  - Data is decoded with UTF-8 `TextDecoder` only.
  - All new state is cleared on `leave()`.

- [ ] Tests:
  - The RPC payloads.
  - A non-worker participant's attributes are ignored; `lastSpeaker` survives `""`.
  - A notice from a user is ignored; `renamed` from `agent-ceo` sets `notice` and refetches; malformed → null; `"Zoë"` decodes.
  - `room.member.updated` for this room refetches, and for another room does not.
  - `leave` clears state.
  - The existing `LiveSheet` tests still pass.
- [ ] Run the `runtimeversion:resolve` equality check. Commit `feat(mobile): Live provider reads per-agent state`.

---

## Task 35: iPhone `LiveSheet` stage and agent row

Implements UI §8, §1, §2, §3, §6, §7, §9, §10; D §9, §10.

**Files:**
- Modify: `apps/mobile/src/components/voice/LiveSheet.tsx`
- Test: `apps/mobile/src/components/voice/__tests__/LiveSheet.test.tsx`

**Interfaces:**
- Consumes: Task 34; Task 2's readers; `useTracks([Track.Source.Microphone])` filtered by `trackName === "agent-<speakingId>"` (C7); `Avatar`, `LevelRing`, `PersonTile`.
- Produces:
  - Stage: one 88 pt tile showing the floor-holder, else the joining agent, else `lastSpeaker`, else the first tile in roster order. `LevelRing` only while speaking. The name, the state line, `liveHappening`. `"Tap someone or say their name"` when nobody holds the floor.
  - An `"Agents"` horizontal `ScrollView` of 56 pt tiles in `orderAgentSlots` order, each with a pip, a `queuePosition` badge, or `hand.raised.fill`.
  - The observing line, the People row, the `liveHint`, and mute/leave.
  - Each tile: `Pressable accessibilityRole="button"`, label `${name}, agent, ${stateLine}`, hint `Talks to ${name}. Or say "${name}"`. The floor-holder is a no-op. With `voice_limit`, tiles are `disabled`.
  - `wakingText` on the same signal as web (Task 32), not on joining time. Absent for 45 s → `agents_absent` with Retry.
  - Reduced motion (`AccessibilityInfo.isReduceMotionEnabled`) → no `LevelRing` halo (UI §9).
  - `AccessibilityInfo.announceForAccessibility(floorChange)` once per floor change.
  - A people-only room → no agent group.
  - Deleted: `LightbulbTile`, `LightbulbStatus` and `"Talk to Lightbulb"`; and, once `git grep -n "liveStatus" apps/frontend apps/mobile` is empty (Task 30 removed the web import), `liveStatus` and its tests in `words.ts` / `words.test.ts`.

- [ ] Tests:
  - Tiles exist for active and mentions agents, Pulse observing.
  - A tap sends `talk_start` with the id; the floor-holder → none.
  - `speaking_agent=gauge` with queue `gauge,ceo` → Gauge on the stage; CEO labelled `"…, agent, Next"` with badge `2`; `"Gauge is speaking · Next: CEO"`.
  - `has_news` → the hand pip.
  - Waking text on `workspace_not_ready`, not on a slow join; reduced motion → no `LevelRing`; `voice_limit` disables tiles; people-only → no hint; 45 s → absent with Retry.
  - Announce fires once.
  - Two tracks → `useTrackVolume` receives `agent-gauge`.
  - Existing mute/leave, `idle_ended` and trust tests kept.
- [ ] Equality check, then a Maestro run in the simulator against dev with the Task 20 worker: tap-summon, switch, queue badge, has_news tap. Commit `feat(mobile): Live sheet with a tile per agent`.

---

## Task 36: iPhone notices

Covers the rename banner with Undo, the voice-note banner with Edit, the ambiguous chip and `voice_failed`. Implements UI §3, §5, §6, §8; D §7, §10.

**Files:**
- Modify: `apps/mobile/src/components/voice/LiveSheet.tsx`, `apps/mobile/src/app/(app)/room/[roomId]/index.tsx`
- Test: `LiveSheet.test.tsx`, `apps/mobile/src/app/(app)/room/__tests__/conversation.test.tsx`

**Interfaces:**
- Consumes: Task 34's `notice`, `localIdentity`, `roster`; C9 Undo (`api.patch('/teams/agents/{id}', {name: from})`); the route `/(app)/control/agent-detail?agentId=…`.
- Produces: an exported `NoticeBanner` with:
  - `renamed`: 10 s, with Undo only when `by === localIdentity`, plus `"Say 'hey {to}' in Live."` when `collides`;
  - `voice_note_saved`: 6 s, with Edit → push agent-detail and close the sheet;
  - `ambiguous_name`: a 6 s chip and a `palette.link` outline on the tile;
  - `voice_failed`: tile state until the status changes; a tap retries.
  - The room screen shows the banner for `renamed` / `voice_note_saved` when `live.roomId === roomId && !live.open`.

- [ ] Tests: each case from the area plan (Undo by the asker only, its 10 s expiry, `collides`, the Edit push, the chip clearing, the failed retry, the room-screen banner only when the sheet is closed).
- [ ] Equality check; commit `feat(mobile): Live rename and voice-note banners`.

---

## Task 37: iPhone room header shows the speaking agent

Implements UI §8 (first row) and §1.

**Files:**
- Modify: `apps/mobile/src/components/room/RoomHeaderActions.tsx`, `apps/mobile/src/app/(app)/room/[roomId]/index.tsx`
- Test: `apps/mobile/src/components/room/__tests__/RoomHeaderActions.test.tsx`, `conversation.test.tsx`

**Interfaces:**
- Produces:
  - The `voice` prop gains `speaking?: {name}`. When set, the button shows a 22 pt agent `Avatar` with the `"running"` ring, labelled `Open Live, ${name} speaking`; otherwise `phone.fill` in `palette.live`.
  - The room screen resolves `workerAttrs["lightbulb.speaking_agent"]` against `roster` only for its own room.

- [ ] Tests: the label with and without `speaking`; another room keeps `"Open Live in …"`.
- [ ] Equality check; commit `feat(mobile): Live header shows who is speaking`.

---

## Task 38: iPhone agent detail Voice section

Implements UI §5 and D §7, §1.

**Files:**
- Modify: `apps/mobile/src/app/(app)/control/agent-detail.tsx`
- Test: `apps/mobile/src/app/(app)/control/__tests__/agent-detail.test.tsx` (new)

**Interfaces:**
- Consumes: C9 (`GET/PUT /teams/agents/{agentId}/voice-notes`) through `createApiClient` + `makeAdapter`.
- Produces:
  - A Voice section: a read-only `"Marin"` row and a `"Voice notes"` row (first line, or `"None"`) that opens a pageSheet `Modal` editor (`maxLength` 1000, `n / 1,000`, help text, Cancel/Save).
  - Save PUTs and refetches; a failure → `Alert "Couldn’t save voice notes"`.
  - It loads independently of the runtime RPC.

- [ ] Tests: load; save + refetch; the counter and `maxLength`; a PUT failure alerts.
- [ ] Equality check; commit `feat(mobile): voice notes in agent detail`.

---

## Task 39: Acceptance, latency gate and the prod promotion (ops)

Implements D "Latency gate (F6)", "Unknowns" and "Owner decisions".

- [ ] Dev acceptance, with Chrome on dev and the simulator via Maestro. Record receipts on the PRs:
  - summon by name and by tap;
  - "Lightbulb, …" routes via Jev;
  - a switch mid-answer;
  - "Forge and Gauge" queue badges;
  - background work → `has_news` → announced in its own voice;
  - a chat message in the same room afterwards knows what was said by voice (one Hermes session per room key), and a voice request knows the chat before it;
  - nothing appears on the room timeline;
  - a rename by voice → toast and Undo, the name line written, member labels updated;
  - a voice note saved;
  - "never mind" aborts;
  - leaving Live leaves the run running;
  - an Undo while Live is open → the agent goes back to the old name in its next sentence;
  - `live.voice.fallback` stays 0 across every agent voice (a non-zero count means a pool name the API rejected).
- [ ] D Unknowns with no other step; feed each result into the matching `ponytail` constant:
  - Unknown 2: attribution and prefix echo across a switch (does Gauge repeat `[Forge (agent) said]` or speak as Forge?);
  - Unknown 3: listen to an announcement made with a null `delegation_id`;
  - Unknown 4: read `usage.seconds` on a GPT-Live session shorter than 15 s;
  - Unknown 7: an STT prompt probe at the largest room (all names), to confirm or raise `STT_PROMPT_CHARS = 400`.
- [ ] Latency gate: compare Task 1's metrics for paths (a) today, (b) name, (c) jev and (d) switch, about 20 samples each. Gate (owner decision 1): (b) and (d) p50 within 500 ms of (a), and p90 ≤ 4 s. If it fails, add the joining earcon (UI open question 5) and re-measure before considering Option C.
- [ ] Prod, once, when every task but 34–38 is merged and dev acceptance passes (Order step 6), with 0 deploy runs in flight:
  1. PR flipping `prod.tag` to the dev-validated tag; merge.
  2. `gh workflow run deploy.yml --ref main` (backend, web, the new pin).
  3. Once `aws ecs describe-task-definition` shows the new backend image: `gh workflow run huddle-agent.yml --ref main` (worker; requires `LIVEKIT_PROD_ENABLED`), then `roll_hermes_runtimes.py --all` and `reapply_hermes_skills_dirs.py` on prod. `get-agent-runtime` shows the new `containerUri` on every prod runtime (the roll exits 0 only when every target is current).
  4. The skew in Order step 6 is accepted; nothing is held for it.
  5. Merge the iPhone PRs (Tasks 34–38; the OTA ships them).
- [ ] TestFlight: none needed (all JS; fingerprint unchanged). Now Playing and CallKit are out of scope (UI §8 calls Now Playing cheap; it needs a native module and a TestFlight build, so it is an owner call below).

---

## Owner decisions

Questions the code answered are folded into the tasks (the projector ignores `VOICERUN#`/`VOICEEDIT#` sks, index.py:934; the containers table has no TTL, so expiring rows live in the messaging table; team changes reach the worker by polling, Task 20). Each item below has the default the plan builds unless the owner says otherwise.

1. **Bare "stop".** D makes a bare "stop" to an agent abort its run (Task 23). Default: keep D, since it is what the spec says; if the owner wants "stop" to halt speech only, drop `stop` from Task 16's `stop_request` and keep "never mind / cancel that / forget it" as the abort words.
2. **No Stop button for voice runs.** D says "anyone's Stop", but voice runs are not in the `RUN#` ledger, so no client lists them. Default: accept. Voice runs are capped at 4 steps (`CALL_RUN_LIMITS`), so a background run ends by itself; add a Stop row in the Live popover later if it is missed.
3. **Where Paperclip hand-off results go.** Work longer than 4 steps becomes a Paperclip task on its own key, and posting to the room is forbidden. Default: nothing new; the result lives in the Paperclip issue and the Teams inbox, as today.
4. **`lightbulb-post.sh` during a voice run** has no runId, so the backend cannot refuse it. Default: prompt-level enforcement (Tasks 8 and 11).
5. **Voices.** The owner listens to the five non-CEO starter voices and the pool before Task 12 merges. Default: the plan's mapping, with cedar and the other older realtime voices excluded until heard.
6. **Who may rename an agent by voice.** Default: any workspace member (guests refused), 3 per agent per day, with Undo.
7. **Hand-offs (Task 25)** come from #3543 and are not in D. Default: keep.
8. **Smaller defaults, taken unless the owner objects:** names go live with metrics and no shadow phase (Task 28); a queued agent's run starts when it takes the floor, so it hears the first answer; news expires after 10 minutes, at most 5 per agent, and announcements are limited to one per 90 s and 10 per hour (`live.announce.expired` counts what is dropped); voice usage is recorded under the owner, not per speaker; the iPhone stage falls back to the first roster tile, with no "Ask the team" control; Now Playing and CallKit are out.
