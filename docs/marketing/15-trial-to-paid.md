# 15 — Trial to paid: the upgrade screen and the week-4 save

## Purpose

Moves "paying $50 a month at list" and "week-4 active" (plan goal; Monday numbers 4 and 5). Serves business plan §7 (the founding-teams offer) and Engineering/Product item 5 (billing matches the plan).

## What we know

Code facts are from `origin/main` at `6249f614d` (13 Oct 2026). Confidence is high for code (first-party) unless marked.

**How the trial ends today**

| Fact | Where | Confidence |
|---|---|---|
| The free trial is a `trial_local` billing row: no card, no Stripe customer, 30 days (`_TRIAL_LOCAL_DURATION_SECONDS = 30 * 24 * 60 * 60`) | `apps/backend/core/repositories/billing_repo.py` | High |
| The clock starts when the provisioner first readies the workspace (`arm_trial_if_unset`, called from `scripts/provision_runner.py`), not at sign-up; a missing `trial_end` is "unarmed", not expired | `billing_repo.py`, `trial_cap.py` | High |
| Expiry is a read-time comparison (`now > trial_end`), no status flip, no worker. `trial_block_reason` returns `trial_expired`; every door (chat gate, provision gate, recover, Teams run guards, voice) uses the same predicate | `core/services/trial_cap.py` | High |
| Inside the window the valve is OpenRouter's per-key daily limit ($0.25/day) on DeepSeek | `CLAUDE.md`, plan Appendix B | High |
| On expiry the trial-idle sweep pauses the company's Paperclip routines and heartbeats; a subscribe re-arms them | `core/services/durable_activity.py` | High |
| Nothing is deleted at expiry: no reaper for expired trials found on `main`. Profiles, messages and the board stay | grep of `apps/backend` for expired-trial delete/wipe paths | Medium (absence of code) |
| Several docstrings still say "3-day window" (`trial_cap.py`, `provision_gate.py`) | same | High, cosmetic |

**What the person sees**

| Surface | Copy today | Where |
|---|---|---|
| Sidebar footer, days 1–26 | "N days left on your free trial · See plans" | `components/chat/FreeTierNote.tsx` |
| Sidebar footer, last 3 days | "N days left on your free trial · Choose a plan" (warning colour); last day "Last day of your free trial" | same (`NUDGE_DAYS = 3`) |
| After expiry, above the composer | "**Your free trial has ended.** Agents won't reply until you choose a plan. Messages to people still send." + "Choose a plan" | `components/messaging/RoomLimitBar.tsx`, `ChatErrorCard.tsx` |
| Blocked card (provision gate / chat gate) | "Your free trial has ended — Pick a plan to keep your agents." CTA "Upgrade" → `/settings?panel=billing` | `core/services/provision_gate.py`, `chat_gate.py` |
| Room note when a run is refused | system line with "Upgrade" link | `core/services/messaging.py` (`BUDGET_BLOCK_LINKS`) |
| Billing page (admin) | "Your free trial has ended / Pick a plan to keep your agents running." then "One price per workspace, with unlimited members." then three cards | `app/settings/page.tsx`, `components/settings/PlanChooser.tsx` |
| Plan cards | **Base** $20 (managed DeepSeek within a daily allowance; or ChatGPT/own key; always-on agents). **Workspace** $50 and **Ultra** $150 both read "Everything in Base · Prepaid credits for Lightbulb-managed models" | `PlanChooser.tsx` |
| Billing page (member) | "Billing restricted. Contact your organization admin…" | `settings/page.tsx` |
| iPhone | Gate copy "Your free trial has ended, so your agents are paused for now." → `PaywallScreen`: "Choose a plan", three products, "Buy", "Or pay $X/month on the web", "Cancel anytime in iOS Settings." (IAP off in prod) | `apps/mobile/src/lib/purchaseCta.ts`, `components/billing/PaywallScreen.tsx` |
| Email to the agent from an allowed sender after expiry | "Your free plan has ended, so I can't run right now. Upgrade and I'll pick this straight back up" | `core/services/agent_mail_autoreply.py` |
| Email from us at day 27, 29 or 30 | **None.** `trial_will_end` fires only for legacy Stripe trials (`trialing`); `TrialBanner` matches only that status | `stripe_webhook_handlers.py`, `TrialBanner.tsx` |

**Checkout and cancellation**

| Fact | Where |
|---|---|
| `create_workspace_checkout` sells one recurring price per workspace: `STRIPE_BASE_PRICE_ID` / `WORKSPACE` / `ULTRA` ($20 / $50 / $150), unlimited members, **billed at once** (no `trial_period_days`, 14-day Stripe trial removed 17 Jul) | `core/services/billing_service.py`; plan Appendix B |
| Every workspace Checkout sets `allow_promotion_codes=True`: anyone holding a code can type it | `billing_service._build_subscription_checkout` |
| The launch competition mints its own 100% "launch free month" coupon, restricted to the Workspace product, redeem-by 31 Oct | `core/services/launch_settings.py` |
| Cancellation is the Stripe customer portal behind "Manage Payment". No exit survey, no save offer, no pause. On `customer.subscription.deleted` we emit `subscription_cancelled`, reset the plan, and tear down the runtime **without** deleting profiles | `billing_service.py`, `stripe_webhook_handlers.py`, `entitlement_teardown.py` |
| No refund code path and no self-serve export route exist | grep of `apps/backend/routers` |
| Seat counting from the 6th member does not exist | plan Engineering/Product item 5 |

**Business facts** (plan §7, §9, Appendix B; first-party, high): the plan's price is $50 a month for up to 5 members, +$20 per member from the 6th; the product still bills $20/$50/$150 unlimited. $55 collected ever; six subscriptions, all on a 100% internal coupon; 0 paying at list. Trial funnel: 20 trials → 10 first agent reply → about 5 active. The guarantee as written ("not using Lightbulb in week four") is dropped (A1). `thelightbulb.company/pricing` returns 404 (checked 13 Oct).

## Findings (ranked by impact)

1. **The week-4 guarantee refunded nothing, and it is gone.** Week four is days 22–28; the first invoice is day 31, so a team "not using it in week four" had paid $0. The free month is the risk reversal. "Week-4 active" stays as a metric only.
2. **There is no card-on-file trial.** Checkout bills immediately. The founding offer ("card on file, then $50") cannot be sold in the product today; by the fix-forward rule the trial checkout ships before the first setup.
3. **The ask comes with no value shown.** The paywall skill's first rule is value before the ask, and "summarise value received" at trial expiry. Every trial-end surface says what stops ("agents won't reply") and nothing says what the team did. Nothing is emailed; a person who is away meets the end mid-conversation.
4. **Three plans, two indistinguishable.** Workspace and Ultra carry identical feature lines and differ only by $100. Base at $20 is cheaper than the plan's price and is leaving the list (item 5). The skill warns against confusing plan selection; the plan wants one: $50.
5. **The words threaten loss that does not happen.** "Pick a plan to keep your agents" implies the agents go. In fact nothing is deleted, people keep messaging, and the board stays. Today's expired state is already close to the reading-only state this file proposes; it is described as a wall.
6. **No save exists.** Cancellation is one click in the Stripe portal with no reason asked. For ten hand-set-up teams the save should be a founder, not a form.
7. **Promotion codes are open to everyone.** "No coupons for outside teams" is not enforced: Checkout accepts any valid code.
8. **Members are left out.** A member sees "Billing restricted" and an "Upgrade" link they cannot use. The person who did the work cannot ask for the plan in one tap.

## Recommendation

- **One plan on every upgrade surface (A9):** "Team — $50 a month for up to 5 people, $20 a member from the 6th." Base ($20) and Ultra ($150) are retired fully; nobody outside pays for them. Internal rows move to the one plan on the internal coupon. iOS sells one product.
- **Lead with the record.** Day 27 email (workstream 12), day 28 in-app recap card with the team's own numbers, then the plan screen.
- **Day 31 is reading-only, not a wall.** Keep everything, keep people talking, stop agent work; say so plainly; one button.
- **Founding teams (A4):** card taken at the setup session through a Stripe checkout with a 30-day trial; first charge on day 31. Price locked two years. No discounts, no coupons.
- **Build order (fix forward):** seat billing and the trial checkout ship before `/pricing` and the one-plan chooser. No interim line.
- **No guarantee and no refund promise (A1).** Instead: the day-28 recap card, reading-only on day 31, one 30-day pause per company for a timing lapse (A12), 90 days reading-only then the standard deletion notice (A13), and a short note that there is no refund policy beyond ordinary billing disputes. Rejected: a money-back promise of any length, and a discount save offer (§7 rules out discounts; a discount does not save someone who is not using it).
- **Save = a founder check-in** triggered by four lapse signals, not an in-app offer. A cancel survey in our own UI waits until after the ten teams.
- **Coupons: internal workspaces only, by procedure,** and promotion-code entry switched off in Checkout. No coupon is owed to anyone: the launch competition is not set up (A10).

## Deliverable

### A. The trial-to-paid sequence

| Day | Channel | What |
|---|---|---|
| 1–24 | Sidebar | Unchanged: "N days left on your free trial · See plans" |
| 25 | Sidebar | Warning state with "Choose a plan" (today it starts at 3 days left; set `NUDGE_DAYS` to 5) |
| 27 | Email (owner + every admin) | The week-four email, owned by **workstream 12** (`docs/marketing/12-lifecycle-emails.md`). This file supplies its numbers block and its single link (the recap card). Founding teams: the email replaces "choose a plan" with "your first $50 is on [date]; nothing to do" |
| 28 | In-app card (every member) | The recap card, below |
| 28–30 | Sidebar + billing page | Plan screen |
| 31 | In-app | Reading-only state, below. No email that day; workstream 12 owns any day-38 note |

Rule from the skill: show the card once per person; dismiss hides it for 3 days; it never covers a conversation.

### B. Day-28 recap card

Shown at the top of the channel list (not over a room), to every member; admins get the button, members get "Ask [admin name]".

> **Here's what your team did in 30 days**
> {handoffs} handoffs · {approvals} approvals · {agents} agents did work for {people} people
> {top_item}  — e.g. "Finance drafted 3 invoices; Anna sent them."
> Keep going for $50 a month. People are free up to five.
> **[Keep the team]**   Not now
> Your trial ends {date}. After that everything stays readable; agents stop until someone picks the plan.

Numbers rules: show a line only if its count is above zero; if handoffs and approvals are both zero, replace the numbers line with "Your agents haven't finished anything for the team yet. Want help setting up one real task?" and a "Book 20 minutes" link (the save, not the sell). Definitions follow plan Appendix C:

| Number | Definition | Source today |
|---|---|---|
| handoffs | approval cards answered + sessions picked up/continued + agent tasks a person moved on, in the window | Paperclip approvals and issues API; `session_picked_up` event |
| approvals | approval cards a person approved | Paperclip approvals |
| agents | distinct agents with at least one completed room run or task | messaging run ledger `OWNER#/RUN#` rows (check TTL), Paperclip issues |
| people | distinct humans with a room message | `lightbulb-{env}-messaging` |
| top_item | the most recent approved item's title | Paperclip |

### C. The plan screen (web billing page and iPhone paywall)

> **Keep your team on Lightbulb**
> $50 a month for up to 5 people. $20 a month for each member from the 6th.
> Includes: your six AI employees, their memory and email, the shared browser, the task board, the always-on runtime.
> AI usage: bring your ChatGPT account or API key at no charge, or use Lightbulb credits at the model's price plus 20%.
> **[Continue for $50 a month]**
> Billed monthly. Cancel any time from this page; your workspace stays readable.

One card. No Base or Ultra card and no "Current plan" card for either; both are retired. Members see the same block with "Ask {admin} to continue" (sends the admin a needs-you item).

### D. Day 31 for a workspace that does not choose: reading-only

What stays: every message, file, board task, memory, agent profile, and the people's ability to message each other. What stops: agent replies, routines, nightly CEO run, agent email replies (already the case). The workspace stays reading-only for 90 days (A13), then the standard deletion notice goes to the owner before anything is deleted. Nothing deletes today.

Bar above the composer (replaces today's RoomLimitBar line):

> **This workspace is reading-only.** Your team's record is all here, and people can still message. Agents start again when an admin picks the plan.  **[Continue for $50]**

Blocked card and room note: "Reading-only — agents start again when the plan is on." Agent auto-reply email: "This workspace's trial has ended, so I can't work on this. When {owner} continues the plan I'll pick it straight back up."

### E. Founding-team variant

At the setup session the founder opens the Stripe checkout with a 30-day trial and the card collected (first charge day 31). Then:

- Day 27 email: "Your first $50 is charged on {date}. Nothing to do. Your price is locked until {date + 2 years}."
- Day 28 card: same recap, button reads "See billing"; no plan picker.
- Billing page line: "Team — $50 a month, locked until {date}."
- Price lock: the subscription stays on today's $50 Price; record `price_lock_until` in subscription metadata; any future price change skips subscriptions whose lock has not passed.

### F. The week-4 save

**Lapse signals** (read Thursday from the founding-team readout; any one triggers the check-in):

| Signal | Definition | Why it predicts a lapse |
|---|---|---|
| One person only | fewer than 2 humans with a message in days 1–21 | every retained workspace had a second person (plan §9, E/P item 1) |
| No handoff | zero handoffs (Appendix C) by day 14 | the product earns its place only where people share work (§9) |
| Quiet week | no human room message in the last 7 days | the plan's weekly-active definition |
| Stop-heavy week | a person stopped 3+ agent runs within 60 s in a week | Appendix C "stop rate after Jev runs"; agents talking when not wanted (E/P item 8). Needs the Stop attribution in item 8 to be counted |

**Founder check-in script** (in the team's shared `lightbulb-<slug>` channel, then a call; Aden owns it per plan GTM):

1. "We saw {signal in plain words, e.g. 'nobody but you has been in since the 3rd'}. Is that right?"
2. "What were you hoping it would do for the team this month?"
3. Pick one: *One person* → "Who should see this with you? I'll add them now." *No handoff* → "Give me one real task this week; I'll set it up so it ends in an approval for {person}." *Quiet* → "Is it timing or is it not useful? If timing, we can pause for 30 days." *Stop-heavy* → "Which agent is getting in the way? I'll switch it to Mentions in that room."
4. Ask the week-4 question: "How would you feel if you could no longer use Lightbulb?" (very / somewhat / not disappointed). Record it.
5. If they want out: "Then we'll cancel it today. Your workspace stays readable." Never argue a second time.

Pause, offered only for "timing": one 30-day pause per company (A12), by extending the trial (pre-charge) or pausing collection in Stripe (post-charge).

**Pause, reading-only and deletion: the rules.** (a) One 30-day pause per company, offered only when the lapse is about timing. (b) At day 31 without a plan, or after a cancellation, the workspace is reading-only for 90 days; the record stays and people can still message. (c) After 90 days the owner gets the standard deletion notice before anything is deleted. (d) There is no refund policy beyond ordinary billing disputes: a charge the customer did not expect or a duplicate charge is refunded through the normal Stripe dispute route.

**Runbook: pause or cancel in Stripe** (a founder, never an agent):

1. Stripe Dashboard → Customers → find by the workspace id in subscription metadata (`workspace_id`).
2. Pause: pause collection for 30 days, once; record the date in the founding-team readout. Cancel: Subscription → Cancel at period end. This fires `customer.subscription.deleted`; our handler tears down the runtime and keeps profiles (verify the workspace shows reading-only).
3. Post "Paused until {date}" or "Cancelled, record kept for 90 days" in their channel.
4. Export: until an export exists, a founder sends the room transcripts and board tasks on request (build task 9).

### G. The coupon rule

1. The 100% internal coupon applies only to workspaces in the plan's internal list (Appendix C exclusions). Nobody types it: it is applied by a founder from the Stripe Dashboard directly to the subscription (or as a promotion code restricted to that one Stripe customer, which Stripe supports).
2. Workspace Checkout stops accepting typed codes (`allow_promotion_codes=False`).
3. Monthly, a founder lists active subscriptions with a discount; any outside workspace on one is moved to list at its next renewal with a personal note.
4. Outside teams never get a coupon. The free month is the risk reducer; the single 30-day pause is the only concession.
5. No other coupon exists. The launch competition is not set up (A10), so no launch coupon is owed.

## Build tasks

| # | Title | Today | Change | Why | Measure |
|---|---|---|---|---|---|
| 1 | Seat billing from the 6th member (first) | Unlimited members, one price | Membership webhooks (`organizationMembership.created/deleted`) set a second subscription item `quantity = max(0, members − 5)` on a $20 Price, `proration_behavior=create_prorations`; Apple-billed rows excluded until IAP has a seat product; billing page shows "6 people: $50 + $20" | Plan price. Built before the first 6th member and before `/pricing` | Seat revenue; no invoice disputes |
| 2 | Trial checkout, card at setup (before the chooser) | Checkout bills at once | Stripe checkout whose trial ends on the workspace's own `trial_end` (first charge day 31), used by founders at setup and by a self-serve team that adds a card from the day-28 card; row stays `trial_local` until day 31, then live. No founder-by-hand interim | Founding offer is unbillable otherwise | Founding teams with a card at setup |
| 3 | Price lock metadata | none | `price_lock_until` on the subscription; a test that a price migration skips locked subs | §7 two-year lock | — |
| 4 | One-plan chooser (after 1 and 2) | Three cards; Workspace = Ultra copy | One "Team $50 for up to 5, $20 a member from the 6th" block (copy §C); no Base or Ultra card, no "Current plan" card for either; internal rows moved to the one plan on the internal coupon; `PaywallScreen` and iOS sell one product | Confusing plan choice; Base and Ultra retired | Chooser → Checkout start rate |
| 5 | Trial recap endpoint + day-28 card | No value summary anywhere | `GET /billing/trial-recap` returns handoffs, approvals, agents, people, top_item for the trial window (Paperclip + messaging table + run ledger; cached per day); web card at top of the channel list, days 28–30, once per person, 3-day dismiss; iPhone same block on Home | Value before the ask | Card → "Keep the team" click rate; trial → paid |
| 6 | Reading-only state copy | "Pick a plan to keep your agents" | Copy §D on `RoomLimitBar`, `ChatErrorCard`, provision/chat gate, room note, iPhone `purchaseCta`, agent-mail auto-reply; members get "Ask {admin}" that files a needs-you item | Nothing is lost; say so | Reactivations after day 31 |
| 7 | Pause, reading-only and deletion runbook | none | `docs/runbooks/pause-and-reading-only.md` with the §F steps; a `paused_at` field so a company can pause once; the 90-day reading-only clock and the standard deletion notice | Rules A12, A13 | Pauses per month |
| 8 | Close promotion codes | `allow_promotion_codes=True` | `False` for workspace Checkout | Coupon rule | Discounted outside subs = 0 |
| 9 | Workspace export | no route | Owner-only download of room transcripts + board tasks as a zip | A team leaving can take its record | Exports requested |
| 10 | Lapse-signal readout | none | Thursday readout per founding team: humans active, handoffs, last human message, Stops in 60 s (needs E/P item 8 Stop attribution) | Save before cancel | Check-ins sent within 2 days of a signal |
| 11 | Docstring fix | "3-day window" in `trial_cap.py`, `provision_gate.py` | Say 30 days | Next reader | — |

Sending the day-27 email is workstream 12's build task; it needs task 1's endpoint.

## Measures

| Measure | Definition | Target (stage 1) | Monday number |
|---|---|---|---|
| Trial → paid at list | Workspaces with a full-price paid invoice ÷ trials that reached day 31, by cohort | Founding teams 6 of 10 by May (plan); self-serve tracked, no target | Paying at list |
| Week-4 active | Plan Appendix C | 25% of teams formed (stage-2 gate) | Week-4 active |
| Recap card → continue | Clicks on "Keep the team" ÷ card views (admins) | Baseline first month | — |
| Saves | Teams with a lapse signal still active 14 days after the check-in ÷ teams flagged | Report, no target (n ≤ 10) | — |
| Pauses | Companies that used the one 30-day pause, and how many are paying 30 days later | Report | Paying at list |
| Reactivations | Reading-only workspaces that pick the plan within 30 days | Report | — |

Events needed (workstream 09): `trial_recap_shown`, `trial_recap_clicked`, `plan_screen_viewed`, `reading_only_entered`, `save_checkin_sent`, `pause_used`; `trial_converted` only on a paid full-price invoice (plan item 7).

## Founder decisions (13 Oct 2026)

- A1: No guarantee. The free month is the risk reversal; "week-4 active" stays as a metric.
- A2: Moot (no guarantee).
- A4: Card at setup; Stripe checkout with a 30-day trial, first charge on day 31, built before the first setup.
- A7: Moot: seat billing is built before the first 6th member.
- A8: Seat billing first, then `/pricing` with the plan's price; no interim line.
- A9: Base and Ultra retired fully; one plan; internal rows move to it on the internal coupon.
- A10: Nothing to honour: the launch competition is not set up; it is post-product work.
- A11, A14: Referrals and the referral credit are stage 2.
- A12: One 30-day pause per company.
- A13: 90 days reading-only, then the standard deletion notice.

## Sources

- Business plan `docs/business-plan.md` §7, §8, §9, Appendix B, C, Engineering/Product items 1, 5, 7, 8 (first-party, high).
- `.agents/product-marketing.md` (business model, objections, anti-persona).
- `docs/marketing/01-marketing-plan.md` (week 8 save play, open question 2).
- Code on `origin/main` `6249f614d`: `apps/backend/core/repositories/billing_repo.py`; `core/services/trial_cap.py`, `provision_gate.py`, `chat_gate.py`, `messaging.py`, `durable_activity.py`, `billing_service.py`, `stripe_webhook_handlers.py`, `entitlement_teardown.py`, `launch_settings.py`, `agent_mail_autoreply.py`; `apps/frontend/src/components/settings/PlanChooser.tsx`, `chat/FreeTierNote.tsx`, `chat/TrialBanner.tsx`, `chat/ChatErrorCard.tsx`, `messaging/RoomLimitBar.tsx`, `app/settings/page.tsx`; `apps/mobile/src/components/billing/PaywallScreen.tsx`, `src/lib/purchaseCta.ts`.
- Live site: `https://thelightbulb.company/pricing` returned 404 on 13 Oct 2026.
- Stripe docs (opened 13 Oct): refunds go "back to the original payment method", credit "approximately 5-10 business days later" — https://docs.stripe.com/refunds; promotion codes can "Restrict to a specific customer" — https://docs.stripe.com/billing/subscriptions/coupons; quantity "often described as 'per-seat licensing'" — https://docs.stripe.com/billing/subscriptions/quantities; prorations and `proration_behavior` — https://docs.stripe.com/billing/subscriptions/prorations; cancel — https://docs.stripe.com/billing/subscriptions/cancel; trials — https://docs.stripe.com/billing/subscriptions/trials. (Medium: one source each, the vendor's own.)
- Method: `marketing-skills:paywalls` (value before ask; one plan; escape hatch; trial-expiry summary of value) and `marketing-skills:churn-prevention` (risk signals; match the save to the reason; personal outreach for small high-value accounts; no guilt copy; easy cancel). Skill benchmarks are not used as targets: n = 10.
