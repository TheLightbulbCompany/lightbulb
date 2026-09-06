# Money — the ledger — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the four Money tabs (Payments · Products · Customers · Spend) with one **Money** tab: a ledger page whose masthead carries the month's landed revenue, whose sections are Payments, Customers, What you sell and Agent spend, and which accepts a `day` so a bar on Home's revenue hero opens that day's payments.

**Architecture:** Client-only except nothing — the backend already serves `start`/`end` on `/company/payments`. One page component (`MoneyPanel`) composes four section components over the hooks the old tabs used (`useCompanyMoney`, `useSpend`, `useBilling`, `useCompanyRollup`). The four old panel keys stay registered and open Money scrolled to their section; a new `money` key is the sidebar's one item. `day` rides the same durable-param grammar Brain's `path` uses. The Home hero's bars become anchors into Money.

**Tech Stack:** Next.js 16 / React 19, SWR via `useApi`, dockview panel params, vitest + testing-library, Tailwind v4 brand tokens.

**Spec:** `docs/superpowers/specs/2026-09-06-owner-tabs-consolidation-design.md` §2 (no sub-tabs; anchors; linked views), §3 Money row, §4 craft rules, §5 data rules.

## Global Constraints

- Worktree `~/dev/wt-tabs-money` (branch `feat/money-ledger`, base `origin/main` @ `84ada5a8f`). Paths are relative to `apps/frontend/` unless they start with `apps/` or `docs/`.
- **No sub-tabs.** Sections down one page, each headed by a `label-caps` label and one right-hand line (a count, a scope, or a link). Views are never a second tab strip.
- **Nothing prints a number it cannot source.** Stripe lists are cursor pages: "Show more", never "Show all N". Month-to-date revenue and the biggest day come from `/company/rollup` rows only. No total customer count.
- Brand tokens only (`node scripts/brand-tokens-scan.mjs src` prints `0`); status words carry colour, fills never do. Owner language in visible copy (no "issues", "runs", "charges" as nouns — "payments").
- Panel keys `teams-payments`, `teams-products`, `teams-customers`, `teams-spend` remain registered and resolve (mobile builds them as strings). `?panel=usage` keeps opening the control Usage panel.
- Commands from `apps/frontend/`: tests `NODE_OPTIONS='--max-old-space-size=8192' pnpm vitest run <paths>`; types `pnpm exec tsc --noEmit`; lint `pnpm exec eslint <files>`; scan `node scripts/brand-tokens-scan.mjs src`.
- Every commit message ends with a blank line and:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01FpmibcEJqdm1xNGY9QDiy3`
  Do not push; the controller pushes once.

---

## File structure

- Modify `src/hooks/useCompanyMoney.ts` — optional `{ start, end }` window on the read key.
- Modify `src/components/workspace/panelRegistry.ts` — `money` list key; `day` durable param for `teams-money`; the four legacy keys retitled "Money".
- Modify `src/components/workspace/panels/TeamsDockPanel.tsx`, `src/components/chat/InlineTeamsPanel.tsx`, `src/components/teams/TeamsPanelRouter.tsx`, `src/app/chat/page.tsx` — forward `day`; widen the durable-param key union; route the five keys to `MoneyPanel`.
- Create `src/components/teams/panels/money/MoneySection.tsx` — section chrome (label · right slot · body).
- Create `src/components/teams/panels/money/PaymentsSection.tsx`, `CustomersSection.tsx`, `ProductsSection.tsx` — the three lists as sections (columns lifted from the old panels).
- Create `src/components/teams/panels/money/AgentSpendSection.tsx` — the Spend headline + by-model rows / plan windows, lifted from `SpendPanel` and `UsagePanel`.
- Modify `src/components/teams/panels/money/money.ts` — `formatMicrocents`, `monthToDate`, `biggestDay`, `formatIsoDay`.
- Rewrite `src/components/teams/panels/MoneyPanel.tsx` (new file at this path; the old scaffold `money/MoneyPanel.tsx` is deleted) — the page.
- Delete `src/components/teams/panels/PaymentsPanel.tsx`, `ProductsPanel.tsx`, `CustomersPanel.tsx`, `SpendPanel.tsx`, `money/MoneyPanel.tsx`, `panels/__tests__/SpendPanel.test.tsx`.
- Modify `src/components/chat/companySections.ts` + `src/components/chat/__tests__/CompanyNav.test.tsx` — one Money item.
- Modify `src/components/teams/dashboard/HomeRollupStrip.tsx` + its test — hero bars link to Money by day.
- Modify `apps/infra/openclaw/skills/lightbulb/SKILL.md:344-372` — "Where the owner sees the money".
- Delete `docs/superpowers/plans/2026-09-06-home-dashboard.md` (shipped in #2343).
- Tests: `src/hooks/__tests__/useCompanyMoney.window.test.tsx` (new), `src/components/workspace/__tests__/panelRegistry.test.ts`, `TeamsDockPanel.test.tsx`, `spendDeepLink.test.tsx` (rewritten), `src/components/teams/panels/money/__tests__/AgentSpendSection.test.tsx` (new, ports SpendPanel's headline tests), `src/components/teams/panels/__tests__/MoneyPanel.test.tsx` (new), `src/components/teams/dashboard/__tests__/HomeRollupStrip.test.tsx`.

---

### Task 1: `useCompanyMoney` takes a day window (S)

**Files:**
- Modify: `src/hooks/useCompanyMoney.ts:118-160` (the `useCompanyMoney` function)
- Test: `src/hooks/__tests__/useCompanyMoney.window.test.tsx`

**Interfaces:**
- Produces: `useCompanyMoney<Row>(kind: MoneyKind, window?: { start: string; end: string })` — same return shape (`{ rows, view, hasMore, next }`). With a window the SWR key is `/company/${kind}?start=${start}&end=${end}` (`&starting_after=` appended on paging). Changing the window resets accumulated pages.

- [ ] **Step 1: Write the failing test**

```tsx
// src/hooks/__tests__/useCompanyMoney.window.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const swrKeys: string[] = [];
vi.mock("swr", () => ({
  default: (key: string) => {
    swrKeys.push(key);
    return {
      data: { stripe_bound: true, data: [{ id: `row-${swrKeys.length}` }], has_more: true, next_cursor: "cur_1" },
      error: undefined,
      isLoading: false,
    };
  },
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn() }) }));

import { useCompanyMoney } from "../useCompanyMoney";

describe("useCompanyMoney — day window", () => {
  beforeEach(() => { swrKeys.length = 0; });

  it("reads the bare list without a window, exactly as before", () => {
    renderHook(() => useCompanyMoney("payments"));
    expect(swrKeys[0]).toBe("/company/payments");
  });

  it("puts start and end on the key when a window is given", () => {
    renderHook(() => useCompanyMoney("payments", { start: "2026-09-05", end: "2026-09-05" }));
    expect(swrKeys[0]).toBe("/company/payments?start=2026-09-05&end=2026-09-05");
  });

  it("pages inside the window and resets the pages when the window changes", () => {
    const { result, rerender } = renderHook(
      ({ w }: { w?: { start: string; end: string } }) => useCompanyMoney("payments", w),
      { initialProps: { w: { start: "2026-09-05", end: "2026-09-05" } } },
    );
    act(() => result.current.next());
    expect(swrKeys.at(-1)).toBe("/company/payments?start=2026-09-05&end=2026-09-05&starting_after=cur_1");
    expect(result.current.rows.length).toBe(2);
    rerender({ w: { start: "2026-09-06", end: "2026-09-06" } });
    expect(swrKeys.at(-1)).toBe("/company/payments?start=2026-09-06&end=2026-09-06");
    expect(result.current.rows.length).toBe(1);
  });
});
```

- [ ] **Step 2: Run — expect FAIL** (the second and third tests: key has no window; rows do not reset).

- [ ] **Step 3: Implement.** Replace the `useCompanyMoney` function body (keep `moneyView` and the types above it untouched):

```ts
export interface MoneyWindow {
  /** Inclusive UTC days, YYYY-MM-DD — the shape /company/payments takes. */
  start: string;
  end: string;
}

export function useCompanyMoney<Row>(kind: MoneyKind, window?: MoneyWindow) {
  const api = useApi();
  const [cursor, setCursor] = useState<string | null>(null);
  const [earlier, setEarlier] = useState<Row[]>([]);
  // A window change is a different list: drop the pages accumulated under
  // the old one, or Sept 5's rows would sit above Sept 6's under a Sept 6
  // heading.
  const windowKey = window ? `${window.start}..${window.end}` : "";
  const [seenWindow, setSeenWindow] = useState(windowKey);
  if (seenWindow !== windowKey) {
    setSeenWindow(windowKey);
    setCursor(null);
    setEarlier([]);
  }

  const base = window
    ? `/company/${kind}?start=${window.start}&end=${window.end}`
    : `/company/${kind}`;
  const key = cursor ? `${base}${window ? "&" : "?"}starting_after=${cursor}` : base;
  const { data, error, isLoading } = useSWR<MoneyEnvelope<Row>>(
    key,
    (path: string) => api.get(path) as Promise<MoneyEnvelope<Row>>,
    { dedupingInterval: 30_000, keepPreviousData: true },
  );

  const next = useCallback(() => {
    if (!data?.next_cursor) return;
    setEarlier((prev) => [...prev, ...data.data]);
    setCursor(data.next_cursor);
  }, [data]);

  const rows = [...earlier, ...(data?.data ?? [])];
  const view = moneyView(data, error, isLoading);

  return {
    rows,
    view: rows.length && view.kind !== "forbidden" ? ({ kind: "rows" } as MoneyView) : view,
    hasMore: !!data?.has_more,
    next,
  };
}
```
Keep the existing comments that explain `earlier`/`keepPreviousData`/the rows-win rule above the corresponding lines (move them, do not drop them).

- [ ] **Step 4: Run the new test and the existing MoneyStates test — expect PASS.** `pnpm exec tsc --noEmit` clean.

- [ ] **Step 5: Commit** `feat(money): useCompanyMoney reads a day window`

---

### Task 2: The `money` key and the `day` durable param (M)

**Files:**
- Modify: `src/components/workspace/panelRegistry.ts:43-70` (`TEAMS_LIST_KEYS`), `:175-200` (teams branch of `describePanel`), `:207-235` (`durableParamsFor`, `clearablePanelParamKeys`)
- Modify: `src/components/workspace/panels/TeamsDockPanel.tsx:17-24,50-60`
- Modify: `src/components/chat/InlineTeamsPanel.tsx:40`
- Modify: `src/components/teams/TeamsPanelRouter.tsx` (`TeamsPanelRouterProps.onUpdateDurableParam`)
- Modify: `src/app/chat/page.tsx:334-342`
- Test: `src/components/workspace/__tests__/panelRegistry.test.ts`, `src/components/workspace/__tests__/TeamsDockPanel.test.tsx`

**Interfaces:**
- Produces: `TEAMS_LIST_KEYS.money = "Money"` (the four absorbed keys keep their titles until Task 6, which retitles them in the same commit as the sidebar change — the registry's label-parity test compares the two). `describePanel("teams-money", { day })` → params `{ teamsKey: "money", day }`. `durableParamsFor("teams-money")` → `["panel", "day"]`; `clearablePanelParamKeys("teams-money")` → `["day"]`. `panelParams.day` reaches `TeamsPanelRouter` on desktop and mobile. `onUpdateDurableParam` key union is `"agent" | "path" | "day"` in all three files.

- [ ] **Step 1: Registry tests first.** In `panelRegistry.test.ts` add:

```ts
  it("describes the money panel with a durable ?day deep link", () => {
    expect(describePanel("teams-money", { day: "2026-09-05" })).toEqual({
      panelId: "teams-money", component: "teams", title: "Money", params: { teamsKey: "money", day: "2026-09-05" },
    });
    expect(describePanel("teams-money", {})!.params).toEqual({ teamsKey: "money" });
    expect(durableParamsFor("teams-money")).toEqual(["panel", "day"]);
    expect(clearablePanelParamKeys("teams-money")).toEqual(["day"]);
  });
```
In `TeamsDockPanel.test.tsx`, beside the Brain `path` tests, add:
```ts
  it("forwards the money `day` param through to TeamsPanelRouter", () => {
    const { props } = makePanelProps("teams-money", { teamsKey: "money", day: "2026-09-05" });
    render(<TeamsDockPanel {...props} />);
    expect((lastRouterProps().panelParams as Params).day).toBe("2026-09-05");
  });
```
(Use this file's existing `makePanelProps` / `lastRouterProps` helpers exactly as the Brain tests do.)

- [ ] **Step 2: Run both files — expect FAIL.**

- [ ] **Step 3: Implement.**

`panelRegistry.ts`:
- In `TEAMS_LIST_KEYS`: add `money: "Money",` before `spend`. Leave the four absorbed keys' titles alone here (Task 6 retitles them).
- In the teams branch of `describePanel`, after the brain `path` line: `if (teamsKey === "money" && params.day) p.day = params.day;` with the comment `// ?day scopes the Payments section to one UTC day — the hero bar on Home links here.`
- `durableParamsFor`: add `if (panelId === "teams-money") return ["panel", "day"];` after the brain line. `clearablePanelParamKeys`: add `if (panelId === "teams-money") return ["day"];`.

`TeamsDockPanel.tsx`: add `day?: string; // durable money day scope` to `TeamsDockPanelParams` and `...(params.day ? { day: params.day } : {}),` in the `panelParams` object.

`TeamsPanelRouter.tsx` and `InlineTeamsPanel.tsx`: change the `onUpdateDurableParam` type to `(key: "agent" | "path" | "day", value: string | null) => void`.

`src/app/chat/page.tsx`: find where `pathParam` is read from the URL (search the file for `pathParam`), add `dayParam` the same way from the `day` search param, and add `...(dayParam ? { day: dayParam } : {}),` to the `panelParams` object at line ~337. If `updateDurableParam` in that file narrows its key type, widen it to include `"day"`.

- [ ] **Step 4: Run** `panelRegistry.test.ts`, `TeamsDockPanel.test.tsx`, and `src/app/chat` tests if any exist (`ls src/app/chat/__tests__ 2>/dev/null`); `pnpm exec tsc --noEmit`. Expect PASS / clean.

- [ ] **Step 5: Commit** `feat(money): the money panel key, and a durable day scope that reaches the panel on desktop and mobile`

---

### Task 3: Section chrome and the three Stripe lists (M)

**Files:**
- Create: `src/components/teams/panels/money/MoneySection.tsx`
- Create: `src/components/teams/panels/money/PaymentsSection.tsx`, `CustomersSection.tsx`, `ProductsSection.tsx`
- Modify: `src/components/teams/panels/money/money.ts` (add `formatIsoDay`)
- Test: `src/components/teams/panels/money/__tests__/sections.test.tsx`

**Interfaces:**
- Consumes: `useCompanyMoney` (Task 1), `PanelDataTable`/`PanelDataColumn`/`EditorialButton` from `@/components/teams/shell`, `formatStripeAmount`/`formatUnixDay` from `./money`, the column definitions from the old `PaymentsPanel.tsx:20-77`, `CustomersPanel.tsx:19-62`, `ProductsPanel.tsx:20-48` (copy them verbatim, including their comments).
- Produces:
  ```ts
  export function MoneySection({ id, label, right, children }: { id: string; label: string; right?: ReactNode; children: ReactNode }): JSX.Element  // <section id={`money-${id}`} aria-label={label}>
  export function PaymentsSection({ day, onDayCleared }: { day: string | null; onDayCleared: () => void }): JSX.Element
  export function CustomersSection({ newIn30Days }: { newIn30Days: number | null }): JSX.Element
  export function ProductsSection({ stripeBound }: { stripeBound: boolean }): JSX.Element
  export function formatIsoDay(day: string): string   // "2026-09-05" → "Sep 5, 2026" (UTC-pinned)
  ```
  Each list section renders: the header, then `PanelDataTable` with its rows; while the page-level view is not `rows`/`zero` the page does not mount these sections at all (Task 5 gates them), so inside a section the only states are loading (skeleton rows), rows, zero (one calm italic line), and "Show more".

- [ ] **Step 1: Write the failing test**

```tsx
// src/components/teams/panels/money/__tests__/sections.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

const hook = vi.hoisted(() => ({ impl: vi.fn() }));
vi.mock("@/hooks/useCompanyMoney", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/hooks/useCompanyMoney")>();
  return { ...mod, useCompanyMoney: (...a: unknown[]) => hook.impl(...a) };
});

import { PaymentsSection } from "../PaymentsSection";
import { CustomersSection } from "../CustomersSection";
import { ProductsSection } from "../ProductsSection";
import { formatIsoDay } from "../money";

const rowsView = (rows: unknown[], hasMore = false) => ({ rows, view: { kind: rows.length ? "rows" : "zero" }, hasMore, next: vi.fn() });

describe("Money sections", () => {
  beforeEach(() => vi.clearAllMocks());

  it("Payments: newest first, receipt links, Show more only when Stripe says there is more", () => {
    const next = vi.fn();
    hook.impl.mockReturnValue({ ...rowsView([
      { id: "ch_1", amount: 8900, amount_refunded: 0, currency: "usd", created: 1788600000, status: "succeeded", paid: true, refunded: false, disputed: false, description: "Fall Candle Trio", receipt_url: "https://r/1", customer: null },
    ], true), next });
    render(<PaymentsSection day={null} onDayCleared={vi.fn()} />);
    const section = screen.getByRole("region", { name: "Payments" });
    expect(within(section).getByText("Fall Candle Trio")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Receipt" })).toHaveAttribute("href", "https://r/1");
    fireEvent.click(within(section).getByRole("button", { name: "Show more" }));
    expect(next).toHaveBeenCalled();
    expect(hook.impl).toHaveBeenCalledWith("payments", undefined);
  });

  it("Payments scoped to a day: names the day, reads that window, and offers the way back", () => {
    const cleared = vi.fn();
    hook.impl.mockReturnValue(rowsView([]));
    render(<PaymentsSection day="2026-09-05" onDayCleared={cleared} />);
    expect(hook.impl).toHaveBeenCalledWith("payments", { start: "2026-09-05", end: "2026-09-05" });
    expect(screen.getByText("Payments on Sep 5, 2026")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("No payments on Sep 5, 2026.");
    fireEvent.click(screen.getByRole("button", { name: "Show all payments" }));
    expect(cleared).toHaveBeenCalled();
  });

  it("Customers: shows the 30-day count only when it was read, and never a total", () => {
    hook.impl.mockReturnValue(rowsView([{ id: "cus_1", email: "h@x.com", name: "Hannah Reyes", created: 1788600000, currency: "usd", delinquent: false, balance: 0 }]));
    const { rerender } = render(<CustomersSection newIn30Days={6} />);
    expect(screen.getByText("6 new in 30 days")).toBeInTheDocument();
    expect(screen.queryByText(/Show all/)).toBeNull();
    rerender(<CustomersSection newIn30Days={null} />);
    expect(screen.queryByText(/new in 30 days/)).toBeNull();
  });

  it("Products: is titled What you sell and offers Add a product only when Stripe is bound", () => {
    hook.impl.mockReturnValue(rowsView([{ id: "prod_1", name: "Gift set", active: true, description: null, created: 1, default_price: { id: "p", unit_amount: 12000, currency: "usd" } }]));
    const { rerender } = render(<ProductsSection stripeBound />);
    expect(screen.getByRole("region", { name: "What you sell" })).toBeInTheDocument();
    expect(screen.getByText("$120.00")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Add a product/ })).toHaveAttribute("href", "https://dashboard.stripe.com/products/create");
    rerender(<ProductsSection stripeBound={false} />);
    expect(screen.queryByRole("link", { name: /Add a product/ })).toBeNull();
  });

  it("empty lists say so in one calm line", () => {
    hook.impl.mockReturnValue(rowsView([]));
    render(<CustomersSection newIn30Days={0} />);
    expect(screen.getByRole("status")).toHaveTextContent("No customers yet.");
  });

  it("formats an ISO day in UTC", () => {
    expect(formatIsoDay("2026-09-05")).toBe("Sep 5, 2026");
  });
});
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.**

`money.ts` — append:
```ts
/** "2026-09-05" → "Sep 5, 2026", pinned to UTC like formatUnixDay. */
export function formatIsoDay(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", {
    month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
  });
}
```

`MoneySection.tsx`:
```tsx
"use client";

// The chrome every Money section shares: a mono-caps label with ONE right-hand
// line (a count, a scope, or a link), then the body. Sections, not sub-tabs
// (spec §2): the shell already spends the tab metaphor on open panels.

import type { ReactNode } from "react";

export function MoneySection({ id, label, right, children }: { id: string; label: string; right?: ReactNode; children: ReactNode }) {
  return (
    <section id={`money-${id}`} aria-label={label} className="flex min-w-0 flex-col gap-2 scroll-mt-4">
      <div className="flex items-baseline justify-between gap-3 border-b border-[var(--edge-soft)] pb-1.5">
        <p className="label-caps text-[var(--midnight-muted)]">{label}</p>
        {right && <div className="flex items-center gap-3 text-xs text-[var(--midnight-muted)]">{right}</div>}
      </div>
      {children}
    </section>
  );
}

/** The one calm line for an empty list. */
export function MoneyEmptyLine({ children }: { children: ReactNode }) {
  return (
    <p role="status" className="px-2 py-3 font-editorial-serif text-base italic" style={{ color: "var(--midnight-muted)" }}>
      {children}
    </p>
  );
}
```

`PaymentsSection.tsx` — the `COLUMNS` and `statusOf` from `PaymentsPanel.tsx:20-77` copied verbatim (comments included), then:
```tsx
export function PaymentsSection({ day, onDayCleared }: { day: string | null; onDayCleared: () => void }) {
  const window = day ? { start: day, end: day } : undefined;
  const { rows, view, hasMore, next } = useCompanyMoney<PaymentRow>("payments", window);
  const rowKey = (r: PaymentRow) => r.id;
  const label = day ? `Payments on ${formatIsoDay(day)}` : "Payments";
  return (
    <MoneySection
      id="payments"
      label={label}
      right={
        day ? (
          <button type="button" onClick={onDayCleared} className="underline-offset-4 hover:text-[var(--midnight)] hover:underline">Show all payments</button>
        ) : (
          <span>newest first</span>
        )
      }
    >
      {view.kind === "loading" ? (
        <PanelDataTable rows={[]} columns={COLUMNS} rowKey={rowKey} loading />
      ) : rows.length === 0 ? (
        <MoneyEmptyLine>{day ? `No payments on ${formatIsoDay(day)}.` : "No payments yet — the first one shows up here as soon as it lands."}</MoneyEmptyLine>
      ) : (
        <>
          <PanelDataTable rows={rows} columns={COLUMNS} rowKey={rowKey} />
          {hasMore && (
            <div className="flex justify-center">
              <EditorialButton size="sm" variant="secondary" onClick={next}>Show more</EditorialButton>
            </div>
          )}
        </>
      )}
    </MoneySection>
  );
}
```
`CustomersSection.tsx` — `COLUMNS` from `CustomersPanel.tsx:19-62` verbatim; right slot = `newIn30Days === null ? undefined : <span>{newIn30Days} new in 30 days</span>`; empty line "No customers yet."; same loading / rows / Show more shape as Payments (no `day`).
`ProductsSection.tsx` — `priceOf` + `COLUMNS` from `ProductsPanel.tsx:20-48` verbatim; label `"What you sell"`; right slot = `stripeBound ? <a href="https://dashboard.stripe.com/products/create" target="_blank" rel="noopener noreferrer" className="underline-offset-4 hover:text-[var(--midnight)] hover:underline">Add a product ↗</a> : undefined` (the same URL `HomeQuickActions.tsx` uses); empty line "Nothing listed yet."; loading / rows / Show more as above.

- [ ] **Step 4: Run — expect PASS.** tsc clean, scan 0.

- [ ] **Step 5: Commit** `feat(money): Payments, Customers and What you sell as sections of one page`

---

### Task 4: `AgentSpendSection` — the Spend headline as a section (M)

**Files:**
- Create: `src/components/teams/panels/money/AgentSpendSection.tsx`
- Test: `src/components/teams/panels/money/__tests__/AgentSpendSection.test.tsx`
- Delete (in Task 5, not here): `panels/SpendPanel.tsx`, `panels/__tests__/SpendPanel.test.tsx`

**Interfaces:**
- Consumes: `spendHeadline` and `periodMonthName` — move them verbatim from `SpendPanel.tsx:56-160` into this file (export `spendHeadline`); `useSpend`, `useBilling`, `useSWR("/billing/usage")` exactly as `SpendPanel.tsx:162-172` does; `formatUsdCents`; `SpendResponse`/`SpendByModel`/`SpendQuotaWindow` from `@/hooks/useSpend`.
- Produces: `export function AgentSpendSection(): JSX.Element`; `export function spendHeadline(...)` (same signature as before).

- [ ] **Step 1: Port the tests.** Create `AgentSpendSection.test.tsx` by copying `panels/__tests__/SpendPanel.test.tsx` and changing: the import to `import { AgentSpendSection, spendHeadline } from "../AgentSpendSection";`, every `render(<SpendPanel />)` to `render(<AgentSpendSection />)`, and drop the two masthead tests ("renders the Money / Spend masthead…", "composes UsagePanel's body…") — the section has no masthead and no longer embeds `UsagePanel`. Replace them with:

```tsx
describe("AgentSpendSection chrome", () => {
  it("is a section labelled Agent spend that names the period and links to Model & Credits", () => {
    setup({ spend: { mode: "bedrock", billed_month_to_date_cents: 1400, by_model: [{ model: "Claude Opus 5", tier: "opus", cents: 1000 }, { model: "Claude Sonnet 5", tier: "sonnet", cents: 400 }], period_start: null, plan: null, windows: null } });
    const section = screen.getByRole("region", { name: "Agent spend" });
    expect(within(section).getByText("$14")).toBeInTheDocument();
    expect(within(section).getByText("Claude Opus 5")).toBeInTheDocument();
    expect(within(section).getByText("$10")).toBeInTheDocument();
    expect(within(section).getByText(/2026-08|August/)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Manage credits" })).toHaveAttribute("href", "/chat?panel=llm");
    expect(within(section).queryByText(/Cache/)).toBeNull(); // the token grid does not come along
  });

  it("shows the ChatGPT plan windows for an OAuth owner", () => {
    setup({ spend: { mode: "oauth_quota", billed_month_to_date_cents: null, by_model: null, period_start: null, plan: "Plus", windows: [{ label: "5h", used_percent: 42, resets_at_ms: null }] } });
    expect(screen.getByText("Included")).toBeInTheDocument();
    expect(screen.getByText(/5h limit/)).toBeInTheDocument();
    expect(screen.getByText(/42% used/)).toBeInTheDocument();
  });
});
```
Keep every other ported test as is (headline honesty, partial chips, empty state, month boundary) — they assert copy that moves unchanged.

- [ ] **Step 2: Run — expect FAIL** (module not found).

- [ ] **Step 3: Implement.** `AgentSpendSection.tsx`: the header comment from `SpendPanel.tsx:3-46` (WHICH FIGURES ARE REAL) moved here, then `spendHeadline` + `periodMonthName` verbatim, then:

```tsx
export function AgentSpendSection() {
  const { account, fetchUsage } = useBilling();
  const { spend } = useSpend();
  const { data: usage, isLoading } = useSWR<UsageSummary | null>("/billing/usage", () => fetchUsage(), { dedupingInterval: 30000 });

  const headline = spendHeadline(spend, account?.provider_choice, account?.subscription_status);
  const billedCents = spend?.mode === "bedrock" ? (spend.billed_month_to_date_cents ?? 0) : 0;
  const nothingThisPeriod = !isLoading && (!usage || usage.request_count === 0) && billedCents === 0;
  const ranInAnEarlierPeriod = (usage?.lifetime_spend ?? 0) > 0;
  const monthName = periodMonthName(usage?.period);

  const right = (
    <>
      {usage?.period && <span>{usage.period}</span>}
      <Link href="/chat?panel=llm" className="underline-offset-4 hover:text-[var(--midnight)] hover:underline">Manage credits</Link>
    </>
  );

  return (
    <MoneySection id="spend" label="Agent spend" right={right}>
      {nothingThisPeriod ? (
        <MoneyEmptyLine>
          {ranInAnEarlierPeriod && monthName
            ? `No agent runs yet in ${monthName}. Your agents ran in an earlier period; ${monthName} counts from zero.`
            : "No agent runs this period. Nothing has been billed because nothing has run yet."}
        </MoneyEmptyLine>
      ) : (
        <div className="flex flex-col gap-3 px-2 py-2">
          {headline && (
            <div data-testid="spend-headline" className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="label-caps">This period</p>
                  {headline.partial && <span className="label-caps rounded-sm border border-[var(--edge)] px-1.5 py-0.5 text-[var(--midnight-muted)]">Partial</span>}
                </div>
                <p className={headline.money ? "font-editorial-serif text-3xl leading-none tabular-nums text-[var(--midnight)]" : "font-editorial-serif text-xl italic leading-tight text-[var(--midnight)]"}>{headline.figure}</p>
                <p className="mt-1 max-w-[52ch] text-xs text-[var(--midnight-muted)]">{headline.caption}</p>
              </div>
              {usage && (
                <div className="text-right">
                  <p className="label-caps">Agent runs</p>
                  <p className="font-mono text-title tabular-nums text-[var(--midnight)]">{usage.request_count.toLocaleString()}</p>
                </div>
              )}
            </div>
          )}
          {spend?.mode === "bedrock" && (spend.by_model ?? []).length > 0 && (
            <dl className="flex flex-col divide-y divide-[var(--edge-soft)] border-t border-[var(--edge-soft)]">
              {(spend.by_model ?? []).map((row) => (
                <div key={row.tier} className="flex items-center justify-between py-1.5 text-sm">
                  <dt>{row.model}</dt>
                  <dd className="font-mono text-xs tabular-nums text-[var(--midnight-muted)]">{formatUsdCents(row.cents)}</dd>
                </div>
              ))}
            </dl>
          )}
          {spend?.mode === "oauth_quota" && (spend.windows ?? []).map((w) => (
            <div key={w.label} className="space-y-1">
              <div className="flex items-center justify-between text-xs">
                <span>{w.label} limit</span>
                <span className="text-[var(--midnight-muted)]">{w.used_percent > 0 && w.used_percent < 1 ? "<1" : Math.round(w.used_percent)}% used{w.resets_at_ms ? ` · resets ${new Date(w.resets_at_ms).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : ""}</span>
              </div>
              <div className="h-1.5 rounded-sm bg-[var(--edge)]"><div className="h-1.5 rounded-sm bg-[var(--midnight)]" style={{ width: `${Math.min(100, Math.max(0, w.used_percent))}%` }} /></div>
            </div>
          ))}
        </div>
      )}
    </MoneySection>
  );
}
```
Imports: `Link` from `next/link`, `useSWR` from `swr`, `useBilling` + `UsageSummary` from `@/hooks/useBilling`, `useSpend` + `SpendResponse` from `@/hooks/useSpend`, `formatUsdCents` from `@/lib/creditAmounts`, `MoneySection`/`MoneyEmptyLine` from `./MoneySection`. The `Headline` interface comes along with `spendHeadline`. Note in the header comment that the token/cache grid and the per-member table stay on the control Usage panel (`?panel=usage`, Settings), per spec §4 rule 4.

- [ ] **Step 4: Run the new test file — expect PASS.** tsc clean, scan 0.

- [ ] **Step 5: Commit** `feat(money): Agent spend as a section — the billed figure, runs, by model or plan windows`

---

### Task 5: `MoneyPanel` — the page — and the four old panels leave (M)

**Files:**
- Create: `src/components/teams/panels/MoneyPanel.tsx`
- Modify: `src/components/teams/panels/money/money.ts` (add `monthToDate`, `biggestDay`, `formatMicrocents`)
- Modify: `src/components/teams/TeamsPanelRouter.tsx:20-48` (loaders) and the list branch
- Delete: `src/components/teams/panels/PaymentsPanel.tsx`, `ProductsPanel.tsx`, `CustomersPanel.tsx`, `SpendPanel.tsx`, `money/MoneyPanel.tsx`, `panels/__tests__/SpendPanel.test.tsx`
- Rewrite: `src/components/workspace/__tests__/spendDeepLink.test.tsx`
- Test: `src/components/teams/panels/__tests__/MoneyPanel.test.tsx`

**Interfaces:**
- Consumes: Tasks 1–4; `useCompanyRollup(90)` (`{ rollup, isLoading }`; `rollup.days[]` of `{ day, revenue_microcents, signups, visitors }`, `rollup.stripe_bound`); `moneyView` + `MoneyStates` (`money/MoneyStates.tsx`, unchanged); `PanelMasthead`.
- Produces:
  ```ts
  export type MoneySectionId = "payments" | "customers" | "products" | "spend";
  export function MoneyPanel({ section, day, onDayCleared }: { section?: MoneySectionId | null; day?: string | null; onDayCleared?: () => void }): JSX.Element
  // money.ts
  export function formatMicrocents(v: number): string        // microcents → "$1,234.50" / "−$5.00"
  export function monthToDate(days: RollupDay[], today: string): { month: string; microcents: number } | null  // days in today's UTC month; null when none
  export function biggestDay(days: RollupDay[], month: string): RollupDay | null   // max positive revenue in that month
  ```
  Router: keys `money`, `payments`, `products`, `customers`, `spend` all render `MoneyPanel`; `section` is the key (`money` → null); `day` and `onDayCleared` are threaded from `panelParams.day` / `onUpdateDurableParam("day", null)`.

- [ ] **Step 1: Write the failing tests**

```tsx
// src/components/teams/panels/__tests__/MoneyPanel.test.tsx
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";

const money = vi.hoisted(() => ({ view: { kind: "rows" } as { kind: string } }));
vi.mock("@/hooks/useCompanyMoney", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/hooks/useCompanyMoney")>();
  return { ...mod, useCompanyMoney: () => ({ rows: [], view: money.view, hasMore: false, next: vi.fn() }) };
});
const rollup = vi.hoisted(() => ({ value: undefined as unknown, loading: false }));
vi.mock("@/hooks/useCompanyRollup", () => ({ useCompanyRollup: () => ({ rollup: rollup.value, isLoading: rollup.loading, windowStart: "2026-06-09", span: 90, error: undefined, refresh: vi.fn(), isUnavailable: () => false }) }));
vi.mock("@/components/teams/panels/money/AgentSpendSection", () => ({ AgentSpendSection: () => <section aria-label="Agent spend" /> }));
vi.mock("@/components/teams/panels/money/PaymentsSection", () => ({ PaymentsSection: ({ day }: { day: string | null }) => <section aria-label="Payments" data-day={day ?? ""} /> }));
vi.mock("@/components/teams/panels/money/CustomersSection", () => ({ CustomersSection: ({ newIn30Days }: { newIn30Days: number | null }) => <section aria-label="Customers" data-new={String(newIn30Days)} /> }));
vi.mock("@/components/teams/panels/money/ProductsSection", () => ({ ProductsSection: () => <section aria-label="What you sell" /> }));

import { MoneyPanel } from "../MoneyPanel";
import { biggestDay, formatMicrocents, monthToDate } from "../money/money";

const bound = { start: "2026-06-09", end: "2026-09-06", stripe_bound: true, first_day: "2026-08-30", unavailable: [], agent_spend_month_to_date_microcents: 0,
  days: [
    { day: "2026-08-31", revenue_microcents: 90_000_000, signups: 1, visitors: 0 },
    { day: "2026-09-02", revenue_microcents: 34_000_000, signups: 2, visitors: 0 },
    { day: "2026-09-05", revenue_microcents: 640_000_000, signups: 4, visitors: 0 },
  ] };

describe("MoneyPanel — one ledger page", () => {
  beforeEach(() => { rollup.value = bound; rollup.loading = false; money.view = { kind: "rows" }; vi.useFakeTimers({ now: new Date("2026-09-06T12:00:00Z"), toFake: ["Date"] }); });
  afterEach(() => vi.useRealTimers());

  it("carries the month's landed revenue in the masthead, the biggest day under it, and the four sections in order", () => {
    render(<MoneyPanel />);
    expect(screen.getByRole("heading", { level: 1, name: "Money" })).toBeInTheDocument();
    expect(screen.getByTestId("panel-masthead-meta")).toHaveTextContent("$674 landed in September");
    expect(screen.getByText(/Biggest day: Sep 5, 2026 · \$640/)).toBeInTheDocument();
    const names = screen.getAllByRole("region").map((r) => r.getAttribute("aria-label"));
    expect(names).toEqual(["Payments", "Customers", "What you sell", "Agent spend"]);
    expect(screen.getByRole("region", { name: "Customers" })).toHaveAttribute("data-new", "6");
  });

  it("hands the day scope to Payments and links the biggest day to it", () => {
    render(<MoneyPanel day="2026-09-05" />);
    expect(screen.getByRole("region", { name: "Payments" })).toHaveAttribute("data-day", "2026-09-05");
  });

  it("gates the Stripe sections behind the account state, and keeps Agent spend", () => {
    money.view = { kind: "unbound" };
    rollup.value = { ...bound, stripe_bound: false, days: [] };
    render(<MoneyPanel />);
    expect(screen.getByText("Payments are not set up for this business yet.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Payments" })).toBeNull();
    expect(screen.getByRole("region", { name: "Agent spend" })).toBeInTheDocument();
    expect(screen.queryByTestId("panel-masthead-meta")).toBeNull();
  });

  it("prints no landed figure while the rollup is in flight, and none when the month has no rows", () => {
    rollup.value = undefined; rollup.loading = true;
    const { rerender } = render(<MoneyPanel />);
    expect(screen.queryByTestId("panel-masthead-meta")).toBeNull();
    rollup.value = { ...bound, days: [bound.days[0]] }; rollup.loading = false;
    rerender(<MoneyPanel />);
    expect(screen.queryByTestId("panel-masthead-meta")).toBeNull();
    expect(screen.queryByText(/Biggest day/)).toBeNull();
  });

  it("helpers: month-to-date is UTC-month scoped and signed; biggest day ignores refund days", () => {
    expect(formatMicrocents(674_000_000)).toBe("$674"); // whole dollars render without cents (formatUsdCents)
    expect(formatMicrocents(674_500_000)).toBe("$674.50");
    expect(formatMicrocents(-5_000_000)).toBe("−$5");
    expect(monthToDate(bound.days, "2026-09-06")).toEqual({ month: "2026-09", microcents: 674_000_000 });
    expect(monthToDate([bound.days[0]], "2026-09-06")).toBeNull();
    expect(biggestDay([...bound.days, { day: "2026-09-04", revenue_microcents: -90_000_000, signups: 0, visitors: 0 }], "2026-09")!.day).toBe("2026-09-05");
  });
});
```

Rewrite `spendDeepLink.test.tsx`: change the stub at lines 22–25 to mock `@/components/teams/panels/MoneyPanel` with `MoneyPanel: (p: { section?: string }) => <div data-testid="money-panel" data-section={p.section ?? ""} />`; keep tests 1–3 and 5 unchanged; change test 4 ("TeamsPanelRouter mounts a panel for that teamsKey") to assert `screen.getByTestId("money-panel")` has `data-section="spend"`, and add the same assertion for `teamsKey: "payments"` → `"payments"` and `teamsKey: "money"` → `""`. Update the file's header comment: the seam now proves the absorbed keys land on Money's section.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.**

`money.ts` — append:
```ts
import { formatUsdCents } from "@/lib/creditAmounts";
import type { RollupDay } from "@/hooks/useCompanyRollup";

/** Microcents → dollars with a typographic minus, the strip's convention. */
export function formatMicrocents(v: number): string {
  const cents = Math.round(v / 10_000);
  return cents < 0 ? `−${formatUsdCents(-cents)}` : formatUsdCents(cents);
}

/** Revenue landed in today's UTC month, from the stored day rows. Null when
 *  the month has no rows at all — that is "no reading", not "$0". */
export function monthToDate(days: RollupDay[], today: string): { month: string; microcents: number } | null {
  const month = today.slice(0, 7);
  const rows = days.filter((d) => d.day.startsWith(month));
  if (rows.length === 0) return null;
  return { month, microcents: rows.reduce((s, d) => s + (d.revenue_microcents ?? 0), 0) };
}

/** The day with the most revenue in the month; refund-heavy (negative) and
 *  zero days never win. Null when nothing positive landed. */
export function biggestDay(days: RollupDay[], month: string): RollupDay | null {
  let best: RollupDay | null = null;
  for (const d of days) {
    if (!d.day.startsWith(month) || (d.revenue_microcents ?? 0) <= 0) continue;
    if (!best || d.revenue_microcents > best.revenue_microcents) best = d;
  }
  return best;
}
```

`MoneyPanel.tsx`:
```tsx
"use client";

// Money — one ledger page (spec §3). No hero: Home has it. The masthead says
// what landed this month; the biggest day sits under it and links to that
// day's payments; then Payments, Customers, What you sell, Agent spend as
// sections. The four old tabs' keys open this page scrolled to their section.
//
// The Stripe sections are gated ONCE by the account state (unbound / partial /
// under review / forbidden / unavailable — MoneyStates), read off the Payments
// envelope. Agent spend is not Stripe's and always renders.

import { useEffect } from "react";
import { PanelMasthead } from "@/components/teams/shell";
import { useCompanyMoney, type PaymentRow } from "@/hooks/useCompanyMoney";
import { useCompanyRollup } from "@/hooks/useCompanyRollup";
import { handleShellLinkClick, teamsPanelUrl } from "@/lib/shellNav";
import { MoneyStates } from "./money/MoneyStates";
import { PaymentsSection } from "./money/PaymentsSection";
import { CustomersSection } from "./money/CustomersSection";
import { ProductsSection } from "./money/ProductsSection";
import { AgentSpendSection } from "./money/AgentSpendSection";
import { biggestDay, formatIsoDay, formatMicrocents, monthToDate } from "./money/money";

export type MoneySectionId = "payments" | "customers" | "products" | "spend";

const MONTH_NAMES = ["January","February","March","April","May","June","July","August","September","October","November","December"];

export function MoneyPanel({ section, day, onDayCleared }: { section?: MoneySectionId | null; day?: string | null; onDayCleared?: () => void }) {
  // The page-level gate reads the same envelope the Payments section reads
  // (same SWR key when unscoped), so it costs no extra request.
  const gate = useCompanyMoney<PaymentRow>("payments");
  const { rollup, isLoading: rollupLoading } = useCompanyRollup(90);

  const today = new Date().toISOString().slice(0, 10);
  const mtd = rollup?.stripe_bound && !rollupLoading ? monthToDate(rollup.days, today) : null;
  const best = mtd ? biggestDay(rollup!.days, mtd.month) : null;
  const monthName = mtd ? MONTH_NAMES[Number(mtd.month.slice(5, 7)) - 1] : null;
  const newIn30Days = rollup?.stripe_bound && !rollupLoading
    ? rollup.days.filter((d) => d.day >= addDays(today, -29)).reduce((s, d) => s + (d.signups ?? 0), 0)
    : null;

  // The absorbed keys open this page at their section.
  useEffect(() => {
    if (!section) return;
    document.getElementById(`money-${section}`)?.scrollIntoView({ block: "start" });
  }, [section]);

  const showStripe = gate.view.kind === "rows" || gate.view.kind === "zero" || gate.view.kind === "loading";
  const dayHref = best ? `${teamsPanelUrl("money")}&day=${best.day}` : null;

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-6 max-w-5xl mx-auto w-full">
      <PanelMasthead overline="Money" title="Money" meta={mtd && monthName ? `${formatMicrocents(mtd.microcents)} landed in ${monthName}` : undefined} />
      {best && dayHref && (
        <p className="-mt-3 text-xs text-[var(--midnight-muted)]">
          Biggest day: {formatIsoDay(best.day)} · {formatMicrocents(best.revenue_microcents)} ·{" "}
          <a href={dayHref} onClick={(e) => handleShellLinkClick(e, dayHref)} className="underline-offset-4 hover:text-[var(--midnight)] hover:underline">Open that day</a>
        </p>
      )}

      {showStripe ? (
        <>
          <PaymentsSection day={day ?? null} onDayCleared={() => onDayCleared?.()} />
          <CustomersSection newIn30Days={newIn30Days} />
          <ProductsSection stripeBound={!!rollup?.stripe_bound} />
        </>
      ) : (
        <MoneyStates view={gate.view} noun="payments" />
      )}

      <AgentSpendSection />
    </div>
  );
}

function addDays(day: string, delta: number): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + delta)).toISOString().slice(0, 10);
}
```
(`useCompanyRollup` already exports `dayOffset` with this exact body — import and use it instead of `addDays` if it is exported; it is, at `useCompanyRollup.ts:79`. Delete the local helper in that case.)

`TeamsPanelRouter.tsx`: remove the `spend`, `payments`, `products`, `customers` entries from `PANEL_LOADERS` and add `money: () => import("./panels/MoneyPanel").then((m) => m.MoneyPanel)`. Add a prop-typed dynamic wrapper like Brain's:
```tsx
const MoneyPanel = dynamic(() => import("./panels/MoneyPanel").then((m) => m.MoneyPanel), { loading: PanelSkeleton });
const MONEY_SECTIONS: Record<string, "payments" | "customers" | "products" | "spend" | null> = { money: null, payments: "payments", customers: "customers", products: "products", spend: "spend" };
```
and, before the `const Cmp = PANELS[panel]` line:
```tsx
  if (panel in MONEY_SECTIONS) {
    return (
      <MoneyPanel
        section={MONEY_SECTIONS[panel]}
        day={panelParams?.day ?? null}
        onDayCleared={() => onUpdateDurableParam?.("day", null)}
      />
    );
  }
```
Delete the five old files and the SpendPanel test. Search the tree for any other import of them (`git grep -n "PaymentsPanel\|ProductsPanel\|CustomersPanel\|SpendPanel\|money/MoneyPanel"`) — the only expected hits are the router (fixed above) and `spendDeepLink.test.tsx` (rewritten above).

- [ ] **Step 4: Run** `MoneyPanel.test.tsx`, `spendDeepLink.test.tsx`, `money/__tests__`, `panelRegistry.test.ts`; `pnpm exec tsc --noEmit`; eslint on changed files; scan. Expect all clean.

- [ ] **Step 5: Commit** `feat(money): one ledger page — what landed this month, the biggest day, and four sections; the four old tabs open it at their section`

---

### Task 6: One Money item in the sidebar (S)

**Files:**
- Modify: `src/components/chat/companySections.ts:76-85` and its icon imports
- Modify: `src/components/chat/__tests__/CompanyNav.test.tsx:20-45` (`ALL_LABELS`)
- Modify: `src/components/workspace/panelRegistry.ts` (`TEAMS_LIST_KEYS` titles of `payments`/`products`/`customers`/`spend`) and `src/components/workspace/__tests__/panelRegistry.test.ts`

- [ ] **Step 1: Update the tests.** In `ALL_LABELS` replace the four entries `"Payments", "Products", "Customers", "Spend"` with the single entry `"Money"`. Search the rest of the file for `Payments`/`Spend` assertions and update them the same way. In `panelRegistry.test.ts` add to the money test from Task 2:
```ts
    for (const k of ["payments", "products", "customers", "spend"]) {
      expect(describePanel(`teams-${k}`, {})!.title).toBe("Money");
      expect(describePanel(`teams-${k}`, {})!.params).toEqual({ teamsKey: k });
    }
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** Replace the Money group with:
```ts
  {
    // One ledger page (spec §3): what landed this month, then Payments,
    // Customers, What you sell and Agent spend as sections. The four old keys
    // still resolve — they open this page at their section.
    label: "Money",
    items: [{ panel: "teams-money", label: "Money", Icon: CreditCard }],
  },
```
Remove `Package`, `Users`, `Wallet` from the lucide import if nothing else in the file uses them (search first).
In `panelRegistry.ts` `TEAMS_LIST_KEYS`, change `spend: "Spend"`, `payments: "Payments"`, `products: "Products"`, `customers: "Customers"` to `"Money"` each, with this comment above them:
```ts
  // Money is ONE page. These four keys are the tabs it absorbed; they stay
  // registered (mobile builds `?panel=teams-<key>` as strings) and open Money
  // scrolled to their section — TeamsPanelRouter maps the key to the section.
```
The registry's label-parity test (dock titles match the sidebar's IA labels) is why this lands in the same commit as the sidebar change.

- [ ] **Step 4: Run** `CompanyNav.test.tsx`, `ChatLayout.companyNav.test.ts`, and the command-palette tests (`git grep -l companySections src | grep __tests__`). Expect PASS; tsc clean.

- [ ] **Step 5: Commit** `feat(nav): Money is one item`

---

### Task 7: Home's hero bars open that day's payments (S)

**Files:**
- Modify: `src/components/teams/dashboard/HomeRollupStrip.tsx:128-165` (`HeroBars`)
- Modify: `src/components/teams/dashboard/__tests__/HomeRollupStrip.test.tsx`

- [ ] **Step 1: Test first.** In the business-first-hero describe add:
```tsx
  it("links every hero bar to Money scoped to that day, in-shell with a real href", () => {
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    const { container } = render(<HomeRollupStrip spendCents={1400} />);
    const links = Array.from(container.querySelectorAll('[aria-label="Revenue over the period"] a'));
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/chat?panel=teams-money&day=2026-08-30",
      "/chat?panel=teams-money&day=2026-08-31",
    ]);
    expect(links[0].querySelector("rect")).not.toBeNull();
  });
```
(Adjust the two days to this file's `LIVE` fixture if they differ.)

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** In `HeroBars`, wrap each `<rect>` in an SVG anchor:
```tsx
        const href = `${teamsPanelUrl("money")}&day=${days[i].day}`;
        return (
          <a key={days[i].day} href={href} onClick={(e) => handleShellLinkClick(e, href)} aria-label={`${formatDay(days[i].day)} · ${format(microcentsToCents(v))}`}>
            <rect x=… y=… width=… height=… fill=…>
              <title>{`${formatDay(days[i].day)} · ${format(microcentsToCents(v))}`}</title>
            </rect>
          </a>
        );
```
`handleShellLinkClick` / `teamsPanelUrl` are already imported in this file. Update the `HeroBars` doc comment: the bars are the chart-as-filter (spec §2) — a click opens Money on that day; the `<a aria-label>` also gives keyboard and screen-reader users the per-day value the hover title carried (this supersedes the earlier "hover-only" note — rewrite that sentence).

- [ ] **Step 4: Run the strip test — expect PASS** (the existing rect-geometry tests still find their rects). tsc clean.

- [ ] **Step 5: Commit** `feat(home): a revenue bar opens that day's payments`

---

### Task 8: The skill describes Money, and the shipped Home plan goes (S)

**Files:**
- Modify: `apps/infra/openclaw/skills/lightbulb/SKILL.md:344-372`
- Delete: `docs/superpowers/plans/2026-09-06-home-dashboard.md`

- [ ] **Step 1:** Replace lines 346–354 (from "Once the company Stripe account is connected" through the Spend bullet) with:

```
Once the company Stripe account is connected, the owner has one **Money** tab
in the workspace sidebar — a ledger page. Point them at it rather than
describing numbers you would have to fetch yourself. Top to bottom:

- the masthead says what landed this month, and the biggest day links to that
  day's payments (a bar on Home's revenue chart does the same);
- **Payments** — every charge, newest first, with its Stripe receipt, so it
  reconciles against their Stripe Dashboard line for line;
- **Customers** — who bought, when, and their balance, with how many are new
  in the last 30 days;
- **What you sell** — their products and prices, and a link to add one in
  Stripe;
- **Agent spend** — what the agents cost this period: the billed figure, runs,
  and the split by model (or the ChatGPT plan windows). The token detail and
  the per-member table live in Settings → Usage.
```
Then change "the charge" point them at Payments." (line ~360) to "the charge" point them at Money."; change "**Only workspace admins can open the Money tabs.**" (line ~371) to "**Only workspace admins can open Money.**"; change "These tabs read the owner's connected Stripe account **through**" to "Money reads the owner's connected Stripe account **through**" and "Before onboarding is finished the tabs say so" to "Before onboarding is finished the page says so".

- [ ] **Step 2:** `git rm docs/superpowers/plans/2026-09-06-home-dashboard.md` (shipped in #2343; git history keeps it).

- [ ] **Step 3:** Verify `grep -n "Money tabs\|four tabs" apps/infra/openclaw/skills/lightbulb/SKILL.md` prints nothing. Commit `docs(skill): Money is one ledger page; drop the shipped Home plan`.

---

### Task 9: Dev walk before prod (S) — after merge

On `https://dev.flickmeon.ai/chat`, signed in as a dev account (the audit account `lightbulb-audit-wtdcno+clerk_test@example.com` is onboarded as "Ember & Wick", Stripe unbound):

- [ ] Sidebar shows one Money item; `?panel=teams-payments`, `teams-products`, `teams-customers`, `teams-spend` all open Money (dock title "Money") scrolled to their section; `?panel=usage` still opens the control Usage panel.
- [ ] Unbound account: masthead has no landed figure; one "Payments are not set up" notice; Agent spend section renders below it with the free-tier "No charge" or empty-period line.
- [ ] `?panel=teams-money&day=2026-09-05` renders "Payments on Sep 5, 2026" with "Show all payments", which clears `day` from the URL.
- [ ] Home's hero bar (on an account with rollup rows — the founder's prod org after promotion) opens Money on that day.
- [ ] Narrow dock: sections stack, tables scroll inside their container, nothing scrolls the page sideways.
- [ ] Brand scan 0, tsc clean, full suite green in CI.

---

## Out of scope (and why)

- The shared "one Stripe sentence" component across Home / Money / Accounts (spec §4 rule 3): the craft-pass PR; Money keeps `MoneyStates`.
- Per-member spend table and the token/cache grid: stay on the control Usage panel (`?panel=usage`, Settings) per spec §4 rule 4.
- "Top payer" in the what-changed line: needs a walk of payments (the data rule forbids); biggest day comes from rollup rows.
- Any backend change: `/company/payments` already takes `start`/`end`.

## Sizing

Tasks 1, 6, 7, 8 S; Tasks 2, 3, 4, 5 M; Task 9 S. One PR.
