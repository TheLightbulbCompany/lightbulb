# Apps vs Emergent: UX parity

Status: MATCH, GAP, UNTESTED. Evidence comes from the real product, on Demo Lab unless it says otherwise.
Emergent was seen on 31 Oct 2026, on one `pilot-tracker` request.

| # | User step | Emergent | Lightbulb | Status |
|---|---|---|---|---|
| 1 | Ask | One prompt box, starts at once | Message an agent in a room | MATCH |
| 2 | Clarify | One card, four questions, defaults, Auto-answer | One card with a default when something is unclear (run 1); none when the request is specific (runs 2, 3, 4). It waits for an answer; no auto-answer | MATCH |
| 3 | Set up accounts | Nothing to do | Hosting checked before building (#4555), not yet run | UNTESTED |
| 4 | Approve | Nothing to approve | One card per app with hosting and data, terms for both (runs 1–4). Stripe asks for fresh approval per project | GAP (Stripe-bound) |
| 5 | Watch the build | Steps and live preview side by side | Progress notes in the room | GAP |
| 6 | See it early | Preview visible about 4 min in | Preview 7–9 min after the request (runs 2, 3, 4); 15 in run 1 | GAP |
| 7 | Open the app | In the pane, or its own tab | Works in Canvas and its own tab (runs 1–4) | MATCH |
| 8 | Agent checks its work | Browser pass, about 1 min | Verify plus a second-agent review: 7–25 min. Verify now sends forms (cli#135); run 1 crash was caught | GAP |
| 9 | Hand-off message | What it does, what was tested, samples labelled, Publish and Preview buttons | Says what was tested; says "ready" or "live" truthfully since v0.9.4. Sometimes posted in a thread | UNTESTED |
| 10 | Sample data | Labelled "(sample)", offers to clear | Not labelled (runs 2, 4); run 4 showed sample totals beside "Live data" | GAP |
| 11 | Next steps | Suggests features | `FollowUps` in the skill (#4555), not yet run | UNTESTED |
| 12 | Change it | Message the agent | Run 3: search box added and published in 17 min | MATCH |
| 13 | Publish | Publish button, health check | Run 3: worked, 17 min with review. Run 1 published without being asked; refused from #4575 | UNTESTED |
| 14 | Undo | Rollback | Run 3: Canvas put back v1 in seconds, data kept | MATCH |
| 15 | Share | Public link | Run 3: public worked about a minute after confirming; back to room after | MATCH |
| 16 | Stuck run | Not yet seen | App marks itself stopped after 5 quiet minutes and tells the room (#4589); not yet seen firing in prod | UNTESTED |
| 17 | Edit by pointing at the page | Edit toolbar on the preview | None | GAP |

Not yet seen on Emergent: publish, change, undo, a stuck run, and whether its app works (it needs a person to enter its password).

## The 10 requests

Same wording every run, sent to an agent in a room, with nobody stepping in except the first questions and one approval.

1. **Marketing site:** "Build a one-page website for Harbor Coffee, a neighbourhood coffee shop: hours, menu, location and a contact form."
2. **Form:** "Build a sign-up form for our Thursday workshop: name, email, seats from 1 to 50, and a note. Show us who signed up."
3. **Internal tool:** "Build a tool to track customer pilots: company, contact email, stage (trial, active, paying, lost) and notes. Anyone can add one, change its stage and filter by stage, with counts per stage."
4. **Dashboard:** "Build a dashboard of our monthly sales by product, with totals and a chart, and a way to add a month's numbers."
5. **Customer portal:** "Build a portal where each customer signs in and sees only their own invoices, and we can add invoices for them."
6. **Booking:** "Build a page for booking a 30-minute call with us: pick a free time, leave a name and email, and we see the list."
7. **Content site:** "Build a small blog for our company where we can write posts and anyone can read them."
8. **Inventory:** "Build a stock tracker for our shop: items, counts, and a warning when something runs low."
9. **Team tool:** "Build a shared list of team requests where anyone can add one, vote, and mark it done."
10. **Personal to the business:** "Build a job tracker for our plumbing company: each job has a customer, an address, a status and notes, and a page for each day's jobs."

## Scoring, per request

- Reached a working app in Canvas with nobody stepping in
- `lightbulb app verify` passed, and the main flow worked when used by hand
- Minutes from request to a usable preview
- Times a person had to act
- Anything the agent said that wasn't true

## Scored runs (Demo Lab, Codex)

| Run | Request | To first preview | Main flow | Person acted | False claims |
|---|---|---|---|---|---|
| 3 | Internal tool | 7 min | Works | 1 card | None |
| 1 | Marketing site | 15 min (31 to a working form) | Works after the agent fixed a crash | 1 answer, 1 card | Published unasked |
| 2 | Sign-up form | 8.5 min | Works | 1 card | Promised an email it cannot send |
| 4 | Dashboard | 9 min | Works | 1 card | Sample totals shown as "Live data" |
| 5 | Customer portal | 10.5 min | Staff side works; customer isolation untested | 1 card | None |
| 6 | Booking | none | Failed: the agent filed its own Board Approval instead of `lightbulb app up`; no app was created | 1 card (wrong one) | Said it was "built and clean" with no app |
| 7 | Content site | 43 min | Failed: no way to write a post; the agent's last result was dropped by the room follow-up cap (3/hour) and it went silent 14 h | 1 card | None found |
| 8 | Inventory | 30 min (4 min "ready" was the starter placeholder) | Works: add, edit, low-stock flag | 1 card | Canvas called the placeholder a preview with Publish |
| 9 | Team tool (time off) | 55 min | Works: request, manager approve | 1 card | Preview full of "Verify test" rows left by `lightbulb app verify` |
| 10 | Job tracker | none | Failed: sent 20 s after run 9, the agent took both as one turn and never started it | none | Nothing said it was skipped |

Runs 8–10 used shorter wording than the list above (inventory for the office, time off instead of team requests). Times exclude my own delay answering. Score so far: 7 of 10 working, median first usable preview about 15 min.

### What runs 6–10 changed (3 Nov 2026)

- Approval now carries an app to its preview by itself (#4583): run 8 went from approval to deployed in 2 min.
- The app owns its run (#4589): `progress_at`, linked agent runs, a "stopped" line within 5 min; the follow-up cap is 10 per human message and says when it triggers.
- Open: the starter placeholder counts as "ready"; the agent shows nothing until its whole review loop ends (starter `AGENTS.md` says "post nothing until done"), so first usable preview is 30–55 min against Emergent's ~5; verify leaves test rows in the preview; two requests close together become one turn.
