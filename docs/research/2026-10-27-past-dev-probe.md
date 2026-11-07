# past.dev under test: how its engine decides what is true (27 October 2026)

Method: the founder's free past.dev account, one project, the public API only. Sixteen crafted data points covering eight situations were sent in one batch (`docs/research/2026-10-27-past-dev-probe.py`, the script that ran), then every source's lineage and every memory node was read, then fifteen recalls were made as three identities and at three points in time. The batch settled in 51 seconds. Cost: 17.5 credits of 150,000 (16 for ingest, the rest for recall), about half a US cent.

## 1. What the extraction produced

45 memories from 16 data points (about three per source), in five kinds: `claim`, `mention`, `temporal-anchor`, `rule`, and (absent here) `observation`, `membership`, `identity`.

- **Claims are slot-shaped**: `<subject> <attribute>: <value>`, prefixed with the speaker when one is named ("Nadia: Acme pilot budget: 40k."; "billing migration owner: Tomás."; "Globex contract seats: 12 seats (exactly)."). Precision qualifiers are kept ("exactly", "always").
- **Temporal anchors** resolve relative time against the source date ("Q1, resolved to 2026-01-01 to 2026-03-31"; "today, resolved to 2026-05-20"; "March 3, resolved to 2026-03-03"; vague ones are kept as written: "mid March", "next March", "this week").
- **Mentions** record that a topic came up ("Mentioned billing migration"; "Asked about…"; "Raised an issue: …"; "Decided: …"), so recall can find the source by subject.
- **Rules** are extracted from explicit norms ("The team deploys on Tuesdays and Thursdays only, never on Fridays…"; the headline carries a garbled "rather than" clause).
- Every memory has exactly one input (its source). No memory was built from other memories in this run.

## 2. How it decides what is current

| Situation | What happened | Verdict |
|---|---|---|
| Budget 30k (10 Jan) then "moved to 40k" (3 Feb) | The 30k claim's `validTo` closed at 3 Feb 09:00, the 40k claim opened there; recall now ranks 40k first and marks 30k `supersededAt 2026-02-03`; recall as of 20 Jan returns only 30k | Works |
| Priya owns the migration (1 Mar), Tomás took over (20 Mar) | Priya's claim valid 1–19 Mar, Tomás's from 20 Mar; a second claim "Priya role: onboarding" created; recall as of 10 Mar returns Priya | Works |
| Lisbon (12 Jun), "Correction: not Lisbon, Porto" (14 Jun) | Lisbon claim closed and labelled "(retracted)"; two new claims, "not Lisbon" and "Porto"; recall leads with Porto | Works, including the retraction label |
| Ana says 30 June, Ben disputes: "I don't think that's right… July 15 according to legal" (30 minutes later) | Ana's claim closed at 09:30; Ben's wins on recency; nothing marks the deadline as contested | **Recency wins; disagreement is lost.** The console's help text promises claims that are "true, false or contested"; none were contested here |
| "Back on March 3 we signed Globex" sent on 15 Apr | The claim's validity starts 15 Apr (the send date); the anchor resolves 3 Mar separately; recall **as of 10 Mar** does not find Globex at all | **Backdated facts are dated by when they were said, not when they happened** |
| Atlas events sent launch → kickoff → beta | `sort: chronological` returns kickoff, beta, launch in true order | Works (orders by source time) |

Supersession behaves as keyed on the slot (subject + attribute): a newer claim on the same slot closes the older one at the newer source's timestamp. The node's `current` flag stayed `true` on superseded claims; it tracks data-point revision, not truth. Truth over time lives in `validFrom`/`validTo` and `supersededAt`.

## 3. Access

Audiences held: the finance identity recalled the payroll figure; the engineering identity asking the same question got no payroll memory at all (it got padding, below). The same in reverse for the credential-rotation fact. Rule audiences (`team is engineering`) worked like fixed lists.

## 4. Recall quality

- Relevant answers come first in every case, with superseded values beside them and flagged, and the source text included.
- **No relevance floor.** Every recall filled the page to the limit (8) with unrelated memories: the engineering identity's payroll query came back with the Acme budget and the Zephyr deadline; "When do credentials rotate?" as finance returned eight unrelated temporal anchors. An agent fed this page gets noise when the true answer is "nothing you may see".
- Evidence pages are small (114–359 tokens for eight documents).

## 5. Coding-session lessons: the weak spot

The session summary ("webhooks failed locally because the signing secret differs between test and live mode. Fix: read STRIPE_WEBHOOK_SECRET_TEST when NODE_ENV is development. Next time check the mode before debugging signatures.") produced three memories: "Raised an issue: Stripe webhook signature failure", "Requested Webhook secret mode check", and a temporal anchor "locally". **The cause and the fix were not extracted.** Recall for the problem still returned the source text second, so an agent reading the page would find the fix, but the memory layer itself does not know it. Their extraction is built for business facts (who, what, how much, when), not procedures.

## 6. What we copy, and where we beat them

Copy:
1. Slot-shaped claims with speaker attribution; supersession on the slot that closes the old claim's validity at the new source's time and flags it on recall.
2. Temporal anchors that resolve relative dates against the source date.
3. Mentions as a cheap topic index so recall finds the source.
4. Retraction detection ("correction: not X").
5. Two-speed ingest (source searchable at once; memories in ~50 s for 16 points).
6. As-of recall and chronological sort; superseded values returned beside current ones with a flag.

Beat them on, each testable with this same script:
1. **Contested claims**: when a later statement disputes rather than updates, keep both and mark the slot contested until someone with authority resolves it (in our product: an approval in the room).
2. **Backdated facts**: validity starts at the resolved event date when the source states one, so "as of 10 March" finds Globex.
3. **A relevance floor**: return fewer results, or none, when nothing the identity may see answers.
4. **Lessons for coding work**: a `lesson` kind with cause, fix and trigger ("when X, do Y"), extracted from session ends; this is the Beacon side and past.dev's gap.
5. **Rooms as audiences** and review before team memory (unchanged from the product definition).

## 7. Housekeeping

The probe data lives in the founder's past.dev project under ids starting `probe-`; delete with `POST /api/v1/data-points/delete` when done. The API key was pasted in chat on 27 Oct and should be revoked after the tests.
