# 01 · The 90-day marketing plan (14 Oct 2026 → 12 Jan 2027)

## Purpose

Moves the stage-1 goal: ten founding teams set up on the founding offer, the first of them paying at list, inbound launched, and the five Monday numbers read from real events. Serves business plan §8 and the Go To Market section (stage 1, weekly rhythm, launch for inbound).

Method: the `marketing-plan` skill (AARRR tags on every line; four phases: Unblock, Foundation, Velocity, Compound; named owners; open decisions stated). The skill's 13-section, 12-month format is cut to this brief's structure. The 12-month view stays in the plan's three stages. The other workstreams hold the channel detail, and this file refers to them by number.

## What we know

| Fact | Source | Confidence |
|---|---|---|
| Stage 1 = ten founder-led teams of 3–15 by 31 Jan 2027, six paying by May. Each team is set up in person and must pass the §5 qualification list | Business plan §8, §12, GTM | High (first-party) |
| Offer (founders, 13 Oct): 30 days free with a card on file from the setup session, first charge on day 31, then $50 a month for up to 5 people and $20 a member from the 6th, price locked for two years. No discounts, no coupons, no refund promise | Business plan §7 | High |
| Floor: ten first touches a week per founder (30 in total). Aden and Leo go to twenty each from week 3. Agents research and draft, a founder sends. Two asks are split-tested. After two weeks, check the quota against the real close rate | Business plan GTM | High |
| Channels: Leo on X + Reddit, Aden on LinkedIn + setup sessions + interviews, Prasiddha on HN + Product Hunt + plugin listings. One founder-signed post a week each. Not in stage 1: Instagram, local SMB lists, paid | Business plan §8, GTM | High |
| Today: 0 paying at list, about 5 active outside workspaces a week, 33 of 38 outside workspaces with one person, about 10 visitors a day, ¾ of traffic unattributed | Business plan §9 (12 Oct prod reads) | High |
| Live site: `/pricing` and `/request-setup` return 404. Homepage H1 is "Your team and your agents, in one place." and its only button is "Get started free" | `curl` of thelightbulb.company, 13 Oct | High |
| `origin/main` (6249f614d): no `setup_requested` / `team_formed` / `first_handoff` events. No `@vercel/analytics`. A setup band with an "Invite your teammates" card exists (`OnboardingBand.tsx`), but the CEO's "Who should see this with you?" line is not in the code | `git grep origin/main` | High |
| Workspace plan already bills $50 with unlimited members, so a founding team of five or fewer can pay list on the current price. Seat counting from the 6th member and the trial checkout are not built; both are built before the first setup | Business plan §7, item 5 | High |
| Median B2B cold-email reply rate is 2.1%. Research-based first lines lift replies 184% | [Visionary Marketing, 28,400 leads](https://visionary-marketing.co.uk/blog/b2b-cold-email-statistics-2026) | Medium (vendor study, 2 sources agree on the range) |
| Lists under 50 recipients: "5.8% reply rates versus 2.1% for lists over 1,000". Context-specific copy "17–18%" | [lemlist, citing Woodpecker](https://lemlist.com/blog/cold-email-response-rate) | Medium |
| Median demo-to-close is 18.7% (18,400 demos). Horizontal SMB SaaS "trails at 9.2%" | [Visionary Marketing demo benchmark](https://visionary-marketing.co.uk/blog/b2b-demo-conversion-statistics-2026) | Low (one study; definitions vary by vendor) |

Current state, scored from these materials with the skill's lens. Phase: pre-revenue ($0–10K ARR). Paid budget: $0 by design. Marketing labour: three founders, Pulse (marketing agent), Forge (engineering agent) and the MKT385 interns. Biggest gap: measurement. No funnel event exists past sign-up, so the gate cannot be read yet.

## Findings (ranked by impact)

1. **Ten teams *paying at list* by 12 Jan cannot be reached at 30 touches a week. Ten teams *set up on the offer* can.** The first invoice comes 30 days after setup. A team only pays inside the window if it is set up by about 8 Dec. At the rates below, 30 touches a week gives about 6 setups by then. The plan's own date (31 Jan, six paying by May) agrees with this. The 90 days should promise ten setups with a card on file, about 3 paying at list by 12 Jan, and the rest paying by mid-February.
2. **Measurement is the binding constraint, not reach.** Without item 7, the Monday numbers are hand-counted. Without "How did you first hear about us?", ¾ of sources stay unknown. The Monday review and the week-2 quota check need real events by week 3.
3. **The setup session carries activation until items 1–3 ship.** Self-serve forms a team in 5 of 38 workspaces. The session invites the second person by hand. Item 3 (email when away) decides whether that second person comes back, so it must ship before the first week-4 check.
4. **The launch is gated by product, not by the calendar.** The plan holds Show HN / Product Hunt until the website brief and item 4 (session link as a door) ship, and uses the first teams' handoffs as the story. This places Show HN in early December and Product Hunt in early January.
5. **The plan has one close-rate fact and no history.** Every rate below is an assumption with a source anchor. The week-2 and week-6 checks replace them with our own numbers.

## Recommendation

Run stage 1 as written, with one change in wording: the 90-day commitment is **ten teams set up on the founding offer (card on file) by 4 Jan, three paying at list by 12 Jan, and Show HN shipped**. Paying at list follows 30 days behind each setup. Keep the 30-touch floor. Aden and Leo go to twenty each from week 3 (50 a week), reviewed against the close rate at the week-2 check. That gives ten setups by about 7 Dec.

Rejected: (a) a two-week free extension or coupon to pull invoices inside the window. The plan rules out discounts, and paying-at-list is the number the round is judged on. (b) Launching in October for traffic. Today the site has no setup door or pricing page, and it gives a solo first session. Launch traffic would land on the 13% team-formation path. (c) Paid tests. They are gated to stage 2.

## Deliverable

### A. The pipeline targets (base case, 30 first touches a week)

| Step | Rate used | Anchor | Label |
|---|---|---|---|
| Touch → conversation (a reply that leads to a call or a written exchange about their team) | 10%, one week later | 2.1% median × 2.84 research-based lift ≈ 6%; small lists 5.8%; context-specific 17–18%. Our touches go to named people who stated the pain in public, so we take the midpoint | Assumption, low |
| Conversation → setup booked and held (passes §5) | 40%, one week later | Demo-to-opportunity median 31.4% (Visionary). Higher because the outreach is pre-qualified | Assumption, low |
| Setup → team formed (2+ people within 48 h) | 85% | The session invites the second person by hand. Self-serve is 13% today | Assumption, medium |
| Team formed → first handoff in week one | 75%, the following week | The session ends with an approval card answered | Assumption, low |
| Setup → paying at list (first charge on day 31 paid) | 50%, five weeks later | Conversation → paying = 40% × 50% = 20%, close to the 18.7% median demo-to-close. The plan's own 6-of-10 = 60% | Assumption, low |

Touch volume in the table is the 30-a-week base case, lower in Thanksgiving week (20) and the two holiday weeks (10 each); there is no holiday pause, so those weeks run at full pace. Inbound from posts and the launch is **not** counted. It is upside, reported in its own row.

| Wk | Mon | Touches | Conversations | Setups | Teams formed | First handoff | Paying at list | Cum. setups | Cum. paying |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 14 Oct (Wed) | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2 | 19 Oct | 30 | 1.5 | 0 | 0 | 0 | 0 | 0 | 0 |
| 3 | 26 Oct | 30 | 3 | 0.6 | 0.5 | 0 | 0 | 0.6 | 0 |
| 4 | 2 Nov | 30 | 3 | 1.2 | 1.0 | 0.4 | 0 | 1.8 | 0 |
| 5 | 9 Nov | 30 | 3 | 1.2 | 1.0 | 0.8 | 0 | 3.0 | 0 |
| 6 | 16 Nov | 30 | 3 | 1.2 | 1.0 | 0.8 | 0 | 4.2 | 0 |
| 7 | 23 Nov | 20 | 3 | 1.2 | 1.0 | 0.8 | 0 | 5.4 | 0 |
| 8 | 30 Nov | 30 | 2 | 1.2 | 1.0 | 0.8 | 0.3 | 6.6 | 0.3 |
| 9 | 7 Dec | 30 | 3 | 0.8 | 0.7 | 0.8 | 0.6 | 7.4 | 0.9 |
| 10 | 14 Dec | 10 | 3 | 1.2 | 1.0 | 0.5 | 0.6 | 8.6 | 1.5 |
| 11 | 21 Dec | 10 | 1 | 1.2 | 1.0 | 0.8 | 0.6 | 9.8 | 2.1 |
| 12 | 28 Dec | 30 | 1 | 0.4 | 0.3 | 0.8 | 0.6 | 10.2 | 2.7 |
| 13 | 4 Jan | 30 (lands after the window) | 3 | 0.4 | 0.3 | 0.3 | 0.6 | 10.6 | 3.3 |

Totals: 325 touches (295 that can convert inside the window) → about 30 conversations → 10–11 setups → 9 teams formed → 6–7 first handoffs → 3 paying at list by 12 Jan, plus about 2 more by mid-February from the December setups. Stretch (50 a week from week 3) gives about 10 setups by 7 Dec and 5 paying by 12 Jan.

### B. Week-by-week calendar

Owners: **P** = Prasiddha, **A** = Aden, **L** = Leo, **F** = Forge (engineering agent), **Pu** = Pulse (marketing agent). Tags: Acq / Act / Ret / Ref / Rev / Meas.

| Wk | Phase | Line | Owner | Tag |
|---|---|---|---|---|
| 1 (14 Oct) | Unblock | Build the first 60 qualified names from the HN and r/ClaudeAI threads, plugin users and founder networks (→ 02) | Pu draft, L review | Acq |
| 1 | | Write the two asks and the three-touch sequence (→ 03). The founders pick ask A and ask B | Pu draft, P/A/L sign | Acq |
| 1 | | Send the first 15 touches (5 each) | P, A, L | Acq |
| 1 | | Set up the manual tracking sheet (touch, source, ask variant, stage) until item 7 ships. Pulse fills it from the founders' sent folders and the calendar | Pu | Meas |
| 1 | | Start item 7 (events, Vercel Analytics, the "how did you hear" field) (→ 09) | F, P | Meas |
| 1 | | Build seat billing (item 5) and the trial checkout (card at setup, first charge day 31). Both before the first setup and before `/pricing` | F, P | Rev |
| 1 | | Start `/request-setup` (→ 10) | F, P | Act |
| 1 | | Setup-session battle card and guide (→ 04, 16) | Pu draft, A owns | Act |
| 1 | | Contact Habits365 (inbound demo request): first setup candidate | A | Acq |
| 2 (19 Oct) | Unblock | 30 touches (10 each); a first post each (→ 13) | P, A, L | Acq |
| 2 | | Ship `/request-setup` (five fields; Aden answers within one business day from his own address, Pulse drafts) | F, P | Act |
| 2 | | Start item 3 (email for a DM, @mention or approval, only when the person is away) | F, P | Ret |
| 2 | | Retire Base and Ultra: one plan for everyone; internal rows move to it on the internal coupon | F, A | Rev |
| 2 | | Ask Anna for consent to the case study; ask Second Catch for its story | A | Acq |
| 3 (26 Oct) | Foundation | 30 touches; posts | P, A, L | Acq |
| 3 | | **Week-2 quota check**: real reply and conversation rates against §A. Decide 30 or 50 a week | P, A, L | Meas |
| 3 | | Seat billing and trial checkout live (gate for the first setup). First setup sessions (target 0–1), run by the founder who sourced the team, Aden by default | A (P covers) | Act/Rev |
| 3 | | Item 7 events live in prod; first automated Monday readout | F, Pu | Meas |
| 3 | | Start the homepage rewrite to the website brief (→ 10). Start the A/B headline test once events are live. Ship `/pricing` with the plan's price once billing is live | F, P | Acq/Act |
| 4 (2 Nov) | Foundation | 30 touches; posts; setups (target 1) | P, A, L | Acq/Act |
| 4 | | Item 3 ships | F | Ret |
| 4 | | Start item 1 (invite at the end of the first reply) and item 2 (one finished thing) (→ 08) | F, P | Act |
| 4 | | Lifecycle emails drafted: first week, week four, needs-you (→ 12) | Pu draft, A sign | Ret |
| 5 (9 Nov) | Velocity | 30 touches; posts; setups (target 1) | P, A, L | Acq/Act |
| 5 | | List the `lightbulb` plugin in the Claude Code and Codex marketplaces (→ 14) | P, F | Acq |
| 5 | | Item 4 (the session page carries the join link) (→ 06) | F | Ref |
| 5 | | Anna case study: one page and a 60-second recording | Pu draft, A | Acq |
| 6 (16 Nov) | Velocity | 30 touches; setups (target 1) | P, A, L | Acq/Act |
| 6 | | **First week-4 question** to the earliest team ("How would you feel if…") | A | Ret |
| 6 | | Item 6: the App Store build without IAP submitted | F, P | Act |
| 6 | | Comparison pages drafted (→ 11) | Pu draft, P sign | Acq |
| 7 (23 Nov) | Velocity | 20 touches (Thanksgiving); setups (target 1); week-4 check on the second team | P, A, L | Acq/Ret |
| 7 | | **Week-6 rate check**: re-fit §A on six weeks of data | Pu, P | Meas |
| 7 | | Items 1 + 2 ship; homepage, `/sign-up` and `/docs/get-started` live | F, P | Act |
| 8 (30 Nov) | Velocity | 30 touches; setups; **first charges (day 31)** (target 0–1 paying) | P, A, L | Rev |
| 8 | | Launch go/no-go: website brief shipped? item 4 live? 3+ first handoffs to cite? (→ 07) | P | Acq |
| 8 | | Week-4 save play for any team that is not using the product (→ 15) | A | Ret |
| 9 (7 Dec) | Compound | **Show HN, Tue 8 Dec** (if go). Founders answer every comment that day | P (A, L on comments) | Acq |
| 9 | | 30 touches; setups; launch inbound triaged to `/request-setup` | P, A, L | Acq |
| 10 (14 Dec) | Compound | 10 touches; setups; directories batch (→ 14) | P, A, L, Pu | Acq |
| 11 (21 Dec) | Compound | Touches and setups continue (no holiday pause) | P, A, L | Acq |
| 12 (28 Dec) | Compound | 30 touches (catch-up); week-4 checks | P, A, L | Acq/Ret |
| 13 (4 Jan) | Compound | **Product Hunt, Tue 5 Jan** (→ 07); quarter readout on Mon 11 Jan; draft the stage-2 gate read for 31 Jan | P, Pu | Acq/Meas |

Item 8 (agent voice) runs all quarter as a research track under P and F. A first round of reply rules before Show HN is a soft dependency: launch visitors will judge the replies. Item 9 is not on this calendar (see §F).

### C. Dependencies: what must ship before which GTM step

| GTM step | Planned week | Must ship first | If it slips |
|---|---|---|---|
| First touches | 1 | Nothing. The ask books a call by reply | — |
| Monday readout from events | 3 | Item 7 | Pulse hand-counts from Clerk and Stripe. The week-2 quota check uses the sheet |
| First setup session | 3 | `/request-setup` (or a reply-to-book link) | The founder books by email |
| Second person comes back after setup | 4 | Item 3 | A founder pings the second person by hand on Thursday |
| Founder posts link to the site | 3 | Homepage setup button, seat billing, then `/pricing` | Posts wait; nothing links to a page that depends on unbuilt billing |
| First setup (card on file) | 3 | Trial checkout (first charge day 31) and item 5 seat billing, built first | No setup until both ship |
| Text invites install from the store | 6 | Item 6 | Second person uses web |
| Show HN | 9 | Website brief, item 4, items 1 + 2, 3+ handoffs | Move to week 12 or 13. Product Hunt moves behind it |
| Product Hunt | 13 | Show HN done, item 6 listed | Move into stage 2 |
| Stage-2 gate read | 31 Jan | Item 7 week-4 events | Cannot read the gate |

### D. The Monday readout (one page, written by Pulse by 9:00 Monday)

```
Lightbulb · Monday readout · week of <date> · week <n> of 13
Source: PostHog + Clerk + Stripe (external workspaces only, per Appendix C)

THE FIVE NUMBERS          this wk   last wk   cum   target cum (§A)
1 Setup requests            _         _        _        _
2 Teams formed (2+ in 48h)  _         _        _        _
3 First handoff in wk 1     _         _        _        _
4 Week-4 active (share)     _/_       _/_      —        25% gate
5 Paying at list            _         _        _        _
On the wall: teams with 2+ people AND a completed handoff = _

TRAFFIC FUNNEL BY SOURCE  visitors → setup request → start free
  HN | Reddit | X | LinkedIn | plugin | outbound email | direct/unknown | other

OUTBOUND PIPELINE BY FOUNDER (and by ask A/B)
            touches  conversations  setups booked  set up  2+ people  handoff  paying
  Prasiddha
  Aden
  Leo
  Inbound (posts/launch)

ACTIVE SETUPS: team · day of trial · 2+? · handoff? · week-4 answer · card on file?
VARIANCE: one line per number off target by >30%, with the cause
DECISIONS NEEDED: (max 3)
```

### E. Thursday check-in agenda (30 min, founders; then each team's shared `lightbulb-<slug>` channel)

1. Each active team, 2 minutes: what their agents did this week, what broke, what we promised and fixed (A reads from the channels).
2. Teams at day 21–28: ask the week-4 question; record the "very disappointed" share.
3. Teams with one person or no handoff: who reaches out, by Friday. Teams with a first handoff: the founder who set them up mails a light bulb.
4. Fix list: bugs the teams hit, ranked by "blocks a handoff". These go to Forge with an owner.
5. Setups booked for next week and who runs each.
6. Public posts for next week: who signs which (→ 13).

### F. What we cut if the founders fall behind (cut in this order)

1. Item 9 (a teammate you text).
2. Product Hunt (keep Show HN).
3. The homepage headline A/B test (ship the chosen headline).
4. Directories and comparison pages (→ 14, 11).
5. Founder posts go from one each a week to one company post a week, rotated.
6. Item 6 App Store (second people use web).
7. Lifecycle emails beyond needs-you (→ 12).
8. Show HN moves to January.

Never cut: the 30-touch floor, setup sessions, Thursday check-ins, the Monday readout, items 3, 5 (seat billing and trial checkout) and 7. They are the stage.

## Build tasks

| Title | Today | Change | Why | Measure |
|---|---|---|---|---|
| Founding-team pipeline view in the Monday readout | No events past sign-up; ¾ of traffic unattributed | Pulse's readout joins item 7 events with the founders' outbound sheet (source, founder, ask variant) and prints §D | The Monday review and the quota checks need it | Readout delivered every Monday from week 3 with no hand edits |
| `/request-setup` | 404 | Five fields from the website brief, plus "How did you first hear about us?" and UTM capture. Notifies the founders' channel. Emits `setup_requested` with the source | The stage-1 door | Setup requests by source |
| Card-on-file at setup | Checkout bills at once (Workspace $50) | Build the Stripe checkout with a 30-day trial, card at setup, first charge day 31, plus seat billing ($20 a member from the 6th). Before the first setup | The offer must be billable on day 31 | First charges paid |
| Outbound UTM convention | None | `?utm_source=<channel>&utm_medium=<founder>&utm_campaign=ft-<ask>` on every link we post | Funnel by source | Share of traffic that is unattributed |

Items 1–9 and the website pages are specified in workstreams 08, 09, 10 and 15 and not repeated here.

## Measures

Measured in this file's terms against the five Monday numbers: cumulative setups vs §A (10 by 4 Jan), teams formed ≥ 85% of setups, first handoff ≥ 75% of teams formed, week-4 active ≥ 25% (the stage-2 gate), paying at list 3 by 12 Jan. Leading indicator: conversation rate per 100 touches at week 3 and week 7. If it is under 7, raise the volume to 50. If it is under 4, rewrite the ask before adding volume. Kill criterion for any channel: no conversation from 40 touches.

## Founder decisions (13 Oct 2026)

- A1: No guarantee and no refund promise. The free month is the risk reversal. "Week-4 active" stays as a metric only.
- A3: Ten teams set up with a card on file by 4 Jan; about three paying by 12 Jan; six paying by May stands.
- A4: Card at setup; Stripe checkout with a 30-day trial, first charge on day 31. Built before the first setup.
- A5: Aden and Leo go to 20 first touches a week each from week 3, reviewed at the week-2 check.
- A6: No holiday pause.
- A7: Moot. Seat billing is built before the first 6th member.
- A8: Seat billing first, then `/pricing` with the plan's price. No interim line.
- A10: The launch competition is not set up; it is post-product work. Nothing to honour.
- A11: Referrals are stage 2 (late).
- C1: The founder who sourced a team runs its setup, Aden by default; the customer answers the first approval card.
- C14: A light bulb in the post after a team's first handoff, from the founder who set them up.

## Sources

- `docs/business-plan.md` §5, §7, §8, §9, §12, Appendix C, Engineering/Product, Go To Market (13 Oct working copy)
- `.agents/product-marketing.md` v7 (13 Oct)
- `origin/main` at 6249f614d: `git grep` for `setup_requested`, `team_formed`, `@vercel/analytics`, "Who should see this"; `apps/frontend/src/components/messaging/OnboardingBand.tsx`
- Live site, 13 Oct: thelightbulb.company (`/` 200, `/pricing` 404, `/request-setup` 404, `/sign-up` 200)
- Visionary Marketing, B2B cold email benchmarks 2026: https://visionary-marketing.co.uk/blog/b2b-cold-email-statistics-2026
- Visionary Marketing, B2B demo conversion benchmark 2026: https://visionary-marketing.co.uk/blog/b2b-demo-conversion-statistics-2026
- lemlist, cold email response rates (citing Woodpecker): https://lemlist.com/blog/cold-email-response-rate
