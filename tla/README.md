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
spec checks safety, not liveness — the pool never goes negative, not that a
stuck hold eventually clears.

| Constant | Committed value | Meaning |
|---|---|---|
| `MaxOvershoot` | `0` | how far a settlement's real debit may exceed the reservation it replaces |

| Invariant | Holds at `MaxOvershoot = 0` | What it says |
|---|---|---|
| `AdmissionRespectsPool` | yes | outstanding holds (Bedrock's + OpenRouter's, on the SAME row) never exceed what the pool has left, however the two independently interleave |
| `PoolNeverNegative` | yes | the shared budget never goes below zero |
| `SettledAtMostOnce` | yes | a request's real cost is debited at most once, however many times settlement is re-driven |

**A real gap, found and reported, not fixed here** (this track's TRAIN HYGIENE
rule holds runtime changes as open PRs until the coordinator announces the
prod dispatch): `commit()`'s transaction conditions the release of the HELD
ESTIMATE (`openrouter_reserved_microcents >= reservation_microcents`) but
never conditions the REAL DEBIT it applies to `remaining_microcents` on
anything — `debit_microcents(actual_nanodollars)` is computed from
OpenRouter's reported cost alone, uncapped by what was reserved. Safety rests
entirely on an assumption made outside this code: that the reservation's
price-ceiling estimate always dominates what OpenRouter actually charges.
Verified: `MaxOvershoot = 2` (a settlement allowed to exceed its own
reservation) breaks `PoolNeverNegative` in 343 states — a scratch run, not
committed, matching how this track documents a demonstrated-but-not-shipped
counterexample. Nothing in the code catches a mispriced or underestimated
ceiling; there's no code-level clamp, only an assumption.
