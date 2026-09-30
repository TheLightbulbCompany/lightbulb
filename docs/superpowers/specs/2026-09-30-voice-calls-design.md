# Voice calls with agents — design

Status: approved in conversation 2026-09-30, awaiting written-spec review.
Delete this file when the feature ships (CLAUDE.md "Planning docs are disposable").

## Goal

A member taps **Call** on an agent (web, desktop web view, iPhone) and talks to it
full-duplex: they can interrupt, the voice backchannels, and real work — anything
that needs tools, facts or memory — is done by that Hermes agent, with the voice
narrating progress while it works.

## Owner decisions

| # | Decision |
|---|---|
| 1 | Full duplex via OpenAI `gpt-live-1`, called **directly** (OpenRouter has no realtime API). The owner adds `OPENAI_API_KEY` after web + phone ship. |
| 2 | **A call leaves nothing behind.** No transcript, no summary, no room message. Durable effects are what the agent did (issues, files, posts) and the agent's own session memory. |
| 3 | **Who gets voice:** workspaces on Lightbulb credits (`provider_choice = openrouter_managed`) pay per minute from credits; every other workspace (trial, Base, BYO key, ChatGPT OAuth) gets **10 free minutes per workspace per UTC day**. |
| 4 | Credit rate **$0.06/min** ($0.05 OpenAI × the existing 1.2 markup), billed per second. |
| 5 | **One agent per call.** Called from the agent's DM/profile. |
| 6 | **Keep live progress** ("working: browser") and sentence streaming. |
| 7 | Deadline enforcement by a one-minute sweeper (existing pattern), not EventBridge Scheduler. |

Out of scope for v1: meeting join (Recall.ai Output Media — the same client page
later), team calls/routing, voice picker, spoken stop phrase, pre-seeding chat
history, BYO OpenAI key, CallKit/background audio.

## Why this shape

- `gpt-live-1` has no tools; it **delegates** (`delegation: {type: "client"}`) and the
  client runs the work. This is Hermes's own desktop design
  (`apps/desktop/src/app/chat/composer/hooks/use-voice-live-conversation.ts`,
  `tools/voice_live.py` upstream) and we port it.
- There are **no ephemeral client keys**: the client's SDP offer is exchanged by our
  backend (`POST https://api.openai.com/v1/live/sessions`) with the platform key.
  The key never leaves the backend; it never enters a tenant runtime (the agent's
  shell can read the runtime's env and role).
- The backend can control a live session from the server by attaching a sideband
  WebSocket (`wss://api.openai.com/v1/live/sessions/{id}/attach`), sending
  `session.close`, and reading `session.closed.usage.seconds`. That makes billing
  authoritative without trusting the client. The sideband is opened only to close —
  the backend holds no per-call connection.

```
Client ──POST /calls {agentId, sdp}──▶ Backend ──▶ OpenAI POST /v1/live/sessions
   │◀── {callId, sdp answer, deadline} ─┘   (budget check, CALL row, pre-wake runtime)
   ├══ WebRTC audio + "oai-events" data channel ══▶ gpt-live-1
   │     session.delegation.created
   ▼
POST /calls/{id}/turns ──▶ Backend ──runtime_transport──▶ bridge non-room run
   ◀── call.turn.frame on /ws/<ws>/users/<callerKey> ◀── bridge FramePublisher
   └─ thinking.append (progress) / commentary.append (answer) ──▶ gpt-live-1
```

## Backend

### Routes (`routers/calls.py`, Clerk auth, org required)

**`POST /api/v1/calls {agentId, sdp}`**
1. Caller is a workspace member; `agentId` is in the workspace roster.
2. `OPENAI_API_KEY` unset → **503 `voice_unavailable`**.
3. Budget (minutes, float):
   - credits: `(remaining_microcents − RESERVE_ESTIMATE_MICROCENTS) / VOICE_MICROCENTS_PER_MINUTE`,
     read via `MetronomeFastGate.get_row` (lazy-refresh the same way `is_owner_blocked` does).
   - otherwise: `10 − usedMinutes` from `__voice__#<owner>` (one row per workspace:
     `{day, usedMinutes}`; a row whose `day` isn't today's UTC date reads as 0).
   - `deadline = now + min(budget, 30 min)`. Budget < 1 min → **402 `voice_limit`**
     with `{reason: "free_minutes_used" | "credits_low"}`.
4. `POST /v1/live/sessions` with `model: gpt-live-1`, the persona instructions (ported
   `LIVE_PERSONA`, "Hermes" → the agent's display name), `audio.output.voice: "marin"`,
   `delegation: {type: "client"}`, `transport: {type: "webrtc", sdp}` — SDP passed
   **byte-exact** (a stripped trailing CRLF 400s). Non-201 → 502 `voice_upstream`.
5. Put `__call__#<owner>#<callId>` in the containers table:
   `{sessionId, userId, agentId, startedAt, deadline, billing: "credits"|"free", status: "call_open", turnRunId?}`.
   `status: "call_open"` puts the row on the table's existing sparse `status-index`
   (other callers query that index by their own status values, so no collision).
6. Fire-and-forget the existing single-flight wake (`runtime_transport._ensure_session`
   via a `health` RPC) so the first delegation doesn't eat the ~55 s cold start.
7. Return `{callId, sdp, deadline}`.

**`POST /api/v1/calls/{callId}/turns {utterance, transcript[]}`** — see "Agent turn".

**`POST /api/v1/calls/{callId}/end`** — the caller hangs up → `close_call(callId)`.

### `close_call` (single seam, used by `/end` and the sweeper)
1. Conditional update `status: call_open → call_closing` (loser returns; idempotent).
2. Abort the in-flight turn run, if any.
3. Attach sideband, `session.close`, wait ≤15 s for `session.closed`, read `usage.seconds`.
   - Attach fails (session already gone): `seconds = min(now, deadline) − startedAt`,
     capped by the last turn/activity + 120 s; emit metric `voice.usage.fallback`.
4. Bill (below; the Metronome `transaction_id = callId` makes a retry after a crash
   between steps safe), then **delete the row** — a call leaves nothing behind, and the
   containers table has no TTL.

### Sweeper (`voice_call_sweeper`, `admitted("voice_calls", interval_s=60)`)
Queries `container_repo.get_by_status("call_open")` and calls `close_call` for any
row past `deadline`. Rows stuck in `call_closing` for >5 min (a closer crashed) are
retried the same way. `__call__#` rows are already excluded from `scan_all` by
`RESERVED_KEY_PREFIX`. Registered in `main.py` like
`browser_autosave` and listed in `system_health.BACKGROUND_TASKS`. Worst-case
overrun is one tick (~60 s).

### Billing
- **Credits:** one Metronome ingest via `MetronomeClient.ingest`:
  `{event_type: "voice_call", transaction_id: callId, customer_id: owner,
  timestamp: startedAt, properties: {seconds, agent_id}}`, plus an atomic DynamoDB
  `ADD remaining_microcents -debit` on the fast-gate row
  (`debit = ceil(seconds × VOICE_MICROCENTS_PER_MINUTE / 60)`). This adds a second
  Metronome writer; update the "sole Metronome writer" comments
  (`service-stack.ts`, `metronome_fast_gate.py`, CLAUDE.md Billing section).
- **Free:** one conditional update on `__voice__#<owner>`: same `day` → `ADD usedMinutes`;
  different `day` → `SET day = today, usedMinutes = seconds/60`.
- Rate lives once: `voice-pricing.json` at the repo root (`{"usd_per_minute": "0.06"}`),
  read by the backend and by `setup_metronome_openrouter_pricing.py`, which is
  parameterized (event type, metric, product, aggregation key, rate) rather than copied.
  Metronome metric: `SUM(seconds)` on `event_type = voice_call`, flat per-second rate.

## Agent turn

**`POST /calls/{callId}/turns {utterance, transcript[]}`**
- Row `call_open`, requester == `userId`.
- Input = the ported `VOICE_LIVE_TURN_NOTE` (verbatim, "Hermes" → agent name) +
  `[Recent spoken conversation, newest last: …]` (≤6,000 chars) + `utterance`.
- A previous `turnRunId` still running → stop it first (`/runs/{id}/stop`); the newer
  delegation supersedes.
- Invoke the bridge's non-room run (`/internal/paperclip/v1/runs` via
  `runtime_transport.request`) with a **call identity**:
  `{kind: "call", runId, agentId, callId, sessionId: "agent:{agentId}:lightbulb:call:{callId}"}`
  and `frames: {channel: "/ws/<ws>/users/<callerKey>", endpoint: APPSYNC_EVENTS_HTTP_ENDPOINT}`.
- Store `turnRunId`; return `{turnId: runId}`.

**Bridge**
- `_PAPERCLIP_SESSION_RE` / identity validation accept the call form; backend-side
  `_verify_paperclip_hermes_identity` gets a non-Paperclip branch (call row exists,
  owner matches, agent matches).
- `_run_native_paperclip` passes a `FramePublisher` for call identities instead of
  `frames=None`; the envelope is `call.turn.frame {callId, turnId, frame}` where
  `frame` reuses the room frame types (`tool_start`, `chunk`, `run_ended`, `error`).
- The runtime role may already publish to its own workspace's user channels
  (`api-stack.ts` `onPublish`: meta row's `ownerId` must equal the caller's owner) —
  no IAM or resolver change.
- Separate Hermes session per call → the agent remembers the call's earlier asks; no
  collision with room sessions.

**Shared** (`packages/shared/src/live/types.ts`): add `call.turn.frame` to the union,
allowlist and parser.

## Client — `useVoiceCall` in `@lightbulb/shared`

Platform-neutral state machine; platforms inject `{RTCPeerConnection, getUserMedia, playRemote}`.

1. `getUserMedia({audio: {echoCancellation, noiseSuppression, autoGainControl}})`,
   `createDataChannel("oai-events")` **before** the offer, gather ICE (≤10 s), `POST /calls`,
   `setRemoteDescription(answer)`.
2. Accumulate `session.input_transcript.delta` / `output_transcript.delta` (last 5 min, ≤80 fragments).
3. `session.delegation.created` → `POST /calls/{id}/turns` with the latest utterance +
   transcript; remember `delegation.id` ↔ `turnId`.
4. `call.turn.frame` for the current turn:
   - `tool_start` → `session.thinking.append {delegation_id, content: "<Agent> is working: <tool>. Not done yet."}` (on label change only)
   - `chunk` → buffer; append completed sentences as `session.commentary.append` (≤1,400 chars each)
   - `run_ended` → flush the tail; no speakable text → thinking "finished without a spoken result"
   - `error` → commentary "Sorry, I couldn't reach <Agent> for that."
   - frames for superseded turns are dropped.
5. At `deadline − 60 s`: `session.instructions.append` asking the voice to say there's about a minute left.
6. Hang up: `POST /calls/{id}/end` (the server closes the session); tear down on
   `session.closed` or after 15 s.
7. Mute toggles `session.input_audio.mute/unmute` and disables the track.

### Web (`apps/frontend`)
- Call button in the agent DM header + agent profile; in-call panel (agent name, timer,
  minutes left, mute, hang up, live "working: …" line from the same frames).
- `next.config.ts` CSP `connect-src` += `https://api.openai.com`.
- 402/503 render as a limit/unavailable card.

### iPhone (`apps/mobile`)
- `react-native-webrtc` + `@config-plugins/react-native-webrtc` (mic string:
  "Lightbulb uses the microphone for voice calls with your agents."). Verify it builds on
  Expo 57 / RN 0.86 New Architecture first; fall back to `@stream-io/react-native-webrtc`.
- Speakerphone by default (voice-chat mode routes to the earpiece).
- New fingerprint → needs a native build (TestFlight). The call button is hidden when the
  native module is absent so an OTA on an old binary cannot crash.

### Desktop (`apps/desktop`)
- `entitlements.plist` += `com.apple.security.device.audio-input`; `Info.plist` +=
  `NSMicrophoneUsageDescription`. Verified on prod after promotion (desktop is not tested on dev).

## Config

- `OPENAI_API_KEY`: Secrets Manager `lightbulb/{env}/openai_api_key`, wired to the backend
  task def in CDK; empty → 503 `voice_unavailable`.

## Verify first (spike, before building on it)

1. Attaching a sideband to a live WebRTC `gpt-live-1` session, `session.close`, and
   `session.closed.usage.seconds` behave as documented.
2. Attaching to a session that already ended (client closed tab / lost network): error
   shape — confirms the billing fallback path.
3. `react-native-webrtc` builds and connects on the iOS simulator on our Expo/RN versions.

## Testing

- Backend unit: budget math (credits/free/cap), 402/503 paths, `close_call` race (two
  closers bill once), Metronome event shape + idempotency key, free-minute counter,
  sweeper closes only past-deadline rows, turn supersede aborts the prior run,
  requester check.
- Bridge unit: call identity accepted; frames published to the given channel with the
  `call.turn.frame` envelope; room/paperclip paths unchanged.
- Shared unit: `useVoiceCall` against a fake data channel — delegation → turn POST,
  frame → thinking/commentary mapping, sentence chunking, superseded-turn drop, deadline warning.
- Dev acceptance (after the key exists): Chrome call on dev; iPhone via TestFlight;
  desktop on prod after promotion. Receipts on the PR.
