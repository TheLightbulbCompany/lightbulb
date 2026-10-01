# Huddles — design

Status: agreed in conversation 2026-10-01 (owner: "this sounds right … you can start implementing").
Builds on Voice v2 (#3529). Research behind it: `.superpowers/sdd/2026-10-01-voice-v2/research-*.md`
in the voice-v2 worktree (multi-party agents, LiveKit vs Pipecat, LiveKit source read).
Delete this file when huddles ship.

## Goal

A channel can hold a live audio huddle: the people in the channel talk to each other, and the CEO
agent is on the call like a colleague — it follows the whole conversation, answers out loud to
everyone when someone says its name or presses Talk, and the rest of the team does the work behind
it. Google Meet comes next (Recall.ai bot bridging into the same room); it reuses everything here.

## Owner decisions

| # | Decision |
|---|---|
| 1 | **One voice: the CEO.** Other agents never speak; they work behind the CEO, which attributes ("Finance says…"). Per-agent voices are a later option, not this spec. |
| 2 | **Summoned, never ambient.** The CEO speaks only when someone says its name or holds Talk. It does not chime in on its own. |
| 3 | **It follows along.** A live, speaker-named transcript of the huddle is the CEO's context when it is summoned. |
| 4 | **Routing is v2's:** a name you say wins → Jev → the CEO, through the same Hermes call path (`voice_calls.start_turn`). Nothing huddle-specific in routing. |
| 5 | **LiveKit Cloud** for the room and the agent worker (managed; no new AWS infra). Agents UI components for the agent visual. |
| 6 | **Recall.ai for Meet**, later; our own bot only if cost/dependency demands it. |

## Experience (web + desktop app; iPhone later — LiveKit needs a native build)

- A channel header gains **Huddle**. Starting one opens a huddle bar/panel and puts the channel's
  marker up: *"🎧 Huddle — Prasiddha, Aden"*. Anyone in the channel can join or leave; leaving
  /chat leaves the huddle.
- The panel shows a tile per person (speaking ring from LiveKit) and one **CEO tile** with the
  Agents UI visualizer. States: *Following along* (transcribing), *Listening to Aden* (summoned),
  *Thinking*, *Speaking*, *Finance is working*.
- **Talk to CEO** button (hold, or click to toggle) and the name ("CEO, …" / the CEO's display
  name / "hey team") both summon it. Whoever summoned it can keep talking back and forth and can
  interrupt it; a short silence ends the exchange and the CEO goes back to following along.
- Everyone hears the CEO. Results the team produces show up in the channel as usual.
- Mute, leave. Errors in plain words (free minutes used, voice not switched on, credits low).
- A huddle leaves nothing behind (same rule as calls). No recording, no saved transcript.
- Vendor shared-channel guests cannot start or join a huddle in this version.

## How it works

```
browser ──(Clerk) POST /huddles/{roomId}/join──► backend ── mints LiveKit token, dispatches the
   │                                                        CEO worker if the room has none
   └──WebRTC──► LiveKit room "huddle-<env>-<roomId>" ◄──── CEO worker (LiveKit Cloud Agents)
                   people ↔ people audio                     │ STT per person → named transcript
                                                             │ summon: name or Talk RPC
                                                             ▼
                                         POST /internal/huddles/{roomId}/summon  (secret)
                                         → opens a voice call row (billing, free claim, persona)
                                                             │
                                  GPTLiveModel(delegation="client"), set_participant(speaker),
                                  append_thinking(transcript), hand over the request text
                                                             │ delegation_created
                                         POST /internal/huddles/calls/{callId}/turns  (secret)
                                         → voice_calls.start_turn (routing → Hermes run)
                                                             │
     Hermes bridge ── frames ──► POST /internal/calls/{callId}/frames (hook token)
                                 → LiveKit SendData(topic "call.frame", to the CEO identity)
                                                             │
                                  worker: append_thinking / append_commentary(delegation_id)
                                  idle → POST /internal/huddles/calls/{callId}/end → close_call
```

**1. Room and tokens (backend, `routers/huddles.py`, `core/services/livekit_huddles.py`).**
`POST /api/v1/huddles/{roomId}/join` (Clerk): requires channel membership in the owner's own
workspace (`messaging.require_room_member` + owner match, as v2 `_room_team`; guests 404), room not
archived, at least one non-observe agent. Mints a LiveKit token: identity = Clerk user id, name =
display name, room `huddle-<env>-<roomId>`, join/publish/subscribe/publishData, TTL 15 min, no
token dispatch. If the LiveKit room has no CEO participant, `create_dispatch(agent_name=
"lightbulb-huddle", room, metadata={roomId, ownerId, ceoId, ceoName, env})` (idempotent: check
`list_participants` first; a duplicate worker exits when it sees another CEO identity). Returns
`{serverUrl, roomName, token}`. Publishes `room.huddle.changed {roomId, userIds}` on join/leave
(the worker posts leave/membership changes; the marker is otherwise driven by
`GET /huddles/{roomId}` reading `list_participants`).

**2. The CEO worker (`apps/huddle-agent/`, forked from `livekit-examples/agent-starter-python`,
MIT; delegation handler from `livekit/agents` `examples/voice_agents/gpt-live/client_delegation.py`).**
- Joins with identity `agent-ceo`, name = the CEO's display name. Stays while any human is in the
  room; leaves when the last one goes.
- **Following along:** one streaming STT per human audio track (LiveKit STT plugin, OpenAI
  `gpt-4o-mini-transcribe`, our OpenAI key as a LiveKit secret) → a rolling transcript of the last
  ~6000 chars, each line `Name: text`. Nothing is stored.
- **Summon by name:** a final transcript line that starts with, or contains "hey/ok" +, the CEO's
  name, "CEO" or "team" (the v2 `named_agent` lead/verb rules, ported) → summon with that speaker
  and that line as the request.
- **Summon by Talk:** LiveKit RPC `talk_start` / `talk_end` from the browser; speaker =
  `caller_identity` (set by LiveKit's server, so trustworthy).
- **On summon:** `POST /internal/huddles/{roomId}/summon {speakerUserId}` → `{callId, persona,
  voice, deadline}` (the backend runs the same gates as `start_call`: plan, free budget/claim,
  credit hold, one open free call per workspace). Start `AgentSession(llm=GPTLiveModel(voice,
  delegation="client"), vad=silero)` with the persona as instructions and `RoomOptions(
  close_on_disconnect=False)` unpinned, then `room_io.set_participant(speaker)`,
  `append_thinking("Transcript so far:\n…")`, `append_thinking("<Name> is talking to you")`, and
  for a name summon hand the request over as a typed user turn (`generate_reply` path, which rides
  the newest typed message as commentary — source `gpt_live_model.py` `_generate_reply`).
- **During the exchange:** the GPT-Live session hears only the summoner (input unmuted). Other
  people's lines keep landing in the transcript and are appended as thinking every few seconds so
  the CEO stays current. A Talk press by someone else re-points `set_participant`.
- **Delegation:** `delegation_created` → `POST /internal/huddles/calls/{callId}/turns {speaker,
  utterance, transcript, seq}` → `{turnId, agentId}`; frames for that turn arrive as LiveKit data
  (topic `call.frame`) and are applied exactly as `packages/shared/src/voice/session.ts` does
  today (reorder window, tool_start → "X is working: …" thinking, chunks → commentary per
  delegation, run_ended). Port that logic to Python in the worker; keep the same frame contract.
- **Ending the exchange:** 20 s with no speech from the summoner and no running turn → mute input,
  `POST …/calls/{callId}/end`, close the GPT-Live session (it bills per second), back to following.
  Hard cap = the call's `deadline` (budget), as calls.
- Auth to the backend: `X-Lightbulb-Huddle-Secret` (one secret per env, in Secrets Manager and in
  LiveKit Cloud agent secrets). Every internal route also checks `ownerId` matches the room.

**3. Frames to the worker.** Today the bridge publishes `call.turn.frame` to the caller's AppSync
user channel. For a huddle call the turn's frame sink is `{"type": "livekit", "callId"}`: the
bridge POSTs each frame batch to `POST /api/v1/internal/calls/{callId}/frames` (hook token), and
the backend forwards it with LiveKit `RoomService.SendData(room, data, destination_identities=
["agent-ceo"], topic="call.frame")`. Stateless, any backend task. DM/room calls keep the AppSync
sink unchanged. (Bridge change → new Hermes image + roll.)

**4. Billing.** A summon is a voice call: same row, hold, free claim, `close_call`, Metronome
`voice_call` event, free daily minutes. Only GPT-Live seconds are billed. Transcription and the
LiveKit agent minutes are absorbed for now (≈$0.003/person-min + $0.01/min) and metered as
EMF metrics (`huddle.stt.seconds`, `huddle.agent.seconds`) so we can price them later.

**5. Infra.** LiveKit Cloud projects `lightbulb-dev` / `lightbulb-prod`. Secrets
`lightbulb/{env}/livekit_api_key`, `livekit_api_secret`, `huddle_worker_secret`; `LIVEKIT_URL` from
CDK env. Worker deploys with `livekit/deploy-action` in a new `huddle-agent.yml` workflow
(path-filtered on `apps/huddle-agent/**`, dev on merge, prod on dispatch). `livekit-api` added to
the backend.

**6. Frontend.** `components/huddle/`: `HuddleProvider` (above the shell like `VoiceCallProvider`,
one huddle at a time; starting a v2 call ends the huddle and vice versa), `HuddleBar` (tiles,
CEO tile, Talk, mute, leave), header button, marker via `useRoomHuddle` (seed from
`GET /huddles/{roomId}`, then `room.huddle.changed`, resync on reconnect — the `useRoomCallers`
pattern). Uses `@livekit/components-react` + `livekit-client`; the CEO visual is the Agents UI
visualizer (aura if the owner adds it, else radial) fed by LiveKit's own track volume.

## Out of scope

Per-agent voices, the CEO speaking unprompted, other people barging in on the CEO mid-answer,
recordings/summaries, iPhone, Meet (next spec, Recall.ai), vendor-channel guests.

## Testing

- Backend unit: join (membership, guest 404, archived 409, token claims, dispatch only when no
  CEO), summon gates (reuses `start_call` tests' cases), internal auth (secret + owner match),
  frames forward to SendData, `room.huddle.changed`.
- Bridge unit: livekit frame sink posts batches; AppSync sink unchanged.
- Worker unit (pytest, no network): name detection; frame application parity with
  `session.ts` cases (reorder, supersede, tool_start/chunk/run_ended); idle end; Talk RPC sets the
  speaker.
- Web: join/leave, marker, Talk button states, one-huddle-or-call rule.
- Dev acceptance: two accounts in a huddle hear each other; "CEO, what did we ship this week?"
  gets a spoken answer both hear; Talk button works; a Finance question routes to Finance and is
  attributed; hang/idle bills the summon seconds only.
