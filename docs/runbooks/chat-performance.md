# Chat performance checks

Measure the authenticated chat workspace, including the Tauri webview. Landing
page load time and native binary version do not establish chat responsiveness
or which frontend release an already-open webview has loaded.

## Existing measurements

Backend metrics use `LightbulbAI`, with `env` and
`service=lightbulb-backend` rollups. Durations are milliseconds.

| Metric | Boundary | Does not establish |
| --- | --- | --- |
| `messaging.work.queue_wait` | SQS sent timestamp → worker processing | Canonical write → queue publication |
| `messaging.ingress.latency` | Worker ingress request → bridge response | Profile, agent construction, or model phase split |
| `messaging.rpc.latency` | Calls through `container_rpc` | Direct `runtime_transport` callers |
| `messaging.run.start_latency` | Recorded dispatch → recorded run start | Genuine first invocation in a microVM |
| `messaging.run.duration` | Recorded run start → end | First visible text, individual tools, or final browser paint |
| `messaging.reply.latency` | Matched canonical human message → backend agent reply handling | Browser delivery/paint, or requests that never reply |
| `messaging.projector.notify.latency` | Canonical creation → AppSync publication | Subscriber receipt or paint |

The projector has `service=messaging-projector` and requires its full dimension
set, including `room_kind`. Discover the exact series before querying it.
Hermes INFO logs contain model/tool durations; native Bedrock inference metrics
cover provider timing. Neither alone measures the entire user turn. Inspect
only timing fields and identifiers: do not export prompt, reply, command,
credential, or file contents from logs.

Example read-only aggregate query (replace the UTC interval, keeping it fixed
for before/after comparisons):

```bash
aws cloudwatch get-metric-statistics --profile isol8-admin --region us-east-1 \
  --namespace LightbulbAI --metric-name messaging.reply.latency \
  --dimensions Name=env,Value=dev Name=service,Value=lightbulb-backend \
  --start-time 2026-09-19T00:00:00Z --end-time 2026-09-20T00:00:00Z \
  --period 86400 --extended-statistics p50 p95
```

Repeat with `--statistics SampleCount` instead of `--extended-statistics p50 p95`
to retain the sample count. Do not add percentiles from different phases: they
are different observations. P21's five-minute threshold is an availability
alarm, not a responsive-chat target; green alarms do not prove fast interaction.

## Repeatable dev fixture

1. Use a dedicated dev account and private room; sanctioned ticket sign-in is
   in [local dev browser verification](local-dev-browser-verification.md).
   Record frontend revision, runtime image, model, viewport, client/browser
   version, loaded message count, and whether the room was cached.
2. Record a browser Performance trace using synthetic messages only. Send the
   same short no-tool request sequentially, waiting for the canonical final
   reply before the next send. Wait on response/DOM events with explicit
   deadlines, never arbitrary sleeps. Keep failures and timeouts in the result.
3. Capture submit → optimistic row, HTTP acknowledgement, first visible agent
   text, and canonical final reply separately. Use `performance.now()` for
   local elapsed times. A double animation-frame callback after a DOM change
   is a paint-opportunity estimate, not proof of physical screen presentation.
   Do not subtract monotonic timestamps from different processes.
4. Repeat typing, scrolling, room switching, opening/closing a thread, selecting
   a search result, and reacting in both a short and a long synthetic room.
   Report input-to-next-paint and long tasks separately from HTTP latency.
   Verify keyboard focus, preserved scroll position, and draft contents.
5. Run the same fixture in the actual Tauri webview after establishing its
   loaded release, preserving drafts before any reload. Browser/jsdom results
   do not prove native smoothness. Record memory pressure and concurrent build
   activity; run heavy profiles sequentially.

Label the first *observed* turn as such. A cache miss, slow request, new agent,
or process age does not prove a new microVM. Cold classification needs a
runtime-session/bridge process-start boundary plus its first invocation; warm
samples need the same confirmed process. If that evidence is absent, report
`cold/warm unknown`. Do not change idle policy or prewarm to improve a score.
Before a runtime optimization, collect at least 30 cold and 30 warm simple
turns across three agents. Rank repeatable phases exceeding roughly 20% of
the total or 1s warm / 5s cold. Change one bottleneck, rerun the same fixture,
and revert if the improvement is absent or delivery/focus/history regresses.

## Frontend regression checks

From the repository root:

```bash
pnpm --filter @lightbulb/shared test src/messaging/client.test.ts src/messaging/useRooms.reliability.test.tsx
pnpm --filter @lightbulb/frontend test src/components/messaging/__tests__/RoomPanel.rendering.test.tsx src/components/messaging/__tests__/RoomTimeline.test.tsx src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx src/__tests__/app/chatPageUrlChannel.test.tsx
```

These check unchanged history during typing/highlighting, fresh actions after
updates, initial room subscription/key-rotation catch-up, cancellable bounded
history requests, and mobile-web room links. They test behavior/render counts,
not native FPS or input latency.

Long rooms start with the newest 100 canonical messages; the Load older
messages control pages backward and preserves the visible row while prepending.
Search, old permalinks, and thread opening can request full history. A pending
reaction disables controls across the loaded room, while Markdown message bodies
remain memoized. The shared hook applies this bound only when the web adapter
opts in; native mobile retains full history until its own room UI supports
older-page loading. The legacy
`.chat-transcript` containment rule does not cover `.room-history`; applying it
without checking scroll geometry and hover-toolbar clipping is unverified.
During a rolling deploy, an older backend omits `nextBeforeSeq`; web chat then
falls back to its previous full-history read until the new API is available.

## Investigation results — 2026-09-20

The call path is:

```text
Tauri WKWebView or browser
  → /chat / ChatPanel
  → MessagingWorkspaceContext
  → RoomPanel
  → RoomTimeline
  → RoomMessageItem → MarkdownView
```

The desktop Tauri configuration loads `https://dev.thelightbulb.company/chat` as its
remote frontend. It does not create a separate native chat renderer. The Rust
local-session transport and node host are separate surfaces; their activity is
not evidence that an ordinary message click is slow.

| Interaction | Evidence | Assessment |
| --- | --- | --- |
| Typing | The shipped render fix reduced the 1,000-row compiler fixture's median render from 199.33ms to 11.88ms. Four live short-turn samples had optimistic paint around 11ms and no long tasks over 50ms. | The prior typing bottleneck was real and addressed. Native input-to-paint remains unmeasured. |
| Highlight/search/permalink | Before the fix, one target change invoked every row renderer; the regression now verifies zero unchanged body renders on highlight activation and expiry. | Verified frontend bottleneck; fixed. |
| Opening a thread | A 100/1,000-row fixture rendered one new thread row on open and zero historical rows on close. | The thread path is not currently proven to redraw all history. |
| Switching rooms | A cached room switch remounts all loaded rows. The 1,000-row synthetic profile produced 1,000 rows on each side of the switch; the fixture timed out under host memory pressure, so no timing is quoted. | The newest-page limit bounds a cold long-room mount to 100 canonical rows. Explicitly loaded older pages remain mounted until room exit; native timing still needs measurement. |
| Reaction | `reactionPending` is room-scoped and disables all loaded controls. A two-message regression confirms the lock and error behavior; the actual Markdown renderer made zero body renders on pending and failed settlement. | The current-page bound reduces controls affected on a cold long room. A separate body memo would add no benefit; revisit the controls only if native traces show this still dominates. |
| Scrolling | `.room-history` is the active scroll container. The existing `content-visibility` rule targets legacy `.chat-transcript`, so it does not apply. | Candidate only. Verify WebKit scroll anchoring, focus, hover toolbar, and intrinsic height before changing CSS. |
| Backend turn | Dev samples: ingress p50 232ms/p95 30.46s; reply p50 13.14s/p95 57.29s; queue wait p50 19ms/p95 23ms; projector notify p50 765ms/p95 1.4s. | Queue is not the observed delay. Existing metrics cannot separate microVM wake, profile restore, agent/tool setup, first token, tool loop, and profile save. |

Native verification is still incomplete: the authenticated CUA session timed out
before a safe click/scroll trace, and the loaded web release could not be
identified without reloading a user's webview. The host had substantial swap
occupancy during the synthetic 1,000-row profile, so those timeout timings are
not a desktop performance measurement.

Regression checks cover latest-page cursor boundaries, explicit older loading,
new live messages and edits arriving during page requests, cancellation,
partial cache revisits, thread/search/full-history escape paths, old links,
and scroll anchoring. These verify correctness and loaded-row count, not native
input-to-paint. Retain the same synthetic fixture to measure before/after
on a loaded desktop webview; revert the page limit if old links, thread history,
unread markers, or scroll position regress.
