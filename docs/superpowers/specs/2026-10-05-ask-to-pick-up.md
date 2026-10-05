# Ask to pick up

Status: draft, 2026-10-05. Builds on session pick-up (`2026-09-27-session-pickup-design.md`,
#3327–#3330) and the session sharing mod (`2026-10-04-session-sharing-mod.md`, #3729).

## 1. Problem

Pick up already works: on a teammate's Mac it restores a Claude Code conversation and opens it in a
fresh git worktree that carries the owner's unpushed commits, edits and untracked files. But the
button only appears after the owner turns on "Allow pick up" for that one session (Details →
Access), and that switch is off by default. A teammate reading a transcript sees no way to ask for
it, so almost nobody knows the feature exists.

## 2. Decisions

1. **Every teammate reading a Claude session sees a pick-up button.** Pick-up allowed and a
   snapshot ready: **Pick up** (unchanged). Otherwise: **Ask to pick up**, then "Requested", or
   "<Owner> declined" (they may ask again), the same pattern as Ask to join.
2. **The owner answers where they are:** a push notification and an activity item, the session view
   on web and phone, and the terminal band of the sharing mod ("Mira wants to pick this up ·
   [Allow] [Decline]").
3. **Allow turns pick-up on for the session**, exactly as the owner's switch does (`set_pickup`):
   the permission stays per session, not per person, so every workspace member can then pick it up.
   The requester is told "<Owner> let you pick up <title>"; their button reads "Preparing…" until
   the snapshot is ready, as it does today.
4. **Claude Code only**, like pick-up itself. An ended session can be asked for: its snapshot is
   kept after it ends.
5. **Terminal decisions follow the join rules.** A pending pick-up request also freezes remote
   typing on the owner's Mac, because Allow shares the transcript and code. The Mac answers through
   the existing door, `POST /messaging/native-session-decisions`, with two more decisions:
   `allow_pickup`, `decline_pickup`.

## 3. Contract

### 3.1 Backend

- Session row: `pickupRequests: {<userId>: {state: "pending"|"declined", requestId, createdAt}}`.
  Allow removes the entry. A request made while pick-up is already on changes nothing. Turning
  pick-up on by any route clears every pending entry.
- `POST /messaging/sessions/{id}/pickup-requests` (any human member, not the owner, Claude only) →
  public session. Repeat while pending is a no-op; after a decline it asks again.
- `POST /messaging/sessions/{id}/pickup-requests/{userId}` `{decision: "allow"|"decline",
  requestId}` (owner only) → public session. Allow runs `set_pickup(…, True)`.
- Public session: the requester sees `pickupRequest` ("pending"|"declined"); the owner sees `pendingPickups: [userId]` and `pendingPickupIds: {userId:
  requestId}`.
- Notifications, as join requests: an activity row (subtype `pickup_request`) and
  `workspace.session.attention` + push to the owner; the answer goes to the requester
  ("<Owner> let you pick up <title>" / "<Owner> declined your request").
- Presence (§4.2 of the mod spec) gains `pickupRequests: [{userId, name, requestId}]`; a pick-up
  request is pushed like a join request; `inputPaused` also counts pending pick-up requests.
- Device door: `decision` adds `allow_pickup` and `decline_pickup` (with `userId` and `requestId`).

### 3.2 Desktop

`presence::Entry` gains `pickup_requests`; `frozen()` is true while either list is non-empty;
`mod.sock` `/v1/state` returns `pickupRequests`; `/v1/decide` accepts the two new decisions under the
same "still pending here" rule.

### 3.3 Mod

The band shows a pick-up request row like a join request row: `Mira Shah wants to pick this up`
`[Allow]` `[Decline]`. Join requests show first.

### 3.4 Web and phone

- Web `LocalSessionPanel`: the button states in §2.1 for a non-owner on a Claude session; the
  owner's request rows beside `JoinRequests`.
- Phone: the owner's request rows beside `JoinRequests`. No requester button: pick-up runs on a
  Mac only.

## 4. Not in this work

Per-person pick-up permission; pick-up for Codex; picking up on the phone.
