# Session Rail — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the session list appear quickly, name itself sensibly, and show what happened —
instead of taking a store-wide scan to display "your first message, truncated".

**Architecture:** Four independent defects compound here. We fetch every agent's sessions and
filter client-side because a comment in three places wrongly claims `sessions.list` cannot
filter by agent — it can. We declare two row fields the RPC has never returned. We name
sessions after the owner's first message because there is no auto-titling upstream. And
"General" is pinned above everything as an unlabelled system inbox. Fix the fetch, drop the
dead fields, have the agent title the session once, and name General for what it is.

**Tech Stack:** React 19 / Next.js 16 frontend (`vitest`, SWR), Python 3 / FastAPI backend
(`uv`, `pytest`), OpenClaw gateway `sessions.*` RPCs.

**Spec:** `docs/superpowers/plans/2026-09-06-openclaw-81-audit-findings.md` — findings A3, A4,
A8, S1, S2, S3, UI4.

## Global Constraints

- **Image tag we run:** `v2026.8.1-isol8.3`. Verify with `git show v2026.8.1-isol8.3:<path>`
  in `~/dev/agent-fork`. **Its working tree is a stale 2026.7.2 — never read it.**
- **"General" is load-bearing. Do not hide it and do not stop writing to it.** The 3-part key
  `agent:{agentId}:{userId}` receives the onboarding kickoff and first-contact nudge
  (`core/services/bootstrap_dispatcher.py:1051`, `:1215`) and every deployed marketplace
  agent's cron jobs (`core/services/catalog_service.py:353`). Hiding it strands all of that.
- **Do NOT use `involvingMe` / `ownerId` / `creatorId` / `ownerFirst`** on `sessions.list`.
  They resolve against the single shared backend operator identity
  (`src/gateway/server-methods/sessions-read.ts:296-298`), not the end user, so they cannot
  replace `_filter_sessions_for_user`.
- **Do NOT use `sessions.groups.*`.** The catalog is gateway-global with no per-client scoping
  (`sessions-groups.ts:39-49`), so on our shared per-owner container one org member's group
  names leak to every other. Per-session `category` is the safe half.
- **The organization field is `category`, not `group`.**
- **`routers/websocket_chat.py` belongs to `wt-proxy`, not this worktree.** That file is where
  the P0 security work (the vendored session-target registry, the default-deny allowlist, the
  `permissionMode` params check) lands, serially, in overlapping regions. **Task 2's backend
  half must either wait for that branch or be folded into it** — do not open a second branch on
  that file. The frontend half of Task 2 is independent and can proceed.
- **`core/services/admin_service.py` (Task 1, Step 5) is owned by no worktree.** It is a
  comment-and-param fix with no overlap; land it here and say so in the PR.
- **Colour literals fail the brand-token scan** outside the documented exemptions.
- **Frontend vitest is RAM-heavy** — run targeted files while iterating.
- No PR or issue references in code comments.

---

## File Structure

| file | responsibility | change |
|---|---|---|
| `apps/frontend/src/hooks/useAgentSessions.ts` | the rail's data layer | Tasks 1, 2, 5, 6 |
| `apps/backend/routers/websocket_chat.py` | `sessions.list` proxy + preview trim | Task 2 |
| `apps/frontend/src/components/workspace/WorkspaceSidebar.tsx` | desktop sidebar | Task 3 |
| `apps/frontend/src/components/chat/SessionSearch.tsx`, `hooks/useSessionSearch.ts` | **deleted** | Task 3 |
| `apps/frontend/src/components/chat/SessionList.tsx` | row rendering | Tasks 4, 6 |
| `apps/frontend/src/hooks/useActiveSession.ts` | active-session store | Task 5 |

---

### Task 1: Stop scanning every agent's sessions

`hooks/useAgentSessions.ts:110-124` says *"the RPC has no agent filter (the container's
`agentId` param requires `sessionKeys`)"*. **That is wrong.** `agentId` is a first-class
`sessions.list` param (`packages/gateway-protocol/src/schema/sessions-list.ts:43`) and narrows
both the row filter and the **store load** (`sessions-read.ts:262-266`) — the expensive part.
The "agentId requires sessionKeys" rule belongs to `sessions.search`
(`sessions-read.ts:114-122`).

Because of that belief the sidebar walks up to **10 pages × 100 rows** of every agent's store
(`:135`) and filters client-side, on a 1-vCPU container, with `includeDerivedTitles` forcing a
transcript read per row. The same wrong claim is repeated at
`core/services/admin_service.py:577-578` and `:606`.

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentSessions.ts`
- Test: `apps/frontend/src/hooks/__tests__/useAgentSessions.test.ts` (create if absent)

**Interfaces:**
- Consumes: nothing.
- Produces: the SWR key becomes per-agent, and the fetcher issues **one** `sessions.list` with
  `agentId`. `AgentSession[]` is unchanged.

- [ ] **Step 1: Verify the param at the tag before trusting this plan**

```bash
cd ~/dev/agent-fork && git show v2026.8.1-isol8.3:packages/gateway-protocol/src/schema/sessions-list.ts | sed -n '30,60p'
git show v2026.8.1-isol8.3:src/gateway/server-methods/sessions-read.ts | sed -n '255,275p'
```

Expected: `agentId` on the params schema, and a store load narrowed by it.

- [ ] **Step 2: Write the failing test**

```ts
it("requests one page scoped to the agent, not a walk of every agent's store", async () => {
  const rpc = mockGatewayRpc();
  renderHook(() => useAgentSessions("agent_a", "user_1"));
  await waitFor(() => expect(rpc).toHaveBeenCalled());
  const calls = rpc.mock.calls.filter(([m]) => m === "sessions.list");
  expect(calls).toHaveLength(1);
  expect(calls[0][1]).toMatchObject({ agentId: "agent_a" });
});

it("does not ask for lastMessage previews it never reads", async () => {
  const rpc = mockGatewayRpc();
  renderHook(() => useAgentSessions("agent_a", "user_1"));
  await waitFor(() => expect(rpc).toHaveBeenCalled());
  const [, params] = rpc.mock.calls.find(([m]) => m === "sessions.list")!;
  expect(params.includeLastMessage).toBeUndefined();
});

it("keeps a separate cache per agent", async () => {
  const rpc = mockGatewayRpc();
  renderHook(() => useAgentSessions("agent_a", "user_1"));
  renderHook(() => useAgentSessions("agent_b", "user_1"));
  await waitFor(() => {
    const scoped = rpc.mock.calls.filter(([m]) => m === "sessions.list").map(([, p]) => p.agentId);
    expect(new Set(scoped)).toEqual(new Set(["agent_a", "agent_b"]));
  });
});
```

- [ ] **Step 3: Run to confirm it fails**

Run: `cd apps/frontend && pnpm test -- useAgentSessions`

Expected: FAIL — multiple paged calls, no `agentId`.

- [ ] **Step 4: Replace the walk with one scoped call**

Replace the SWR key and fetcher. The key becomes `["sessions.list", userId, agentId]` — keyed
by user **and** agent (keyed by user alone was deliberate to avoid one scan per agent; with a
server-side filter that reason is gone, and per-agent keys are now cheaper, not dearer). Keyed
by user still matters: the app-wide SWR cache outlives a sign-out.

```ts
    agentId && userId ? ["sessions.list", userId, agentId] : null,
    async () => {
      // agentId narrows the container's STORE LOAD, not just the row filter
      // (sessions-read.ts). The previous comment here claimed the RPC had no
      // agent filter and that the walk below was therefore required; that was
      // wrong — the "agentId requires sessionKeys" rule belongs to
      // sessions.search, not sessions.list.
      //
      // includeLastMessage is deliberately NOT sent: the row field is
      // `lastMessagePreview`, we never read it, and asking for it costs a
      // transcript read per row on a 1-vCPU container.
      const res = (await callRpc("sessions.list", {
        agentId,
        includeGlobal: false,
        includeUnknown: false,
        includeDerivedTitles: true,
        limit: 100,
      })) as { sessions?: RawSessionRow[]; totalCount?: number };
      return { sessions: res?.sessions ?? [] };
    },
```

**Keep `keepPreviousData: true`** — a slow revalidation must never blank the sidebar; that was
a real regression once. Keep the `dedupingInterval`.

If `totalCount` exceeds the returned length, surface it rather than silently truncating — but
do **not** reinstate the page walk in this task; a 100-row cap per agent is already far beyond
what a rail displays.

- [ ] **Step 5: Fix the two stale comments in the backend**

`core/services/admin_service.py:577-578` and `:606` repeat the same wrong claim. Correct the
comments and pass `agentId` there too if the call site has one; if it deliberately wants every
agent, say so in the comment instead of citing a limitation that does not exist.

- [ ] **Step 6: Run the tests**

Run: `cd apps/frontend && pnpm test -- useAgentSessions && pnpm exec tsc --noEmit`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/hooks/ apps/backend/core/services/admin_service.py
git commit -m "perf(sessions): filter sessions.list by agent instead of walking every store"
```

---

### Task 2: Delete the dead fields and the no-op trim

The row field is **`lastMessagePreview`** — a top-level string capped at 240 chars upstream
(`session-utils-row.ts:475`). There is no `lastMessage` object and no `title`, at 8.1 or 7.2.

- `useAgentSessions.ts:42-43` declares `title` and `lastMessage?: { text?: string }`, and `:74`
  uses both in the label fallback chain. **Both branches are unreachable.**
- `routers/websocket_chat.py:335-350` `_slim_sessions_list_previews` reads
  `row["lastMessage"]["text"]` — **a no-op on every row** — and is redundant anyway, since
  upstream's 240-char cap is below our 280 (`:288`).

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentSessions.ts`
- Modify: `apps/backend/routers/websocket_chat.py`
- Test: both suites

**Interfaces:**
- Consumes: nothing.
- Produces: `RawSessionRow` gains `lastMessagePreview?: string` and loses `title` and
  `lastMessage`. `AgentSession` gains `preview?: string` for Task 4.

- [ ] **Step 1: Prove the fields do not exist**

```bash
cd ~/dev/agent-fork && git show v2026.8.1-isol8.3:src/agents/tools/sessions-list-tool.ts | sed -n '70,100p' | grep -E "lastMessage|title|derivedTitle|displayName"
git show v2026.7.2-isol8.8:src/agents/tools/sessions-list-tool.ts | grep -cE '"?title"?:' || true
```

Expected: `lastMessagePreview`, `derivedTitle`, `displayName`, `label` — and no `title` or
`lastMessage` at either tag.

- [ ] **Step 2: Write the failing tests**

```ts
it("uses lastMessagePreview, not the fields the RPC never sent", () => {
  const rows = [{ key: "agent:a:u:11111111-1111-4111-8111-111111111111",
                  lastMessagePreview: "the preview", updatedAt: 1 }];
  const sessions = projectRows(rows, "a", "u");
  expect(sessions[0].preview).toBe("the preview");
});
```

```python
def test_slim_previews_is_gone():
    """_slim_sessions_list_previews read row["lastMessage"]["text"], a field the
    RPC has never returned, so it was a no-op on every row — and upstream already
    caps lastMessagePreview at 240 chars, below our 280."""
    from routers import websocket_chat
    assert not hasattr(websocket_chat, "_slim_sessions_list_previews")
```

- [ ] **Step 3: Run to confirm they fail, then implement**

Frontend: drop `title` and `lastMessage` from `RawSessionRow`; add
`lastMessagePreview?: string`; remove both from the `labelsFor` fallback chain; add
`preview: row.lastMessagePreview` to the projected `AgentSession`.

Backend: delete `_slim_sessions_list_previews`, `_SESSION_PREVIEW_MAX_CHARS`, and the call site.

- [ ] **Step 4: Run both suites**

```bash
cd apps/frontend && pnpm test -- useAgentSessions
cd ../backend && uv run pytest tests/unit/routers/ -v -k session
```

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/hooks/ apps/backend/routers/websocket_chat.py apps/backend/tests/
git commit -m "fix(sessions): read lastMessagePreview and delete the no-op preview trim"
```

---

### Task 3: Remove the sidebar search

One usage site: `WorkspaceSidebar.tsx:211`. Deleting it drops `SessionSearch.tsx`,
`useSessionSearch.ts` and two test files. Upstream independently moved session search into the
command palette (#128356), and **we already have a global ⌘K palette**
(`components/teams/command-palette/CommandPaletteProvider.tsx:33-48`, mounted at
`WorkspaceShell.tsx:428`).

**Files:**
- Modify: `apps/frontend/src/components/workspace/WorkspaceSidebar.tsx`
- Delete: `apps/frontend/src/components/chat/SessionSearch.tsx`,
  `apps/frontend/src/hooks/useSessionSearch.ts`,
  `apps/frontend/src/components/chat/__tests__/SessionSearch.test.tsx`,
  `apps/frontend/src/hooks/__tests__/useSessionSearch.test.ts`
- Modify: `apps/frontend/src/components/workspace/__tests__/WorkspaceSidebar.test.tsx`

**Interfaces:** Consumes nothing. Produces a smaller sidebar.

- [ ] **Step 1: Confirm there is exactly one usage site**

```bash
grep -rn "SessionSearch\|useSessionSearch" apps/frontend/src apps/mobile/src packages/shared/src | grep -v "components/chat/SessionSearch.tsx\|hooks/useSessionSearch.ts"
```

Expected: only `WorkspaceSidebar.tsx` and the two test files. **If anything else appears, stop**
— the deletion is wider than this task assumes.

- [ ] **Step 2: Remove the render and the import**

Delete the `<SessionSearch … />` block at `:211-215` and its import at `:46`. The
`renderSessions` prop then returns `<SessionList … />` directly and no longer needs a fragment.

- [ ] **Step 3: Delete the files and the sidebar test's mock**

Remove the four files above, and the `vi.mock("@/components/chat/SessionSearch", …)` block at
`WorkspaceSidebar.test.tsx:127-129` plus any assertion depending on it.

- [ ] **Step 4: Verify nothing dangles**

```bash
cd apps/frontend && pnpm exec tsc --noEmit && pnpm test -- WorkspaceSidebar
```

Expected: no unresolved imports, tests PASS.

- [ ] **Step 5: Commit**

```bash
git add -A apps/frontend/src
git commit -m "feat(sessions): remove the sidebar search; ⌘K already covers it"
```

---

### Task 4: Show a timestamp and a preview on each row

`SessionList.tsx:104` renders `s.label` and nothing else. **Our own Expo app already does
better** — `apps/mobile/src/components/chat/SessionsSheet.tsx:88-100` shows label, relative
time and a one-line preview. Web is behind our own mobile app.

**Files:**
- Modify: `apps/frontend/src/components/chat/SessionList.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/SessionList.test.tsx` (extend)

**Interfaces:** Consumes `AgentSession.preview` (Task 2) and `updatedAt`. Produces no exports.

- [ ] **Step 1: Write the failing tests**

```tsx
it("shows a relative timestamp", () => {
  renderList([session({ label: "Q3 pricing", updatedAt: Date.now() - 7_200_000 })]);
  expect(screen.getByText("2h")).toBeInTheDocument();
});

it("shows a one-line preview when present", () => {
  renderList([session({ label: "Q3 pricing", preview: "I checked the numbers" })]);
  expect(screen.getByText(/I checked the numbers/)).toBeInTheDocument();
});

it("renders cleanly with neither", () => {
  renderList([session({ label: "Q3 pricing", preview: undefined, updatedAt: undefined })]);
  expect(screen.getByText("Q3 pricing")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to confirm they fail, then implement**

Reuse the mobile app's relative-time helper if it is in `packages/shared`; otherwise write the
smallest local one (`<1m` → "now", minutes, hours, days). Keep the preview to one line with
`truncate`, and keep the row's click target the whole row.

- [ ] **Step 3: Run tests, typecheck, brand scan**

```bash
cd apps/frontend && pnpm test -- SessionList && pnpm exec tsc --noEmit && node scripts/brand-tokens-scan.mjs
```

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/components/chat/
git commit -m "feat(sessions): show relative time and a preview on each row"
```

---

### Task 5: Name "General" honestly and stop new chats landing in it

A fresh agent's active session is `null`, and `buildChatSessionKey(agentId, userId, null)`
returns the 3-part key — so **every agent's first conversation lands in "General"**, which then
sits pinned above everything, unrenameable and undeletable.

But General is **not** dead weight: it receives the onboarding kickoff, the first-contact nudge
and every deployed agent's cron jobs. In 8.1 it is not special —
`classifySessionKind` returns `"direct"` for both key shapes — and upstream retired the concept
(#123521). So: keep it, name it for what it is, unpin it, and give the owner's first typed
message a real session.

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentSessions.ts` (the `generalRow` unshift at `:192-201`)
- Modify: `apps/frontend/src/hooks/useActiveSession.ts` (default for an agent with no stored session)
- Test: both hooks' test files

**Interfaces:**
- Consumes: nothing.
- Produces: the `sessionId: null` row keeps its identity but is sorted by `updatedAt` like any
  other, and `useActiveSession.read()` returns a **fresh uuid** for an agent that has never
  selected a session.

- [ ] **Step 1: Write the failing tests**

```ts
it("labels the legacy row for what it actually is", () => {
  const sessions = projectRows([generalRow(), namedRow()], "a", "u");
  const legacy = sessions.find((s) => s.sessionId === null)!;
  expect(legacy.label).not.toBe("General");
  expect(legacy.label).toMatch(/agent|updates|system/i);
});

it("does not pin the legacy row above newer sessions", () => {
  const sessions = projectRows(
    [generalRow({ updatedAt: 1 }), namedRow({ updatedAt: 999 })], "a", "u",
  );
  expect(sessions[0].sessionId).not.toBeNull();
});

it("does not pin the legacy row to the BOTTOM either — it sorts by recency", () => {
  // Guards the opposite bug: the projected legacy row historically carried no
  // updatedAt, so a naive unshift->push flip sorts it last forever.
  const sessions = projectRows(
    [generalRow({ updatedAt: 999 }), namedRow({ updatedAt: 1 })], "a", "u",
  );
  expect(sessions[0].sessionId).toBeNull();
});

it("a brand-new agent settles into a real session, not the legacy key", async () => {
  const { result } = renderHook(() => useActiveSession("agent_never_used"));
  await waitFor(() => {
    expect(result.current.activeSessionId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });
});

it("getActiveSessionId stays pure and keeps returning null", () => {
  // RunNotifications calls this for every visible agent. If it minted, it would
  // create and persist a session per agent just by rendering a list.
  expect(getActiveSessionId("agent_never_touched")).toBeNull();
  expect(window.localStorage.getItem("isol8:activeSession:agent_never_touched")).toBeNull();
});

it("an agent WITH a stored session still restores it", () => {
  window.localStorage.setItem("isol8:activeSession:agent_b", "22222222-2222-4222-8222-222222222222");
  const { result } = renderHook(() => useActiveSession("agent_b"));
  expect(result.current.activeSessionId).toBe("22222222-2222-4222-8222-222222222222");
});
```

Pick the label with the owner in mind — it is where the platform talks to the agent, so
something like **"Agent updates"** reads truthfully. Agree the exact string before writing the
regex-loose assertion above into something stricter.

- [ ] **Step 2: Run to confirm they fail, then implement**

In `useAgentSessions`, give the legacy row the agreed label instead of `"General"` — and note
two things the current code makes easy to get wrong:

1. **The projected legacy row carries no `updatedAt`.** `:192-201` projects `sessionId`, `key`,
   `label`, `rawLabel` and the model controls, and nothing else. Carry it over:
   `updatedAt: generalRow.updatedAt`.
2. **`out.sort(...)` runs on the line ABOVE the unshift.** Simply switching `unshift` to `push`
   puts the legacy row last, always — the opposite bug. Move the sort so it runs *after* the row
   is added.

```ts
    if (generalRow) {
      out.push({
        sessionId: null,
        key: generalRow.key,
        label: LEGACY_ROW_LABEL,
        rawLabel: LEGACY_ROW_LABEL,
        updatedAt: generalRow.updatedAt,   // was dropped; without it the row sorts last forever
        ...modelControlsOf(generalRow),
      });
    }
    // Sort AFTER the legacy row is in, so it takes its place by recency like
    // any other session instead of being pinned to either end.
    out.sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0));
    return out;
```

**Do NOT mint inside `read()`.** `read` is passed as *both* `getSnapshot` and
`getServerSnapshot` to `useSyncExternalStore` (`useActiveSession.ts:105-109`), so minting and
`localStorage.setItem` there is a side effect during render and diverges the server and client
snapshots. Worse, `getActiveSessionId()` is the *same function* and is called by
`RunNotifications` to resolve a session per visible chat agent — minting in `read()` would
create and persist a session for every agent merely by rendering the notification list. Its own
docstring promises `null` for an agent that has never selected a session.

Mint in an **effect** inside `useActiveSession` instead:

```ts
  // A brand-new agent starts in a real session rather than the legacy 3-part key.
  // This lives in an effect, NOT in read(): read() is getSnapshot AND
  // getServerSnapshot for useSyncExternalStore, and it is also the non-hook
  // getActiveSessionId() that RunNotifications calls for every visible agent.
  // Minting there would be a render-phase side effect and would create a session
  // per agent just by rendering a list.
  useEffect(() => {
    if (!agentId) return;
    if (activeSessionId !== null) return;
    if (readStoredSession(agentId) !== null) return;
    write(agentId, crypto.randomUUID());
  }, [agentId, activeSessionId]);
```

`read()` keeps returning `null`, and `getActiveSessionId()` keeps its documented contract.

**Do not change `buildChatSessionKey`.** `null` must still map to the 3-part key — the backend
writes to it. This task changes only which session a *new UI session* starts in.

- [ ] **Step 3: Confirm the backend still reaches General**

```bash
grep -n "build_chat_session_key" apps/backend/core/services/bootstrap_dispatcher.py apps/backend/core/services/catalog_service.py
```

Expected: still passing `None` / the 3-part form. **These must not change.**

- [ ] **Step 4: Run the tests**

Run: `cd apps/frontend && pnpm test -- useAgentSessions useActiveSession && pnpm exec tsc --noEmit`

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/hooks/
git commit -m "feat(sessions): name the legacy row honestly and start new chats in a real session"
```

---

### Task 6: Have the agent title the session once

`derivedTitle` is the **first user message truncated**
(`src/gateway/session-utils.ts:165-196`, `deriveSessionTitle`). **There is no LLM auto-titling
upstream** — `/name` with no argument returns a deterministic suggestion, and its own comment
says *"no LLM, no mutation"* (`src/auto-reply/reply/commands-name.ts`). So this is ours.

`sessions.patch {key, label}` is the persisted user-set name and already wins over
`derivedTitle` in our label chain. We already call it for manual rename.

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentSessions.ts` (a title-once effect)
- Test: `apps/frontend/src/hooks/__tests__/useAgentSessions.test.ts`

**Interfaces:**
- Consumes: `renameSession(key, label)`, already exported.
- Produces: no new exports.

- [ ] **Step 1: Write the failing tests**

```ts
it("titles a session once, after the first exchange", async () => {
  const rpc = mockGatewayRpc();
  renderWithSessions([sessionAfterFirstExchange({ key: "agent:a:u:1111…", label: undefined })]);
  await waitFor(() => {
    const patches = rpc.mock.calls.filter(([m]) => m === "sessions.patch");
    expect(patches).toHaveLength(1);
    expect(patches[0][1].label).toBeTruthy();
    expect(patches[0][1].label.length).toBeLessThanOrEqual(48);
  });
});

it("never re-titles a session that already has a user label", async () => {
  const rpc = mockGatewayRpc();
  renderWithSessions([sessionAfterFirstExchange({ label: "My own name" })]);
  await waitFor(() => {
    expect(rpc.mock.calls.filter(([m]) => m === "sessions.patch")).toHaveLength(0);
  });
});

it("does not title the legacy General row", async () => {
  const rpc = mockGatewayRpc();
  renderWithSessions([generalRow()]);
  await waitFor(() => {
    expect(rpc.mock.calls.filter(([m]) => m === "sessions.patch")).toHaveLength(0);
  });
});

it("titles at most once per session even across re-renders", async () => {
  const rpc = mockGatewayRpc();
  const { rerender } = renderWithSessions([sessionAfterFirstExchange({})]);
  rerender();
  rerender();
  await waitFor(() => {
    expect(rpc.mock.calls.filter(([m]) => m === "sessions.patch")).toHaveLength(1);
  });
});
```

- [ ] **Step 2: Run to confirm they fail**

Run: `cd apps/frontend && pnpm test -- useAgentSessions`

- [ ] **Step 3: Decide where the title comes from, and say so in the commit**

Two options. **Take (a) unless it demonstrably produces poor titles:**

**(a) Client-side, free.** Derive from `derivedTitle` — strip a leading filler opener ("can
you", "hey", "please"), sentence-case it, truncate at ~40 chars on a word boundary. No model
call, no spend, deterministic and testable.

**(b) Ask the agent.** One cheap RPC asking for a 2–4 word title. Better titles, but it is a
model call per new session — on a $3.00/day trial cap that is real, and it needs the same
paid-only gating `skills.workshop.autonomous` already has.

- [ ] **Step 4: Implement, guarded against repeats**

Fire once per session key, tracked in a module-level `Set` so a re-render or a second mounted
consumer cannot double-patch. Skip when: `sessionId === null` (the legacy row), a user `label`
already exists, or there is no `derivedTitle` yet (the session has not materialised).

Failures must be silent — a failed auto-title is not worth a toast. Follow `renameSession`'s
existing error handling, minus the notification.

- [ ] **Step 5: Run the tests**

Run: `cd apps/frontend && pnpm test -- useAgentSessions && pnpm exec tsc --noEmit`

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/hooks/
git commit -m "feat(sessions): title a session once after its first exchange"
```

---

### Task 7: Show what happened on each row

`status` (`queued|running|done|failed|killed|timeout`), `lastRunError`, `abortedLastRun` and
`agentStatus` are on **every** `sessions.list` row (`session-utils-row.ts:504-537`) and we read
none of them — we consume `status` only as a boolean run-active flag off `sessions.changed`
(`connection_pool.py:2272-2300`). **A failed routine looks identical to a successful one.**

This lands here rather than in the liveness plan because Task 2 already changes
`RawSessionRow`, and because the liveness plan cannot meet its own "a failed routine is
visually distinct" criterion without it.

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentSessions.ts`, `components/chat/SessionList.tsx`
- Test: both files' test suites

**Interfaces:** Consumes the row fields above. Produces `AgentSession.status`,
`.lastRunError`, `.agentStatus`.

- [ ] **Step 1: Confirm the fields on the row schema at the tag**

```bash
cd ~/dev/agent-fork && git show v2026.8.1-isol8.3:src/gateway/session-utils-row.ts | sed -n '500,540p'
```

- [ ] **Step 2: Write the failing tests**

```tsx
it("carries run status onto the session", () => {
  const s = projectRows([row({ status: "failed", lastRunError: "boom" })], "a", "u");
  expect(s[0].status).toBe("failed");
  expect(s[0].lastRunError).toBe("boom");
});

it("marks a failed row visibly", () => {
  renderList([session({ label: "Nightly CEO", status: "failed" })]);
  expect(screen.getByLabelText(/failed/i)).toBeInTheDocument();
});

it("shows nothing extra for a successful row", () => {
  renderList([session({ label: "Nightly CEO", status: "done" })]);
  expect(screen.queryByLabelText(/failed/i)).toBeNull();
});
```

- [ ] **Step 3: Run to confirm they fail, then add the fields and a small state marker**

Add `status`, `lastRunError`, `abortedLastRun` and `agentStatus` to `RawSessionRow` and
`AgentSession`, type-guarded the way `modelControlsOf` already guards its fields. Render a
single quiet marker for `failed`/`killed`/`timeout` — colour from `var(--…)` tokens, with an
`aria-label`, not colour alone.

- [ ] **Step 4: Run tests, typecheck, brand scan**

```bash
cd apps/frontend && pnpm test -- useAgentSessions SessionList && pnpm exec tsc --noEmit && node scripts/brand-tokens-scan.mjs
```

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/
git commit -m "feat(sessions): show run outcome on each row"
```

---

## Verification — the whole feature, on dev

- [ ] **Step 1: Ship to dev** and watch the run.

- [ ] **Step 2: Measure the fetch.** Open the workspace with several agents. In devtools,
  confirm **one** `sessions.list` per agent carrying `agentId`, and no `includeLastMessage`.
  **PASS:** the rail populates in about a second. **FAIL:** repeated paged calls.

- [ ] **Step 3: New-session latency.** Start a new chat and send one message.
  **PASS:** the row appears within a couple of seconds, named something recognisable.
  **FAIL:** it takes many seconds, or is named after your raw first message.

- [ ] **Step 4: General.** Confirm the legacy row is labelled honestly, sorts by recency rather
  than pinned to the top, and **still receives platform traffic** — check that a dev owner's
  onboarding kickoff still lands there.

- [ ] **Step 5: Regression — no orphaned history.** An agent whose only history predates
  sessions must still show that conversation and let you open it.

- [ ] **Step 6: Promote to prod** only after 2–5 pass — `gh workflow run deploy.yml --ref main`.

---

## Expected UX

**Before:** open an agent and the rail sits empty for seconds while the container scans every
agent's session store and reads a transcript per row. What appears is a list named after
whatever you happened to type first, with an unexplained "General" pinned on top and a search
box nobody asked for.

**After:** the list is there almost immediately. Each row has a name a person would recognise,
a relative time and a line of what was last said. The legacy row is named for what it is —
where the platform talks to your agent — and sits in date order like everything else. New chats
start in their own session.

## Not in this plan

- ~~**Run outcome on the row (A8)**~~ — **MOVED IN. See Task 7.** It was previously deferred
  here *and* deferred to here by the liveness plan, so it belonged to nobody — while the
  liveness plan's own acceptance criterion ("a failed routine is visually distinct from a
  successful one") depends on it. Task 2 already changes `RawSessionRow`, so this is the cheap
  moment.
- **Archive vs delete (A6)** and **`pinned`/`category`/`unread` (A7)** — organisation features
  that want the row work above to settle first.
- **Consuming the `sessions.changed` snapshot instead of polling (A9)** — the larger latency
  win, but it is a backend forwarding change plus a client merge, and it should land after the
  fetch is correct so the improvement is attributable.
- **Session search in the ⌘K palette** — Task 3 removes the sidebar search on the grounds that
  ⌘K covers discovery. Adding transcript search *to* the palette is a separate change to the
  palette, not the rail.
