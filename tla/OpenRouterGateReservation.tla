--------------------- MODULE OpenRouterGateReservation ---------------------
(***************************************************************************)
(* One owner's shared fast-gate budget row, reserved at request time and    *)
(* settled asynchronously once OpenRouter reports the real cost.            *)
(*                                                                          *)
(* Mirrors (main @ e3dfd88cb):                                              *)
(*   apps/infra/lambda/openrouter-gate/ledger.mjs        reserve, release   *)
(*   apps/infra/lambda/openrouter-gate/core.mjs          handleRequest      *)
(*     (billingMode = "paid" only -- free/capped rows never touch this row) *)
(*   apps/infra/lambda/openrouter-settlement/index.py    Store.commit,      *)
(*     debit_microcents, Store.pending, settle_one                          *)
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
(* checks safety (the pool never goes negative), not liveness (a starved    *)
(* reservation eventually clears).                                         *)
(*                                                                          *)
(* The one guard commit() does NOT carry: nothing clamps the REAL debit to  *)
(* the held ESTIMATE. debit_microcents(actual_nanodollars) is computed from *)
(* OpenRouter's reported cost alone; the transaction's ConditionExpression  *)
(* only checks openrouter_reserved_microcents >= reservation_microcents (a  *)
(* pre-image check on the HOLD, unrelated to the sign of the debit). Safety *)
(* therefore rests entirely on an assumption made outside this code: that   *)
(* the reservation estimate (ceilPricedTokens against policy.max_price, a   *)
(* configured PRICE CEILING) always dominates what OpenRouter actually      *)
(* charges. MaxOvershoot models what happens if that assumption is ever     *)
(* wrong -- a stale policy, a priced field the ceiling math misses, a       *)
(* multi-completion response -- with no code-level guard to catch it.       *)
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
  remaining,        \* remaining_microcents: the shared pool
  bedrockReserved,  \* reserved_microcents: Bedrock's own hold on the SAME row
  orReserved,       \* openrouter_reserved_microcents: this module's hold
  state,            \* [Requests -> {"idle","reserved","released","settled"}]
  settleCount       \* ghost: how many times SettleAttempt actually applied r's debit

vars == <<remaining, bedrockReserved, orReserved, state, settleCount>>

TypeOK ==
  /\ remaining \in Int              \* Int, not Nat -- going negative is exactly the failure mode checked
  /\ bedrockReserved \in 0..BedrockCap
  /\ orReserved \in Nat
  /\ state \in [Requests -> {"idle", "reserved", "released", "settled"}]
  /\ settleCount \in [Requests -> Nat]

Init ==
  /\ remaining = Cap
  /\ bedrockReserved = 0
  /\ orReserved = 0
  /\ state = [r \in Requests |-> "idle"]
  /\ settleCount = [r \in Requests |-> 0]

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
  /\ UNCHANGED <<remaining, orReserved, state, settleCount>>

BedrockRelease ==
  /\ bedrockReserved > 0
  /\ bedrockReserved' = bedrockReserved - 1
  /\ UNCHANGED <<remaining, orReserved, state, settleCount>>

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
  /\ UNCHANGED <<remaining, bedrockReserved, settleCount>>

(* release(): the gate's own path when upstream failed before any content   *)
(* arrived. Narrow guard (only its own field), no touch to `remaining` --   *)
(* the pool was never actually debited, only held. *)

ReleasePreGen(r) ==
  /\ state[r] = "reserved"
  /\ orReserved >= ReserveAmt
  /\ orReserved' = orReserved - ReserveAmt
  /\ state' = [state EXCEPT ![r] = "released"]
  /\ UNCHANGED <<remaining, bedrockReserved, settleCount>>

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
  /\ UNCHANGED bedrockReserved

-----------------------------------------------------------------------------
Next ==
  \/ BedrockHold \/ BedrockRelease
  \/ \E r \in Requests : Reserve(r) \/ ReleasePreGen(r) \/ SettleAttempt(r)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* What must hold. *)

\* The CAS mechanism's own correctness: outstanding holds never exceed what
\* the pool has left, no matter how Bedrock's hold moves underneath it.
AdmissionRespectsPool ==
  bedrockReserved + orReserved <= remaining

\* The property that actually matters: the shared pool never goes negative.
\* Provably requires MaxOvershoot = 0 (see header) -- checked failing on
\* purpose under a nonzero MaxOvershoot to demonstrate the gap is real, not
\* hypothetical.
PoolNeverNegative ==
  remaining >= 0

\* A request's debit is applied to the pool at most once, however many times
\* SettleAttempt(r) is scheduled (models a settlement Lambda re-driven by a
\* retry, a redundant schedule tick, or two overlapping invocations).
SettledAtMostOnce ==
  \A r \in Requests : settleCount[r] <= 1

=============================================================================
