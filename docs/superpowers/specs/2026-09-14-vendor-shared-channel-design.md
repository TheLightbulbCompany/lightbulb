# Vendor shared channel — design

**Status:** approved design, awaiting implementation plan
**Baseline:** `origin/main` at `9c427190f`
**Owner ruling (2026-09-14):** vendor channel first; members are every human of
both workspaces plus the customer's CEO agent; approach A (foreign member rows).

## 1. Decision

Every customer workspace gets one channel shared with the Lightbulb workspace,
created by the platform, so Lightbulb staff can talk to each customer inside
the customer's own app. It behaves like a Slack Connect channel from the
user's seat: one conversation, both companies' people in it, a visible
"shared" marker.

Under the hood it is **one ordinary customer-owned room**. Nothing is
mirrored, bridged, or federated. Lightbulb humans are members of that room
whose membership rows point at their home workspace instead of the room's.
Everything else in the messaging plane (the container that runs the agent,
the EFS that holds attachments, the AppSync channel, the run ledger, the
worker) keeps treating the room as the customer's.

Non-goals for this release: general any-two-workspaces sharing and its invite
handshake; Lightbulb agents in customer channels; disconnecting the channel;
more than one shared channel per customer; per-workspace room names.

## 2. Membership rule

| Side | Who | Role | Listen mode |
| --- | --- | --- | --- |
| Customer | the workspace owner | `owner` | — |
| Customer | every other human in the Clerk org | `member` | — |
| Customer | the CEO from `starter_agent_ids["ceo"]` | `member` | `mentions` |
| Lightbulb | every human in the vendor Clerk org | `member` | — |

"Everyone" is a standing rule, not a one-time seed: a person who joins either
org is added, a person who leaves either org is removed with the normal
delivery-key rotation. Humans cannot be added, removed, or leave through the
room UI. Mute is the escape hatch. Customer admins may still change the CEO's
listen mode.

The CEO sits in `mentions`, not `active`, so it does not answer human-to-human
support traffic. A legacy roster with no resolvable CEO id gets a humans-only
room; that is not an error.

## 3. Data model

No new table, no migration. Existing rows already satisfy the new reading.

| Row | Change |
| --- | --- |
| `ROOM#<room>` / `MEMBER#human#<id>` | New `homeOwnerId`. The `by-member` GSI key `memberKey` becomes `OWNER#<homeOwnerId>#human#<id>`. Readers take a missing `homeOwnerId` as `ownerId`, which is exactly what every existing row's `memberKey` encodes. Agent rows keep home = room owner. |
| `ROOM#<room>` / `META` | Unchanged. |

There is deliberately **no** `managed`/`vendor` attribute on the room. The
vendor room id is deterministic:

```python
def vendor_room_id(owner_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-vendor-room|{owner_id}"))

def is_vendor_room(meta: dict) -> bool:
    return meta["roomId"] == vendor_room_id(meta["ownerId"])
```

The room is named `lightbulb-<customer org slug>` on both sides. That one
name tells the customer whose channel it is and tells Lightbulb staff which
customer it is, so no per-side label, `customerName`, or auto-section exists.
`PATCH /rooms/{id}` refuses a `name` change on the vendor room to keep the
convention true; `topic` stays editable.

There is no catalog of vendor rooms. The one reader that needs "every vendor
room" (the vendor-side join webhook, §4) walks the containers table and
computes each org owner's room id.

## 4. Creation and lifecycle

### Seed

`ensure_vendor_room(owner_id)` lives beside `ensure_default_room` in
`core/services/messaging.py`. Its seat is `core/services/user_provision.py`
`ensure_user_ready`, immediately after the `_reconcile_owner_seed` try/except
and only in org context. That point is reached on every `/container/status`
poll of a running org container by any member, admin or not, which is how
existing customers pick the room up with no backfill script. (The Paperclip
seed block itself is the wrong seat: `_reconcile_owner_seed` returns before
it for every org that already has a company.)

Preconditions: `settings.VENDOR_ORG_ID` is set, `owner_id` is an org, and
`owner_id != VENDOR_ORG_ID`. If `get_room(vendor_room_id(owner_id))` returns a
row, return. That is the per-poll cost: one consistent read, no Clerk calls.
Creation also waits until the owner's Paperclip company row is `active`, so a
fresh workspace gets its CEO in the room rather than a humans-only room that
never heals; an active company with no `starter_agent_ids["ceo"]` is a legacy
roster and gets the humans-only room.

First creation:

1. `clerk_admin.list_organization_members(owner_id)` and
   `clerk_admin.list_organization_members(VENDOR_ORG_ID)`.
2. `clerk_admin.get_organization(owner_id)` for the slug and `created_by`:
   name `lightbulb-<slug>` (fall back to `lightbulb` if the slug is empty),
   room `owner` = `created_by` (fall back to the first `org:admin`).
3. Roster per §2. Customer humans carry `homeOwnerId = owner_id`, vendor
   humans `homeOwnerId = VENDOR_ORG_ID`. The CEO is included only when the
   Paperclip company is `active` and `starter_agent_ids["ceo"]` resolves in
   `load_agent_roster(owner_id)`; its `mentionHandle` comes from
   `mention_handles(roster)` as today.
4. `messaging_repo.ensure_user_stream(home, user)` for every human, keyed on
   **their home**, so their private notices reach the user channel their
   client actually subscribes to.
5. `messaging_repo.create_room(...)` with `client_room_id =
   f"vendor-room-v1:{owner_id}"`, extended so each member dict may carry
   `homeOwnerId`. No seeded message.
6. `_emit_room_created(..., by="system")`.

Cap: `MAX_ROOM_MEMBERS` stays 32. Order is room owner, CEO, customer humans
sorted by user id, then vendor humans sorted by user id; the list is cut at
the cap and `messaging.vendor_room.capped` fires once.
`# ponytail: 32-member cap; raise MAX_ROOM_MEMBERS when a customer org exceeds ~25 humans`.

### Incremental membership (existing Clerk webhook)

- `organizationMembership.created` for a customer org → if
  `get_room(vendor_room_id(org))` exists, `ensure_user_stream(org, user)` then
  `messaging_repo.upsert_member(room, member with homeOwnerId=org)`.
- `organizationMembership.created` for the vendor org →
  `container_repo.scan_all()`, keep `is_org_owner_id` rows, and for each whose
  vendor room exists, add the user with `homeOwnerId = VENDOR_ORG_ID`.
  `# ponytail: full containers scan per vendor join; add a VENDORROOM# catalog row when org count makes this slow`.
- `organizationMembership.deleted` already calls `revoke_human_membership(org,
  user)`; after §5 it enumerates by home owner, so it finds shared rooms too
  and removes with the existing key rotation.

Both handlers are idempotent: `upsert_member` on an existing row is a no-op
update, and a missing room is a skip.

### Forbid rules

One helper, three callers:

```python
def _deny_vendor_human_change(meta: dict, kind: str) -> None:
    if kind == "human" and is_vendor_room(meta):
        raise ApiProblem("ROOM_FORBIDDEN", "Membership of this channel is managed by Lightbulb.")
```

Called from `upsert_member`, `remove_member`, and `leave_room`. `patch_room`
refuses `name` on the vendor room with the same code.

### Wipe and reprovision

Customer wipe deletes the room partition as today; the vendor humans' GSI
rows live in that partition, so nothing dangles. Reprovision recreates the
room on the next poll with the current rosters. Wiping or deleting the vendor
org is out of scope.

## 5. Authorization and delivery seams

Every seam is the same rule change: "room owner equals the caller's
workspace" becomes "the caller has a member row whose home equals the
caller's workspace".

| Where | Change |
| --- | --- |
| `messaging_repo._member_item`, `member_key`, `upsert_member` | Accept `homeOwnerId`; default it to the room owner; key `memberKey` on it. |
| `messaging_repo.list_rooms_for_member` | Drop the `meta.ownerId == owner_id` filter (the GSI key already selects by home); keep a belt-and-braces `member.homeOwnerId or member.ownerId == owner_id` check. |
| `messaging_repo.remove_member` | `live_events` entries carry their own `ownerId` (the human's home) into `_live_item`, instead of one room-level owner. |
| `messaging.require_room_member` | Missing room → `ROOM_NOT_FOUND`; missing member row → `ROOM_FORBIDDEN`; member home ≠ active workspace → `ROOM_NOT_FOUND`. Every room-scoped route inherits this. |
| `messaging.revoke_human_membership` | Home check instead of `meta.ownerId`. |
| `messaging._remove_member_with_notices` | `ensure_user_stream(home(human), …)` per human; pass that home as the notice's `ownerId`. |
| `messaging._resolved_member` | Unchanged for the public routes: humans added through the UI must still be in the room's org. The seed and webhook paths build member dicts directly with an explicit `homeOwnerId`. |
| `routers/messaging.py` attachments | `store_attachment` / `load_attachment` take `meta["ownerId"]` from `require_room_member`, not the caller's workspace, so a file a Lightbulb human posts lands in the customer's EFS where the customer can read it. |
| `apps/infra/lambda/messaging-projector/index.py` `_notify_members` | User-stream and reverse-mapping reads use `member.get("homeOwnerId") or ownerId`; the `member.ownerId == room owner` recheck stays (it is still true). Push follows for free. |
| `routers/webhooks.py` | The two membership handlers call the §4 functions. |

Untouched by design: `openclaw_work.py` (the only agent is the customer's, in
the customer's container), the run ledger and hook-token routes, the AppSync
handler (subscribe is already a member-row check with no org claim; publish is
by the customer's task role or the platform roles), typing, read cursors,
reactions, pins, prefs and sections (already keyed on the caller's workspace).

## 6. Public contract

`RoomSummary` and `RoomDetail` gain two fields; everything else is unchanged.

```ts
type RoomSummary = {
  // ...existing fields
  workspaceId: string; // the ROOM's opaque AppSync segment
  shared: boolean;     // true for the vendor room, on both sides
};
```

`workspaceId` is the segment already stamped on `META` by
`workspace_id_for_meta` (no extra read per room); `shared` is
`is_vendor_room(meta)`. New error uses: `ROOM_FORBIDDEN` from the forbid
rules; `ROOM_NOT_FOUND` when a member views a shared room from an org that is
not their home for it.

## 7. Clients

- Room channel path comes from the room, not the config:
  `apps/frontend/src/hooks/useLiveEvents.tsx` and
  `apps/mobile/src/providers/live-events.tsx` use
  `activeRoom.workspaceId ?? config.workspaceId`, so the mobile OTA is safe
  whether it lands before or after the backend.
- `RoomDirectoryRow` and `RoomHeader` (web) and the mobile row/header show a
  small "Shared" marker when `shared`, with the title "Shared between this
  workspace and Lightbulb". Same text on both sides; for the customer it is
  the disclosure that Lightbulb staff read the channel.
- `RoomDetailsPanel` hides add/remove for humans and the rename control when
  `shared`. The server guard is the security; the hiding is courtesy.
- No auto-section. Lightbulb staff can put their customer rooms in a custom
  section with the sections feature that already exists.

## 8. Configuration

`VENDOR_ORG_ID: str = ""` in `core/config.py`, threaded per environment from
`LightbulbStageProps` → `service-stack.ts` env map, the same shape as
`AGENT_EMAIL_DOMAIN`. Empty disables the feature entirely (no seed, no webhook
fan-out, `shared` never true). Prod value: the founder org
`org_3H3viGAfq2SXc9dIdcatmGXg8Td`. Dev value: the founder's dev org, set in
the same file by the implementer after `aws sso login` and a Clerk lookup.

## 9. Known limits, accepted

- A person in both orgs holds one member row with whichever home seeded
  first; from the other org's context the room is `ROOM_NOT_FOUND`.
- The name is captured at creation; an org slug change does not rename it.
- Lightbulb staff who are in N customer rooms make `GET /rooms` batch-read N
  extra `META` rows. Fine at founder scale.
- A vendor-org join fans out with one containers scan. See the `ponytail:`
  ceiling in §4.

## 10. Tests

One check per non-trivial path, in the existing suites:

- repo: `memberKey` on home; `list_rooms_for_member` returns a foreign room
  for its home and not for the room owner's org.
- service: `require_room_member` for a foreign member from home (ok), from
  the room's org (404), non-member (403); `ensure_vendor_room` idempotent,
  humans-only when no CEO, cap ordering; the forbid helper on add/remove/
  leave/rename; attachment routes use the room owner.
- webhooks: customer join adds with customer home; vendor join adds across
  two fake org owners; leave removes.
- projector: a foreign member is notified through their home stream.
- web + mobile jest: channel path per room; marker renders when `shared`.
- dev verification: dev `VENDOR_ORG_ID` = dev founder org; create a second
  dev org; channel appears on both sides; message each way; `@ceo` answered
  from the customer container; join-link redemption adds the new person; org
  leave removes them and rotates the key.

## 11. Rollout

Two PRs: backend + infra env first (safe alone: no client reads the new
fields yet), then web + mobile (safe alone: the fallback in §7). Dev deploys
on merge; prod is a `deploy.yml` dispatch. Existing customers get the channel
on the next `/container/status` poll by anyone in the org, once their
container is running, after the backend promotion. Mobile
merge ships an OTA.

Delete this file in the PR that ships the second half.
