# iPhone Redesign Part 3 — Activity, Teams, Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Activity a grouped-inset list (Needs you first, then Today / Earlier) built from the same row as Chats, with one action button per row and All · Needs you · Mentions filters, and restyle the Teams and Settings tabs to the shared visual language — light and dark — with no data or feature change.

**Architecture:** An `InsetGroup` helper gives any `SectionList` iOS grouped-inset cards around Part 1's `ListRow`. Activity keeps `useMobileActivity`, `groupActivity`, `filterActivity`, `activityRowParts` and every action path (confirm → `ReasonSheet` / `PickerSheet` → `runAction`) and only changes presentation. Teams converts its static `StyleSheet` to a palette-driven `makeStyles(palette)`; Settings' SwiftUI `Form` follows the system appearance through a palette context on `GroupedList`.

**Tech Stack:** Expo SDK 57 / React Native 0.86, `@expo/ui` SwiftUI (`Form`, `Picker` segmented), jest-expo + @testing-library/react-native.

**Spec:** `docs/superpowers/specs/2026-09-26-iphone-messaging-redesign-design.md` (binding). Visual reference only: `.superpowers/brainstorm/73529-1790476059/content/activity.html` option B ("rows"). **Depends on Parts 1 and 2** being merged (`chatPalette`/`useChatPalette`, `ListRow`, `Avatar` `ring`/`palette`, `agentRingState`, Teams/Settings living in `(tabs)/`).

## Global Constraints

- **JS/TS only.** No new dependency, no native module, no change to `apps/mobile/app.json`, `apps/mobile/package.json` or `pnpm-lock.yaml`; the OTA fingerprint must not move (Task 6).
- **Tokens:** `useChatPalette()` only. Light / dark: `ground` `#EFEFEF` / `#101012` (page behind inset groups), `surface` `#FFFFFF` / `#1E1E21` (group cards, rows), `edge` `#E2E2E2` / `#323236` (card stroke, separators), `selected` `#E4E4E6` / `#2E2E33` (pressed / highlighted row), `chip` `#E4E4E6` / `#27272B` (unselected filter), `fg` `#1A1A1C` / `#ECECEE` (text, selected filter fill, primary button fill), `onFg` `#FFFFFF` / `#101012`, `fgSoft` `#52525A` / `#B4B4B8`, `fgMute` `#606069` / `#97979C` (metadata, section titles), `link` `#1E52CC` / `#8FB0FF` (unread dot, links), `alert` `#A83C29` / `#E7806F` (destructive, errors, needs-you count), `live` `#186D48` / `#4BC08A`, `warn` `#52525A` / `#B4B4B8`, `agentTint` `#6E6E78` / `#9B9BA2`, `onAgentTint` `#FFFFFF` / `#101012`. No new colour literal outside `theme.ts`. The Teams board's state tints (`colors.status.*`, `colors.priority.*`, `colors.tool.*`) are state colours with their own labels and stay as they are.
- **Legacy-token mapping** (applied mechanically in Tasks 3–4): `cream`/`white` → `surface` for cards and rows, `ground` for page backgrounds; `ink`/`assistantInk` → `fg`; `inkSoft`/`mutedSecondary` → `fgSoft`; `mute`/`muted`/`muteSoft`/`placeholder`/`sectionCount` → `fgMute`; `edge`/`border`/`edgeSoft`/`edgeForest` → `edge`; `creamDeeper`/`creamDeepest`/`chip`/`codeBg`/`skeleton`/`forestTint`/`avatarBg`/`bannerBg` → `raised`; `rowPressed`/`rowActive`/`creamPressed` → `selected`; `forest` as a fill (buttons) → `fg` with `onFg` content; `forest`/`info` as text or icon tint → `link`; `danger`/`error`/`errorText`/`errorBorder` → `alert`; `runningGreen`/`green`/`forestSoft`/`greenLight` → `live`; `warn`/`bannerText`/`bannerBorder`/`connectingYellow` → `warn`; `gold` → `alert` (it only ever marked a count); `errorBg`/`mentionBg`/`mentionRowBg`/`forestHalo`/`forestFocusBorder` → keep (they are ≤10% state washes over any ground).
- **Fonts:** Source Sans 3 (`SourceSans3_400Regular` / `_500Medium` / `_600SemiBold`) for content, `Lora_500Medium` for display titles, system font in nav chrome and inside SwiftUI lists (the `GroupedList` exemption). Metadata ≥ 12pt.
- **Avatars:** rounded squares only (Part 1 rules); `AssigneeAvatar` loses its circles.
- **No monospace except code.** Teams' mono uses are issue identifiers and run ids in `teams.tsx` — they are labels, not code: switch them to `fonts.sansMedium`. Code/output wells elsewhere keep mono.
- **No feature or data change:** every label, callback, API call and copy string on Activity, Teams and Settings stays the same except where a task names the change.
- **Accessibility:** rows keep their VoiceOver labels; the one row button is its own element (sibling of the row); 44pt targets; section titles are `accessibilityRole="header"`.
- **Test commands (targeted only):** `cd apps/mobile && pnpm jest <paths>` (escape parentheses), `pnpm typecheck`, `pnpm lint`.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. **A needs-you item whose only actions are navigations** (e.g. just "Open issue") — the row's button reads "Open" and navigates; it never opens an empty Review sheet. Pinned in Task 2.
2. **A resolved item that still carries actions from a stale page** — no button at all, "Resolved by …" footer shown. Pinned in Task 2 (row renders no button when `state !== "open"`).
3. **The payload that used to show inline on needs-you rows** (destination, preview text, fields such as "Recommended action") — it must stay reachable: it becomes the Review sheet's message, verbatim. Pinned in Task 2.
4. **A push-focused item under a filter that hides it** — focusing still switches to All and highlights the row (existing behaviour, new highlight colour). Pinned by the kept "opens on a pushed approval…" test (Task 2).
5. **Dark mode on the Settings SwiftUI list** — every row title, subtitle, icon and the danger buttons stay legible (palette context, not the light `colors`). Pinned in Task 5 (`GroupedList` context test).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/mobile/src/components/ui/InsetGroup.tsx` (create) + test | `insetRowStyle`, `InsetSectionHeader` for grouped-inset `SectionList`s |
| `apps/mobile/src/components/ui/ListRow.tsx` (modify) | optional `detail` third line |
| `apps/mobile/src/app/(app)/(tabs)/activity.tsx` (modify) + `__tests__/activity.test.tsx` | Activity as grouped rows, one button, three filters |
| `apps/mobile/src/app/(app)/(tabs)/teams.tsx` (modify) | palette styles, inbox as inset rows |
| `apps/mobile/src/components/teams/{ApprovalCard,IssueCard,AssigneeAvatar,BoardView,InteractionCard,PickerSheet,ReasonSheet}.tsx` (modify) | palette styles, square avatars |
| `apps/mobile/src/components/ui/SegmentedControl.tsx` (modify) | optional `colorScheme` |
| `apps/mobile/src/components/ui/GroupedList.tsx` (modify) + test | `colorScheme` prop + palette context for `NavigationRow` |
| `apps/mobile/src/app/(app)/(tabs)/settings.tsx` (modify) + test | follows the system appearance |

---

### Task 1: InsetGroup helpers and ListRow `detail`

**Files:**
- Create: `apps/mobile/src/components/ui/InsetGroup.tsx`
- Modify: `apps/mobile/src/components/ui/ListRow.tsx`
- Test: `apps/mobile/src/components/ui/__tests__/InsetGroup.test.tsx`, extend `ListRow.test.tsx`

**Interfaces:**
- Consumes: `ChatPalette`, `ListRow` (Part 1).
- Produces:
  ```ts
  /** The card slice one row of a grouped-inset section draws: side strokes always, top corners on the first row, bottom corners on the last. */
  export function insetRowStyle(palette: ChatPalette, index: number, count: number): ViewStyle;
  /** 13pt semibold fgMute section title on the ground, 32pt left inset, header role, optional trailing count. */
  export function InsetSectionHeader(props: { title: string; count?: number; palette: ChatPalette }): JSX.Element;
  // ListRow gains:
  detail?: string | null;   // third line, 1 line, fonts.sans 13/18 fgMute (Activity's snippet)
  ```
- `insetRowStyle` returns `{ marginHorizontal: 16, backgroundColor: p.surface, borderColor: p.edge, borderLeftWidth: 1, borderRightWidth: 1, overflow: "hidden", ...(first ? { borderTopWidth: 1, borderTopLeftRadius: 12, borderTopRightRadius: 12 } : {}), ...(last ? { borderBottomWidth: 1, borderBottomLeftRadius: 12, borderBottomRightRadius: 12, marginBottom: 8 } : {}) }` with `first = index === 0`, `last = index === count - 1`.

- [ ] **Step 1: Write the failing tests**

```tsx
// apps/mobile/src/components/ui/__tests__/InsetGroup.test.tsx
import { render, screen } from "@testing-library/react-native";

import { chatPalette } from "../../../config/theme";
import { InsetSectionHeader, insetRowStyle } from "../InsetGroup";

const p = chatPalette.dark;

describe("insetRowStyle", () => {
  it("rounds the top of the first row and the bottom of the last", () => {
    expect(insetRowStyle(p, 0, 3)).toMatchObject({ borderTopWidth: 1, borderTopLeftRadius: 12, backgroundColor: p.surface, borderColor: p.edge });
    expect(insetRowStyle(p, 2, 3)).toMatchObject({ borderBottomWidth: 1, borderBottomRightRadius: 12, marginBottom: 8 });
    expect(insetRowStyle(p, 1, 3)).not.toHaveProperty("borderTopWidth");
    expect(insetRowStyle(p, 0, 1)).toMatchObject({ borderTopWidth: 1, borderBottomWidth: 1 });
  });
});

it("titles a section as a header with its count", async () => {
  await render(<InsetSectionHeader title="Needs you" count={2} palette={p} />);
  expect(screen.getByRole("header", { name: /Needs you/ })).toBeTruthy();
  expect(screen.getByText("2")).toBeTruthy();
});
```

Append to `ListRow.test.tsx`:

```tsx
it("adds a one-line detail under the preview", async () => {
  await render(<ListRow palette={p} leading={null} title="Growth" preview="needs your approval" detail="Pilot X auto-replies" accessibilityLabel="x" />);
  expect(screen.getByText("Pilot X auto-replies").props.numberOfLines).toBe(1);
});
```

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest src/components/ui/__tests__/InsetGroup.test.tsx src/components/ui/__tests__/ListRow.test.tsx` → FAIL.

- [ ] **Step 3: Implement** both to the Interfaces block (`InsetSectionHeader`: a `Text` `accessibilityRole="header"` with the title, then the count as a nested `Text` in `palette.fgMute`; `paddingHorizontal: 32`, `paddingTop: 16`, `paddingBottom: 6`, `backgroundColor: palette.ground`).

- [ ] **Step 4: Run tests** — same command → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/ui/InsetGroup.tsx apps/mobile/src/components/ui/ListRow.tsx apps/mobile/src/components/ui/__tests__/InsetGroup.test.tsx apps/mobile/src/components/ui/__tests__/ListRow.test.tsx
git commit -m "feat(mobile): grouped-inset helpers and a detail line on ListRow

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Activity as grouped rows with one action button

**Files:**
- Modify: `apps/mobile/src/app/(app)/(tabs)/activity.tsx`
- Test: `apps/mobile/src/app/(app)/(tabs)/__tests__/activity.test.tsx`

**Interfaces:**
- Consumes: `ListRow` (+`detail`), `insetRowStyle`, `InsetSectionHeader` (Task 1); `Avatar` (`ring`, `palette`); `agentRingState` (shared); `activityRowParts`, `groupActivity`, `filterActivity`, `isNeedsYou`, `ACTIVITY_EMPTY_COPY` (shared, unchanged); `useWorkspaceRoster` (Part 1; replaces this screen's private `useRoster` roster fetch for `person` inputs and supplies agent status for rings).
- Produces (exported for tests):
  ```ts
  /** Mobile shows three of the shared filters. */
  export const MOBILE_ACTIVITY_FILTERS: readonly { id: ActivityFilter; label: string }[]; // all, needs-you, mentions — labels from ACTIVITY_FILTERS
  /** The one button a row carries: Review (actions to take), Open (only somewhere to go), or none. */
  export function rowButton(item: ActivityItem): { kind: "review"; actions: ActivityAction[] } | { kind: "open"; target: NonNullable<ActivityAction["navigate"]> } | null;
  /** The Review sheet's message: the item's why and payload, verbatim, one per line. */
  export function reviewMessage(item: ActivityItem): string | undefined;
  ```
- `rowButton`: `item.state !== "open"` → `null`; `review = item.actions.filter((a) => !a.navigate)`; `review.length > 0` → `{ kind: "review", actions: review }`; else `itemTarget(item)` → `{ kind: "open", target }`; else `null`.
- `reviewMessage`: `[summary.reason, summary.preview?.text, summary.preview?.destination ? `To ${summary.preview.destination}` : null, ...(summary.preview?.fields ?? []).map((f) => `${f.label}: ${f.value}`)].filter(Boolean).join("\n") || undefined`.
- Row rendering (per item, inside `View style={[insetRowStyle(palette, index, section.data.length), highlighted && { backgroundColor: palette.selected }]}`):
  - `ListRow` with `leading = <Avatar name={item.actor.name} kind={item.actor.kind} size={36} palette ring={item.actor.kind === "agent" ? agentRingState(agentsById.get(item.actor.id)?.status) : null} />`; `title = parts.lead ?? item.actor.name`; `preview = parts.lead ? `${parts.rest.trimStart()}${parts.location ? ` · ${parts.location}` : ""}` : `${item.summary.title}${parts.location ? ` · ${parts.location}` : ""}``, `previewLines={2}`; `detail = item.state === "open" ? parts.snippet : resolvedLine(item)`; `time = relativeTime(Date.parse(item.createdAt))`; `unread = !item.read`; `accessibilityLabel = item.summary.title`; `onPress = go(itemTarget(item))` (disabled when no target and no button); `onLongPress` = open the Review sheet when `rowButton` is review (VoiceOver action "More actions" kept).
  - `trailing` = the one button: `review` → "Review" (pill, `fg` fill, `onFg` 13pt `fonts.sansSemiBold`, 32pt tall, `hitSlop={6}`, `accessibilityLabel={`Review: ${item.summary.title}`}`); `open` → "Open" (pill, `chip` fill, `fg` text, label `Open: ${item.summary.title}`); a spinner replaces the pill while `busy`.
  - Review sheet = `Alert.alert(item.summary.title, reviewMessage(item), [...review.map((a) => ({ text: a.label, style: a.style === "destructive" ? "destructive" : "default", onPress: () => onAction(item, a) })), ...(target ? [{ text: "Open", onPress: () => go(target) }] : []), { text: "Cancel", style: "cancel" }])` — `onAction` is today's function unchanged (confirm → sheet → `run`), so every downstream sheet and request body is identical.
  - Row error (`errors.get(item.id)`) stays as an `alert`-coloured line under the row inside the card.
- Screen: `backgroundColor: palette.ground`; `SectionList` with `stickySectionHeadersEnabled={false}`, `renderSectionHeader` → `InsetSectionHeader` (`count` only on "Needs you"); filters = `MOBILE_ACTIVITY_FILTERS` chips (selected: `fg` fill / `onFg` text; unselected: `chip` fill / `fg` text; the Needs-you count pill `alert` fill / `onFg` text); skeleton, error card, empty state, Paperclip footnote, load-more, `markSeen`, focus/highlight, `ReasonSheet`, `PickerSheet` all kept (colours mapped). `useRoster` is deleted; the `person` picker options come from `useWorkspaceRoster(pending?.action.input.kind === "person" || focused)` as `[...agents → { key: a.id, label: a.name || "Agent", section: "Agents" }, ...members → { key: m.userId, label: m.name || m.email || "Workspace member", section: "People" }]`.

- [ ] **Step 1: Update the tests** in `activity.test.tsx`:
  - Add `jest.mock("../../../../lib/workspaceRoster", () => ({ useWorkspaceRoster: () => ({ agents: [{ id: "agent-9", name: "Growth", status: "running" }], members: [{ userId: "user_2", name: "Dana" }] }) }));` (keep the `teamsData` mock for anything else that imports it).
  - Replace "pins a needs-you item with its why, payload, time and unread dot":

```tsx
it("pins a needs-you item under its header with the why, time, unread dot and a Review button", async () => {
  mockState = { items: [item()] };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  expect(screen.getByRole("header", { name: /Needs you/ })).toBeTruthy();
  expect(screen.getByText("Growth")).toBeTruthy();
  expect(screen.getByText("wants to publish a post on LinkedIn")).toBeTruthy();
  expect(screen.getByText("Launch announcement")).toBeTruthy();
  expect(screen.getByText("2m")).toBeTruthy();
  expect(screen.getByLabelText("Unread")).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Review: Growth wants to publish a post on LinkedIn"));
  expect(alert).toHaveBeenCalledWith(
    "Growth wants to publish a post on LinkedIn",
    "Launch announcement\nWe just shipped v2\nTo LinkedIn · Lightbulb page",
    expect.any(Array),
  );
});
```

  - In "filters with chips and groups the rest by day" delete the two `Threads` lines and add `expect(screen.queryByRole("button", { name: "Threads" })).toBeNull(); expect(screen.queryByRole("button", { name: "Agents’ work" })).toBeNull();`.
  - Replace "runs a plain action with no extra fields":

```tsx
it("runs a plain action from the Review sheet", async () => {
  mockState = { items: [item()] };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Review: Growth wants to publish a post on LinkedIn"));
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Approve")!.onPress!(); });
  expect(mockRunAction).toHaveBeenCalledWith("paperclip:approval:1", "approve", {});
});
```

  - In "confirms, then asks for the note, then runs with it": replace the first `fireEvent.press(... "Reject" ...)` with pressing Review and choosing Reject from `alert.mock.calls[0][2]`; the confirm is then `alert.mock.calls[1]` (`expect(alert).toHaveBeenNthCalledWith(2, "Reject this request?", undefined, expect.any(Array))`), pressing its "Reject" opens the sheet; the rest of the test is unchanged (the sheet's submit is the only "Reject" button left on screen, so `getAllByRole("button", { name: "Reject" }).at(-1)` still resolves).
  - Apply the same Review-first change to "picks a select option…", "picks a person…", "shows the server's message when an action fails", "shows local neutral copy…" and "pins an agent's question and confirms it inline" wherever they pressed an inline action button: press `Review: <title>` then the action from `alert.mock.calls[0][2]`.
  - Append:

```tsx
it("offers Open, not Review, when the only actions navigate", async () => {
  mockState = { items: [item({ actions: [action({ id: "open", label: "Open issue", navigate: { kind: "issue", id: "iss-1", roomId: null } })] })] };
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Open: Growth wants to publish a post on LinkedIn"));
  expect(mockPush).toHaveBeenCalledWith({ pathname: "/(app)/teams/issue/[id]", params: { id: "iss-1" } });
  expect(screen.queryByText("Review")).toBeNull();
});

it("rings an agent actor that is running", async () => {
  mockState = { items: [item({ actor: { kind: "agent", id: "agent-9", name: "Growth" } })] };
  await render(<Activity />);
  expect(screen.getByLabelText("Working")).toBeTruthy();
});
```

  - "shows who resolved an item and no buttons" stays as is (resolved → `detail` shows "Resolved by Dana · 5m", `rowButton` is null).

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest 'src/app/\(app\)/\(tabs\)/__tests__/activity.test.tsx'` → FAIL.

- [ ] **Step 3: Implement** to the Interfaces block.

- [ ] **Step 4: Run tests** — same command → PASS (including "opens on a pushed approval…", "navigates: …", "marks seen…", "shows the empty state…", "reports a load failure…", "activity links"); `pnpm typecheck`.

- [ ] **Step 5: Commit**

```bash
git add 'apps/mobile/src/app/(app)/(tabs)/activity.tsx' 'apps/mobile/src/app/(app)/(tabs)/__tests__/activity.test.tsx'
git commit -m "feat(mobile): Activity as grouped rows with one Review/Open button

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Teams tab restyle

**Files:**
- Modify: `apps/mobile/src/app/(app)/(tabs)/teams.tsx` (`styles` at the bottom → `makeStyles`; `InboxRow`)
- Modify: `apps/mobile/src/components/ui/SegmentedControl.tsx`
- Test: `apps/mobile/src/app/(app)/(tabs)/__tests__/teams.test.tsx` (create)

**Interfaces:**
- Consumes: `useChatPalette`, `ChatPalette`, `ListRow`, `insetRowStyle`, `InsetSectionHeader`.
- Produces: `SegmentedControl` gains `colorScheme?: "light" | "dark"` (default `"light"`, passed to its `Host`); `teams.tsx` exports nothing new.
- Behaviour:
  - `const palette = useChatPalette(); const styles = useMemo(() => makeStyles(palette), [palette]);` in `TeamsScreen` and in every sub-component that reads `styles` (`InboxRow`, `CenteredState`, `OutlineButton`, `DashTile`, `DashboardSkeleton`, `RunActivityCard`, `DashboardView`, `ApprovalsView`, `InboxTabChips`, `FailedRunRow`, `InboxView`, `PulsingDot`, `SkeletonRows`) — each calls `useChatPalette()` itself (it is a cheap context-free hook) rather than threading props.
  - `makeStyles(p: ChatPalette)` = today's style object with every `colors.*` replaced per the Global Constraints mapping; page/host backgrounds → `p.ground`; tiles, cards, rows → `p.surface` with 1px `p.edge` and radius 12; identifier/run-id labels switch from `fonts.mono` to `fonts.sansMedium`.
  - `InboxRow` becomes `ListRow` (`leading={null}`, `title={item.title}`, `time={when}`, `preview={[identifier, preview].filter(Boolean).join(" · ")}`, `previewLines={1}`, `unread={!!item.unread}`, same `onPress` push and `accessibilityLabel={`${item.title}. ${meta}`}`) wrapped in `insetRowStyle(p, index, count)`; the inbox list's section titles use `InsetSectionHeader`.
  - `SegmentedControl` in `TeamsScreen` gets `colorScheme={useColorScheme() === "dark" ? "dark" : "light"}`; the header "+" (via `useTabHeader`) tints `palette.fg`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/app/(app)/(tabs)/__tests__/teams.test.tsx
import { fireEvent, render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import { chatPalette } from "../../../../config/theme";

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));
jest.mock("../../../../lib/tabHeader", () => ({ useTabHeader: () => {} }));
const mockNoop = async () => {};
const mockDashboard = { state: { kind: "loading" }, refreshing: false, retry: mockNoop, refresh: mockNoop };
const mockInbox = {
  state: {
    kind: "ready",
    items: [{ id: "i1", title: "Ship v2", identifier: "LB-12", status: "in_progress", unread: true, updatedAt: new Date().toISOString() }],
    failedRuns: [], agents: [],
  },
  refreshing: false, retry: mockNoop, refresh: mockNoop,
  dismissedRunIds: new Set<string>(), dismissFailedRun: jest.fn(), retryFailedRun: jest.fn(),
};
jest.mock("../../../../lib/teamsData", () => ({
  ...jest.requireActual("../../../../lib/teamsData"),
  useTeamsDashboard: () => mockDashboard,
  useTeamsInbox: () => mockInbox,
  useTeamsApprovals: () => ({ state: { kind: "loading" }, refreshing: false, retry: mockNoop, refresh: mockNoop }),
  useCreateTeamIssue: () => jest.fn(),
}));

import Teams from "../teams";

it("paints the page on the ground", async () => {
  await render(<Teams />);
  expect(StyleSheet.flatten(screen.getByTestId("teams-host").props.style).backgroundColor).toBe(chatPalette.light.ground);
});

it("shows inbox items as inset rows with their unread dot and label", async () => {
  await render(<Teams />);
  await fireEvent.press(screen.getByRole("button", { name: "Inbox" }));
  expect(screen.getByLabelText(/^Ship v2\. LB-12/)).toBeTruthy();
  expect(screen.getByLabelText("Unread")).toBeTruthy();
});
```

Add `testID="teams-host"` to `TeamsScreen`'s root `View`. The `Picker` mock in `jest-setup.ts` draws one pressable per segment containing the segment's `Text`, so `getByRole("button", { name: "Inbox" })` resolves; if the approvals hook's real return shape carries more fields that `ApprovalsView` reads on first render, add them to the mock with inert values (the Overview/Inbox tests never mount `ApprovalsView`).

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest 'src/app/\(app\)/\(tabs\)/__tests__/teams.test.tsx'` → FAIL.

- [ ] **Step 3: Implement** the Behaviour list.

- [ ] **Step 4: Run tests** — same command plus `pnpm jest src/components/teams` (BoardView/InteractionCard/IssueRunsSection unchanged) → PASS; `pnpm typecheck`.

- [ ] **Step 5: Commit**

```bash
git add 'apps/mobile/src/app/(app)/(tabs)/teams.tsx' 'apps/mobile/src/app/(app)/(tabs)/__tests__/teams.test.tsx' apps/mobile/src/components/ui/SegmentedControl.tsx
git commit -m "feat(mobile): Teams tab in the shared visual language

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Teams components — cards, sheets, square avatars

**Files:**
- Modify: `apps/mobile/src/components/teams/ApprovalCard.tsx`, `IssueCard.tsx`, `AssigneeAvatar.tsx`, `BoardView.tsx`, `InteractionCard.tsx`, `PickerSheet.tsx`, `ReasonSheet.tsx`
- Test: `apps/mobile/src/components/teams/__tests__/AssigneeAvatar.test.tsx` (create); existing `BoardView.test.tsx`, `InteractionCard.test.tsx` must stay green

**Interfaces:**
- Consumes: `useChatPalette`, `chatPalette`.
- Produces: no prop changes. `AssigneeAvatar` draws a rounded square: radius `size > 32 ? 12 : size >= 24 ? 8 : 4`; agent fill `agentTint` / glyph `onAgentTint`; human fill `selected` / glyph `fg`; the photo keeps the same radius.
- Behaviour: each component converts its module `styles` to `makeStyles(p)` + `useMemo` (same mapping as Task 3). `PickerSheet` and `ReasonSheet` render inside `BottomSheet`, which stays pinned light — they use `chatPalette.light` explicitly (not the hook) so a dark phone never draws dark text on the light sheet. Status/priority chips keep `colors.status.*` / `colors.priority.*`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/components/teams/__tests__/AssigneeAvatar.test.tsx
import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import { chatPalette } from "../../../config/theme";
import { AssigneeAvatar } from "../AssigneeAvatar";

it("draws an agent as a tinted rounded square, never a circle", async () => {
  await render(<AssigneeAvatar assignee={{ name: "Growth", kind: "agent" }} size={28} />);
  const glyph = screen.getByText("G");
  const tile = glyph.parent!.parent!; // Text host → Text → tile View
  const style = StyleSheet.flatten(tile.props.style);
  expect(style.borderRadius).toBe(8);
  expect(style.backgroundColor).toBe(chatPalette.light.agentTint);
  expect(StyleSheet.flatten(glyph.props.style).color).toBe(chatPalette.light.onAgentTint);
});

it("keeps small footer avatars square too", async () => {
  await render(<AssigneeAvatar assignee={{ name: "Dana Lee", kind: "user" }} />);
  const tile = screen.getByText("DL").parent!.parent!;
  expect(StyleSheet.flatten(tile.props.style).borderRadius).toBe(4);
});
```

(If the tree depth differs, give the tile `testID="assignee-avatar"` and query it instead — then the test reads `screen.getByTestId("assignee-avatar")`.)

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest src/components/teams/__tests__/AssigneeAvatar.test.tsx` → FAIL (radius is `size / 2`).

- [ ] **Step 3: Implement** the Behaviour block across the seven files.

- [ ] **Step 4: Run tests** — `cd apps/mobile && pnpm jest src/components/teams 'src/app/\(app\)/teams' 'src/app/\(app\)/\(tabs\)/__tests__/teams.test.tsx' 'src/app/\(app\)/\(tabs\)/__tests__/activity.test.tsx' && pnpm typecheck` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/teams
git commit -m "feat(mobile): Teams cards and sheets on the palette, square avatars

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Settings follows the system appearance

**Files:**
- Modify: `apps/mobile/src/components/ui/GroupedList.tsx`
- Modify: `apps/mobile/src/app/(app)/(tabs)/settings.tsx` (the modifier constants at lines ~281–291 and `return (<GroupedList>`)
- Test: `apps/mobile/src/components/ui/__tests__/GroupedList.test.tsx` (create); `(tabs)/__tests__/settings.test.tsx` stays green

**Interfaces:**
- Consumes: `chatPalette`, `ChatPalette`, `useChatPalette`.
- Produces:
  ```ts
  export function GroupedList(props: { children: ReactNode; colorScheme?: "light" | "dark" }): JSX.Element; // default "light" — every other caller unchanged
  export function useGroupedPalette(): ChatPalette;   // chatPalette[colorScheme] of the nearest GroupedList (light outside one)
  export function NavigationRow(props unchanged): JSX.Element;   // icon/title/subtitle/chevron colours from useGroupedPalette()
  ```
- Behaviour: `GroupedList` renders `Host colorScheme={colorScheme} seedColor={p.fg}` and provides `chatPalette[colorScheme]` through a React context; `NavigationRow` builds its `ICON` (`p.link`), `TITLE` (`p.fg`), `SUBTITLE` (`p.fgMute`), `CHEVRON` (`p.fgMute`) modifiers from the context. Settings: `const scheme = useColorScheme() === "dark" ? "dark" : "light"; const p = chatPalette[scheme];` passes `colorScheme={scheme}` and replaces the module constants with `const m = useMemo(() => ({ BODY: [foregroundStyle(p.fg)], SECONDARY: [foregroundStyle(p.fgMute)], CAPTION: [font({ textStyle: "footnote" }), foregroundStyle(p.fgMute)], DANGER: [foregroundStyle(p.alert)], ERROR: [foregroundStyle(p.alert)], PLACEHOLDER, AVATAR: [font({ textStyle: "subheadline", weight: "semibold" }), foregroundStyle(p.fg), frame({ width: 40, height: 40 }), background(p.selected, shapes.roundedRectangle({ cornerRadius: 12 }))] }), [p])` — the profile initials tile becomes a rounded square too (if `shapes.roundedRectangle` is not exported by the installed `@expo/ui`, keep the existing `shapes.circle` call's module and use its rounded-rectangle sibling as named in `node_modules/.pnpm/@expo+ui@*/node_modules/@expo/ui/build/swift-ui/modifiers/index.d.ts`; add that name to the `jest-setup.ts` `shapes` mock). Every `m.X` use replaces the old constant. `control/index.tsx`, `RoomDetails`, `WorkspaceGate` keep calling `GroupedList` without `colorScheme` (light, unchanged).

- [ ] **Step 1: Write the failing test**

```tsx
// apps/mobile/src/components/ui/__tests__/GroupedList.test.tsx
import { render, screen } from "@testing-library/react-native";
import { Text } from "react-native";

import { chatPalette } from "../../../config/theme";
import { GroupedList, useGroupedPalette } from "../GroupedList";

function Probe() {
  return <Text>{useGroupedPalette().fg}</Text>;
}

it("hands the dark palette to rows inside a dark list", async () => {
  await render(<GroupedList colorScheme="dark"><Probe /></GroupedList>);
  expect(screen.getByText(chatPalette.dark.fg)).toBeTruthy();
});

it("stays light by default and outside any list", async () => {
  await render(<><GroupedList><Probe /></GroupedList><Probe /></>);
  expect(screen.getAllByText(chatPalette.light.fg)).toHaveLength(2);
});
```

- [ ] **Step 2: Run to verify failure** — `cd apps/mobile && pnpm jest src/components/ui/__tests__/GroupedList.test.tsx` → FAIL.

- [ ] **Step 3: Implement** the Behaviour block.

- [ ] **Step 4: Run tests** — `cd apps/mobile && pnpm jest src/components/ui 'src/app/\(app\)/\(tabs\)/__tests__/settings.test.tsx' 'src/app/\(app\)/control' src/components/room/__tests__/RoomDetails.test.tsx src/components/workspace && pnpm typecheck` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/ui/GroupedList.tsx apps/mobile/src/components/ui/__tests__/GroupedList.test.tsx 'apps/mobile/src/app/(app)/(tabs)/settings.tsx' apps/mobile/jest-setup.ts
git commit -m "feat(mobile): Settings follows light and dark through the list palette

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Verification, fingerprint and simulator acceptance

- [ ] **Step 1: Targeted suites, typecheck, lint**

```bash
cd apps/mobile && pnpm jest src/components/ui src/components/teams 'src/app/\(app\)/\(tabs\)' 'src/app/\(app\)/teams' 'src/app/\(app\)/control' src/components/room/__tests__/RoomDetails.test.tsx src/components/workspace && pnpm typecheck && pnpm lint
```
Expected: PASS, zero lint errors.

- [ ] **Step 2: Fingerprint unchanged** — `git diff --stat origin/main -- apps/mobile/app.json apps/mobile/package.json pnpm-lock.yaml` prints nothing; compare `npx @expo/fingerprint .` hashes of this branch and a throwaway `git worktree add /tmp/fp-base origin/main` exactly as Part 1 Task 12 Step 2 (never `git stash`). Expected: equal.

- [ ] **Step 3: Simulator acceptance (manual, on the PR)** — `pnpm start:dev` + `pnpm ios`, a dev workspace with a pending approval, an agent question (interaction), a mention and a resolved item. In light and dark (`xcrun simctl ui booted appearance light|dark`, `xcrun simctl io booted screenshot <name>.png`) capture: (1) Activity "All" with the Needs-you group first (count pill), then Today / Earlier, unread dots, a running agent's ring, a Review and an Open button, a resolved row's footer; (2) the Review sheet of an approval showing its payload lines; (3) the "Needs you" and "Mentions" filters; (4) Teams Overview, Inbox (inset rows) and Approvals; (5) an issue card on the Board; (6) Settings top to bottom (profile tile, Chats switch, Workspace rows, Plan, Legal, Help, Sign out). Check: grouped inset cards with 12pt corners on the `ground`, no circle avatars, no monospace outside code, every text legible in dark. Attach all screenshots to the PR.

- [ ] **Step 4:** open the PR `feat(mobile): iPhone redesign part 3 — Activity, Teams, Settings`, body ending `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. After it merges and all three parts have shipped, delete `docs/superpowers/specs/2026-09-26-iphone-messaging-redesign-design.md` and the three plan files in a follow-up (CLAUDE.md: planning docs are disposable).

---

## Self-Review

- **Spec coverage (Part 3):** Activity as grouped inset rows with the Chats row component → Tasks 1, 2; sections Needs you (approvals, sessions waiting, login requests — `isNeedsYou` already covers all three) then Today / Earlier → Task 2 (`groupActivity` unchanged); one action button per row ("Review", "Open") opening the existing sheets → Task 2 (`Alert` Review sheet → unchanged `onAction` → `ReasonSheet` / `PickerSheet` / confirm); filters All · Needs you · Mentions → Task 2; data and actions unchanged (`useActivity`) → Task 2; Teams / Settings restyle, grouped inset lists, avatars, type, no feature/data change → Tasks 3, 4, 5; light + dark screenshots → Task 6.
- **Resolutions:** the inline needs-you payload moves into the Review sheet (one button per row leaves no room for inline approve/deny); Threads and Agents' work filters are dropped on the phone only (web keeps all five via the shared `ACTIVITY_FILTERS`); Teams detail screens (`teams/issue/[id]`, `teams/run/[runId]`, `teams/routines`) and Control stay on the light legacy palette — only what the Teams tab root and its shared cards render is restyled (the spec scopes the restyle to the Teams and Settings tabs); sheets stay pinned light because `BottomSheet` pins its SwiftUI host light.
- **Type consistency:** `insetRowStyle`/`InsetSectionHeader`/`ListRow.detail` (Task 1) used in Tasks 2, 3; `rowButton`/`reviewMessage`/`MOBILE_ACTIVITY_FILTERS` exported from `activity.tsx` for its test; `GroupedList` `colorScheme` + `useGroupedPalette` (Task 5) used by `NavigationRow` and Settings.
