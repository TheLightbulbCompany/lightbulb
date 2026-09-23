--------------------- MODULE OpenRouterGateReservation ---------------------
(***************************************************************************)
(* One owner's shared fast-gate budget row, reserved at request time and    *)
(* settled asynchronously once OpenRouter reports the real cost.            *)
(*                                                                          *)
(* Mirrors (main @ 78c0a0c45, plus PR #3028 fix/openrouter-settlement-      *)
(* overshoot @ 2647c294e -- open, not yet merged, held under this track's   *)
(* TRAIN HYGIENE. This spec validates that PR's design before it merges;    *)
(* re-sync this header once it lands):                                     *)
(*   apps/infra/lambda/openrouter-gate/ledger.mjs        reserve, release   *)
(*   apps/infra/lambda/openrouter-gate/core.mjs          handleRequest      *)
(*     (billingMode = "paid" only -- free/capped rows never touch this row) *)
(*   apps/infra/lambda/openrouter-settlement/index.py    Store.commit,      *)
(*     debit_microcents, Store.pending, settle_one                          *)
(*   core/services/metronome_gate.py                     _row_blocked       *)
(*   routers/billing.py                                  _is_exhausted      *)
(*                                                                          *)
(* The row carries THREE numbers: remaining_microcents (the shared budget   *)
(* pool -- Bedrock and OpenRouter draw from the same one), reserved_microcents*)
(* (Bedrock's own hold, modelled here as an independent actor so the spec   *)
(* exercises the row's real concurrency, not just OpenRouter in isolation), *)
(* and openrouter_reserved_microcents (this module's hold). reserve() is a  *)
(* full-row compare-and-swap on all three (fresh read, retried on failure); *)
(* release() and commit() instead guard only openrouter_reserved_microcents,*)
(* so they never conflict with a concurrent change to the other two fields  *)
(* -- modelled faithfully below with narrow per-field guards, not a second  *)
(* full CAS. Free/capped requests (ledger.recordFree) never reach this row  *)
(* at all and are out of scope, along with everything the retry budget and  *)
(* the "unmetered rows only" auto-release after an hour cover -- this spec  *)
(* checks safety, not liveness (a starved reservation eventually clears).   *)
(*                                                                          *)
(* PR #3028's fix does NOT clamp the real debit -- clamping would           *)
(* undercharge the owner relative to what Metronome actually bills, which   *)
(* is the wrong direction for a money path. debit_microcents(actual_nano-   *)
(* dollars) still comes from OpenRouter's reported cost alone, uncapped by  *)
(* what was reserved, so `remaining` can still legitimately go negative.    *)
(* What changed: the overshoot is now surfaced (an EMF metric + a warn      *)
(* alarm, W81) rather than silent, and _row_blocked / _is_exhausted already *)
(* treat any `remaining <= RESERVE_ESTIMATE_MICROCENTS` (negative included) *)
(* as exhausted with no floor -- so a negative pool blocks every FURTHER    *)
(* admission on this row, on both the gate's own CAS and the backend's      *)
(* credit-balance reads. The safety property is no longer "the pool never   *)
(* goes negative" (PoolNeverNegative, the ORIGINAL invariant this spec      *)
(* found failing) -- it is "no new admission is EVER granted while the pool *)
(* is exhausted" (NoAdmissionWhileExhausted) plus "however far negative it  *)
(* goes is bounded, not unbounded" (OvershootBounded). MaxOvershoot models  *)
(* how far a single settlement's real cost may exceed its own reservation.  *)
(***************************************************************************)
EXTENDS Integers, FiniteSets

CONSTANTS
  Requests,      \* abstract ids of concurrent paid OpenRouter requests for this one owner
  Cap,           \* initial remaining_microcents
  ReserveAmt,    \* Nat: the ceiling estimate reserve() holds -- same for every request, to
                 \* keep the model tractable; nothing checked below depends on requests differing
  BedrockCap,    \* the range Bedrock's own independent hold may occupy: 0..BedrockCap
  MaxOvershoot   \* how far a settled amount may exceed its own reservation (0 = ceiling never violated)

ASSUME /\ Cap \in Nat
       /\ ReserveAmt \in Nat
       /\ BedrockCap \in Nat
       /\ MaxOvershoot \in Nat

VARIABLES
  remaining,        \* remaining_microcents: the shared pool (may legitimately be negative now)
  bedrockReserved,  \* reserved_microcents: Bedrock's own hold on the SAME row
  orReserved,       \* openrouter_reserved_microcents: this module's hold
  state,            \* [Requests -> {"idle","reserved","released","settled"}]
  settleCount,      \* ghost: how many times SettleAttempt actually applied r's debit
  admittedWhileExhausted  \* ghost: did ANY admission (Reserve or BedrockHold) ever fire
                          \* while remaining <= 0 was already true? Should never happen.

vars == <<remaining, bedrockReserved, orReserved, state, settleCount, admittedWhileExhausted>>

TypeOK ==
  /\ remaining \in Int              \* Int, not Nat -- going negative is now an accepted outcome
  /\ bedrockReserved \in 0..BedrockCap
  /\ orReserved \in Nat
  /\ state \in [Requests -> {"idle", "reserved", "released", "settled"}]
  /\ settleCount \in [Requests -> Nat]
  /\ admittedWhileExhausted \in BOOLEAN

Init ==
  /\ remaining = Cap
  /\ bedrockReserved = 0
  /\ orReserved = 0
  /\ state = [r \in Requests |-> "idle"]
  /\ settleCount = [r \in Requests |-> 0]
  /\ admittedWhileExhausted = FALSE

-----------------------------------------------------------------------------
(* Bedrock: a totally separate gate (its own Lambda, its own code) on the   *)
(* SAME row, modelled only as an opaque hold that can grow or shrink within *)
(* its own bound -- not to model Bedrock's own reservation logic in detail  *)
(* (out of scope; CLAUDE.md's "bedrock-gate is being retired" note is why   *)
(* this track doesn't spec it separately). It DOES need the same admission  *)
(* guard OpenRouter's Reserve uses, though: CLAUDE.md's Billing section     *)
(* describes Bedrock's own valve as "admit only if spent + holds + estimate *)
(* < cap" against this identical shared row, and "every door predicting     *)
(* admission must subtract the reserve or a dead zone opens" only makes     *)
(* sense if EVERY admission path enforces the shared margin -- an           *)
(* unconditional Bedrock hold would violate AdmissionRespectsPool trivially *)
(* and prove nothing about OpenRouter's own mechanism.                      *)

BedrockHold ==
  /\ bedrockReserved < BedrockCap
  /\ remaining - bedrockReserved - orReserved - 1 > 0
  /\ bedrockReserved' = bedrockReserved + 1
  /\ admittedWhileExhausted' = admittedWhileExhausted \/ (remaining <= 0)
  /\ UNCHANGED <<remaining, orReserved, state, settleCount>>

BedrockRelease ==
  /\ bedrockReserved > 0
  /\ bedrockReserved' = bedrockReserved - 1
  /\ UNCHANGED <<remaining, orReserved, state, settleCount, admittedWhileExhausted>>

-----------------------------------------------------------------------------
(* reserve(): a full-row CAS against the freshly-read values of all three   *)
(* fields, admitting only if the margin after this hold is STRICTLY         *)
(* positive (the code's own `<= 0` refusal). Modelled as reading current    *)
(* state at the moment it fires -- equivalent to the real 4-attempt         *)
(* fresh-read retry loop for an unbounded retry budget (see header: this is *)
(* a safety spec, so the loop's eventual give-up under sustained contention *)
(* is out of scope, same as it was out of scope for the retry budget in the *)
(* messaging deliveryKey spec). *)

Reserve(r) ==
  /\ state[r] = "idle"
  /\ remaining - bedrockReserved - orReserved - ReserveAmt > 0
  /\ orReserved' = orReserved + ReserveAmt
  /\ state' = [state EXCEPT ![r] = "reserved"]
  /\ admittedWhileExhausted' = admittedWhileExhausted \/ (remaining <= 0)
  /\ UNCHANGED <<remaining, bedrockReserved, settleCount>>

(* release(): the gate's own path when upstream failed before any content   *)
(* arrived. Narrow guard (only its own field), no touch to `remaining` --   *)
(* the pool was never actually debited, only held. *)

ReleasePreGen(r) ==
  /\ state[r] = "reserved"
  /\ orReserved >= ReserveAmt
  /\ orReserved' = orReserved - ReserveAmt
  /\ state' = [state EXCEPT ![r] = "released"]
  /\ UNCHANGED <<remaining, bedrockReserved, settleCount, admittedWhileExhausted>>

(* commit(): the settlement Lambda applies the REAL debit and frees the     *)
(* ESTIMATE in one transaction. The precondition `state[r] = "reserved"`    *)
(* is exactly the request row's own ConditionExpression (`#state = :pending`)*)
(* -- the same mechanism that makes two concurrent commit() attempts for    *)
(* the same row safe: only one TransactWriteItems can win a conditioned     *)
(* update against a value that has already changed, so a second attempt's   *)
(* transaction is cancelled and its recovery path detects `state ==         *)
(* "settled"` and returns without re-debiting. An atomic TLA+ action gated  *)
(* on that same precondition models that guarantee directly -- no separate  *)
(* "loser detects duplicate" action is needed for TLC to rule out a double  *)
(* debit; settleCount is the ghost that proves it didn't happen anyway.     *)

SettleAttempt(r) ==
  /\ state[r] = "reserved"
  /\ orReserved >= ReserveAmt
  /\ \E amt \in 0..(ReserveAmt + MaxOvershoot) :
       /\ remaining' = remaining - amt
       /\ orReserved' = orReserved - ReserveAmt
  /\ state' = [state EXCEPT ![r] = "settled"]
  /\ settleCount' = [settleCount EXCEPT ![r] = @ + 1]
  /\ UNCHANGED <<bedrockReserved, admittedWhileExhausted>>

-----------------------------------------------------------------------------
Next ==
  \/ BedrockHold \/ BedrockRelease
  \/ \E r \in Requests : Reserve(r) \/ ReleasePreGen(r) \/ SettleAttempt(r)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* What must hold, post PR #3028.                                          *)
(*                                                                         *)
(* AdmissionRespectsPool -- the ORIGINAL always-scoped check from before    *)
(* the fix -- is defined below but deliberately NOT in the committed        *)
(* config's INVARIANTS list any more: it is now a category error, not a     *)
(* property the fix provides. Verified by a scratch run (not committed):    *)
(* with BedrockCap = 2, Cap = 6, ReserveAmt = 3, two Bedrock holds admitted  *)
(* while the pool was healthy plus one OpenRouter settlement overshooting   *)
(* afterward legitimately breaks it (bedrockReserved=2, orReserved=0,       *)
(* remaining=1) with NO new, unsafe admission anywhere in the trace --      *)
(* every admission that ran was sound when it ran. The two invariants below *)
(* are what actually survives the fix, correctly scoped to admission TIME   *)
(* rather than every reachable state.                                      *)

\* Kept for the scratch-run comparison above; not part of the committed
\* config -- see the paragraph above for why it no longer belongs there.
AdmissionRespectsPool ==
  bedrockReserved + orReserved <= remaining

\* No admission (OpenRouter's Reserve or Bedrock's own hold, on the SAME
\* row) is EVER granted while the pool is already exhausted. This is what
\* "no admission while remaining <= 0" means precisely, and it is what
\* stops one overshoot from compounding into a second, unrelated one.
NoAdmissionWhileExhausted ==
  ~admittedWhileExhausted

\* However far negative the pool goes, it is bounded -- not unbounded.
\* Each of the (at most) |Requests| reservations ever outstanding can
\* individually overshoot by at most MaxOvershoot before NoAdmissionWhile-
\* Exhausted stops anything new from adding to it.
OvershootBounded ==
  remaining >= -(Cardinality(Requests) * MaxOvershoot)

\* A request's debit is applied to the pool at most once, however many times
\* SettleAttempt(r) is scheduled (models a settlement Lambda re-driven by a
\* retry, a redundant schedule tick, or two overlapping invocations).
SettledAtMostOnce ==
  \A r \in Requests : settleCount[r] <= 1

=============================================================================
