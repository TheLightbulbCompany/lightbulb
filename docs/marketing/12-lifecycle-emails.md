# 12. Lifecycle emails: the first 30 days, from the CEO's address

## Purpose
Moves goal 1 (second person within 48 hours, completed handoff in week one, still active in week four) by email, plan stage 2 "Owned". Serves product items 1, 3 and 8 and the Monday numbers team formed, first handoff, week-4 active.

## What we know
Verified on `origin/main` (12-13 Oct), not on the live site.
- **Welcome** (`core/services/welcome_email.py`) fires from the Clerk `user.created` webhook for every sign-up, from `hello@thelightbulb.company`, reply-to `support@`. It is single-player and partly untrue today: "a container that belongs only to you" (the workspace is an AgentCore runtime), "the CEO wakes overnight and emails you" (armed for live owners, but whether it mails is the agent's call), and "hire a specialist from the marketplace" (no curated agents are listed; Friday and Atlas are not ingested). It never mentions a second person. High confidence.
- **Daily digest** (`daily_digest.py`): one per person per workspace at 08:00 local, only when approvals, assigned issues or mentions are unseen; zero items sends nothing; push plus email plus desktop event; RFC 8058 one-click unsubscribe; per-day claim row makes duplicates impossible; non-prod mails only `+clerk_test` and `@thelightbulb.company` addresses. This is the pattern to copy. High.
- **Agent mail**: one address, `agents@{slug}.thelightbulb.company`, agents send under their own display name (`agent_email.py`). `send_to_owner` (`agent_mail_owner_send.py`) mails the workspace owner only, purposes `first_contact` or `routine`, 50 sends a day cap, first contact claimed once. Inbound to the address goes to the CEO. Every agent-authored send carries `Auto-Submitted` (RFC 3834). The address row is created on first use so the slug follows the company name the CEO chose. High.
- **Transport**: SES, per-owner SES tenants, sending identity on `thelightbulb.company`; `email_service.send_email` adds `[ENV]` subject tags off prod and accepts custom headers. High.
- **PostHog Workflows**: not referenced anywhere in code or docs. PostHog is capture-only server-side (`product_analytics.py`); events that exist: `agent_run_completed`, `trial_converted`. Team formed, first handoff, setup requested and week-4 active are item 7, unbuilt. High.
- **Billing**: one plan, $50 for up to 5 people and $20 a member from the 6th (Base and Ultra retired). Seat billing and the trial checkout ship before any email names the price. There is no guarantee.

## Findings (ranked)
1. Every email except the welcome needs state we hold in the backend (member count, approvals answered, last human message, trial dates). PostHog cannot see them; the backend can. Build on the digest sweeper, not PostHog Workflows.
2. Suppression is the product. A nudge for something already done reads as spam from a colleague.
3. The existing owner-send door reaches the owner only. The away email and the teammate welcome need a send-to-member door. Invited members do not get the owner nudges.
4. A reply must land somewhere real: it goes to the CEO via the team address. Unverified: whether a non-owner member's reply is accepted (it depends on `sender_allowed` and `verified_senders`).

## Recommendation
Ship 9 emails, each one job, all from "{CEO name}" at `agents@{slug}.thelightbulb.company`, plain text first. Replace the welcome with the CEO's first contact (reuse `send_to_owner` purpose `first_contact`, which already fires once). Build one sweeper (`lifecycle_email.py`, same pass-claim and per-(person, workspace, email-id) claim row as the digest). One lifecycle email per person per day; nothing sends twice. Rejected: PostHog Workflows (no state access, no code precedent), a drip series of tips (no job), HTML-heavy templates (the voice is a colleague).

## Deliverable

### 1. Map
| # | Email | Trigger | Suppress if | Job |
|---|---|---|---|---|
| E1 | First contact | Workspace ready, +10 min | They already typed in #general | Get one finished thing |
| E2 | Invite nudge | 24 h after ready, still 1 human | 2+ humans, or invite sent | Second person in |
| E3 | First handoff | Day 3, no approval answered and no session continued | Any handoff done | First approval |
| E4 | Away: DM, @mention, approval | Event, +5 min unread | Read, app/desktop connected, push sent, setting off, quiet hours | Open that message |
| E5 | Week 2 summary | Day 14 | Zero finished work (send nothing) | See the team's work |
| E6 | Week 4 question | Day 24 | Already answered | One honest answer |
| E7 | Trial ending | Day 27 | Already paying | Choose |
| E8 | Founding check-in | Thursdays, founding teams | In the shared channel that week | Keep the thread |

(E4 has three variants: DM, mention, approval. Day numbers count from workspace ready, not sign-up. Cap: one E1-E3, E5-E8 per person per day; E4 after five minutes unread, never between 22:00 and 08:00 in the person's time zone, at most one per room per 30 min. E2, E3, E5-E7 go to owners only; invited members get a teammate welcome and E4. The welcome is E1, from the CEO agent's address in the agent's voice; it replaces today's welcome entirely. A lapse about timing gets one 30-day pause per company; a reading-only workspace is kept 90 days, then gets the standard deletion notice.)

### 2. Copy
Signature on all: the CEO's name. Footer on E2-E8: "Turn these emails off: {link}". Words in {braces} come from real data; if a value is missing the email does not send.

**E1. Subject: "{company} is set up. Start with this."** Preview: one finished thing, one decision.
> {first}, your team is in #general. Tell me the one thing you want off your plate this week and I'll have a draft back in the room before you finish your coffee. You make one decision; I do the rest.
> What is it?
> Open #general: {link}

**E2. Subject: "Who should see this with you?"**
> {first}, {company} has one person in it. Work an agent finishes lands better when a second person can see it and answer it. Send one invite and I'll show them what's already done.
> Who should be in here?
> Invite someone: {invite link}

**E3. Subject: "One thing is waiting on you"**
> {first}, {title of the approval}: {one line what it is}. It takes one tap to approve or change it. Until then I stop.
> Open it: {link to that card}

**E4a. Subject: "{Name} sent you a message"** (mention: "{Name} mentioned you in {room}")
> {Name}: "{first 140 characters}"
> Reply in {room}: {link to that message}
> Turn these off: {setting link}

**E4b. Subject: "Approval waiting: {title}"**
> {Agent} needs your yes on {title}. Nothing happens until you answer.
> Open it: {link}
> Turn these off: {setting link}

**E5. Subject: "What your team did in two weeks"**
> {first}, {n} things finished, {m} are waiting on you. Most recent: {title}, {title}, {title}.
> The {m} are the ones I can't move without you. Start with {oldest}: {link}

**E6. Subject: "A question, and you can answer in one click"**
> {first}, how would you feel if you could no longer use Lightbulb?
> Very disappointed: {link}
> Somewhat disappointed: {link}
> Not disappointed: {link}
> I've stopped using it: {link}
> Or reply in your own words. A person reads the answers.

**E7. Subject: "Your free month ends {date}"** (founding variant)
> {first}, your 30 days end {date}. Your card is charged on day 31: $50 a month for up to 5 people and $20 a member from the 6th, locked for two years.
> Keep {company}: {link}
> Self-serve variant: same first line, then "Add a card to keep your agents working: {link}" with the price from `/billing/account`; no lock sentence.

**E8. Subject: "Thursday: what's blocking {company}?"** (drafted by the Growth agent, sent by the founder who runs that team, in the shared `lightbulb-{slug}` channel; email only if absent that week)
> {first}, what slowed your team this week? One line is plenty. I'll answer today and take the fix to the build list.

### 3. Deliverability
- Send through SES from `agents@{slug}.thelightbulb.company` (verified domain identity; per-owner tenant already attached). Display name = the CEO. Reply-to = the same address, so a reply is a message to the CEO and lands in the Agent mail inbox; do not point lifecycle mail at `support@`.
- Plain text first with a short HTML twin. No images, one link per email (E6 has four, each one option).
- Headers: `List-Unsubscribe` and `List-Unsubscribe-Post` as in the digest, with a new token kind per category so a token turns off only its own category. E4 gets its own setting (item 3's "one setting"). Transactional E4 still carries the line.
- `Auto-Submitted` is stamped on agent mail. Whether it hurts inbox placement for lifecycle mail is unverified; seed-test Gmail and Outlook before volume and decide then.
- The tenant cap is 50 a day; lifecycle mail is under 1 a day per person. Watch bounce and complaint rates through the existing SES events webhook; pause a workspace's lifecycle mail on a complaint.
- Off prod, mail only reaches `+clerk_test` and company addresses (existing `_email_allowed`); test there.

### 4. Build tasks (Forge-ready)
1. **Lifecycle sweeper.** Today: only the digest sweeps. Change: `lifecycle_email.py` on the digest pattern (pass claim, per-(user, workspace, email id) conditional put, 60 s tick) evaluating E1-E3, E5-E7 (owners only) from workspace-ready time. E7 ships with the trial checkout. Why: suppression needs backend state. Measure: sends per email, suppressions by reason.
2. **Send-to-member door.** Today: `send_to_owner` reaches the owner only. Change: `send_to_member(owner_id, user_id, ...)` from the team address under the CEO name, same cap and metrics. Measure: result codes.
3. **Away notifier (E4).** Today: toast, digest, push. Change: on DM, mention and approval, wait 5 min, send if unread and no push token and not connected; skip 22:00-08:00 in the person's time zone; at most one per room per 30 min. Needs a presence signal: AppSync has no disconnect callback, so use last read cursor and last live-event activity; mark unverified until built. Measure: seconds from needs-you to open.
4. **Preferences and unsubscribe.** Today: one "Daily summary" pref. Change: categories `lifecycle` and `away`, each with its own token kind and setting. Measure: unsubscribe rate per email.
5. **Reply acceptance for members.** Check `sender_allowed` for non-owner members; add workspace members to the accepted senders. Measure: member replies delivered to the CEO.
6. **Events** (needs item 7): `team_formed`, `first_handoff_completed`, `week4_active`, plus `lifecycle_email_sent {email_id, workspace}` and `lifecycle_email_clicked {email_id, action}` (action links via the backend, redirect after recording).
7. **Retire the old welcome entirely** once E1 ships. Invited members get the teammate welcome (a one-line confirmation) and E4 only.
8. **Voice.** E1-E8 are subject to item 8's rules and eval (1-4 sentences, one question at most, no promise without a task). Any generated variable text (titles, summaries) passes the same grader.

### 5. Measures (Appendix C definitions)
| Email | The action it exists to cause | Count it as |
|---|---|---|
| E1 | First message or first approval in the first session | Share of new workspaces with a first artifact and approval |
| E2 | Second person joins | Team formed (2+ humans within 48 h); invite-to-join rate |
| E3 | Approval answered | First handoff within the first week |
| E4 | Message opened in the app | Minutes from needs-you to open |
| E5 | A human message after the send | Share of recipients with a message within 3 days |
| E6 | One-click answer | Share very disappointed, beside week-4 active |
| E7 | Paid invoice at list | Paying at list; invoices paid by day 30 |
| E8 | Reply or channel message | Share of founding teams heard from each Thursday |

Open rate and click rate are diagnostics only. Judge each email by suppress-and-compare: hold out 10% of eligible workspaces and compare the action rate. Stop an email that does not beat its holdout after 30 workspaces each (small base; label results low confidence).

## Founder decisions (13 Oct 2026)
- A1/A2: No guarantee. Removed from E7; "week-4 active" stays as a metric only.
- A4: Card at setup, first charge on day 31. E7 says so and ships with the trial checkout.
- A8: Billing first; no email names the price before it ships.
- A12: A lapse about timing gets one 30-day pause per company.
- A13: A reading-only workspace is kept 90 days, then gets the standard deletion notice.
- D8: Away emails after five minutes; quiet hours 22:00-08:00 local; at most one per room per 30 minutes.
- D9: Five minutes confirmed. Invited members get the teammate welcome and away emails, not the owner nudges.
- D10: The welcome comes from the CEO agent's address in the agent's voice and replaces today's welcome entirely.
- D11: The Thursday check-in is drafted by the Growth agent and sent by the founder who runs that team.

## Sources
- `core/services/welcome_email.py`, `daily_digest.py`, `email_service.py`, `ses_email.py`, `agent_email.py`, `agent_mail_owner_send.py`, `agent_mail_send.py`, `agent_mail_inbound.py`, `core/config.py` (`EMAIL_FROM_WELCOME`, `AGENT_EMAIL_DOMAIN`), `core/observability/product_analytics.py` on `origin/main`.
- `docs/business-plan.md` section 7, Appendix C, Engineering/Product items 1, 3, 7, 8, Go To Market stage 2; `.agents/product-marketing.md`.
- Skill: marketing-skills:emails (one job per email, value before ask, clear next step).
