# Home — the dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Home the one dashboard: company name, revenue hero and anchored figures, then the top of each detail page (Needs you · Happening now · Recent work) with a link into it, and remove the ported Paperclip agent telemetry from Home.

**Architecture:** Client-only, no backend change. The revenue hero becomes hover-labelled bars and every tile gains an Ask Friday handoff (spec §2, linked views). `DashboardPanel` is recomposed around three new small section components that each read a hook the product already has (`useAttentionFeed`, `useLiveRuns`, the `/artifacts` read) through one shared `HomeSection`/`HomeRow` pair. The existing `HomeRollupStrip` (hero + tiles) and `HomeQuickActions` stay, with one label and one layout change to the strip. The "Your team" section and its telemetry (`MetricCard` grid, `ActivityCharts`, `ActiveAgentsPanel`, Recent tasks, Recent activity) leave Home; the files stay because `ActivityPanel`, `RunDetailPanel` and the landing demo still import them.

**Tech Stack:** Next.js 16 / React 19, SWR via `useTeamsApi`, vitest + testing-library, Tailwind v4 with the brand tokens in `globals.css`, lucide-react.

**Spec:** `docs/superpowers/specs/2026-09-06-owner-tabs-consolidation-design.md` §3 (Home row), §4, §5.

## Global Constraints

- Work in the worktree `~/dev/wt-tabs-home` (branch `feat/home-dashboard`, base `origin/main` @ `399fd625b`). Every path below is relative to `apps/frontend/` inside it unless it starts with `apps/` or `docs/`.
- Brand tokens only. `node scripts/brand-tokens-scan.mjs src` must print `0`. Use `var(--midnight)`, `var(--midnight-muted)`, `var(--edge)`, `var(--edge-soft)`, `var(--cream-deep)`, `text-warn`. No hex, no Tailwind palette classes, no shadows.
- One state vocabulary: non-row states inside a Home section are one calm italic serif line (the pattern `DashboardPanel` already uses for its lists); whole-strip states stay `PanelNotice` inside `HomeRollupStrip`. Never render a number that was not read ("…" while pending, never `0`).
- Panel key `teams-dashboard` and the registry are untouched. No new dependencies.
- Copy is owner language: "Needs you", "Happening now", "Recent work", "Open Inbox", "Open Tasks", "Open Library". No "issues", "heartbeat", "runs" in visible text.
- Commands run from `apps/frontend/`: tests `NODE_OPTIONS='--max-old-space-size=8192' pnpm vitest run <path>`; types `pnpm exec tsc --noEmit`; lint `pnpm exec eslint <files>`; brand scan `node scripts/brand-tokens-scan.mjs src`.
- Commit after every task with the message given. Do not push; the driver pushes once at the end.

---

## File structure

- Create `src/components/teams/dashboard/HomeSection.tsx` — `HomeSection` (label + right link), `HomeRow` (avatar · title/sub · right), `HomeRowsEmpty` (the one-line state). Shared by the three sections.
- Create `src/components/teams/dashboard/HomeNeedsYou.tsx` — top 3 decisions from `useAttentionFeed`, "Open Inbox · N".
- Create `src/components/teams/dashboard/HomeHappeningNow.tsx` — active runs from `useLiveRuns`, "Open Tasks · N open".
- Create `src/components/teams/dashboard/HomeRecentWork.tsx` — latest 6 artifacts from `/artifacts?groupBy=none&limit=6`, "Open Library".
- Rewrite `src/components/teams/panels/DashboardPanel.tsx` — masthead, strip, quick actions, the three sections. Telemetry gone.
- Modify `src/components/teams/dashboard/HomeRollupStrip.tsx:150,407,426` — hero spans two columns with the tiles stacked beside it; "Signups" becomes "New customers"; the hero's sparkline becomes bars with a hover title per day; every tile gets an Ask Friday link.
- Create `src/hooks/useAskFriday.ts` — the desktop/mobile chat-draft handoff BrainPanel already performs inline, as one hook.
- Modify `apps/infra/openclaw/skills/lightbulb/SKILL.md:104` — the Home row describes the new surface.
- Tests: create `src/components/teams/dashboard/__tests__/HomeNeedsYou.test.tsx`, `HomeHappeningNow.test.tsx`, `HomeRecentWork.test.tsx`; rewrite `src/__tests__/components/teams/panels/DashboardPanel.test.tsx`; modify `src/__tests__/teams/DashboardPanel.test.tsx`, `src/components/teams/panels/__tests__/DashboardPanel.costs.test.tsx`, `src/components/teams/dashboard/__tests__/HomeRollupStrip.test.tsx:138`.

---

### Task 1: `HomeSection`, `HomeRow`, `HomeRowsEmpty` — the shared chrome (S)

**Files:**
- Create: `src/components/teams/dashboard/HomeSection.tsx`
- Test: `src/components/teams/dashboard/__tests__/HomeSection.test.tsx`

**Interfaces:**
- Produces:
  ```ts
  export function HomeSection(props: { label: string; href?: string; linkLabel?: string; children: ReactNode }): JSX.Element
  export function HomeRow(props: { who: string; title: ReactNode; sub?: ReactNode; right?: ReactNode; href: string }): JSX.Element
  export function HomeRowsEmpty(props: { children: ReactNode; role?: "status" | "alert" }): JSX.Element
  ```
  `HomeSection` renders `<section aria-label={label}>` with a `label-caps` header and an optional right-aligned in-shell link (`handleShellLinkClick` + real `href`). `HomeRow` is an anchor row: `Identity` avatar (size `xs`), title, muted sub line, muted right slot. `HomeRowsEmpty` is the single italic serif line every section uses for loading / error / nothing.

- [ ] **Step 1: Write the failing test**

```tsx
// src/components/teams/dashboard/__tests__/HomeSection.test.tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("@/lib/shellNav", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/shellNav")>();
  return { ...mod, handleShellLinkClick: vi.fn() };
});

import { HomeRow, HomeRowsEmpty, HomeSection } from "../HomeSection";
import { handleShellLinkClick } from "@/lib/shellNav";

describe("HomeSection chrome", () => {
  it("names the section, shows the label, and routes the link in-shell while keeping a real href", () => {
    render(
      <HomeSection label="Needs you" href="/chat?panel=teams-inbox" linkLabel="Open Inbox · 3">
        <p>child</p>
      </HomeSection>,
    );
    const section = screen.getByRole("region", { name: "Needs you" });
    expect(within(section).getByText("Needs you")).toBeInTheDocument();
    const link = within(section).getByRole("link", { name: "Open Inbox · 3" });
    expect(link).toHaveAttribute("href", "/chat?panel=teams-inbox");
    fireEvent.click(link);
    expect(handleShellLinkClick).toHaveBeenCalledWith(expect.anything(), "/chat?panel=teams-inbox");
  });

  it("renders a row as one anchor with avatar, title, sub and right slot", () => {
    render(
      <HomeRow who="Friday" title="Hire Pulse as an SDR" sub="Atlas · adds a seat" right="2h ago" href="/chat?panel=teams-inbox" />,
    );
    const row = screen.getByRole("link", { name: /Hire Pulse as an SDR/ });
    expect(row).toHaveAttribute("href", "/chat?panel=teams-inbox");
    expect(within(row).getByText("Atlas · adds a seat")).toBeInTheDocument();
    expect(within(row).getByText("2h ago")).toBeInTheDocument();
    expect(within(row).getByText("F")).toBeInTheDocument(); // Identity initials
  });

  it("renders the empty line as a status by default and as an alert on request", () => {
    const { rerender } = render(<HomeRowsEmpty>Nothing is waiting on you.</HomeRowsEmpty>);
    expect(screen.getByRole("status")).toHaveTextContent("Nothing is waiting on you.");
    rerender(<HomeRowsEmpty role="alert">Couldn't read.</HomeRowsEmpty>);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't read.");
  });
});
```

- [ ] **Step 2: Run it — expect FAIL** (module not found).

`NODE_OPTIONS='--max-old-space-size=8192' pnpm vitest run src/components/teams/dashboard/__tests__/HomeSection.test.tsx`

- [ ] **Step 3: Implement**

```tsx
// src/components/teams/dashboard/HomeSection.tsx
"use client";

// The chrome every Home section shares: a mono-caps label with one right-hand
// link into the detail page, rows that are one anchor each, and a single
// italic line for loading / error / nothing. Three sections use all three, so
// this is the one place their rhythm is decided (system.md: calm when
// healthy — no coloured states here; `HomeRollupStrip` owns the loud ones).

import type { MouseEvent, ReactNode } from "react";
import { Identity } from "@/components/teams/shared/components/Identity";
import { handleShellLinkClick } from "@/lib/shellNav";

export function HomeSection({
  label,
  href,
  linkLabel,
  children,
}: {
  label: string;
  href?: string;
  linkLabel?: string;
  children: ReactNode;
}) {
  return (
    <section aria-label={label} className="flex min-w-0 flex-col">
      <div className="flex items-baseline justify-between gap-3 border-b border-[var(--edge-soft)] pb-1.5">
        <p className="label-caps text-[var(--midnight-muted)]">{label}</p>
        {href && linkLabel && (
          <a
            href={href}
            onClick={(e: MouseEvent<HTMLAnchorElement>) => handleShellLinkClick(e, href)}
            className="text-xs text-[var(--midnight-muted)] underline-offset-4 hover:text-[var(--midnight)] hover:underline"
          >
            {linkLabel}
          </a>
        )}
      </div>
      <div className="flex flex-col">{children}</div>
    </section>
  );
}

export function HomeRow({
  who,
  title,
  sub,
  right,
  href,
}: {
  /** Whose row this is — an agent name for the avatar initials. */
  who: string;
  title: ReactNode;
  sub?: ReactNode;
  right?: ReactNode;
  /** In-shell destination; stays a real href for new-tab / middle-click. */
  href: string;
}) {
  return (
    <a
      href={href}
      onClick={(e: MouseEvent<HTMLAnchorElement>) => handleShellLinkClick(e, href)}
      className="grid grid-cols-[28px_1fr_auto] items-center gap-3 border-b border-[var(--edge-soft)] px-2 py-2.5 no-underline transition-colors last:border-b-0 hover:bg-[var(--cream-deep)]"
      style={{ color: "var(--midnight)" }}
    >
      <Identity name={who} size="xs" />
      <span className="min-w-0">
        <span className="block truncate text-sm">{title}</span>
        {sub && (
          <span className="block truncate text-xs" style={{ color: "var(--midnight-muted)" }}>
            {sub}
          </span>
        )}
      </span>
      {right !== undefined && (
        <span className="shrink-0 text-xs tabular-nums" style={{ color: "var(--midnight-muted)" }}>
          {right}
        </span>
      )}
    </a>
  );
}

export function HomeRowsEmpty({
  children,
  role = "status",
}: {
  children: ReactNode;
  role?: "status" | "alert";
}) {
  return (
    <p
      role={role}
      className="px-2 py-3 font-editorial-serif text-base italic"
      style={{ color: "var(--midnight-muted)" }}
    >
      {children}
    </p>
  );
}
```

- [ ] **Step 4: Run the test — expect PASS.** Then `node scripts/brand-tokens-scan.mjs src` → `0`.

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/dashboard/HomeSection.tsx src/components/teams/dashboard/__tests__/HomeSection.test.tsx
git commit -m "feat(home): the shared section chrome — label, one link, one-anchor rows, one calm empty line"
```

---

### Task 2: `HomeNeedsYou` — the top three decisions (S)

**Files:**
- Create: `src/components/teams/dashboard/HomeNeedsYou.tsx`
- Test: `src/components/teams/dashboard/__tests__/HomeNeedsYou.test.tsx`

**Interfaces:**
- Consumes: `useAttentionFeed()` → `{ items: AttentionItem[]; count: number; isLoading; isError }` (`src/components/teams/inbox/hooks/useAttentionFeed.ts`); `AttentionItem` (`shared/types.ts:1407`: `id, sourceKind, subject{title,identifier}, whyNow, createdAt, relatedIssue{id}|null, originAgentName|null`); `HomeSection`, `HomeRow`, `HomeRowsEmpty` from Task 1; `teamsPanelUrl("inbox")`, `teamsDetailUrl("issue", id)`, `timeAgo`.
- Produces: `export function HomeNeedsYou(): JSX.Element`; pure helpers `needsYouTitle(item)`, `needsYouSub(item)` (exported for tests).

- [ ] **Step 1: Write the failing test**

```tsx
// src/components/teams/dashboard/__tests__/HomeNeedsYou.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("@/components/teams/inbox/hooks/useAttentionFeed", () => ({ useAttentionFeed: vi.fn() }));
vi.mock("@/lib/shellNav", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/shellNav")>();
  return { ...mod, handleShellLinkClick: vi.fn() };
});

import { HomeNeedsYou, needsYouSub, needsYouTitle } from "../HomeNeedsYou";
import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import type { AttentionItem } from "@/components/teams/shared/types";

const item = (over: Partial<AttentionItem>): AttentionItem => ({
  id: "a1",
  sourceKind: "approval",
  subject: { kind: "approval", id: "ap1", title: "Hire Pulse as an SDR", identifier: null, status: null, href: null },
  whyNow: "adds a seat at $20/mo",
  severity: "high",
  activityAt: "2026-09-05T17:40:00Z",
  createdAt: "2026-09-05T17:40:00Z",
  relatedIssue: { kind: "issue", id: "iss1", title: null, identifier: "LUL-10", status: null, href: null },
  originAgentName: "Atlas",
  ...over,
});

function feed(over: Partial<ReturnType<typeof useAttentionFeed>>) {
  vi.mocked(useAttentionFeed).mockReturnValue({
    items: [], count: 0, isLoading: false, isError: false, refresh: vi.fn(), ...over,
  });
}

describe("HomeNeedsYou", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows at most three decisions and the company-wide count on the link", () => {
    feed({ items: [item({ id: "1" }), item({ id: "2" }), item({ id: "3" }), item({ id: "4", subject: { kind: "approval", id: "x", title: "Fourth", identifier: null, status: null, href: null } })], count: 7 });
    render(<HomeNeedsYou />);
    const section = screen.getByRole("region", { name: "Needs you" });
    expect(within(section).getAllByRole("link", { name: /Hire Pulse/ })).toHaveLength(3);
    expect(within(section).queryByText("Fourth")).not.toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Open Inbox · 7" })).toHaveAttribute("href", expect.stringContaining("teams-inbox"));
  });

  it("links a decision to its task when there is one, else to the Inbox", () => {
    feed({ items: [item({ id: "1" }), item({ id: "2", relatedIssue: null })], count: 2 });
    render(<HomeNeedsYou />);
    const rows = screen.getAllByRole("link", { name: /Hire Pulse/ });
    expect(rows[0]).toHaveAttribute("href", expect.stringContaining("iss1"));
    expect(rows[1]).toHaveAttribute("href", expect.stringContaining("teams-inbox"));
  });

  it("says nothing is waiting, calmly, when the feed is empty", () => {
    feed({});
    render(<HomeNeedsYou />);
    expect(screen.getByRole("status")).toHaveTextContent("Nothing is waiting on you.");
    expect(screen.getByRole("link", { name: "Open Inbox" })).toBeInTheDocument();
  });

  it("never renders a count it has not read", () => {
    feed({ isLoading: true });
    render(<HomeNeedsYou />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking what needs you…");
    expect(screen.queryByText(/Open Inbox ·/)).not.toBeInTheDocument();
  });

  it("reports a read failure as unavailable, not as empty", () => {
    feed({ isError: true });
    render(<HomeNeedsYou />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't read what needs you just now.");
  });

  it("titles a row from the subject, falling back to the kind, and builds the sub line", () => {
    expect(needsYouTitle(item({}))).toBe("Hire Pulse as an SDR");
    expect(needsYouTitle(item({ subject: { kind: "x", id: "y", title: "  ", identifier: null, status: null, href: null }, sourceKind: "recovery_action" }))).toBe("Recovery");
    expect(needsYouSub(item({}))).toBe("Atlas · adds a seat at $20/mo");
    expect(needsYouSub(item({ originAgentName: null }))).toBe("adds a seat at $20/mo");
  });
});
```

- [ ] **Step 2: Run — expect FAIL** (module not found).

- [ ] **Step 3: Implement**

```tsx
// src/components/teams/dashboard/HomeNeedsYou.tsx
"use client";

// Home's "Needs you": the top three decisions waiting on this human, read from
// the same server-ranked feed the Inbox badge uses (useAttentionFeed), so Home
// and Inbox can never disagree about the count. Rows link to the task when
// there is one; the section link opens the Inbox, where the actions live.

import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { AttentionItem } from "@/components/teams/shared/types";
import { teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { HomeRow, HomeRowsEmpty, HomeSection } from "./HomeSection";

const LIMIT = 3;

// Same words AttentionList uses, so a decision is called the same thing on
// Home and in the Inbox.
const SOURCE_LABEL: Record<string, string> = {
  approval: "Approval",
  issue_thread_interaction: "Question",
  decision: "Decision",
  join_request: "Join request",
  review: "Review",
  blocker_attention: "Blocked",
  recovery_action: "Recovery",
};

export function needsYouTitle(item: AttentionItem): string {
  const title = item.subject.title?.trim();
  return title || SOURCE_LABEL[item.sourceKind] || "Needs you";
}

export function needsYouSub(item: AttentionItem): string {
  return [item.originAgentName, item.whyNow].filter(Boolean).join(" · ");
}

export function HomeNeedsYou() {
  const { items, count, isLoading, isError } = useAttentionFeed();
  const inbox = teamsPanelUrl("inbox");
  const settled = !isLoading || items.length > 0;

  return (
    <HomeSection
      label="Needs you"
      href={inbox}
      linkLabel={settled && count > 0 ? `Open Inbox · ${count}` : "Open Inbox"}
    >
      {!settled ? (
        <HomeRowsEmpty>Checking what needs you…</HomeRowsEmpty>
      ) : isError && items.length === 0 ? (
        <HomeRowsEmpty role="alert">Couldn&apos;t read what needs you just now.</HomeRowsEmpty>
      ) : items.length === 0 ? (
        <HomeRowsEmpty>Nothing is waiting on you.</HomeRowsEmpty>
      ) : (
        items.slice(0, LIMIT).map((item) => (
          <HomeRow
            key={item.id}
            who={item.originAgentName ?? "Your team"}
            title={needsYouTitle(item)}
            sub={needsYouSub(item)}
            right={timeAgo(item.createdAt)}
            href={item.relatedIssue?.id ? teamsDetailUrl("issue", item.relatedIssue.id) : inbox}
          />
        ))
      )}
    </HomeSection>
  );
}
```

- [ ] **Step 4: Run — expect PASS.** `pnpm exec tsc --noEmit` clean.

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/dashboard/HomeNeedsYou.tsx src/components/teams/dashboard/__tests__/HomeNeedsYou.test.tsx
git commit -m "feat(home): Needs you — the top three decisions, counted by the same feed as the Inbox"
```

---

### Task 3: `HomeHappeningNow` — what agents are doing right now (S)

**Files:**
- Create: `src/components/teams/dashboard/HomeHappeningNow.tsx`
- Test: `src/components/teams/dashboard/__tests__/HomeHappeningNow.test.tsx`

**Interfaces:**
- Consumes: `useLiveRuns()` → `{ runs: LiveRun[]; isLoading; isError }` and `isRunActive`, `liveRunActivity`, `liveRunTitle` from `src/components/teams/dashboard/hooks/useLiveRuns.ts`; `LiveRun` (`shared/lib/liveRuns.ts:38`: `id, status, agentName, issueId, startedAt, createdAt, nextAction, outputSilence`); `Issue` (`shared/types.ts`, has `id`, `title`); Task 1 chrome.
- Produces: `export function HomeHappeningNow(props: { issuesById: Map<string, Issue>; openTasks: number | undefined }): JSX.Element`. `openTasks === undefined` means not read yet (link says just "Open Tasks").

- [ ] **Step 1: Write the failing test**

```tsx
// src/components/teams/dashboard/__tests__/HomeHappeningNow.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/components/teams/dashboard/hooks/useLiveRuns")>();
  return { ...mod, useLiveRuns: vi.fn() };
});
vi.mock("@/lib/shellNav", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/shellNav")>();
  return { ...mod, handleShellLinkClick: vi.fn() };
});

import { HomeHappeningNow } from "../HomeHappeningNow";
import { useLiveRuns, type LiveRun } from "@/components/teams/dashboard/hooks/useLiveRuns";
import type { Issue } from "@/components/teams/shared/types";

const run = (over: Partial<LiveRun>): LiveRun => ({
  id: "r1", status: "running", agentName: "Friday", issueId: "iss1",
  startedAt: new Date(Date.now() - 4 * 60_000).toISOString(), nextAction: "Drafting the stockist email", ...over,
});
const issues = new Map<string, Issue>([["iss1", { id: "iss1", title: "Launch fall collection" } as Issue]]);

function runs(over: Partial<ReturnType<typeof useLiveRuns>>) {
  vi.mocked(useLiveRuns).mockReturnValue({ runs: [], isLoading: false, isError: false, ...over });
}

describe("HomeHappeningNow", () => {
  beforeEach(() => vi.clearAllMocks());

  it("lists only active runs, names the task, and shows what the agent is doing", () => {
    runs({ runs: [run({}), run({ id: "r2", status: "completed" })] });
    render(<HomeHappeningNow issuesById={issues} openTasks={12} />);
    const section = screen.getByRole("region", { name: "Happening now" });
    const rows = within(section).getAllByRole("link", { name: /Launch fall collection/ });
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText(/Running — Drafting the stockist email/)).toBeInTheDocument();
    expect(within(rows[0]).getByText(/4m ago|4 min/)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Open Tasks · 12 open" })).toHaveAttribute("href", expect.stringContaining("teams-issues"));
  });

  it("marks a stalled run in a word, not only a colour", () => {
    runs({ runs: [run({ outputSilence: { level: "critical", silenceAgeMs: 600_000 } as LiveRun["outputSilence"] })] });
    render(<HomeHappeningNow issuesById={issues} openTasks={1} />);
    expect(screen.getByText("stalled")).toBeInTheDocument();
  });

  it("falls back to the agent's own words when the task is unknown", () => {
    runs({ runs: [run({ issueId: "missing" })] });
    render(<HomeHappeningNow issuesById={issues} openTasks={0} />);
    expect(screen.getByRole("link", { name: /Drafting the stockist email/ })).toBeInTheDocument();
  });

  it("caps at four rows", () => {
    runs({ runs: [1, 2, 3, 4, 5].map((n) => run({ id: `r${n}` })) });
    render(<HomeHappeningNow issuesById={issues} openTasks={5} />);
    expect(screen.getAllByRole("link", { name: /Launch fall collection/ })).toHaveLength(4);
  });

  it("is calm when nobody is running, and does not print an unread open count", () => {
    runs({});
    render(<HomeHappeningNow issuesById={issues} openTasks={undefined} />);
    expect(screen.getByRole("status")).toHaveTextContent("Nobody is running right now.");
    expect(screen.getByRole("link", { name: "Open Tasks" })).toBeInTheDocument();
  });

  it("reports a feed failure as unavailable", () => {
    runs({ isError: true });
    render(<HomeHappeningNow issuesById={issues} openTasks={3} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't see what's running just now.");
  });
});
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**

```tsx
// src/components/teams/dashboard/HomeHappeningNow.tsx
"use client";

// Home's "Happening now": the live view of the team. One row per active run —
// who, on which task, doing what — read from the same /inbox/live-runs feed
// the Inbox and Tasks indicators use. This is the daily job the org chart was
// doing; the org chart itself moves into the Agents panel.

import {
  isRunActive,
  liveRunActivity,
  liveRunTitle,
  useLiveRuns,
  type LiveRun,
} from "@/components/teams/dashboard/hooks/useLiveRuns";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { Issue } from "@/components/teams/shared/types";
import { teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { HomeRow, HomeRowsEmpty, HomeSection } from "./HomeSection";

const LIMIT = 4;

function rowTitle(run: LiveRun, issuesById: Map<string, Issue>): string {
  const issue = run.issueId ? issuesById.get(run.issueId) : undefined;
  return issue?.title || run.nextAction?.trim() || "Working";
}

export function HomeHappeningNow({
  issuesById,
  openTasks,
}: {
  issuesById: Map<string, Issue>;
  /** `/dashboard.tasks.open`; `undefined` while that request is in flight. */
  openTasks: number | undefined;
}) {
  const { runs, isLoading, isError } = useLiveRuns();
  const active = runs.filter(isRunActive).slice(0, LIMIT);
  const tasks = teamsPanelUrl("issues");

  return (
    <HomeSection
      label="Happening now"
      href={tasks}
      linkLabel={openTasks === undefined ? "Open Tasks" : `Open Tasks · ${openTasks} open`}
    >
      {isLoading && runs.length === 0 ? (
        <HomeRowsEmpty>Looking at what&apos;s running…</HomeRowsEmpty>
      ) : isError && runs.length === 0 ? (
        <HomeRowsEmpty role="alert">Couldn&apos;t see what&apos;s running just now.</HomeRowsEmpty>
      ) : active.length === 0 ? (
        <HomeRowsEmpty>Nobody is running right now.</HomeRowsEmpty>
      ) : (
        active.map((run) => {
          const started = run.startedAt ?? run.createdAt;
          const stalled = liveRunActivity(run) === "stalled";
          return (
            <HomeRow
              key={run.id}
              who={run.agentName ?? "Agent"}
              title={rowTitle(run, issuesById)}
              sub={`${liveRunTitle(run)}${started ? ` · ${timeAgo(started)}` : ""}`}
              right={stalled ? <span className="text-warn">stalled</span> : "running"}
              href={run.issueId ? teamsDetailUrl("issue", run.issueId) : tasks}
            />
          );
        })
      )}
    </HomeSection>
  );
}
```

- [ ] **Step 4: Run — expect PASS.** If the `4m ago` assertion fails on `timeAgo`'s exact wording, read `src/components/teams/shared/lib/timeAgo.ts` and adjust the regex in the test to its actual "minutes" form — the test asserts elapsed time is shown, not its spelling.

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/dashboard/HomeHappeningNow.tsx src/components/teams/dashboard/__tests__/HomeHappeningNow.test.tsx
git commit -m "feat(home): Happening now — the live view of the team, one row per active run"
```

---

### Task 4: `HomeRecentWork` — the latest deliverables (S)

**Files:**
- Create: `src/components/teams/dashboard/HomeRecentWork.tsx`
- Test: `src/components/teams/dashboard/__tests__/HomeRecentWork.test.tsx`

**Interfaces:**
- Consumes: `useTeamsApi().read<CompanyArtifactsResponse>(path)` (returns SWR-shaped `{ data, error, isLoading }`); `CompanyArtifactsResponse { artifacts: CompanyArtifact[] }` and `CompanyArtifact { id, title, issue: { id, title }, createdByAgent?: { name } | null, updatedAt }` (`shared/types.ts:715-755`); Task 1 chrome.
- Produces: `export function HomeRecentWork(): JSX.Element`; `export const RECENT_WORK_PATH = "/artifacts?groupBy=none&limit=6"`.

- [ ] **Step 1: Write the failing test**

```tsx
// src/components/teams/dashboard/__tests__/HomeRecentWork.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("@/lib/shellNav", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/shellNav")>();
  return { ...mod, handleShellLinkClick: vi.fn() };
});

import { HomeRecentWork, RECENT_WORK_PATH } from "../HomeRecentWork";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const artifact = (n: number) => ({
  id: `a${n}`, source: "document", mediaKind: "document", title: `Weekly digest ${n}`,
  issue: { id: `iss${n}`, identifier: `LUL-${n}`, title: "Nightly CEO progress" },
  createdByAgent: { id: "ag1", name: "Friday" }, updatedAt: "2026-09-05T08:10:00Z", href: `/x/${n}`,
});

function mockRead(result: { data?: unknown; error?: unknown; isLoading?: boolean }) {
  const read = vi.fn(() => ({ data: result.data, error: result.error ?? null, isLoading: result.isLoading ?? false, mutate: vi.fn(), isValidating: false }));
  vi.mocked(useTeamsApi).mockReturnValue({ read } as unknown as ReturnType<typeof useTeamsApi>);
  return read;
}

describe("HomeRecentWork", () => {
  beforeEach(() => vi.clearAllMocks());

  it("reads the six newest deliverables and links each to its task", () => {
    const read = mockRead({ data: { artifacts: [artifact(1), artifact(2)], nextCursor: null } });
    render(<HomeRecentWork />);
    expect(read).toHaveBeenCalledWith(RECENT_WORK_PATH);
    const section = screen.getByRole("region", { name: "Recent work" });
    const row = within(section).getByRole("link", { name: /Weekly digest 1/ });
    expect(row).toHaveAttribute("href", expect.stringContaining("iss1"));
    expect(within(row).getByText(/Friday · Nightly CEO progress/)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Open Library" })).toHaveAttribute("href", expect.stringContaining("teams-artifacts"));
  });

  it("is calm when nothing has been made yet", () => {
    mockRead({ data: { artifacts: [], nextCursor: null } });
    render(<HomeRecentWork />);
    expect(screen.getByRole("status")).toHaveTextContent("Nothing made yet. The first deliverable your agents finish lands here.");
  });

  it("never shows stale rows over a failed read", () => {
    mockRead({ data: { artifacts: [artifact(1)], nextCursor: null }, error: new Error("boom") });
    render(<HomeRecentWork />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't read the Library just now.");
    expect(screen.queryByText(/Weekly digest/)).not.toBeInTheDocument();
  });

  it("shows a loading line, not an empty verdict, while in flight", () => {
    mockRead({ isLoading: true });
    render(<HomeRecentWork />);
    expect(screen.getByRole("status")).toHaveTextContent("Gathering recent work…");
  });
});
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**

```tsx
// src/components/teams/dashboard/HomeRecentWork.tsx
"use client";

// Home's "Recent work": the six newest deliverables from the Library, each
// linked to the task that produced it. Same route the Library reads
// (ArtifactsPanel RECENT_PATH) with a smaller limit; a separate SWR key, so
// opening Home does not pull the Library's hundred-row page.
//
// A failed read is never rendered as rows (ArtifactsPanel's rule): with SWR's
// retained data an error on revalidation would otherwise list yesterday's
// deliverables as if they were fresh.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyArtifactsResponse } from "@/components/teams/shared/types";
import { teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { HomeRow, HomeRowsEmpty, HomeSection } from "./HomeSection";

export const RECENT_WORK_PATH = "/artifacts?groupBy=none&limit=6";

export function HomeRecentWork() {
  const { read } = useTeamsApi();
  const q = read<CompanyArtifactsResponse>(RECENT_WORK_PATH);
  const artifacts = q.error ? [] : (q.data?.artifacts ?? []);

  return (
    <HomeSection label="Recent work" href={teamsPanelUrl("artifacts")} linkLabel="Open Library">
      {q.isLoading && !q.data ? (
        <HomeRowsEmpty>Gathering recent work…</HomeRowsEmpty>
      ) : q.error ? (
        <HomeRowsEmpty role="alert">Couldn&apos;t read the Library just now.</HomeRowsEmpty>
      ) : artifacts.length === 0 ? (
        <HomeRowsEmpty>Nothing made yet. The first deliverable your agents finish lands here.</HomeRowsEmpty>
      ) : (
        artifacts.map((a) => (
          <HomeRow
            key={a.id}
            who={a.createdByAgent?.name ?? "Your team"}
            title={a.title}
            sub={[a.createdByAgent?.name, a.issue.title].filter(Boolean).join(" · ")}
            right={timeAgo(a.updatedAt)}
            href={teamsDetailUrl("issue", a.issue.id)}
          />
        ))
      )}
    </HomeSection>
  );
}
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/dashboard/HomeRecentWork.tsx src/components/teams/dashboard/__tests__/HomeRecentWork.test.tsx
git commit -m "feat(home): Recent work — the six newest deliverables, each linked to its task"
```

---

### Task 5: Recompose `DashboardPanel` and retire the telemetry from Home (M)

**Files:**
- Rewrite: `src/components/teams/panels/DashboardPanel.tsx`
- Rewrite: `src/__tests__/components/teams/panels/DashboardPanel.test.tsx`
- Modify: `src/__tests__/teams/DashboardPanel.test.tsx:77-96`
- Modify: `src/components/teams/panels/__tests__/DashboardPanel.costs.test.tsx:10-16` (mocks)

**Interfaces:**
- Consumes: Tasks 2–4 components; `HomeRollupStrip({ spendCents })`, `HomeQuickActions({ stripeBound })`, `useCompanyRollup()`, `useTeamsApi().read` for `/dashboard`, `/issues`, `/settings`; `DashboardResponse`, `Issue` types.
- Produces: `export function DashboardPanel(): JSX.Element` (same export, same panel key).

- [ ] **Step 1: Rewrite the panel**

Replace the whole file with:

```tsx
"use client";

// apps/frontend/src/components/teams/panels/DashboardPanel.tsx
//
// Home is the dashboard: the company's name, the business hero and its
// anchored figures (HomeRollupStrip), the verbs (HomeQuickActions), then the
// top of each detail page — what needs the owner, what the team is doing now,
// what it made — each with one link into Inbox, Tasks or Library.
//
// The Paperclip dashboard port that used to sit below (run charts, metric
// cards, recent tasks, recent activity) is gone from Home: those answer "are
// my agents busy", and Inbox / Tasks own that question now. `/dashboard` is
// still read for one thing, the Metronome-billed spend the strip prints, and
// its ECS-probing slowness is why the strip takes `undefined` while it loads.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { PanelMasthead } from "@/components/teams/shell";
import { HomeRollupStrip } from "@/components/teams/dashboard/HomeRollupStrip";
import { HomeQuickActions } from "@/components/teams/dashboard/HomeQuickActions";
import { HomeNeedsYou } from "@/components/teams/dashboard/HomeNeedsYou";
import { HomeHappeningNow } from "@/components/teams/dashboard/HomeHappeningNow";
import { HomeRecentWork } from "@/components/teams/dashboard/HomeRecentWork";
import { useCompanyRollup } from "@/hooks/useCompanyRollup";
import type { DashboardResponse, Issue } from "@/components/teams/shared/types";

/** Normalize the BFF /issues envelope (array | {items} | {issues}) to Issue[]. */
function normalizeIssues(data: unknown): Issue[] {
  if (Array.isArray(data)) return data as Issue[];
  if (data && typeof data === "object") {
    const obj = data as { items?: unknown; issues?: unknown };
    if (Array.isArray(obj.items)) return obj.items as Issue[];
    if (Array.isArray(obj.issues)) return obj.issues as Issue[];
  }
  return [];
}

export function DashboardPanel() {
  const { read } = useTeamsApi();
  const dashboardQuery = read<DashboardResponse>("/dashboard");
  // Task titles for the Happening-now rows (a run carries only an issue id).
  const issuesQuery = read<unknown>("/issues");
  // Paperclip company record; `name` is the editable company name. Falls back
  // to "Home" while loading — PanelMasthead hides the overline when it equals
  // the title, so the fallback reads as a plain "Home" masthead.
  const settingsQuery = read<{ name?: string }>("/settings");
  const companyName = settingsQuery.data?.name?.trim() || "Home";
  // Same SWR key the strip reads at its default span — one request, not two.
  const { rollup } = useCompanyRollup();

  const dashboardLoading = dashboardQuery.isLoading && !dashboardQuery.data;
  const dashboard = dashboardQuery.data?.dashboard;
  const issuesById = new Map(normalizeIssues(issuesQuery.data).map((i) => [i.id, i]));

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-5 max-w-[1600px] mx-auto w-full">
      <PanelMasthead overline="Home" title={companyName} />

      {/* Three states, not two: `undefined` while /dashboard is in flight keeps
          the strip from rendering an all-zero verdict; `null` is the settled
          "the backend omitted `costs`". */}
      <HomeRollupStrip
        spendCents={dashboardLoading ? undefined : (dashboard?.costs?.monthSpendCents ?? null)}
      />
      <HomeQuickActions stripeBound={!!rollup?.stripe_bound} />

      {/* Container query, not viewport: this renders inside a dock pane. */}
      <div className="grid grid-cols-1 gap-6 @3xl:grid-cols-2">
        <HomeNeedsYou />
        <div className="flex flex-col gap-6">
          <HomeHappeningNow
            issuesById={issuesById}
            openTasks={dashboardLoading ? undefined : (dashboard?.tasks.open ?? 0)}
          />
          <HomeRecentWork />
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Replace `src/__tests__/components/teams/panels/DashboardPanel.test.tsx`** with:

```tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/hooks/useCompanyRollup", () => ({
  useCompanyRollup: () => ({
    rollup: undefined, windowStart: "2026-08-03", span: 30, error: undefined, isLoading: true,
    refresh: vi.fn(), isUnavailable: () => false,
  }),
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => ({ data: undefined }),
}));
vi.mock("@/components/teams/dashboard/HomeQuickActions", () => ({
  HomeQuickActions: () => <section aria-label="Quick actions" />,
}));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("@/components/teams/inbox/hooks/useAttentionFeed", () => ({
  useAttentionFeed: () => ({ items: [], count: 0, isLoading: false, isError: false, refresh: vi.fn() }),
}));
vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/components/teams/dashboard/hooks/useLiveRuns")>();
  return { ...mod, useLiveRuns: () => ({ runs: [], isLoading: false, isError: false }) };
});

import { DashboardPanel } from "@/components/teams/panels/DashboardPanel";
import { useTeamsApi } from "@/hooks/useTeamsApi";

function mockRead(perPath: Record<string, { data?: unknown; isLoading?: boolean; error?: Error | null }>) {
  const read = vi.fn((path: string) => {
    const key = Object.keys(perPath).find((k) => path === k || path.startsWith(`${k}?`));
    const r = key ? perPath[key] : {};
    return { data: r.data, isLoading: r.isLoading ?? false, error: r.error ?? null, mutate: vi.fn(), isValidating: false };
  });
  vi.mocked(useTeamsApi).mockReturnValue({ read } as unknown as ReturnType<typeof useTeamsApi>);
  return read;
}

describe("DashboardPanel — Home is the dashboard", () => {
  beforeEach(() => vi.clearAllMocks());

  it("puts the company name in the H1 and the three detail sections under the business strip", () => {
    mockRead({
      "/settings": { data: { name: "Northwind Candle Co." } },
      "/dashboard": { data: { dashboard: { agents: {}, tasks: { open: 12 }, runActivity: [], pendingApprovals: 0, budgets: { pendingApprovals: 0 } } } },
      "/issues": { data: [] },
      "/artifacts": { data: { artifacts: [], nextCursor: null } },
    });
    render(<DashboardPanel />);
    expect(screen.getByRole("heading", { level: 1, name: "Northwind Candle Co." })).toBeInTheDocument();
    const strip = screen.getByRole("region", { name: "Business summary" });
    const needs = screen.getByRole("region", { name: "Needs you" });
    const now = screen.getByRole("region", { name: "Happening now" });
    const work = screen.getByRole("region", { name: "Recent work" });
    // Order: strip → quick actions → sections.
    const before = (a: Element, b: Element) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(before(strip, screen.getByLabelText("Quick actions"))).toBe(true);
    expect(before(screen.getByLabelText("Quick actions"), needs)).toBe(true);
    expect(before(needs, now)).toBe(true);
    expect(before(now, work)).toBe(true);
    expect(screen.getByRole("link", { name: "Open Tasks · 12 open" })).toBeInTheDocument();
  });

  it("no longer renders the agent telemetry", () => {
    mockRead({ "/settings": { data: { name: "Phomo" } }, "/dashboard": { data: { dashboard: { agents: { running: 2 }, tasks: { open: 3 }, runActivity: [], pendingApprovals: 1, budgets: { pendingApprovals: 0 } } } } });
    render(<DashboardPanel />);
    for (const gone of ["Your team", "Key metrics", "Run Activity", "Success Rate", "Recent Tasks", "Recent activity", "Pending Approvals"]) {
      expect(screen.queryByText(gone)).not.toBeInTheDocument();
      expect(screen.queryByLabelText(gone)).not.toBeInTheDocument();
    }
  });

  it("does not print an open-task count while /dashboard is still loading", () => {
    mockRead({ "/settings": { data: {} }, "/dashboard": { isLoading: true } });
    render(<DashboardPanel />);
    expect(screen.getByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Tasks" })).toBeInTheDocument();
    expect(screen.queryByText(/open$/)).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Update `src/__tests__/teams/DashboardPanel.test.tsx`.** Add the two mocks above (`useAttentionFeed`, `useLiveRuns`) after the existing `vi.mock` block, then replace the body of the single `it` (lines 78–96) with:

```tsx
  it("renders the company name, the quick actions and the three sections", () => {
    render(<DashboardPanel />);
    expect(screen.getByRole("heading", { level: 1, name: "Phomo" })).toBeInTheDocument();
    expect(screen.getByText("Home")).toBeInTheDocument();
    expect(screen.getByLabelText("Quick actions")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Needs you" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Happening now" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recent work" })).toBeInTheDocument();
    expect(screen.queryByText("Your team")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Key metrics")).not.toBeInTheDocument();
  });
```

If that file's `useTeamsApi` mock (lines 41–57) returns a fixed object for every path, leave it: the three sections tolerate `undefined` data.

- [ ] **Step 4: Update `DashboardPanel.costs.test.tsx`.** Replace the `useActivityFeed` mock (lines 11–13) with the `useAttentionFeed` mock from Step 2, and change the `useLiveRuns` mock to the `importOriginal` form from Step 2 so `isRunActive` and friends stay real. Keep every assertion — the spend tile is unchanged.

- [ ] **Step 5: Run the four Home test files + tsc + lint + scan**

```bash
NODE_OPTIONS='--max-old-space-size=8192' pnpm vitest run src/__tests__/components/teams/panels/DashboardPanel.test.tsx src/__tests__/teams/DashboardPanel.test.tsx src/components/teams/panels/__tests__/DashboardPanel.costs.test.tsx src/components/teams/dashboard
pnpm exec tsc --noEmit
pnpm exec eslint src/components/teams/panels/DashboardPanel.tsx src/components/teams/dashboard
node scripts/brand-tokens-scan.mjs src
```
Expected: all pass, tsc clean, eslint clean (no unused imports — `MetricCard`, `ActivityCharts`, `ActiveAgentsPanel`, `ActivityRow`, `useActivityFeed`, `PanelSection`, `Link`, `useState`, lucide icons are all gone from the panel), scan `0`.

- [ ] **Step 6: Commit**

```bash
git add src/components/teams/panels/DashboardPanel.tsx src/__tests__/components/teams/panels/DashboardPanel.test.tsx src/__tests__/teams/DashboardPanel.test.tsx src/components/teams/panels/__tests__/DashboardPanel.costs.test.tsx
git commit -m "feat(home): Home is the dashboard — needs you, happening now, recent work; the agent telemetry leaves"
```

---

### Task 6: The strip — tiles beside the hero, bars you can hover, Ask on every tile (M)

**Files:**
- Create: `src/hooks/useAskFriday.ts`
- Test: `src/hooks/__tests__/useAskFriday.test.tsx`
- Modify: `src/components/teams/dashboard/HomeRollupStrip.tsx:150,407,426-433` and the `Tile` / `Sparkline` block (lines 60-197)
- Modify: `src/components/teams/dashboard/__tests__/HomeRollupStrip.test.tsx:138,143-166`

**Interfaces:**
- Consumes: `openChatPanelWithDraft(draft: string): boolean` (`src/components/workspace/workspaceController.ts:265`); `requestFocusComposer(agentId, draft)` (`src/lib/focusComposerRequest.ts`); `useAgents().defaultId`; `navigateShell` (`src/lib/shellNav.ts`); `notifyError` (`src/lib/notify.tsx`); `RollupDay`, `formatUsdCents`, `microcentsToCents`, `formatDay` already in the strip.
- Produces: `export function useAskFriday(): (draft: string) => void`; `Tile` gains `chart?: ReactNode` (rendered instead of `series` when given) and `askDraft?: string` (renders an "Ask Friday" link that calls the hook); `HeroBars({ days, format }: { days: RollupDay[]; format: (cents: number) => string })` inside the strip.

- [ ] **Step 1: Write the hook test**

```tsx
// src/hooks/__tests__/useAskFriday.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

vi.mock("@/components/workspace/workspaceController", () => ({ openChatPanelWithDraft: vi.fn() }));
vi.mock("@/lib/focusComposerRequest", () => ({ requestFocusComposer: vi.fn() }));
vi.mock("@/lib/shellNav", () => ({ navigateShell: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyError: vi.fn() }));
vi.mock("@/hooks/useAgents", () => ({ useAgents: vi.fn() }));

import { useAskFriday } from "../useAskFriday";
import { openChatPanelWithDraft } from "@/components/workspace/workspaceController";
import { requestFocusComposer } from "@/lib/focusComposerRequest";
import { navigateShell } from "@/lib/shellNav";
import { notifyError } from "@/lib/notify";
import { useAgents } from "@/hooks/useAgents";

describe("useAskFriday", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useAgents).mockReturnValue({ defaultId: "friday" } as unknown as ReturnType<typeof useAgents>);
  });

  it("opens the dock chat with the draft when the desktop shell takes it", () => {
    vi.mocked(openChatPanelWithDraft).mockReturnValue(true);
    const { result } = renderHook(() => useAskFriday());
    result.current("Why did revenue drop?");
    expect(openChatPanelWithDraft).toHaveBeenCalledWith("Why did revenue drop?");
    expect(navigateShell).not.toHaveBeenCalled();
  });

  it("falls back to the mobile shell: records the draft, selects the agent, swaps the URL", () => {
    vi.mocked(openChatPanelWithDraft).mockReturnValue(false);
    const dispatched: string[] = [];
    const spy = vi.spyOn(window, "dispatchEvent").mockImplementation((e) => { dispatched.push(e.type); return true; });
    const { result } = renderHook(() => useAskFriday());
    result.current("Who are our new customers?");
    expect(requestFocusComposer).toHaveBeenCalledWith("friday", "Who are our new customers?");
    expect(dispatched).toEqual(["selectAgent", "getstarted:focus-composer"]);
    expect(navigateShell).toHaveBeenCalledWith("/chat");
    spy.mockRestore();
  });

  it("says Friday is not ready when there is no default agent and no dock", () => {
    vi.mocked(openChatPanelWithDraft).mockReturnValue(false);
    vi.mocked(useAgents).mockReturnValue({ defaultId: null } as unknown as ReturnType<typeof useAgents>);
    const { result } = renderHook(() => useAskFriday());
    result.current("anything");
    expect(notifyError).toHaveBeenCalledWith("Friday isn't ready yet — try again in a moment.");
    expect(navigateShell).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run — expect FAIL** (module not found).

- [ ] **Step 3: Implement the hook**

```ts
// src/hooks/useAskFriday.ts
"use client";

// One "Ask Friday" for every surface that hands the owner's question to the
// CEO agent with the composer pre-filled. Desktop: the dock opens Friday's
// chat with the draft. Mobile (no dock; `openChatPanelWithDraft` returns
// false): record the draft, name the agent the mobile shell should select,
// swap the URL to /chat, then fire the event for the already-mounted composer.
// Lifted verbatim from BrainPanel's `ask` so the two cannot drift.

import { useCallback } from "react";
import { useAgents } from "@/hooks/useAgents";
import { openChatPanelWithDraft } from "@/components/workspace/workspaceController";
import { requestFocusComposer } from "@/lib/focusComposerRequest";
import { notifyError } from "@/lib/notify";
import { navigateShell } from "@/lib/shellNav";

export function useAskFriday(): (draft: string) => void {
  const { defaultId } = useAgents();
  return useCallback(
    (draft: string) => {
      if (openChatPanelWithDraft(draft)) return;
      if (!defaultId) {
        notifyError("Friday isn't ready yet — try again in a moment.");
        return;
      }
      requestFocusComposer(defaultId, draft);
      window.dispatchEvent(
        new CustomEvent("selectAgent", { detail: { agentId: defaultId, source: "notification" } }),
      );
      navigateShell("/chat");
      window.dispatchEvent(
        new CustomEvent("getstarted:focus-composer", { detail: { agentId: defaultId } }),
      );
    },
    [defaultId],
  );
}
```

- [ ] **Step 4: Run the hook test — expect PASS.** Commit: `git add src/hooks/useAskFriday.ts src/hooks/__tests__/useAskFriday.test.tsx && git commit -m "feat(chat): useAskFriday — the one draft handoff, desktop dock or mobile shell"`

- [ ] **Step 5: Update the strip tests first.** In `HomeRollupStrip.test.tsx`:
  - Add at the top, beside the other mocks: `vi.mock("@/hooks/useAskFriday", () => ({ useAskFriday: () => askFriday }));` with `const askFriday = vi.fn();` declared above it (hoisting: use `vi.hoisted(() => ({ askFriday: vi.fn() }))` if vitest complains).
  - Line 138: `"Signups over the period"` → `"New customers over the period"`.
  - Lines 143–166, the negative-day test: replace the `polyline` assertions with rects:
    ```tsx
    const rects = Array.from(container.querySelectorAll('[aria-label="Revenue over the period"] rect'));
    expect(rects.length).toBe(2);
    for (const r of rects) {
      const y = Number(r.getAttribute("y"));
      const h = Number(r.getAttribute("height"));
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y + h).toBeLessThanOrEqual(20.01);
    }
    ```
  - Add to the `business-first hero` describe:
    ```tsx
    it("draws the hero as bars with the exact day and amount on hover, and keeps sparklines on the tiles", () => {
      mockRollup.mockReturnValue(view({ rollup: LIVE }));
      const { container } = render(<HomeRollupStrip spendCents={1400} />);
      const hero = container.querySelector('[aria-label="Revenue over the period"]')!;
      const titles = Array.from(hero.querySelectorAll("rect > title")).map((t) => t.textContent);
      expect(titles).toEqual(["Aug 30 · $10", "Aug 31 · $20"]);
      expect(container.querySelector('[aria-label="New customers over the period"] polyline')).not.toBeNull();
    });

    it("calls the customer tile New customers and lets the hero span two columns beside the tiles", () => {
      mockRollup.mockReturnValue(view({ rollup: LIVE }));
      render(<HomeRollupStrip spendCents={1400} />);
      expect(screen.getByRole("group", { name: "New customers" })).toBeInTheDocument();
      expect(screen.queryByRole("group", { name: "Signups" })).not.toBeInTheDocument();
      const hero = screen.getByRole("group", { name: "Revenue" });
      expect(hero.className).toContain("@3xl:row-span-3");
      expect(hero.className).not.toContain("col-span-3");
    });

    it("hands each tile's question to Friday with the period in it", () => {
      mockRollup.mockReturnValue(view({ rollup: LIVE }));
      render(<HomeRollupStrip spendCents={1400} />);
      const revenue = screen.getByRole("group", { name: "Revenue" });
      fireEvent.click(within(revenue).getByRole("button", { name: "Ask Friday" }));
      expect(askFriday).toHaveBeenCalledWith(expect.stringMatching(/revenue.*30 days/));
      fireEvent.click(within(screen.getByRole("group", { name: "Agent spend" })).getByRole("button", { name: "Ask Friday" }));
      expect(askFriday).toHaveBeenLastCalledWith(expect.stringMatching(/agents cost/));
    });
    ```
  (`LIVE` in this file has two days, 2026-08-30 at 1,000,000 microcents and 2026-08-31 at 2,000,000 — confirm at lines 11–55 and adjust the expected titles to that fixture's days and amounts if it differs.)

- [ ] **Step 6: Run — expect FAIL** on the new assertions.

- [ ] **Step 7: Implement in `HomeRollupStrip.tsx`**

  1. Import `useAskFriday` from `@/hooks/useAskFriday` and `type { RollupDay }` is already imported.
  2. Add `HeroBars` below `Sparkline`:
  ```tsx
  /**
   * The hero's chart: one bar per day with the exact day and amount in a
   * native <title>, so hovering answers "what was that day" without a chart
   * library (spec §2, linked views). Negative days (refund-heavy) hang below a
   * hairline baseline; the last bar is midnight, the rest the neutral chart
   * token — one accent, and it means "today".
   */
  function HeroBars({ days, format }: { days: RollupDay[]; format: (cents: number) => string }) {
    const values = days.map((d) => (Number.isFinite(d.revenue_microcents) ? d.revenue_microcents : 0));
    if (values.length === 0) return null;
    const lo = Math.min(...values, 0);
    const hi = Math.max(...values, 0);
    const range = hi - lo || 1;
    const zeroY = 20 - ((0 - lo) / range) * 20;
    const slot = 100 / values.length;
    const gap = slot * 0.25;
    return (
      <svg viewBox="0 0 100 20" preserveAspectRatio="none" role="img" aria-label="Revenue over the period" className="mt-3 h-12 w-full">
        {values.map((v, i) => {
          const h = (Math.abs(v) / range) * 20;
          const y = v >= 0 ? zeroY - h : zeroY;
          return (
            <rect
              key={days[i].day}
              x={(i * slot + gap / 2).toFixed(2)}
              y={y.toFixed(2)}
              width={(slot - gap).toFixed(2)}
              height={Math.max(h, 0.2).toFixed(2)}
              fill={i === values.length - 1 ? "var(--midnight)" : "var(--chart-5)"}
            >
              <title>{`${formatDay(days[i].day)} · ${format(microcentsToCents(v))}`}</title>
            </rect>
          );
        })}
        <line x1="0" x2="100" y1={zeroY.toFixed(2)} y2={zeroY.toFixed(2)} stroke="var(--edge)" strokeWidth="0.5" vectorEffect="non-scaling-stroke" />
      </svg>
    );
  }
  ```
  3. `Tile`: add props `chart?: ReactNode` and `askDraft?: string`. Render `chart ?? (series && !unavailable && <Sparkline …/>)`. Below the existing link, render when `askDraft`:
  ```tsx
  {askDraft && (
    <button
      type="button"
      onClick={() => ask(askDraft)}
      className="mt-2.5 ml-4 inline-block label-caps text-[var(--midnight-muted)] underline-offset-4 hover:text-[var(--midnight)] hover:underline"
    >
      Ask Friday
    </button>
  )}
  ```
  where `const ask = useAskFriday();` is called at the top of `Tile` (a component, so the hook is legal there).
  4. Line 150: hero class `@3xl:col-span-3` → `@3xl:row-span-3`. Line 407: grid → `"grid grid-cols-1 gap-2 @3xl:grid-cols-[2fr_1fr]"`.
  5. Revenue tile: replace `series={days.map((d) => d.revenue_microcents)}` with `chart={<HeroBars days={days} format={formatSignedUsdCents} />}`; add `askDraft={`Walk me through revenue for the last ${span} days: what landed, what was refunded, and what changed versus the previous ${span} days.`}`.
  6. Signups tile: `label="New customers"`, caption `Stripe customers created · ${comparison(...)}`, `askDraft={`Who are our new customers from the last ${span} days, and what did they buy?`}`.
  7. Visitors tile: `askDraft={isUnavailable("visitors") ? undefined : `How is traffic to our site trending over the last ${span} days, and where is it coming from?`}`.
  8. Agent spend tile: `askDraft="Break down what my agents cost this month by agent and by task, and tell me what is driving it."`.

- [ ] **Step 8: Run the strip test, tsc, eslint, brand scan — expect all clean.** The `unavailable` Revenue case must still render "Unavailable" with no chart: `chart` is only passed when the tile renders values, so guard `chart` with `!unavailable` in `Tile` too.

- [ ] **Step 9: Commit**

```bash
git add src/components/teams/dashboard/HomeRollupStrip.tsx src/components/teams/dashboard/__tests__/HomeRollupStrip.test.tsx
git commit -m "feat(home): hover-labelled revenue bars beside the anchored tiles, and Ask Friday on every figure"
```

---

### Task 7: Tell the agents what Home is now (S)

**Files:**
- Modify: `apps/infra/openclaw/skills/lightbulb/SKILL.md:104`

- [ ] **Step 1: Read line 104** (the `| Home | … |` row of the surfaces table) and keep its first two cells verbatim. Replace the third cell's text with:

> The business first: the company name, a **Revenue** hero with a 7d / 30d / 90d toggle and a comparison to the period before, beside **New customers**, **Visitors** and **Agent-spend** tiles; quick actions; then the top three things that **need the owner** (link: Inbox), what agents are **doing right now** (link: Tasks) and the latest **work in the Library**. There is no agent telemetry on Home any more — for run history and tasks point the owner at Tasks and Inbox; for money detail, Money.

Also check lines 357–359 (the "Home shows the same Stripe numbers as a summary — revenue, signups, visitors" paragraph): change "signups" to "new customers".

- [ ] **Step 2: Run the skill tests** (they assert copy anchors): from `apps/backend`, `uv run pytest tests/unit -k skill -q`. Expected: pass. If a test pins the old Home row text, update that assertion in the same commit.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/openclaw/skills/lightbulb/SKILL.md
git commit -m "docs(skill): Home is the dashboard — what the owner sees there now"
```

Note for the PR body: this file reaches running containers only after an image rebuild and a `prod.tag` bump; it does not ship with the frontend deploy.

---

### Task 8: Dev walk before prod (S) — after merge, dev auto-deploys

Through Claude-in-Chrome on `https://dev.flickmeon.ai/chat?panel=teams-dashboard`, signed in as a dev account whose company has at least one pending approval and one artifact:

- [ ] H1 is the company name; overline "Home".
- [ ] The revenue hero spans two columns with New customers / Visitors / Agent spend stacked beside it at desktop width; one column in a narrow dock. Hovering a bar shows the day and amount; the last bar is darker.
- [ ] Ask Friday on the Revenue tile opens Friday's chat with the revenue question pre-filled (desktop); on the mobile shell it lands on /chat with the composer filled.
- [ ] Quick actions row present; "Add a product" only when Stripe is bound.
- [ ] Needs you shows ≤3 rows; "Open Inbox · N" matches the Inbox badge count; a row opens its task.
- [ ] Happening now shows active runs only; while nothing runs it says so calmly; "Open Tasks · N open" opens Tasks.
- [ ] Recent work shows ≤6 deliverables; a row opens the task; "Open Library" opens Library.
- [ ] Nothing from the old telemetry renders (no "Your team", no charts, no metric cards).
- [ ] Cold-start: with the container asleep, the strip shows "…" for spend and the sections show their loading lines, never zeros.
- [ ] Brand scan `0`, `tsc` clean, full frontend suite green in CI.

---

## Out of scope (and why)

- Moving quick actions into the masthead `actions` slot: a layout nicety, not part of the consolidation; the chip row is already tested.
- Clicking a hero bar to open Money scoped to that day: Money as one tab does not exist yet; the `day` param lands with the Money PR and the bars gain their click then.
- Deleting `MetricCard`, `ActivityCharts`, `ActiveAgentsPanel`, `ActivityRow`, `useActivityFeed`: still imported by `ActivityPanel`, `RunDetailPanel` and the landing `WorkspaceDemo`. They go with the Inbox PR (Activity) and a later landing pass.
- Any backend change; issue #2285 (stepper hides read-only panels).

## Sizing

Tasks 1–4 S each, Tasks 5–6 M, Task 7 S, Task 8 S. One PR.
