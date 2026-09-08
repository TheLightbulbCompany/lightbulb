# Inbox — one page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn today's Inbox (five pill tabs over issue rows) and the separate Activity tab into ONE Inbox page with three sections — Needs you · Happening now · Recently — that holds no task rows at all.

**Architecture:** The section chrome Money shipped is promoted to a shared shell primitive (`PanelBand`) and both pages use it. `teams-activity` aliases to `teams-inbox` in `RETIRED_PANEL_ALIASES` with its section synthesized from the raw key — the exact mechanism the four Money keys use. The attention feed keeps its own component (`AttentionList`), the failed-run feed keeps its own row (`FailedRunRow`), and the two render inside one "Needs you" band; the issue list, its five tabs, its toolbar, its keyboard nav and its archive stack are deleted, because those rows move to the Tasks page in the next PR.

**Tech Stack:** Next.js 16 App Router, React 19, TypeScript, Tailwind v4, SWR, dockview, vitest + @testing-library/react.

**Spec:** `docs/superpowers/specs/2026-09-06-owner-tabs-consolidation-design.md` (the Inbox and Tasks rows were amended on this branch with the owner's 2026-09-08 decisions — read them before Task 1).

**Worktree:** `~/dev/wt-tabs-inbox`, branch `feat/inbox-one-page`, base `origin/main` at `e774a5515`. All paths below are relative to `apps/frontend/` unless stated.

## Global Constraints

- **Inbox holds NO task rows.** Inbox answers "what needs me"; Tasks answers "what is the work". Anything that renders an `Issue` as a list row does not belong on this page.
- **No sub-tabs inside a panel.** Sections down one page. The shell is tabs-only; it already spends the tab metaphor on open panels.
- **Panel keys are permanent.** Mobile builds `?panel=teams-<key>` as strings. An absorbed key ALIASES to the page that absorbed it and `describePanel` synthesizes the section from the RAW key. Mapping absorbed keys in the router instead leaves them as separate panel ids, and the dock dedupes by panel id — that bug shipped four "Money" tabs and was caught in review.
- **Brand tokens only.** `node scripts/brand-tokens-scan.mjs src` must print 0. No colour literals; use `var(--midnight)`, `var(--midnight-muted)`, `var(--edge)`, `var(--edge-soft)`, `var(--warn)`, and the `.label-caps` / `.font-editorial-serif` utilities.
- **One state vocabulary.** Loading, empty and error states are one calm sentence in `font-editorial-serif italic text-[var(--midnight-muted)]`; an error carries `role="alert"`, an empty list carries `role="status"`.
- **Live-run words are exactly three:** `stalled` (in `text-warn`), `queued`, `running` — and `stalled` is checked BEFORE `queued`. `HomeHappeningNow.tsx` is the shipped reference; do not invent a fourth word.
- **Never render a total the API does not return.** The activity feed is a capped list, not a count of everything.
- **`CompanyNav.test.tsx` asserts the exact label list and section order** and must change in the same commit as the nav.
- **Tests:** run only the named test files, one `vitest` invocation per step. This machine runs out of memory on whole-directory runs. Use `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run <files>`.
- **Commit trailers** on every commit:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3
  ```

---

## File Structure

**Created**
| File | Responsibility |
|---|---|
| `src/components/teams/shell/PanelBand.tsx` | The labelled section band + its one-line empty state. Shared by Money and Inbox. |
| `src/components/teams/inbox/lib/groupFailedRuns.ts` | `groupFailedRuns` lifted out of the dying `NeedsAttentionBand`, so failed runs can group inside "Needs you". |
| `src/components/teams/inbox/InboxNeedsYou.tsx` | Section 1: failed runs + the attention feed, in one band. |
| `src/components/teams/inbox/InboxHappeningNow.tsx` | Section 2: live runs, in Home's vocabulary. |
| `src/components/teams/inbox/InboxRecently.tsx` | Section 3: the activity feed, day-grouped, one type filter. |
| `src/components/teams/inbox/lib/groupByDay.ts` | Day-bucketing for Recently (Today / Yesterday / date). |

**Modified**
| File | Change |
|---|---|
| `src/components/teams/inbox/InboxPage.tsx` | Gutted to: masthead, three bands, section scroll, the onboarding `NewIssueDialog` one-shot. |
| `src/components/teams/inbox/AttentionList.tsx` | Gains `showEmptyState?: boolean` so its "nothing needs you" line does not contradict failed rows above it. |
| `src/components/teams/panels/InboxPanel.tsx` | Takes `section` instead of `initialTab`. |
| `src/components/workspace/panelRegistry.ts` | `teams-activity` → `teams-inbox` alias; `INBOX_SECTIONS`; section synthesis; durable/clearable params; drop `activity` from `TEAMS_LIST_KEYS`. |
| `src/components/teams/TeamsPanelRouter.tsx` | `panel === "inbox"` passes `section`; the `activity` loader goes. |
| `src/app/chat/page.tsx` | Forwards `section` for inbox (mobile). |
| `src/components/chat/companySections.ts` | Drops the Activity sidebar item. |
| `src/components/notifications/RunNotifications.tsx` | Both toast families point at `teams-inbox`, with the section that matches the toast. |
| `src/components/teams/panels/money/{MoneySection→callers}` | Money's four sections import `PanelBand`. |
| `apps/infra/openclaw/skills/lightbulb/SKILL.md` | The Inbox row describes one page with three sections. |

**Deleted**
`inbox/NeedsAttentionBand.tsx`, `inbox/InboxList.tsx`, `inbox/InboxToolbar.tsx`, `inbox/SwipeToArchive.tsx`, `inbox/hooks/useInboxData.ts`, `inbox/hooks/useInboxArchiveStack.ts`, `inbox/hooks/useInboxKeyboardNav.ts`, `inbox/hooks/useReadInboxItems.ts`, `panels/ActivityPanel.tsx`, `panels/money/MoneySection.tsx`, plus each one's test file (enumerated in Task 7).

**Deliberately untouched:** `inbox/IssueRow.tsx`, `inbox/KanbanBoard.tsx`, `inbox/KanbanCard.tsx`, `inbox/KanbanColumn.tsx`, `inbox/IssueFiltersPopover.tsx` — these are Tasks-page machinery that merely lives in the `inbox/` folder. Moving them is the next PR's job, not this one.

---

### Task 1: Promote the section band to a shared shell primitive

**Files:**
- Create: `src/components/teams/shell/PanelBand.tsx`
- Modify: `src/components/teams/shell/index.ts`
- Modify: `src/components/teams/panels/money/PaymentsSection.tsx`, `CustomersSection.tsx`, `ProductsSection.tsx`, `AgentSpendSection.tsx`
- Delete: `src/components/teams/panels/money/MoneySection.tsx`
- Test: `src/components/teams/shell/__tests__/PanelBand.test.tsx` (create)

**Interfaces:**
- Produces: `PanelBand({ id, label, right?, children })` — `id` is the FULL dom id (`money-payments`, `inbox-needs-you`), and `PanelBandEmpty({ children, role? })`.

- [ ] **Step 1: Write the failing test**

Create `src/components/teams/shell/__tests__/PanelBand.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PanelBand, PanelBandEmpty } from "@/components/teams/shell";

describe("PanelBand", () => {
  it("uses the id verbatim so a section deep link can scroll to it", () => {
    const { container } = render(
      <PanelBand id="inbox-needs-you" label="Needs you">
        <p>body</p>
      </PanelBand>,
    );
    expect(container.querySelector("#inbox-needs-you")).not.toBeNull();
  });

  it("labels the section for assistive tech and renders the right slot", () => {
    render(
      <PanelBand id="money-payments" label="Payments" right={<span>3 shown</span>}>
        <p>body</p>
      </PanelBand>,
    );
    expect(screen.getByRole("region", { name: "Payments" })).toBeInTheDocument();
    expect(screen.getByText("3 shown")).toBeInTheDocument();
  });

  it("announces an empty line as a status and an error as an alert", () => {
    const { rerender } = render(<PanelBandEmpty>Nothing yet.</PanelBandEmpty>);
    expect(screen.getByRole("status")).toHaveTextContent("Nothing yet.");
    rerender(<PanelBandEmpty role="alert">Could not load.</PanelBandEmpty>);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load.");
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/shell/__tests__/PanelBand.test.tsx`
Expected: FAIL — `PanelBand` is not exported from `@/components/teams/shell`.

- [ ] **Step 3: Create the primitive**

Create `src/components/teams/shell/PanelBand.tsx`. This is `money/MoneySection.tsx` with the id prefix lifted out to the caller — nothing else changes, so Money's rendering is byte-identical:

```tsx
"use client";

// The chrome every page section shares: a mono-caps label with ONE right-hand
// line (a count, a scope, or a link), then the body. Sections, not sub-tabs
// (spec §2): the shell already spends the tab metaphor on open panels.
//
// `id` is the FULL dom id — `money-payments`, `inbox-needs-you` — because a
// ?section deep link scrolls to it by that exact string.

import type { ReactNode } from "react";

export interface PanelBandProps {
  id: string;
  label: string;
  right?: ReactNode;
  children: ReactNode;
}

export function PanelBand({ id, label, right, children }: PanelBandProps) {
  return (
    <section id={id} aria-label={label} className="flex min-w-0 flex-col gap-2 scroll-mt-4">
      <div className="flex items-baseline justify-between gap-3 border-b border-[var(--edge-soft)] pb-1.5">
        <p className="label-caps text-[var(--midnight-muted)]">{label}</p>
        {right && <div className="flex items-center gap-3 text-xs text-[var(--midnight-muted)]">{right}</div>}
      </div>
      {children}
    </section>
  );
}

/** The one calm line for an empty, loading or failed section. */
export function PanelBandEmpty({
  children,
  role = "status",
}: {
  children: ReactNode;
  role?: "status" | "alert";
}) {
  return (
    <p
      role={role}
      className="px-2 py-3 font-editorial-serif text-base italic text-[var(--midnight-muted)]"
    >
      {children}
    </p>
  );
}
```

Append to `src/components/teams/shell/index.ts`:

```ts
export { PanelBand, PanelBandEmpty } from "./PanelBand";
export type { PanelBandProps } from "./PanelBand";
```

- [ ] **Step 4: Repoint Money's four sections**

In each of `PaymentsSection.tsx`, `CustomersSection.tsx`, `ProductsSection.tsx`, `AgentSpendSection.tsx`:
- replace `import { MoneySection, MoneyEmptyLine } from "./MoneySection";` (the exact import list varies per file — some import only one of the two) with `import { PanelBand, PanelBandEmpty } from "@/components/teams/shell";`
- replace `<MoneySection id="payments"` with `<PanelBand id="money-payments"` (and likewise `customers`, `products`, `spend`), closing tags too
- replace `<MoneyEmptyLine>` with `<PanelBandEmpty>`

Then `git rm apps/frontend/src/components/teams/panels/money/MoneySection.tsx`.

Grep for stragglers before moving on: `grep -rn "MoneySection\|MoneyEmptyLine" src/` must return nothing.

- [ ] **Step 5: Run the new test and Money's existing suites**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/shell/__tests__/PanelBand.test.tsx src/components/teams/panels/money/__tests__/sections.test.tsx src/components/teams/panels/money/__tests__/AgentSpendSection.test.tsx src/components/teams/panels/__tests__/MoneyPanel.test.tsx`
Expected: PASS, all four files. Money's tests assert `#money-payments` / `#money-spend` and must be untouched by this change — if one goes red, the id prefix was dropped somewhere.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(teams): the section band is a shell primitive, not a Money local

Inbox needs the same chrome, so MoneySection moves to shell/PanelBand with
the id prefix lifted to the caller. Money renders byte-identically.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3
EOF
)"
```

---

### Task 2: One panel id — Activity aliases to Inbox and carries its section

**Files:**
- Modify: `src/components/workspace/panelRegistry.ts`
- Modify: `src/components/teams/TeamsPanelRouter.tsx`
- Modify: `src/components/workspace/panels/TeamsDockPanel.tsx`
- Modify: `src/app/chat/page.tsx`
- Modify: `src/components/chat/companySections.ts`
- Modify: `src/components/notifications/RunNotifications.tsx`
- Modify: `src/components/teams/panels/InboxPanel.tsx`
- Test: `src/components/workspace/__tests__/panelRegistry.test.ts`, `src/components/control/__tests__/CompanyNav.test.tsx`

**Interfaces:**
- Produces: `INBOX_SECTIONS: ReadonlySet<string>` = `{"needs-you","happening-now","recently"}` and `type InboxSectionId = "needs-you" | "happening-now" | "recently"`, both exported from `panelRegistry.ts`; `InboxPanel({ section })`; `describePanel("teams-activity", …)` returns `panelId: "teams-inbox"`, `params.section === "recently"`.
- Consumes: nothing from Task 1.

- [ ] **Step 1: Write the failing tests**

Add to `src/components/workspace/__tests__/panelRegistry.test.ts`:

```ts
it("aliases teams-activity onto Inbox, pre-scrolled to Recently", () => {
  const d = describePanel("teams-activity", {});
  expect(d?.panelId).toBe("teams-inbox");
  expect(d?.title).toBe("Inbox");
  expect(d?.params.section).toBe("recently");
});

it("keeps one panel id when Activity and Inbox are both opened", () => {
  expect(describePanel("teams-activity", {})?.panelId).toBe(
    describePanel("teams-inbox", {})?.panelId,
  );
});

it("carries an explicit ?section for a fresh Inbox deep link", () => {
  expect(describePanel("teams-inbox", { section: "happening-now" })?.params.section).toBe(
    "happening-now",
  );
});

it("ignores a section that is not a real Inbox band", () => {
  expect(describePanel("teams-inbox", { section: "constructor" })?.params.section).toBeUndefined();
});

it("lands a legacy ?tab=approvals bookmark on Needs you", () => {
  expect(describePanel("teams-inbox", { tab: "approvals" })?.params.section).toBe("needs-you");
});

it("makes section the durable Inbox param", () => {
  expect(durableParamsFor("teams-inbox")).toEqual(["panel", "section"]);
  expect(clearablePanelParamKeys("teams-inbox")).toEqual(["section"]);
});
```

In `src/components/control/__tests__/CompanyNav.test.tsx`, remove `"Activity"` from the expected label list (it sits under the Company group today).

- [ ] **Step 2: Run them and watch them fail**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/workspace/__tests__/panelRegistry.test.ts src/components/control/__tests__/CompanyNav.test.tsx`
Expected: FAIL — `teams-activity` still resolves to its own panel id and `params.section` is undefined.

- [ ] **Step 3: Registry — alias, sections, params**

In `src/components/workspace/panelRegistry.ts`:

Add to `RETIRED_PANEL_ALIASES`, beside the four Money entries:

```ts
  // Activity folded into Inbox's "Recently" band (spec §3). Aliased, not
  // routed: the dock dedupes by panel id, so a router-level mapping would
  // leave an Activity link opening a SECOND tab titled "Inbox".
  "teams-activity": "teams-inbox",
```

Add beside `MONEY_SECTIONS`:

```ts
/** The three bands of the Inbox page, in render order. A ?section deep link
 *  scrolls to `inbox-<section>`; anything else is ignored. */
export const INBOX_SECTIONS: ReadonlySet<string> = new Set([
  "needs-you",
  "happening-now",
  "recently",
]);

/** Declared HERE, not in InboxPage, so the router and InboxPanel can narrow to
 *  it in this task — InboxPage is not rewritten until Task 6. (Money declares
 *  `MoneySectionId` in MoneyPanel.tsx because that panel already existed when
 *  its router branch was written; this page does not yet.) */
export type InboxSectionId = "needs-you" | "happening-now" | "recently";
```

Remove the `activity: "Activity",` line from `TEAMS_LIST_KEYS`.

In `describePanel`, replace the existing inbox `?tab` line:

```ts
      if (teamsKey === "inbox" && params.tab) p.tab = params.tab;
```

with:

```ts
      // Inbox absorbed Activity, so the band a link meant survives only if it
      // is read off the RAW key — same synthesis, same reason, as `money`.
      if (teamsKey === "inbox") {
        if (panelKey === "teams-activity") p.section = "recently";
        else if (params.section && INBOX_SECTIONS.has(params.section)) p.section = params.section;
        // Bookmarks predating the pill tabs still carry ?tab=approvals; that
        // band is now "Needs you". Every other old tab value named an issue
        // list, which lives on Tasks — those land at the top of Inbox.
        else if (params.tab === "approvals") p.section = "needs-you";
      }
```

Update the two param functions:

```ts
  if (panelId === "teams-inbox") return ["panel", "section"];
```
in `durableParamsFor`, and
```ts
  if (panelId === "teams-inbox") return ["section"];
```
in `clearablePanelParamKeys`.

- [ ] **Step 4: Router, shells and nav**

`src/components/teams/TeamsPanelRouter.tsx`:
- delete the `activity: () => import(...ActivityPanel...)` entry from `PANEL_LOADERS`
- replace the `panel === "inbox"` branch's `initialTab={panelParams?.tab ?? null}` with `section={asInboxSection(panelParams?.section)}`
- add the narrowing helper beside the existing `asMoneySection`:

```ts
function asInboxSection(value: string | undefined): InboxSectionId | undefined {
  return value && INBOX_SECTIONS.has(value) ? (value as InboxSectionId) : undefined;
}
```

Import both from the registry: `import { INBOX_SECTIONS, type InboxSectionId } from "@/components/workspace/panelRegistry";` — the file already imports `MONEY_SECTIONS` from there, so add to that import.

`src/components/teams/panels/InboxPanel.tsx`: replace the `initialTab?: string | null` prop with `section?: InboxSectionId`, and pass it through to `<InboxPage section={section} … />`. Export the type from `InboxPage`:

```ts
export type InboxSectionId = "needs-you" | "happening-now" | "recently";
```

`src/components/workspace/panels/TeamsDockPanel.tsx`: verified — it already declares `section?: string` (line 22, commented "durable money section") and spreads it into `panelParams` (line 60), so inbox gets it for free. Two edits: widen that comment to say Money AND Inbox, and DELETE the now-dead `tab` param — its declaration (line 19, "durable inbox band deep link") and its spread (line 57) exist only for the pill tabs.

`src/app/chat/page.tsx`: the mobile shell reads `tabParam` for inbox — replace that read with the same `section` synthesis the desktop registry does, so a mobile `?panel=teams-activity` lands on Recently. Reuse `describePanel`'s output rather than re-deriving if the shell already calls it.

`src/components/chat/companySections.ts`: delete the `{ panel: "teams-activity", label: "Activity", … }` item from the Company group. Leave the Inbox item where it is.

`src/components/notifications/RunNotifications.tsx`: `viewTeamsAction` currently takes `"teams-activity" | "teams-inbox"`. Point both families at Inbox with the band that matches the toast:

```ts
// Attention toasts (approvals, blocked) are decisions → Needs you.
// Background-run toasts (working, success, failure) are history → Recently.
function viewTeamsAction(section: "needs-you" | "recently") {
  return { label: "View", href: `/chat?panel=teams-inbox&section=${section}` };
}
```
and update its two call sites (the attention branch passes `"needs-you"`, the run branch `"recently"`), keeping whatever click/label wrapper the function already has.

- [ ] **Step 5: Run the tests**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/workspace/__tests__/panelRegistry.test.ts src/components/control/__tests__/CompanyNav.test.tsx src/components/workspace/__tests__/TeamsDockPanel.test.tsx src/components/workspace/__tests__/urlChannel.test.ts`
Expected: PASS. `urlChannel.test.ts` hard-codes `?panel=teams-inbox&tab=approvals` in a clear-panel assertion — change that string to `&section=needs-you`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(teams): Activity folds into Inbox as a section, not a second tab

teams-activity aliases to teams-inbox and synthesizes section=recently from
the raw key — the Money precedent. One panel id, so a run toast and the
sidebar cannot open two tabs both titled Inbox.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3
EOF
)"
```

---

### Task 3: "Needs you" — decisions and failed runs in one band

**Files:**
- Create: `src/components/teams/inbox/lib/groupFailedRuns.ts`
- Create: `src/components/teams/inbox/InboxNeedsYou.tsx`
- Modify: `src/components/teams/inbox/AttentionList.tsx`
- Test: `src/components/teams/inbox/__tests__/InboxNeedsYou.test.tsx` (create), `src/components/teams/inbox/lib/__tests__/groupFailedRuns.test.ts` (create)

**Interfaces:**
- Consumes: `PanelBand`, `PanelBandEmpty` from Task 1.
- Produces: `InboxNeedsYou({ companyId, agents, currentUserId, onOpenIssue })`.

**Why failed runs live here:** `useAttentionFeed` deliberately excludes `failed_run` because those runs "keep their own home in the Agent failures band" — and this PR deletes that band. Relying on the attention feed alone would silently drop failed runs from the owner's view, so the two feeds render together under one label. They stay separate data paths; only the presentation merges.

- [ ] **Step 1: Write the failing test for the grouping helper**

Create `src/components/teams/inbox/lib/__tests__/groupFailedRuns.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { groupFailedRuns } from "@/components/teams/inbox/lib/groupFailedRuns";
import type { HeartbeatRun } from "@/components/teams/shared/types";

const run = (over: Partial<HeartbeatRun>): HeartbeatRun =>
  ({ id: "r1", status: "failed", agentId: "a1", failureReason: "timeout", startedAt: "2026-09-08T10:00:00Z", ...over }) as HeartbeatRun;

describe("groupFailedRuns", () => {
  it("collapses identical agent+reason failures and counts them", () => {
    const groups = groupFailedRuns([run({ id: "r1" }), run({ id: "r2" })]);
    expect(groups).toHaveLength(1);
    expect(groups[0].count).toBe(2);
  });

  it("keeps distinct reasons apart", () => {
    const groups = groupFailedRuns([run({ id: "r1" }), run({ id: "r2", failureReason: "oom" })]);
    expect(groups).toHaveLength(2);
  });

  it("represents a group by its most recent occurrence", () => {
    const groups = groupFailedRuns([
      run({ id: "old", startedAt: "2026-09-08T09:00:00Z" }),
      run({ id: "new", startedAt: "2026-09-08T11:00:00Z" }),
    ]);
    expect(groups[0].representative.id).toBe("new");
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/lib/__tests__/groupFailedRuns.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Lift the helper out of the dying band**

Create `src/components/teams/inbox/lib/groupFailedRuns.ts` by moving `FailedRunGroup`, `startedAtDesc` and `groupFailedRuns` verbatim out of `NeedsAttentionBand.tsx` (lines ~20-63) and exporting the type and the function. Do not change the logic — its grouping behaviour is covered by the band's existing tests, which Task 7 ports here.

```ts
import type { HeartbeatRun } from "@/components/teams/shared/types";

/** A run plus the identical repeats it stands for, so a flapping heartbeat
 *  shows as one row with a ×N badge instead of N copies. */
export interface FailedRunGroup {
  key: string;
  representative: HeartbeatRun;
  runs: HeartbeatRun[];
  count: number;
}
```
followed by the two functions exactly as they read in `NeedsAttentionBand.tsx` today, with `export` on `groupFailedRuns`.

- [ ] **Step 4: Run the helper test**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/lib/__tests__/groupFailedRuns.test.ts`
Expected: PASS.

- [ ] **Step 5: Write the failing test for the section**

Create `src/components/teams/inbox/__tests__/InboxNeedsYou.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InboxNeedsYou } from "@/components/teams/inbox/InboxNeedsYou";

const attention = vi.hoisted(() => ({ value: { items: [], count: 0, isLoading: false, isError: false, refresh: vi.fn() } }));
const triage = vi.hoisted(() => ({ value: { failedRuns: [], count: 0, isLoading: false, isError: false } }));

vi.mock("@/components/teams/inbox/hooks/useAttentionFeed", () => ({
  useAttentionFeed: () => attention.value,
}));
vi.mock("@/components/teams/inbox/hooks/useInboxTriage", () => ({
  useInboxTriage: () => triage.value,
}));
vi.mock("@/components/teams/inbox/hooks/useAcknowledgedRuns", () => ({
  useAcknowledgedRuns: () => ({ acknowledgedRunIds: new Set<string>(), isAcked: () => false, ack: vi.fn() }),
}));

beforeEach(() => {
  attention.value = { items: [], count: 0, isLoading: false, isError: false, refresh: vi.fn() };
  triage.value = { failedRuns: [], count: 0, isLoading: false, isError: false };
});

describe("InboxNeedsYou", () => {
  it("is one labelled band with a section id a deep link can reach", () => {
    const { container } = render(<InboxNeedsYou companyId="c1" agents={[]} />);
    expect(container.querySelector("#inbox-needs-you")).not.toBeNull();
    expect(screen.getByRole("region", { name: "Needs you" })).toBeInTheDocument();
  });

  it("says nothing needs you when both feeds are empty", () => {
    render(<InboxNeedsYou companyId="c1" agents={[]} />);
    expect(screen.getByRole("status")).toHaveTextContent(/nothing needs you/i);
  });

  it("shows a failed run and does NOT also claim nothing needs you", () => {
    triage.value = {
      failedRuns: [{ id: "r1", status: "failed", agentId: "a1", failureReason: "timeout", startedAt: "2026-09-08T10:00:00Z" }],
      count: 1,
      isLoading: false,
      isError: false,
    } as never;
    render(<InboxNeedsYou companyId="c1" agents={[{ id: "a1", name: "Atlas" } as never]} />);
    expect(screen.getByText(/timeout/i)).toBeInTheDocument();
    expect(screen.queryByText(/nothing needs you/i)).toBeNull();
  });

  it("counts both feeds in the right-hand line", () => {
    attention.value = { ...attention.value, count: 2 };
    triage.value = { ...triage.value, count: 1 } as never;
    render(<InboxNeedsYou companyId="c1" agents={[]} />);
    expect(screen.getByText("3 waiting")).toBeInTheDocument();
  });
});
```

- [ ] **Step 6: Run it and watch it fail**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/__tests__/InboxNeedsYou.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 7: Let AttentionList yield its empty line**

In `src/components/teams/inbox/AttentionList.tsx`, add to `AttentionListProps`:

```ts
  /** When false, render nothing instead of the "nothing needs you" line —
   *  the caller has other rows above and that line would contradict them. */
  showEmptyState?: boolean;
```
default it in the signature (`showEmptyState = true`) and guard the existing empty branch with it, returning `null` when it is false and there is nothing to show. Leave the loading and error branches alone: those are still true statements about this feed.

- [ ] **Step 8: Write the section**

Create `src/components/teams/inbox/InboxNeedsYou.tsx`:

```tsx
"use client";

// Inbox section 1 — everything waiting on a human, in one band.
//
// Two disjoint feeds render here on purpose. `useAttentionFeed` is the
// server-ranked decision queue (approvals, agent questions, blocked, recovery)
// and deliberately EXCLUDES failed runs, which used to live in their own
// "Agent failures" band. That band is gone, so its feed (`useInboxTriage`,
// dismissed locally via `useAcknowledgedRuns`) renders here too — otherwise a
// failed run would have nowhere left to be seen.

import { PanelBand, PanelBandEmpty } from "@/components/teams/shell";
import { AttentionList } from "@/components/teams/inbox/AttentionList";
import { FailedRunRow } from "@/components/teams/inbox/FailedRunRow";
import { groupFailedRuns } from "@/components/teams/inbox/lib/groupFailedRuns";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";
import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";
import type { CompanyAgent } from "@/components/teams/shared/types";

export function InboxNeedsYou({
  companyId,
  agents,
  currentUserId = null,
  onOpenIssue,
}: {
  companyId: string;
  agents: CompanyAgent[];
  currentUserId?: string | null;
  onOpenIssue?: (issueId: string) => void;
}) {
  const { acknowledgedRunIds, ack } = useAcknowledgedRuns(companyId);
  const { failedRuns, count: failedCount, isError: runsError } = useInboxTriage(acknowledgedRunIds);
  const { count: decisionCount } = useAttentionFeed();

  const groups = groupFailedRuns(failedRuns);
  const agentName = (id: string | null | undefined) =>
    agents.find((a) => a.id === id)?.name ?? "An agent";
  const waiting = decisionCount + failedCount;

  return (
    <PanelBand
      id="inbox-needs-you"
      label="Needs you"
      right={waiting > 0 ? <span>{waiting} waiting</span> : undefined}
    >
      {runsError && (
        <PanelBandEmpty role="alert">Couldn&apos;t check for failed runs just now.</PanelBandEmpty>
      )}
      {groups.length > 0 && (
        <ul className="flex flex-col">
          {groups.map((group) => (
            <FailedRunRow
              key={group.key}
              run={group.representative}
              agentName={agentName(group.representative.agentId)}
              count={group.count}
              onDismiss={() => group.runs.forEach((r) => ack(r.id))}
            />
          ))}
        </ul>
      )}
      <AttentionList
        agents={agents}
        currentUserId={currentUserId}
        onOpenIssue={onOpenIssue}
        showEmptyState={groups.length === 0}
      />
    </PanelBand>
  );
}
```

- [ ] **Step 9: Run both new test files plus AttentionList's**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/__tests__/InboxNeedsYou.test.tsx src/components/teams/inbox/lib/__tests__/groupFailedRuns.test.ts src/components/teams/inbox/__tests__/AttentionList.test.tsx`
Expected: PASS. `AttentionList.test.tsx` must stay green untouched — the new prop defaults to today's behaviour.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(inbox): one "Needs you" band — decisions and failed runs together

The attention feed excludes failed runs because they had their own band;
that band is being deleted, so its feed renders here rather than vanishing.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3
EOF
)"
```

---

### Task 4: "Happening now" — the live runs band

**Files:**
- Create: `src/components/teams/inbox/InboxHappeningNow.tsx`
- Test: `src/components/teams/inbox/__tests__/InboxHappeningNow.test.tsx` (create)

**Interfaces:**
- Consumes: `PanelBand`, `PanelBandEmpty`; `useLiveRuns`, `isRunActive`, `liveRunActivity`, `liveRunTitle` from `@/components/teams/dashboard/hooks/useLiveRuns`.
- Produces: `InboxHappeningNow()` — no props.

Home already ships this vocabulary in `dashboard/HomeHappeningNow.tsx`. Read it first and match it: three words only, `stalled` checked before `queued`.

- [ ] **Step 1: Write the failing test**

Create `src/components/teams/inbox/__tests__/InboxHappeningNow.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InboxHappeningNow } from "@/components/teams/inbox/InboxHappeningNow";

const live = vi.hoisted(() => ({ runs: [] as unknown[], isLoading: false, isError: false }));
vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", async (orig) => ({
  ...(await orig<typeof import("@/components/teams/dashboard/hooks/useLiveRuns")>()),
  useLiveRuns: () => live,
}));

beforeEach(() => {
  live.runs = [];
  live.isLoading = false;
  live.isError = false;
});

describe("InboxHappeningNow", () => {
  it("is a labelled band with a deep-linkable id", () => {
    const { container } = render(<InboxHappeningNow />);
    expect(container.querySelector("#inbox-happening-now")).not.toBeNull();
    expect(screen.getByRole("region", { name: "Happening now" })).toBeInTheDocument();
  });

  it("says nobody is running when the feed is empty", () => {
    render(<InboxHappeningNow />);
    expect(screen.getByRole("status")).toHaveTextContent(/nobody is running/i);
  });

  it("calls a silent run stalled before it calls it queued", () => {
    live.runs = [
      { id: "r1", status: "queued", agentName: "Atlas", nextAction: "Scrape leads", outputSilence: { level: "critical" } },
    ];
    render(<InboxHappeningNow />);
    expect(screen.getByText("stalled")).toBeInTheDocument();
    expect(screen.queryByText("queued")).toBeNull();
  });

  it("names the agent and what it is doing", () => {
    live.runs = [{ id: "r1", status: "running", agentName: "Friday", nextAction: "Draft the brief" }];
    render(<InboxHappeningNow />);
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.getByText("Draft the brief")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/__tests__/InboxHappeningNow.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the section**

Create `src/components/teams/inbox/InboxHappeningNow.tsx`:

```tsx
"use client";

// Inbox section 2 — the live view of the team, same feed and same three words
// as Home's "Happening now" (dashboard/HomeHappeningNow.tsx). `stalled` is
// checked BEFORE `queued`: a queued run that has gone silent is stalled, and
// saying "queued" there would be a comfortable lie.

import {
  isRunActive,
  liveRunActivity,
  liveRunTitle,
  useLiveRuns,
} from "@/components/teams/dashboard/hooks/useLiveRuns";
import { PanelBand, PanelBandEmpty } from "@/components/teams/shell";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";

const LIMIT = 6;

export function InboxHappeningNow() {
  const { runs, isLoading, isError } = useLiveRuns();
  const active = runs.filter(isRunActive).slice(0, LIMIT);

  return (
    <PanelBand
      id="inbox-happening-now"
      label="Happening now"
      right={active.length > 0 ? <span>{active.length} running</span> : undefined}
    >
      {isLoading && runs.length === 0 ? (
        <PanelBandEmpty>Looking at what&apos;s running…</PanelBandEmpty>
      ) : isError && runs.length === 0 ? (
        <PanelBandEmpty role="alert">Couldn&apos;t see what&apos;s running just now.</PanelBandEmpty>
      ) : active.length === 0 ? (
        <PanelBandEmpty>Nobody is running right now.</PanelBandEmpty>
      ) : (
        <ul className="flex flex-col">
          {active.map((run) => {
            const started = run.startedAt ?? run.createdAt;
            const stalled = liveRunActivity(run) === "stalled";
            const href = run.issueId ? teamsDetailUrl("issue", run.issueId) : null;
            const body = (
              <>
                <span className="min-w-0 flex-1 truncate text-[var(--midnight)]">
                  {run.nextAction?.trim() || "Working"}
                </span>
                <span className="shrink-0 text-xs text-[var(--midnight-muted)]">
                  {run.agentName ?? "Agent"}
                  {started ? ` · ${timeAgo(started)}` : ""}
                </span>
                <span className="w-16 shrink-0 text-right text-xs">
                  {stalled ? (
                    <span className="text-warn">stalled</span>
                  ) : run.status === "queued" ? (
                    "queued"
                  ) : (
                    "running"
                  )}
                </span>
              </>
            );
            return (
              <li key={run.id} className="border-b border-[var(--edge-soft)] last:border-b-0">
                {href ? (
                  <a
                    href={href}
                    onClick={handleShellLinkClick}
                    title={liveRunTitle(run)}
                    className="flex items-baseline gap-3 px-2 py-2 hover:bg-[var(--edge-soft)]"
                  >
                    {body}
                  </a>
                ) : (
                  <div title={liveRunTitle(run)} className="flex items-baseline gap-3 px-2 py-2">
                    {body}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </PanelBand>
  );
}
```

- [ ] **Step 4: Run the test**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/__tests__/InboxHappeningNow.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(inbox): "Happening now" band, in Home's live-run vocabulary

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3
EOF
)"
```

---

### Task 5: "Recently" — the activity feed, day-grouped

**Files:**
- Create: `src/components/teams/inbox/lib/groupByDay.ts`
- Create: `src/components/teams/inbox/InboxRecently.tsx`
- Test: `src/components/teams/inbox/lib/__tests__/groupByDay.test.ts` (create), `src/components/teams/inbox/__tests__/InboxRecently.test.tsx` (create)

**Interfaces:**
- Consumes: `PanelBand`, `PanelBandEmpty`; `useActivityFeed(limit)` → `{ events: ActivityEvent[]; isLoading; isError }`; `ActivityRow({ event, agentNames? })`.
- Produces: `InboxRecently({ agents })`, `groupByDay<T>(items, getIso, now?)` → `{ label: string; items: T[] }[]`.

Two fixes ride along, both already flagged in review: `ActivityPanel` never wired `agentNames`, so every actor rendered as a raw id — this section passes the roster. And its stale header comment ("ActivityRow degrades to the raw entity id without those maps") dies with the file.

- [ ] **Step 1: Write the failing test for day grouping**

Create `src/components/teams/inbox/lib/__tests__/groupByDay.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { groupByDay } from "@/components/teams/inbox/lib/groupByDay";

const NOW = new Date("2026-09-08T12:00:00Z");
const at = (iso: string) => ({ createdAt: iso });

describe("groupByDay", () => {
  it("labels today and yesterday by name", () => {
    const groups = groupByDay(
      [at("2026-09-08T09:00:00Z"), at("2026-09-07T09:00:00Z")],
      (e) => e.createdAt,
      NOW,
    );
    expect(groups.map((g) => g.label)).toEqual(["Today", "Yesterday"]);
  });

  it("keeps same-day items in one group, newest group first", () => {
    const groups = groupByDay(
      [at("2026-09-08T09:00:00Z"), at("2026-09-08T08:00:00Z"), at("2026-09-06T08:00:00Z")],
      (e) => e.createdAt,
      NOW,
    );
    expect(groups).toHaveLength(2);
    expect(groups[0].items).toHaveLength(2);
  });

  it("dates anything older than yesterday", () => {
    const groups = groupByDay([at("2026-09-01T09:00:00Z")], (e) => e.createdAt, NOW);
    expect(groups[0].label).not.toMatch(/today|yesterday/i);
    expect(groups[0].label).toMatch(/Sep/);
  });

  it("drops items with an unparseable timestamp rather than inventing a day", () => {
    expect(groupByDay([at("not-a-date")], (e) => e.createdAt, NOW)).toEqual([]);
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/lib/__tests__/groupByDay.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the helper**

Create `src/components/teams/inbox/lib/groupByDay.ts`:

```ts
/** Bucket a newest-first list into calendar days in the viewer's timezone.
 *  Today and yesterday get names; everything older gets a date. Items whose
 *  timestamp will not parse are dropped — a wrong day is worse than no row. */
export interface DayGroup<T> {
  label: string;
  items: T[];
}

const DATE_LABEL = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
});

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

export function groupByDay<T>(
  items: readonly T[],
  getIso: (item: T) => string | null | undefined,
  now: Date = new Date(),
): DayGroup<T>[] {
  const todayKey = dayKey(now);
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  const yesterdayKey = dayKey(yesterday);

  const order: string[] = [];
  const byKey = new Map<string, { label: string; items: T[] }>();

  for (const item of items) {
    const iso = getIso(item);
    const when = iso ? new Date(iso) : null;
    if (!when || Number.isNaN(when.getTime())) continue;
    const key = dayKey(when);
    let bucket = byKey.get(key);
    if (!bucket) {
      bucket = {
        label:
          key === todayKey ? "Today" : key === yesterdayKey ? "Yesterday" : DATE_LABEL.format(when),
        items: [],
      };
      byKey.set(key, bucket);
      order.push(key);
    }
    bucket.items.push(item);
  }

  return order.map((key) => byKey.get(key)!);
}
```

- [ ] **Step 4: Run the helper test**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/lib/__tests__/groupByDay.test.ts`
Expected: PASS.

- [ ] **Step 5: Write the failing test for the section**

Create `src/components/teams/inbox/__tests__/InboxRecently.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InboxRecently } from "@/components/teams/inbox/InboxRecently";

const feed = vi.hoisted(() => ({ events: [] as unknown[], isLoading: false, isError: false }));
vi.mock("@/components/teams/dashboard/hooks/useActivityFeed", () => ({
  useActivityFeed: () => feed,
}));

const evt = (over: Record<string, unknown>) => ({
  id: "e1",
  action: "created",
  entityType: "issue",
  entityId: "i1",
  createdAt: new Date().toISOString(),
  ...over,
});

beforeEach(() => {
  feed.events = [];
  feed.isLoading = false;
  feed.isError = false;
});

describe("InboxRecently", () => {
  it("is a labelled band with a deep-linkable id", () => {
    const { container } = render(<InboxRecently agents={[]} />);
    expect(container.querySelector("#inbox-recently")).not.toBeNull();
    expect(screen.getByRole("region", { name: "Recently" })).toBeInTheDocument();
  });

  it("says nothing happened yet when the feed is empty", () => {
    render(<InboxRecently agents={[]} />);
    expect(screen.getByRole("status")).toHaveTextContent(/nothing has happened yet/i);
  });

  it("groups events under a day heading", () => {
    feed.events = [evt({ id: "e1" })];
    render(<InboxRecently agents={[]} />);
    expect(screen.getByText("Today")).toBeInTheDocument();
  });

  it("filters by type and says so when the filter empties the list", async () => {
    feed.events = [evt({ id: "e1", entityType: "issue" }), evt({ id: "e2", entityType: "agent" })];
    render(<InboxRecently agents={[]} />);
    await userEvent.selectOptions(screen.getByLabelText("Filter by type"), "agent");
    expect(screen.queryByRole("status")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Filter by type"), "issue");
    expect(screen.getByText("Today")).toBeInTheDocument();
  });

  it("surfaces a read failure as an alert", () => {
    feed.isError = true;
    render(<InboxRecently agents={[]} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn.t load/i);
  });
});
```

- [ ] **Step 6: Run it and watch it fail**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/__tests__/InboxRecently.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 7: Write the section**

Create `src/components/teams/inbox/InboxRecently.tsx`:

```tsx
"use client";

// Inbox section 3 — what happened, day by day. This is the old Activity page,
// which is now a band rather than its own tab.
//
// It passes `agentNames` to ActivityRow, which the Activity page never did:
// the row has been able to resolve agent actors to roster names since June, but
// its only caller never wired the map, so every actor rendered as a raw id.

import { useMemo, useState } from "react";
import { ActivityRow } from "@/components/teams/dashboard/ActivityRow";
import { useActivityFeed } from "@/components/teams/dashboard/hooks/useActivityFeed";
import { groupByDay } from "@/components/teams/inbox/lib/groupByDay";
import { PanelBand, PanelBandEmpty } from "@/components/teams/shell";
import type { CompanyAgent } from "@/components/teams/shared/types";

// Matches the old Activity page's cap. The feed is a capped list, never a
// count of everything that ever happened.
const LIMIT = 200;

export function InboxRecently({ agents }: { agents: CompanyAgent[] }) {
  const { events, isLoading, isError } = useActivityFeed(LIMIT);
  const [filter, setFilter] = useState("all");

  const agentNames = useMemo(
    () => new Map(agents.map((a) => [a.id, a.name])),
    [agents],
  );

  const entityTypes = useMemo(
    () =>
      Array.from(new Set(events.map((e) => e.entityType).filter((t): t is string => !!t))).sort(),
    [events],
  );

  const filtered = useMemo(
    () => (filter === "all" ? events : events.filter((e) => e.entityType === filter)),
    [events, filter],
  );

  const days = useMemo(() => groupByDay(filtered, (e) => e.createdAt), [filtered]);

  return (
    <PanelBand
      id="inbox-recently"
      label="Recently"
      right={
        entityTypes.length > 0 ? (
          <select
            aria-label="Filter by type"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="h-7 rounded-sm border border-[var(--edge)] bg-[var(--cream)] px-2 text-xs text-[var(--midnight)]"
          >
            <option value="all">All types</option>
            {entityTypes.map((type) => (
              <option key={type} value={type}>
                {type.charAt(0).toUpperCase() + type.slice(1)}
              </option>
            ))}
          </select>
        ) : undefined
      }
    >
      {isLoading && events.length === 0 ? (
        <PanelBandEmpty>Looking at what happened…</PanelBandEmpty>
      ) : isError && events.length === 0 ? (
        <PanelBandEmpty role="alert">Couldn&apos;t load what happened.</PanelBandEmpty>
      ) : events.length === 0 ? (
        <PanelBandEmpty>Nothing has happened yet.</PanelBandEmpty>
      ) : (
        <div className="flex flex-col gap-3">
          {days.map((day) => (
            <div key={day.label} className="flex flex-col">
              <p className="label-caps sticky top-0 bg-[var(--cream)] py-1 text-[var(--midnight-muted)]">
                {day.label}
              </p>
              {day.items.map((evt) => (
                <div key={evt.id} className="border-b border-[var(--edge-soft)] last:border-b-0">
                  <ActivityRow event={evt} agentNames={agentNames} />
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </PanelBand>
  );
}
```

Note the empty-vs-filtered distinction the test pins: when the FEED is empty you get the status line; when a filter merely hides everything you get no rows and no status line, because "nothing has happened yet" would be false.

- [ ] **Step 8: Run the test**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/__tests__/InboxRecently.test.tsx`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(inbox): "Recently" band — the activity feed, grouped by day

Also wires ActivityRow's agentNames, which the Activity page never passed,
so an actor reads as a name instead of a raw agent id.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3
EOF
)"
```

---

### Task 6: The page — three bands, one masthead, section scroll

**Files:**
- Modify: `src/components/teams/inbox/InboxPage.tsx` (rewrite)
- Test: `src/components/teams/inbox/__tests__/InboxPage.test.tsx` (rewrite), `src/components/teams/inbox/__tests__/InboxPage.prefill.test.tsx` (keep passing)

**Interfaces:**
- Consumes: `InboxNeedsYou`, `InboxHappeningNow`, `InboxRecently`, `InboxSectionId` from Task 2.
- Produces: `InboxPage({ companyId, currentUserId, agents, section, newIssue, onNewIssueConsumed })`.

The scroll must be scoped to the page's own root and must not fire before the bands have content — the Money review caught exactly this bug with a global `document.getElementById`.

- [ ] **Step 1: Rewrite the page test**

Replace `src/components/teams/inbox/__tests__/InboxPage.test.tsx` with:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InboxPage } from "@/components/teams/inbox/InboxPage";

vi.mock("@/components/teams/inbox/InboxNeedsYou", () => ({
  InboxNeedsYou: () => <section id="inbox-needs-you" aria-label="Needs you" />,
}));
vi.mock("@/components/teams/inbox/InboxHappeningNow", () => ({
  InboxHappeningNow: () => <section id="inbox-happening-now" aria-label="Happening now" />,
}));
vi.mock("@/components/teams/inbox/InboxRecently", () => ({
  InboxRecently: () => <section id="inbox-recently" aria-label="Recently" />,
}));

const props = { companyId: "c1", currentUserId: "u1", agents: [] };

describe("InboxPage", () => {
  it("renders the three bands in order and no task list", () => {
    render(<InboxPage {...props} />);
    const regions = screen.getAllByRole("region").map((r) => r.getAttribute("aria-label"));
    expect(regions).toEqual(["Needs you", "Happening now", "Recently"]);
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByPlaceholderText(/search/i)).toBeNull();
  });

  it("scrolls to the requested section, scoped to its own root", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    const { container } = render(<InboxPage {...props} section="recently" />);
    expect(scrollIntoView).toHaveBeenCalled();
    expect(container.querySelector("#inbox-recently")).not.toBeNull();
  });

  it("does not scroll when no section was asked for", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    render(<InboxPage {...props} />);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/__tests__/InboxPage.test.tsx`
Expected: FAIL — the page still renders the toolbar and issue lists.

- [ ] **Step 3: Rewrite the page**

Replace the body of `src/components/teams/inbox/InboxPage.tsx` with:

```tsx
"use client";

// The Inbox page: what needs a human, what is running, what happened.
//
// It holds NO task rows (spec §3, owner decision 2026-09-08). Inbox and Tasks
// used to render the same issue list behind different filters — Inbox with
// five pill tabs, Tasks grouped by goal — which meant two tabs answering the
// same question. They now split by KIND: this page answers "what needs me",
// Tasks answers "what is the work".

import { useCallback, useEffect, useRef } from "react";
import { PanelMasthead } from "@/components/teams/shell";
import { InboxHappeningNow } from "@/components/teams/inbox/InboxHappeningNow";
import { InboxNeedsYou } from "@/components/teams/inbox/InboxNeedsYou";
import { InboxRecently } from "@/components/teams/inbox/InboxRecently";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import type { InboxSectionId } from "@/components/workspace/panelRegistry";
import type { CompanyAgent } from "@/components/teams/shared/types";

export interface InboxPageProps {
  companyId: string;
  currentUserId: string | null;
  agents: CompanyAgent[];
  /** ?section deep link — scrolls to that band once it is on screen. */
  section?: InboxSectionId;
}

export function InboxPage({ companyId, currentUserId, agents, section }: InboxPageProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  // Same nav the page uses today (InboxPage.tsx:107-115): the inline hook,
  // wrapped so an unhydrated attention row can open its thread in the shell.
  const inlineNav = useInlineTeamsNav();
  const openIssue = useCallback(
    (issueId: string) => inlineNav.openDetail("issue", issueId),
    [inlineNav],
  );

  // Scoped to this page's root, not the document: two Inbox panels can be open
  // in one dock, and a global lookup would scroll the wrong one. Runs after
  // the bands mount so the target exists.
  useEffect(() => {
    if (!section) return;
    rootRef.current?.querySelector(`#inbox-${section}`)?.scrollIntoView?.({ block: "start" });
  }, [section]);

  return (
    <div
      ref={rootRef}
      className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-6 max-w-4xl mx-auto w-full"
    >
      <PanelMasthead overline="Operations" title="Inbox" />
      <InboxNeedsYou
        companyId={companyId}
        agents={agents}
        currentUserId={currentUserId}
        onOpenIssue={openIssue}
      />
      <InboxHappeningNow />
      <InboxRecently agents={agents} />
    </div>
  );
}
```

⚠️ `inlineNav.openDetail("issue", id)` is written from the shape at `InboxPage.tsx:107-115`. Open that block first and copy the call it actually makes — if the wrapper does more than one thing (it may also close a dialog or clear selection), keep only the navigation part.

**The onboarding one-shot:** `InboxPage.prefill.test.tsx` asserts that `newIssue="onboarding"` opens `NewIssueDialog` pre-filled. Creating a task from Inbox no longer fits a page with no task rows, and the same one-shot is served by Home's "New task" quick action, which targets Tasks. Move it: change `WorkspaceShell.tsx:476` and `HomeQuickActions.tsx:21` to target `teams-issues` instead of `teams-inbox`, delete the `newIssue` props from `InboxPanel`/`InboxPage`, and delete `InboxPage.prefill.test.tsx` — Tasks already owns `NewIssueDialog` via its own "New task" button. Note this in the PR body as a deliberate move, not a lost affordance.

- [ ] **Step 4: Run the page tests**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=4096' pnpm exec vitest run src/components/teams/inbox/__tests__/InboxPage.test.tsx src/components/workspace/__tests__/WorkspaceShell.test.tsx src/components/teams/dashboard/__tests__/HomeQuickActions.test.tsx`
Expected: PASS. Update the two quick-action tests to expect `teams-issues`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(inbox): one page — Needs you, Happening now, Recently

No task rows: Inbox answers "what needs me", Tasks answers "what is the
work". The five pill tabs, the toolbar and the issue list go. "New task"
moves to Tasks, which owns the dialog.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3
EOF
)"
```

---

### Task 7: Delete what the page no longer uses, and tell the skill

**Files:**
- Delete (components): `inbox/NeedsAttentionBand.tsx`, `inbox/InboxList.tsx`, `inbox/InboxToolbar.tsx`, `inbox/SwipeToArchive.tsx`, `panels/ActivityPanel.tsx`
- Delete (hooks): `inbox/hooks/useInboxData.ts`, `inbox/hooks/useInboxArchiveStack.ts`, `inbox/hooks/useInboxKeyboardNav.ts`, `inbox/hooks/useReadInboxItems.ts`
- Delete (tests): the test file of each of the above, plus `__tests__/teams/NeedsAttentionBand.test.tsx`, `__tests__/teams/ActivityPanel.test.tsx`, `inbox/__tests__/InboxToolbar.test.tsx`, `inbox/__tests__/InboxList.test.tsx`, `inbox/__tests__/SwipeToArchive.test.tsx`, `InboxPage.prefill.test.tsx`
- Modify: `src/components/teams/shared/lib/inboxStorage.ts`, `apps/infra/openclaw/skills/lightbulb/SKILL.md`
- Keep: `IssueRow.tsx`, `KanbanBoard/Card/Column.tsx`, `IssueFiltersPopover.tsx` and their tests — Tasks-page machinery, moved in the next PR.

- [ ] **Step 1: Delete, then let the compiler find the stragglers**

```bash
cd apps/frontend
git rm src/components/teams/inbox/NeedsAttentionBand.tsx \
       src/components/teams/inbox/InboxList.tsx \
       src/components/teams/inbox/InboxToolbar.tsx \
       src/components/teams/inbox/SwipeToArchive.tsx \
       src/components/teams/panels/ActivityPanel.tsx \
       src/components/teams/inbox/hooks/useInboxData.ts \
       src/components/teams/inbox/hooks/useInboxArchiveStack.ts \
       src/components/teams/inbox/hooks/useInboxKeyboardNav.ts \
       src/components/teams/inbox/hooks/useReadInboxItems.ts
```
then delete each one's test file (search for them: `git ls-files | grep -E "NeedsAttentionBand|InboxList|InboxToolbar|SwipeToArchive|ActivityPanel|useInboxData|useInboxArchiveStack|useInboxKeyboardNav|useReadInboxItems" | grep test`). Note there are TWO copies of the `useInboxTriage` test (one under `inbox/hooks/__tests__/`, one under `__tests__/teams/`) — `useInboxTriage` SURVIVES, so keep one and delete the duplicate, saying which in the report.

Port the grouping assertions from `NeedsAttentionBand.test.tsx` (identical agent+reason collapse to ×N, distinct reasons stay apart, bulk dismiss acks every member) into `lib/__tests__/groupFailedRuns.test.ts` and `__tests__/InboxNeedsYou.test.tsx` before deleting it — that behaviour is still live, only its home changed.

- [ ] **Step 2: Prune the tab machinery from storage**

In `src/components/teams/shared/lib/inboxStorage.ts`, delete `InboxTab`, `isInboxTab`, `loadLastInboxTab`, `saveLastInboxTab`, `loadBandCollapsed`, `saveBandCollapsed` and their storage keys — every caller is gone. Keep anything the surviving hooks still import (`useAcknowledgedRuns` has its own key; check before deleting). If `teamsQueryKeys.inbox.list` is now unused, delete it too.

- [ ] **Step 3: Typecheck, lint and scan**

Run:
```bash
cd apps/frontend
pnpm exec tsc --noEmit
pnpm exec eslint src/components/teams/inbox src/components/teams/panels src/components/workspace
node scripts/brand-tokens-scan.mjs src
```
Expected: `tsc` clean, eslint clean on changed files, scan prints 0. `tsc` is the tool that finds every dangling import from Step 1 — fix each by deleting the import, never by resurrecting the file.

- [ ] **Step 4: Update the bundled skill**

In `apps/infra/openclaw/skills/lightbulb/SKILL.md`, find the Inbox row (near the Money section rewritten in #2351) and replace its description with one page and three bands:

```markdown
### Where the owner sees what needs them

One **Inbox** tab, three bands down one page — point them at it rather than
describing counts you would have to fetch:

- **Needs you** — every decision waiting on this human: approvals (approve or
  decline in place), questions an agent asked, blocked and recovery items, and
  failed runs with a retry.
- **Happening now** — the runs in flight, with `queued`, `running` or
  `stalled`.
- **Recently** — what happened, newest first, grouped by day.

Tasks are NOT here. The work itself lives on the **Tasks** tab; Inbox answers
"what needs me", Tasks answers "what is the work". `?panel=teams-activity`
still resolves — it opens Inbox at Recently.
```

⚠️ This text reaches containers only after an extended-image build and a `prod.tag` bump. Do not claim it is live on merge.

- [ ] **Step 5: Delete this plan**

`git rm docs/superpowers/plans/2026-09-08-inbox-one-page.md` — shipped plans go in the same PR per CLAUDE.md; the PR body is the durable record.

- [ ] **Step 6: Full suite**

Run: `cd apps/frontend && NODE_OPTIONS='--max-old-space-size=8192' pnpm vitest run`
Expected: every file passes. The branch removes roughly a dozen test files; the total count drops and that is correct.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore(inbox): delete the pill-tab machinery the page no longer uses

The issue list, its toolbar, its keyboard nav, its archive stack and its
per-browser read marks all went with the tabs. Activity's panel is gone;
its feed is a band. The bundled skill describes one page.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3
EOF
)"
```

---

## Dev walk (after merge, before the promotion)

On `dev.flickmeon.ai`, signed in as a dev account with an active subscription:

1. The sidebar shows **Inbox** once and **no Activity item**.
2. `?panel=teams-activity` opens ONE tab titled Inbox, scrolled to Recently.
3. Opening Activity's old link and then the sidebar's Inbox yields exactly ONE dock tab.
4. `?panel=teams-inbox&section=happening-now` scrolls to that band; a bare `?panel=teams-inbox` does not scroll.
5. A legacy `?panel=teams-inbox&tab=approvals` bookmark lands on Needs you.
6. No pill tabs, no search box, no issue rows anywhere on the page.
7. Needs you shows a failed run with Retry and Dismiss if the account has one, and does not also say "nothing needs you".
8. Recently groups under Today, and an actor reads as an agent name, not `agent_…`.
9. Narrow the dock to its minimum width: no horizontal page scroll.

## Self-review notes

- **Spec coverage:** §3's Inbox row → Tasks 2-6; "the needs-attention band and the five pill tabs go" → Tasks 6-7; `teams-activity` opens Recently → Task 2; §4 craft rules → the state vocabulary in Tasks 3-5; §7 alias constraint → Task 2. The spec's Tasks row is NOT in this plan — it is the next PR.
- **Known deviation to declare in the PR body:** "New task" moves from Inbox to Tasks (Task 6), and per-browser unread marks retire with the pill tabs rather than moving, per the amended spec.
- **Not in scope:** moving `IssueRow`/`Kanban*`/`IssueFiltersPopover` out of the `inbox/` folder; the Tasks group-by work; the craft pass's Ask Friday affordance.
