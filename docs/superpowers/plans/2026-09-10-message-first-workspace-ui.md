# Message-First Workspace UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the authenticated desktop workspace as one Slack-familiar, message-first shell where people and agents share channels and direct messages, while preserving the existing messaging contracts and hidden Dockview capabilities.

**Architecture:** Keep `useRooms`, `RoomDirectory`, `RoomPanel`, Dockview, the panel registry, and the workspace controller as the system of record. Extract the stateful orchestration from `MessagingWorkspace` into one provider so the desktop sidebar can render the room directory while the singleton Messages Dockview panel renders the selected room; the existing joined composition remains on mobile. Migrate legacy per-agent chat panels to one parameter-driven Messages panel and derive all agent activity presentation from `useRosterEntries`.

**Tech Stack:** Next.js 16, React, TypeScript, Vitest, Testing Library, Playwright, Dockview React, SWR, Clerk, existing Lightbulb CSS variables and Lucide icons.

**Spec:** `docs/superpowers/specs/2026-09-10-message-first-workspace-ui-design.md`

## Global Constraints

- Start execution from current `origin/main` containing merged PR #2433; preserve its onboarding and commercial-identity changes.
- Work in an isolated worktree created with `superpowers:using-git-worktrees`; do not implement in the current dirty main worktree.
- If the spec commit `cb2dfb2ab` is not in that worktree, cherry-pick it before Task 1 so the implementation and its source-of-truth design travel together.
- Stay in the UI lane: no onboarding, pricing, provisioning, Paperclip seed, starter-agent, GStack package, marketplace-install, first-message, backend messaging, or container changes.
- Render only canonical rooms and roster members; do not hardcode Friday, Atlas, Gauge, GStack, or any placeholder roster.
- Keep Dockview and `disableDnd`; add no layout-editing toggle, dependency, API endpoint, persistence service, or feature flag.
- Preserve Browser, Activity/Inbox, Tasks/Teams, Agents, Marketplace, files, approvals, deep links, layout recovery, provisioning gates, and current mobile behavior.
- Use `--cream`, `--cream-deep`, `--cream-deeper`, `--edge`, `--midnight`, existing semantic tokens, Source Sans 3, selective Lora, and the existing 4px density rhythm.
- Agent activity is honest: running gets a restrained amber halo; attention/paused gets a static semantic indicator; idle, terminated, and unknown get no halo.
- Respect reduced motion, visible keyboard focus, non-color labels, Escape handling, and focus restoration.
- Do not push, deploy, or promote production unless the user explicitly asks.

## Execution Preflight

- [ ] Invoke `superpowers:using-git-worktrees`, fetch `origin`, and create `feat/message-first-workspace-ui` from the latest `origin/main`.
- [ ] Verify PR #2433 is in the baseline:

```bash
merge_oid=$(gh pr view 2433 --repo TheLightbulbCompany/lightbulb --json mergeCommit --jq '.mergeCommit.oid')
git merge-base --is-ancestor "$merge_oid" HEAD
```

Expected: exit 0.

- [ ] Carry the approved spec when it is not already an ancestor:

```bash
git merge-base --is-ancestor cb2dfb2ab HEAD || git cherry-pick cb2dfb2ab
```

- [ ] Establish the clean frontend baseline:

```bash
cd apps/frontend
pnpm test -- src/hooks/__tests__/useRooms.test.tsx src/components/workspace/__tests__/workspaceController.test.ts src/components/workspace/__tests__/layoutStorage.test.ts
pnpm lint
```

Expected: all selected tests and lint pass before product code changes.

## File Structure

| File | Responsibility after this plan |
|---|---|
| `apps/frontend/src/hooks/useRooms.ts` | Canonical room data, lightweight DM/group-DM detail hydration, active-room selection, and idempotent agent-DM resolution. |
| `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` | The one stateful messaging owner: roster enrichment, drafts, typing, creation, threads, runs, and final prop bags. |
| `apps/frontend/src/components/messaging/MessagingWorkspace.tsx` | Thin joined mobile composition plus exported desktop directory and conversation consumers. |
| `apps/frontend/src/components/messaging/ParticipantAvatar.tsx` | Shared human/agent avatar with honest, accessible activity treatment. |
| `apps/frontend/src/components/messaging/RoomDirectory.tsx` | Channels, named canonical DMs/group DMs, unread state, and conversation creation UI. |
| `apps/frontend/src/components/messaging/RoomPanel.tsx` | Center conversation plus wide default context, selected thread/context detail, and responsive context behavior. |
| `apps/frontend/src/components/messaging/RoomComposer.tsx` | Existing composer plus the preserved focus/draft handoff used by Get Started and Ask Friday. |
| `apps/frontend/src/components/messaging/types.ts` | UI-only enriched room/participant/activity types; shared wire contracts remain unchanged. |
| `apps/frontend/src/components/messaging/messaging.css` | Message-first density, three-region room layout, overlay/mobile breakpoints, and activity halo. |
| `apps/frontend/src/hooks/useIsDesktop.ts` | JavaScript shell boundary matching the 860px message-first desktop breakpoint. |
| `apps/frontend/src/components/chat/ChatLayout.css` | Existing mobile composition through 859px; no mobile information-architecture rewrite. |
| `apps/frontend/src/components/workspace/panelRegistry.ts` | Stable singleton Messages panel identity while retaining legacy chat-id parsing helpers. |
| `apps/frontend/src/components/workspace/workspaceController.ts` | Activate or retarget the singleton Messages panel and publish its selected agent in the workspace snapshot. |
| `apps/frontend/src/components/workspace/layoutStorage.ts` | Sanitize legacy multi-chat layouts into one Messages panel without discarding auxiliary panels. |
| `apps/frontend/src/components/workspace/panels/ChatPanel.tsx` | Apply Dockview agent-parameter changes to shared messaging state and render only the selected conversation. |
| `apps/frontend/src/components/workspace/WorkspaceSidebar.tsx` | One desktop navigation surface for primary destinations, channels, DMs, More, Marketplace, health, and account state. |
| `apps/frontend/src/components/workspace/WorkspaceShell.tsx` | Mount the shared messaging provider around the desktop sidebar and dock while preserving #2433 gates. |
| `apps/frontend/src/components/workspace/WorkspaceHeader.tsx` | Search, files, browser, help, and profile; no promoted layout-editing control. |
| `apps/frontend/src/components/workspace/workspace.css` | 248px shell grid and desktop sidebar styling; remove the unused `.team-room*` prototype block. |

---

### Task 1: Make canonical direct messages resolvable after initial mount

**Files:**
- Modify: `apps/frontend/src/hooks/useRooms.ts:55`
- Test: `apps/frontend/src/hooks/__tests__/useRooms.test.tsx:104`

**Interfaces:**
- Consumes: existing `messaging.listRooms()`, `messaging.getRoom(roomId)`, `messaging.createRoom(request)`, and `RoomDetail`.
- Produces: `directoryRoomDetails: ReadonlyMap<string, RoomDetail>` and `selectAgentDm(agentId: string): Promise<void>` on the `useRooms` return value.

- [ ] **Step 1: Add failing tests for DM hydration and retargeting**

Add two tests to `useRooms.test.tsx`. Use two null-named DM summaries whose details contain Friday and Atlas, then assert an already-selected Friday DM can switch to Atlas without creating another room:

```tsx
it("hydrates direct-message members and retargets an existing DM", async () => {
  const fridayDm = {
    ...room,
    roomId: "11111111-1111-4111-8111-111111111112",
    kind: "dm" as const,
    name: null,
    members: [room.members[0], { ...room.members[1], id: "agent-friday", displayName: "Friday" }],
  };
  const atlasDm = {
    ...fridayDm,
    roomId: "11111111-1111-4111-8111-111111111113",
    members: [room.members[0], { ...room.members[1], id: "agent-atlas", displayName: "Atlas" }],
  };
  messaging.listRooms.mockResolvedValue({ rooms: [fridayDm, atlasDm] });
  messaging.getRoom.mockImplementation(async (roomId: string) =>
    roomId === atlasDm.roomId ? atlasDm : fridayDm,
  );

  const { result } = renderHook(() => useRooms("agent-friday"), { wrapper });
  await waitFor(() => expect(result.current.directoryRoomDetails.size).toBe(2));
  await act(async () => result.current.selectAgentDm("agent-atlas"));

  expect(result.current.activeRoomId).toBe(atlasDm.roomId);
  expect(messaging.createRoom).not.toHaveBeenCalled();
});

it("creates one canonical DM when the requested agent has none", async () => {
  const created = {
    ...room,
    roomId: "11111111-1111-4111-8111-111111111114",
    kind: "dm" as const,
    name: null,
    members: [room.members[0], { ...room.members[1], id: "agent-new", displayName: "New agent" }],
  };
  messaging.createRoom.mockResolvedValue(created);
  const { result } = renderHook(() => useRooms(), { wrapper });
  await waitFor(() => expect(result.current.activeRoomId).toBe(room.roomId));

  await act(async () => result.current.selectAgentDm("agent-new"));

  expect(messaging.createRoom).toHaveBeenCalledWith(expect.objectContaining({
    kind: "dm",
    name: null,
    members: [{ kind: "agent", id: "agent-new", role: "member", listenMode: "active" }],
  }));
  expect(result.current.activeRoomId).toBe(created.roomId);
});
```

- [ ] **Step 2: Run the tests and confirm the missing interface fails**

```bash
cd apps/frontend
pnpm test -- src/hooks/__tests__/useRooms.test.tsx
```

Expected: FAIL because `directoryRoomDetails` and `selectAgentDm` do not exist.

- [ ] **Step 3: Hydrate non-channel room details through one SWR request**

In `useRooms.ts`, import `RoomDetail`, derive a revision-sensitive key, and reuse one loader both for SWR and on-demand selection:

```ts
const directRoomSignature = rooms
  .filter((room) => room.kind !== "channel")
  .map((room) => `${room.roomId}:${room.roomRevision}`)
  .join("|");

const loadDirectoryRoomDetails = useCallback(async (): Promise<Map<string, RoomDetail>> => {
  const details = await Promise.all(
    rooms.filter((room) => room.kind !== "channel").map((room) => messaging.getRoom(room.roomId)),
  );
  return new Map(details.map((detail) => [detail.roomId, detail]));
}, [messaging, rooms]);

const directoryDetailsResponse = useSWR(
  scope && roomsData ? ["messaging-room-directory", scope, directRoomSignature] : null,
  loadDirectoryRoomDetails,
  { revalidateOnFocus: false, shouldRetryOnError: false },
);
const directoryRoomDetails: ReadonlyMap<string, RoomDetail> = directoryDetailsResponse.data ?? EMPTY_ROOM_DETAILS;
```

Define `EMPTY_ROOM_DETAILS` once beside `NO_ROOMS`. Keep this frontend-only. Do not change the shared `RoomSummary` wire contract or backend list endpoint.

- [ ] **Step 4: Extract initial-agent resolution into the reusable selector**

Move the existing find/create logic into a callback declared after the existing `createRoom` callback:

```ts
const selectAgentDm = useCallback(async (agentId: string): Promise<void> => {
  const details = directoryDetailsResponse.data ?? await loadDirectoryRoomDetails();
  const existing = [...details.values()].find(
    (detail) => detail.kind === "dm" && detail.members.some(
      (member) => member.kind === "agent" && member.id === agentId,
    ),
  );
  if (existing) {
    setActiveRoomId(existing.roomId);
    return;
  }
  const request = initialDmRequest.current?.agentId === agentId
    ? initialDmRequest.current
    : { agentId, clientRoomId: crypto.randomUUID() };
  initialDmRequest.current = request;
  const created = await createRoom({
    clientRoomId: request.clientRoomId,
    kind: "dm",
    name: null,
    members: [{ kind: "agent", id: agentId, role: "member", listenMode: "active" }],
  });
  setActiveRoomId(created.roomId);
}, [createRoom, directoryDetailsResponse.data, loadDirectoryRoomDetails]);
```

Keep `createRoom`'s existing `RoomDetail` return. Move the initial-selection effect below `selectAgentDm` and gate the intent by scope plus agent—not by `activeRoomId`—so changing the prop retargets once but ordinary channel selection does not snap back:

```ts
const handledInitialAgent = useRef<string | null>(null);

useEffect(() => {
  if (!roomsData) return;
  if (!initialAgentId) {
    handledInitialAgent.current = null;
    if (!activeRoomId && rooms.length > 0) setActiveRoomId(rooms[0].roomId);
    return;
  }
  const requestKey = `${scope}:${initialAgentId}`;
  if (handledInitialAgent.current === requestKey) return;
  handledInitialAgent.current = requestKey;
  void selectAgentDm(initialAgentId).catch(() => {
    if (handledInitialAgent.current === requestKey && rooms.length > 0) {
      setActiveRoomId(rooms[0].roomId);
    }
  });
}, [activeRoomId, initialAgentId, rooms, roomsData, scope, selectAgentDm]);
```

Expose `selectAgentDm` and `directoryRoomDetails`. Preserve fallback to the first room when resolution fails.

- [ ] **Step 5: Run the focused room suite**

```bash
cd apps/frontend
pnpm test -- src/hooks/__tests__/useRooms.test.tsx
```

Expected: PASS, including existing reconciliation, subscription, optimistic-send, and reconnect cases.

- [ ] **Step 6: Commit the canonical selection increment**

```bash
git add apps/frontend/src/hooks/useRooms.ts apps/frontend/src/hooks/__tests__/useRooms.test.tsx
git commit -m "feat(frontend): make room selection retargetable"
```

---

### Task 2: Extract one shared messaging state owner

**Files:**
- Create: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx`
- Create: `apps/frontend/src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx`
- Create: `apps/frontend/src/components/messaging/__tests__/RoomComposer.focus.test.tsx`
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspace.tsx:16`
- Modify: `apps/frontend/src/components/messaging/RoomComposer.tsx:11`
- Modify: `apps/frontend/src/components/messaging/types.ts:19`

**Interfaces:**
- Consumes: Task 1's `directoryRoomDetails` and `selectAgentDm`, `useRosterEntries(agents)`, existing `RoomDirectoryProps`, and existing `RoomPanelProps`.
- Produces:
  - `MessagingWorkspaceProvider(props: { children: ReactNode; initialAgentId?: string | null; subscribeToRoom?: boolean }): ReactElement`
  - `useMessagingWorkspace(): MessagingWorkspaceValue`
  - `MessagingDirectory(props: { onOpenRoom?: (room?: DirectoryRoom) => void }): ReactElement`
  - `MessagingConversation(): ReactElement`
  - `RoomParticipant.agentActivity?: RosterState` and `DirectoryParticipant.agentActivity?: RosterState`
  - `RoomComposerProps.focusAgentId?: string`

- [ ] **Step 1: Write a failing shared-owner composition test**

Mock `useRooms`, `useTeamsApi`, `useAgents`, and `useRosterEntries`, then render both exported consumers under one provider:

```tsx
it("feeds the directory and conversation from one selected-room owner", () => {
  const view = render(
    <MessagingWorkspaceProvider subscribeToRoom>
      <MessagingDirectory />
      <MessagingConversation />
    </MessagingWorkspaceProvider>,
  );

  expect(useRoomsMock).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("navigation", { name: "Conversations" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Messages" })).toBeInTheDocument();
  view.unmount();
});
```

Add a second assertion that a roster entry with `state: "running"` appears as `agentActivity: "running"` in both the directory participant and active-room participant props. Mock `RoomDirectory` and `RoomPanel` as prop-capture components so the test checks data instead of CSS.

- [ ] **Step 2: Run the new test and verify the context is absent**

```bash
cd apps/frontend
pnpm test -- src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx
```

Expected: FAIL because the provider and consumers do not exist.

- [ ] **Step 3: Define enriched UI types without touching wire contracts**

In `types.ts`, import `RosterState` as a type and extend only UI view models:

```ts
import type { RosterState } from "@/components/agents/AgentRoster";

export type RoomParticipant = RoomMember & {
  agentState?: WorkState;
  agentActivity?: RosterState;
};
export type DirectoryParticipant = Pick<
  RoomMember,
  "kind" | "id" | "displayName" | "mentionHandle"
> & {
  agentActivity?: RosterState;
};
export type DirectoryRoom = CanonicalRoomSummary & {
  participants: readonly DirectoryParticipant[];
};
```

Do not add these fields to `@lightbulb/shared`.

- [ ] **Step 4: Move orchestration into `MessagingWorkspaceContext.tsx`**

Move `frameText`, `runView`, all hooks/state/callbacks, and prop construction currently in `MessagingWorkspace` into the provider. Keep their behavior unchanged except for Task 1 integration and roster enrichment. The public value is:

```tsx
export interface MessagingWorkspaceValue {
  activeRoomId: string | null;
  directoryProps: RoomDirectoryProps;
  roomProps: RoomPanelProps | null;
  selectAgentDm: (agentId: string) => Promise<void>;
}

const MessagingWorkspaceContext = createContext<MessagingWorkspaceValue | null>(null);

export function useMessagingWorkspace(): MessagingWorkspaceValue {
  const value = useContext(MessagingWorkspaceContext);
  if (!value) throw new Error("useMessagingWorkspace must be used within MessagingWorkspaceProvider");
  return value;
}
```

Build one `activityByAgentId` map from `useRosterEntries(useAgents().agents)`. Enrich active-room members and hydrated directory-room members from that map; humans keep no agent fields. Keep the existing `agentState: "unknown"` only until Task 5 removes the old `WorkStatus` member-row treatment. Build `DirectoryRoom[]` from room summaries plus Task 1's detail map, filtering the signed-in human (`member.kind === "human" && member.id === userId`) out of directory-room participants so a null-named DM labels its counterpart rather than the owner. Continue using `CompanyAgent[]` and `CompanyMember[]` for the complete create-conversation picker.

Return exactly one provider value:

```tsx
return (
  <MessagingWorkspaceContext.Provider value={{
    activeRoomId: rooms.activeRoomId,
    directoryProps,
    roomProps,
    selectAgentDm: rooms.selectAgentDm,
  }}>
    {children}
  </MessagingWorkspaceContext.Provider>
);
```

`useRooms` remains the sole owner of the `initialAgentId` effect and calls `selectAgentDm(initialAgentId)` on every changed non-null ID. The provider only passes the prop through; do not add a second effect. This keeps mobile and newly mounted consumers on the old entry behavior without duplicate selection calls.

- [ ] **Step 5: Reduce `MessagingWorkspace.tsx` to two render compositions**

```tsx
export function MessagingDirectory({ onOpenRoom }: { onOpenRoom?: (room?: DirectoryRoom) => void }) {
  const { directoryProps } = useMessagingWorkspace();
  return <RoomDirectory
    {...directoryProps}
    onSelectRoom={(roomId) => {
      directoryProps.onSelectRoom(roomId);
      const room = directoryProps.rooms.find((candidate) => candidate.roomId === roomId);
      if (room) onOpenRoom?.(room);
    }}
    onCreate={(creation) => {
      onOpenRoom?.();
      directoryProps.onCreate?.(creation);
    }}
  />;
}

export function MessagingConversation() {
  const { roomProps } = useMessagingWorkspace();
  return roomProps
    ? <RoomPanel {...roomProps} />
    : <section className="room-panel room-blocked" aria-label="No conversation selected">
        <p role="status">Choose or create a conversation.</p>
      </section>;
}

export function MessagingWorkspace(props: {
  initialAgentId?: string | null;
  subscribeToRoom?: boolean;
}) {
  return <MessagingWorkspaceProvider {...props}>
    <div className="messaging-workspace">
      <MessagingDirectory />
      <MessagingConversation />
    </div>
  </MessagingWorkspaceProvider>;
}
```

- [ ] **Step 6: Write the failing focus/draft preservation test**

In `RoomComposer.focus.test.tsx`, record the existing cross-panel request and assert an empty textarea receives the draft and focus:

```tsx
const baseProps: RoomComposerProps = {
  text: "",
  onTextChange: vi.fn(),
  members: [],
  selectedMentions: [],
  onMentionsChange: vi.fn(),
  clientMessageId: "11111111-1111-4111-8111-111111111111",
  sendState: "idle",
  onSubmit: vi.fn(),
};

it("consumes the existing agent focus request without overwriting typed text", () => {
  const onTextChange = vi.fn();
  requestFocusComposer("agent-friday", "File the inbox");
  render(<RoomComposer {...baseProps} text="" focusAgentId="agent-friday" onTextChange={onTextChange} />);

  expect(onTextChange).toHaveBeenCalledWith("File the inbox");
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
});
```

Add the paired case with `text="Already typing"`, asserting focus occurs but `onTextChange` is not called.

- [ ] **Step 7: Consume focus requests in `RoomComposer`**

Add `focusAgentId?: string`, a textarea ref, and reuse the existing request slot:

```tsx
useEffect(() => {
  if (!focusAgentId) return;
  const applyFocusRequest = (event?: Event) => {
    const requestedAgent = (event as CustomEvent<{ agentId?: string }> | undefined)?.detail?.agentId;
    if (requestedAgent && requestedAgent !== focusAgentId) return;
    const request = consumeFocusComposerRequest(focusAgentId);
    if (!request) return;
    if (request.draft && !text.trim()) onTextChange(request.draft);
    field.current?.focus();
  };
  applyFocusRequest();
  window.addEventListener("getstarted:focus-composer", applyFocusRequest);
  return () => window.removeEventListener("getstarted:focus-composer", applyFocusRequest);
}, [focusAgentId, onTextChange, text]);
```

Pass `ref={field}` to `Textarea`. In the provider, set `focusAgentId` only when the active room is a DM with exactly one agent counterpart. This preserves `openChatPanelWithDraft` and Get Started without another event or store.

- [ ] **Step 8: Run shared messaging tests**

```bash
cd apps/frontend
pnpm test -- src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx src/components/messaging/__tests__/RoomComposer.focus.test.tsx src/hooks/__tests__/useRooms.test.tsx
```

Expected: PASS.

- [ ] **Step 9: Commit the shared state owner**

```bash
git add apps/frontend/src/components/messaging apps/frontend/src/hooks/useRooms.ts apps/frontend/src/hooks/__tests__/useRooms.test.tsx
git commit -m "refactor(frontend): share messaging workspace state"
```

---

### Task 3: Collapse per-agent Dockview tabs into one Messages panel

**Files:**
- Modify: `apps/frontend/src/components/workspace/panelRegistry.ts:16`
- Modify: `apps/frontend/src/components/workspace/workspaceController.ts:88`
- Modify: `apps/frontend/src/components/workspace/workspaceShellReconciler.ts:87`
- Modify: `apps/frontend/src/components/workspace/urlChannel.ts:100`
- Modify: `apps/frontend/src/components/workspace/layoutStorage.ts:20`
- Modify: `apps/frontend/src/components/workspace/buildDefaultLayout.ts:20`
- Modify: `apps/frontend/src/components/workspace/panels/ChatPanel.tsx:3`
- Test: `apps/frontend/src/components/workspace/__tests__/panelRegistry.test.ts:54`
- Test: `apps/frontend/src/components/workspace/__tests__/workspaceController.test.ts:187`
- Test: `apps/frontend/src/components/workspace/__tests__/workspaceShellUrl.test.ts:122`
- Test: `apps/frontend/src/components/workspace/__tests__/urlChannel.test.ts:40`
- Test: `apps/frontend/src/components/workspace/__tests__/layoutStorage.test.ts:326`
- Test: `apps/frontend/src/components/workspace/__tests__/buildDefaultLayout.test.ts:5`
- Test: `apps/frontend/src/components/workspace/__tests__/ChatPanel.test.tsx:61`

**Interfaces:**
- Consumes: Task 2's `useMessagingWorkspace()` and `MessagingConversation`, existing `useDockPanelParams`, and existing `addOrFocus` parameter updates.
- Produces `MESSAGES_PANEL_ID = "chat:__default__"`, `activateMessagesPanel(agentId?: string): void`, singleton `openChatPanel(agentId)`, and snapshot agent IDs read from panel parameters.

- [ ] **Step 1: Rewrite focused tests around singleton identity**

```ts
expect(describePanel("chat", { agent: "agent-a" })?.panelId).toBe(MESSAGES_PANEL_ID);
expect(describePanel("chat", { agent: "agent-b" })?.panelId).toBe(MESSAGES_PANEL_ID);
expect(describePanel("chat", { agent: "agent-b" })?.params).toEqual({ agentId: "agent-b" });
```

Replace the controller's per-agent case with:

```ts
it("retargets one Messages panel for every agent open", () => {
  const api = makeFakeApi();
  attachDockviewApi(api, () => "agent-friday");
  openChatPanel("agent-friday");
  openChatPanel("agent-atlas");

  expect(api.addPanel).toHaveBeenCalledTimes(1);
  expect(api.getPanel(MESSAGES_PANEL_ID)?.api.updateParameters)
    .toHaveBeenLastCalledWith({ agentId: "agent-atlas" });
  expect(_snapshotForTests().focusedChatAgentId).toBe("agent-atlas");
});
```

Make the fake panel's `updateParameters` merge into `this.params` before firing active/layout callbacks, matching Dockview.

Add the generic-room case to the controller test:

```ts
activateMessagesPanel();
expect(api.getPanel(MESSAGES_PANEL_ID)?.api.updateParameters)
  .toHaveBeenLastCalledWith({ agentId: undefined });
expect(_snapshotForTests().focusedChatAgentId).toBeNull();
```

- [ ] **Step 2: Add legacy layout migration tests**

Replace the dead-agent multi-chat cases with a layout containing two chat panels plus Browser. Make Atlas active and assert:

```ts
type GridNode = SerializedDockview["grid"]["root"];

function allGridViews(node: GridNode): string[] {
  if (node.type === "branch") return (node.data as GridNode[]).flatMap(allGridViews);
  return [...((node.data as unknown as { views: string[] }).views ?? [])];
}

expect(Object.keys(clean.panels).sort()).toEqual([MESSAGES_PANEL_ID, "browser"].sort());
expect(clean.panels[MESSAGES_PANEL_ID].params).toEqual({ agentId: "agent-atlas" });
expect(allGridViews(clean.grid.root).filter((id) => id === MESSAGES_PANEL_ID)).toHaveLength(1);
expect(clean.panels.browser).toBeDefined();
```

Add a case where the winner is absent from settled `knownAgentIds`; its params become `{ agentId: DEFAULT_AGENT_SENTINEL }` rather than deleting Messages.

- [ ] **Step 3: Run the tests and confirm per-agent behavior fails**

```bash
cd apps/frontend
pnpm test -- src/components/workspace/__tests__/panelRegistry.test.ts src/components/workspace/__tests__/workspaceController.test.ts src/components/workspace/__tests__/layoutStorage.test.ts src/components/workspace/__tests__/ChatPanel.test.tsx
```

Expected: FAIL on per-agent IDs, duplicate panels, and absent migration.

- [ ] **Step 4: Define and use the singleton descriptor**

Keep `chatPanelId(agentId)` for old ID parsing/migration and add:

```ts
export const MESSAGES_PANEL_ID = chatPanelId(DEFAULT_AGENT_SENTINEL);
```

Change the chat descriptor to:

```ts
return {
  panelId: MESSAGES_PANEL_ID,
  component: "chat",
  title: "Messages",
  params: { agentId },
  renderer: "always",
};
```

Use `MESSAGES_PANEL_ID` in `buildMissionControlLayout`. `durableParamsFor` keeps matching the `chat:` prefix, so URL grammar remains unchanged.

- [ ] **Step 5: Simplify controller open/focus and preserve snapshot consumers**

```ts
export function openChatPanel(agentId: string): void {
  const descriptor = describePanel("chat", { agent: agentId });
  if (descriptor) addOrFocus(descriptor);
}

export function activateMessagesPanel(agentId?: string): void {
  const descriptor = describePanel("chat", { agent: agentId ?? DEFAULT_AGENT_SENTINEL });
  if (!descriptor) return;
  addOrFocus({ ...descriptor, params: agentId ? { agentId } : {} });
}
```

Add `MESSAGES_PANEL_ID` to `clearablePanelParamKeys` with `["agentId"]`, so selecting a channel, group DM, human DM, or newly-created room clears a stale agent target. Update `agentIdOfChatPanel` to accept only `MESSAGES_PANEL_ID`, read `panel.params.agentId`, and resolve the sentinel through `_resolveDefaultAgentId`; an absent parameter reports no visible agent. Call `recomputeSnapshot()` after parameter updates so `RunNotifications` and `FileViewerDockPanel` immediately see the new agent. Remove `closeChatPanelIfOpen`; agent deletion now lives in Agents and must not close shared Messages.

Simplify `isCommandTargetOpen`: any chat command is open when `MESSAGES_PANEL_ID` is present. Keep the current `agentsSettled` reconciler gate; changing onboarding/deep-link timing is outside this UI work.

- [ ] **Step 6: Migrate stored multi-chat layouts in place**

Choose one chat winner before the existing sanitizer loop: active view in `raw.activeGroup`, then existing `MESSAGES_PANEL_ID`, then first serialized chat. Rename it to `MESSAGES_PANEL_ID` and drop other chat panels through the existing `dropped` set. Preserve its agent while the roster is unknown (`knownAgentIds === null`), or when it is the sentinel or belongs to a settled non-empty roster; use the sentinel only when a settled non-empty roster excludes it.

Use one tree reader:

```ts
function activeViewInGroup(node: GridNode, groupId: string | undefined): string | undefined {
  if (node.type === "branch") {
    for (const child of node.data as GridNode[]) {
      const active = activeViewInGroup(child, groupId);
      if (active) return active;
    }
    return undefined;
  }
  const leaf = node.data as unknown as { id?: string; activeView?: string };
  return groupId === undefined || leaf.id === groupId ? leaf.activeView : undefined;
}
```

Let existing grid migration prune empty leaves and collapse branches. Do not bump the v3 storage key: valid auxiliary panels must survive.

- [ ] **Step 7: Make `ChatPanel` parameter-driven**

```tsx
export function ChatPanel(props: IDockviewPanelProps<ChatPanelParams>): React.ReactElement {
  const params = useDockPanelParams(props);
  const { defaultId } = useAgents();
  const { selectAgentDm } = useMessagingWorkspace();
  const lastRequested = useRef<string | null>(null);
  const requested = params.agentId === DEFAULT_AGENT_SENTINEL
    ? defaultId ?? null
    : params.agentId ?? null;

  useEffect(() => {
    if (!requested) {
      lastRequested.current = null;
      return;
    }
    if (lastRequested.current === requested) return;
    lastRequested.current = requested;
    void selectAgentDm(requested).catch(() => {
      lastRequested.current = null;
    });
  }, [requested, selectAgentDm]);

  return <MessagingConversation />;
}
```

Make `ChatPanelParams.agentId` optional and import `useRef`. The ref makes an agent parameter a one-shot selection intent: ordinary room changes do not snap back to the last agent, while clearing the parameter lets a later same-agent command retarget correctly. Keep the dock title `Messages`; the room header carries channel/DM identity.

- [ ] **Step 8: Update URL and default-layout expectations**

In the URL/reconciler/default-layout tests, retain `{ agentId: "agent-id" }` assertions but expect `MESSAGES_PANEL_ID` for every chat descriptor. Closing Messages must still clear bare `?agent`, `?panel=chat&agent=`, and `?panel=chat&agentId=`.

- [ ] **Step 9: Run the workspace-controller cluster**

```bash
cd apps/frontend
pnpm test -- src/components/workspace/__tests__/panelRegistry.test.ts src/components/workspace/__tests__/workspaceController.test.ts src/components/workspace/__tests__/workspaceShellUrl.test.ts src/components/workspace/__tests__/urlChannel.test.ts src/components/workspace/__tests__/layoutStorage.test.ts src/components/workspace/__tests__/buildDefaultLayout.test.ts src/components/workspace/__tests__/ChatPanel.test.tsx
```

Expected: PASS with one Messages panel, current agent parameters in snapshots, auxiliary panels retained, and old URLs valid.

- [ ] **Step 10: Commit the singleton increment**

```bash
git add apps/frontend/src/components/workspace
git commit -m "feat(frontend): converge chat on one messages panel"
```

---

### Task 4: Converge the desktop shell on one primary sidebar

**Files:**
- Modify: `apps/frontend/src/components/workspace/WorkspaceSidebar.tsx:1`
- Modify: `apps/frontend/src/components/workspace/WorkspaceShell.tsx:39`
- Modify: `apps/frontend/src/components/workspace/WorkspaceHeader.tsx:3`
- Modify: `apps/frontend/src/components/workspace/workspace.css:1`
- Test: `apps/frontend/src/components/workspace/__tests__/WorkspaceSidebar.test.tsx:1`
- Test: `apps/frontend/src/components/workspace/__tests__/WorkspaceShell.test.tsx` (present after syncing merged PR #2433)
- Test: `apps/frontend/src/components/workspace/__tests__/WorkspaceHeader.test.tsx:36`

**Interfaces:**
- Consumes: Task 2's provider/directory; Task 3's `MESSAGES_PANEL_ID`, `activateMessagesPanel`, `openPanel`, and `useWorkspaceState`.
- Produces: one 248px desktop sidebar with `aria-label="Workspace"`, canonical conversations, native More disclosure, and no nested session navigation.

- [ ] **Step 1: Replace roster-oriented tests with the approved IA contract**

Mock `MessagingDirectory` as an identifiable navigation and assert the primary order and preserved exits:

```tsx
it("renders one message-first desktop navigation", () => {
  render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

  const workspace = screen.getByRole("navigation", { name: "Workspace" });
  expect(within(workspace).getByRole("button", { name: "Home" })).toBeInTheDocument();
  expect(within(workspace).getByRole("button", { name: "Activity" })).toBeInTheDocument();
  expect(within(workspace).getByRole("button", { name: "Tasks" })).toBeInTheDocument();
  expect(within(workspace).getByRole("button", { name: "Browser" })).toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "Conversations" })).toBeInTheDocument();
  expect(screen.queryByText("New session")).not.toBeInTheDocument();
  expect(screen.getByText("Marketplace")).toBeInTheDocument();
});
```

Add room-open tests proving an agent DM calls `activateMessagesPanel(agentId)` while a channel and new-conversation action call `activateMessagesPanel()` to clear a stale agent target. Add active-state tests for Activity and Browser via `aria-current="page"`. Keep existing `chatReady` and health-recovery assertions that still apply.

- [ ] **Step 2: Run sidebar/header tests and confirm the old structure fails**

```bash
cd apps/frontend
pnpm test -- src/components/workspace/__tests__/WorkspaceSidebar.test.tsx src/components/workspace/__tests__/WorkspaceHeader.test.tsx
```

Expected: FAIL because the roster/session rail remains and layout reset is still promoted.

- [ ] **Step 3: Delete roster/session management from `WorkspaceSidebar`**

Remove `AgentRoster`, `SessionList`, archived sessions, agent/session dialogs, runtime config, published-agent decoration, and their handlers/imports. Preserve the logo/settings link, `HealthIndicator`, `NodeStatusPill`, marketplace hire-completion `emitStep("hire")` effect, billing footer, `FreeTierNote`, `DesktopUpdater`, `chatReady`, and health recovery callback.

Do not recreate removed management actions in the sidebar. The existing `teams-agents` panel owns agent creation, rename, deletion, and inspection.

- [ ] **Step 4: Render primary destinations, conversations, and native More**

```tsx
const PRIMARY_NAV = [
  { panel: "teams-dashboard", label: "Home", Icon: Home },
  { panel: "teams-inbox", label: "Activity", Icon: Bell },
  { panel: "teams-issues", label: "Tasks", Icon: ListTodo },
  { panel: "browser", label: "Browser", Icon: Chrome },
] as const;
```

Call `visibleCompanySections` with the existing Stripe provisioning flag, flatten it, and exclude dashboard, search, inbox, and issues. Prefix the remainder with `{ panel: "teams-agents", label: "Agents", Icon: UsersRound }`. Render it under native `<details><summary>More</summary>…</details>` so Domain & Site keeps the existing capability filter.

The main structure is:

```tsx
<nav className="workspace-primary-nav" aria-label="Workspace">
  {PRIMARY_NAV.map(({ panel, label, Icon }) => (
    <button
      key={panel}
      type="button"
      aria-current={activePanelId === panel ? "page" : undefined}
      disabled={!chatReady}
      onClick={() => openPanel(panel, {}, { pushUrl: true })}
    >
      <Icon size={16} aria-hidden="true" />
      <span>{label}</span>
    </button>
  ))}
</nav>
{chatReady
  ? <MessagingDirectory onOpenRoom={(room) => {
      const agent = room?.kind === "dm"
        ? room.participants.find((participant) => participant.kind === "agent")
        : undefined;
      activateMessagesPanel(agent?.id);
    }} />
  : <p role="status" className="workspace-conversations-disabled">Conversations are available when your workspace is ready.</p>}
```

Keep `GallerySection` and the account footer below the shared scroll region.

- [ ] **Step 5: Mount the messaging provider once in `WorkspaceShell`**

```tsx
function DesktopMessagingProvider({ children }: { children: React.ReactNode }) {
  const { activePanelId } = useWorkspaceState();
  return (
    <MessagingWorkspaceProvider subscribeToRoom={activePanelId === MESSAGES_PANEL_ID}>
      {children}
    </MessagingWorkspaceProvider>
  );
}
```

Inside the existing `TeamsEventsProvider`, wrap both `WorkspaceSidebar` and the Dockview main area with `DesktopMessagingProvider`. Do not move or change `useOnboardingGate`, Clerk organization scoping, provisioning steppers, banners, checkout-return handling, URL reconciliation, or `disableDnd` from merged #2433.

- [ ] **Step 6: Remove promoted layout reset without removing recovery**

Delete `onResetLayout` and the `RotateCcw` button from `WorkspaceHeader`. Remove `handleResetLayout` and now-unused imports from `WorkspaceShell`. Leave `readLayout`/`sanitizeLayout` fallback, Dockview, and `disableDnd` intact.

Update the header test to render `<WorkspaceHeader />` and assert no button named `Reset workspace layout` exists while files, browser, search, help, and profile remain.

- [ ] **Step 7: Apply the 248px shell and compact sidebar styles**

```css
.workspace-shell {
  grid-template-columns: 248px minmax(0, 1fr);
}

.workspace-primary-nav,
.workspace-more-nav {
  display: grid;
  gap: 2px;
  padding: 8px 12px;
}

.workspace-primary-nav button,
.workspace-more-nav button,
.workspace-more-nav summary {
  min-height: 36px;
  border-radius: 4px;
  padding: 6px 8px;
  color: var(--midnight-soft);
}

.workspace-primary-nav button:hover,
.workspace-primary-nav button[aria-current="page"],
.workspace-more-nav button:hover,
.workspace-more-nav button[aria-current="page"] {
  background: var(--cream-deeper);
  color: var(--midnight);
}
```

Scope overrides under `.workspace-shell` so mobile `ChatLayout.css` does not change. Delete the unused `.team-room*` block from `workspace.css`; the current repository has no markup consumer.

- [ ] **Step 8: Run shell/sidebar regression tests**

```bash
cd apps/frontend
pnpm test -- src/components/workspace/__tests__/WorkspaceSidebar.test.tsx src/components/workspace/__tests__/WorkspaceShell.test.tsx src/components/workspace/__tests__/WorkspaceHeader.test.tsx src/components/workspace/__tests__/buildDefaultLayout.test.ts src/components/workspace/__tests__/workspaceController.test.ts
```

Expected: PASS with #2433 loading/organization guards preserved and one desktop conversation navigation.

- [ ] **Step 9: Commit shell convergence**

```bash
git add apps/frontend/src/components/workspace
git commit -m "feat(frontend): make messaging the desktop workspace"
```

---

### Task 5: Give canonical DMs names and truthful agent identity

**Files:**
- Create: `apps/frontend/src/components/messaging/ParticipantAvatar.tsx`
- Create: `apps/frontend/src/components/messaging/__tests__/ParticipantAvatar.test.tsx`
- Create: `apps/frontend/src/components/messaging/__tests__/RoomDirectory.test.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomDirectory.tsx:17`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx:187`
- Modify: `apps/frontend/src/components/messaging/types.ts:19`
- Modify: `apps/frontend/src/components/messaging/messaging.css:59`

**Interfaces:**
- Consumes: Task 2's `DirectoryRoom`, enriched participants, and existing `Identity`.
- Produces: `ParticipantAvatar({ participant, size }): ReactElement` and `roomDisplayName(room, participants)` for participant-derived null-named canonical DMs/group DMs.

- [ ] **Step 1: Write failing avatar semantics tests**

```tsx
it.each([
  ["running", "Agent working"],
  ["attention", "Agent needs attention"],
  ["paused", "Agent paused"],
] as const)("labels %s activity without relying on color", (state, label) => {
  render(<ParticipantAvatar participant={{
    kind: "agent", id: "a-1", displayName: "Friday", mentionHandle: "friday", agentActivity: state,
  }} size="sm" />);
  expect(screen.getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-agent-state", state);
  expect(screen.getByText(label)).toHaveClass("sr-only");
});

it.each(["idle", "terminated", "unknown"] as const)("does not invent a halo for %s", (state) => {
  render(<ParticipantAvatar participant={{ kind: "agent", id: "a-1", displayName: "Friday", mentionHandle: null, agentActivity: state }} />);
  expect(screen.getByRole("img", { name: "Friday" }).parentElement).not.toHaveAttribute("data-agent-state");
});
```

Also assert a human avatar has no agent-state attribute or agent-status wording.

- [ ] **Step 2: Write the failing directory-name test**

Render one channel, one null-named agent DM, and one null-named group DM using `DirectoryRoom.participants`:

```tsx
expect(screen.getByRole("button", { name: /Friday/ })).toBeInTheDocument();
expect(screen.getByRole("button", { name: /Alice and Atlas/ })).toBeInTheDocument();
expect(screen.queryByRole("button", { name: /^Direct message$/ })).not.toBeInTheDocument();
```

Click Friday and assert `onSelectRoom(fridayRoomId)`. Retain an unread-count assertion on that row.

- [ ] **Step 3: Run new tests and confirm visual semantics are absent**

```bash
cd apps/frontend
pnpm test -- src/components/messaging/__tests__/ParticipantAvatar.test.tsx src/components/messaging/__tests__/RoomDirectory.test.tsx
```

Expected: FAIL because shared avatar activity and participant-derived labels do not exist.

- [ ] **Step 4: Implement the avatar by reusing `Identity`**

```tsx
const ACTIVITY_LABEL = {
  running: "Agent working",
  attention: "Agent needs attention",
  paused: "Agent paused",
} as const;

interface ParticipantAvatarProps {
  participant: DirectoryParticipant | RoomParticipant;
  size?: "xs" | "sm" | "default" | "lg";
}

export function ParticipantAvatar({ participant, size = "default" }: ParticipantAvatarProps) {
  const activity = participant.kind === "agent" ? participant.agentActivity : undefined;
  const label = activity && activity in ACTIVITY_LABEL
    ? ACTIVITY_LABEL[activity as keyof typeof ACTIVITY_LABEL]
    : undefined;
  return (
    <span className="participant-avatar" data-agent-state={label ? activity : undefined}>
      <Identity name={participant.displayName} avatarOnly size={size} />
      {label && <span className="sr-only">{label}</span>}
    </span>
  );
}
```

Do not create another avatar/initials implementation.

- [ ] **Step 5: Render DM identity from canonical hydrated members**

Change `RoomDirectoryProps.rooms` to `readonly DirectoryRoom[]`. Add one shared helper to `types.ts` because both the directory row and room header need the same identity:

```ts
export function roomDisplayName(
  room: Pick<CanonicalRoomSummary, "kind" | "name"> | undefined,
  participants: readonly Pick<RoomMember, "displayName">[],
): string {
  if (!room) return "Conversation";
  if (room.name) return room.name;
  const names = participants.map((participant) => participant.displayName);
  if (room.kind === "dm") return names[0] ?? "Direct message";
  if (room.kind === "group-dm") return names.length ? names.join(" and ") : "Group message";
  return "Untitled channel";
}
```

Call `roomDisplayName(room, room.participants)` in `RoomDirectory`. In `RoomPanel`, call it with participants excluding the signed-in human so the center header and accessible room label also say `Friday` or `Alice and Atlas`, never the owner or generic `Direct message`.

Use `ParticipantAvatar` for a one-counterpart DM, the group icon for group DMs, and hash for channels. In People and agents, show `Agent` for an agent without a mention handle instead of the current `Person` fallback.

Use `ParticipantAvatar` in `RoomPanel` for message authors found in `participants` and for member rows. Replace the old member-row `WorkStatus`, then remove the transitional `agentState` field; `agentActivity` is the only participant activity field afterward. Keep `WorkStatus` for message, run, and context status. System/unknown authors retain `Identity`; the restrained visible `Agent` byline remains.

- [ ] **Step 6: Add halo styles and reduced-motion fallback**

```css
.participant-avatar {
  position: relative;
  display: inline-flex;
  flex: none;
  border-radius: 999px;
}
.participant-avatar[data-agent-state="running"] {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--brass) 68%, transparent);
  animation: agent-activity-halo 1.8s var(--ease-out) infinite;
}
.participant-avatar[data-agent-state="attention"] { box-shadow: 0 0 0 2px var(--err); }
.participant-avatar[data-agent-state="paused"] { box-shadow: 0 0 0 2px var(--warn); }
@keyframes agent-activity-halo {
  50% { box-shadow: 0 0 0 4px color-mix(in srgb, var(--brass) 20%, transparent); }
}
@media (prefers-reduced-motion: reduce) {
  .participant-avatar[data-agent-state="running"] { animation: none; }
}
```

Do not style idle, terminated, or unknown as online/offline.

- [ ] **Step 7: Run directory, avatar, and provider tests**

```bash
cd apps/frontend
pnpm test -- src/components/messaging/__tests__/ParticipantAvatar.test.tsx src/components/messaging/__tests__/RoomDirectory.test.tsx src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx
```

Expected: PASS with names and activity sourced from canonical data.

- [ ] **Step 8: Commit identity work**

```bash
git add apps/frontend/src/components/messaging
git commit -m "feat(frontend): show people and agents as message peers"
```

---

### Task 6: Build the center conversation and contextual right pane

**Files:**
- Create: `apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx:131`
- Modify: `apps/frontend/src/components/messaging/messaging.css:11`
- Modify: `apps/frontend/src/hooks/useIsDesktop.ts:5`
- Modify: `apps/frontend/src/hooks/__tests__/useIsDesktop.test.tsx:11`
- Modify: `apps/frontend/src/components/chat/ChatLayout.css:874`

**Interfaces:**
- Consumes: existing `RoomPanelProps`, message/thread/context/run data, and Task 5's `ParticipantAvatar`.
- Produces: wide 320px default context, medium 320px selected-detail overlay, the current mobile shell through 859px, local collapse state, Escape handling, and focus restoration.

- [ ] **Step 1: Write failing wide/default-context and collapse tests**

Render `RoomPanel` with two participants and one running `RoomRunView`, then assert:

```tsx
expect(screen.getByRole("heading", { name: "Friday" })).toBeInTheDocument();
expect(screen.getByRole("complementary", { name: "Room details" })).toBeInTheDocument();
expect(within(screen.getByRole("complementary", { name: "Room details" })).getByText("Friday")).toBeInTheDocument();
expect(screen.getByRole("button", { name: "Hide room details" })).toHaveAttribute("aria-expanded", "true");

fireEvent.click(screen.getByRole("button", { name: "Hide room details" }));
expect(screen.queryByRole("complementary", { name: "Room details" })).not.toBeInTheDocument();
expect(screen.getByRole("button", { name: "Show room details" })).toHaveFocus();
```

- [ ] **Step 2: Add failing thread focus-restoration tests**

```tsx
const reply = screen.getByRole("button", { name: "Reply" });
fireEvent.click(reply);
expect(screen.getByRole("complementary", { name: "Thread" })).toBeInTheDocument();
expect(screen.getByRole("heading", { name: "Thread" })).toHaveFocus();
fireEvent.keyDown(screen.getByRole("complementary", { name: "Thread" }), { key: "Escape" });
expect(reply).toHaveFocus();
```

Add a `narrow` case asserting the center conversation is hidden only while selected thread/context detail is open.

In `useIsDesktop.test.tsx`, change the expected query to `(min-width: 860px)`. This must fail until the JavaScript shell boundary and `ChatLayout.css` mobile boundary move together.

- [ ] **Step 3: Run the new suite and verify default context is absent**

```bash
cd apps/frontend
pnpm test -- src/components/messaging/__tests__/RoomPanel.test.tsx
```

Expected: FAIL because only a selected thread/context currently creates the right pane.

- [ ] **Step 4: Add local default-context collapse state**

Import the existing class helper rather than adding another conditional-class utility:

```tsx
import { cn } from "@/lib/utils";
```

Keep existing selection/focus refs and add:

```tsx
const [contextCollapsed, setContextCollapsed] = useState(false);
const showDetail = !!(parentMessage || selectedContext);
const showContext = showDetail || !contextCollapsed;
```

Use distinct classes so CSS can separate wide default context from selected detail:

```tsx
<section className={cn(
  "room-panel",
  showContext && "room-has-context",
  showDetail ? "room-has-detail" : "room-has-default-context",
)} aria-label={roomName}>
```

When `!showDetail`, add one header button with class `room-details-toggle`, labelled `Hide room details` / `Show room details`, and `aria-expanded={!contextCollapsed}`. The same button remains focused when its label changes. Selected details still close through Back or Escape.

- [ ] **Step 5: Render default context from existing props only**

When no thread/context is selected, render members and current running work. Compute `activeRuns` once and do not render the default aside while it is collapsed:

```tsx
const activeRuns = runs.filter((run) => run.state === "running");

{showContext && !showDetail && <aside className="room-context" aria-label="Room details">
  <header className="room-context-header">
    <h3 className="font-serif text-lg">Room details</h3>
  </header>
  <div className="room-context-body">
    <section aria-labelledby={`${id}-context-members`}>
      <h4 id={`${id}-context-members`}>Members</h4>
      <ul>{participants.map((participant) => (
        <li key={`${participant.kind}:${participant.id}`} className="room-member">
          <ParticipantAvatar participant={participant} size="sm" />
          <span>{participant.displayName}</span>
          <span className="text-sm text-muted-foreground">
            {participant.kind === "agent" ? "Agent" : "Person"}
          </span>
        </li>
      ))}</ul>
    </section>
    <section aria-labelledby={`${id}-context-runs`}>
      <h4 id={`${id}-context-runs`}>Active work</h4>
      {activeRuns.length === 0
        ? <p className="text-sm text-muted-foreground">No active runs.</p>
        : activeRuns.map((run) => (
            <p key={run.runId}>{participants.find((p) => p.id === run.agentId)?.displayName ?? "Agent"} is working</p>
          ))}
    </section>
  </div>
</aside>}
```

Keep the existing selected thread/context body and `onLinkToWork`. Keep the existing Members disclosure for medium/mobile access, but give its button `.room-members-trigger` and hide that duplicate at the wide breakpoint.

- [ ] **Step 6: Implement the three responsive modes in CSS**

```css
.room-panel { position: relative; grid-template-columns: minmax(0, 1fr); }
.room-context { display: none; width: 320px; }
.room-details-toggle { display: none; }

@media (min-width: 1180px) {
  .room-details-toggle { display: inline-flex; }
  .room-panel.room-has-context { grid-template-columns: minmax(0, 1fr) 320px; }
  .room-panel.room-has-context > .room-context { display: flex; width: auto; }
  .room-members-trigger { display: none; }
}

@media (min-width: 860px) and (max-width: 1179px) {
  .room-panel.room-has-detail > .room-context {
    position: absolute;
    z-index: 3;
    inset: 0 0 0 auto;
    display: flex;
    width: 320px;
    box-shadow: -16px 0 32px color-mix(in srgb, var(--midnight) 12%, transparent);
  }
}

@media (max-width: 859px) {
  .room-panel.room-has-detail > .room-context { display: flex; width: 100%; }
}
```

Change `useIsDesktop` to `(min-width: 860px)` and the matching `ChatLayout.css` block to `@media (max-width: 859px)`, including the nearby comments that pin them as complements. Retain `hidden={showDetail && isNarrow}`, 44px mobile controls, safe-area composer padding, and reduced motion. Tighten desktop header/history/composer padding to the 4px rhythm without shrinking mobile targets.

- [ ] **Step 7: Run message component tests**

```bash
cd apps/frontend
pnpm test -- src/components/messaging/__tests__/RoomPanel.test.tsx src/components/messaging/__tests__/RoomDirectory.test.tsx src/components/messaging/__tests__/RoomComposer.focus.test.tsx src/components/messaging/__tests__/ParticipantAvatar.test.tsx src/hooks/__tests__/useIsDesktop.test.tsx
```

Expected: PASS for default context, responsive detail state, focus restoration, activity semantics, and composer behavior.

- [ ] **Step 8: Commit the three-region conversation**

```bash
git add apps/frontend/src/components/messaging apps/frontend/src/hooks/useIsDesktop.ts apps/frontend/src/hooks/__tests__/useIsDesktop.test.tsx apps/frontend/src/components/chat/ChatLayout.css
git commit -m "feat(frontend): add contextual room details"
```

---

### Task 7: Verify the complete desktop story and preserve mobile

**Files:**
- Modify: `apps/frontend/tests/e2e/workspace.spec.ts` (present after syncing merged PR #2433)

**Interfaces:**
- Consumes: Tasks 1–6 and PR #2433's authenticated workspace fixture.
- Produces: one end-to-end workspace assertion and recorded manual visual verification; no runtime interface.

- [ ] **Step 1: Add one end-to-end message-first workspace test**

Extend the merged workspace journey after its existing sign-in/provisioning setup:

```ts
await expect(page.getByRole("navigation", { name: "Workspace" })).toBeVisible();
const conversations = page.getByRole("navigation", { name: "Conversations" });
await expect(conversations).toBeVisible();
await expect(page.getByRole("button", { name: "Home" })).toBeVisible();
await expect(page.getByRole("button", { name: "Activity" })).toBeVisible();
await expect(page.getByRole("button", { name: "Tasks" })).toBeVisible();
await expect(page.getByRole("button", { name: "Browser" })).toBeVisible();
await expect(page.getByText("New session")).toHaveCount(0);
await expect(page.getByRole("button", { name: "Reset workspace layout" })).toHaveCount(0);

await conversations.locator(".room-directory-entry").first().click();
await expect(page.getByRole("region", { name: "Messages" })).toBeVisible();
const composer = page.getByRole("textbox", { name: "Message" });
await composer.fill("Workspace UI smoke");
await page.getByRole("button", { name: "Send message" }).click();
const sent = page.getByRole("article", { name: /Message from/ })
  .filter({ hasText: "Workspace UI smoke" })
  .last();
await sent.getByRole("button", { name: "Reply" }).click();
await expect(page.getByRole("complementary", { name: "Thread" })).toBeVisible();
await page.keyboard.press("Escape");
await expect(sent.getByRole("button", { name: "Reply" })).toBeFocused();
```

Keep the journey roster-driven; do not assert a named starter agent.

- [ ] **Step 2: Run focused unit and end-to-end checks**

```bash
cd apps/frontend
pnpm test -- src/hooks/__tests__/useRooms.test.tsx src/components/messaging/__tests__ src/components/workspace/__tests__/panelRegistry.test.ts src/components/workspace/__tests__/workspaceController.test.ts src/components/workspace/__tests__/workspaceShellUrl.test.ts src/components/workspace/__tests__/urlChannel.test.ts src/components/workspace/__tests__/layoutStorage.test.ts src/components/workspace/__tests__/buildDefaultLayout.test.ts src/components/workspace/__tests__/ChatPanel.test.tsx src/components/workspace/__tests__/WorkspaceSidebar.test.tsx src/components/workspace/__tests__/WorkspaceShell.test.tsx src/components/workspace/__tests__/WorkspaceHeader.test.tsx
pnpm run test:e2e -- tests/e2e/workspace.spec.ts
```

Expected: all focused tests pass. If the sanctioned local E2E environment is unavailable, run the repository's on-demand `e2e-dev.yml` after a PR exists; do not hardcode credentials.

- [ ] **Step 3: Run complete frontend gates**

```bash
cd apps/frontend
pnpm test
pnpm lint
pnpm build
```

Expected: all tests pass, ESLint reports no errors, and Next.js completes a production build.

- [ ] **Step 4: Perform desktop and responsive visual verification**

Use the sanctioned dev sign-in-token path from `AGENTS.md` and verify:

- 1440×900: 248px sidebar, center room, and 320px default context visible;
- 1100×800: sidebar and center fixed; selected thread/context overlays at 320px;
- 859px and below: current mobile composition usable; detail replaces conversation with Back;
- long names truncate without covering unread counts or controls;
- empty room lists, reconnect, access removed, pending send, and active run states remain legible;
- running halo is restrained; attention/paused is static; unknown/idle/terminated has none;
- Tab reaches workspace navigation, room navigation, header, history, composer, and context;
- Escape closes detail and restores focus;
- reduced motion removes halo animation and transitions.

Capture temporary comparison screenshots; do not commit them.

- [ ] **Step 5: Review scope and hidden regressions**

```bash
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git status --short
rg -n "Friday|Atlas|Gauge|GStack" --glob '!**/__tests__/**' apps/frontend/src/components/messaging apps/frontend/src/components/workspace
rg -n "disableDnd" apps/frontend/src/components/workspace/WorkspaceShell.tsx
```

Expected: no whitespace errors, hardcoded starter roster, backend/onboarding/pricing/provisioning changes, mobile information-architecture changes beyond the approved breakpoint alignment, or missing `disableDnd`.

- [ ] **Step 6: Commit end-to-end verification**

```bash
git add apps/frontend/tests/e2e/workspace.spec.ts
git commit -m "test(frontend): cover message-first workspace"
```

- [ ] **Step 7: Use the required completion workflow**

Invoke `superpowers:verification-before-completion`, then `superpowers:requesting-code-review`. Resolve findings with the relevant focused test and rerun complete frontend gates. Do not claim the feature is live: merge deploys dev only, and production requires an explicit `deploy.yml` workflow dispatch.

After the feature ships, delete this plan and its paired spec in the shipping PR or immediate cleanup PR, as required by the repository's disposable-planning-doc policy.
