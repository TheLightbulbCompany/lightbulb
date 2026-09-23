--------------------- MODULE JevDecisionConvergence ---------------------
(***************************************************************************)
(* One message, one shared DECISION row, and every candidate agent's own    *)
(* work-queue consumer racing to compute -- or just adopt -- the SAME       *)
(* routing decision. Dispatch correctness rests entirely on this: every     *)
(* consumer must act on an IDENTICAL final decision, because each one       *)
(* independently decides "am I the agent Jev picked?" against whatever      *)
(* value it ends up holding (openclaw_work.py's `decision.get("who") ==     *)
(* member["id"]` check). Two consumers holding two different decisions is   *)
(* two agents dispatched for one message.                                   *)
(*                                                                          *)
(* Mirrors (main @ 78c0a0c45):                                              *)
(*   apps/backend/core/repositories/messaging_repo.py   claim_decision,     *)
(*     finalize_decision, get_decision                                     *)
(*   apps/backend/core/services/jev.py                  route()             *)
(*   apps/backend/core/services/openclaw_work.py        the dispatch site   *)
(*     that reads route()'s return value and acts on decision.get("who")   *)
(*                                                                          *)
(* One consumer per candidate agent calls route() for the same message.     *)
(* Exactly one CAN claim_decision() and compute (a full JEV_MIN_P /         *)
(* who/whoP decision object, abstracted here to a plain value); a slow      *)
(* claim that outlives the 60s staleness window lets ANOTHER consumer       *)
(* re-claim and ALSO compute, without the first one knowing -- modelled as  *)
(* Claim being re-enabled whenever the row is still "claimed", not just     *)
(* when it's empty, since the code's own re-claim condition never checks    *)
(* WHO holds a pending claim, only how old it is. Every other consumer      *)
(* waits, either observing the row go final (the common case) or hitting    *)
(* its own deadline first and writing a loser_timeout fallback.             *)
(*                                                                          *)
(* Out of scope: the retry/rate-limit backoff cycle (mechanically the same  *)
(* "reclaimable, not yet final" state as an empty row, so folding it in     *)
(* would not exercise a distinct risk), the no-gateway-key "disabled" mode  *)
(* (a secondary path using the same finalize_decision primitive), staleness *)
(* TIMING itself (a liveness concern, not safety -- this spec checks that   *)
(* consumers never disagree, not that one is never left waiting forever).   *)
(***************************************************************************)
EXTENDS FiniteSets

CONSTANTS
  Consumers,       \* one per candidate agent racing to route this one message
  FinalValues,      \* the distinct decision values a computing consumer might produce
  LoserTO,          \* the distinct {"fallback": true, "reason": "loser_timeout"} value
  FirstWriteWins    \* TRUE = the code today (finalize_decision's conditioned write);
                    \* FALSE = the hypothetical pre-#2960 bug (an unconditional overwrite)

ASSUME /\ LoserTO \notin FinalValues
       /\ FirstWriteWins \in BOOLEAN

NoValue == "none"  \* a string literal, disjoint from the model values in FinalValues/LoserTO by type

VARIABLES
  decisionState,  \* "empty" (absent, or a rate-limit retry past retryAt -- both reclaimable),
                   \* "claimed" (at least one consumer believes it holds the live claim),
                   \* "final" (a non-pending, non-retry decision is stored)
  decisionValue,  \* the stored value once decisionState = "final"
  phase,          \* [Consumers -> {"unclaimed","computing","waiting","done"}]
  myResult        \* [Consumers -> FinalValues \cup {LoserTO, NoValue}]: what each consumer acted on

vars == <<decisionState, decisionValue, phase, myResult>>

TypeOK ==
  /\ decisionState \in {"empty", "claimed", "final"}
  /\ decisionValue \in FinalValues \cup {LoserTO, NoValue}
  /\ phase \in [Consumers -> {"unclaimed", "computing", "waiting", "done"}]
  /\ myResult \in [Consumers -> FinalValues \cup {LoserTO, NoValue}]

Init ==
  /\ decisionState = "empty"
  /\ decisionValue = NoValue
  /\ phase = [c \in Consumers |-> "unclaimed"]
  /\ myResult = [c \in Consumers |-> NoValue]

-----------------------------------------------------------------------------
(* claim_decision(): succeeds on an empty row (the normal case) or a row    *)
(* that's "claimed" but stale (the code's staleness window doesn't check    *)
(* who holds it, only how long ago -- so this deliberately allows MORE than *)
(* one consumer to believe itself the live computer). *)

Claim(c) ==
  /\ phase[c] = "unclaimed"
  /\ decisionState \in {"empty", "claimed"}
  /\ decisionState' = "claimed"
  /\ phase' = [phase EXCEPT ![c] = "computing"]
  /\ UNCHANGED <<decisionValue, myResult>>

(* A consumer whose own claim attempt found the row already claimed enters  *)
(* the poll loop instead (route()'s `elif claim_decision(...)` being False). *)

StartWaiting(c) ==
  /\ phase[c] = "unclaimed"
  /\ decisionState = "claimed"
  /\ phase' = [phase EXCEPT ![c] = "waiting"]
  /\ UNCHANGED <<decisionState, decisionValue, myResult>>

(* finalize_decision(): the conditioned write is exactly the guard below --  *)
(* first commit wins, every later attempt (winner or loser) just reads and   *)
(* adopts whatever is already there. FirstWriteWins = FALSE is the          *)
(* hypothetical unconditioned overwrite this spec exists to rule out.       *)

Finalize(c, v) ==
  /\ phase[c] \in {"computing", "waiting"}
  /\ IF FirstWriteWins /\ decisionState = "final"
       THEN /\ myResult' = [myResult EXCEPT ![c] = decisionValue]
            /\ UNCHANGED <<decisionState, decisionValue>>
       ELSE /\ decisionState' = "final"
            /\ decisionValue' = v
            /\ myResult' = [myResult EXCEPT ![c] = v]
  /\ phase' = [phase EXCEPT ![c] = "done"]

WinnerFinalize(c) == \E v \in FinalValues : phase[c] = "computing" /\ Finalize(c, v)
LoserTimeout(c)   == phase[c] = "waiting" /\ Finalize(c, LoserTO)

(* The common path: a waiter notices the row already final and adopts it    *)
(* before its own deadline (`get_decision`'s poll loop, checked every       *)
(* _LOSER_POLL_S). *)

ObserveFinal(c) ==
  /\ phase[c] = "waiting"
  /\ decisionState = "final"
  /\ myResult' = [myResult EXCEPT ![c] = decisionValue]
  /\ phase' = [phase EXCEPT ![c] = "done"]
  /\ UNCHANGED <<decisionState, decisionValue>>

-----------------------------------------------------------------------------
Next ==
  \E c \in Consumers :
    \/ Claim(c) \/ StartWaiting(c) \/ WinnerFinalize(c) \/ LoserTimeout(c) \/ ObserveFinal(c)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* What must hold: every consumer that has acted (myResult set) acted on     *)
(* the SAME value as every other one that has. This is the actual dispatch  *)
(* guarantee -- openclaw_work.py's per-consumer "am I the chosen one?"      *)
(* check is a pure function of this shared value, so two different values   *)
(* held by two consumers is two different dispatch decisions for one        *)
(* message. *)

AtMostOneFinalValue ==
  \A c1, c2 \in Consumers :
    (myResult[c1] /= NoValue /\ myResult[c2] /= NoValue) => myResult[c1] = myResult[c2]

===========================================================================
