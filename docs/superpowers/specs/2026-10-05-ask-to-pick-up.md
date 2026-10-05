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
   typing on the owner's Mac, because Allow shares the transcript and code. A pick-up request rides
   the join-request plumbing as a variant (`kind: "pickup"`), so the Mac, the mod and the activity
   feed need no new paths: the Mac answers through the existing door with `allow` / `decline`, and
   the backend tells the two apart by which map holds the request id.

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
- Notifications, as join requests: an activity row with subtype `join_request` and
  `request: "pickup"` (title "<Name> wants to pick up <title>", its own reason line; activity
  item id `pickup:<sid>:<user>:<requestId>`, Allow/Decline routed to the pick-up decision), and
  `workspace.session.attention` state `join_request` + push to the owner. The answer goes to the
  requester as `join_allowed`/`join_declined` attention ("<Owner> let you pick up <title>" /
  "<Owner> declined your request to pick up <title>").
- Presence entries' `joinRequests` items gain `kind: "join" | "pickup"`; pick-up requests are
  included and pushed like join requests; `inputPaused` counts both.
- Device door (`native-session-decisions`): `allow`/`decline` route to the pick-up decision when
  `requestId` matches the user's pending pick-up request, else to the join decision.

### 3.2 Desktop

`presence::JoinRequest` gains `kind` (serde default `"join"`), passed through to `/v1/state`.
Nothing else changes: the freeze, the "still pending here" check and the local update after a
decision all key on the request id.

### 3.3 Mod

The band's request row reads by kind: `Mira Shah asked to join` or `Mira Shah wants to pick this
up`, `[Allow]` `[Decline]`.

### 3.4 Web and phone

- Web `LocalSessionPanel`: the button states in §2.1 for a non-owner on a Claude session; the
  owner's request rows beside `JoinRequests`.
- Phone: the owner's request rows beside `JoinRequests`. No requester button: pick-up runs on a
  Mac only.

## 4. Not in this work

Per-person pick-up permission; pick-up for Codex; picking up on the phone.
