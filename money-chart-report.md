# Money leads with a revenue chart

Branch `feat/rail-cut-money-chart`, three commits on top of `c4aa18724`.

| sha | what |
|---|---|
| `29589140c` | refactor(money): one revenue chart, shared by Home and Money |
| `8e13f9ca0` | feat(money): lead with the revenue chart, not the ledger |
| `b946c13a3` | feat(money): demote the payments ledger to a five-row preview |

## 1. The chart is shared, not written twice

`HomeRollupStrip`'s private `HeroBars` moved out whole to
`src/components/teams/shared/RevenueBars.tsx`. Both Home and Money render it.
It keeps every hook Home's suite selects on — `[aria-label="Revenue over the
period"]`, `rect > title`, `role="group"`, the per-bar anchors to
`?panel=teams-money&day=…` — so `HomeRollupStrip.test.tsx` passes unchanged
(32/32).

Two changes went with the move:

- **The `format` prop is gone.** Home's `formatSignedUsdCents(microcentsToCents(v))`
  and `money.ts`'s `formatMicrocents(v)` are the same function; the chart now
  formats its own microcents and neither caller passes a formatter.
- **`selectedDay` is new**, unused by Home. Money accents the day the page is
  scoped to instead of "today", and marks that anchor `aria-current="true"`.

## 2. Money's revenue section

`src/components/teams/panels/money/RevenueSection.tsx`, rendered by `MoneyPanel`
directly under the masthead, inside the existing `MoneySection` band (`id="money-revenue"`).

**One request, three periods.** The page already reads `useCompanyRollup(90)`
for the masthead figure, and `rollupWindow` asks for DOUBLE the span — 180 rows.
7 / 30 / 90 and each one's equal-length previous period are all inside that, so
the period toggle slices rows already in hand: no second SWR key, no refetch, no
loading flash between periods. The window end comes from `rollup.end` (the
server's own range end), not the browser clock — a viewer in UTC-7 slicing
against their own date silently drops the last row.

Previous-period coverage is `first_day <= prevStart`. Uncovered means no
comparison sentence at all, never a `$0` for days nobody measured (there is no
backfill). The change is stated in dollars, not a percentage: revenue can be
zero or negative, and a percentage off either baseline is undefined or
misleading.

Every figure comes from `days[].revenue_microcents`. Nothing sums the
payments/customers/products cursor pages.

States, each one calm italic serif sentence in the existing vocabulary:

| condition | what renders |
|---|---|
| read failed, no cached rollup | `role="alert"` — temporary read failure, not a zero |
| first load in flight | an `aria-hidden` box; the period tabs stay mounted |
| `stripe_bound: false` | "Connect Stripe to see revenue here…" |
| `revenue_microcents` in `unavailable[]` | "Revenue isn't available for this account." |
| `first_day === null` | "No daily readings yet." — no chart drawn |
| no rows inside the selected period | "No daily readings in the last N days." |

`MoneyEmptyLine` gained one optional prop (`role`, default `"status"`) so the
error line carries `role="alert"` without a second primitive.

**Day scoping.** Bars are the same anchors Home uses, so a click pushes
`?panel=teams-money&day=YYYY-MM-DD` and the existing `day` / `onDayCleared`
plumbing does the rest. When a day is active the section header says
"Showing Sep 5, 2026" and offers "Show the whole period", the scoped bar takes
the accent, and the "pick a day" hint is suppressed.

**Admin gate.** The section renders only inside `MoneyPanel`'s `showStripe`
branch, so a `forbidden` gate shows the one page-level notice and no chart —
pinned by a test.

## 3. The ledger demoted

Unscoped, `PaymentsSection` shows `rows.slice(0, 5)`, says "5 most recent", drops
"Show more" entirely, and links to `https://dashboard.stripe.com/payments` —
only when the envelope says the account is bound. `useCompanyMoney` now returns
`stripeBound` off that envelope rather than the section inferring it.

The day-scoped case is untouched: full list, the day-naming heading, "Show all
payments", and its pager.

Customers, What you sell and Agent spend are unchanged, in the same order.

## Checks

- `vitest run` (named files, one invocation): **73 passed** across
  `HomeRollupStrip`, `MoneyPanel`, `RevenueSection` (new, 9), `sections`,
  `MoneyStates`. `AgentSpendSection` (21) and `spendDeepLink` (8) also run green.
- `pnpm exec tsc --noEmit` — clean.
- `pnpm exec eslint` on all ten touched files — clean.
- `node scripts/brand-tokens-scan.mjs src` — **0 colour literals**.

## Concerns

1. **The toggle no longer refetches.** Slicing the 90-day payload is exact for
   all three periods and saves a request, but it means the period selector
   cannot ever show more than 180 days of history. Fine today (90 is the widest
   option); if a 180d option is ever added, the fetch span has to move with it.
2. **The masthead's "Biggest day … Open that day" line now sits directly above a
   chart whose bars do the same job.** I left it — the task scoped the masthead
   out — but it is the obvious next deletion.
3. **`stripe_bound` has two sources on this page**: the rollup's flag (used by
   the chart and `ProductsSection`) and the payments envelope's (used by the new
   ledger link). Same underlying binding, and the chart's is the one that can
   lag behind by a render while the rollup loads, which only costs a beat before
   the link appears.
