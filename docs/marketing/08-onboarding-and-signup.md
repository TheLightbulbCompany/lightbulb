# 08. Onboarding and sign-up: the first session ends with a second person in the room

## Purpose
Moves the goal's activation half: a second person in each workspace within 48 hours and a completed handoff in week one. Serves Engineering/Product items 1, 2 and 3 (with item 9's phone shape), Go To Market "The setup session", and the `/sign-up` row of "The website". Method: the `onboarding` and `signup` skills (aha moment, minimum path to value, checklist with endowed progress, empty states, field-by-field audit, two doors).

## What we know

All code facts are from `origin/main` at `6249f614d` (13 Oct 2026). Confidence: high (first-party code or data) unless marked.

| Fact | Source | Confidence |
|---|---|---|
| Funnel, 30 days to 12 Oct: 227 product-page visitors → 18 clicked sign up → 20 trials → 10 first agent reply → about 5 active outside workspaces a week. 33 of 38 outside workspaces have one person; 24 of 50 accounts never returned after day one | `docs/business-plan.md` §9 | High (first-party) |
| Weeks of 5 Oct and 28 Sep: 4 of 5 and 3 of 4 active outside workspaces had 2+ people | §9 weekly table | High |
| Clerk sign-up asks: email (required, verified by emailed code before entry), password (required), first and last name (optional); Google and Apple buttons; invisible "smart" CAPTCHA; no GitHub or Microsoft | Clerk public environment `clerk.thelightbulb.company/v1/environment`, read 13 Oct | High |
| `/sign-up` headline: "Your AI *company* starts here." Side panel: "Private by design / Your own private workspace. No shared data.", "Free for 30 days", "Extensible skills". Under the form: "Joining a team? If you got an invitation email, open it on this device." plus a join-link paste box | `apps/frontend/src/app/sign-up/[[...sign-up]]/page.tsx`, `components/onboarding/JoinTeamBlock.tsx` | High |
| After sign-up: `/welcome/name` ("What should people call you?") only if Clerk has no name; then `/onboarding` "Create your workspace" with two fields: Workspace name and Agent email address (slug, shown as `ceo@{slug}.{domain}`) | `app/welcome/name/page.tsx`, `hooks/useNameGate.ts`, `app/onboarding/page.tsx` | High |
| Email invitations came back on 8 Oct next to the join link; admin-only; every invitee joins as member; Clerk sends the email and redirects to `/sign-up` | `routers/orgs.py` docstring, `core/services/workspace_invitations.py` | High |
| A teammate who joins is seated in #general automatically (`join_default_room` on `organizationMembership.created`) | `routers/webhooks.py` ~L460 | High |
| The CEO's seeded first message and three reply chips (quoted in the Deliverable). The first human message stages `BRIEF.md` and seeds a goal and project | `core/services/messaging.py` `_FIRST_MESSAGE`, `FIRST_REPLIES`; `core/services/onboarding_brief.py` | High |
| **The five-step Get started card the plan describes is not rendered.** Its guide was removed in #2544; `getStartedConfig.ts` survives only for a type import. The live checklist is the `OnboardingBand` in #general: an optional starter-model card, Stripe / GitHub / Notion connect cards, "See more connections", then "Invite your teammates" (copies the join link, admin-only), "Name your agents", "Share your context" | `components/getstarted/`, `components/messaging/OnboardingBand.tsx`, `packages/shared/src/messaging/onboardingSteps.ts` | High |
| Approval links and Paperclip document links in a chat message already render as cards (`ApprovalRow`, `RoomDocumentCards`). Agents can mint a one-click sign-in link for a connector (`lightbulb-connect.sh link <provider>`), but it renders as a plain link. QuickBooks, Google, GitHub, Stripe, Notion, Microsoft 365 are in the catalog | `components/messaging/RoomAppLinks.tsx`, `appLinks.ts`, `skills-src/lightbulb/scripts/lightbulb-connect.sh`, `credentials/providers.yaml` | High |
| Off-app notice today: phone push for every notifying message ("Pulse · #GTM Strategy" / message preview; muted rooms skipped); a daily 08:00 local "N things need you" digest by email and push, with a "Daily summary" setting and one-click unsubscribe. No email for a single DM, mention or approval | `apps/infra/lambda/messaging-projector/index.py`, `core/services/daily_digest.py`, `components/settings/DailySummaryCard.tsx` | High |
| Presence is an in-process 90-second heartbeat; the code says it flickers on deploys. The durable "has this person seen it" signal is the per-room read cursor (`READ#human#<user>.lastReadSeq`) and Activity's unseen items | `routers/messaging.py` ~L709, projector | High |
| Welcome email: sent on Clerk `user.created` to every new account, invitees included, before any workspace exists. Subject has an emoji. Says "runs in a container that belongs only to you. Nothing is shared", and that the CEO "wakes overnight… and emails you" | `core/services/welcome_email.py`, `routers/webhooks.py` ~L840 | High (text); the overnight email claim is unverified |
| Mobile with no workspace: "Create a workspace / Your agents and conversations live in a workspace. Create one on the web, then come back here." Joining instructions second | `apps/mobile/src/components/workspace/WorkspaceGate.tsx` | High |
| Events that exist: `signup_started`, `signup_cta_clicked`, `onboarding_step_entered/completed`, `onboarding_ready` (browser-local, once), `first_run_brief_submitted`, `trial_first_agent_response` (once per owner), `invitation_sent`, `org_invitation_accepted`, `join_link_copied`, `join_link_redeemed` (with `members_count`, `invited_by`), `room_opened`, `room_message_sent` (`first_in_room`, `sender_is_owner`), `get_started_*` (now dead). No event for team formed, first handoff, approval answered, or a notification opened from email | `lib/analytics.ts`, backend `capture_product_event` call sites | High |
| `/pricing` and `/request-setup` return 404 on thelightbulb.company | curl, 13 Oct | High |
| The plan credits Muse with "setup inside the chat" and an "Ideas list where every item is a finished promise". The research file does not describe either; it has Muse's "trust fall" (small task, then bigger) and Instinct's "first 30%" reply rule | `docs/research/2026-10-13-employee-voice-and-consumer-agents.md` | Muse claims: unverified |

## Findings (ranked by impact)

1. **Nothing in the first session asks for a second person, and the one invite door is last and hidden.** The band's invite card sits below three connector cards, copies a link (no email field) and is admin-only. Every retained workspace has 2+ people (§9). This is the biggest lever. *Skill: B2B order is setup → first value → team invite; for us the invite is the value.*
2. **The first run ends on the board, not in the room.** The cards that make a result visible in chat (document, approval) already exist; the CEO is not told to produce them. 10 of 20 trials got a first reply; we do not know how many saw a finished thing. *Skill: one goal per session; do, don't show; peak-end.*
3. **Sign-up promises the opposite of the product.** "Your AI company starts here" and "Your own private workspace. No shared data" sell a solo tool. The join door is a grey line under the form.
4. **The second person never hears that something happened.** A web-only teammate gets a toast in an open tab and a digest the next morning. The read cursor gives us a clean "still unseen" test without presence.
5. **The welcome email is false in three places** (container, nothing shared, sent before the workspace exists) and goes to invitees as if they had signed up to start a company.
6. **The plan's Get started description is stale.** The card it describes is gone; the change lands on `OnboardingBand`.
7. **We cannot count activation.** No team-formed or first-handoff event exists, so items 1–3 cannot be judged until they do.

## Recommendation

- **Activation = a second person sees the first handoff.** Concretely: within 48 hours a second human is a member and has opened the room where an agent's result and its approval card sit, and within week one a person answers that approval or continues the work. This is the plan's "team formed" plus "first handoff", in the skill's terms: the aha is not "the CEO replied" but "someone else saw it and said yes".
- **Minimum path to value:** sign up (1 screen) → name the workspace (1 field) → answer the CEO (1 message) → invite one person (inline) → approve one card. Five actions. Everything else moves to "later".
- **Put the invite in the conversation, first in the checklist, and make it email-first.** Show it while the first run is working, so the wait is used.
- **Make the first run's shape deterministic** where we can (the invite line is posted by the backend, not the model) and **instructed** where we must (artifact, approval, connect card, three offers in `BRIEF.md`).
- **Email the away person within minutes** for DMs, mentions and approvals, gated on the read cursor, with one setting.

Rejected: a multi-step wizard before chat (adds friction before value; the band already does setup in context); asking team size or "how did you hear" in the Clerk form (defer to the workspace form, optional); a model-written invite ask (would not fire reliably and could not be measured); presence-based suppression (in-process, flickers).

## Deliverable

### 1. Activation definition and current funnel

| Stage | Today (30 days to 12 Oct) | Event that measures it |
|---|---|---|
| Product-page visitor | 227 | `landing_viewed` |
| Clicked sign up | 18 (8%) | `signup_cta_clicked` |
| Trial started | 20 | `trial_started` |
| First agent reply | 10 (50% of trials) | `trial_first_agent_response` |
| Second person joined within 48 h | 5 of 38 outside workspaces have 2+ people (all time) | **new** `team_formed` |
| First handoff in week one | not measurable | **new** `first_handoff` |
| Active in week four | about 5 workspaces a week | existing sweeper → `week4_active` (workstream 09) |

Targets for the first ten founding teams: 10 of 10 team formed in 48 h (a founder is in the setup session); for self-serve, 25% team formed and 15% first handoff in week one by end of December (assumption; reset after four weeks of data).

### 2. Sign-up audit (field by field)

| Field / element | Today | Change | Priority |
|---|---|---|---|
| Headline | "Your AI company starts here." | **"Bring your team."** Sub: "Your team and your AI employees, in one place. 30 days free, no card." | High |
| Doors | One form; join is a grey line below | Two buttons above the form: **Start a workspace** / **Join my team** (the second shows "Open the invitation email on this device, or paste the join link") | High |
| Email | Required, code-verified before entry | Keep the emailed code (D6). It protects the agents' sending reputation and cuts fake sign-ups | — |
| Password | Required, no rules shown | Keep, with the passkey option Clerk already offers. Social sign-in above it | Low |
| First / last name | Optional in Clerk, then a separate `/welcome/name` page if blank | **First name required in Clerk, last name removed.** The name gate then only fires for Apple "Hide My Email". One screen fewer for email sign-ups | Medium |
| Social | Google, Apple | Add **GitHub** (the wedge lives in Claude Code and GitHub). Test against Google-first | Medium |
| CAPTCHA | Smart (mostly invisible) | Keep | — |
| Side panel | "Private by design / No shared data", "Extensible skills" | Replace with three team lines: "Mention an AI employee in a channel. The work comes back where everyone can see it." · "Spend, sends and deploys wait for a person." · "30 days free. The card goes on file at setup; the first charge is on day 31." | High |
| Workspace name | Required | Keep, label "Your company or team" | — |
| Agent email address (slug) | A second required-looking field | Collapse to one line under the name: "Your CEO will email as ceo@acme.thelightbulb.company · Change". Edit stays possible because a slug cannot be deleted | Medium |
| "How did you first hear about us?" | Absent | Optional select on the workspace form only (owner, once): "A friend or teammate / Hacker News / Reddit / X / LinkedIn / Claude Code or Codex plugin / Search / AI assistant / Other". Item 7 needs it; the Clerk form stays at 3 fields. On the `/request-setup` form the same question is required (D13) | Medium |
| Invitation landing | Clerk email redirects to `/sign-up`, which says "start a company" | Redirect to `/join/invite`, headline "**{Inviter} invited you to {Workspace}.**" Sub: "Sign up to see what the team and its AI employees are working on." | High |
| Mobile, no workspace | "Create a workspace" first | Lead with joining: "**Join your team.** Open the invitation email on this phone, or the join link they sent you. Starting a new workspace? Do that on the web." | Medium |

### 3. The first session, screen by screen

**Screen 1. Sign-up** (as above). **Screen 2. Name the workspace** (one field, slug line, optional source). Button: "Create workspace".

**Screen 3. #general while agents start.** The channel opens at once (messaging works during provisioning). Stepper line stays: "Your team will be ready in about a minute." The Get started band shows above the first message (spec below).

**Screen 4. The CEO's first message: unchanged** (`_FIRST_MESSAGE`; its facts stay):
> Welcome to {workspace}. I'm your CEO. Growth, Engineering, Finance, Analytics and Customer Ops are in this room with me.
> One thing gets us started: what's the business? A website or a sentence is plenty.
> → Here's our website: …
> → I'm starting …
> → Not sure yet. Pick something useful and show me how you work.
> I'll turn your answer into a plan and hand the first pieces to the team. You'll watch them work here.

**Screen 5. The CEO's first reply** (model, shape fixed by `BRIEF.md`): one to three sentences, the point first.
> Got it: {one-line restatement of the business}. I'm writing a one-page plan for the next two weeks. It'll be here in a few minutes.

**Screen 6. The invite line** (posted by the backend as the CEO, immediately after the first reply ends, only while the workspace has one member; static text):
> Who should see this with you? Add them now and they'll see the plan the moment it's ready, and can approve it with you.

In a setup session the second person is asked for at booking; if the founder comes alone, this first approval card is the invite (C4). Under it, an inline card (rendered from an app link, like approval cards): `[ name@company.com ] [Send invite]` · `Copy join link` · `Just me for now`. Sent state: "Invite sent to sam@acme.com. They'll land in this channel." "Just me for now" collapses the card to: "You can invite people any time from Get started."

**Screen 7. The result** (model; required shape). The run's live preview shows meanwhile. Then one message:
> Done. Here's the plan: {document card}
> One decision for you: {approval card, e.g. "Start outreach to 20 named design partners this week?" Approve / Not now}
> To do the first piece for real I need {QuickBooks | Gmail | GitHub}: {connect card}
> Three things I can finish next:
> → I'll draft the three invoices due this week; you send them in a tap.
> → I'll write the email to your 40 waitlist sign-ups; you approve it before it goes.
> → I'll open a pull request for the top error in Sentry; you review and merge.

Rules for the offers: each names a finished thing and the person's one part; each is something the agent can do with what is connected or one connect away; never offer work the agent will not do (research: a broken promise fails the "trust fall" at step one).

**Screen 8. The peak and the end.** When the approval is answered:
> Approved. Growth has it and will post here when the first ten are drafted.

This is the first time a specialist is named as doing something; the CEO is the only voice until then. If a teammate answered it, the line names them: "Sam approved. Growth has it…"

**The Get started band, reordered** (`OnboardingBand`; header "Get started · 2 of 6 done"):

| # | Card | Detail | Done when |
|---|---|---|---|
| ✓ | Your workspace | "acme is ready" | always (endowed) |
| ✓ | Your AI employees | "Six of them, in #general" | always (endowed) |
| 1 | **Invite your team** | "They see what your AI employees do and can approve it with you." Email field + Send, and "Copy join link" | 2+ members |
| 2 | Tell your CEO about the business | "A website or a sentence" | first human message in #general |
| 3 | Connect one tool | Stripe · GitHub · Notion · See more | any connector connected |
| 4 | Answer your first approval | "Your CEO will ask for one decision" | an approval answered |
| Later | Name your agents · Share your context | unchanged | not counted |

A completed band shows "All set. Your team can see everything your AI employees do." for one session, then goes. Members, not only admins, see "Invite your team" enabled and can send email invites and copy the join link (D5).

**Empty state for a one-person workspace** (sidebar, under Direct messages, until a second member joins; no CEO nagging):
> It's just you and your AI employees. Invite one person who should see their work. [Invite]

In the members list of #general: "1 person · 6 AI employees · Invite people".

**On the phone (item 9's shape).** The owner usually starts on the web; the phone's job is the interrupt and the one tap.
- When the result lands: push title "CEO · #general", body "The plan is ready. One thing needs your OK." Tapping opens the message with the approval card in view.
- An approval waiting: "Finance · needs your OK" / "Invoice 1042 is ready to send. OK?" (agent-named, never "Lightbulb: new activity").
- An invited teammate on the phone: the invite email says "Open this on your computer, or on iPhone" and the app opens on the room they were invited into, not a workspace tree. Until item 6 ships, the iPhone link is TestFlight; say so.
- Replies on the phone are short by rule; documents open in Library, not in the thread.

### 4. Away notifications (item 3)

| | Spec |
|---|---|
| Triggers | A DM to you (from a person or an agent); an @mention of you; an approval assigned to you or to the workspace's admins |
| Delay | 5 minutes after the message (so someone in the app reads it first) |
| Send if | Still unread at 5 minutes: message seq above your read cursor for that room, or the approval still pending and unseen in Activity |
| Suppress | You wrote it; the room is muted; you read it in the window; quiet hours 22:00–08:00 in your timezone (rolled into the 08:00 digest); one email per room per 30 minutes (later items batch into it); at most 10 a day; setting off |
| Phone | Push stays immediate (it is already); the email is for people without the app |
| Who gets it | Every member, invited members included. Invited members get the teammate welcome and away emails, not the owner nudges (D9) |
| Setting | Settings → Notifications, beside Daily summary: **"Email me when something needs me and I'm away"**, on by default; one-click unsubscribe in every email, reusing the digest's token pattern |
| Link | Opens that message (or the approval) in the web app, not the Activity list |

Email copy (DM):
- Subject: `{Sender}: {first 60 characters of the message}`
- Body: "{Sender} sent you a message in {Workspace}: "{preview, 180 characters}" [Open the message] · You get this because you weren't in Lightbulb when it arrived. Turn these off: {link}"

Email copy (mention): Subject `{Sender} mentioned you in #{room}`; same body with "mentioned you in #{room}".

Email copy (approval): Subject `{Agent} needs your OK: {approval title}`; body "{Agent} is waiting on one decision in {Workspace}: {title}. {one-line summary} [Approve or decline]". Same footer.

Push copy is the projector's existing two lines; approvals add "· needs your OK" to the title.

### 5. The welcome email, rewritten to be true

The CEO agent's address sends it, in the agent's voice, and it replaces today's welcome entirely (D10). Trigger moves from `user.created` to `organizationMembership.created`, so the owner gets it when their workspace exists and a teammate gets the teammate version. No emoji.

**Owner version.** Subject: `Your team at {Workspace} is ready`
> Hi {first name},
>
> I'm your CEO. Your workspace is set up, and the rest of the team is in #general with me: Engineering, Finance, Growth, Analytics and Customer Operations.
>
> Tell me what the business is (a website is enough). I'll write a short plan, post it in the channel, and ask you for one decision. While I work, invite the person who should see it with you. They can approve it too.
>
> [Open #general]
>
> We run in your workspace's own cloud, so routines and email keep going when your laptop is closed. Spend, sends and deploys wait for a person. Your 30 days are free.
>
> Reply to this email if anything is unclear.
>
> — CEO, {Workspace}

**Teammate version.** Subject: `You're in {Workspace} on Lightbulb`
> Hi {first name},
>
> {Inviter} added you to {Workspace}. The team's AI employees work in the channels, and their results and approvals show up there for everyone.
>
> Start in #general: you'll see what's been asked and what's waiting on a decision. Mention an agent by name to hand it something.
>
> [Open #general]
>
> — CEO, {Workspace}

## Build tasks (Forge-ready, dependency order)

| # | Title | What today | What changes | Files | Why | Measure |
|---|---|---|---|---|---|---|
| 1 | Activation events | No team-formed, first-handoff or notification-opened events | Backend emits `team_formed` (second member joins within 48 h of org creation; once), `first_handoff` (first approval answered, rejected counts, session picked up, or agent task moved by a person in week one; once), `approval_answered`, `needs_you_email_sent`, `notification_opened` (with `channel`, `latency_s`). All carry `workspace_id` and `environment` | `routers/webhooks.py` (membership created), `workspace_join.py`, `core/observability/product_analytics.py`, Teams approvals route under `routers/teams/` | Without them nothing below can be judged | Events visible in PostHog for dev workspaces |
| 2 | Inline invite card | Invite only in the band, link-only, admin-only | New app-link kind `invite` (`/chat?panel=invite`) rendered by `RoomAppLinks` as email field + Send (`POST /orgs/{id}/invitations`) + Copy join link + "Just me for now"; members may send invites, so the route and the join-link mint stop being admin-only | `components/messaging/appLinks.ts`, `RoomAppLinks.tsx`, reuse `useCopyJoinLink.ts` | One component for both the CEO line and the band | `invitation_sent` with `source=chat` |
| 3 | The invite line | Nothing asks | After the first `run.ended` in the default room, if org has 1 member, post the static CEO line with the invite link, once (marker on the room META) | `core/services/messaging.py` (`_after_run_ended`, beside `_FIRST_MESSAGE`) | Deterministic, measurable ask | Share of new workspaces with `invitation_sent` or `join_link_copied` in session 1 |
| 4 | Band reorder + endowed progress | Connectors first, invite last, no progress count | Order and done-states per §3; "2 of 6 done"; invite card uses task 2's component; "Answer your first approval" from the Teams approvals read; delete dead `getStartedConfig.ts` STEPS and `get_started_*` events | `packages/shared/src/messaging/onboardingSteps.ts`, `components/messaging/OnboardingBand.tsx`, `components/getstarted/` | Invite first; head start lifts completion | Band step-click events; `team_formed` |
| 5 | One-person empty states | None | Sidebar and #general member-list lines per §3 | `components/workspace/WorkspaceSidebar.tsx`, `components/messaging/RoomPanel.tsx` | Quiet, persistent reminder | Invites sent after day 1 |
| 6 | First-run shape in the brief | `BRIEF.md` says who the person is, not what to deliver | Add the result contract: first reply ≤3 sentences; result message with one Paperclip document link, one `request_board_approval` linked, one connect link via `lightbulb-connect.sh`, three outcome offers. Mirror in the lightbulb skill reference and `MAINTENANCE.md` | `core/services/onboarding_brief.py` (`_BRIEF_TEMPLATE`), `apps/infra/hermes/skills-src/lightbulb/references/chat-vs-document.md`, `MAINTENANCE.md` | Item 2: one finished thing, one decision | Share of new workspaces with a document and an approval in session 1 |
| 7 | Connect card in chat | Connect links render as plain URLs | App-link kind `connect` for the one-click sign-in URL, rendered with `ConnectButton` | `appLinks.ts`, `RoomAppLinks.tsx`, `components/settings/ConnectButton.tsx` | Tool connected inside the chat | `integration_connected` with `source=chat` |
| 8 | Away email | Daily digest only | Sweeper on the daily-digest pattern: every minute, unseen DM/mention/approval Activity items older than 5 min, read-cursor check, rules per §4; per-(user, room) 30-min claim; new pref `awayEmail` | new `core/services/needs_you_email.py`, `daily_digest.py` (reuse render/unsubscribe), `routers/users.py` prefs, `components/settings/DailySummaryCard.tsx`, `main.py` background tasks | Item 3 | Median time needs-you → opened |
| 9 | Approval push copy | Approvals: unverified whether they push | Ensure an approval assigned to a person pushes "{Agent} · needs your OK" / title | projector or Teams approval path (find the writer first) | Item 9's interrupt | Push → open rate |
| 10 | Sign-up page | Solo copy, one door | §2: headline, two doors, side panel, GitHub sign-in added (D7) and first-name-required in the Clerk dashboard (manual, dev then prod) | `app/sign-up/[[...sign-up]]/page.tsx`, `JoinTeamBlock.tsx`, `SignupPlanPill.tsx` | Sell the team, show the join door | `signup_started` → `trial_started`; join share |
| 11 | Workspace form | Two required-looking fields | Slug as one line with Change; optional source select (event property) | `app/onboarding/page.tsx` | Fewer fields; attribution | Form completion; source fill rate |
| 12 | Invitation landing | Clerk invite → `/sign-up` | `redirect_url` to `/join/invite`, naming inviter and workspace | `workspace_invitations.py`, new `app/join/invite/page.tsx` | Invitee sees why they're here | `org_invitation_accepted` / `invitation_sent` |
| 13 | Welcome email | False claims; sent to every account at `user.created` | §5 copy, from the CEO agent's address in the agent's voice, replacing today's welcome entirely; trigger on membership created; owner vs teammate | `core/services/welcome_email.py`, `routers/webhooks.py` | True first-ten-minutes promise | Click-through to #general |
| 14 | Mobile no-workspace screen | "Create a workspace" first | §2 copy | `apps/mobile/src/components/workspace/WorkspaceGate.tsx` | Teammates arrive by text | Joins from mobile |

Seat billing and the trial checkout (workstream 15) are built before tasks 10 and 13, whose copy quotes the offer. Tasks 1–3 are one PR's worth and come first; 4–7 next; 8–9 independent; 10–14 can run in parallel with workstream 10 (website copy).

## Measures (Appendix C)

| Appendix C metric | How this workstream moves it | Read from |
|---|---|---|
| Team formed (2+ people within 48 h) | Invite line, band order, invite landing, empty states | `team_formed` / new workspaces, weekly |
| First handoff (in week one) | Approval card in the first run; away email to the second person | `first_handoff` / teams formed |
| Time from needs-you to opened (item 3) | Away email + approval push | `notification_opened.latency_s` median and p75, by channel |
| Leading indicators | — | invite sent in session 1; document + approval in session 1; band completion; sign-up form completion by field; Join-door share |
| Week-4 active | Downstream of the three above | workstream 09's sweeper event |

Monday readout line: "New workspaces N · team formed X% · first handoff Y% · needs-you → opened median Z min".

## Founder decisions (13 Oct 2026)

- D5: members, not only admins, may send invites.
- D6: keep the emailed sign-up code.
- D7: add GitHub sign-in.
- D8: away emails after five minutes; quiet hours 22:00–08:00 local; at most one per room per 30 minutes.
- D9: invited members get the teammate welcome and away emails, not the owner nudges.
- D10: the welcome email comes from the CEO agent's address, in the agent's voice, and replaces today's welcome entirely.
- C4: ask for the second person at booking; the first approval card is the invite.
- C1: the customer answers the first approval card, never the founder.
- A4: founding teams put a card on file at the setup session (Stripe checkout with a 30-day trial, first charge on day 31), built first. Self-serve sign-up stays no-card.
- D13: source question optional at sign-up, required on the setup form.

## Sources

- `docs/business-plan.md` (§8, §9, Engineering/Product 1–3 and 9, Go To Market, Appendix C); `.agents/product-marketing.md` v7
- `docs/research/2026-10-13-employee-voice-and-consumer-agents.md`
- Code at `origin/main` `6249f614d`: `apps/backend/core/services/{messaging,onboarding_brief,welcome_email,workspace_invitations,workspace_join,daily_digest}.py`; `apps/backend/routers/{orgs,webhooks,messaging,users}.py`; `apps/infra/lambda/messaging-projector/index.py`; `apps/frontend/src/app/{sign-up,onboarding,welcome/name,join}`; `apps/frontend/src/components/{onboarding/JoinTeamBlock,messaging/OnboardingBand,messaging/RoomAppLinks,messaging/FirstReplyChips,chat/ProvisioningStepper,getstarted/getStartedConfig}`; `packages/shared/src/messaging/onboardingSteps.ts`; `apps/mobile/src/components/workspace/WorkspaceGate.tsx`; `apps/frontend/src/lib/analytics.ts`; `apps/infra/hermes/skills-src/lightbulb/scripts/lightbulb-connect.sh`; `apps/infra/hermes/skills-src/paperclip/references/interactions.md`
- Clerk public environment: https://clerk.thelightbulb.company/v1/environment (read 13 Oct 2026)
- Live site: https://thelightbulb.company/sign-up (200), /pricing and /request-setup (404), 13 Oct 2026
