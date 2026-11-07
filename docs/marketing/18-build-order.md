# Build order: every product and site task from the marketing pass

## Purpose

Product and site work from workstreams 01–17, duplicates merged, founders' 13 Oct answers applied.
Dependencies first; file on the board one at a time, top to bottom.
"Today" lines checked against `origin/main` at 6249f614d.

## Standing rules

- Fix forward: build the real thing before copy depends on it. No interim lines, no billing by hand.
- Seat billing and the trial checkout (T04–T05) ship before the one-plan chooser, `/pricing`, the comparison pages and any copy that quotes a price.
- Base and Ultra are retired fully. One plan: $50 for up to 5, then $20 a member.
- Self-serve stays no-card for 30 days; founding teams put a card on file at setup; first charge on day 31.
- Every event carries `workspace_id` and `environment`. A rejected approval counts as a handoff.
- Shipped copy uses the plan's words; never "multiplayer", "AI-native", "orchestration", "bot", "prompt".
- Order: measurement first, then billing (before the first setup), then items 1–9, then site.

## Contents

| T | Title | Depends on | Size | Item |
|---|---|---|---|---|
| T01 | Event hygiene: environment, identity, room runs | nothing | M | 7 |
| T02 | Join events: `workspace_created`, `member_joined`, `team_formed` | T01 | M | 7 |
| T03 | Handoff events | T01 | M | 7 |
| T04 | Seat billing from the 6th member | nothing | M | 5 |
| T05 | Trial checkout: card at setup, first charge day 31 | T04 | M | 5 |
| T06 | One plan: Base and Ultra retired | T04, T05 | M | 5 |
| T07 | Members may invite; invitations land on an explaining page | nothing | M | 1 |
| T08 | The CEO asks "Who should see this with you?" | T07 | M | 1 |
| T09 | The first approval asks who should decide | T07, T03 | M | 1 |
| T10 | Onboarding band: invite first; one-person reminders | T08 | M | 1 |
| T11 | Sign-up: two doors and GitHub sign-in | T06 | M | 1 |
| T12 | Workspace form and first-touch source | T01 | M | 7 |
| T13 | The first run ends with one finished thing | nothing | M | 2 |
| T14 | Welcome email from the CEO agent's address | T06, T07 | M | 2 |
| T15 | Away email | T01 | L | 3 |
| T16 | Session page: count opens, "Start your own workspace", share warning | T12 | M | 4 |
| T17 | Ask to join for outsiders | T02 | L | 4 |
| T18 | "Full / N watching", then a load test before raising the cap | nothing | M | 4 |
| T19 | Trial end: day-28 recap, reading-only, one pause, 90 days | T05, T06 | L | 5 |
| T20 | Workspace export | nothing | M | 5 |
| T21 | Phone app in the App Store without in-app purchase | T06 | M | 6 |
| T22 | `/request-setup` with the source question | T12 | M | 7 |
| T23 | Remaining Monday-number events | T02, T05, T22 | M | 7 |
| T24 | Monday readout | T02, T03, T22, T23 | M | 7 |
| T25 | Phone: a teammate you text | T07 | L | 9 |
| T26 | Opt-out footer on agent mail | nothing | S | — |
| T27 | Site cleanup: channels, thin pages, `flickmeon.ai`, dev index | nothing | M | site |
| T28 | Homepage hero: two buttons, headline test | T22 | M | site |
| T29 | Homepage sections, metadata, voice | T28 | M | site |
| T30 | `/pricing` and every price on the site | T04, T05, T06 | M | site, 5 |
| T31 | Six comparison pages (incl. the session-sharing page) | T30, T22, T17 | L | site |
| T32 | Blog bylines and the get-started doc | T22, T13 | M | site |
| T33 | Data page | nothing | S | site |
| T34 | Plugin repo: MIT licence, README for a stranger | T22 | S | site |
| T35 | Run routing and Stop ledger | T01 | M | 8 |

---

### T01. Event hygiene: environment, identity, room runs
- **From:** 09 tasks 1–3; item 7
- **Depends on:** nothing
- **Today:** `components/PostHogProvider.tsx` derives `environment` from `NEXT_PUBLIC_API_URL`, so dev pages can read `prod`. The usage callback in `core/services/messaging.py` returns `ignored` for room runs before `run_analytics.schedule_run_completed`.
- **Change:** derive web `environment` from the hostname (`thelightbulb.company`/`www.` → `prod`, `flickmeon.ai` → `launch`, else `dev`). `capture_product_event`: an `org_` distinct id becomes `workspace_id`, the creator the person. Call `schedule_run_completed` from the room `run.ended` path with the requester as person.
- **Why:** every Monday number is counted per workspace and environment.
- **Measure:** zero `org_` distinct ids; zero dev-host events tagged `prod`; room-run events within 5% of the run ledger.
- **Size:** M

### T02. Join events: `workspace_created`, `member_joined`, `team_formed`
- **From:** 06 T1, 08 task 1, 09 task 4, 12 task 6; items 4, 7
- **Depends on:** T01
- **Today:** none exists. The membership handler in `routers/webhooks.py` seats the member (`on_org_member_joined`) and emits nothing.
- **Change:** `workspace_created` from `_handle_organization_created`; `member_joined {via, members_count, invited_by}` from `organizationMembership.created`, `via` from a short-lived marker each door writes first; `team_formed` once (uuid5 of the org id) when human members reach 2 within 48 h.
- **Why:** "team formed" is the activation event and has no emitter.
- **Measure:** `member_joined` on 100% of dev joins; `team_formed` matches Clerk two Mondays running.
- **Size:** M

### T03. Handoff events
- **From:** 08 task 1, 09 task 5, 12 task 6; item 7; D12
- **Depends on:** T01
- **Today:** only `session_picked_up` exists (`core/services/session_pickup.py`).
- **Change:** `approval_answered {decision}` from approve and reject in `routers/teams/approvals.py`; `task_moved` when a person changes the status of an agent-assigned task; `handoff_completed {kind}` once per workspace on the first of the three in week one.
- **Why:** "first handoff" is a Monday number.
- **Measure:** the Monday query names workspaces a founder confirms.
- **Size:** M

### T04. Seat billing from the 6th member
- **From:** 15 task 1, 07 task 1, 01; item 5; A7, A8
- **Depends on:** nothing
- **Today:** `core/services/billing_service.py` checks out one line item at `quantity: 1`; members are not counted.
- **Change:** a second subscription item on a $20 Price, `quantity = max(0, human members − 5)`, set on membership created/deleted with `create_prorations`. Apple-billed rows excluded.
- **Why:** the plan's price cannot be published until it is billed.
- **Measure:** seat revenue on invoices; zero seat disputes.
- **Size:** M

### T05. Trial checkout: card at setup, first charge day 31
- **From:** 15 tasks 2, 3, 8, 11; 07 task 1; 01; item 5; A4
- **Depends on:** T04
- **Today:** `create_workspace_checkout` charges at once with `allow_promotion_codes=True` (`billing_service.py:495`). `trial_cap.py` and `provision_gate.py` docstrings say "3-day window" for a 30-day trial.
- **Change:** Checkout whose trial ends on the workspace's own `trial_end`; founders use it at setup, self-serve teams from the day-28 card (T19); the row stays `trial_local` until day 31. Two-year `price_lock_until` on founding subscriptions. `allow_promotion_codes=False`. Fix the docstrings.
- **Why:** the founding offer is unbillable without it.
- **Measure:** founding teams with a card and a trial end date at setup.
- **Size:** M

### T06. One plan: Base and Ultra retired
- **From:** 15 task 4, 05 B3; item 5; A9
- **Depends on:** T04, T05
- **Today:** `billing_service.py` maps base, standard and ultra prices; `apps/mobile/src/components/billing/PaywallScreen.tsx` lists Base and Ultra; the chooser shows three cards.
- **Change:** one "Team: $50 for up to 5, $20 a member from the 6th" block (15 §C) on web and `PaywallScreen`; one price constant every price line reads. Our workspaces move to it on the internal coupon, restricted to them.
- **Why:** one plan is what the site and calls will say.
- **Measure:** chooser → Checkout start rate.
- **Size:** M

### T07. Members may invite; invitations land on an explaining page
- **From:** 08 tasks 2 and 12; item 1; D5
- **Depends on:** nothing
- **Today:** email invitations and the join-link mint in `routers/orgs.py` are admin-only (`_require_admin_of`); `workspace_invitations.py` redirects invitees to `/sign-up`.
- **Change:** any member may send invitations and copy the link; revoke stays admin-only. Redirect to a new `app/join/invite/page.tsx` naming the inviter and workspace. Write T02's `via=invite` marker.
- **Why:** the person who wants the cofounder in is often not the admin.
- **Measure:** invitations sent by non-admins; accepted / sent.
- **Size:** M

### T08. The CEO asks "Who should see this with you?"
- **From:** 08 tasks 2–3, 04 task 1, 06 T5; item 1
- **Depends on:** T07
- **Today:** `components/messaging/appLinks.ts` has no `invite` kind; after `_FIRST_MESSAGE` nothing asks.
- **Change:** app-link kind `invite` rendered by `RoomAppLinks` as email field, Send, Copy join link, "Just me for now". After the first `run.ended` in the default room of a one-person workspace, the CEO posts the static line with that card, once (marker on room META).
- **Why:** every retained customer is a workspace where a second person showed up.
- **Measure:** new workspaces with an invite sent in session 1; `team_formed`.
- **Size:** M

### T09. The first approval asks who should decide
- **From:** 17 T1; item 1
- **Depends on:** T07, T03
- **Today:** approval cards go to members only and invite nobody.
- **Change:** on the first approval card in a one-person workspace, "Ask someone else to decide" with an email field; the invitation names the decision and opens the card after sign-up.
- **Why:** the second person joins for a reason.
- **Measure:** `team_formed`, split by invitations that carry a card.
- **Size:** M

### T10. Onboarding band: invite first; one-person reminders
- **From:** 08 tasks 4–5; item 1
- **Depends on:** T08
- **Today:** `packages/shared/src/messaging/onboardingSteps.ts` puts connectors first; `components/getstarted/getStartedConfig.ts` is imported only for a type and still says "Slack, Discord or Telegram".
- **Change:** 08 §3 order with "Invite your team" first, "2 of 6 done", T08's card, "Answer your first approval". Delete the dead `getStartedConfig.ts` steps and `get_started_*` events. Quiet one-person lines in `WorkspaceSidebar.tsx` and the #general member list until someone joins.
- **Why:** the reminder stays until someone joins.
- **Measure:** invites sent after day 1.
- **Size:** M

### T11. Sign-up: two doors and GitHub sign-in
- **From:** 08 task 10, 10 task 9, 05 B7; item 1; D6, D7
- **Depends on:** T06
- **Today:** `app/sign-up/[[...sign-up]]/page.tsx` reads "…starts here." over Clerk `<SignUp/>`; GitHub is off in the Clerk instance; sign-up and `join` inherit the old root title.
- **Change:** 10 Section C: "Bring your team.", create a workspace or open your teammate's invite. Enable GitHub and require first name in Clerk, dev then prod; keep the emailed code. Title "Create your team's workspace" with canonical; `noindex` on sign-in and join.
- **Why:** the wedge signs in with GitHub; the join door must be visible.
- **Measure:** `signup_started` → `trial_started`; join-door and GitHub shares.
- **Size:** M

### T12. Workspace form and first-touch source
- **From:** 08 task 11, 07 task 4, 09 task 7, 14 task 4, 13 C3, 16 task 3; item 7; D13
- **Depends on:** T01
- **Today:** `app/onboarding/page.tsx` has no source question; no `source_reported` event; first-touch UTM is not kept.
- **Change:** slug as one line with "Change"; optional "How did you first hear about us?" firing `source_reported`. Keep first-touch UTM, referrer and landing path (including `/s`) through sign-up onto the workspace and `trial_started`.
- **Why:** three quarters of traffic is unattributed.
- **Measure:** new workspaces with a known source over 80%.
- **Size:** M

### T13. The first run ends with one finished thing
- **From:** 08 tasks 6–7, 04 task 2; item 2
- **Depends on:** nothing
- **Today:** `_BRIEF_TEMPLATE` in `core/services/onboarding_brief.py` says who the person is, not what to deliver; connect links are plain URLs.
- **Change:** a result contract in the brief: first reply three sentences at most; one Library document link, one approval card, one connect card, three offers written as outcomes with the person's part named; only the CEO speaks. Mirror in the `lightbulb` skill reference. App-link kind `connect` rendered with `ConnectButton`.
- **Why:** the person sees work done and makes one decision.
- **Measure:** new workspaces with a document and an approval in session 1.
- **Size:** M

### T14. Welcome email from the CEO agent's address
- **From:** 08 task 13, 12 tasks 2, 5, 7; item 2; D9, D10
- **Depends on:** T06, T07
- **Today:** `welcome_email.py` sends from `EMAIL_FROM_WELCOME` on `user.created` (`routers/webhooks.py:841`); `send_to_owner` reaches only the owner; `sender_allowed` admits only `verified_senders`.
- **Change:** delete today's welcome. On membership created, 08 §5 copy from the CEO agent's address: owner version, or a one-line teammate welcome. Add `send_to_member` beside `send_to_owner` (same cap, metrics) and accept replies from workspace members.
- **Why:** the first email is the team's AI employee, and true.
- **Measure:** click-through to #general.
- **Size:** M

### T15. Away email
- **From:** 08 task 8, 12 tasks 3–4; item 3; D8, D9
- **Depends on:** T01
- **Today:** `core/services/daily_digest.py` is the only off-app email.
- **Change:** `core/services/needs_you_email.py` on the digest pattern, every minute: a DM, @mention or approval unseen after five minutes, sent only when the person is not connected (read cursor and live activity; AppSync has no disconnect), linking to the message. One per room per 30 minutes; none 22:00–08:00 local; invited members included. Setting `awayEmail` with its own unsubscribe. Emit `needs_you_email_sent` and `notification_opened {channel, latency_s}`.
- **Why:** the second person must learn something happened.
- **Measure:** median "needs you" → opened.
- **Size:** L

### T16. Session page: count opens, "Start your own workspace", share warning
- **From:** 06 T2, T4, T8; item 4
- **Depends on:** T12
- **Today:** `share_link_opened` fires only for signed-in viewers (`session_links.py:236`); "Create your free workspace" shows only inside `app/s/page.tsx`'s Continue block; the printed-secrets warning covers workspace sharing (`DesktopPermissionsGate.tsx`), not the per-session Anyone choice.
- **Change:** `share_link_opened {signed_in:false, link_ref}` once per load, distinct id `anon:<link_ref>`. "Start your own workspace" for every non-member, source stashed server-side, `workspace_created {source: session_link}`. Where Anyone is chosen: "Anyone with the link sees this terminal live, including anything printed in it."
- **Why:** the loop's top of funnel is invisible.
- **Measure:** opens per link per week; workspaces created from `/s`.
- **Size:** M

### T17. Ask to join for outsiders
- **From:** 06 T3 (replaces 07's join-link task); item 4; D1, D2
- **Depends on:** T02
- **Today:** teammates ask through `/messaging/sessions/{id}/join-requests` (`routers/messaging.py:482`); public routes are only `open` and `viewing`.
- **Change:** open that Ask to join to a signed-in outsider on an Anyone page. Owner gets a Needs-you item and email; Allow creates the membership (`via=session_page`) and grants the session's controls. Per-session switch, on by default. No join link on the page.
- **Why:** the one surface outsiders already reach.
- **Measure:** joins from session links per week; their first handoffs.
- **Size:** L

### T18. "Full / N watching", then a load test before raising the cap
- **From:** 06 T6, 07 task 7; item 4; D3, B11
- **Depends on:** nothing
- **Today:** `MAX_LINK_VIEWERS = 20` (`session_links.py:270`); the 21st viewer sees "Could not reach this session. Trying again."
- **Change:** a "Full: N people are watching" state with both buttons; load-test viewers per link and raise the cap only to what holds.
- **Why:** Show HN traffic will hit the cap.
- **Measure:** full-state views versus button clicks; the tested ceiling.
- **Size:** M

### T19. Trial end: day-28 recap, reading-only, one pause, 90 days
- **From:** 15 tasks 5–7, 12 (E7); item 5; A1, A12, A13
- **Depends on:** T05, T06
- **Today:** no recap route; lapse copy is "Pick a plan to keep your agents". No refund promise (A1).
- **Change:** `GET /billing/trial-recap` (handoffs, approvals, agents, people, top item) and a card on days 28–30, once per person, opening T05's checkout; same on phone Home; a day-27 email from the CEO agent. 15 §D reading-only copy everywhere the lapse shows; members get "Ask {admin}". `paused_at` for one 30-day pause per company; 90 days reading-only, then the standard deletion notice.
- **Why:** value before the ask, nothing lost after it.
- **Measure:** card → checkout rate; reactivations after day 31.
- **Size:** L

### T20. Workspace export
- **From:** 15 task 9; item 5
- **Depends on:** nothing
- **Today:** no export route under `routers/`.
- **Change:** owner-only zip of room transcripts and board tasks, offered in reading-only.
- **Why:** a team that leaves takes its record.
- **Measure:** exports requested.
- **Size:** M

### T21. Phone app in the App Store without in-app purchase
- **From:** item 6
- **Depends on:** T06
- **Today:** TestFlight only; `APPLE_IAP_SELLING` is off in prod.
- **Change:** submit the production build with in-app purchase off; subscriptions stay on the web; text invitations link to the listing.
- **Why:** TestFlight is a wall for a teammate invited by text.
- **Measure:** installs from invitations.
- **Size:** M

### T22. `/request-setup` with the source question
- **From:** 10 task 5, 01, 02 task 1, 03 task 1, 07 task 3, 09 task 7, 13 C3; item 7; D13, F3
- **Depends on:** T12
- **Today:** no such route.
- **Change:** 10 Section D's five fields; "How did you first hear about us?" required (Hacker News, Product Hunt, plugin, X, Reddit, LinkedIn, a friend, other + text); optional thread link. Store UTM, first-touch path, `?source=` and anonymous id; email Aden; emit `setup_requested`.
- **Why:** the stage-1 door.
- **Measure:** setup requests by source, 100% self-reported.
- **Size:** M

### T23. Remaining Monday-number events
- **From:** 09 tasks 6, 8–10, 10 task 11, 12 task 6, 05 B11; item 7
- **Depends on:** T02, T05, T22
- **Today:** the plan says `week4_active` comes from "the existing sweeper": none exists. `STRIPE_WEBHOOK_HANDLERS` lacks `invoice.paid`; `trial_converted` fires on status, coupons included. `landing_viewed` fires only on `/`. `ops/growth.py` has no environment or host filter. `@vercel/analytics` is not installed.
- **Change:** a daily pass emitting `week4_active` (teams formed with a human message in week four). Handle `invoice.paid`, emit `invoice_paid {at_list}`, and `trial_converted` only on the first at-list invoice. `landing_viewed` on any marketing route; `setup_cta_clicked` on setup buttons. Filter `ops/growth.py`. Vercel Web Analytics on.
- **Why:** the five numbers come from real events.
- **Measure:** paying-at-list equals Stripe; Vercel within 20% of PostHog.
- **Size:** M

### T24. Monday readout
- **From:** 09 task 11, 01, 15 task 10, 16 task 2; item 7
- **Depends on:** T02, T03, T22, T23
- **Today:** none under `apps/backend/scripts/`.
- **Change:** `scripts/monday_readout.py`: 09 §4 queries with the exclusion list, Clerk and Stripe reads, the founders' outbound sheet, each founding team's lapse signals and teams reaching day 28; one page to the founders' channel, never the repo. Stop counts wait for item 8.
- **Why:** the weekly review.
- **Measure:** four unattended Mondays.
- **Size:** M

### T25. Phone: a teammate you text
- **From:** item 9; 08 tasks 9, 14
- **Depends on:** T07
- **Today:** the app mirrors the web workspace; no approval push copy in `notification_copy.py` or `push_service.py` (unverified whether approvals push); `WorkspaceGate.tsx` leads with "Create a workspace".
- **Change:** the CEO's DM is home; push reads as a text from a named teammate with one action ("Finance: invoice 1042 is ready to send. OK?"); approvals push "{Agent} · needs your OK"; outputs open in Library; `WorkspaceGate.tsx` leads with a teammate's invite.
- **Why:** the second person lives on their phone.
- **Measure:** first approval and first "Done" accepted from the phone; push → open rate.
- **Size:** L

### T26. Opt-out footer on agent mail
- **From:** 17 T2; D14
- **Depends on:** nothing
- **Today:** no footer in `agent_mail_send.py`, `agent_mail_delivery.py` or `agent_mail_reply.py`.
- **Change:** one line naming the agent, company and Lightbulb with a `utm_source=agent_email` link; on by default, owner can turn it off.
- **Why:** agent mail reaches people outside the team.
- **Measure:** sign-ups with that source.
- **Size:** S

### T27. Site cleanup: channels, thin pages, `flickmeon.ai`, dev index
- **From:** 05 B3, B4, B9, B10; E1, E4, `/launch`
- **Depends on:** nothing
- **Today:** homepage cards list "All channels (Telegram, Discord, WhatsApp)" (`lib/providerCards.ts:76`); ten `seoLandingPages` in `lib/seo.tsx` under `app/[seoSlug]`; `middleware.ts` serves `/launch` at the `flickmeon.ai` root; no dev `X-Robots-Tag`; LinkedIn `sameAs` 404s.
- **Change:** Telegram, Discord, WhatsApp off public pages. Delete nine thin pages with 308s to the nearest real page, else `/`; `/ai-agents-for-slack` moves with T31. Redirect every `flickmeon.ai` path to the product site. `noindex` keyed on host. Drop `sameAs` URLs that do not return 200.
- **Why:** search and AI engines repeat what they find.
- **Measure:** no 404s on old URLs; noindex on dev only.
- **Size:** M

### T28. Homepage hero: two buttons, headline test
- **From:** 10 tasks 1–2
- **Depends on:** T22
- **Today:** `components/landing/Hero.tsx` has one untracked "Get started free".
- **Change:** "Request a guided setup" → `/request-setup`, "Start free" → `/sign-up`, both firing `signup_cta_clicked {ctaId, headline_variant}`. Cookie-stable A/B on the two H1s from the plan, stored on landing events and the setup form.
- **Why:** the site's one job is a setup request.
- **Measure:** setup requests and trial starts per visitor, per variant.
- **Size:** M

### T29. Homepage sections, metadata, voice
- **From:** 10 tasks 6–8, 05 B2; B6, B7
- **Depends on:** T28
- **Today:** root title "AI Agents for Autonomous Businesses"; copy says "your next hire", "AI-native", "Copy prompt"; a mock stat.
- **Change:** Titles and 05 Part B's `@graph` in `lib/seo.tsx`; subhead "…in your team's channels"; `ProblemBand`, proof (Anna, Second Catch by name), `FoundingTeams`, `HomeFaq` with FAQPage, in the plan's order; three screenshots; voice fixes. Email copy: the owner decides who agents can email. No price (T30).
- **Why:** a founder recognises their situation in five seconds.
- **Measure:** setup requests per visitor.
- **Size:** M

### T30. `/pricing` and every price on the site
- **From:** 05 B1, B3; 10 tasks 3–4; 07 task 2; 11 task 1; item 5; A8
- **Depends on:** T04, T05, T06
- **Today:** no route; nav and footer link `/#pricing`; `llms.txt/route.ts` and `get-started.md` quote Ultra "2 vCPU / 8 GB".
- **Change:** `app/pricing/page.tsx` (10 Section B): $50 for up to 5, $20 a member after, 30 days free, founding two-year lock, managed AI at provider price × 1.2 or your own key, FAQ, JSON-LD, sitemap, `/pricing.md`. Nav and footer point there. Homepage price block replaces the provider-cards-as-plans. Every price reads T06's constant; Base, Ultra and compute claims gone.
- **Why:** the most-asked buying question has no answer.
- **Measure:** indexed within 14 days; pricing → setup or start free.
- **Size:** M

### T31. Six comparison pages (incl. the session-sharing page)
- **From:** 11 tasks 2–7, 05 B5–B6; E2, E7, E8
- **Depends on:** T30, T22, T17
- **Today:** none; `/ai-agents-for-slack` says Lightbulb runs "behind the scenes" of Slack.
- **Change:** 11's six pages (Slack AI, session sharing, Slack alternatives, Grok Bot, Microsoft Teams + Copilot, Bloome) from `content/compare/index.ts` with a source URL and `checkedOn` per cell; `/compare/[slug]` plus hub; the session-sharing page at 05's `/claude-code-session-sharing`. Leo's byline; `?source=compare-<slug>` on buttons. Ship `/ai-agents-for-slack` → `/compare/slack` in the same release; a test fails on a cell without URL or date.
- **Why:** "X vs Y" queries are where AI answers shortlist products.
- **Measure:** pages return 200; setup requests per page.
- **Size:** L

### T32. Blog bylines and the get-started doc
- **From:** 13 C1–C2, 05 B8, 10 task 10; E3
- **Depends on:** T22, T13
- **Today:** posts are by "Lightbulb"; "What is an AI-native company operating system?" is live with Paperclip tags; `get-started.md` is a solo session.
- **Change:** `author` in front matter (Prasiddha product, Leo comparisons, Aden customer stories); both doors at post end with the landing UTM. Rewrite or 308 the off-voice post; strip internal-name tags. Rewrite `/docs/get-started` around the team (10 Section E).
- **Why:** signed posts; a doc that matches the setup.
- **Measure:** setup requests first touched on `/blog/*`.
- **Size:** M

### T33. Data page
- **From:** 04 task 3; B9
- **Depends on:** nothing
- **Today:** only `app/privacy/page.tsx`.
- **Change:** `/security`, limited to what the Privacy Policy says plus the three provider paths; Prasiddha signs off.
- **Why:** "what happens to our data" gets a link.
- **Measure:** setup calls that raise it and still book.
- **Size:** S

### T34. Plugin repo: MIT licence, README for a stranger
- **From:** 14 tasks 1–2, 07 task 5, 17 T3; E11, E13
- **Depends on:** T22
- **Today:** `packages/claude-plugin/` has no LICENSE; the README opens with contents; `.codex-plugin/plugin.json` has an empty `hooks` key.
- **Change:** in the monorepo copy (CI overwrites the public repo): MIT LICENSE and three opening lines on what Lightbulb is with `/request-setup` and a 30-second install. Drop the empty `hooks` key if the OpenAI portal rejects it.
- **Why:** directories check licence and docs.
- **Measure:** `claude plugin validate` clean; setup requests from the plugin.
- **Size:** S

---

### T35. Run routing and Stop ledger
- **From:** plan item 8 (the feedback loop); docs/research/2026-10-13-socially-aware-agents.md; founder ruling 13 Oct ("Jev decides and we run stop, we should learn from that")
- **Depends on:** T01
- **Today:** a run does not record whether Jev, the fallback or an overrule started it or Jev's confidence; a Stop does not record who pressed it or after how many seconds; so the cleanest negative signal we have cannot be counted (verified in the research file against `core/services/jev.py`, `openclaw_work.py` and the run ledger).
- **Change:** on each room run, store who routed it (`jev`, `fallback`, `mention`, `dm`) and Jev's confidence on the ledger row; on Stop, who pressed it and seconds since `run.started`; a follow-up check 30 minutes later (replied, reacted, re-asked, ignored) written to the same row; one event per run with those fields, carrying `workspace_id` and `environment`; the Monday readout (T24) gets "stop rate after Jev-routed runs" from it. Nothing changes agent behaviour in this task; the per-room bar and the Mentions-mode suggestion come later, from this data.
- **Why:** item 8's grader and the monthly preference passes need labels, and this is the only free source of them.
- **Measure:** 100% of room runs carry a routing source; stop rate after Jev-routed runs reported on the Monday readout.
- **Size:** M

## Not on this list, on purpose

- **Item 8:** a research track.
- **Stage 2:** referral credit (06 T7), the launch competition (only the `flickmeon.ai` redirect is in, T27), and 12's owner nudges (E1–E3, E5–E6), which the plan puts in stage 2. Welcome (T14), away (T15) and trial-ending (T19) email are in.
- **By hand:** outreach and send sheets (02, 03), Search Console and Bing, LinkedIn and Crunchbase profiles, directory submissions, the Paperclip research template (16), the Friday drafting routine (13 C4).
- **Later:** copyable workflow starter (17 T4).
- **Wrong premise:** 14 task 3 (public Mac download). `/api/download/desktop` already serves the latest DMG without a token and is linked from nav and footer.
- **Not built by ruling:** a per-message email approval gate (B6); a join link on the session page (D1).
