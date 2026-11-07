# 06 · Referrals and loops: session links as the door, the first-session invite, and a stage-2 referral

## Purpose

Moves the plan's activation number (a second person in the workspace within 48 hours) and stage 2's "loops the product runs itself". Serves Engineering/Product items 1 and 4, Go To Market "Launch for inbound" and stage 2, and §7's no-discount rule.

## What we know

Code facts are from `origin/main` at `6249f614d` (13 Oct 2026). Confidence is high for code facts (first-party) unless marked.

**The sharing model.**
- A session has one link. Its audience is `workspace` or `anyone` (`local_sessions.AUDIENCES`). "Only me" is the session's `unshared` state, and no link opens then.
- An "Anyone with the link" link shows the live terminal, view only, with no sign-in. The viewer gets screen frames and nothing else: no input, chat, events, session id or workspace id (`session_links.py`, "Watching" block).
- A public link holds at most **20 concurrent viewers** (`MAX_LINK_VIEWERS = 20`) and 120 seats per ten minutes. A 21st viewer gets a 409, which `/s` shows as "Could not reach this session. Trying again." That is a dead end for launch-day traffic.
- A session with no terminal (inside the Claude app, the Codex app, an IDE or an SDK) is never public: `public_id()` returns none for `conversation_only` rows.
- Outsiders see the owner's first name, a redacted title, a public repo's line, and the state; ended or offline shows no screen. Members are redirected into the app. No workspace name or id reaches an outsider.
- **Continue** (`lightbulb continue '<link>'`) gives an outsider a redacted copy of a **Claude Code** session, only when the owner allowed pick-up (off by default). `create_copy` requires the taker's **own** workspace, so Continue is already an acquisition path, not a path into the sharer's workspace.
- **Pick up** and **Ask to join** are for members only. Pick up is for Claude only (`session_pickup` refuses `provider != "claude"`). Codex can be watched and typed into, but not picked up or continued.
- Only the Mac app shares sessions. The `lightbulb` plugin (`TheLightbulbCompany/lightbulb-plugin`) gives the hosted MCP server and the sharing skill, and "does not share sessions" (`skills-src/lightbulb/references/surfaces.md`). Sharing is opt-in on the Mac.

**The doors into a workspace.**
- **Join link:** one per workspace, open join (no approval), everyone joins as `org:member`, no expiry. Only an admin can mint, show or revoke it. URL `/join/<orgId>#<token>` (`workspace_join.py`, `routers/orgs.py`).
- **Email invitation:** admin only, back since 8 Oct, no role choice. Clerk sends it and redirects to `/sign-up` (`workspace_invitations.py`).
- **Sign-up page:** "Have a join link?" sits under the form (`sign-up` page test).
- **First session:** the Onboarding band has an "invite" card that copies the join link ("Connect what your team already uses, then bring the rest of them in"). The CEO's static first message asks only "what's the business?" (`messaging._FIRST_MESSAGE`). Nothing in chat asks who else should be there.

**Events emitted today.**

| Event | Where | What it carries | Gap |
|---|---|---|---|
| `join_link_copied` | web (`useCopyJoinLink`, band) | `source`, `workspace_id` | none |
| `join_link_redeemed` | backend, on the joiner | `workspace_id`, `invited_by`, `members_count` | no `via`: cannot tell a session-link join from a pasted link |
| `invitation_sent` | backend, on the inviter | `workspace_id` | no acceptance event: `org_invitation_accepted` is declared in `analytics.ts` and never emitted |
| `share_link_created` | backend | `audience`, session, provider | none |
| `share_link_opened` | backend `open_page` | `member` | **fires only when signed in** (`auth is not None`); anonymous viewers, the main audience of an "anyone" link, are not counted. `/s` records only a route pageview, with no link identity |
| `session_copied` | backend | `member` | the Continue door's only fact |
| `session_join_requested` / `_decided` | backend | `decision` | members only today; outsiders after T3 |
| `session_picked_up` | backend (`session_pickup`) | ids | members only |
| `session_opened` | web + backend | `own`, provider, origin | members only |

No `team_formed` event exists. The admin growth page computes "multiplayer share" from messages (`ops/growth.py`), not the plan's 48-hour definition.

**The `/launch` referral code.** Each sheet row has a `referral_code`. `/?ref=CODE` is captured and attributed through `POST /launch/referral`. A referral counts once, when the referee's first subscription goes live (`on_entering_live` → `launch_referral.on_subscription_activated`, with a CAS guard). Each counted referral moves the referrer up 10 spots (`referral_jump`, first 20 count). 5 referrals earn $50 in credits and 20 earn $250 in total, granted through Metronome. **In code it cannot fire for any current account.** `on_subscription_activated` returns at once for `org_` owners. `POST /launch/join` refuses every org context (`org_not_supported`). Every product owner is a Clerk org now (`CLAUDE.md`, Provisioning Flow). Confidence: high from code. Prod rows not checked.

**Where we start** (plan §9 and the context doc, 12 Oct): 36 of 46 workspaces have one member, and 33 of 38 outside workspaces have one person. No customer pays list price.

## Findings (ranked by impact)

1. **We have an exposure loop that ends at a wall.** With Continue off (the default), a signed-out viewer sees a live terminal and no ask at all. Loom's founder describes the loop we lack: "If you send someone a loom video, they'll watch it, they're curious and they'll sign up". Loom prompted a sign-up after the viewer watched, so they could reply ([Latka interview, 2019](https://getlatka.com/companies/loom/interviews/loom-shahed-khan-2019); high for the mechanism, the only number is Loom's own: 1.1M registered users, about 21,000 companies, no paid marketing).
2. **The proven move from viewer to member is "ask, and the owner approves", not an open door.** Figma's view-only users click **Ask to edit**. Figma notifies the owner by email and in-app, and the owner grants access ([Figma help](https://help.figma.com/hc/en-us/articles/4408435431319-Request-to-edit-a-file); high). Plan item 4 says to put "the workspace's join link" on `/s`. That would be wrong here. A join link admits its holder, with no approval, to "a workspace containing the owner's agents, files and connected accounts" (`workspace_join.py`), and it carries the org id that `/s` is built never to reveal. One public post would turn it into a public door.
3. **Two kinds of viewer need two doors.** A teammate should join *this* workspace; a stranger should start *their own*. Continue does the second only for Claude and only on opt-in. Notion and Replit make the copy the door and credit the source: Notion's "Duplicate" puts a public page into the visitor's own workspace, on unless the owner turns it off ([Notion help](https://www.notion.com/help/duplicate-public-pages); high). Replit's remix shows a "Remixed from" link back to the original ([Replit docs](https://docs.replit.com/features/projects-and-artifacts/remix-an-app); high). Replit's claim that shared projects drove "a huge portion of sign-ups" comes only from an AI summary of an interview ([Sacra](https://sacra.com/chat/h/067eb151-4c1f-4add-b07b-66d0abc748df/); low).
4. **The nearest competitor is already here.** Delta shares a thread by link that opens in a browser, syncs a Claude Code session live, and lets teammates "pick up the work" ([Zed, 12 Aug](https://zed.dev/blog/introducing-delta)). It has been a free public beta since 16 Sep, not the private beta our context doc lists ([Zed](https://zed.dev/blog/delta-public-beta)); high. Our difference, which `/s` should say: the viewer joins a workspace with agents and the rest of the company's work, not one thread.
5. **We cannot measure the loop today.** Anonymous opens are not counted, joins do not record where they came from, invitation acceptance is not emitted, and there is no `team_formed` event. Until those exist, every number below is unmeasurable (high).
6. **The `/launch` referral cannot be "repointed" as it stands.** The plan's stage-2 line assumes it can. The pieces to reuse are the code generator, the `?ref=` capture, the once-only CAS and the `on_entering_live` hook. The key (personal email), the reward (Metronome credits) and the org guard must change (high).
7. **K will be well under 1: amplification, not virality.** Sharing needs the Mac app, a terminal session and a deliberate "Anyone" choice, and shows a screen only while live (medium; our inference).

## Recommendation

1. **Ship item 4 as "Ask to join", not as an embedded join link.** An outsider on an "Anyone" `/s` page gets the same "Ask to join" teammates already have: they sign up, the session owner taps Allow, and they may use the session's controls. That Allow also makes them a workspace member. One door. "Viewers can ask to join" is on by default for "Anyone with the link" sessions, and the owner can turn it off per session. No join token or workspace id ever reaches the page. The public session page is the only no-account look; nothing more is built for launch.
2. **Give the stranger the second door on the same page:** "Not on Sam's team? Start your own workspace", with the source recorded as the sharing workspace. In stage 2 this becomes the referral attribution.
3. **Ship item 1 as a chat line, not a settings card:** the CEO's next message after its first reply asks "Who should see this with you?" with an email field in the message.
4. **Stage 2 referral (deferred to stage 2; the session-page door and the first-session invite stay in stage 1):** a workspace-keyed code. When a referred team pays its first full-price invoice, both teams get $50 of Stripe customer balance. The reward is a balance credit, not a coupon, so every counted invoice stays "paying at list". Reuse the `/launch` plumbing, not its table.
5. **Fix measurement first** (build tasks T1 and T2). Nothing else can be judged without it.

**Rejected.** The join link on `/s`, or auto-join on an "anyone" link: an open door to agents and connected accounts, and the first leaks the org id. A "Powered by" badge: the page already is Lightbulb. Credits per referral as on `/launch`: worthless to BYO-key and ChatGPT teams. Queue jumps and tiers: easy to game. Asks at sign-up: the skill says ask at the aha moment.

## Deliverable

### (1) The loop map: every door into a workspace today

| Door | Who it reaches | Who can open it | Approval | What it measures today |
|---|---|---|---|---|
| Email invitation | A named person the admin knows | Admin | None (email = intent) | `invitation_sent` only; acceptance not emitted |
| Join link (copied anywhere) | Whoever the admin sends it to | Admin mints; anyone with it joins | None | `join_link_copied`, `join_link_redeemed` (no source) |
| Sign-up "Have a join link?" | A teammate who signed up first | Joiner | None | Folded into `join_link_redeemed` |
| Onboarding band invite card | The founder in the first minutes | Admin | n/a | `join_link_copied {source: onboarding_band}` |
| Session link, "My workspace" | Members only | Session owner | n/a | `share_link_opened {member}` |
| Session link, "Anyone" | Anyone with the URL | Session owner | Ask to join: the owner taps Allow | `share_link_created`; opens only when signed in |
| Continue (Claude, owner opt-in) | A stranger with Claude Code | Owner's pick-up switch | n/a | `session_copied`; creates the taker's **own** workspace |
| `/launch` `?ref=` | Competition entrants | Personal owners only | n/a | Dead for workspaces (see above); the competition is not set up, so no credit exists to honour |
| Ask to join (session) | Members, and outsiders on an "Anyone" session | Member or outsider | Session owner taps Allow; for an outsider, Allow also makes them a member | `session_join_requested/decided` |

### (2) The session-link loop

```
Founder shares a terminal session as "Anyone with the link" → posts it (team chat, X, HN, a DM)
  → viewer opens /s, watches the live terminal (no sign-in)
     ├─ teammate:  "Ask to join Sam's workspace" → sign up / sign in → request → Sam taps Allow
     │             → member, with the session's controls (Watch · Pick up)  ⇒ team formed
     └─ stranger:  "Start your own workspace" (or Continue, if on) → own workspace, source = sharer
                   ⇒ new team; in stage 2 a referral
```

**What changes in `/s`.** Shown only when the link is "Anyone", the viewer is not a member and the page is not in the CLI confirm mode. The panel goes under the header, above the terminal, so it is seen before the screen fills.

| State | Copy |
|---|---|
| Signed out | **Sam is working with their team and AI employees in Lightbulb.** Join Sam's workspace to type in this session, pick it up on your Mac, or see the rest of the work. [Ask to join] · Not on Sam's team? [Start your own workspace]. |
| Codex session | Same, without "pick it up on your Mac" (Codex cannot be picked up). |
| Signed in, no request | [Ask to join Sam's workspace] |
| Requested | Sam has your request. We'll email you when Sam lets you in. You can keep watching. |
| Approved (next poll) | You're in. [Open in Lightbulb] (the existing member redirect takes over) |
| Declined or expired | Silent: the panel goes back to "Start your own workspace" only. A decline is not announced. |
| Link full | This session is full. N people are watching. Try again in a minute. The same two buttons. |
| Ended / offline | No terminal. The panel stays: "Sam's session ended. Join Sam's workspace to see what came of it." |

"Ask to join" while signed out opens Clerk's sign-up modal with `forceRedirectUrl="/s"`. The link stays in sessionStorage, as Continue does today, and the request goes on return.

**The join grant.** The existing session "Ask to join", opened to a signed-in outsider: `POST /public/session-links/join-request {token}` with a Clerk bearer token, no org needed.
- The backend resolves the workspace from the token (`resolve`) and refuses the same way for every non-"anyone" or dead link.
- It writes one request row per (workspace, user), pending for 7 days, at most 20 pending per link per day. A repeat is a no-op.
- The request goes to the session owner as a Needs-you item and an email (item 3):
  > **Priya Shah (priya@acme.com) watched "fix the billing webhook" and asks to join.** [Allow] [Decline]
- Allow calls `clerk_admin.create_organization_membership(org, user, "org:member")`, the join-link call, and grants the session's controls. The webhook does the rest. No role and no admin rights are ever granted this way.
- The requester learns nothing about the workspace until approved.

**Privacy posture by sharing state.**

| State | Join panel | Why |
|---|---|---|
| Only me | none (link does not open) | as today |
| My workspace | none | the signed-out refusal must read the same as a turned-off link (ISOA-732); a join prompt would reveal a workspace exists |
| Anyone with the link | Ask to join + Start your own; "Viewers can ask to join" on by default, the owner can turn it off per session | the owner already chose to show the screen; the owner's Allow guards everything beyond it |
| Continue on | adds the existing Continue block | unchanged; off by default |
| Per-session switch "Viewers can ask to join" | default **on** for "Anyone" sessions (founder decision) | an owner who never wants requests turns it off |

**Events** (content-free; never a title, token or path):

| Event | Fires | Properties |
|---|---|---|
| `share_link_opened` | now also signed out, once per page load | `signed_in`, `member`, `provider`, `state`, `link_ref` (hash of the token hash; distinct id `anon:<link_ref>`) |
| `share_link_cta_clicked` | web | `cta: ask_to_join \| start_own \| continue`, `signed_in` |
| `workspace_join_requested` | backend | `workspace_id`, `via: session_link`, `session_id`, `provider` |
| `workspace_join_decided` | backend | `decision`, `latency_s` |
| `member_joined` | backend, `organizationMembership.created` (one place for all doors) | `workspace_id`, `via: invitation \| join_link \| session_link \| other`, `members_count`, `invited_by` |
| `workspace_created` with `source` | existing creation path | `source: session_link`, `source_workspace_id` (server-side, from the stashed token) |

### (3) The invite in the first session (item 1)

**Timing.** Not in the welcome message: it asks one thing (the business), and a second ask there would split attention. The invite goes **after the CEO's first substantive reply** (the plan), when there is something worth seeing. That matches the skill's "right after the first aha" rule.

**The CEO's next line** (a system-authored follow-up in the CEO's name, not model-written, so it is always there and always the same):
> Who should see this with you? Add their email and I'll bring them in. They'll land here with the plan.
> [email field] [Invite] · or [Copy join link]

- A non-admin first user sees: "Ask your workspace admin to invite them." (invitations are admin-only).
- After a send, the CEO line becomes: "Invited priya@acme.com. I'll tell you when they're here." When they join, the CEO posts once in #general: "Priya joined. Priya, the plan is above; Finance has the first task."

**Get started, first item:** "Invite your team. The people who own the work should see it." It is marked done at two members.

**One-person reminder.** A quiet line above the composer at 24 h and again at 72 h after creation, while the workspace has one human:
> You're the only person here. Your AI employees work better when your team can see them. [Invite]

It stops for good at two members or after a dismissal. It never appears as a toast, and it is never sent by email in week one.

### (4) Stage 2 referral programme, deferred to stage 2: "Refer a team that pays, you both get $50"

Nothing in this section is built for launch.

- **Trigger moments** (cooldown 30 days per admin, never two asks in a week):
  - the team's first **handoff** (an approval answered or a session picked up);
  - the team's first **paid invoice**;
  - an admin approving a **session-link join**, which proves they share links.
- **Mechanism:** Settings → "Refer a team" shows the workspace's link `thelightbulb.company/?ref=CODE`, a pre-written message and a copy button. The "Start your own workspace" button on `/s` carries the sharing workspace's code server-side, so a session link becomes a referral link with no extra step.
- **Incentive:** double-sided, led with the dollar figure ("$50 off for both teams", per the skill's under-$100 rule).
  - When the referred team's **first full-price invoice is paid** and 14 days pass, both workspaces get a −$50 Stripe customer balance on their next invoice.
  - The cap is 12 rewards per referrer per year.
  - There are no self-referrals: the referrer and the referee are different workspaces with no shared admin.
  - Because the reward is a balance and not a coupon, the referee's first invoice still counts as "paying at list". This keeps §7's rule: there is no discount *to acquire*, only a thank-you after paying.
- **Sizing:** $100 per counted pair, against an assumed $600 twelve-month value, sits inside the skill's B2B range ("$50–500 or 1–3 months free").
- **Reuse of `/launch`:**

| Reuse | Change |
|---|---|
| `_new_code`, `?ref=` capture (`LaunchRefCapture`, `useReferralAttribution`), rate-limited attribute route | key by **workspace id**, not email |
| `mark_referral_counted` CAS, `on_entering_live` hook | count on the first *paid invoice* (`invoice.paid`, amount > 0), not on entering live; remove the `org_` early return |
| Metronome grant pattern | replace with a Stripe customer-balance transaction (idempotency key `ref:<referrer>:<referee>`) |
| Sheet and queue jump | not reused; the competition is not set up (post-product work) |

- **Nurture, as a marketing loop** (skill anatomy):
  - **Cadence:** weekly.
  - **Acts when:** an admin hit a trigger moment and is outside the cooldown.
  - **Body:** the CEO agent drafts one line in the admin's DM, *staged* for a founder to approve in stage 2's first month.
  - **Self-check:** skip if the workspace had a failed run or a Stop in the last 48 h.
  - **State:** a last-asked timestamp per admin.
  - **Stop:** kill switch in admin settings; halt on any complaint.

### (5) Honest limits

- **Sessions inside apps have no terminal.** Claude app, Codex app, IDE and SDK sessions are `conversation_only` and can never be "Anyone". The owner must use "Open in a terminal" first. This is the default way many people run Claude Code (unverified share).
- **Codex sessions cannot be picked up or continued.** The panel's promise shrinks to "type in this session or see the rest", and the stranger branch has no Continue.
- **Only the Mac app shares.** Linux and Windows users, and plugin-only users, cannot start the loop.
- **Live only.** An ended or offline session shows no screen to outsiders, so a link posted on X loses most of its pull within hours. The panel's ended-state copy is the only salvage.
- **20 viewers per link.** A Show HN post would turn most visitors away. The cap rises only after a load test, with a "full / N people watching" state regardless (T6).
- **Secrets on screen.** The terminal is unredacted (titles are redacted, the screen is not). Founders should share public sessions they chose for that purpose, not their working session. The share dialog should say so.
- **The sixth member costs $20 a month.** Seat billing is built before any of this ships. Session-link joins could push a team past five, so owners allow knowing that.
- **Small base.** Few workspaces have the Mac app paired (count not pulled). Expect single-digit session-link joins a month in stage 1. That makes it a launch story first and a loop second.

### (6) Build tasks (Forge-ready)

| # | Title | What today | What changes | Why | Measure |
|---|---|---|---|---|---|
| T1 | One `member_joined` event for every door | joins recorded per door, without a source; invitation acceptance not emitted | Emit `member_joined {via, members_count, invited_by}` from the `organizationMembership.created` handler; `via` from a short-lived marker each door writes before the Clerk call; emit `team_formed` when `members_count` reaches 2 within 48 h of workspace creation | Monday number "team formed" has no event (item 7) | event present for 100% of joins in dev acceptance |
| T2 | Count anonymous session-link opens | `share_link_opened` only when signed in | Emit it for signed-out opens too, with `signed_in:false`, `link_ref`, distinct id `anon:<link_ref>`, once per page load | The loop's top of funnel is invisible | opens per "anyone" link per week |
| T3 | "Ask to join" on `/s` | an "anyone" viewer gets no ask; Ask to join exists for teammates | The panel and copy in (2); open the existing Ask to join to a signed-in outsider (`POST /public/session-links/join-request`); owner Needs-you item + email; Allow → `create_organization_membership` and the session's controls; per-session switch, default on | Plan item 4; Figma's ask-to-edit pattern | joins from session links / week |
| T4 | "Start your own workspace" with source | Continue's "Create your free workspace" shows only when Continue is on | Show for every non-member on an "anyone" page; stash the source server-side; `workspace_created {source: session_link}` | the stranger branch; stage-2 attribution | workspaces created from `/s` |
| T5 | First-session invite line | the welcome asks only for the business; band card copies the link | the CEO follow-up after the first reply, Get started item 1, the 24/72 h one-person line, all as in (3) | Plan item 1 | team-formed rate (48 h) |
| T6 | `/s` when a link is full | the 21st viewer sees "Could not reach this session. Trying again." | a "full / N people watching" state with both buttons; raise `MAX_LINK_VIEWERS` only after a load test | Show HN traffic | 409s per link vs CTA clicks |
| T7 | Workspace referrals (deferred to stage 2) | `/launch` counting returns for `org_` owners and the sheet refuses orgs | workspace-keyed codes; count on the first full-price `invoice.paid`; −$50 customer balance to both; caps, self-referral check, terms page; Settings → Refer a team | Plan stage 2 | referred paying teams / month |
| T8 | Share dialog warning | the "Anyone" choice has no screen warning | one line: "Anyone with the link sees this terminal live, including anything printed in it." | the secrets risk | none (safety) |

T1, T2 and T5 overlap workstreams 09 (analytics) and 08 (onboarding). Build them once, from whichever ships first.

### (7) Measures, tied to the Monday numbers

| Monday number (App. C) | Loop measure | First read | Decision rule |
|---|---|---|---|
| Team formed | team-formed rate (48 h), split by `via` of the second person | 4 weeks after T1 + T5 | if the CEO line moves team formed by < 5 points against the 4 weeks before, rewrite the copy before adding more prompts |
| Team formed | joins from session links / week; opens → requests → approvals | 4 weeks after T2 + T3 | if < 1 join per 20 "anyone" links with an outside viewer, treat `/s` as a launch demo, not a door, and stop investing |
| Setup requests / sign-ups | workspaces created from `/s` (`source: session_link`) | launch week | reported per launch post |
| First handoff | share of session-link joiners who pick up or join a session in week one | 6 weeks | if under a third, the panel is over-promising |
| Paying at list | referred teams paying their first full-price invoice; reward cost per paying team | from stage 2 | keep if referred teams cost under $100 each and churn no worse than others |

The Monday readout adds two lines: "second people this week, by door" and "session-link funnel: opens → asks → joins".

## Founder decisions (13 Oct 2026)

- D1: an outsider on a public session page gets the same "Ask to join" teammates have; the owner taps Allow, and that also makes them a workspace member with the session's controls. One door.
- D2: "Viewers can ask to join" is on by default for Anyone-with-the-link sessions; the owner can turn it off per session.
- D3: raise the viewer cap only after a load test; a "full / N watching" state regardless.
- D4: the public session page is the only no-account look; nothing more for launch.
- A11 / A14: referrals, including the $50 credit, are deferred to stage 2. The session-page door and the first-session invite stay in stage 1.
- A10: the launch competition is not set up, so no `/launch` credit or coupon exists to honour. Setting it up is post-product work.

## Sources

- Code on `origin/main` @ `6249f614d`: `apps/backend/core/services/local_sessions.py`, `session_links.py` (copies live here; there is no `session_copies.py`), `session_pickup.py`, `workspace_join.py`, `workspace_invitations.py`, `routers/orgs.py`, `routers/launch.py`, `launch_referral.py`, `launch_service.py`, `stripe_webhook_handlers.py`, `messaging.py`, `ops/growth.py`, `observability/session_analytics.py`; `apps/frontend/src/app/s/page.tsx`, `lib/analytics.ts`, `components/messaging/OnboardingBand.tsx`, `components/launch/*`; `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md`.
- `docs/business-plan.md` (items 1 and 4, §7, §8, Go To Market, App. C); `.agents/product-marketing.md` v7.
- Loom: [Shahed Khan interview, Latka 2019](https://getlatka.com/companies/loom/interviews/loom-shahed-khan-2019).
- Figma: [Request to edit a file](https://help.figma.com/hc/en-us/articles/4408435431319-Request-to-edit-a-file); [Share files and prototypes](https://help.figma.com/hc/en-us/articles/360040531773-Share-or-embed-your-files-and-prototypes).
- Notion: [Duplicate public pages](https://www.notion.com/help/duplicate-public-pages); [Affiliate programme](https://www.notion.com/affiliates) (up to $50 per activated sign-up, closed to new affiliates).
- Replit: [Remix an app](https://docs.replit.com/features/projects-and-artifacts/remix-an-app); [Sacra summary](https://sacra.com/chat/h/067eb151-4c1f-4add-b07b-66d0abc748df/) (low).
- Zed Delta: [Introducing Delta, 12 Aug 2026](https://zed.dev/blog/introducing-delta); [Delta public beta, 16 Sep 2026](https://zed.dev/blog/delta-public-beta).
- Skills: `marketing-skills:referrals` (trigger moments, double-sided rewards, sizing, K-factor); `marketing-skills:marketing-loops` (loop anatomy, cooldowns, human checkpoint).
- Not pulled: PostHog counts for the events above (owned by workstream 09; no production reads were made for this file).
