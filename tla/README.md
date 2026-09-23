# TLA+ specs

Model-checked specifications of the platform's concurrency protocols. Each
`.tla` module is a model of a protocol as the code implements it, with the
code it mirrors named in its header comment; each `.cfg` is one checked
configuration (constants + invariants). `tla/check.sh` runs TLC over every
config and is what `tla.yml` runs on PRs that touch this directory.

```bash
tla/check.sh                                  # everything (fetches the pinned TLC jar once)
tla/check.sh MessagingDeliveryKey.cfg         # one config
```

A spec checks the DESIGN, not the code: when the code it names changes, the
spec must change with it or it is checking something that no longer exists.
Treat the header's file list like an import list.

## MessagingDeliveryKey

The AppSync revocation boundary from CLAUDE.md's Messaging section: removing a
human from a room rotates the room's `deliveryKey` in the same transaction that
deletes their `MEMBER` row, because AppSync cannot close an open subscription.

| Constant | Code today | Meaning |
|---|---|---|
| `MemberCheckInTxn` | `TRUE` (since PR #2965) | `create_message`'s transaction carries a `ConditionCheck` on the author's `MEMBER` row |
| `ConsistentMetaRead` | `TRUE` | the projector reads `META` with `ConsistentRead` |

| Invariant | Holds under the shipped code | What it says |
|---|---|---|
| `NoLeak` | yes | a client only ever receives, over the room channel, messages written while it was a member |
| `RevokedNeverHoldsLiveKey` | yes | a removed client never holds the current key |
| `AuthorWasMember` | yes | a message's author was a member when the write committed |

The single committed config models the code as it stands. Two counterexamples
are worth knowing anyway — flip the named constant in a copy of the config and
run it to see the trace:

- `ConsistentMetaRead = FALSE` breaks `NoLeak`: the projector reads a stale
  `META`, publishes a post-removal message on the old key, and the removed
  client receives it. The `ConsistentRead=True` in the projector's `_get_item`
  is load-bearing.
- `MemberCheckInTxn = FALSE` breaks `AuthorWasMember` — this was the shipped
  code until PR #2965: a sender passes `require_room_member`, is removed, and
  their message still commits because the transaction conditioned only on
  `lastSeq`. TLC found this in 281 states (the spec's original PR, #2963); the
  `ConditionCheck` on the author's `MEMBER` row that closes it is what
  `MemberCheckInTxn = TRUE` now models.

Out of scope, deliberately: agents and `WORK#` fanout, HTTP history paging,
mobile push, re-adding a removed member, edits, and liveness (eventual
delivery relies on the client's reconcile-on-rotate and its periodic refresh,
which the model would only restate as fairness).

The committed config bounds `Humans` to 2 (owner + one removable member) so
CI stays under two seconds (~9k distinct states); this already exercises both
notice kinds (`removed` and `rotated`) and every race between a send, a
removal, and a client's resubscribe. A deeper run with 3 humans and the same
`MaxSeq` — exhaustive to 14.7M distinct states, ~2.5 minutes — was checked by
hand and found the same result; it isn't committed because it adds runtime
without adding a reachable state shape the 2-human bound doesn't already hit.

## OpenRouterGateReservation

The money valve that replaced the (now-retiring) Bedrock gate for paid
inference: `apps/infra/lambda/openrouter-gate`'s `reserve()` holds an estimate
against the owner's shared fast-gate row before forwarding to OpenRouter;
`apps/infra/lambda/openrouter-settlement`'s `commit()` later replaces that
estimate with the real cost, asynchronously, once OpenRouter reports it. Free
and capped requests never touch this row at all (`ledger.recordFree` only
writes the outbox row) and are out of scope; so is everything the retry
budget and the "unmetered rows only" auto-release after an hour cover. This
spec checks safety, not liveness.

**History.** The first version of this spec found a real gap: `commit()`
conditions the release of the held estimate but never conditions the real
debit it applies to `remaining_microcents` on anything, so an under-estimated
price ceiling could drive the shared pool negative with no code-level
defense. Reported to the coordinator, not fixed inline (this track's TRAIN
HYGIENE rule). The fix landed as **PR #3028**, and this section models that
design: **it does not clamp the real debit** — clamping would undercharge the
owner relative to what Metronome actually bills, the wrong direction for a
money path. What changed instead: the overshoot is now surfaced (an EMF
metric plus a warn alarm, `W81`), and the backend's own exhaustion checks
(`metronome_gate._row_blocked`, `billing._is_exhausted`) already treat any
`remaining <= RESERVE_ESTIMATE_MICROCENTS` — negative included, no floor — as
exhausted, so a negative pool blocks every further admission on the row.

| Constant | Committed value | Meaning |
|---|---|---|
| `MaxOvershoot` | `2` (nonzero — overshoot is now an accepted, bounded, alarmed outcome, not a bug to assume away) | how far a single settlement's real debit may exceed the reservation it replaces |

| Invariant | Holds at `MaxOvershoot = 2` | What it says |
|---|---|---|
| `NoAdmissionWhileExhausted` | yes | no admission (OpenRouter's `Reserve` or Bedrock's own hold, on the SAME row) is ever granted while the pool is already exhausted — this is the precise meaning of "no admission while remaining ≤ 0," and it's what stops one overshoot from compounding into another |
| `OvershootBounded` | yes | however far negative the pool goes, it's bounded by the number of requests that were ever outstanding times `MaxOvershoot`, not unbounded |
| `SettledAtMostOnce` | yes | unchanged from before — a request's real cost is debited at most once, however many times settlement is re-driven |

**`AdmissionRespectsPool`** (`bedrockReserved + orReserved <= remaining`,
checked at every reachable state) was the original spec's admission-safety
check, and it no longer belongs in the committed config — not because
admission is unsafe, but because it's now a category error. Verified by a
scratch run (not committed): two Bedrock holds admitted while the pool was
healthy, followed by one OpenRouter settlement overshooting, legitimately
breaks it (`bedrockReserved=2, orReserved=0, remaining=1`) with the ghost
`admittedWhileExhausted` flag staying `FALSE` throughout the same trace —
every admission that ran was sound when it ran; the pool simply moved
underneath already-approved holds afterward, exactly as designed.
`NoAdmissionWhileExhausted` is the correctly-scoped (admission-time, not
always) successor to what `AdmissionRespectsPool` was trying to check.

The header comment notes this section models PR #3028 as of its own branch
commit, not yet merged to `main` — re-sync it once that PR lands, the same
way `MessagingDeliveryKey`'s config was re-synced after PR #2965.

## JevDecisionConvergence

One message, one shared `DECISION` row, and every candidate agent's own
work-queue consumer racing to compute — or just adopt — the routing
decision. `openclaw_work.py`'s dispatch site is a pure function of whatever
value each consumer ends up holding (`decision.get("who") == member["id"]`),
so two consumers holding two different decisions is two agents dispatched
for one message. Exactly one consumer can `claim_decision()` and compute; a
slow claim that outlives the 60 s staleness window lets ANOTHER consumer
re-claim and also compute, without the first one knowing (the staleness
check never verifies who holds the claim, only how old it is) — modelled as
allowing more than one consumer to reach "computing" for the same message,
the actual worst case the code permits. Every other consumer waits, either
observing the row go final (the common path) or hitting its own deadline
first and writing a `loser_timeout` fallback.

| Constant | Committed value | Meaning |
|---|---|---|
| `FirstWriteWins` | `TRUE` | `finalize_decision`'s write is conditioned: once the row holds a final decision, no later write (winner's own real decision, a second loser's fallback) can replace it |

| Invariant | Holds at `FirstWriteWins = TRUE` | What it says |
|---|---|---|
| `AtMostOneFinalValue` | yes | every consumer that has acted, acted on the same decision as every other one that has |

`FirstWriteWins = FALSE` models the pre-`#2960` shape this spec exists to
rule out: an unconditioned overwrite, where a slow winner finishing its own
computation AFTER a loser already stored its `loser_timeout` fallback would
silently replace it — and any consumer that read the row before that
replacement already dispatched on the OLD value. Verified: `AtMostOneFinalValue`
breaks in 76 distinct states under that config. `#2960` is already shipped;
this spec exists to keep it that way — a future change to `finalize_decision`
that drops the condition would be caught here before it reaches production.

Out of scope: the retry/rate-limit backoff cycle (mechanically the same
"reclaimable, not yet final" state as an empty row, so it wouldn't exercise a
distinct risk), the no-gateway-key "disabled" fallback mode (a secondary path
built on the same `finalize_decision` primitive), and staleness timing itself
(a liveness concern — this spec checks that consumers never disagree, not
that one is never left waiting forever).

## ProvisioningClaimLease

The last of the original three-item backlog's siblings, and the fourth spec
overall: the token-fenced stale-claim-steal pattern that guards Paperclip
company creation against a double-create when multiple backend instances
race to provision the same org. One shared claim row: absent, or held by
some (holder identity, per-attempt token). A holder can be stolen once its
claim looks stale; the SAME admin identity re-polls with the SAME holder
value, so a release fenced on holder alone is a classic ABA hazard — a
resurrected straggler from a superseded attempt can still match `holder` and
delete a second, live attempt's claim. Both real claims this spec abstracts
(`try_acquire_org_create_claim`, `try_acquire_seed_claim`) use a per-attempt
TOKEN instead, specifically to close this.

| Constant | Committed value | Meaning |
|---|---|---|
| `TokenFenced` | `TRUE` | release/steal fenced on the per-attempt token — the code today |

| Invariant | Holds at `TokenFenced = TRUE` | What it says |
|---|---|---|
| `NoStaleReleaseCorruption` | yes | a release never deletes a different, still-live attempt's claim (the ABA property) |
| `AtMostOneEverSucceeded` | **no — see below** | not checked in the committed config |

`TokenFenced = FALSE` models the historical, holder-only design
`release_org_create_claim`'s own docstring narrates as the pre-fix shape
("an OLD worker still runs the pre-token `release_org_create_claim` whose
condition is `holder = :h`"). Verified: `NoStaleReleaseCorruption` breaks
under it — the token fence is genuinely load-bearing, not decorative.

**A real gap, found and reported, not fixed here** (TRAIN HYGIENE): `AtMostOneEverSucceeded`
fails even under `TokenFenced = TRUE`, independent of the ABA fix entirely.
The staleness check a steal relies on only measures a timestamp's age — it
has no way to distinguish "the original holder crashed hard" from "the
original holder is still alive and about to legitimately finish, just
slower than the ~135 s the code's own docstring assumes as a ceiling." TLC
finds a reachable trace where attempt `t1` claims, the row goes stale while
`t1` is still genuinely working, `t2` steals and also succeeds — both
`everSucceeded[t1]` and `everSucceeded[t2]` end up `TRUE`. This matters most
for the **poll-thread reconcile path** (`_reconcile_owner_seed`), which
passes the POLLING ADMIN's own identity as `owner_user_id` — unlike the
fresh-provision executor's step 0, which always resolves the org's fixed
Clerk creator regardless of which execution triggered it. Because
`provision_org`'s own inner seed claim (`try_acquire_seed_claim`) is keyed
on that SAME `owner_user_id`, two different admins of one org polling
concurrently, with one attempt legitimately slow past the staleness
threshold, hit two DIFFERENT inner-claim keys too — so the inner claim does
not backstop this race for the reconcile path the way it structurally does
for a fresh provision. Verified via a scratch run (not committed, matching
this track's convention): the violation is reachable in 6 steps regardless
of `TokenFenced`.

## A precedence bug found auditing this track's own specs

While writing this spec, TLC reported "Successor state is not completely
specified" for an action that assigns every primed variable in every branch
— a symptom of `X' = Y \/ Z` parsing as `(X' = Y) \/ Z`, not `X' = (Y \/ Z)`:
`=` binds tighter than `\/` in TLA+. `OpenRouterGateReservation.tla` (merged
in `#3022`/`#3030`) had this exact bug in both places it set
`admittedWhileExhausted'`. Fixed here, in the same PR, alongside this new
spec. The substantive conclusion did not change — `Reserve` and `BedrockHold`'s
OWN admission guards already imply `remaining > 0` whenever they fire, so
the buggy expression's "unconstrained" branch was structurally unreachable
and `NoAdmissionWhileExhausted` held for the right underlying reason even
while the ghost variable meant to double-check it was silently a no-op.
Re-verified after the fix: same result, now for a meaningful reason instead
of an accidental one. `MessagingDeliveryKey.tla` and `JevDecisionConvergence.tla`
were audited too and don't use this pattern.
