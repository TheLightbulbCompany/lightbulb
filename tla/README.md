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
| `MemberCheckInTxn` | `FALSE` | `create_message`'s transaction conditions on the author's `MEMBER` row |
| `ConsistentMetaRead` | `TRUE` | the projector reads `META` with `ConsistentRead` |

| Invariant | Holds under | What it says |
|---|---|---|
| `NoLeak` | code today | a client only ever receives, over the room channel, messages written while it was a member |
| `RevokedNeverHoldsLiveKey` | code today | a removed client never holds the current key |
| `AuthorWasMember` | `MemberCheckInTxn = TRUE` only | a message's author was a member when the write committed |

Two counterexamples worth knowing. Flip a constant in a copy of the config and
run it to see the trace:

- `ConsistentMetaRead = FALSE` breaks `NoLeak`: the projector reads a stale
  `META`, publishes a post-removal message on the old key, and the removed
  client receives it. The `ConsistentRead=True` in the projector's `_get_item`
  is load-bearing.
- `MemberCheckInTxn = FALSE` (the code) breaks `AuthorWasMember`: a sender
  passes `require_room_member`, is removed, and their message still commits
  because the transaction only conditions on `lastSeq`. A `ConditionCheck` on
  the author's `MEMBER` row closes it.

Out of scope, deliberately: agents and `WORK#` fanout, HTTP history paging,
mobile push, re-adding a removed member, edits, and liveness (eventual
delivery relies on the client's reconcile-on-rotate and its periodic refresh,
which the model would only restate as fairness).

The committed configs bound `Humans` to 2 (owner + one removable member) so
CI stays under two seconds (~9k distinct states); this already exercises both
notice kinds (`removed` and `rotated`) and every race between a send, a
removal, and a client's resubscribe. A deeper run with 3 humans and the same
`MaxSeq` — exhaustive to 14.7M distinct states, ~2.5 minutes — was checked by
hand and found the same result; it isn't committed because it adds runtime
without adding a reachable state shape the 2-human bound doesn't already hit.
