# Isol8 through the Blitzscaling lens

Living strategy doc. Started 2026-08-17. Framework: Reid Hoffman & Chris Yeh,
*Blitzscaling* (four growth factors, two growth limiters, three innovation types,
firefighting hierarchy).

Metric definitions moved to §3; the admin growth page (`/admin/growth`) and the
founder's Daily Lightbulb Brief implement them.

**Status legend:** `DECIDED` · `LEANING` · `OPEN` · `REJECTED`

---

## 1. Where we are in the framework

Blitzscaling = prioritizing speed over efficiency in the face of uncertainty.
It is the *wrong* mode before product/market fit. Our current position:

| Element | Verdict |
|---|---|
| Market size | ✅ Strong. Riding the model-capability curve for free — every frontier release improves the product at zero R&D cost. |
| Distribution | ❌ **Fire #1.** Latent assets (agent email, channels, Teams seats, marketplace) exist but none is instrumented as a growth loop. |
| Gross margins | ⚠️ Mixed, see §2. Subscription margin is software-shaped; credits are pass-through. |
| Network effects | ❌ Essentially none today. Candidates in §5. |
| **Limiter: PMF** | Unproven — not disproven. Cannot be measured until distribution opens. |
| **Limiter: op. scalability** | Infra cost/ops scale linearly with owners (per-user ECS + EFS). Org is Family-stage with an AI-agent workforce. |

**Market validation ≠ our PMF.** Polsia and Grokbot having traction proves the
category is real (a market-size signal) and starts the first-scaler clock. It says
nothing about whether *our* product retains *our* users. Do not substitute one for
the other in any investor-facing claim.

### The activation thesis `DECIDED` — the most important finding we have

Polsia's public numbers (via the GTM plan's mining): **283,381 companies created vs.
16,447 active (~5.8%); ~50% month-one churn but ~85% month-TWO retention; only ~10%
ever earned a dollar.**

That split is the whole game. A curve that loses half in month one and then holds 85%
is **not** a product the market rejects — it is a product almost nobody successfully
starts. The category has PMF *for the ~6% who activate*.

Three consequences that should govern every decision below:

1. **The winner of this category is whoever solves activation** — not whoever has the
   best agents (Grokbot's axis) and not whoever locks users in hardest (Polsia's axis).
2. Our own funnel says the same thing from the other side: the leak is **pre-payment**
   (7 of 15 external signups never started checkout), not container reliability and
   not OAuth.
3. It validates trial → first successful autonomous run as the metric that decides
   everything, which is exactly what §3 measures.

Per Hoffman's firefighting order (distribution → product → revenue model →
operations → competition → what's next), distribution is correctly fire #1: without
flow we cannot generate PMF evidence at all. Caution: fixing distribution does not
fix PMF (the Groupon failure mode). Open the taps enough to **measure**, watch
retention, then pour.

---

## 2. Unit economics (corrected 2026-08-17)

Read the P&L as **two businesses**, and never blend them:

**A. Subscription business — where the margin dollars live**
- Workspace: $50/mo · Ultra: $150/mo — both seat-free, unlimited members per
  workspace, no per-seat pricing (`CLAUDE.md` Billing section).
- COGS/owner/mo: Fargate Spot 1vCPU/4GB ≈ $13 (on-demand fallback ≈ $42 — Spot
  availability is a real margin variable), plus EFS/NAT/DDB/ALB share ≈ $3–6
- **≈ 60–70% gross margin.** Additional workspace members are near-pure margin
  — they share the owner's existing container at no extra COGS.

**B. Credit business — pass-through, not profit**
- Bedrock credits resold at ~3%. This is **GMV, not revenue**. Report net (the 3%),
  never gross — blending pass-through into revenue destroys the margin optic while
  telling an investor nothing.
- Its job is friction removal (no BYO key needed to start), not profit.
- Consequence: **3% revenue cannot fund a blitzscale.** Only subscriptions,
  marketplace take-rate, and (see §4) financing can.

**C. Subsidy exposure** — the only inference we subsidize is the $3 trial cap.
That is bounded CAC by design. The bedrock-gate reservation model makes heavy paid
users self-funding.

Provider mix as of the last read was ~10 ChatGPT-OAuth : 1 BYO : 0 Bedrock, i.e.
near-zero inference cost to Isol8 today.

---

## 3. PMF evidence — the metric set `DECIDED`

Superseded 2026-09-12: the automations-North-Star metric set below this line
used to live here (chat-session engagement vs. autonomous-run counts). The
2026-09-10 pivot to a native multiplayer messaging workspace replaced it —
"an agent did real work the owner did not prompt" is no longer the value
moment; a human and an agent working together in a shared room is. The
replacement set is the business-metrics section of the observability audit
(`docs/superpowers/plans/2026-09-12-observability-audit.md` §6), adopted
verbatim below.

**Cohorts anchor to the 2026-09-10 messaging pivot** — a workspace's week 0 is
its first exposure to the multiplayer messaging surface; chat-era activity
before that date does not count toward any cohort below.

Stage: 37 workspaces, 4 paying, 5 with any room activity. Focus is
activation and retention, not revenue. Five weekly metrics, computed from the
messaging table (`MSG#` rows carry `authorKind` + `createdAt`) and PostHog:

| # | Metric | Definition | Now | 30 / 60 / 90 day | Decision it informs / if below |
|---|---|---|---|---|---|
| 1 | **Activated workspace rate** | % of new trial workspaces where within 7 days a human sent ≥5 room messages on ≥2 distinct days and received ≥1 agent reply | not instrumented; countable by hand from 104 prod messages | 25% / 35% / 45% | spend on acquisition vs first session. Below: fix the top onboarding wall the Daily Brief names before scaling outreach |
| 2 | **Weekly active workspaces + multiplayer share** | workspaces with ≥1 human room message that week; share with ≥2 distinct human senders | ≤5 WAW; share unknown | 8 / 15 / 30 WAW; 20 / 30 / 40% share | is the multiplayer bet real. Below: in-product invite nudge in `#general` |
| 3 | **Agent answered rate + p50 reply time** | % of agent-addressed human messages answered within 2 min; p50 seconds | unmeasured live; 4 known misses in the DLQ | 95%/60 s → 98%/45 s → 99%/30 s | reliability vs feature spend. Below: treat as an incident (DLQ, worker, wake) |
| 4 | **Paying workspaces + trial→paid by cohort** | count of active subscriptions; conversions ÷ trials by signup week | 4 paying (≈$200 MRR); 7 conversions / ~61 trials ≈ 11% in 30d | 8 / 15 / 25 paying; 12 / 15 / 20% | trial window and cap ($3/day, 3 days). Below: interview the 3 cancellations; extend trial for activated workspaces |
| 5 | **W4 retention of activated workspaces** | % of activated workspaces with ≥1 human message in week 4 | n too small | 40 / 50 / 60% | leaky bucket vs growth. Below: seed the rooms/agents retained workspaces use |

Ignore: landing visitors and raw signups (382 → 63 → 7 says attention is not
the constraint); total messages (agents inflate it — count human messages);
roster size / agent count; Paperclip company count (38, includes tests);
`agent_run_completed` volume (routines and the nightly CEO inflate it — it
was the old North Star and rewards automation churn, not humans coming back).

Events to add (server-side `capture_product_event`, `distinct_id` = sender's
Clerk id, mirroring `run_analytics.py`): `room_message_sent{room_kind,
mentions_agents, is_thread_reply, has_attachment, first_in_room}`,
`agent_reply_delivered{room_kind, latency_ms, event_kind}`,
`agent_dispatch_missed{reason}`, `room_created{kind}`,
`room_member_added{member_kind}`, `workspace_second_human_active` (once),
`reaction_added`, `notification_shown`/`notification_clicked{channel:web|push}`
(client), `message_search_used` (client); redefine
`trial_first_agent_response` as first agent reply in a room; add `reason`
from Stripe `cancellation_details` to `subscription_cancelled`.

---

## 4. Business model options

### 4.0 The three-way positioning map

| | Polsia | Grokbot (SpaceXAI) | Isol8 |
|---|---|---|---|
| Price | free + $49/mo | **$120/seat/mo team; $200–300/mo individual** | $50/mo flat |
| How you buy it | direct | **no standalone plan** — bundled into Cursor Ultra / Cursor Premium Teams / SuperGrok Heavy | direct |
| Usage billing | 20% of all payments | subscription + weekly allowance + on-demand tokens, **no spend cap** | credits at ~3%, **hard $3 trial cap + per-request gate** |
| Where it runs | their umbrella cloud accounts | **macOS only, Apple Silicon only — on your laptop** | per-tenant cloud container |
| Unit | a generated company | a persistent digital coworker operating *your* apps | a company of agents with their own identities |
| Moat | lock-in | model + bundled distribution | ownership + marketplace/labor market |

Three exploitable gaps fall out of that table:

1. **Grokbot's agents sleep when the laptop sleeps.** Running on-device is
   architecturally incompatible with the "works while you're asleep" promise. Our
   entire autonomous-run North Star (§3) is a capability they cannot ship without
   rebuilding on cloud tenancy. This is the single sharpest differentiator we have
   and it should be said plainly in launch copy.
2. **Grokbot has no spend cap; we have a hard one.** Their uncapped meter is already
   the headline critique in third-party pricing coverage. "It cannot surprise you
   with a bill" is a real purchase objection we answer and they do not.
3. **Grokbot is 2.4–6× our price and cannot be bought on its own.** Their
   distribution is bundling (riding the xAI/Cursor base — a legitimate Blitzscaling
   move), but it caps their market at existing subscribers of a developer tool.
   Non-developer SMB owners cannot buy Grokbot at all.

Neither competitor is building multi-tenant agent infrastructure: Grokbot is
single-tenant-on-device, Polsia is umbrella-isolated. **Neither can build an agent
labor market (§5A) without rebuilding their architecture.**

*Polsia detail:* the 20% is enforced by umbrella-account lock-in — their
Render/GitHub/Neon/Postmark accounts, with users given no dashboard. ~48%/mo churn.
*Grokbot pricing sources (2026-08-17):* [eesel AI](https://www.eesel.ai/blog/grok-bot-pricing),
[VentureBeat](https://venturebeat.com/orchestration/spacexais-grok-bot-turns-agents-into-persistent-digital-coworkers-that-can-operate-your-apps-for-120-per-month),
[CellCog](https://cellcog.ai/blog/grok-bot-pricing/). Re-verify before quoting in a
deck — launch pricing moves.

**Isol8's position** — user-ownership ("you own everything — container, repo, data,
domain, 100% of revenue," per shipped GTM positioning). Hoffman's point applies:
**business model innovation defends better than technology innovation, because
technology gets copied.** Our wedge is *aligned* rather than extractive: **do not tax
revenue — finance growth.** Note that revenue share is not merely rejected on
principle; it directly contradicts the "100% of revenue" promise already in market.

### 4.0.1 The paywall inversion `OPEN` — highest-leverage change available

**Superseded by the 2026-09-10 messaging pivot.** This section's framing —
chat is the free commodity demo, automations behind the paywall are the real
product, so trial owners need one unlocked automation to see it — assumed the
old activation thesis. Messaging is now the product surface itself, live for
every workspace including trial, so the "employee is invisible pre-payment"
problem this section solves no longer exists in the same shape. Kept for the
cost-fencing and scheduled-waker analysis below, which still applies to
whatever the next paywall-visibility gap turns out to be; do not re-propose
the automation unlock as written without re-deriving it against §3's
messaging-first activation metrics.

**The value moment is behind the paywall, and the paywall is where we lose people.**

- Automations are paid-only, and correctly enforced in three places
  (`require_always_on_entitlement` on routine create, trigger create, and
  `/automations/from-integration`).
- A trial container scales to zero and wakes only on a `/container/status` poll, so
  an automation **physically cannot fire** for a trial owner.
- Therefore the free tier demonstrates the **chatbot** — the commodity — while the
  **employee**, our actual differentiator and the thing §1's activation thesis says
  decides the category, is only visible after payment.

Proposal: **let a trial owner arm exactly one scheduled automation**, on a backend-
initiated wake, for a bounded window (e.g. 7–14 days).

- **Cost is already fenced.** Inference is bounded by the existing $3 cap. Compute:
  a daily wake→run→sleep of ~15 min on 1 vCPU/4 GB Spot is on the order of $0.15/mo
  per trial owner. Total exposure stays inside the CAC we already accept.
- **The real work is a scheduled waker** — today's wake is poll-driven (client), and
  a sleeping container has nothing to trigger it. The `update-service` scheduled
  worker is the natural host. Verify before scoping.
- **Why it matters:** it converts the trial from "chat with a bot" into "wake up to
  work that got done," which is the actual purchase trigger and the direct answer to
  a category-wide 5.8% activation rate.

**Coordination required before acting:** this proposal touches the same
`require_always_on_entitlement` gates the operating-loops plan (§7) treats as settled
and correct. Two sessions must not reach opposite conclusions about whether trials can
run automations — raise it with that workstream before either side changes the gate.

### 4.1 Local model on the ECS container `REJECTED`

Two independent disqualifiers:

1. **AWS Fargate has no GPU support at all.** Not a tuning problem — the capability
   does not exist on our compute platform.
2. **The economics invert even if we moved to GPU EC2.** Cheapest viable GPU
   instances run ≈ $384–734/mo on-demand per instance (g4dn/g5/g6 xlarge), vs. a
   $50/mo subscription. Spot lands ~$120–220/mo — still 3–4× the plan price. Getting
   to break-even needs ~10–20 users per GPU with continuous batching, which is a
   separate inference-provider business with its own ops burden and idle waste on
   bursty traffic.
3. **CPU-only is worse than it sounds.** 4 GB holds roughly a 3B model at q4. Small
   models fail hardest at exactly our workload (long context, multi-step tool
   calling, instruction following). And we already crash-looped an 11-agent prod
   container by oversubscribing 1 vCPU — stealing that vCPU for inference repeats a
   known outage.

Net: cost is 3–15× worse **and** quality is dramatically worse. Double loss.

### 4.2 Inference margin via arbitrage, not ownership `LEANING`

The instinct behind 4.1 is right — 3% is too thin. The margin is available from
engineering, not hardware:
- **Prompt caching.** We have large stable prefixes (SKILL.md, AGENTS.md, the isol8
  skill, system prompts). Cached input reads are ~10% of standard input cost. Bill
  standard, pay cached.
- **Model routing.** Cheap model for the mechanical majority of agent steps, frontier
  model for reasoning steps. Polsia already does a crude version (strong for
  planning, cheap for routine).
- **Volume commitments** once scale justifies them.

This is a business model *and* a technology differentiator: "your agents cost less to
run here than anywhere else," while our markup goes up rather than down. The
bedrock-gate already meters everything needed to implement and prove it.

### 4.3 Compute credit / "token lending" `LEANING` — strongest new idea

The Shopify Capital / Stripe Capital analog, applied to agent compute.

- **Product:** draw credits now, repay from revenue (fixed % of Stripe volume until
  advance + fee is repaid). v0 could be as simple as a tenure-and-health-scaled
  credit line with net terms.
- **Why we can underwrite better than a bank:** we hold the Stripe integration (real
  revenue), run success rates, automation survival, container uptime, tenure, and
  marketplace earnings. Nobody else sees an AI-native company's actual production
  function.
- **Margin:** MCA-style factor rates (1.06–1.12 over 6–12 months) ≈ 15–25% effective
  APR. On a $10k advance that is $600–1,200 per customer per cycle vs. $600/yr of
  subscription. **Financing can roughly double ARPU** at ~100% gross margin.
- **Flywheel:** more credit → more agent work → more customer revenue → more credit.
  Switching cost becomes financial as well as technical.
- **Risks:** we become a lender. Default risk, capital requirements, and regulation
  (structuring as a purchase of future receivables / MCA is materially lighter than a
  loan, but needs counsel). Start tiny on our own balance sheet; a credit facility
  only after loss data exists.
- **Target the "grow an existing company" door, not "start a company."** Only ~10% of
  companies created on Polsia ever earned a dollar, so revenue-based financing has
  almost no addressable base at the company-formation end. Owners bringing an
  existing business — the second of our two doors — arrive with Stripe history we can
  underwrite from day one. This is the sharpening that makes the product viable early.
- **Sequencing:** underwritable only *after* run-outcome + revenue data exists at
  scale. Phase 2 business — but instrument for it now (§3 largely already does).

### 4.4 Equity in tenant companies `LEANING` — but reframe as distribution

- **Do not buy equity with cash.** Grant compute credits in exchange for **warrants**
  or a small slice, only for companies founded on Isol8. Cash out ≈ 0; COGS of
  credits is near-zero for OAuth/BYO owners.
- Honest framing: an accelerator is a **distribution strategy** wearing an investment
  costume — and distribution is fire #1. A cohort of AI-native companies built on
  Isol8 produces case studies, evangelist founders, and marketplace supply.
- Blitzscaling caution: equity returns are illiquid on a 7–10 year horizon and **do
  not fund growth**. Justify the spend as marketing with optional upside, never as
  the revenue plan. Watch the neutrality conflict — a platform that must serve all
  tenants favoring its portfolio is a governance problem.

### 4.5 Revenue share `REJECTED`

Polsia's 20%-of-all-payments is the model we are positioned *against*. It taxes
success, requires lock-in to enforce, and their churn/review numbers show what users
think of it. Financing growth and taxing growth are opposite postures; we take the
former.

---

## 5. Network effect candidates (ranked)

Our per-tenant isolation is deliberately anti-network-effect at the container layer.
Network effects must therefore live *above* the containers.

**A. Agent labor market — direct + two-sided. The biggest prize. `OPEN`**
Today the marketplace sells agent *templates* (a tarball you copy) — indirect NE with
no ongoing relationship. The upgrade: **agents hired as services, not copied as
software.** Company A's Friday delegates a task to Company B's specialist agent,
which runs in B's container; money moves per task.
- Every new company adds both demand and supply to the labor pool → value of being on
  Isol8 rises with the number of other companies on Isol8.
- Take rate on each transaction is ~100%-margin revenue.
- Neither Polsia (isolated umbrella tenants) nor a single-tenant technology
  competitor can copy it without rebuilding their architecture.
- Primitives already exist: Paperclip models agents/assignments/runs; the trigger rail
  already does authenticated cross-boundary event delivery.

**B. Marketplace liquidity — indirect NE. Built but unlit. `OPEN`**
Needs supply. The §4.4 credits-for-warrants cohort is the supply feed. Ignition
signal: sellers earning real money and repeat buyers.
*Differentiation caveat from the GTM plan:* Paperclip already ships its own company
templates (GStack, 167-agent agency packs), so "we have agents to hire" is **not**
differentiated on its own. What is defensible is the **packaging** — a listing
arrives with its integrations connected and its routines wired, and for
`auto_github` / `auto_vercel` / `auto_google_watch` / `auto_meta` we **register the
webhook for the buyer**. Zapier and n8n make you paste. Never lead with the raw
inbound-event count (480 across 10 providers) — enumerating a vendor's webhook
catalog is not a moat; installing it for the buyer is.

**C. Performance-data network effect — real, and isolation-safe. `OPEN`**
We observe run outcomes across every tenant: which templates succeed, which
automations survive 30 days, which providers produce useful work. This is
**metadata, not customer content** — safe to aggregate. It powers ranking,
"companies like yours hired X" recommendations, and the §4.3 underwriting model.
More tenants → better ranking and better underwriting.

**D. Standards NE — weak alone. `OPEN`**
The agent-slice artifact format as *the* portable agent package. Only matters once B
has liquidity.

**E. Capital lock-in — not a network effect, but a moat.**
A financed company does not leave mid-term. Compounds with A.

**The synthesis:** A + C + 4.3 form one loop — more companies → more agent-labor
supply and demand → more transactions → more outcome data → better underwriting →
more credit extended → more agent work → more companies attracted. Requires (a)
multi-tenant agent infra, (b) revenue visibility via integrations, (c) run telemetry.
**We have all three; no competitor has all three.**

---

## 6. Sequencing

**Hard constraint: the raise is November.** Working backwards, PMF evidence must
exist by roughly October, which means the distribution taps and the §3 run-event
instrumentation need to be live in weeks, not months. Runway and burn (GTM loop 8)
are the first two numbers an angel asks for.

1. **Now:** distribution loops + the §3 instrumentation gap + the §4.0.1 paywall
   inversion. Nothing else matters until flow exists and PMF is measurable.
2. **On PMF evidence:** marketplace liquidity (B), inference arbitrage (4.2).
3. **On liquidity + revenue data:** agent labor market (A), compute credit (4.3).
4. **Blitzscale trigger:** first real marketplace/labor-market liquidity. That is when
   winner-take-most dynamics start and speed-over-efficiency becomes correct — and it
   implies raising *before* that point, not after competitors see it too. The November
   raise is therefore correctly timed *ahead* of the flywheel, not after it.

---

## 7. Reconciliation with the GTM / operating-loops plan

Ref: `docs/superpowers/plans/2026-08-14-gtm-and-operating-loops.md` (rev 5, branch
`plan/autonomous-operating-loops`, actively being worked). That plan and this one were
derived independently and **agree on the load-bearing questions**:

- Its "metrics come from the integrations the loop already needs, never a count the
  loop produces" is the same anti-gaming principle as §3's origin tagging. Its "cost
  per successful completion" is a sharper single number than anything here — adopt it.
- Its `Work product:` requirement (an artifact with a resolving `url`/`external_id`,
  or the run scores zero) is the direct answer to the competitor's task-completion
  illusion (one audit: 41 of 47 tasks marked complete, real success 21.3%, income $0).
- Its nine-loop dogfood is Track 1 R&D; this doc is the business model around it.

**One distinction to keep straight, because the plan slightly blurs it:** the plan's
loop metrics measure whether *our* loops work; §3 measures whether *customers'* loops
work. Different systems. **The bridge is the plan's loop 7 (PostHog activation watch)
— that loop literally IS the PMF measurement system, built as a dogfood loop.** It is
correctly flagged there as the highest-value gap; this doc raises it further, because
it is also the instrument the November raise depends on. Its trigger events are
already available and idle.

**What this doc adds back to that plan:**
- §4.0.1 — the paywall inversion. The plan establishes automations are paid-only and
  physically cannot fire on a trial, and separately establishes the category's problem
  is activation. Those two facts collide and nothing currently resolves the collision.
- §4.0 — Grokbot's on-device architecture as an exploitable, permanent gap.
- §1 — the 85% month-two retention read as category PMF evidence, which is a stronger
  claim than "competitors exist" and belongs in the raise narrative.

---

## 8. Open questions

- Are Polsia/Grokbot *scaling* (raising, growing, retaining) or also searching?
  Grokbot's bundled distribution suggests real reach but caps it at existing
  Cursor/xAI subscribers. Worth a firmer read before setting aggression.
- Does a backend-initiated scheduled wake for trial containers exist or need building
  (§4.0.1)? Blocking question for the highest-leverage change on the list.
- Legal structure for §4.3 (MCA vs. loan) and which state licenses, if any.
- Agent-labor-market trust model: how does Company A verify Company B's agent did the
  work, and what is the dispute/refund path?
- Does the §4.2 caching/routing arbitrage survive contact with OpenClaw's actual
  request shapes, given it sends no `maxTokens` on the adaptive path?
