# Mobile Agent Grid + "New Task" — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the mobile Chat-tab agent roster into a 2-column compact grid and add a discoverable "＋ New task" entry point that starts a fresh session with a picked agent.

**Architecture:** Client-only, on top of existing `agents.list` + sessions RPCs. New `AgentPickerSheet` component; `chat.tsx` re-rendered as a grid with a New-task button; a `newSession` route param honored by the agent chat screen.

**Tech Stack:** Expo / React Native, expo-router, `@isol8/shared`, jest-expo + @testing-library/react-native.

## Global Constraints

- **No color literals** — use `colors.*` from `src/config/theme` only. Fonts via `fonts.*`, spacing via `spacing.*`, radii via `radii.*`, text presets via `type.*`.
- **No backend or `packages/shared` changes.**
- **RNTL 14:** `render()` is async — `await render(...)`; import from `@testing-library/react-native`.
- All of `pnpm --filter @isol8/mobile typecheck` + `lint` + `export:ios` must pass at the end.
- Session ids must be lowercase (reuse the existing `newSession()` which lowercases).

---

### Task 1: `AgentPickerSheet` component

**Files:**
- Create: `apps/mobile/src/components/chat/AgentPickerSheet.tsx`
- Test: `apps/mobile/src/components/chat/__tests__/AgentPickerSheet.test.tsx`

**Interfaces:**
- Produces: `AgentPickerSheet` (default export not required; named export) with props
  `{ visible: boolean; onClose: () => void; agents: Agent[]; onPick: (agent: Agent) => void }`
  where `Agent` is the element type of `AgentsListResponse["agents"]`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/components/chat/__tests__/AgentPickerSheet.test.tsx
import { render, screen, fireEvent } from "@testing-library/react-native";
import { AgentPickerSheet } from "../AgentPickerSheet";

const agents = [
  { id: "agent_a", name: "Friday", identity: { emoji: "🤖" } },
  { id: "agent_b", name: "Atlas" },
] as never[];

test("renders agents and fires onPick with the chosen agent", async () => {
  const onPick = jest.fn();
  await render(
    <AgentPickerSheet visible agents={agents} onClose={() => {}} onPick={onPick} />,
  );
  fireEvent.press(screen.getByText("Atlas"));
  expect(onPick).toHaveBeenCalledWith(agents[1]);
});

test("scrim press closes", async () => {
  const onClose = jest.fn();
  await render(
    <AgentPickerSheet visible agents={agents} onClose={onClose} onPick={() => {}} />,
  );
  fireEvent.press(screen.getByLabelText("Close agent picker"));
  expect(onClose).toHaveBeenCalled();
});
```

- [ ] **Step 2: Run it, verify it fails** — `pnpm --filter @isol8/mobile test AgentPickerSheet` → FAIL (module not found).

- [ ] **Step 3: Implement** (model the shell on `SessionsSheet.tsx` — Modal + scrim + cream card + handle):

```tsx
// apps/mobile/src/components/chat/AgentPickerSheet.tsx
import { FlatList, Modal, Pressable, StyleSheet, Text, View } from "react-native";
import { agentDisplayName, type AgentsListResponse } from "@isol8/shared";
import { colors, fonts, radii, spacing } from "../../config/theme";

type Agent = AgentsListResponse["agents"][number];

interface Props {
  visible: boolean;
  onClose: () => void;
  agents: Agent[];
  onPick: (agent: Agent) => void;
}

/** Slide-up agent picker for "New task" — pick an agent to start a fresh session. */
export function AgentPickerSheet({ visible, onClose, agents, onPick }: Props) {
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <Pressable style={styles.scrim} onPress={onClose} accessibilityLabel="Close agent picker" />
      <View style={styles.card}>
        <View style={styles.handle} />
        <Text style={styles.overline}>START A NEW TASK WITH…</Text>
        <FlatList
          data={agents}
          keyExtractor={(a) => a.id}
          contentContainerStyle={styles.list}
          renderItem={({ item }) => (
            <Pressable
              style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
              onPress={() => onPick(item)}
              accessibilityRole="button"
              accessibilityLabel={agentDisplayName(item)}
            >
              <View style={styles.monogram}>
                <Text style={item.identity?.emoji ? styles.emoji : styles.letter}>
                  {item.identity?.emoji ?? (agentDisplayName(item).trim()[0] ?? "8").toUpperCase()}
                </Text>
              </View>
              <Text style={styles.name} numberOfLines={1}>{agentDisplayName(item)}</Text>
            </Pressable>
          )}
        />
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  scrim: { flex: 1, backgroundColor: colors.scrim },
  card: {
    backgroundColor: colors.cream,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    maxHeight: "70%",
    paddingBottom: spacing.xl,
  },
  handle: {
    alignSelf: "center", width: 36, height: 4, borderRadius: 2,
    backgroundColor: colors.edge, marginTop: spacing.sm,
  },
  overline: {
    fontFamily: fonts.mono, fontSize: 10, lineHeight: 14, letterSpacing: 1.4,
    color: colors.mute, paddingHorizontal: spacing.lg, paddingTop: spacing.lg, paddingBottom: spacing.sm,
  },
  list: { paddingHorizontal: spacing.sm, paddingBottom: spacing.sm },
  row: {
    flexDirection: "row", alignItems: "center", gap: 10,
    padding: spacing.sm, borderRadius: radii.md,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  monogram: {
    width: 28, height: 28, borderRadius: radii.md, backgroundColor: colors.forestTint,
    alignItems: "center", justifyContent: "center",
  },
  letter: { fontFamily: fonts.loraBoldItalic, fontSize: 14, color: colors.forest },
  emoji: { fontSize: 14 },
  name: { flex: 1, fontFamily: fonts.dmSansMedium, fontSize: 14, lineHeight: 18, color: colors.ink },
});
```

- [ ] **Step 4: Run tests, verify pass** — `pnpm --filter @isol8/mobile test AgentPickerSheet` → PASS (2/2).
- [ ] **Step 5: Commit** — `feat(mobile): AgentPickerSheet for New task`

---

### Task 2: Agent roster as a 2-column grid

**Files:**
- Modify: `apps/mobile/src/app/(app)/chat.tsx`

**Interfaces:**
- Consumes: `AgentsListResponse`, `agentDisplayName`, `useGatewayRpc` (unchanged).

- [ ] **Step 1: Convert the `FlatList` to a 2-column grid.** Add `numColumns={2}` and `columnWrapperStyle={styles.column}`; replace `renderItem` with a **tile** (not a row). Keep `ListHeaderComponent` (AGENTS · N), `ListEmptyComponent`, `HealthRow`, error/`connectionDown` branches.

```tsx
// renderItem — tile form
renderItem={({ item }) => (
  <Pressable
    style={({ pressed }) => [styles.tile, pressed && styles.tilePressed]}
    onPress={() =>
      router.push({
        pathname: "/(app)/agent/[agentId]",
        params: { agentId: item.id, name: agentDisplayName(item) },
      })
    }
  >
    <Monogram name={agentDisplayName(item)} emoji={item.identity?.emoji ?? undefined} big />
    <Text style={styles.tileName} numberOfLines={2}>{agentDisplayName(item)}</Text>
    <View style={styles.tileStatus}>
      <View style={styles.statusDot} />
      <Text style={styles.statusLabel}>Active</Text>
    </View>
  </Pressable>
)}
```

- [ ] **Step 2: Extend `Monogram`** to take an optional `big` prop (44px tile, 20px glyph) and center it; keep the 28px default for other callers.

```tsx
function Monogram({ name, emoji, big }: { name: string; emoji?: string; big?: boolean }) {
  return (
    <View style={[styles.monogram, big && styles.monogramBig]}>
      <Text style={[big ? styles.monogramGlyphBig : styles.monogramLetter, emoji && styles.monogramEmoji]}>
        {emoji ?? (name.trim()[0] ?? "8").toUpperCase()}
      </Text>
    </View>
  );
}
```

- [ ] **Step 3: Add grid styles** (replace `row`/`rowPressed`/`rowText`/`name`/`id` with tile equivalents; keep `monogram`/`monogramLetter`/`monogramEmoji`):

```tsx
column: { gap: spacing.sm, paddingHorizontal: spacing.md },
tile: {
  flex: 1, minHeight: 120, alignItems: "center", justifyContent: "center",
  gap: spacing.sm, paddingVertical: spacing.lg, paddingHorizontal: spacing.sm,
  backgroundColor: colors.cream, borderWidth: 1, borderColor: colors.edge,
  borderRadius: radii.lg, marginBottom: spacing.sm,
},
tilePressed: { backgroundColor: colors.creamPressed },
monogramBig: { width: 44, height: 44, borderRadius: radii.lg },
monogramGlyphBig: { fontFamily: fonts.loraBoldItalic, fontSize: 20, color: colors.forest },
tileName: {
  fontFamily: fonts.dmSansMedium, fontSize: 14, lineHeight: 18, color: colors.ink, textAlign: "center",
},
tileStatus: { flexDirection: "row", alignItems: "center", gap: spacing.xs },
statusDot: { width: 7, height: 7, borderRadius: 4, backgroundColor: colors.forestSoft },
statusLabel: { fontFamily: fonts.dmSans, fontSize: 11, lineHeight: 14, color: colors.mute },
```

- [ ] **Step 4: Grid-shape the skeleton** — `SkeletonRows` → render 4 tiles in a 2-col wrap (`skeletonTile: { flex:1, minHeight:120, borderRadius: radii.lg, backgroundColor: colors.skeleton }`), pulsing via `usePulse`. `listContent` keeps `paddingBottom`; remove the now-unused `list` row paddings that conflict.

- [ ] **Step 5: Verify** — `pnpm --filter @isol8/mobile typecheck` (no unused-style / type errors), then `export:ios` compiles.
- [ ] **Step 6: Commit** — `feat(mobile): agent roster as a 2-column grid`

---

### Task 3: "＋ New task" button + picker wiring on the roster

**Files:**
- Modify: `apps/mobile/src/app/(app)/chat.tsx`

- [ ] **Step 1:** Add local state + the `AgentPickerSheet`. Render the button **outside** the `FlatList`, between `HealthRow` and the grid, so it's always visible.

```tsx
const [pickerOpen, setPickerOpen] = useState(false);
// …
return (
  <View style={styles.page}>
    <HealthRow isConnected={gateway.isConnected} errorMessage={gateway.error} />
    <Pressable
      style={({ pressed }) => [styles.newTask, pressed && styles.newTaskPressed]}
      onPress={() => setPickerOpen(true)}
      accessibilityRole="button"
      accessibilityLabel="New task"
    >
      <Feather name="plus" size={16} color={colors.cream} />
      <Text style={styles.newTaskLabel}>New task</Text>
    </Pressable>
    {body}
    <AgentPickerSheet
      visible={pickerOpen}
      agents={agents}
      onClose={() => setPickerOpen(false)}
      onPick={(agent) => {
        setPickerOpen(false);
        router.push({
          pathname: "/(app)/agent/[agentId]",
          params: { agentId: agent.id, name: agentDisplayName(agent), newSession: "1" },
        });
      }}
    />
  </View>
);
```

- [ ] **Step 2:** Import `Feather from "@expo/vector-icons/Feather"` and `useState`; add styles:

```tsx
newTask: {
  flexDirection: "row", alignItems: "center", justifyContent: "center", gap: spacing.xs,
  marginHorizontal: spacing.md, marginBottom: spacing.sm,
  paddingVertical: spacing.sm, borderRadius: radii.md, backgroundColor: colors.forest,
},
newTaskPressed: { opacity: 0.85 },
newTaskLabel: { fontFamily: fonts.dmSansMedium, fontSize: 14, lineHeight: 18, color: colors.cream },
```

- [ ] **Step 3: Verify** — `typecheck` + `export:ios` clean.
- [ ] **Step 4: Commit** — `feat(mobile): New task button + agent picker on roster`

---

### Task 4: Honor the `newSession` route param in the agent chat screen

**Files:**
- Modify: `apps/mobile/src/app/(app)/agent/[agentId].tsx`

**Interfaces:**
- Consumes: existing `newSession` (mints a fresh lowercased session via `switchSession(id, null)`), `useLocalSearchParams`.

- [ ] **Step 1: Read the param** — extend the params type:

```tsx
const { agentId, name, newSession: newSessionParam } =
  useLocalSearchParams<{ agentId: string; name?: string; newSession?: string }>();
```

- [ ] **Step 2: Fire `newSession()` once on mount when the param is set**, guarded so it can't re-fire on re-render. Place after `newSession` is defined:

```tsx
const didNewSessionParamRef = useRef(false);
useEffect(() => {
  if (newSessionParam === "1" && !didNewSessionParamRef.current) {
    didNewSessionParamRef.current = true;
    newSession();
  }
}, [newSessionParam, newSession]);
```

- [ ] **Step 3: Focus the composer** (best-effort). If the composer `TextInput` has no ref, add `const inputRef = useRef<TextInput>(null);` + `ref={inputRef}` on it, then in the effect above (after `newSession()`), call `inputRef.current?.focus()`. If a ref already exists, reuse it.

- [ ] **Step 4: Write a focused unit test** for the guard logic (the effect fires `newSession` exactly once). If mounting the full screen is impractical under jest-expo (many gateway deps), extract the guard into a tiny hook `useNewSessionParam(param, fn)` and unit-test that hook instead:

```tsx
// covers: fires once when param==="1", never when undefined, not twice on rerender
```

- [ ] **Step 5: Run** — `pnpm --filter @isol8/mobile test` (new + existing green), `typecheck`, `export:ios`.
- [ ] **Step 6: Commit** — `feat(mobile): start fresh session on New task navigation`

---

## Final verification (all tasks done)

- [ ] `pnpm --filter @isol8/shared test` (unchanged, sanity) + `pnpm --filter @isol8/mobile test` — all green.
- [ ] `pnpm --filter @isol8/mobile typecheck && pnpm --filter @isol8/mobile lint && pnpm --filter @isol8/mobile export:ios` — clean.
- [ ] Manual smoke (post-merge, device/sim): 2-col grid; tile opens chat; New task → picker → new agent chat with fresh session + focused composer.
