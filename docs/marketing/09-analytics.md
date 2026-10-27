# 09. Analytics: the tracking plan behind the five Monday numbers

## Purpose
Moves the measurement half of the goal: "the five Monday numbers reported from real events". Serves business plan §9 "What we measure from here", Appendix C, Engineering/Product item 7 and the Go To Market "Weekly rhythm".

## What we know
All facts below are first-party (code on `origin/main` at `6249f614d`, 13 Oct 2026, and read-only HogQL aggregates over PostHog project 380894 for the last 30 days). Confidence is high unless marked.

**Web (posthog-js).** `apps/frontend/src/components/PostHogProvider.tsx` initialises PostHog with `person_profiles: "identified_only"` and manual pageviews, registers `environment` and `signup_flow_id` as super properties, and calls `identify(clerkUserId)`. `environment` is derived from `NEXT_PUBLIC_API_URL`, not the page's host. `src/lib/analytics.ts` holds the event registry (a strict TypeScript union) and stores a **first-touch and last-touch** record (`utm_*`, referrer, referring domain, landing path) in localStorage, stamped on every client event as `first_touch_*` / `last_touch_*`. No `posthog.group()` call exists. Funnel events today: `landing_viewed` (only on `/`), `signup_cta_clicked`, `signup_started` (means only "Clerk account created"), `onboarding_ready`, `join_link_copied`, `session_opened`.

**Backend (posthog-python).** One choke point, `core/observability/product_analytics.py::capture_product_event`, stamps `environment` on every server event. Most events carry `workspace_id` (the Clerk org id) and use the acting human as `distinct_id`. Server events found: `trial_start_requested`, `trial_started`, `trial_start_failed`, `trial_first_agent_response`, `trial_cap_reached`, `trial_expired_block`, `trial_converted`, `trial_will_end`, `subscription_activated` / `_past_due` / `_status_changed` / `_cancelled`, `agent_run_completed`, `agent_reply_delivered`, `agent_dispatch_missed`, `room_created`, `room_member_added`, `room_message_sent` (humans only), `reaction_added`, `invitation_sent`, `invitation_revoked`, `join_link_redeemed`, `share_link_created`, `share_link_opened`, `session_copied`, `session_join_requested`, `session_join_decided`, `session_picked_up`, `local_session_*`, `first_run_brief_submitted`, `automation_armed`, `catalog_agent_deployed`.

**Mobile** (`apps/mobile`): no PostHog client; it emits nothing client-side. Its human messages still reach PostHog through the backend's `room_message_sent`.

**What is broken or missing, measured over 30 days:**

| Gap | Evidence |
|---|---|
| Dev drowns prod without a filter | `room_message_sent`: 40,460 dev vs 2,709 prod; `trial_started`: 50 dev vs 20 prod |
| Client `environment` is wrong on dev hosts | 125 pageviews on `dev.thelightbulb.company` tagged `environment = prod` |
| The launch site shares the funnel | 2,088 of 4,702 pageviews are on `flickmeon.ai` |
| Org-keyed "people" | `agent_run_completed`: 1,583 of 1,588 have an `org_…` distinct_id and no `workspace_id`; `trial_started`: 52 of 70 org-keyed, 18 carry `workspace_id` |
| Billing events have no workspace | `trial_converted`, `subscription_activated`, `subscription_cancelled`: 0 with `workspace_id` |
| `trial_converted` counts coupons | It fires on a status change (`trial_local` → `active`), never reads an invoice; six subscriptions are on a 100% internal coupon (plan §9) |
| `agent_run_completed` skips room runs | `messaging.handle_usage_report` returns `ignored` for room session keys before analytics runs |
| No setup request, team-formed, handoff, or invoice-paid event | No emitter on `origin/main`; Stripe webhook handles no `invoice.paid` |
| Direct dominates | Prod `landing_viewed`, 30 days, unique visitors by first touch: direct 212, `ig` 26, m.facebook.com 17, linkedin 16, reddit app 15, threads 9, google 4, t.co 3 |
| The admin growth funnel has no environment filter | `ops/growth.py` `HOGQL_FUNNEL` and `HOGQL_SIGNUP_SOURCES` filter neither environment nor host |
| Internal exclusion is by email domain only | `run_analytics.resolve_internal_owner` checks the org creator's email; Appendix C's list (demo-lab, MKT385 intern workspace, …) is not encoded anywhere |
| No setup door, no pricing page | `thelightbulb.company/pricing` and `/request-setup` both return 404 (curl, 13 Oct) |
| Vercel Web Analytics off | `@vercel/analytics` is not in `apps/frontend/package.json`; plan item 7 lists turning it on |

## Findings (ranked by impact)
1. **The data is mostly there; the filters are not.** Without `environment = 'prod'`, every count is 6 to 15 times too high. Without a host filter, dev pageviews and the launch site enter the product funnel. These are query fixes, free today.
2. **Three of the five numbers have no event yet.** Setup requests, first handoff and paying at list need emitters. Team formed and week-4 active can be computed from data we already have (Clerk memberships, `room_message_sent`), so their events are a convenience, not a blocker.
3. **The workspace is the unit, and half the server events lose it.** Billing events have no `workspace_id`; run events use the org as a person. The fix is one rule in one function.
4. **Attribution is wired for first and last touch but has nothing to read.** Three quarters of landings carry no UTM and no referrer. The cure is UTMs on every founder link plus a self-reported question, per the attribution skill: "direct" is a measurement problem, not a channel, and self-report is the only way to see dark social.
5. **Volume is tiny.** At ten teams, every number needs the named workspaces behind it; anything beyond first-touch and last-touch is noise.

## Recommendation
Keep PostHog as the one event store and the workspace id as the unit. A rejected approval counts as a completed handoff. Fix identity in `capture_product_event`, add four events (`setup_requested`, `workspace_created`, `team_formed`, `handoff_completed`) and one Stripe-backed event (`invoice_paid`), move `trial_converted` behind a paid invoice, and ship a readout script that runs the HogQL below plus a Clerk and a Stripe read. Turn on Vercel Web Analytics as a second, cookieless traffic count.

Rejected: PostHog group analytics as the primary key (a paid add-on; PostHog's docs: "Group analytics is a paid add-on", and a plain `workspace_id` property already answers every query; the founders decided against the paid add-on, F2); a sweeper event for week-4 active (the readout computes it from `room_message_sent`; one fewer moving part); GA4 or a second analytics tool; multi-touch models (too few conversions).

## Deliverable

### 1. Tracking plan
Naming: `object_action`, lowercase, past tense. Every event carries `environment` (`prod`/`dev`/`local`) and, once a workspace exists, `workspace_id` (Clerk org id). Client events also carry `first_touch_*` and `last_touch_*` automatically.

| Event | Emitter | Trigger | Properties | Status |
|---|---|---|---|---|
| `$pageview` | web | every route change | `$host`, path, touch props | exists; host filter needed |
| `landing_viewed` | web | entry on a marketing page | `path` | change: fire on `/`, `/pricing`, `/request-setup`, `/docs/*` entry, not only `/` |
| `setup_cta_clicked` | web | "Request a guided setup" clicked | `cta_id`, `surface`, `route` | new (same helper as `signup_cta_clicked`) |
| `signup_cta_clicked` | web | "Start free" clicked | `cta_id`, `surface`, `destination` | exists |
| `setup_requested` | backend | `/request-setup` form accepted | `request_id`, `team_size`, `agents_used`, `self_reported_source`, `self_reported_detail`, the posted `first_touch_*`/`last_touch_*` | new; `distinct_id` = the visitor's PostHog anonymous id posted with the form |
| `source_reported` | web | "How did you first hear about us?" answered | `surface` (`sign_up` / `setup_form`), `answer`, `detail`, `workspace_id` | new; also `$set_once` `self_reported_source` on the person |
| `signup_started` | web | Clerk account created | `signup_path` | exists |
| `workspace_created` | backend | Clerk `organization.created` | `workspace_id`, `created_by` | new |
| `trial_started` | backend | trial row created | `workspace_id` | exists; fix org-keyed persons |
| `member_joined` | backend | Clerk `organizationMembership.created` | `workspace_id`, `members_count`, `hours_since_created` | new |
| `team_formed` | backend | same webhook, when human members reach 2 and the workspace is under 48 h old | `workspace_id`, `members_count`, `hours_since_created` | new, once per workspace (deterministic uuid) |
| `join_link_redeemed` | backend | join link used | `workspace_id`, `invited_by`, `members_count` | exists |
| `invitation_sent` | backend | email invite | `workspace_id` | exists |
| `handoff_completed` | backend | a person approved or rejected an approval, picked up a session, or moved an agent's task | `workspace_id`, `kind` (`approval_answered` / `session_picked_up` / `task_moved`), `days_since_created` | new; every time, the readout takes the first |
| `session_picked_up` | backend | pick-up ticket issued | `workspace_id`, `session_id` | exists; also emits `handoff_completed` |
| `room_message_sent` | backend | human room message | `workspace_id`, `room_kind`, `mentions_agents` | exists; drives weekly active and week-4 active |
| `agent_reply_delivered` | backend | agent answers a human | `workspace_id`, `latency_ms` | exists |
| `agent_run_completed` | backend | run ends | `workspace_id`, `run_source`, `outcome` | change: fire for room runs; add `workspace_id`; key on the requester |
| `invoice_paid` | backend | Stripe `invoice.paid` | `workspace_id`, `amount_paid`, `currency`, `plan`, `at_list` (amount_paid > 0 and no discount), `billing_reason` | new |
| `trial_converted` | backend | first `invoice_paid` with `at_list` | `workspace_id`, `plan` | change: only on a paid, undiscounted invoice |
| `subscription_*` | backend | Stripe subscription webhooks | add `workspace_id` | change |

### 2. Identity model
- **Unit: the workspace** (`workspace_id` = Clerk org id). Every Monday number is a count of workspaces. **Person: secondary**, the Clerk user id, so the admin per-user view keeps working.
- **One rule, enforced in `capture_product_event`:** `distinct_id` is always a human (`user_…`) or a pre-sign-up anonymous id, never an `org_…` id. When the caller has no human (sweepers, usage callbacks, Stripe without `clerk_user_id`), use the workspace creator (Clerk `created_by`, already resolved and cached by `run_analytics`). `workspace_id` is always a property, filled from `owner_id` when the caller gave an org id.
- **No `$groups` and no paid group analytics (F2).** `workspace_id` stays a plain property.
- **History:** do not rewrite old events. Queries read `coalesce(properties.workspace_id, if(distinct_id like 'org_%', distinct_id, null))` for dates before the fix.

### 3. Attribution

**UTM conventions for every link a founder posts.** Lowercase, hyphens, no spaces. Keep links short; the setup page URL plus three tags.

| Tag | Values |
|---|---|
| `utm_source` | `x`, `reddit`, `hn`, `linkedin`, `producthunt`, `email`, `claude-plugins`, `codex-plugins`, `instagram`, `flickmeon` (the launch site's link to the product), `partner-<name>` |
| `utm_medium` | `social` (post), `reply`, `dm`, `email`, `directory`, `bio`, `referral` |
| `utm_campaign` | `<yyyy-mm-dd>-<topic>` for a post (`2026-10-20-handoffs`); `founding-teams` for cold email; `launch-hn` / `launch-ph` for launches |
| `utm_content` | the founder: `leo`, `aden`, `prasiddha`; add `-a` / `-b` for the cold-email ask variant |

Example: `https://thelightbulb.company/request-setup?utm_source=reddit&utm_medium=reply&utm_campaign=2026-10-20-handoffs&utm_content=leo`. Profile bios use `utm_medium=bio` and no campaign.

**Self-reported question.** "How did you first hear about us?" One pick-list plus optional free text ("Tell us more"), asked (a) on `/request-setup`, where it is required, and (b) on `/onboarding` when the workspace is created, where it is optional (D13). Clerk's hosted sign-up form stays untouched.

| Value | Label |
|---|---|
| `x` | X (Twitter) |
| `reddit` | Reddit |
| `hn` | Hacker News |
| `linkedin` | LinkedIn |
| `producthunt` | Product Hunt |
| `plugin` | Claude Code or Codex plugin |
| `friend` | A friend or colleague |
| `teammate` | My team already uses it |
| `ai_assistant` | ChatGPT, Claude or another AI assistant |
| `search` | Google or another search engine |
| `founder_email` | An email from a Lightbulb founder |
| `instagram` | Instagram |
| `other` | Other |

**First-touch and last-touch side by side.** Both already ride on every client event. The readout reports, per setup request and per new workspace: tracked first touch, tracked last touch, and the self-reported answer. When they disagree, the self-reported answer is the tiebreaker for "which channel" and the tracked one is the check on volume.

**How the setup form keys sources.** The form posts three hidden values: `posthog.get_distinct_id()` (only while it is still anonymous, never an email; the backend rejects anything containing `@` or longer than 64 characters), and the stored `first_touch` / `last_touch` objects. The backend emits `setup_requested` on that anonymous id, so when the person later signs up and is identified, PostHog merges the request onto their person and the workspace inherits the source. The same three values go into the founder's notification email so the source is visible before any query runs.

### 4. The Monday readout
An agent runs this every Monday for the ISO week just ended. Inputs: `{start}` = last Monday 00:00 UTC, `{end}` = this Monday 00:00 UTC, `{excluded}` = Appendix C workspace ids (resolve the slugs to ids via Clerk on each run; see §6). Output: one page with the five numbers, each followed by the named workspaces behind it.

**PostHog query call (read-only):**
```bash
KEY=$(aws secretsmanager get-secret-value --profile isol8-admin --region us-east-1 \
  --secret-id lightbulb/prod/posthog_project_api_key --query SecretString --output text)
curl -s -X POST https://us.posthog.com/api/projects/380894/query/ \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"query":{"kind":"HogQLQuery","query":"<HogQL>"}}'
```

**Shared filter** (paste into every query): `properties.environment = 'prod' and toString(properties.workspace_id) not in {excluded}`; client events add `and properties.$host in ('thelightbulb.company','www.thelightbulb.company')`.

**1. Setup requests by source**
```sql
select coalesce(nullIf(properties.self_reported_source,''),'not_answered') as said,
       multiIf(coalesce(properties.first_touch_utm_source,'')!='', lower(properties.first_touch_utm_source),
               coalesce(properties.first_touch_referring_domain,'')!='', properties.first_touch_referring_domain, 'direct') as first_touch,
       multiIf(coalesce(properties.last_touch_utm_source,'')!='', lower(properties.last_touch_utm_source),
               coalesce(properties.last_touch_referring_domain,'')!='', properties.last_touch_referring_domain, 'direct') as last_touch,
       count() as requests
from events
where event = 'setup_requested' and properties.environment = 'prod'
  and timestamp >= {start} and timestamp < {end}
group by said, first_touch, last_touch order by requests desc
```

**2. Teams formed** (cohort: workspaces created in the week ending 48 h before `{end}`, so every window has closed)
```sql
with created as (
  select toString(properties.workspace_id) as ws, min(timestamp) as t0 from events
  where event = 'workspace_created' and properties.environment = 'prod'
    and timestamp >= {start} - interval 2 day and timestamp < {end} - interval 2 day
    and toString(properties.workspace_id) not in {excluded}
  group by ws),
formed as (
  select toString(properties.workspace_id) as ws, min(timestamp) as t1 from events
  where event = 'team_formed' and properties.environment = 'prod' group by ws)
select count() as created, countIf(f.ws != '' and f.t1 <= c.t0 + interval 48 hour) as teams_formed
from created c left join formed f on f.ws = c.ws
```
Cross-check against Clerk (the truth for membership): for each org created in the window, `GET https://api.clerk.com/v1/organizations/{id}/memberships?limit=100` with `lightbulb/prod/clerk_secret_key`; a team is formed when the second membership's `created_at` is within 48 h of the org's `created_at`. Until `workspace_created` ships, list orgs with `GET /v1/organizations?order_by=-created_at&limit=100&include_members_count=true`.

**3. First handoff in week one** (cohort: workspaces created in the week ending 7 days before `{end}`)
```sql
with created as (
  select toString(properties.workspace_id) as ws, min(timestamp) as t0 from events
  where event = 'workspace_created' and properties.environment = 'prod'
    and timestamp >= {start} - interval 7 day and timestamp < {end} - interval 7 day
    and toString(properties.workspace_id) not in {excluded}
  group by ws),
handoff as (
  select toString(properties.workspace_id) as ws, min(timestamp) as t1, argMin(properties.kind, timestamp) as kind
  from events where event in ('handoff_completed','session_picked_up') and properties.environment = 'prod'
  group by ws)
select count() as created, countIf(h.ws != '' and h.t1 <= c.t0 + interval 7 day) as first_handoff_week1
from created c left join handoff h on h.ws = c.ws
```

**4. Week-4 active** (cohort: teams formed whose fourth week, days 21 to 28 after creation, ended inside the readout week)
```sql
with teams as (
  select toString(properties.workspace_id) as ws, min(timestamp) as t0 from events
  where event = 'team_formed' and properties.environment = 'prod'
    and toString(properties.workspace_id) not in {excluded}
  group by ws having t0 + interval 28 day >= {start} and t0 + interval 28 day < {end}),
msgs as (
  select toString(properties.workspace_id) as ws, timestamp as t from events
  where event = 'room_message_sent' and properties.environment = 'prod'
    and timestamp >= {start} - interval 35 day)
select count(distinct teams.ws) as teams, count(distinct if(m.t >= teams.t0 + interval 21 day and m.t < teams.t0 + interval 28 day, teams.ws, null)) as week4_active
from teams left join msgs m on m.ws = teams.ws
```
Report beside it the Thursday "very disappointed" share (asked by hand; not in PostHog). Cross-check with `GET /api/v1/ops/growth` (platform admin), which computes week-4 retention from the DynamoDB message table.

**5. Paying at list** (Stripe is the source of truth)
```bash
SK=$(aws secretsmanager get-secret-value --profile isol8-admin --region us-east-1 \
  --secret-id lightbulb/prod/stripe_secret_key --query SecretString --output text)
curl -s -G https://api.stripe.com/v1/invoices -u "$SK:" \
  -d status=paid -d limit=100 -d 'expand[]=data.customer' -d 'expand[]=data.discounts'
```
Count distinct `customer.metadata.workspace_id` where `amount_paid > 0`, `discounts` is empty, `billing_reason` is `subscription_create` or `subscription_cycle`, and the workspace is not excluded. Page with `starting_after` until `has_more` is false. Report all-time and new this week. After `invoice_paid` ships, the same number in HogQL is `select count(distinct properties.workspace_id) from events where event = 'invoice_paid' and properties.at_list = true and properties.environment = 'prod'`, and it must match Stripe.

**Funnel by source: landing → setup request or start free** (tested 13 Oct on existing events)
```sql
with landed as (
  select person_id,
    argMin(multiIf(coalesce(properties.first_touch_utm_source,'')!='', lower(properties.first_touch_utm_source),
                   coalesce(properties.first_touch_referring_domain,'')!='', properties.first_touch_referring_domain, 'direct'), timestamp) as source
  from events
  where event = 'landing_viewed' and properties.environment = 'prod'
    and properties.$host in ('thelightbulb.company','www.thelightbulb.company')
    and timestamp >= {start} and timestamp < {end}
  group by person_id),
acts as (
  select person_id,
    countIf(event = 'setup_cta_clicked') as setup_clicked, countIf(event = 'setup_requested') as setup_requested,
    countIf(event = 'signup_cta_clicked') as start_free_clicked, countIf(event = 'signup_started') as signed_up
  from events
  where properties.environment = 'prod' and timestamp >= {start} and timestamp < {end} + interval 7 day
    and event in ('setup_cta_clicked','setup_requested','signup_cta_clicked','signup_started')
  group by person_id)
select l.source, count() as landed,
  countIf(a.setup_clicked > 0) as setup_clicked, countIf(a.setup_requested > 0) as setup_requested,
  countIf(a.start_free_clicked > 0) as start_free_clicked, countIf(a.signed_up > 0) as signed_up
from landed l left join acts a on a.person_id = l.person_id
group by l.source order by landed desc
```
Add the Vercel Web Analytics visitor count and top referrers for the same week (Vercel MCP `aggregate_pageviews` on the `frontend` project) as a cookieless check on the `landed` total.

### 5. Vercel Web Analytics
Turn it on in the `frontend` project's Analytics tab and add `<Analytics />` from `@vercel/analytics/next` to the root layout. It is "available on all plans" and "does not use cookies" (Vercel docs), counting visitors by a one-day request hash and excluding bots by user agent. What it adds: a traffic count that ad blockers and consent choices cannot strip (PostHog already routes through `/ingest`, so the gap should be small; the difference is itself a measure), top pages and referrers per page, and country/device. It adds no identity, funnels or workspace joins, so it complements PostHog; no custom events.

### 6. Data-quality guards

| Guard | Rule |
|---|---|
| Environment | Every query: `properties.environment = 'prod'`. Client events also: `$host in ('thelightbulb.company','www.thelightbulb.company')`. Fix the client to derive `environment` from `window.location.hostname` (task 1) |
| Launch site | `flickmeon.ai` traffic is a separate line, never in the product funnel. Links from it to the product carry `utm_source=flickmeon` |
| Coupons | Paying at list comes from Stripe invoices with `amount_paid > 0` and no discount. `trial_converted` before task 6 ships is not a revenue number |
| Internal workspaces | Prasiddha owns the exclusion list (F1). Exclude Appendix C: lightbulb, demo-lab, 10-06-test, adenprivatetest, external-review, starter-roster-audit-sep-12, and the MKT385 intern workspace. One list, in the readout script, by slug; resolved to org ids via Clerk `GET /v1/organizations?query=<slug>` on each run. Any workspace whose creator is a platform admin is also excluded |
| Org-keyed persons | Never count `uniq(distinct_id)` for a workspace number; count `workspace_id` |
| Duplicates | Server events that can retry pass a deterministic `event_uuid` (already the pattern); new events follow it |
| Small numbers | Every number is printed with the workspace names behind it; no percentages on a base under 10 |

### 7. What still cannot be measured, and why
- **Dark social and word of mouth** (DMs, group chats, a founder telling a friend): no referrer exists. Only the self-reported answer sees it, and only for people who reach a form.
- **AI assistant referrals**: an assistant's answer usually arrives as direct or branded search. Self-report's `ai_assistant` option is the only signal.
- **Cross-device and cross-domain journeys**: attribution lives in one browser's localStorage. A visitor who reads a post on a phone and signs up on a laptop shows as direct. `flickmeon.ai` links lose the original source on the redirect unless they carry UTMs. Old `flickmeon.ai` posts are not retro-tagged (E10); every link from now carries a UTM.
- **Mobile**: the iPhone app emits nothing client-side; installs from invites (plan item 6) are unmeasured until the app has a client or App Store Connect is read.
- **Handoffs outside the product**: a teammate who reads an agent's output and acts on it in another tool leaves no event.
- **Paperclip task moves by people** are only measurable once task 5 adds an emitter on the Teams BFF route; tasks moved inside Paperclip's own UI are not.
- **Server events lost while the backend is down** are dropped (best-effort by design); DynamoDB, Clerk and Stripe stay the truth for counts.

## Build tasks
In order. Each is one PR. Seat billing and the trial checkout (workstream 15) are built before task 6, which reads their invoices.

1. **Stamp the true environment on web events.** Today: `environment` comes from `NEXT_PUBLIC_API_URL`, so 125 dev pageviews in 30 days read `prod`. Change: derive it from `window.location.hostname` (`thelightbulb.company`/`www.` → `prod`, `flickmeon.ai` → `launch`, else `dev`). Why: every Monday query depends on it. Measure: 0 events with `$host = 'dev.thelightbulb.company'` and `environment = 'prod'` a week after deploy.
2. **One identity rule in `capture_product_event`.** Today: `agent_run_completed` and trial events use the org id as a person; billing events lack `workspace_id`. Change: if `distinct_id` starts with `org_`, move it to `workspace_id` and resolve the creator (`created_by`) as the person; Stripe events add `workspace_id` from the billing row. Why: the workspace is the unit; the admin per-user view needs user ids. Measure: 0 events with an `org_` distinct_id; 100% of server events after the deploy carry `workspace_id` when a workspace exists.
3. **`agent_run_completed` for room runs.** Today: room runs return `ignored` before analytics. Change: call `run_analytics.schedule_run_completed` from the room `run.ended` handler with the requester as person. Why: plan item 7. Measure: room-run events per day match the run ledger's closed runs within 5%.
4. **`workspace_created`, `member_joined`, `team_formed` from the Clerk webhooks.** Today: none. Change: emit in `_handle_organization_created` and the `organizationMembership.created` handler; `team_formed` once when human members reach 2 within 48 h (uuid5 of the org id). Why: number 2. Measure: matches the Clerk cross-check for two consecutive Mondays.
5. **`handoff_completed`.** Today: only `session_picked_up` exists. Change: emit from `routers/teams/approvals.py` approve and reject (`approval_answered`), from session pick-up (`session_picked_up`), and from the Teams issue update route when a person changes the status of an agent-assigned task (`task_moved`). Why: number 3. Measure: Monday query 3 returns named workspaces; a founder confirms each against the workspace.
6. **`invoice_paid` and an honest `trial_converted`.** Today: no `invoice.paid` handler; `trial_converted` fires on status, coupons included. Change: add `invoice.paid` to the Stripe handler map (and to the webhook endpoint's enabled events), emit `invoice_paid` with `at_list`; emit `trial_converted` only on the first `at_list` invoice; stop emitting it from `handle_subscription_state`. Why: number 5 and the coupon rule. Measure: PostHog paying-at-list equals the Stripe read.
7. **`/request-setup` emits `setup_requested`; both forms ask the source question.** Today: the page 404s (workstream 10 owns the page). Change: the form posts the anonymous id and touch objects with the answers; the backend validates the id, emails the founders and emits the event; the question is required on the setup form; `/onboarding` adds the same question as optional and fires `source_reported`. Why: number 1 and the "direct" gap. Measure: share of setup requests with a self-reported answer (100%, required) and of new workspaces (target over 90%).
8. **`setup_cta_clicked` and `landing_viewed` on every marketing entry page.** Today: `landing_viewed` fires only on `/`. Change: add the event to the registry and the setup buttons; fire `landing_viewed` on first page view of a session on any marketing route. Why: the funnel. Measure: the funnel query returns a non-zero `setup_clicked` row within a week of the homepage change.
9. **Fix the admin growth funnel's filters.** Today: `ops/growth.py` funnel and source queries filter neither environment nor host. Change: add the shared filter. Why: the admin page overstates the funnel. Measure: admin numbers match the readout query.
10. **Vercel Web Analytics on.** Today: off; package not installed. Change: enable in the dashboard, add `<Analytics />`. Measure: weekly visitors visible in Vercel and within 20% of PostHog's prod `landing_viewed` visitors.
11. **The Monday readout script.** Today: none under `apps/backend/scripts/`. Change: `scripts/monday_readout.py` runs the queries in §4 with the exclusion list, reads Clerk and Stripe, and prints one page of markdown into the founders' channel (not into the repo). Why: the weekly rhythm. Measure: it runs unattended for four Mondays.

## Measures
- The five numbers print every Monday from events, each with named workspaces: setup requests by source, teams formed, first handoff in week one, week-4 active, paying at list.
- Unattributed share of new workspaces (no UTM, no referrer, no self-reported answer) falls from about three quarters to under 20%.
- PostHog paying-at-list equals Stripe; PostHog teams formed equals Clerk.
- Zero `org_` persons and zero dev events tagged `prod` after tasks 1 and 2.

## Founder decisions (13 Oct 2026)

- D12: a rejected approval counts as a completed handoff; a person decided.
- D13: the source question is optional at sign-up and required on the setup form.
- F1: Prasiddha owns the internal-workspace exclusion list.
- F2: no paid PostHog group analytics; a plain `workspace_id` property.
- E10: old `flickmeon.ai` posts are not retro-tagged; every link from now carries a UTM.
- A1 / A4: no guarantee. "Week-4 active" stays as a metric only. Paying at list reads Stripe invoices from the day-31 first charge.

## Sources
- `origin/main` at `6249f614d`: `apps/frontend/src/components/PostHogProvider.tsx`, `apps/frontend/src/lib/analytics.ts`, `apps/frontend/src/lib/signupFlow.ts`, `apps/backend/core/observability/product_analytics.py`, `session_analytics.py`, `core/services/{messaging,run_analytics,user_provision,stripe_webhook_handlers,workspace_join,workspace_invitations,local_sessions,session_links,session_pickup,ops/growth}.py`, `routers/teams/approvals.py`, `routers/ops.py`, `apps/frontend/package.json`.
- PostHog project 380894, read-only HogQL aggregates, 30 days to 13 Oct 2026 (first-party, high).
- Live site, curl 13 Oct 2026: `/pricing` 404, `/request-setup` 404.
- PostHog docs, group analytics: https://posthog.com/docs/product-analytics/group-analytics
- Vercel docs, Web Analytics: https://vercel.com/docs/analytics
- `docs/business-plan.md` §9, Appendix C, Engineering/Product item 7, Go To Market; `.agents/product-marketing.md`.
- Skills: `marketing-skills:analytics`, `marketing-skills:attribution`.
