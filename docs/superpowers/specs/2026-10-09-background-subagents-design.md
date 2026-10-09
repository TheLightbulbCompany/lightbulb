# Background subagents in rooms: design

Status: draft for owner review, 2026-10-09.

## Problem

An agent's `delegate_task` starts background subagents that outlive the turn that started them.
The turn's run closes, "working…" clears, and the room shows nothing until the follow-up turn
(`bridge/followups.py`) posts the result, which can take 20+ minutes. Nobody can see the subagents,
stop them, or steer them. Seen on prod 2026-10-09: Forge said it started three research tracks,
then looked idle while three subagents ran.

## Rulings (owner, 2026-10-09)

1. Copy the Hermes desktop app. It is open source, and it decides how this looks and works.
2. Build all of it: the strip above the composer, the card on the reply, Stop, Steer, and the transcript.
3. Any room member who can see the room may Stop or Steer a subagent. This is the same rule as
   run Stop and steer today (`_resolved_run` plus `chat_gate.gate_chat`).
4. No polling unless upstream needs it. Live status is pushed. Polling is only upstream's safety-net
   snapshot and the open transcript pane.
5. Room `deliveryKey` rotation stays as it is. The backend publishes, so a removed member's old channel stays silent.

## Upstream reference

All of these files exist at our pinned tag `v2026.9.14` (`hermes-version.json` base, pulled 2026-09-16):

| Upstream | What we take from it |
|---|---|
| `tools/delegate_tool_progress.py` `_ChildProgressRelay` | The event stream: `subagent.start/progress/tool/text/thinking/complete`, sent through the parent agent's `tool_progress_callback` with `_identity_kwargs` (`task_index, task_count, goal, subagent_id, parent_id, depth, model, toolsets, child_session_id, delegation_id, tool_count`). Complete adds `status, duration_seconds, summary, failure_reason`. |
| `tui_gateway/methods_subagents.py` | `subagent.list` (snapshot fields `subagent_id, parent_id, depth, goal, delegation_id, model, started_at, status, tool_count, last_tool, accepting_steer`), `subagent.tail` (last 16 384 bytes of `_live_transcript_path`), `subagent.interrupt` (`request_hard_interrupt` on the authorized record's agent). |
| `tools/delegate_tool_registry.py` | `steer_subagent(...)`, `_active_subagents`, `_owns_subagent_record`. |
| `apps/desktop/src/store/subagents.ts` | Client state: statuses `queued, running, completed, failed, interrupted`. `timeout`/`error` map to failed and `cancelled` to interrupted. An unknown terminal status fails closed to failed. Retired ids never come back. Finished rows are pruned at the next turn start, and running background rows are kept. Stream cap 24, preview 220 characters. |
| `apps/desktop/.../status-stack/subagent-section.tsx`, `-controls.tsx`, `-transcript.tsx`, `use-subagent-snapshot.ts` | Strip: live rows only, each with spinner, goal, last line ("queued / waiting for activity"), and elapsed time (1 s tick). A row expands to Steer, Stop and transcript. The snapshot hydrates once, then re-polls every 5 s while visible and stops after 3 failures. The tail polls every 2 s while open. Steer is acknowledged as "queued", never "delivered". |
| `apps/desktop/src/components/assistant-ui/tool/delegate.tsx`, `delegate-model.ts` | The card under the `delegate_task` call: one row per child with status icon, goal, `model · duration`, and one updating activity line. A child that outlived its turn, with no live view watching, shows a static `dispatched` dot and never spins. A result status other than ok/completed renders as failed. |

**Version rule:** the bridge calls only upstream symbols present at `v2026.9.14`. The CLI monitor and
`independent_completions` are newer and are out of scope.

## Architecture

```
Hermes child ──subagent.* events──▶ parent tool_progress_callback (bridge FramePublisher.activity)
                                         │ subagent.* split off (any run state)
                                         ▼
                                  bridge SubagentRelay ──POST (hook token, ≤1/s per room+agent)──▶
                    backend /internal/messaging/subagents/{owner}/events
                                         │ validate room+agent, current deliveryKey
                                         ▼
                        AppSync room channel: room.subagent.event ──▶ web / desktop / iPhone store

client ──GET /messaging/rooms/{room}/subagents (all agent members, one invoke)▶ bridge RPC subagent.list
client ──GET  …/subagents/{id}/tail────────────────────────────────────▶ bridge RPC subagent.tail
client ──POST …/subagents/{id}/steer {text} / …/{id}/stop──────────────▶ bridge RPC subagent.steer / .interrupt
```

### Bridge (`apps/infra/hermes/bridge/`)

- **Event split.** `FramePublisher.activity` passes any event starting with `subagent.` to a
  per-process `SubagentRelay`, with the room id, agent id and session key the publisher already
  holds. It does this whether or not the publisher's run has ended. A child keeps its parent's
  callback for its whole life, so a background child's events still arrive here after the parent
  turn has closed. While the parent run is live, `subagent.*` events also stay `agent_activity`
  frames on the run, as today.
- **`SubagentRelay`** (new module `subagents.py`):
  - **Batching.** It batches per (room, agent): at most one POST per second, with up to 25 events,
    to `{LIGHTBULB_API_URL}/api/v1/internal/messaging/subagents/{owner}/events`, using the hook token.
  - **Order.** It keeps event order and stamps each event with a per-process monotonic `seq`.
  - **Coalescing.** `subagent.text` is never sent, as upstream's `_progress_subagent` skips it.
    `subagent.thinking` collapses to the last one per subagent within a batch. `start`, `progress`,
    `tool` and `complete` are never dropped.
  - **Failures.** Failures are logged and dropped, the same as frames. The snapshot heals the client.
  - **Startup.** It starts lazily, like `followups.ensure_started`. It does nothing on a warm-pool microVM.
- **RPC methods** in `server.py` `RPC_METHODS`: `subagent.list`, `subagent.tail`, `subagent.steer`
  and `subagent.interrupt`. These are ports of the upstream handlers.
  - **Authority.** It comes from the room **session key**, not from upstream's TUI transport. The
    relay records `OWNER_OF[subagent_id] = session_key` for every event it forwards; a child's events
    only ever arrive through the callback of the room turn that started it. A record in Hermes's
    `_active_subagents` is owned by a session key when `OWNER_OF` maps its id to that key.
    `subagent.list` takes a list of session keys (one per agent member), so a room costs one invoke.
  - **Rules.** As upstream, control acts on the authorized record's own agent object and never
    re-resolves a global id. Stop never touches siblings.
- **Liveness.** `/ping` already reports `HealthyBusy` while delegations live (`followups.busy()`).
  This is unchanged.

### Backend

- **`POST /internal/messaging/subagents/{owner_id}/events`.** Hook-token auth, the same as the
  run and usage callbacks.
  - **Input.** The body is `{sessionKey, events: [...]}`, up to 25 events and 64 KB. Each event's
    fields are passed through from upstream, with strings capped: preview 220 characters, goal 500.
  - **Check.** It parses room and agent from the session key and checks that the agent is a member
    of that room in this owner's workspace.
  - **Publish.** It publishes one `room.subagent.event` live event per batch on the room's current channel.
  - **Storage.** It stores nothing. AppSync is the fanout, and the runtime's registry is the truth
    while children live.
- **Product routes:**
  - `GET /api/v1/messaging/rooms/{roomId}/subagents` (snapshot for every agent member, one RPC),
    then, under `/agents/{agentId}/subagents`: `GET /{subagentId}/tail`, `POST /{subagentId}/steer {text}`
    and `POST /{subagentId}/stop`.
  - **Shape.** Envelopes are camelCase. Each subagent entry is upstream's snake_case snapshot record
    plus `agent_id`, passed through so the ported reducer reads it unchanged.
  - **Auth.** A room member (ruling 3). Steer and Stop also pass `chat_gate.gate_chat` and
    `touch_last_interactive`, as `steer_run` does.
  - **Runtime calls.** Each route makes one runtime RPC through `container_rpc`, sending the room
    session key `agent:{agentId}:lightbulb:channel:{roomId}`.
  - **Errors.** The bridge's `UNAVAILABLE` maps to `RUNTIME_UNAVAILABLE` (retryable). A
    missing or unowned subagent returns 404 `SUBAGENT_NOT_FOUND`.
  - **No session wake.** `GET` snapshot and tail never wake a parked session: if the owner's
    session estimate (`__session__#<owner>`) reads cold, they answer `{subagents: []}` /
    `{available: false}` without invoking. A cold session has no live subagents.
- **Allowlist.** These are dedicated endpoints, not entries in `_PROXY_ALLOWED_METHODS`. The
  generic proxy is owner-scoped and knows nothing about rooms.

### Shared (`packages/shared`)

- **Event type.** A live event type `room.subagent.event` is added to `live/types.ts` and to the
  parser allowlist.
- **`messaging/subagents.ts`.** A port of upstream `store/subagents.ts`: a pure reducer over events
  and snapshots, keyed by (roomId, agentId, subagentId). It carries upstream's rules unchanged:
  fail-closed terminal status, retired ids, live events win over the snapshot, and pruning at the
  parent agent's next `run_active`.
- **Client.** `createMessagingClient` gets `listSubagents`, `subagentTail`, `steerSubagent` and `stopSubagent`.
- **Hook.** `useRoomSubagents(roomId)` owns the snapshot: hydrate on room open, re-poll every 5 s
  only while the strip is visible and at least one row is live, stop after 3 failures, and resume
  on focus. `useSubagentTail(...)` polls every 2 s while its pane is open.
- **Strip rows.** Strip rows reuse the existing `StripRow` shape and `sessionClock` from
  `localSessionTranscript.ts`, so room subagents and shared-session subagents look the same.

### Web / desktop (`apps/frontend`)

- **Strip.** `RoomSubagentStrip` sits above `RoomComposer`. It shows "Subagents (N)", is
  collapsible, and lists live rows only, showing three with "+N more" as upstream does. Each row
  shows a spinner, goal, last line and elapsed time. A row expands to a Steer input ("Steer queued"
  on success), a Stop button, and the transcript tail in a `<pre>`.
- **Card.** On the run card, a `delegate_task` tool row renders a `DelegateCard`: one row per child,
  with status icon, goal, `model · duration` and the activity line. It shows the static
  `dispatched` dot after a reload when nothing live is watching.
- **Styling.** It uses the existing `NativeSessionTranscript` strip styling and the colour tokens.
  No colour literals.

### iPhone (`apps/mobile`)

The same strip goes above the room composer, and the same card goes on `RunBlock`. Both use the
shared reducer and hooks. Steer and Stop sit in the row's expanded view, matching `RunBlock`'s
steer box. The transcript opens in a native sheet.

## Failure handling

- **Lost live event:** the 5 s snapshot heals it while the strip is visible.
- **microVM dies or is recycled:** children are gone, and the next snapshot returns none. As upstream's
  `reconcileSubagentSnapshot` does at our pin, live rows missing from the snapshot are dropped, never
  marked `completed`. No spinner stays after the snapshot answers.
- **Hermes stall monitor kills a child:** the `subagent.complete` it emits carries a failed or
  stalled status, which shows as failed.
- **Steer after the child finished:** the RPC answers not-found, and the UI says the subagent already finished.
- **Removed member:** they get no events, because the backend publishes on the rotated key. Their
  product routes 403 on the membership check.

## Testing

- **Bridge:** unit tests for the relay (batching, ordering, coalescing, emission after the run ended)
  and for the four RPC handlers against a fake registry. The ownership check rejects another room's
  subagent.
- **Backend:** route tests for auth (member, non-member, removed member), the session-key/room
  mismatch, publish on the current key, and no invoke on a cold session.
- **Shared:** reducer tests ported from upstream's store behaviour (fail-closed, retired ids,
  snapshot-vs-live race, prune keeps running background rows).
- **Acceptance on dev,** with an agent driving Chrome and the simulator:
  - Ask an agent for a three-track `delegate_task`.
  - The strip shows three rows with ticking activity.
  - Open one transcript, steer one, and stop one.
  - The follow-up reply lands, and the strip clears.
  - Repeat on the iPhone simulator.

## Out of scope

Pausing spawns, the nested subagent tree view, and per-child tokens and cost (the TUI overlay).
Also out: `independent_completions`, and opening a child's own session in a new window. These can
be added later from the same event stream.

## Rollout

The backend and clients ship first; with no events they do nothing. Then a Hermes image carrying the
bridge change is pinned, dev first then prod, and runtimes are moved with `roll_hermes_runtimes.py`.
The founder runtime is rolled only while it has no live delegations.
