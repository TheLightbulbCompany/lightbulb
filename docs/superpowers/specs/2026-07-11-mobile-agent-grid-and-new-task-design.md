# Mobile: Agent Grid Roster + "New Task" Entry Point — Design

**Date:** 2026-07-11
**Surface:** `apps/mobile` (Expo / React Native, Chat tab)
**Depends on:** existing `agents.list` + sessions RPCs (no backend change)

## Goal

Two client-only UI changes to the mobile Chat tab:
1. Turn the agent **roster** from a vertical list into a **2-column compact grid** of agent tiles.
2. Add a discoverable **"＋ New task"** entry point on the roster that starts a **fresh session** with a chosen agent — the mobile equivalent of the website's "new session" affordance (today it's buried behind a "layers" icon → bottom sheet inside an agent chat).

## Current state (what we're changing)

- `src/app/(app)/chat.tsx` — the Chat tab. Renders a `FlatList` of agent **rows** (28px monogram + name + `agent_id` + status dot). Tap → `/(app)/agent/[agentId]`.
- `src/app/(app)/agent/[agentId].tsx` — per-agent chat. Owns session state; `sheetOpen` opens `SessionsSheet` via a top-right **"layers"** header icon; `newSession()` (line ~312) mints a fresh session UUID and activates it.
- `src/components/chat/SessionsSheet.tsx` — bottom-sheet session switcher with a "New session" action. Unchanged by this work.

## Design

### Feature A — Agent roster as a 2-column grid

`chat.tsx` renders the roster with `FlatList numColumns={2}` (compact-tile variant chosen):

**Tile anatomy** (uses existing theme tokens in `config/theme` — cream ground, `forestTint`/`forest`, DM Sans + Lora, `radii`/`spacing`):
- Cream card, hairline border (`colors.edge`), `radii.md`, soft pressed state (`colors.rowPressed`), min height ~120px, centered content.
- **Avatar:** ~44px monogram tile (`forestTint` bg, `radii.md`). Agent emoji (`identity.emoji`) if set, else Lora italic-bold uppercase initial (`fonts.loraBoldItalic`, `colors.forest`).
- **Name:** `agentDisplayName(item)`, DM Sans medium (`fonts.dmSansMedium`), centered, `numberOfLines={2}`.
- **Status row:** small forest dot + `Active` label (DM Sans, `colors.mute`). (Static "active" — matches the current list's `StatusDot`; no live per-agent state exists yet.)
- Tap → `router.push({ pathname: "/(app)/agent/[agentId]", params: { agentId, name } })` — **unchanged** navigation (open/resume General session).

**Layout details:**
- Two even columns with a gutter: `columnWrapperStyle` gap `spacing.sm`; each tile `flex: 1`; row gap via `contentContainerStyle`.
- Keep the existing **`HealthRow`**, the **`AGENTS · N`** section label, the **error card**, and the **empty state**.
- **Skeleton:** grid-shaped placeholder tiles (reuse `usePulse`) instead of the three list rows.

### Feature B — "＋ New task" entry point

**Placement:** a full-width, forest-accented **"＋ New task"** button pinned at the **top of the roster**, directly below `HealthRow` and above the `AGENTS · N` grid (always visible; not scrolled with the grid content — render it outside the `FlatList` or as a fixed header region).

**Interaction:**
1. Tap **New task** → opens `AgentPickerSheet` (new component), a slide-up modal reusing the same `agents.list` data, listing agents (compact rows: monogram + name).
2. Pick an agent → dismiss sheet → `router.push({ pathname: "/(app)/agent/[agentId]", params: { agentId, name, newSession: "1" } })`.
3. The agent chat screen honors a **`newSession` route param on mount**: if `newSession === "1"`, it calls the existing `newSession()` once (mint + activate a fresh session) instead of opening General, and focuses the composer.

**Distinction preserved:** tapping an agent **tile** = open/resume (General); **New task** = start fresh.

## New / changed files

- **Modify** `src/app/(app)/chat.tsx` — grid rendering + "New task" button + `AgentPickerSheet` wiring; adapt skeleton/empty/section styles to the grid.
- **Create** `src/components/chat/AgentPickerSheet.tsx` — slide-up agent picker (Modal, cream card, drag handle, `agents.list` rows). Props: `visible`, `onClose`, `agents`, `onPick(agent)`.
- **Modify** `src/app/(app)/agent/[agentId].tsx` — read `newSession` param via `useLocalSearchParams`; on mount, if `"1"`, invoke `newSession()` once (guard against re-fire on re-render) + focus composer.

## Testing

- **jest-expo** unit test for `AgentPickerSheet`: renders agent rows from props, `onPick` fires with the selected agent, `onClose` on scrim press. (RNTL 14 `render()` is async.)
- **jest-expo** test for the chat screen's `newSession` param handling if feasible (mount with `newSession:"1"` → `newSession()` called once); otherwise assert the param-guard logic in isolation.
- `pnpm --filter @isol8/mobile typecheck` + `lint` + `export:ios` (Metro bundle) all clean.
- Manual (post-merge, on device/simulator): grid renders 2-col; tiles open chats; New task → picker → fresh session with focused composer.

## Out of scope / non-goals

- No backend or `packages/shared` changes.
- No live per-agent running/busy indicator (status stays static "Active"; a real signal is a separate effort).
- `SessionsSheet` and the in-chat "layers" sessions switcher are unchanged (New task is additive, not a replacement).
- No Android-specific picker work beyond what RN `Modal` gives cross-platform (the sheet is plain RN, not `ActionSheetIOS`).
