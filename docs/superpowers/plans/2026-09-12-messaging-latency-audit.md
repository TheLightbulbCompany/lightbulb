# Messaging latency audit — why it doesn't feel like Slack

Read of the live code on 2026-09-12 (`packages/shared/src/messaging`,
`packages/shared/src/live`, `apps/frontend/src/components/messaging`,
`apps/mobile/src/app/(app)/room`, `apps/backend/{routers,core}/messaging*`).
Every claim below has a file:line. Nothing here is measured in a browser yet —
it is a read of the call graph, and the ordering claims are structural.

---

## 0. The visible oddity in the sidebar — not a timing bug

Agents whose "Agent" tag sits flush right (Tally, Vera) and agents whose tag sits
~32px in (Atlas, Forge, Friday, Gauge, Pulse) are **two different components**:

| | rendered by | overflow menu |
|---|---|---|
| agent with **no DM room yet** | `RoomDirectory.tsx:371-377` — bare `<li>` + one button | none |
| agent with an **existing DM room** | `RoomDirectory.tsx:378` → `RoomDirectoryRow.tsx:56-58` | `.room-directory-actions` |

`.room-directory-actions` is `flex: none; opacity: 0` (`messaging.css:168`) — invisible
but permanently occupying its width inside the flex row (`messaging.css:166`). So real
room rows reserve it and roster rows don't.

Clicking Tally creates the DM, the row flips from roster to `RoomDirectoryRow`, the
"…" appears and the label jumps left. That is the whole phenomenon.

**Fix:** reserve the same width on the roster row (a spacer, or give it the same
overflow menu with Copy link / Start DM). One CSS rule.

---

## 1. Opening a conversation: four serialized waits before a single message paints

For a DM that already exists:

1. **State wipe.** `useRooms.ts:210-226` clears `messages` to `[]` on every
   `activeRoomId` change. Message history lives in plain `useState`, not SWR, so
   even a room you left five seconds ago goes blank.
2. **`GET /messaging/rooms/{id}`** (`useRooms.ts:173-181`). 4 DDB round trips in
   2 waves. The only thing the client actually needs from it to proceed is
   `deliveryKey`.
3. **Clerk template-token mint + AppSync subscribe.** `appsyncEvents.ts:322-340`
   awaits `getToken()` (Clerk `lightbulb-appsync` template) *before* sending the
   `subscribe` frame, then waits for `subscribe_success` (`:258`) to mark the
   channel live.
4. **Only now** does history load. `useRooms.ts:319-323`:
   ```ts
   if (!subscriptionPath || !liveChannels.includes(subscriptionPath)) return;
   void reconcile(0)...
   ```
   → `GET …/messages?afterSeq=0&limit=100`.

Throughout, `roomLoading` renders "Loading conversation…" (`RoomPanel.tsx:183`,
state decided at `MessagingWorkspaceContext.tsx:376`).

**The gate in step 4 is the load-bearing mistake.** The subscription exists to
deliver *future* messages. History is reconciled by `afterSeq` anyway — that is
precisely what `reconcile` is for, and `room.message.created` already re-reconciles
on a sequence gap (`useRooms.ts:364`). Waiting for the socket buys nothing and
costs two of the four waits.

## 1b. Opening an agent with no DM yet (what you did to Tally): six waits

`resolveDm` (`useRooms.ts:543-581`) must scan full member lists to find an existing
DM, so it first `await`s the whole directory fan-out (§2), then `POST /messaging/rooms`,
then `await mutateRooms()`, then steps 2-4 above. Meanwhile `creationBusy`
(`MessagingWorkspaceContext.tsx:316-327`) disables **every** roster row
(`RoomDirectory.tsx:372`) for the duration.

### 1c. Three more things the live layer does that nobody asked for

**`liveChannels` gets a new array identity on every status emission.**
`emitStatus()` (`appsyncEvents.ts:361-367`) builds a fresh array unconditionally, and
it is called from every `setChannels()` (even a no-op diff, `:127`), every
`connection_ack` (`:252`), every `subscribe_success` (`:264`), every `subscribe_error`
(`:273`) and every socket `onclose` (`:186`). `liveChannels` is an **object-identity
dependency** of the history gate (`useRooms.ts:319-323`), so an ack on the *user*
channel — or any unrelated socket event — re-fires `reconcile(0)`: a full paged
history refetch of the active room. Fixing §1 (ungating history) removes this too.

**Room switch leaves a real subscription gap.** `selectRoom` only sets
`activeRoomId` (`useRooms.ts:503-509`); `activeRoom` then goes `undefined` until
`GET /rooms/{id}` resolves, so `subscriptionRoomId` drops to `null` for a render.
The claim effect's cleanup (`useRooms.ts:243-249`) fires on that, unsubscribing the
**old** room before the new one's `deliveryKey` is even known. On a first visit
that is a full HTTP round trip with no room channel subscribed at all. Hold the old
claim until the new one is ready.

**A reconnect costs a full history replay.** `connectionRevision` is
`status.ackCount`, bumped only on `connection_ack` (`appsyncEvents.ts:244`) — once
per socket handshake, including every reconnect (keepalive timeout, laptop wake,
Wi-Fi blip). Each bump re-runs `mutateRooms()` (`useRooms.ts:118-120`, the ~2N+3
room-list query) **and** `reconcile(0)` + one `getRunStatus` per in-flight run
(`:319-323`), whether or not anything changed.

**Cold start is five serialized round trips before the first message page.**
`LiveEventsProvider` is mounted per-page (`app/chat/page.tsx:72`), not in the root
layout, and `getLiveConfig()` hard-gates socket construction
(`live/useLiveEvents.ts:48-54,74`). The chain is: Clerk loaded →
`GET /messaging/live-config` → `AppSyncEventsClient` → `await getToken()` → WS
handshake → `connection_init`/`connection_ack` → (separately) `GET /rooms/{id}` for
`deliveryKey` → `subscribe` → `subscribe_success` → **then** history.

Claiming a room is otherwise cheap and correct: `claimActiveRoomSubscription`
(`useLiveEvents.tsx:102-117`) is pure React state, `setChannels` diffs incrementally
(`appsyncEvents.ts:110-128`), and the user channel is never disturbed by a room
switch. Only a genuine socket death re-subscribes everything (`:180-188`, `:249-251`).

---

## 2. The sidebar costs ~100 DynamoDB round trips to paint

`RoomSummary` carries no `members` (`packages/shared/src/messaging/types.ts:20-32`;
backend `messaging.py:138-150`). A DM's name *is* its counterpart. So:

- `useRooms.ts:137-153` fans out **one `getRoom` per non-channel room**, in
  `Promise.allSettled`.
- Until it resolves, `roomDisplayName` falls back to literally `"Direct message"`
  / `"Group chat"` (`timeline.ts:164-165`) with a generic icon.
- The SWR key includes every DM's `roomRevision` (`useRooms.ts:133-136,154-158`),
  so **any** message in **any** DM invalidates the whole fan-out and flips
  `directoryState` to `"loading"` (`MessagingWorkspaceContext.tsx:375`).

Server side, the list itself is worse than it looks:

- `list_rooms_for_member` → 1 GSI query + batch-get = 2
- then a **serial** `get_member` + `get_read_cursor` **per room**
  (`core/services/messaging.py:964-970`) = 2N sequential
- `_user_sections` = 1

`list_rooms_for_member` already returns the member row (`messaging_repo.py:400`)
and `_room_summaries` throws it away and re-fetches it.

**20 rooms, 15 of them DMs ≈ 43 + 15×4 = ~103 DDB round trips to paint a sidebar.**
Every DDB call is an `asyncio.to_thread` hop onto a default executor with ~6 threads
on a 2-vCPU task (`core/dynamodb.py:43`), so this doesn't just slow the sidebar — it
starves every other request on the process.

---

## 3. Every room open replays the entire history, forward, serially

`reconcile(0)` (`useRooms.ts:259-277`) pages from `afterSeq=0` in 100-message pages
and **keeps going until the server returns `nextAfterSeq: null`** — the whole room,
oldest first, each page awaited before the next. Then `RoomTimeline` renders every
single item with no windowing (`RoomTimeline.tsx:96-102`). There is no "load older"
control on the timeline at all; `onLoadHistory` is wired only to the search box
(`RoomPanel.tsx:526-527`).

`messaging_repo.list_messages_before(room_id, before_seq, limit)` already exists
(`messaging_repo.py:365`) and is only used for thread reply-scanning. It is not
exposed on the HTTP route (`routers/messaging.py:296-303` takes `afterSeq` only).

---

## 4. Sending freezes the composer until the server answers

- `send()` awaits the POST **before** clearing the draft
  (`MessagingWorkspaceContext.tsx:239-254`).
- `sendState` is `"sending"` for the whole flight → `pending` true → `editable`
  false (`RoomComposer.tsx:195-200`) → `<Textarea disabled>` (`:433`).

So you press Enter and your text sits in a frozen box while an optimistic copy of
the same text renders below it. Slack clears the box on the keystroke and lets the
message row carry the pending/failed state.

The POST it is waiting on is not cheap. Synchronously, before the 201:

| step | cost |
|---|---|
| `require_room_member` | 2 parallel gets |
| `_validate_mentions` (`messaging.py:1548`) | serial `get_member` **per mention** |
| `_validate_message_links` (`messaging.py:1397`) | `get_message_by_id` → **full query of every `MSG#` row in the room partition**, unbounded, up to 2× |
| `list_members` | 1 query |
| attachments (`messaging.py:755`) | `read_text()` + `read()` **directly on the event loop** — EFS is NFS; a 5 MiB read blocks the whole process |
| `human_display_name` (`clerk_admin.py:153-160`) | **uncached Clerk HTTPS call, fresh `httpx.AsyncClient` → new TLS handshake, every single send** |
| `create_message` | 2 gets + `TransactWriteItems` |
| `touch_last_interactive` (`messaging.py:1631`) | an extra awaited `update_item` on another table that nothing in the response needs |

Fan-out is genuinely off the response path (outbox row → DDB stream → Pipe → SQS),
which is right. The waste is all in validation and the Clerk call.

---

## 5. The typing indicator is the worst cost-to-value ratio on the surface

`routers/messaging.py:394-436`, once per second per room:
3 DDB gets (one of which re-reads the META row `require_room_member` just read),
1 uncached Clerk HTTPS call for the caller's own name, and an AppSync POST with a
**new `httpx.AsyncClient` per publish** (`live_events.py:201`) behind a
**process-global signing lock** (`live_events.py:93,137`). It should cost one
AppSync POST.

---

## 5b. Every HTTP call awaits a Clerk token before the fetch is even issued

`createAuthenticatedFetch` (`packages/shared/src/api/client.ts:174`) does
`await config.getToken()` **before** building headers or calling `fetch` — same at
`:135` and `:244`. `getToken` is Clerk's `session.getToken()`.

Clerk keeps an in-memory token cache, so this is usually free. But it evicts when
the JWT is within ~5s + leeway of its ~60s expiry — i.e. exactly after an idle tab,
a backgrounded tab, or a pause between clicks. On a miss you pay **a full round trip
to Clerk's Frontend API, serially, before the backend request starts**. Nothing
overlaps it.

That is a strong candidate for why the delay is intermittent and feels worst on the
first click after a pause. **Confirm in the Network tab: does a
`clerk.*/v1/client/sessions/.../tokens` request precede the slow
`/messaging/rooms/:id`?** If so, warming the token on hover/focus (or pre-minting on
an interval) is a cheap fix.

Two related notes from the same read:

- There is **no default timeout or AbortController** on the messaging surface
  (`client.ts:130-159` takes an optional `signal`, but `ApiMethods.get/post/...`
  never passes one). A hung request just hangs — no skeleton timeout, no retry.
- `useApi()` (`apps/frontend/src/lib/api.ts:39-51`) memoizes on Clerk's `getToken`
  identity and **relies on Clerk's internal memoization** to stay stable. Mobile
  does not trust this: it pins the getter in a ref and builds the client with
  `useMemo(..., [])` (`apps/mobile/src/providers/live-events.tsx:51-58`), with
  comments across the mobile tree noting clerk-expo hands out a new `getToken`
  every render. `messaging` is an effect dependency throughout `useRooms`
  (`:292, :317, :399, :428`) and the hook's own contract
  (`useRooms.ts:65-69`) says instability there means "Maximum update depth
  exceeded", not merely extra fetches. Web should pin it the same way
  defensively.

---

## 6. Render cost

React Compiler is on for both web and mobile (`next.config.ts:86`,
`app.json:56`), so most of the object-literal churn in
`MessagingWorkspaceContext` is handled. What it cannot reach:

- `MarkdownView` is unmemoized (`MarkdownView.tsx:183-195`) and passes a **fresh
  `remarkPlugins` array literal every render**, so react-markdown re-parses
  remark-gfm for every message on every RoomPanel render. It is invoked through
  a render-prop closure (`RoomPanel.tsx:435` → `RoomTimeline` → `items.map`),
  which the compiler can't memoize across.
- `linkifyMentions` (`mentions.ts:47-67`) sorts the token list and **compiles a new
  RegExp** per message per render (`RoomPanel.tsx:435`). It depends only on
  `participants`.
- No timeline virtualization (see §3).

RoomPanel re-renders on every composer keystroke, because `composer.text` is part
of `roomProps` (`MessagingWorkspaceContext.tsx:399-452`).

---

## 7. Mobile, on top of all of the above

- **Six independent `useRooms` mounts** (`_layout.tsx:34`, `home.tsx`,
  `agent/[agentId].tsx:15`, `room/[roomId]/index.tsx:65`,
  `room/[roomId]/thread/[messageId].tsx:62`, `NewMessageSheet.tsx`). Web mounts it
  once, in the provider.
- The room route **forces a cold remount per room**:
  `return <Conversation key={roomId ?? "none"} />` (`room/[roomId]/index.tsx:38-43`).
  Combined with React Navigation unmounting popped screens, every back-and-forth
  rebuilds the hook and re-walks the full history.
- The AppSync socket is **torn down on every backgrounding**
  (`providers/live-events.tsx:143` → `live/useLiveEvents.ts:48-54`); web never
  passes `enabled` so it never tears down. Each foreground re-fires `mutateRooms()`
  **and** the full `reconcile(0)`.
- **No persistence anywhere** — no `SWRConfig` provider, no AsyncStorage cache. Cold
  launch is always a skeleton waiting on the network.
- `AttachmentImage` (`components/room/MessageGroup.tsx:23-55`) fires one
  authenticated `downloadAttachment` per attachment per rendered message, uncached.
- `FlatList` with no `getItemLayout` / `initialNumToRender` / `windowSize`, and
  `onContentSizeChange` → animated `scrollToEnd` on every appended history page
  (`index.tsx:181-183`).

---

## Plan, ordered by perceived win per unit of effort

### Tier 1 — make opening a conversation instant

1. **Stop gating history on the live subscription.** Run `reconcile` off
   `activeRoomId`, not off `liveChannels` (`useRooms.ts:319-323`). Removes two of
   the four serial waits. The gap-recovery path already exists — and it also kills
   the spurious full-history refetches caused by `liveChannels` identity churn
   (§1c). While there, hold the outgoing room's subscription claim until the new
   one is ready, and stop `connectionRevision` from triggering a blanket
   `reconcile(0)` on every reconnect.
2. **Cache messages per room.** Replace the `setMessages([])` wipe
   (`useRooms.ts:210-226`) with a per-room map (or an SWR key). Re-opening a room
   paints from cache immediately and reconciles behind it.
3. **Put `members` + `lastReadSeq` on `RoomSummary`**, and `asyncio.gather` the
   `_room_summaries` loop while dropping the redundant `get_member`
   (`messaging.py:964-970`). Deletes the entire client fan-out (`useRooms.ts:137-153`)
   and the `"Direct message"` placeholder flash. ~103 round trips → ~3N concurrent.
   Keep `deliveryKey` out of the list — it is the channel secret.
4. **Tail-first history.** Add `beforeSeq` to `GET …/messages` (the repo function
   already exists) and load the newest ~50 on open, paging older on scroll.

### Tier 2 — make sending instant

5. Clear the composer synchronously on submit and keep it editable; move the
   retry / "check status" affordance onto the optimistic message row.
6. Send path: cache the sender's display name (the JWT already carries identity),
   `create_task` the `touch_last_interactive`, `run_in_thread` the three
   `load_image_attachment` / `store_image_attachment` call sites, and bound
   `get_message_by_id`.

### Tier 3 — sustained smoothness

7. `memo()` `MarkdownView`, hoist the `remarkPlugins` array to module scope,
   memoize `linkifyMentions` per `(participants, text)`.
8. Virtualize the timeline once tail-first loading lands.
9. Typing / live publish: reuse the META already in hand, cache the display name,
   module-level `httpx.AsyncClient` in `live_events.py` and `clerk_admin.py`.
10. Mount `LiveEventsProvider` above the chat route (or prefetch
    `/messaging/live-config`) so the socket is warming before the user lands on
    `/chat` (§1c). Pin `getToken` in a ref on web the way mobile already does, and warm the Clerk
    token so a click after an idle pause doesn't pay a serial Clerk round trip
    before the backend request (§5b). Give the messaging client a default timeout.

### Tier 4 — mobile

11. One `useRooms` at the layout level; drop the `key={roomId}` remount.
12. Grace period before tearing the socket down on background.
13. Persist the SWR cache and the active room's tail so a cold launch paints.

### Cosmetic
14. Reserve the overflow-menu width on roster rows (§0).

---

## Measured baseline — production, 2026-09-12, before any of this shipped

Taken on `https://flickmeon.ai/chat`, founder org, signed in, warm tab.

**Opening a conversation that was already in the sidebar:**

```
click → first message painted:  3047 ms

+35ms   (107ms)  GET /messaging/rooms/{id}      ← room detail, for the delivery key
        ~915ms of nothing                       ← Clerk template-token mint + AppSync subscribe ack
+951ms  (254ms)  GET /messaging/rooms           ← room list refetch
+951ms  (253ms)  GET .../messages?afterSeq=0
```

The ~915ms gap between the room-detail response and the first message request is
the gate this PR removes. Nothing is in flight during it.

**Painting the sidebar once:**

```
17  messaging requests total
10  GET /messaging/rooms/{id}   ← the per-DM fan-out, all issued in the same
                                  millisecond, 335-507ms each
 4  GET /messaging/rooms        ← the list, fetched four times
```

Two room ids appear **twice** in that fan-out, which is the `roomRevision`-keyed
SWR invalidation re-running it mid-load.

**Sidebar rows, read out of the live DOM:**

```
founder-acceptance  menu: yes      report bugs  menu: yes
Aden Kim            menu: yes      Friday       menu: yes     Prasiddha  menu: yes
Atlas               menu: NO       Forge        menu: NO      Gauge      menu: NO
Pulse               menu: NO       Tally        menu: NO      Vera       menu: NO
```

Six of eleven rows reserve no width for the overflow control the other five
reserve — the misalignment, confirmed in production rather than inferred.

**Loading screen:** bare "Loading…" text, no mark. The only brand on screen
during load is the square app-icon tile in the browser tab.
