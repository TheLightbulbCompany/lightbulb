# Founder decisions from the execution pass (13 Oct 2026)

Seventy-six questions the seventeen workstreams could not answer themselves, grouped, each with a recommendation. Answer in bulk: "accept all recommendations" plus exceptions is enough. Items marked **blocks** stop a build or an outreach until answered.

## A. Offer, billing, guarantee

| # | Question | Recommendation |
|---|---|---|
| A1 | The guarantee as written ("not using it in week four") lands before the first invoice, so it refunds nothing. Adopt: "If your team stops using Lightbulb in its first 90 days as a paying customer, tell us and we refund every month you paid, and help you take your work with you." (15; also raised by 01, 10, 12) **blocks** the pricing page and the setup script | Yes. 90 days from the first paid invoice, no proof asked, one refund per company, cancelled the same day. Refunds include subscription only; used managed credits are not refunded. |
| A2 | Does the guarantee apply to self-serve teams or founding teams only? (12) | Founding teams now; self-serve when the day-28 card ships. |
| A3 | Accept "ten teams set up on the offer with a card on file by 4 Jan, about three paying by 12 Jan" as the 90-day promise, in place of "ten paying"? (01) | Yes; the plan's "six paying by May" stands. |
| A4 | Card on file at setup, or no-card trial with the card added by day 30? (01, 15) | Card at setup for founding teams, with Stripe's trial ending on the workspace's own day 30; until that checkout ships, founders add the card by hand by day 30. |
| A5 | Aden and Leo to 20 first touches a week from week 3? (01) | Yes, reviewed against the close rate at the week-2 check. |
| A6 | Pause setups over the holidays? (01) | Pause 21 Dec to 4 Jan; keep outreach running. |
| A7 | A 6th member before seat counting ships? (01) | Bill by hand in Stripe (quantity on a $20 item), prorated. |
| A8 | Publish the plan's price ($50 for up to 5) on `/pricing` now, or today's checkout prices until billing changes? (05, 10, 11, 13) **blocks** `/pricing`, the comparison pages, the "people free up to five" post | Publish $50 for up to 5 now with one honest line ("$20 a member from the 6th starts when seat billing ships"); checkout keeps charging $50 until then. |
| A9 | Do existing Base ($20) workspaces stay at $20? (15) | Yes; Base leaves the public list only. |
| A10 | Does the launch competition's free-month coupon (redeem by 31 Oct) stand? (15) | Honour it; it predates the no-coupon rule. |
| A11 | Honour or retire the `/launch` referral credits on 1 Dec? (06) | Honour anything earned; the mechanism is repointed at paying-team referrals after 1 Dec. In code it cannot fire for workspace owners today, so check prod rows before promising. |
| A12 | Offer a one-time 30-day pause when a lapse is about timing, or refund only? (15) | Offer the pause once; it keeps the record and the relationship. |
| A13 | How long is a reading-only workspace kept after day 31? (15) | 90 days, then the standard deletion notice. |
| A14 | Stage-2 referral reward: $50 Stripe balance credit, or a free month? (06) | The credit; a free month breaks "paying at list". |

## B. Naming customers and making claims

| # | Question | Recommendation |
|---|---|---|
| B1 | May Second Catch be named to prospects and on the site, or stay "a two-founder team"? (03, 04, 10) **blocks** outreach proof and the homepage | Ask them this week (B5); until then "a two-founder team". |
| B2 | Which second team will consent to be named for the launch? (07) | Ask CABLE or Brainwaves once they are set up; Anna is already cleared. |
| B3 | Publish the real numbers in the Show HN first comment (57 accounts, about five active teams, no one paying list)? (07) | Yes, as the plan does; HN rewards it and punishes the alternative. |
| B4 | May posts and listings use our own numbers (33 of 38 one-person workspaces, $55 revenue) and a shared-session screenshot from a founder workspace? (13, 14) | Yes to the numbers; screenshots only of the founders' own workspace with nothing customer-related in frame. |
| B5 | Who asks Anna and Second Catch for consent this week? (13) | Aden, in their shared channels. |
| B6 | May public copy promise that outbound email waits for a person? (10) | Not until the code gates it; it is a build task, not a copy line. |
| B7 | Does the subhead "the rooms your team already talks in" read as "inside Slack"? (10) | Change to "in your team's channels". |
| B8 | Quote Slack Pro's price in objections? (04) | No; the Business+ figure is enough and Pro changes often. |
| B9 | The data answer: what do we say about model vendors and training on customer data, and who answers in writing? (04) | Say only what the Privacy Policy says (isolated AWS environment; content not sold or used to train models) plus the provider path the team chose; Prasiddha answers in writing within a day; a data page is a build task. |
| B10 | May we name the open-source projects we run on in developer communities? (17) | Yes for Hermes and Paperclip as "built on"; never as our product's name. |
| B11 | Use Prasiddha's live terminal on a clean repo as the Show HN link? (07) | Yes, on a public demo repo, with the 20-viewer cap raised or a fallback page ready. |

## C. Outreach and research

| # | Question | Recommendation |
|---|---|---|
| C1 | Who runs the 45-minute setup sessions? (03, 04, 48) | The founder who sourced the team runs it; Aden is the default. The customer answers the first approval card, never the founder. |
| C2 | Will each founder reply in public threads in his own name, no pitch, never a DM? (02, 03) | Yes. |
| C3 | Does the channel owner sign every message from that channel (Leo X/Reddit, Aden LinkedIn, Prasiddha HN)? (03) | Yes. |
| C4 | Refuse to book a setup when the founder comes alone? (17) | Ask for the second person at booking; book anyway if they insist, and make the first approval card the invite. |
| C5 | Cross-role teams like CABLE before pure engineering teams? (02) | Yes; a non-engineer shipping with agents is the thesis in one team. |
| C6 | Are Foremerge, Feyn's Critic and Driver AI customers, partners or neither? (02) | Neither for now; revisit at stage 2 partnerships. |
| C7 | A $50 gift card for churn interviews under the no-coupons rule, and who pays? (16) | Allowed (research, not acquisition); company card. |
| C8 | May Aden email the 24 day-one churners at their sign-up addresses? (16) | Yes, once each, from his own address, with the consent line. |
| C9 | Report the "very disappointed" share separately for wedge teams and small businesses? (16) | Yes, with n. |
| C10 | 20-minute churn calls, or the skill's 30? (16) | 20; these people left after one day. |
| C11 | Can Aden share three past LinkedIn posts so drafts match his voice? (13) | Yes. |
| C12 | Is Leo's Reddit karma at least 50? (13) | Leo checks; below 50, replies only, no posts, until it is. |
| C13 | Which accelerators do the three of you have real contacts at? (17) | Founders list them in the outreach log this week. |
| C14 | Mail a light bulb after a team's first handoff? (17) | Yes, cheap and memorable; founders' own address as sender. |
| C15 | A public address that reaches our CEO agent, every reply founder-approved? (17) | Not yet; the shared channel per team is the line. |

## D. Product doors, sharing, notifications

| # | Question | Recommendation |
|---|---|---|
| D1 | Accept "Ask to join" with admin approval in place of the join link on the public session page? (06) **blocks** item 4 | Yes; a join link on a public page admits anyone. |
| D2 | "Viewers can ask to join": default on or off? (06) | On for "Anyone with the link" sessions; the owner can turn it off. |
| D3 | Raise the 20-viewer cap for launch week? (06, 07) | Yes, after a load test; a "20 people are watching" state regardless. |
| D4 | Accept the sign-up barrier for Show HN visitors, or build a no-account way to look first? (14) | The public session page is the no-account look; nothing more. |
| D5 | May members, not only admins, send invites? (08) | Yes. |
| D6 | Keep the emailed sign-up code? (08) | Keep; Clerk's default and it reduces fake sign-ups. |
| D7 | Add GitHub sign-in? (08) | Yes; the wedge lives there. |
| D8 | Default quiet hours for away emails? (08) | 22:00–08:00 in the person's time zone, and never more than one per room per 30 minutes. |
| D9 | Five minutes before an away email; do invited members get the lifecycle emails? (12) | Five minutes; invited members get the teammate welcome and the away emails, not the owner nudges. |
| D10 | Welcome email from a founder's name or the CEO agent's address? (08, 12) | The CEO agent's address, in the agent's voice, replacing the current welcome entirely. |
| D11 | Is the Thursday check-in sent by a founder, or drafted by the Growth agent? (12) | Drafted by the agent, sent by the founder who runs that team. |
| D12 | Does a rejected approval count as a handoff? (09) | Yes; a person decided. |
| D13 | Make the onboarding source question required? (09) | Optional at onboarding, required on the setup form. |
| D14 | An opt-out footer on customers' agent emails, on by default? (17) | Yes, on by default; check whether agent mail already carries one. |

## E. Site, SEO, content

| # | Question | Recommendation |
|---|---|---|
| E1 | Delete all ten thin `[seoSlug]` landing pages? (05) | Yes, with redirects to the nearest real page. |
| E2 | Keep or retire `/ai-agents-for-slack` (its story contradicts the comparison page)? (11) | Redirect it to the Slack comparison page. |
| E3 | Named author for blog and comparison pages? (05, 11) | Prasiddha for product and engineering, Leo for comparisons, Aden for customer stories. |
| E4 | Do the Telegram, Discord and WhatsApp channels work today? If not, off the homepage. (05) | Forge verifies; anything unverified comes off. |
| E5 | Is there a LinkedIn page under the Lightbulb name, and who grants Search Console access? (05) | Aden confirms the page; Prasiddha grants Search Console and Bing. |
| E6 | Hold the comparison pages until billing ships, or publish with "See pricing"? (11) | Publish with the honest pricing line from A8. |
| E7 | Should Bloome get its own comparison page? (11) | Not yet; it sits on the session-sharing page. |
| E8 | Who signs off comparison-page edits? (11) | Leo. |
| E9 | Publish the "why people are free up to five" post when `/pricing` goes live? (13) | Yes, with the A8 line. |
| E10 | Edit old `flickmeon.ai` posts to add UTMs? (09) | No; too old to matter. Tag everything from now. |
| E11 | Which licence for the plugin repo? (14) | MIT, matching the public plugin's purpose. |
| E12 | Who owns the Product Hunt maker account? (14) | Prasiddha. |
| E13 | Launch without the Codex directory listing if it isn't open to us, or has hooks that disqualify it? (07, 14) | Yes; the `codex plugin marketplace add` route already works. |
| E14 | Does G2 treat "early access" as beta and refuse the listing? (14) | Check when the site gate passes; not a stage-1 item. |

## F. Data and ownership

| # | Question | Recommendation |
|---|---|---|
| F1 | Which founder owns the internal-workspace exclusion list for the Monday numbers? (09) | Prasiddha. |
| F2 | Do we want PostHog's paid group analytics? (09) | No; a plain `workspace_id` property does the job. |
| F3 | Who answers setup requests, and from which address? (10) | Aden within one business day, from his own address; Pulse drafts the reply. |
| F4 | Founder bios for the plan and the launch pages? (07; plan §10) | Each founder writes 60 words this week. |

## Superseded or settled

- Items 8 and 26 in the running list are both A1.
- Workstream 07's build task "a join link on `/s`" is replaced by workstream 06's "Ask to join" (D1).
- "AI employees" on calls and in copy: settled by the founder on 13 Oct, yes.

## Answers (founder, 13 Oct 2026, question by question)

**Standing rule (from A8):** fix forward. Build the real thing (seat billing, the trial checkout, the email gate) before the site or copy depends on it. No "by hand until it ships", no "honest line until it ships". Applies to A4, A7, A8, B6, E6, E9 and any later item of that shape.

| # | Answer |
|---|---|
| B1 | Name Second Catch now. |
| A8 | Seat billing first, then `/pricing` with the plan's price. No interim line. |
| A1 | No guarantee. The free month is the risk reversal; the "week-4 refund" leaves §7, §8, the changelog, item 5 and workstreams 01/10/12/15. "Week-4 active" stays as a metric. |
| A2 | Moot (no guarantee). |
| A3 | Ten teams set up with a card on file by 4 Jan; about three paying by 12 Jan; six paying by May stands. |
| A4 | Card at setup; Stripe checkout with a 30-day trial, first charge on day 31. Built before the first setup (fix-forward rule). |
| A7 | Moot: seat billing is built before the first 6th member (fix-forward rule). |
| D1 | On a public session page an outsider gets the same "Ask to join" teammates have: sign up, the session owner taps Allow, and that yes also makes them a workspace member with the session controls. One door. |
| A5 | Yes, 20 a week each from week 3, reviewed at the week-2 check. |
| A6 | No holiday pause. |
| A9 | Retire Base and Ultra fully. One plan: $50 for up to 5, then $20 a member. Verified in Stripe 13 Oct (evening): six active subscriptions, all on the $50 price (none on Base or Ultra), all billing $0; four of the six are outside addresses on a free coupon, two are ours. Nothing to migrate. |
| A10 | Nothing to honour: the launch competition is not set up. Setting it up is a task, after the product work (items 1–9). Check the live `/launch` page for promises it cannot keep yet. |
| A11 | Referrals are a stage-2 (late) network-effect item; they are in the plan (Go To Market stage 2, workstream 06). The competition is post-product. |
| A12 | Offer one 30-day pause per company. |
| A14 | Deferred to stage 2 with A11. |
| A13 | 90 days reading-only, then the standard deletion notice. |
| B2 | No further named teams until three teams are paying. Anna and Second Catch only. |
| B3 | Yes, the real numbers in the Show HN first comment. |
| B4 | Yes to our own numbers and founder-workspace screenshots, but every number and screenshot is vetted by a founder before it is posted (what it shows and what it says). |
| B5 | Fine; the founders handle any wording check with Anna and Second Catch themselves. Not a blocker. |
| B6 | Email is already rule-based server-side (`agent_mail_send.py`: recipient policy, `send_to_anyone` on by default, owner can close it to an allowlist, daily cap). No per-message approval exists. Copy states the rule as it is ("you decide who your agents can email"), never "waits for a person". No new gate. |
| B7 | Subhead becomes "in your team's channels". |
| B9 | Founder unsure. Provisional: say what the Privacy Policy says (isolated AWS environment; content not sold or used to train) plus the team's provider path; Prasiddha answers in writing within a day; a data page is a build task. Revisit when the first call asks it. |
| B8 | No Pro price. Slack appears in the battle card only to answer "we already have Slack" (the price-for-8 line); nowhere else on calls. |
| B10 | Yes: Hermes and Paperclip as "built on", never as the product's name. |
| B11 | Yes: Prasiddha's live terminal on a public demo repo, viewer cap raised after a load test, fallback page ready. |
| C1 | The founder who sourced the team runs the setup; Aden by default; the customer answers the first approval card. |
| C2 | Public replies in each founder's own name, no pitch. A DM only after a person asks for help in the thread. |
| C3 | Yes: Leo signs X and Reddit, Aden LinkedIn, Prasiddha HN and Product Hunt. |
| C4 | Ask for the second person at booking; book anyway if they insist; the first approval card is the invite. |
| C5 | Cross-role teams first. |
| C6 | Neither; revisit at stage 2. |
| C7 | $50 gift card for churn interviews allowed (research, not acquisition); company card. |
| C8 | Aden emails the 24 day-one churners once, own address, consent line. |
| C9 | "Very disappointed" share reported separately for wedge teams and small businesses, with n. |
| C10 | Churn calls 20 minutes. |
| C11 | Aden shares three past LinkedIn posts. |
| C12 | Leo checks his Reddit karma; replies only until it is over 100. |
| C13 | Each founder lists real accelerator contacts in the outreach log this week. |
| C14 | Yes: a light bulb in the post after the first handoff, from the founder who set them up. |
| C15 | Not yet; the shared channel per team is the line. |
| D2 | "Viewers can ask to join" on by default for Anyone-with-the-link sessions; the owner can turn it off per session. |
| D3 | Raise the viewer cap after a load test; a "full / N watching" state regardless. |
| D4 | The public session page is the no-account look; nothing more for launch. |
| D5 | Members may send invites. |
| D6 | Keep the emailed sign-up code. |
| D7 | Add GitHub sign-in. |
| D8 | Away emails after five minutes; quiet hours 22:00–08:00 local; at most one per room per 30 minutes. |
| D9 | Invited members get the teammate welcome and away emails, not the owner nudges. |
| D10 | Welcome email from the CEO agent's address, in the agent's voice, replacing today's welcome. |
| D11 | Thursday check-in drafted by the Growth agent, sent by the founder who runs that team. |
| D12 | A rejected approval counts as a handoff. |
| D13 | Source question optional at sign-up, required on the setup form. |
| D14 | Opt-out footer on customers' agent mail, on by default; check whether it already exists first. |
| E1 | Delete the ten thin `[seoSlug]` pages with redirects to the nearest real page. |
| E2 | Redirect `/ai-agents-for-slack` to the Slack comparison page. |
| E3 | Authors: Prasiddha product and engineering, Leo comparisons, Aden customer stories. |
| E6 | Settled by the fix-forward rule: comparison pages publish after seat billing ships, with the plan's price. |
| E9 | Settled by the fix-forward rule: the "people are free up to five" post goes out when `/pricing` goes live, after billing ships. |
| E4 | Telegram, Discord and WhatsApp come off the homepage now. Homepage lists the workspace (web, desktop, phone), Claude Code and Codex sessions, email, Meet. The channels stay in product and docs if Forge verifies them. |
| E5 | Aden confirms the LinkedIn page; Prasiddha grants Search Console and Bing access. |
| E7 | Bloome gets its own comparison page (six pages). |
| E8 | Leo signs off comparison-page edits. |
| E10 | Old flickmeon.ai posts untouched; every link from now carries a UTM. |
| E11 | MIT licence on the plugin repo. |
| E12 | Prasiddha owns the Product Hunt maker account. |
| E13 | Launch without the Codex directory listing if it is closed or its hooks rule disqualifies the plugin. |
| E14 | G2 waits until the site gate passes. |
| F1 | Prasiddha owns the internal-workspace exclusion list. |
| F2 | No paid PostHog group analytics; a `workspace_id` property. |
| F3 | Aden answers setup requests within one business day from his own address; the Growth agent drafts. |
| F4 | Each founder writes a 60-word bio this week. |
| /launch | flickmeon.ai redirects to the product site until the competition is set up; the competition is post-product work. |
| Board | Founder signs in to the hosted connection; the current board is cleared (existing tasks closed) before the new tasks go on. |

All 72 numbered items answered 13 Oct 2026. Items settled by the fix-forward rule: A4 (build the trial checkout), A7 (build seat billing), A8, B6 (state the rule, no gate), E6, E9.
