# Owner tabs consolidation — design

**Date:** 2026-09-06. **Status:** approved by the owner in conversation (mockup
walked in Chrome; "ok, that's fine, let's do it like that then").
**Supersedes** the per-tab plans of 2026-09-01/02 for Home, Money, Inbox and
Tasks; the Growth/Company/Support plans are deferred, not cancelled (§6).

## 1. Problem

The sidebar carries 24 items in six groups. Seven are "Coming soon" cards with
no data behind them, one is an empty month calendar, three are Stripe lists
that re-render the Stripe Dashboard with fewer columns, three are one Paperclip
hierarchy split three ways (Goals · Projects · Tasks), and two are the same
feed twice (Inbox · Activity). The detail pages under them are operator
debuggers (session ids, exit codes, webhook signing secrets). The nouns came
from a competitor's roster, not from the owner's work.

## 2. The rule

**One dashboard, then detail pages. No sub-tabs inside a panel.** The shell is
tabs-only since #2326, so a panel's insides are **sections down one page**,
each headed by a mono caps label and the one number that matters, with
"Show more" extending a list in place. Views inside a page are a `select`
(group-by, filter), never a second tab strip.

Every figure is an **anchor**: traceable to where it landed (receipt link,
Stripe id, published URL, a run that finished), with its source and period
named beside it. Nothing prints a number it cannot source.

**Linked views (the Tableau rule).** The owner uses these tabs the way a
Tableau dashboard is used, not authored: a chart is a filter, and every
aggregate opens its rows. Hovering a bar shows the exact day and value;
clicking a day on the revenue hero opens Money scoped to that day; clicking
New customers scopes the Customers section to the window. Every anchored
figure carries **Ask Friday**, which opens the composer pre-filled with a
question about that figure (Tableau Pulse's Ask, on our chat). Where the
data allows, a card states what changed (biggest day, top payer); pace to a
goal waits until goals carry a number. Not copied: chart authoring,
statistical outlier bands, forecasts.

## 3. End state

```
Home · Inbox
Money
Work:      Tasks · Automations · Leads
Knowledge: Brain · Library
Company:   Accounts
```

Nine items. Panel keys never change (mobile builds `?panel=teams-<key>` as
strings); retired keys keep resolving to the page that absorbed them, with the
right section or grouping preselected via the existing `initialTab`/`tab`
param grammar.

| Today | Decision |
|---|---|
| Home | The dashboard. Company name as H1. Revenue hero (7/30/90d **bars with hover values**, vs previous period, net of refunds) beside the anchored figures, each with **Ask Friday**: Agent spend (billed by us, runs, by-model link) and New customers (30d). Then the top of each detail page: **Needs you** (3, from the attention feed and approvals), **Happening now** (live runs), **Recent work** (Library artifacts), each with an "Open …" link. Quick actions live in the masthead meta. The ported Paperclip telemetry (Run Activity / Success Rate charts, Agents / Open Tasks / Pending Approvals metric cards, Recent tasks, Recent activity) is removed from Home. |
| Payments · Products · Customers · Spend | **One tab, Money.** Ledger only, no hero (Home has it). Masthead meta carries "$N landed in <month>". Sections: Payments (newest first, Show more), Customers (with "N new in 30 days"), What you sell, Agent spend billed by us (figure · runs · by model). Accepts a `day` param so a bar on Home's hero opens the payments of that day, and states what changed in the window (biggest day, top payer). The four keys stay registered and open Money scrolled to their section. |
| Inbox · Activity | **One tab, Inbox — and it holds NO task rows.** Owner decision 2026-09-08, after the finding that Inbox and Tasks render the same issue rows today (Inbox = five pill tabs over `/issues`, plus one attention tab): the two pages split **by kind**, not by filter. Inbox answers "what needs me"; Tasks answers "what is the work". Sections: **Needs you** (approvals with Approve/Decline inline, agent questions, failed/blocked runs, recovery — everything awaiting a human response, each with its action), **Happening now** (live runs), **Recently** (the activity feed, day-grouped, one type filter select). The needs-attention band, the five pill tabs and the issue list all go. `teams-activity` opens Inbox scrolled to Recently. **Consequences, accepted:** "mine" was a server scope (`/issues?scope=mine`) and survives as an assignee filter on Tasks; "unread" was per-browser `localStorage` read-marks (`useReadInboxItems`), never server state and never synced across devices — it retires with the pill tabs rather than moving. |
| Tasks · Goals · Projects | **One tab, Tasks.** Goals are headings with "N of M done" and a progress rule; projects are sub-headings; tasks are rows (status circle, title, assignee, status word, updated-ago). Group-by select: Goal · Project · Agent · Status. List/Board toggle stays. `teams-goals` / `teams-projects` open Tasks grouped by Goal / Project. **A goal heading stays clickable and opens the existing goal detail view** (owner decision 2026-09-08): a project is only `{id, name, color}` and folds into a grouping losing nothing, but a goal carries `description`, its own `status`, an `ownerAgentId` and a parent (`parentId`/`level`, so goals nest) — flattening it to a heading would drop all four. Tasks also gains the **assignee filter** that Inbox's "mine" scope used to provide. No due dates (Paperclip issues have none), no goal money measure (goals have no metric). |
| Search | Folds into ⌘K: the palette gains a results section over the existing `/search` route. |
| Org chart | A view inside the Agents panel the roster opens. |
| Brain | Off the rail; key stays; Dreams returns to a Settings control panel. The company-graph Brain is its own spec. |
| Support · Publish · SEO & AEO · Ads · Identity · Compliance · Contracts | Off the rail, keys kept, hidden **capability-driven**: `visibleCompanySections` already filters on a status flag; each returns automatically when its rail is on (Publish when a posting rail is live for the owner, Support when the thread store ships). Their jobs are housed elsewhere meanwhile (§6). |
| Automations · Leads · Library · Accounts | Stay. Automations list rewrite (plain sentence, next run, on/off; cron and secrets behind Advanced) is a later feature PR. Accounts' guardrail switches render read-only until enforced. |

## 4. Craft rules (every PR)

Inside `.interface-design/system.md`: cream ground, hairline borders only,
Lora for titles and hero figures, Source Sans for body, mono for meta and
numbers, 4px grid, calm when healthy.

1. **One state vocabulary.** `PanelNotice` for every non-row state. No ad hoc
   "Loading…" lines or coloured error divs.
2. **One masthead grammar.** `PanelMasthead`: overline = group, title = tab,
   meta = the one number that matters (Inbox "3 need you", Money "$4,120
   landed in September", Tasks "12 open · 2 blocked").
3. **One Stripe sentence.** A shared status component with three states (not
   connected · finishing setup · connected) used by Home, Money and Accounts.
   No account ids, no coloured dots.
4. **Control-plane panels do not wear business mastheads.** Spend's Usage body
   is restyled into the editorial shell; the token/cache grid goes. Dreams
   leaves Brain.
5. **Tables carry at most five columns**, one primary text column, right-aligned
   tabular numbers, status as a word with colour only reinforcing it.
6. **Detail pages hide the operator.** Runs, subtree control, session ids, exit
   codes, revisions, secret rotation sit behind one "Advanced" disclosure.
7. **Empty states are a verb.** One drafted prompt Friday can act on today.

## 5. Data rules (what a page may print)

- Revenue, signups, visitors, previous-period comparison: `/company/rollup`
  days (revenue is net of refunds: every balance transaction's `net`).
- Agent spend: `/billing/spend` + `/billing/usage` (Bedrock owners); "Not
  billed by us" otherwise. Never the DynamoDB counters.
- Lists over Stripe cursors print no totals ("Show more", never "Show all N").
- Approvals, questions, recovery and blocked rows: `/teams/attention` +
  approvals; live runs: `/inbox/live-runs`; artifacts: `/artifacts`.
- Goals/projects/tasks: `/goals`, `/projects`, `/issues` (issues carry
  `goalId`, `projectId`). Derived counts are fine; invented fields are not.

## 6. Sequence and process

One tab per PR, in this order: **Home → Money → Inbox → Tasks → rail cut
(shells, Search, Org chart, Brain) → craft pass.** Each PR: fresh worktree
off `origin/main`, superpowers writing-plans + subagent-driven development,
per-task Sonnet review plus a whole-branch review, targeted tests + `tsc` +
brand scan locally, full suite in CI, Claude-in-Chrome walk on dev, then one
prod promotion at the end of the sequence. Growth (Publish store, Content,
SEO scan), Support (thread store), Company (Identity, Compliance) and the
Brain graph follow as their own specs after the consolidation lands.

## 7. Constraints

Panel keys permanent. An absorbed key ALIASES to the page that absorbed it
(`RETIRED_PANEL_ALIASES`: `teams-spend` → `teams-money`) and `describePanel`
synthesizes the section from the RAW key — the `dreams` → `teams-brain`
precedent, which carries Brain's Dreams tab the same way. Mapping absorbed keys
in the router instead would leave them as separate panel ids, and the dock
dedupes by panel id: Home's Payments/Customers/Spend tiles would open three
sibling tabs all titled "Money". `CompanyNav.test.tsx` asserts the exact label list and section order
and must change in the same commit as the nav. Brand tokens only
(`node scripts/brand-tokens-scan.mjs src` prints 0). Issue #2285 (the
provisioning stepper hides every read-only panel) is out of scope here.
