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
