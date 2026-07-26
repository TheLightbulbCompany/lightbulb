# Org billing: $50 base + $20 per invited seat

**Status:** design approved 2026-07-26
**Issues:** #1466 (supersedes its "seats derive from accepted memberships" scoping), #1461 (merged), #1465 (pulled in as a PR 2 prerequisite — see below)
**Not in scope:** #1467, #1468, #1470

## Goal

An org owner signs in, sees a real subscribe button, pays the correct amount,
and lands back ready for the provider step. Today they are charged $20 instead
of $70, and un-accepted invites are free.

## Principle

**No free members.** A seat is anyone who is a member *or* has been invited.
Inviting someone costs money from the moment the invitation is sent, not from
the moment it is accepted.

```
seats = accepted_members + pending_invitations
bill  = $50 base + seats × $20
```

A solo admin pays **$70/mo**. An admin who has invited three people pays
**$130/mo**, whether or not any of them have accepted yet.

### Why this formula and not two counters

Acceptance moves a person from `pending` to `member`, so the total is
unchanged and **the bill does not move when someone accepts**. Every billing
event is an explicit admin action:

| Action | members | pending | seats | bill |
|---|---|---|---|---|
| Invite sent | — | +1 | +1 | up |
| Invite accepted | +1 | −1 | — | **unchanged** |
| Invite revoked | — | −1 | −1 | down |
| Invite expired | — | −1 | −1 | down |
| Member removed | −1 | — | −1 | down |

Counting `status=pending` only is what makes the expired row work, and it is
robust regardless of Clerk's expiry policy — an invitation that stops being
`pending` for any reason stops being billed.

### The cost of this principle

An invitation sent to a typo'd address bills $20/mo until someone revokes it.
That is the accepted trade for "no free members", but it means **revocation is
the safety valve, not polish** — hence PR 2. Until PR 2 ships, a bad invite can
only be cleared from the Clerk dashboard.

## Current state

Verified in code on 2026-07-26, not taken from the issue.

**Seats today derive from accepted memberships only.**
`clerk_admin.get_organization_member_count` reads the Clerk org's
`members_count`, falling back to the memberships endpoint's `total_count`.
Both count memberships, never invitations.

**`sync_team_seat_quantity` (`billing_service.py:382`) is correct and stays.**
Single writer, absolute set with proration in both directions, and it refuses
to act on a Clerk read of `0` so an outage cannot zero a live subscription.
Only its *source of truth* changes.

**There is no base price.** `_build_subscription_checkout:331` hardcodes
`line_items=[{single item}]`, and `create_team_seat_checkout` passes only
`STRIPE_TEAM_SEAT_PRICE_ID`.

**Seat sync only actually runs on the poll.** Of the three call sites —
`routers/webhooks.py:396` (membership webhook), `:507` (org deleted), and
`routers/container.py:187` (opportunistic, on the org `/container/status`
poll) — the first two have **never fired**: Clerk has no webhook endpoint
configured in either environment (#1465). Treat them as dead until that
issue ships.

**There is no removal path in our API.** `routers/orgs.py` exposes exactly one
route, `create_invitation`. No revoke, no remove-member.

## The `items[0]` hazard

This ships in the same PR as the base price. It is not a follow-up.

`stripe_webhook_handlers.py:756` reads `items[0]` and stores its id as
`subscription_item_id` and its quantity as `seat_count`. Stripe does not
guarantee line-item order. Once a subscription has two items, if the base
lands at index 0 then `sync_team_seat_quantity` starts driving **the base
item's quantity** — a 4-member org becomes `$50 × 4 + $20 = $220/mo` instead
of `$50 + $20 × 4 = $130/mo`.

Fix: select the seat item **by price ID** (`STRIPE_TEAM_SEAT_PRICE_ID`), never
by position. Same treatment at `:897`.

`billing_service.py:530`'s Ultra guard uses `len(items_data) > 1` to mean "this
is a team sub". That stays *correct* with two items but only by coincidence;
make the predicate explicit while in the file.

## Stripe prices

Audited live on 2026-07-26 via the keys in Secrets Manager.

| Env | Base $50/mo | Seat $20/mo | Product |
|---|---|---|---|
| prod (live) | `price_1TxGYyI54BysGS3rcf0lxymj` ✅ exists | `price_1TrtEtI54BysGS3rql640tph` | `Isol8 Teams (per seat)` |
| dev (test) | **must be created** | `price_1TrsyoI54BysGS3r2t1YOC5f` | `Isol8 Teams (per seat)` |

The prod base price was created ad-hoc without a spec (#1466 flagged it as
unvalidated). It has been checked and is correct: live, active, $50.00 USD,
`recurring` / `licensed` / every 1 month, on the same product as the seat
price. **Adopt it.**

The only Stripe write required is one test-mode $50/mo licensed monthly price
on the test-mode `Isol8 Teams (per seat)` product, so dev is testable.

### Prices live in Stripe; only the ID is wired

Nothing in this repo creates a Stripe price. The price object is created once
via the Stripe API. What travels through the codebase is only its **ID**,
following the identical path the flat, seat, and Ultra prices already use:

```
GH secret  STRIPE_TEAM_BASE_PRICE_ID_{DEV,PROD}
  → app.ts             references the secret into the deploy workflow env
  → service-stack.ts   reads process.env, sets the container env var per env
  → config.py          backend reads it, names it at Stripe checkout
```

So "wire the price" is three one-line additions plus two `gh secret set` calls.

## PR 1 — correct billing

Unblocks the founder org signup.

1. **Create** the dev test-mode base price. **Wire**
   `STRIPE_TEAM_BASE_PRICE_ID` as a `_DEV`/`_PROD` GitHub-secret pair through
   `app.ts` → `service-stack.ts` → `config.py`, mirroring
   `STRIPE_TEAM_SEAT_PRICE_ID` exactly. Add display constant
   `TEAM_BASE_PRICE_USD = 50` beside the existing `TEAM_SEAT_PRICE_USD = 20`.

2. **Checkout emits two line items.** `_build_subscription_checkout` accepts a
   `line_items` list instead of a `price_id`/`quantity` pair. Team checkout
   passes `[{base, 1}, {seat, N}]`; personal and Ultra pass a single element
   and are behaviourally unchanged. Metadata, tax, and expiry untouched.

3. **Webhook selects the seat item by price ID** at both `:756` and `:897`;
   make the Ultra guard's team predicate explicit.

4. **Seat source becomes members + pending.** New
   `clerk_admin.get_organization_seat_count(org_id)` returning
   `members_count + pending_invitations_total`, reading
   `GET /organizations/{org_id}/invitations?status=pending` for the second
   term — same defensive posture as the existing helpers (returns the member
   count alone if the invitations read fails, never a partial that would
   under-bill into a downgrade). `sync_team_seat_quantity` and
   `routers/billing.py` both switch to it.

5. **Sync on invite.** `routers/orgs.py:create_invitation` calls
   `sync_team_seat_quantity` after a successful Clerk invite, so the bill moves
   at the moment of the admin's action rather than on the next poll.

6. **Display the math.** `/billing/account` returns `team_base_price_usd`;
   `TeamBillingCard` renders `$50 + N × $20 = $X/mo` instead of `N × $20`, and
   distinguishes joined from invited in that count. Swap the card's
   `#1c1917`/`#6b655e`/`#b63025` literals for the `--ink`/`--mute`/`--danger`
   tokens the rest of that screen uses (the one non-blocking finding on #1461).

### Migration

None. Existing team subscriptions keep their single seat item and their stored
`subscription_item_id` stays valid. If the founder org already holds a
seat-only $20 subscription, cancel and resubscribe it — writing a migration for
one row under our own control is waste.

## PR 2 — Clerk-native team management

What makes the invite model safe to point at anyone but the founder.

### Use Clerk for the surface, keep invites ours

Clerk is already the org system of record — `<CreateOrganization>`,
`useOrganization`, and `useOrganizationList` are in use. There is no
`<OrganizationProfile />` anywhere, so the member list, roles, revoke, and
remove are all unbuilt. Build them on Clerk rather than on a bespoke backend
API.

**One deliberate exception: invitations stay on our endpoint.**
`InviteTeammatesStep.tsx:37` posts to `/orgs/{org_id}/invitations` because
`routers/orgs.py` enforces Gate A — it refuses to invite anyone already
holding an active personal subscription (`_has_active_personal_tenancy`).
Clerk's own invite UI calls Clerk directly and would bypass that gate,
breaking the one-org-per-user tenancy invariant. Any Clerk surface adopted
here must not expose its native invite affordance.

### The consequence: billing sync must move to webhooks

A Clerk-native remove or revoke — via `<OrganizationProfile />` or via
`useOrganization()` membership `.destroy()` / invitation `.revoke()` — writes
to **Clerk, not to us**. Our billing learns about it only on the next org
`/container/status` poll. For a model whose whole principle is "no free
members", a seat that keeps billing after removal until someone happens to
open a tab is not acceptable.

So Clerk webhooks become the sync mechanism, which makes **#1465 a hard
prerequisite for this PR**: pointing Clerk at `/api/v1/webhooks/clerk` while
it accepts unsigned requests is precisely the P0 — an unauthenticated caller
could POST `organization.deleted` and cancel an org's subscription. Its CDK
wiring is already drafted in the uncommitted `feat/event-triggered-provisioning`
working tree and can be salvaged.

### Scope

1. **#1465 first:** wire `CLERK_WEBHOOK_SECRET` through Secrets Manager +
   `auth-stack.ts` + `service-stack.ts`; replace the inline fail-open
   `_verify_svix_signature` with the shared fail-closed
   `core/crypto/svix.verify_svix_signature`; fail closed in every non-local
   environment. Then configure the endpoint in the Clerk dashboard.
2. **Handle invitation events.** Only membership events are handled today
   (`webhooks.py:396`). Add `organizationInvitation.created` / `.revoked` /
   `.accepted`, each calling `sync_team_seat_quantity`. The dead membership
   handlers become live in the same step.
3. **Member management UI** on Clerk's components/hooks, with the native
   invite affordance suppressed, showing joined and invited separately with
   each row's cost so the bill is legible before it arrives.
4. **Keep the poll reconciler.** Webhooks are the fast path; the poll stays
   as the backstop, exactly as it does for provisioning.

## Testing

Unit coverage for: the two-line-item checkout shape; seat-item selection by
price ID **with the base deliberately placed at `items[0]`** (the regression
guard for the $220 bug); the seat formula across all five transitions in the
table above; the invitations-read-failure fallback; and the card's rendered
total.

End to end on dev: create a Clerk org, subscribe, confirm Stripe shows two line
items with the seat item — not the base — carrying quantity, invite someone,
confirm the quantity moves without the base item changing.

## Open question

Whether Clerk organization invitations expire on their own. If they do, a
forgotten invite self-heals out of the seat count; if not, only revocation
clears it. Filtering on `status=pending` is correct either way, so this
changes the urgency of PR 2, not the design.
