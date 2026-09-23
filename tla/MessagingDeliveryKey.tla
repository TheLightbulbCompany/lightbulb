------------------------- MODULE MessagingDeliveryKey -------------------------
(***************************************************************************)
(* Live-delivery revocation for one messaging room.                        *)
(*                                                                         *)
(* Members receive new messages over an AppSync channel named by the       *)
(* room's deliveryKey.  AppSync cannot close an open subscription, so      *)
(* removing a human rotates the key in the same DynamoDB transaction that  *)
(* deletes their MEMBER row.  This module checks that the rotation really  *)
(* revokes: over the room channel a client only ever receives messages     *)
(* that were written while it was a member.                                *)
(*                                                                         *)
(* Mirrors (main @ ed4e23f0f):                                             *)
(*   messaging_repo.create_message, remove_member       the two transactions*)
(*   messaging.create_human_message, _remove_member_with_notices           *)
(*   messaging-projector _project_message, _project_live, _get_item        *)
(*   packages/shared useRooms: room.access.removed, room.subscription.rotated*)
(* AppSync is a set of publishes, so it may duplicate, drop and reorder.   *)
(* Out of scope: agents and WORK# fanout, HTTP history paging, mobile      *)
(* push, re-adding a removed member, message edits.                        *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS
  Humans,             \* every human who is ever in the room
  Owner,              \* the room owner: the service refuses to remove them
  MaxSeq,             \* how many messages the model may write
  MemberCheckInTxn,   \* TRUE if create_message's transaction conditions on the author's MEMBER row
  ConsistentMetaRead  \* TRUE if the projector reads META with ConsistentRead

ASSUME /\ Owner \in Humans
       /\ MaxSeq \in Nat
       /\ MemberCheckInTxn \in BOOLEAN
       /\ ConsistentMetaRead \in BOOLEAN

NoKey == 0  \* subs[u] = NoKey: the client holds no room subscription

VARIABLES
  members,   \* the MEMBER#human rows
  key,       \* META.deliveryKey as a rotation counter (uuid4 in code: fresh on every rotation)
  lastSeq,   \* META.lastSeq
  msgs,      \* MSG rows [seq, author, members]; members = the MEMBER rows when the write committed
  inflight,  \* senders past require_room_member whose transaction has not committed
  pending,   \* stream records the projector has not processed yet
  published, \* room-channel publishes [key, seq]
  inbox,     \* private-stream notices per human
  subs,      \* the room-channel key each client is subscribed to
  live       \* message seqs each client received over the room channel

vars == <<members, key, lastSeq, msgs, inflight, pending, published, inbox, subs, live>>

Notice       == [kind: {"removed", "rotated"}, key: Nat]
MsgRecord    == [type: {"msg"}, seq: 1..MaxSeq]
NoticeRecord == [type: {"notice"}, user: Humans, notice: Notice]

TypeOK ==
  /\ members \subseteq Humans
  /\ key \in Nat /\ lastSeq \in 0..MaxSeq
  /\ msgs \subseteq [seq: 1..MaxSeq, author: Humans, members: SUBSET Humans]
  /\ inflight \subseteq Humans
  /\ pending \subseteq MsgRecord \cup NoticeRecord
  /\ published \subseteq [key: Nat, seq: 1..MaxSeq]
  /\ inbox \in [Humans -> SUBSET Notice]
  /\ subs \in [Humans -> Nat]
  /\ live \in [Humans -> SUBSET (1..MaxSeq)]

Init ==
  /\ members = Humans
  /\ key = 1 /\ lastSeq = 0
  /\ msgs = {} /\ inflight = {} /\ pending = {} /\ published = {}
  /\ inbox = [u \in Humans |-> {}]
  /\ subs = [u \in Humans |-> 1]   \* every member has fetched room detail once
  /\ live = [u \in Humans |-> {}]

-----------------------------------------------------------------------------
(* Sending.  create_human_message checks membership (require_room_member),  *)
(* then create_message's transaction conditions on META.lastSeq only.  Its  *)
(* retry loop on a lastSeq race converges, so the commit is one step here.  *)

BeginSend(u) ==
  /\ u \in members /\ u \notin inflight /\ lastSeq < MaxSeq
  /\ inflight' = inflight \cup {u}
  /\ UNCHANGED <<members, key, lastSeq, msgs, pending, published, inbox, subs, live>>

CommitSend(u) ==
  /\ u \in inflight /\ lastSeq < MaxSeq
  /\ (MemberCheckInTxn => u \in members)
  /\ lastSeq' = lastSeq + 1
  /\ msgs' = msgs \cup {[seq |-> lastSeq + 1, author |-> u, members |-> members]}
  /\ pending' = pending \cup {[type |-> "msg", seq |-> lastSeq + 1]}
  /\ inflight' = inflight \ {u}
  /\ UNCHANGED <<members, key, published, inbox, subs, live>>

AbandonSend(u) ==  \* the request fails or the retry budget runs out before commit
  /\ u \in inflight
  /\ inflight' = inflight \ {u}
  /\ UNCHANGED <<members, key, lastSeq, msgs, pending, published, inbox, subs, live>>

-----------------------------------------------------------------------------
(* Removal.  _remove_member_with_notices builds one LIVE notice per human   *)
(* member; remove_member commits them together with the MEMBER delete and   *)
(* the new deliveryKey in ONE transaction.                                  *)

NoticeFor(h, removed, newKey) ==
  [type |-> "notice", user |-> h,
   notice |-> [kind |-> IF h = removed THEN "removed" ELSE "rotated", key |-> newKey]]

Remove(u) ==
  /\ u \in members /\ u /= Owner
  /\ members' = members \ {u}
  /\ key' = key + 1
  /\ pending' = pending \cup {NoticeFor(h, u, key + 1) : h \in members}
  /\ UNCHANGED <<lastSeq, msgs, inflight, published, inbox, subs, live>>

-----------------------------------------------------------------------------
(* Projection.  The DynamoDB stream feeds the messaging-projector Lambda,   *)
(* which reads the room's META at publish time to choose the channel.       *)

ProjectMsg(r) ==
  /\ r \in pending /\ r.type = "msg"
  /\ \E k \in (IF ConsistentMetaRead THEN {key} ELSE 1..key) :
       published' = published \cup {[key |-> k, seq |-> r.seq]}
  /\ pending' = pending \ {r}
  /\ UNCHANGED <<members, key, lastSeq, msgs, inflight, inbox, subs, live>>

ProjectNotice(r) ==
  /\ r \in pending /\ r.type = "notice"
  /\ inbox' = [inbox EXCEPT ![r.user] = @ \cup {r.notice}]
  /\ pending' = pending \ {r}
  /\ UNCHANGED <<members, key, lastSeq, msgs, inflight, published, subs, live>>

-----------------------------------------------------------------------------
(* Clients.  Notices apply in any order, any number of times.  The real      *)
(* client's roomRevision guard only ever applies FEWER of them, so this      *)
(* over-approximates what it can do; a property that holds here holds there. *)

Receive(u, p) ==  \* room.message.created arrives on the channel u is subscribed to
  /\ p \in published /\ p.key = subs[u] /\ subs[u] /= NoKey
  /\ live' = [live EXCEPT ![u] = @ \cup {p.seq}]
  /\ UNCHANGED <<members, key, lastSeq, msgs, inflight, pending, published, inbox, subs>>

ApplyRemoved(u, n) ==  \* room.access.removed -> removeActiveAccess()
  /\ n \in inbox[u] /\ n.kind = "removed"
  /\ subs' = [subs EXCEPT ![u] = NoKey]
  /\ UNCHANGED <<members, key, lastSeq, msgs, inflight, pending, published, inbox, live>>

ApplyRotated(u, n) ==  \* room.subscription.rotated -> setDeliveryOverride
  /\ n \in inbox[u] /\ n.kind = "rotated"
  /\ subs' = [subs EXCEPT ![u] = n.key]
  /\ UNCHANGED <<members, key, lastSeq, msgs, inflight, pending, published, inbox, live>>

Refetch(u) ==  \* getRoom (mutateRoom, the 60 s refresh): membership-checked, carries the live key
  /\ subs' = [subs EXCEPT ![u] = IF u \in members THEN key ELSE NoKey]
  /\ UNCHANGED <<members, key, lastSeq, msgs, inflight, pending, published, inbox, live>>

-----------------------------------------------------------------------------
Next ==
  \/ \E u \in Humans : BeginSend(u) \/ CommitSend(u) \/ AbandonSend(u) \/ Remove(u) \/ Refetch(u)
  \/ \E r \in pending : ProjectMsg(r) \/ ProjectNotice(r)
  \/ \E u \in Humans, p \in published : Receive(u, p)
  \/ \E u \in Humans : \E n \in inbox[u] : ApplyRemoved(u, n) \/ ApplyRotated(u, n)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* What must hold. *)

\* The security boundary: nothing reaches a client over the room channel
\* unless the client was a member when that message was written.
NoLeak ==
  \A u \in Humans : \A m \in msgs : m.seq \in live[u] => u \in m.members

\* The mechanism behind it: once removed, a client never holds the live key.
RevokedNeverHoldsLiveKey ==
  \A u \in Humans \ members : subs[u] /= key

\* Every message's author was a member when its write committed.
\* Needs MemberCheckInTxn = TRUE; the code today has no such condition.
AuthorWasMember ==
  \A m \in msgs : m.author \in m.members

=============================================================================
