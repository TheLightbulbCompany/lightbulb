# Apps vs Emergent: UX parity

Status: MATCH, GAP, UNTESTED. Evidence comes from the real product, on Demo Lab unless it says otherwise.
Emergent was seen on 31 Oct 2026, on one `pilot-tracker` request.

| # | User step | Emergent | Lightbulb | Status |
|---|---|---|---|---|
| 1 | Ask | One prompt box, starts at once | Message an agent in a room | MATCH |
| 2 | Clarify | One card, four questions, defaults, Auto-answer | One card in the skill (#4555), not yet run | UNTESTED |
| 3 | Set up accounts | Nothing to do | Hosting checked before building (#4555), not yet run | UNTESTED |
| 4 | Approve | Nothing to approve | One card per app (#4556), not yet seen live. Stripe's guide asks for fresh approval per project | UNTESTED |
| 5 | Watch the build | Steps and live preview side by side | Progress notes in the room | GAP |
| 6 | See it early | Preview visible about 4 min in | Preview only after approval and deploy | GAP |
| 7 | Open the app | In the pane, or its own tab | Works in Canvas (`account-tracker`, 2 Nov) | MATCH |
| 8 | Agent checks its work | Browser pass, about 1 min | 16 to 22 min; `lightbulb app verify` released, not yet run | UNTESTED |
| 9 | Hand-off message | What it does, what was tested, samples labelled, Publish and Preview buttons | Skill and CLI wording changed; said "live" falsely twice before that | UNTESTED |
| 10 | Sample data | Labelled "(sample)", offers to clear | In the skill (#4555), not yet run | UNTESTED |
| 11 | Next steps | Suggests features | `FollowUps` in the skill (#4555), not yet run | UNTESTED |
| 12 | Change it | Message the agent | Not retested | UNTESTED |
| 13 | Publish | Publish button, health check | Button readable (#4554); agent publish fixed (#4546), not yet run | UNTESTED |
| 14 | Undo | Rollback | Not retested | UNTESTED |
| 15 | Share | Public link | Room, invite or public; not retested | UNTESTED |
| 16 | Stuck run | Pauses, refunds, says so | Sits at building or checking | GAP |
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
