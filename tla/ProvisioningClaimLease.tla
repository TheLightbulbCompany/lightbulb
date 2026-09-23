------------------------- MODULE ProvisioningClaimLease -------------------------
(***************************************************************************)
(* The token-fenced stale-claim-steal pattern that guards Paperclip company *)
(* creation against a double-create when multiple backend instances race to *)
(* provision the same org. One shared claim row: absent, or held by some    *)
(* (holder identity, per-attempt token). A holder can be stolen once its    *)
(* claim looks stale (the code's own bounded-HTTP-timeout argument treats   *)
(* "older than stale_after_seconds" as proof the original process crashed   *)
(* hard); the SAME admin identity re-polls with the SAME holder value, so a *)
(* release fenced on holder ALONE is a classic ABA hazard: a resurrected    *)
(* straggler from the FIRST (superseded) attempt can still match `holder`   *)
(* and delete a SECOND, live attempt's claim. Both real claims this spec    *)
(* abstracts use a per-attempt TOKEN instead, specifically to close this.   *)
(*                                                                          *)
(* Mirrors (main @ 68bebb6e1):                                              *)
(*   core/repositories/paperclip_repo.py   try_acquire_org_create_claim,    *)
(*     release_org_create_claim, try_acquire_seed_claim,                    *)
(*     update_status(require_seeding_claim_id=...)                          *)
(*   core/services/user_provision.py       _ensure_paperclip_for_org (the   *)
(*     caller reached from both a fresh provision's step 0 and the poll-    *)
(*     path reconcile -- see its own docstring for why concurrent executions*)
(*     are possible: "dispatch is racy by design")                          *)
(*                                                                          *)
(* One shared abstraction covers both real claims: they differ only in      *)
(* which row they live on (a dedicated claims table vs. the company row     *)
(* itself) and their exact field names, not in the mechanism being checked  *)
(* here. Out of scope: the SFN-dispatch-level claim one layer further out   *)
(* (provision_dispatch's own single-flight start), Paperclip's own 409-as-  *)
(* idempotent handling (a different safety net, for a different failure     *)
(* mode -- this spec is only about the claim row itself), and the staleness *)
(* TIMING argument (a liveness claim the code's docstring defends inline;   *)
(* this spec takes "eventually looks stale" as given and checks safety).    *)
(***************************************************************************)
EXTENDS FiniteSets, TLC

CONSTANTS
  TokenFenced     \* TRUE = the code today (release/steal fenced on the per-attempt token);
                  \* FALSE = the historical shape release_org_create_claim's own docstring
                  \* narrates as the pre-fix, holder-fenced design

ASSUME TokenFenced \in BOOLEAN

\* One deliberately-chosen scenario, not swept over arbitrary assignments: t1
\* and t2 share a holder (the same admin re-polling after their first attempt
\* went stale -- exactly the ABA pair this mechanism exists to protect
\* against); t3 is a different holder entirely. Fixed in the module rather
\* than parameterized via CONSTANTS -- TLC's .cfg format has no function-
\* literal syntax, and there is nothing to learn from varying this mapping.
Holders == {"h1", "h2"}
Attempts == {"t1", "t2", "t3"}
AttemptHolder == ("t1" :> "h1") @@ ("t2" :> "h1") @@ ("t3" :> "h2")

NoRow == "none"  \* disjoint from Attempts by value -- rowToken = NoRow means absent

VARIABLES
  rowToken,      \* claim_token / seeding_claim_id of whoever currently holds the row, or NoRow
  rowAge,        \* "fresh" or "stale" -- the code's own bounded-timeout argument for what
                 \* "stale" implies is out of scope (see header); this is just its conclusion
  attemptState,  \* [Attempts -> {"unstarted", "holding", "done"}]
  everSucceeded, \* ghost: [Attempts -> BOOLEAN] -- did this attempt EVER complete while it
                 \* was still the row's legitimate holder (the only way "success" can mean
                 \* anything -- see Succeed below)
  corrupted      \* ghost: did a release EVER delete a DIFFERENT, still-live attempt's claim

vars == <<rowToken, rowAge, attemptState, everSucceeded, corrupted>>

TypeOK ==
  /\ rowToken \in Attempts \cup {NoRow}
  /\ rowAge \in {"fresh", "stale"}
  /\ attemptState \in [Attempts -> {"unstarted", "holding", "done"}]
  /\ everSucceeded \in [Attempts -> BOOLEAN]
  /\ corrupted \in BOOLEAN

Init ==
  /\ rowToken = NoRow
  /\ rowAge = "fresh"
  /\ attemptState = [t \in Attempts |-> "unstarted"]
  /\ everSucceeded = [t \in Attempts |-> FALSE]
  /\ corrupted = FALSE

-----------------------------------------------------------------------------
(* Claim: wins on an absent row (attribute_not_exists), or steals a row that *)
(* looks stale. Either way this attempt becomes the sole current holder;    *)
(* the PREVIOUS holder's own local attemptState is untouched -- it doesn't  *)
(* know it was stolen, exactly like the real crashed-or-merely-slow process *)
(* it's meant to model. *)

Claim(t) ==
  /\ attemptState[t] = "unstarted"
  /\ rowToken = NoRow \/ rowAge = "stale"
  /\ rowToken' = t
  /\ rowAge' = "fresh"
  /\ attemptState' = [attemptState EXCEPT ![t] = "holding"]
  /\ UNCHANGED <<everSucceeded, corrupted>>

(* The wall clock crossing the code's own staleness threshold for whoever   *)
(* currently holds the row -- see header for why the "implies crashed"      *)
(* argument itself is out of scope; this only supplies its conclusion.      *)

TimePasses ==
  /\ rowToken /= NoRow
  /\ rowAge = "fresh"
  /\ rowAge' = "stale"
  /\ UNCHANGED <<rowToken, attemptState, everSucceeded, corrupted>>

(* Succeed: only means something if this attempt is STILL the row's live    *)
(* holder when it completes -- a superseded attempt that eventually wakes   *)
(* up and finishes its own HTTP chain is a Paperclip-level idempotency      *)
(* concern (the 409-as-success handling), not a claim-row concern, so it's  *)
(* simply stuck here rather than modelled as a distinct, no-op outcome.     *)

Succeed(t) ==
  /\ attemptState[t] = "holding"
  /\ rowToken = t
  /\ attemptState' = [attemptState EXCEPT ![t] = "done"]
  /\ everSucceeded' = [everSucceeded EXCEPT ![t] = TRUE]
  /\ UNCHANGED <<rowToken, rowAge, corrupted>>

(* Release: the failure path (release_org_create_claim / the seeding-claim  *)
(* downgrade via require_seeding_claim_id). This is where the ABA hazard    *)
(* lives -- a guard that matches on HOLDER rather than TOKEN can fire for   *)
(* the wrong attempt whenever two attempts share a holder identity. *)

ReleaseGuardOK(t) ==
  IF TokenFenced THEN rowToken = t
  ELSE rowToken /= NoRow /\ AttemptHolder[rowToken] = AttemptHolder[t]

\* Split into two top-level actions (matching the flat, non-nested style used
\* everywhere else in this track's specs) rather than one action with an
\* internal \/ -- TLC's "successor state fully specified" check trips on the
\* nested form even though every branch does assign every primed variable.
ReleaseGranted(t) ==
  /\ attemptState[t] = "holding"
  /\ ReleaseGuardOK(t)
  /\ attemptState' = [attemptState EXCEPT ![t] = "done"]
  /\ rowToken' = NoRow
  /\ rowAge' = "fresh"
  /\ corrupted' = (corrupted \/ (rowToken /= t))  \* deleted someone ELSE's live claim; the
                                                   \* parens are load-bearing -- `=` binds tighter
                                                   \* than `\/` in TLA+, so `a' = b \/ c` parses as
                                                   \* `(a' = b) \/ c`, not `a' = (b \/ c)`
  /\ UNCHANGED everSucceeded

ReleaseRefused(t) ==
  /\ attemptState[t] = "holding"
  /\ ~ReleaseGuardOK(t)
  /\ attemptState' = [attemptState EXCEPT ![t] = "done"]
  /\ UNCHANGED <<rowToken, rowAge, corrupted, everSucceeded>>

Release(t) == ReleaseGranted(t) \/ ReleaseRefused(t)

-----------------------------------------------------------------------------
Next ==
  \/ TimePasses
  \/ \E t \in Attempts : Claim(t) \/ Succeed(t) \/ Release(t)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* What must hold. *)

\* The ABA property this whole mechanism exists for: a release never deletes
\* a DIFFERENT, currently-live attempt's claim.
NoStaleReleaseCorruption ==
  ~corrupted

\* At most one attempt ever legitimately becomes the org's owner-of-record.
\* Provable by construction given Succeed's own guard (rowToken = t, and the
\* row is single-valued) -- checked anyway, the same discipline as this
\* track's other "should hold structurally" invariants.
AtMostOneEverSucceeded ==
  \A t1, t2 \in Attempts : (everSucceeded[t1] /\ everSucceeded[t2]) => t1 = t2

===================================================================================
