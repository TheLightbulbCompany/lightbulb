# Onboarding Aha First-Run — Design

**Status:** Draft for review
**Issue:** [#1203 — Onboarding: build a non-technical guided 'aha' demo + rewrite 'How It Works' for operators](https://github.com/Isol8AI/isol8/issues/1203)
**Reference:** [Polsia founder interview + live onboarding demo (Latent Space)](https://www.youtube.com/watch?v=Yw-m0PI2Atk)

---

## 1. What we are trying to do

The July design-partner feedback (Max, Ben) said the value is real but the concept is
abstract: *Friday = CEO, you hire workers* doesn't land until the buyer sees it applied
to **their own business**. Issue #1203 proposed an Apple-store-style guided demo. This
doc replaces that direction with a stronger one, proven by Polsia ($1M ARR ~1 month
after launch, same core proposition as us):

> **The aha moment is not a demo the user watches. It is an autonomous first-run the
> user receives.** Between signup and first chat, Lightbulb turns one line of input into a
> researched, operating company: the user watches Friday come online and do real work
> on *their* business — research, a mission document, a populated task board — before
> they have paid anything or connected anything.

Polsia's onboarding, mechanically: two buttons (create new company / grow my company) →
one text box → then, before payment, it researches the business *and* the signup user,
writes a mission doc, produces a market-research report, creates a company email,
sends a welcome email, posts a tweet, generates a landing page, and seeds a task board
with three starter tasks it chose itself. The paywall sits **after the demonstration,
before the execution**. Their founder's stated design principle: strip every
connect-your-account step ("connect your GitHub" sounds sketchy on a platform claiming
to run your business) and **provision everything instead**.

The construction-business owner from Max's feedback doesn't need to imagine the value.
He types his company's website, and three minutes later his CEO is showing him a
market read on his own trade, with tasks queued. Nothing to imagine.

## 2. Decisions already made

These were settled in design discussion and are not open:

1. **V1 targets new personal signups only**, on the no-card free tier
   (`trial_local`). Teams/org signups and existing users keep the current flow.
2. **Both input paths ship**: "start something new" (free-text idea) and "grow my
   business" (website URL, with a short-description fallback). Both paths are ICP —
   idea-stage founders and operators with existing businesses alike. (Polsia sees
   20–25% choose grow; the grow path demos especially well because the research runs
   on a real business, but neither path is secondary.)
3. **The container does everything. Zero serverless.** No Lambda, no Bedrock
   AgentCore, no backend inference. The free tier auto-provisions the container at
   first poll; the aha is Friday's **first general session**, running in the user's
   own container, on the trial Sonnet AIP, counted by the existing meter Lambda and
   fenced by the existing $3/day cap. The backend's only new jobs are file seeding,
   one Paperclip write, and a session kickoff — all existing capabilities.
4. **Paywall sequencing flips to demonstrate-free → pay-for-autonomy.** Everything
   user-initiated — chat, and user-clicked task runs — is free under the $3/day
   trial cap (this matches shipped gating, §6). `has_always_on_entitlement` gates
   the unattended company: channels, webhooks, routines, always-on. The upgrade
   moment is "keep this running while I'm away."
5. **Everything learned during onboarding lands in OpenClaw primitives** — workspace
   files in Friday's EFS dir, Paperclip issues, her memory files. No parallel
   onboarding state store. The brief the user types IS a file in Friday's workspace.

### The runtime rule (recorded so the slope stays un-slippery)

**User workloads run in the user's container, always.** Serverless agent compute is
permitted only for (a) platform-owned jobs that are not the user's workload, or
(b) a future, data-justified pre-container exception (§9). Our moat is the sovereign,
filesystem-based runtime: marketplace artifacts are workspace tarballs, config is
deep-merged JSON on EFS, memory is user-readable markdown. Managed runtimes
(AgentCore, Claude Managed Agents — which Polsia uses) externalize state behind APIs;
that is coherent for a centralized orchestrator like Polsia and incoherent for us. We
copy Polsia's **sequencing**, not their substrate.

## 3. The experience, end to end

```
t=0      Clerk signup completes → lands on /chat
         · trial_local row auto-created; container provisioning starts immediately
           (existing behavior — first /container/status poll fires ensure_user_ready)
         · Full-screen funnel renders in the chat shell (same slot the
           ProviderPicker occupies today): "Start something new" / "Grow my business"

t≈30–60s User submits the brief (idea text or website URL)
         · Brief written to Friday's workspace on EFS (§4, step B). The board is
           NOT seeded here — it fills at the END of the run.
         · Waiting state: honest copy — "Spinning up your company's container…"
           with the live elapsed clock. No theater.

t≈2–4min Container ready → THE SHOW
         · The frontend auto-fills the chat input with the onboarding prompt and
           sends it, visibly, on the user's behalf (§4.C) — the user sees exactly
           what was asked of Friday
         · Layout becomes a split view (dockview preset):
             LEFT  — Friday's general session (Surface A, §4.H), streaming live
             RIGHT — the Teams board, which fills with the three next-move tasks
                     at the end of the run
         · Friday's first session, research-first (§4.D): understand the business →
           write market-research.md + MISSION.md (watched live in the left pane) →
           propose 3 next-move tasks on the board as the closing output (path-aware:
           website-first for a new business, growth moves for a grow one) → close
           with a summary addressed to the user by name, inviting them to run one.
         · The user can type into the session at any point — it is a normal chat
           session with their CEO, not a replay

after    The coda: the user clicks run on a seeded task → their first Paperclip
         task run (Surface B), watched live in run-detail — still free, cap-fenced
         → the upgrade card ($50/mo) renders when they reach for AUTONOMY:
           a recurring schedule, a channel, "keep going overnight", or more
           runway than $3/day
```

The first minute of dead air is absorbed by the form itself (provisioning starts at
t=0, before the form is submitted). The remaining wait shows honest status. The
climax lands exactly when the product becomes real.

## 4. Sequencing and primitives

Every step names the primitive it rides. New surface is deliberately thin.

**A. t=0 — unchanged provisioning.** First `/container/status` poll →
`ensure_personal_trial_default` → `ensure_user_ready` → synchronous Paperclip seed
(Friday + Atlas exist within seconds, with agent IDs) → `background_provision`.
Nothing in this design delays or forks provisioning.

**B. Form submit → brief lands as workspace files.** New endpoint
`POST /api/v1/onboarding/brief` (personal owners, one-shot, only while no prior brief
exists):

- Writes `BRIEF.md` into Friday's agent workspace dir on EFS via
  `workspace.write_file()` — the same pre-boot seeding window `friday_identity.py`
  uses; OpenClaw's `writeFileIfMissing` honors pre-written files. `BRIEF.md` contains
  the path chosen (new/grow), the user's input verbatim, and their name/email — the
  same personalization Polsia uses to research the signup user, not just the company.
- Selects the first-run variant of `BOOTSTRAP.md` (§ below).
- Does NOT seed any board issue (revised 2026-07-13). The first run is Friday's own
  research session; the board fills at the END with the three next-move tasks she
  proposes (§4.D step 4). The "watch Friday work" moment lives in the left/chat pane
  during research, not in early board writes.
- Submission state = file existence on EFS. No DDB onboarding table, no new state
  store. (Re-render of the form checks brief existence through the workspace read
  path.)

**C. Container ready → visible auto-prompt kickoff (frontend-driven).** When the
frontend observes (container `ready` + brief submitted + first-run not completed), it
**auto-fills the chat input with a specific onboarding prompt and sends it on the
user's behalf, visibly** — the message appears in the transcript as the first thing
"they" said to Friday. No hidden backend dispatch, no backend first-run detection.
The visible prompt carries the human-readable ask — read the brief, research the
business, write the mission + research docs, then propose the next-move tasks — while
the first-run `BOOTSTRAP.md` variant carries the mechanics (the research-first ordering
and path-aware task proposals of §4.D, file paths, Paperclip key loading). Transparency is the point: the user sees exactly
what was asked, learns by example how to drive Friday, and can interrupt or steer at
any moment because this is a normal message in their normal chat session.

Why the main chat session and not a Paperclip-issue-driven run: the left pane must be
conversational — the user should be able to interrupt, ask questions, and steer,
exactly like Polsia's "talk to your AI CEO" panel (their #1 engagement surface:
15 messages/user/day). Issue runs are isolated, non-conversational sessions (§4.H).
The first run stays a plain chat session; its only board writes are the **three
next-move tasks it creates at the end** (§4.D step 4), which the user then runs as
their own Surface-B sessions.

If the user closes the tab before or during the show, the same frontend condition
re-fires on next visit (brief exists, first-run incomplete) and the BOOTSTRAP resume
path (§4.F) backstops a partially-consumed brief.

**D. The run itself — research and docs first, tasks as the closing OUTPUT**
(revised 2026-07-13, PR follow-up to #1240). The first-run `BOOTSTRAP.md` variant
directs her opening session (prompt-work, the same lever `friday_identity.py`
pulls). We deliberately reversed the earlier "board-first" ordering: the two-surfaces
model (§4.H) makes each task its own future Surface-B session, so the first session's
job is to *understand the business* (research + docs, watched live in the left/chat
pane), and its **output** is three next-move tasks the user runs later. The board is
NOT pre-seeded; it stays empty during research and fills at the end. Order:

1. **Greet + start.** One-line greeting to {first_name}, then start working, narrating
   briefly. Read `BRIEF.md`.
2. **Understand the business.** `web_search` (Parallel keyless MCP, configured
   fleet-wide) + on the grow path fetch their site first. This is the heart of the
   session; the left/chat pane carries it live.
3. **Write the docs (the visible research output).** `research/market-research.md`
   (market, competitors, positioning) and `MISSION.md`; fold the mission into
   `USER.md` Context + `MEMORY.md` so it becomes permanent operating context.
4. **Propose three next-move tasks — the closing OUTPUT.** Create them on the board
   (do NOT execute them; each runs later as its own Surface-B session). **Path-aware:**
   on the **new** path the first task is always *"Build [company] website"* (a landing
   page — note the run produces the site in-workspace; going live needs the owner's
   GitHub + Vercel), plus two launch moves. On the **grow** path they already have a
   site — do NOT propose a from-scratch website; propose the three highest-leverage
   *growth* moves the research surfaced (sharpen the existing site/conversion,
   outreach, content, leads, pricing).
5. **Close.** Final chat message: what she learned, the three tasks she queued, and
   the coda invitation — *"pick one and hit run — I'll do it while you watch."* (That
   run is free, §6.) The autonomy tee-up — *"want me to keep this running while you're
   away?"* — is the
   paywall's cue, not hers.

Bounds: the existing trial config bounds (timeout, retry, loop-detection) plus a
brief instruction to keep the run under ~10 minutes of work. Worst case cost is a
normal Sonnet session inside the $3/day fence.

**G. Liveness — why the board updates without a refresh (verified end-to-end).**
Friday creates issues by POSTing to the Paperclip BFF with her claimed API key —
the same route users hit. That route calls `logActivity(action: "issue.created")`
(fork `server/src/routes/issues.ts:4403`), and `logActivity` publishes a
`type: "activity.logged"` live event (`server/src/services/activity-log.ts:87`) —
as do checkout, comment, status-change, and close, which all funnel through
`logActivity`. Our backend's `paperclip_event_client` consumes the BFF's live
stream; `teams_event_broker.py:205` forwards each event as `teams.{type}` with
owner-keyed fanout to the user's WebSocket connections. On the frontend,
`TeamsEventsProvider` maps `teams.activity.logged` → SWR prefix invalidation of
`/teams/issues`, `/teams/inbox`, `/teams/dashboard`, `/teams/activity`
(`TeamsEventsProvider.tsx` `EVENT_KEY_MAP`), so the board panel refetches itself
within ~1–2 seconds of each of Friday's writes. **No new transport, no polling, no
dedicated issue events needed** — there is no `issue.created` live-event type in
Paperclip (`LIVE_EVENT_TYPES` in `packages/shared/src/constants.ts`); the board's
realtime story is `activity.logged`, which is exactly what every step in §4.D emits.

Two implementation requirements this imposes:
- **The split view must hold the teams-events subscription.** `TeamsEventsProvider`
  mounts with the Teams layout and the subscription is refcounted
  (`teamsSubscription`); the show's right pane must mount it (or an equivalent
  holder) for the entire show, including while the left-pane session has focus.
- **The prompt must write early and often** (§4.D ordering) — the pipeline was never
  the gap; today's board looks static during agent work only because nothing writes
  to Paperclip until work completes. This is also why the get-started checklist
  needed a "create a task" step for the user to ever see a run: liveness was always
  event-driven, but nothing seeded the first event. The first-run show replaces that
  workaround.

**Liveness acceptance criteria (ship gates, not aspirations):**
1. Every Paperclip write Friday makes during the show is reflected on the visible
   board within **≤3 seconds**, with zero user action. Measured, not assumed: emit
   an event→refetch latency metric during the show.
2. The teams-events subscription is provably held for the show's full duration
   (regression-testable via the `teamsSubscription` refcount).
3. A WS drop mid-show self-heals: on reconnect the existing full invalidation
   (`ALL_KEYS` on the false→true transition, plus `teams.stream.resumed`) refetches
   the board — the show may show a gap, never a stale pane. Already built; the show
   must not break it by bypassing `TeamsEventsProvider`.
4. E2E proof in CI (hermetic, per the E2E policy): with the board rendered, create
   an issue via an agent API key and assert the row appears in the DOM without
   navigation or reload.
5. Pre-ship manual run of the complete show on dev — watched end-to-end, both
   panes — before this flips on for real signups.

**H. Two execution surfaces — what runs where, and in what order.** This design
deliberately involves two different kinds of agent execution, and confusing them
breaks both the UX and the paywall. Pinned here:

|  | Surface A: general chat session | Surface B: Paperclip task run |
|---|---|---|
| Started by | a chat message (here: the auto-prompt, §4.C) | issue assignment/wake via the openclaw-gateway adapter |
| Session | the user's ongoing conversational session | a **fresh, isolated** OpenClaw session per issue |
| Streams to | the chat pane (left pane of the show) | the run-detail view (`teams.heartbeat.run.*` events) |
| User can talk into it | yes — it's chat | no — it's a reviewable run |
| Paperclip writes | via Friday's claimed API key; board activity, **no run row** | attributed to a run via `X-Paperclip-Run-Id` |
| Free-tier status | free under the $3/day cap | user-clicked: **free under the same cap** (`require_trial_runway`); scheduled/channel-triggered: paid (§6) |

**The order is: A first, B after, never both in the show.** The show is one
Surface-A run that researches and **creates** tasks; those tasks do not execute
during the show. The first Surface-B run in the user's life is the show's coda —
they click run on a seeded task and watch it live in the run-detail view, which is
what task execution actually looks like from then on. Still free, still cap-fenced;
the paywall arrives when they want it *unattended* (§6). The show must not fake a
B-run inside the chat pane, and the left pane will never show B-run content: those
are different sessions by construction (Friday's SOUL.md: each issue arrives as a
fresh session).

**Relation to the get-started checklist:** its "create your first task" step existed
so a user would trigger a B-run and finally see the machine work. Same intent,
superseded mechanism: Friday now creates the tasks (A), and run-witnessing moves to
the execute moment (B) where it belongs. The checklist's derived completion signals
(a chat session exists, ≥1 task on the board) auto-complete as a side effect of the
show.

**E. The split view.** A dockview layout preset in `WorkspaceShell`: session panel
left, Teams board panel right, applied when the first-run kicks off; dissolves to the
user's normal layout after. Right pane liveness rides the existing refcounted
`teams.subscribe` event stream — tasks appear as Friday creates them, no new
transport. Mobile (`ChatLayout`) gets the sequential version: session stream first,
"view your board" card on completion.

**F. Failure handling.** If the handshake never completes (stepper's existing 5-min
soft-timeout), current recovery UX applies unchanged. If Friday's run errors or the
user closes the tab mid-show: the brief and any artifacts are durable on EFS; her
BOOTSTRAP treats an unconsumed `BRIEF.md` as "finish onboarding on next open," so the
show resumes rather than vanishing. No brief (user skips the form — it must be
skippable) → today's flow, Friday's standard greeting.

## 5. Artifacts by phase — worked backwards from the customer

The doc's rule for every artifact: name the customer moment first, then choose the
railway (existing integration vs platform-provisioned build), then phase it.

### V1 — the core three (no new infra)

| Artifact | Customer moment | Railway |
|---|---|---|
| Market-research doc | "It understood my business in 3 minutes" | `web_search`/`web_fetch` + workspace file; viewable in Files panel |
| Mission doc → context | "My company has a brain that remembers" | Workspace file, folded into USER.md/MEMORY.md |
| 3 next-move tasks | "Here's exactly what to do next" | Paperclip issues via Friday's claimed key, created at the END of the run; path-aware (website-first for new, growth moves for grow) |

### V2 — capability demos (no new infra, prompt + UX work only)

- **Draft outreach/welcome email as a document.** Moment: "it wrote my customer
  email." Friday drafts, does not send. The natural upsell seam: *"want me to
  actually send these every week?"* → autonomy paywall, and later the v3 mailbox.
- **Atlas hire recommendations.** Moment: "it's telling me who to hire" — the
  Friday-CEO/hire-workers mental model, taught by doing. Research concludes with
  Atlas proposing 2–3 specialists (marketplace search or scaffold), feeding the
  marketplace funnel.

### V3 — provisioned-identity infra (each its own project; this doc sets direction)

**Per-company email (the big one).**
- *Customer moment:* the company has its own address — Friday sends the morning
  digest from it, agents do outreach from it, replies flow back in. Polsia's daily
  email is their retention engine (their 91-year-old-dad test runs entirely on
  reply-to-email).
- *Railways weighed:* we already have **gog** (per-agent Google OAuth) — but gog is
  the **act-as-me** rail: it sends from the *user's* Gmail identity and requires an
  OAuth connect, exactly the "sounds sketchy" step Polsia's founder stripped from
  onboarding. The company-identity rail must be **provisioned**: SES on a Lightbulb
  subdomain (`friday@{slug}.thelightbulb.company` shape — agent mail lives on the mail domain, never on `flickmeon.ai`), outbound with DKIM, inbound routed
  to Paperclip (email → issue/comment or Friday wake). Both rails coexist — they do
  different jobs (company-acts-as-itself vs agent-acts-as-me); onboarding and the
  daily digest use the provisioned one. **Decision: provision, don't integrate.**
- *Unlocks:* morning-digest loop (the retention surface this whole design points at),
  outreach-with-approval, reply-driven steering.

**Landing page.**
- *Customer moment:* "my company exists — here's its page," plus lead capture on it.
- *Railways weighed:* the **Vercel CLI integration (shipped)** is the power rail —
  but it needs the user's Vercel account: an integration step, wrong for onboarding.
  Platform rail: static publish to S3/CloudFront under a wildcard subdomain
  (`{slug}.sites.flickmeon.ai`) — DNS, CloudFront, and certs are rails we already
  operate in CDK; the new piece is a publish path from Friday's workspace and a
  safety/abuse review (user-authored HTML on our domain). **Decision: platform
  static hosting for the default; Vercel integration remains the power path.**

**Social posting.**
- *Customer moment:* "it's already marketing for me."
- *Railways weighed:* channels (Telegram/Discord/WhatsApp) are conversation rails,
  not publishing rails; X's API is paid and gated; browser-automation posting via
  the desktop pairing is real but too fragile for a first-run promise. Polsia
  half-retreated here too (posts from their own HQ account, no @-mentions after a
  consent backlash). **Decision: defer, revisit after email ships.** V2's drafted
  content ("here are this week's three posts — want them published?") previews the
  value without the rail.

## 6. The paywall moment

Sequencing: **demonstrate free → pay for autonomy.** This is grounded in what the
gates already enforce — no billing-code changes in v1:

- **Free (`trial_local`, automatic, no card), fenced by the $3/day cap:** the entire
  first-run show, interactive chat (Surface A), the seeded board, **and user-clicked
  task runs (Surface B)**. Verified against shipped code: the Teams run-dispatch
  guard is `require_trial_runway` (`routers/teams/_guards.py`, used by
  `teams/agents.py` + `teams/issues.py`) — a **spend-cap** check, not a subscription
  check. The meter Lambda counts every model call from the container — webchat, and
  Paperclip runs with no browser tab — so the cap genuinely fences both surfaces.
  The aha arc therefore runs all the way through *"click run on one of these and
  watch your team work"* (run-detail view) before any payment.
- **Paid ($50/mo): autonomy — the company that works while you're away.**
  Enforcement already shipped: channels (`routers/config.py:153` — the gate's own
  comment: *"bound bots keep triggering runs while the owner is away"*), webhooks
  (`routers/hooks_proxy.py:154`), always-on container (trial boxes scale to zero;
  paid stay up), and scheduled autonomy/routines per the
  `has_always_on_entitlement` contract. The upgrade moment is when the user wants
  it running unattended: a recurring schedule, a channel, "keep going overnight,"
  or more runway than $3/day. Friday's close tees this up: *"want me to keep this
  running while you're away?"*
- **The line, stated once:** who initiates decides the tier. A human click —
  chat message, run button — is interactive and free under the cap. A clock,
  webhook, or channel is autonomy and paid. Demonstrate everything interactively;
  monetize the unattended company.
- The ProviderPicker (card-first checkout) stops being the personal-signup front
  door; it remains the checkout surface behind the upgrade moment and for
  provider choice. Team flow unchanged in v1. Trial-cap limit cards remain the
  UX when the $3/day runway is spent — and are themselves an upgrade surface.

## 7. What this supersedes

- **The get-started widget** (bottom-right checklist) retires from the personal
  first-run path. Its framing — what *you* must do — is the opposite message of what
  *Friday already did*; its 5 steps (meet/chat/task/hire/channel) are all performed
  or previewed by the show itself. "Learn the basics" content survives somewhere
  reachable (likely the help surface).
- **The cold-start stepper card** is replaced by funnel + waiting state + show for
  new personal signups. Recovery, team-pending, and blocked states keep today's UX.
- **"How It Works" (issue #1203 workstream 2)** gets rewritten to narrate Friday's
  first hour from the operator's seat — the landing page describes the first-run,
  because the first-run *is* the pitch. Copy work, same reframe, separate PR.

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Cold-start variance: show opens late at p95 | Instrument handshake p50/p95 from day one (dev first). Form absorbs the head; honest waiting copy for the rest. If p95 > ~4 min sustained, fix provisioning (image size, boot order) before reaching for architecture (§9). |
| Cold-container first run is sluggish (MCP connect storm, boot CPU contention) | First-run prompt orders work so early narration needs no tools (greet + restate brief while tools warm). Known issue #954 tracks the underlying contention. |
| Thin input → generic research ("a social media agency" tells Sonnet little) | Both paths are ICP; the grow path self-grounds via the URL. New-idea path: brief format nudges one clarifying detail at form time (target customer or region — one field, not a wizard). |
| Friday improvises off-script during the most important demo | The run is rail-shaped by BOOTSTRAP (read → research → write → tasks → close), bounded by trial config caps. Accepted residual: she's an agent; mild variance is the authenticity Polsia's demo *sells*. |
| Free-tier abuse: every anonymous signup now triggers real inference | Unchanged exposure class from #1231 — the show runs inside the same per-owner fence ($3/day, Sonnet AIP, meter kill). One-shot brief per owner. Clerk bot protection upstream. |
| User bounces mid-show | Artifacts are durable on EFS + Paperclip; BOOTSTRAP resumes an unconsumed brief on next open. The morning-email loop (v3) is the real answer to "came back later." |

## 9. Pressure valve (explicitly not in scope)

If measured funnel data shows meaningful drop-off in the post-submit wait window
*after* provisioning-time fixes, the escape hatch is a pre-container research stream
(serverless engine behind the same seam: brief in → progress events out → artifacts
to EFS/Paperclip). The seam in §4 is designed so that engine swap touches nothing
else. This is a documented contingency with a data trigger — not a plan, and not v1.
The runtime rule in §2 governs.

## 10. Success metrics

- **Form completion rate** (landed on funnel → submitted brief), split by path.
- **Time-to-show**: signup → handshake p50/p95 (the number that arms/disarms §9).
- **Show completion**: % of first-runs where Friday finishes all core-three artifacts.
- **The aha proxy**: % of users who send ≥1 message to Friday during the show
  (Polsia's leading engagement signal was messages-per-day to the CEO).
- **Conversion**: show completed → coda run clicked (first Surface-B run) →
  autonomy reached-for (schedule/channel attempted, or cap hit) → checkout started
  → subscribed.
- Baseline comparison: existing personal-signup activation (first chat message rate)
  pre/post.

## 11. Open questions (for the implementation plan, not blockers)

1. Exact `BRIEF.md` schema + the one optional clarifying field on the new-idea path.
2. First-run-completed marker: file in Friday's workspace (consistent with §2.5) vs
   deriving from BRIEF consumption — decide in implementation.
3. ~~"Set up" issue checkout limits~~ — moot: the flow no longer seeds or checks out
   a Set-up issue (revised 2026-07-13). The first run is a plain chat session that
   creates the three next-move task issues at the end.
4. Skip-the-form rate to watch: if high, test making the funnel pre-signup (Polsia
   collects the idea *before* auth).
5. Mission-doc naming/placement conventions so marketplace export filters and
   config reconciler ignore onboarding artifacts correctly.
6. ~~One free sampler run?~~ **Resolved:** user-clicked Surface-B runs are already
   free under the cap in shipped code (`require_trial_runway` is a spend guard, not
   a subscription guard) — the coda run is in (§4.H, §6). **Routine sub-question —
   resolved the other way after a corrected analysis:** routine *creation* is
   ungated for trial owners today (`routers/teams/work.py:45` create + `:58`
   triggers; `has_always_on_entitlement`'s docstring claims routines but nothing
   enforces it). An earlier draft said a trial routine "runs inside the cap" —
   **wrong**: trial containers scale to zero, `resolve_running_container`
   (`ecs_manager.py:1415`) returns `(None, None)` for a stopped container, and no
   scheduled-dispatch path ever calls `start_user_service` — so a trial user's
   nightly routine **silently never fires**. That is a broken affordance, not a
   free feature. Decision: **gate routine create + trigger-create with
   `has_always_on_entitlement`** (mirroring the channel gate at `config.py:153`),
   shipped independently of this project (owned by the free-tier workstream). This
   doesn't move the paywall — the capability never worked for trial owners — it
   converts silent failure into an honest 402 upgrade surface, which is exactly
   §6's "reached for autonomy" moment. Known limitation: the raw Paperclip proxy
   remains a side door; a proxy route filter is a follow-up only if usage shows
   people walking around the front gate.
7. Exact auto-prompt copy (§4.C) — short enough to read at a glance, specific
   enough that Friday's run is deterministic; versioned alongside the BOOTSTRAP
   variant so prompt and mechanics never drift apart.
