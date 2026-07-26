# Explicit tenancy: stop inferring "is this an org?" from the owner_id's spelling

**Status:** design approved 2026-07-26
**Enables:** personal → org conversion (separate PR)
**Not in scope:** the conversion flow itself, the Settings entry point, Ultra-tier conversion policy

## The problem

`owner_id` currently answers two unrelated questions:

1. **Which container / workspace / billing row?** — a key.
2. **Is this tenancy an org or a personal account?** — a fact about the account.

Question 2 is answered by reading the *text* of the answer to question 1:

```python
def is_org_owner_id(owner_id: str) -> bool:     # core/auth.py:107
    return owner_id.startswith("org_")
```

That works only because, today, every org's container is keyed by its Clerk
`org_id`. It stops working the moment an org's container is keyed by anything
else — which is exactly what personal → org conversion requires.

## Why conversion needs this

The intended conversion model is **attach an org to the existing container**,
not move the container to the org. The converting user stays the owner
permanently; `owner_id` remains their `user_...` id; nothing migrates — not the
11 owner-keyed DynamoDB tables, not the EFS tree at `/users/{owner_id}/`, not
the ECS service `openclaw-{user_id}-{hash}`, not the per-user EFS access point,
not the Paperclip company, not the Bedrock gate token.

Under that model a real org has an `owner_id` beginning `user_`, so every
prefix check answers **"personal"** for something that is genuinely an org.

The decisive one is `routers/billing.py:638`:

```python
if not auth.is_org_context or not is_org_owner_id(owner_id):
    raise HTTPException(400, "team-checkout requires an organization context")
```

A converted org **could not buy a team subscription at all** — an org, with
members, permanently stuck on personal billing, because the endpoint that sells
team plans checks how its owner id is spelled.

## The key finding: only 5 of the 14 call sites are about tenancy

`is_org_owner_id` appears on 16 lines: **14 call sites** plus 2 thin
module-local delegating helpers (`friday_identity.py:667`,
`paperclip_openclaw_pairing.py:55`).

Those 14 sites ask **two different questions**, and only one of them is wrong
under conversion — 5 are tenancy, 9 are about the id's shape. Conflating them
is what made this look like a 14-site refactor; it is five.

### Category A — genuinely TENANCY. These must change.

| Site | What breaks for a converted org |
|---|---|
| `routers/billing.py:139` | `/billing/account` skips seat count and team pricing → the org sees personal billing |
| `routers/billing.py:638` | **Cannot buy a team subscription.** The blocker. |
| `routers/container.py:184` | Seat sync never fires → invited members never bill |
| `core/services/owner_facts.py:88` | Writes `owner_kind: "personal"` into the container's facts file → the agent itself believes it is a personal account |
| `core/services/friday_identity.py:832` | Friday is seeded down the personal path, so it never learns the org name |

`friday_identity.py:832` needs **both** answers: that the account is an org
(tenancy) *and* the Clerk `org_id` to call `clerk_admin.get_organization` with
(shape). It is the only site needing both, and the only one where a converted
org must supply an org id distinct from its `owner_id`.

### Category B — genuinely about the CLERK ID's SHAPE. These stay as they are.

These ask "is this string a Clerk organization id?", which remains a real and
correctly-answered question. Changing them would be a bug.

| Site | Why the prefix check is correct |
|---|---|
| `routers/admin.py:904`, `core/services/account_wipe.py:206` | `clerk_admin.get_user` 404s on an `org_...` id — the wipe flow refuses ids of that shape. Its own comment says so. |
| `core/repositories/paperclip_repo.py:70` | Chooses between two ids by shape |
| `core/repositories/paperclip_repo.py:1081` | Selects the `by-org-id` GSI vs a base-table `get` — **where the row physically lives** |
| `core/services/paperclip_openclaw_pairing.py:83,178` | Same GSI-vs-get distinction |
| `core/services/paperclip_provisioning.py:522` | Passes a real Clerk org id through to Paperclip |
| `core/services/user_provision.py:271` | Skips trial-row creation for org-shaped ids |
| `routers/oauth.py:164` | Gates on the id the OAuth session was bound to |

**This is load-bearing for conversion.** A converted org's Paperclip row was
written down the personal path and lives in the base table, not the GSI — so
those checks continue to route it correctly *because* they read the shape. The
Paperclip company keeps working with no migration and no change.

## The mechanism: a field that already exists

`billing_accounts` already carries `owner_type` — `"personal"` or `"org"` —
written at `billing_repo.py:327` (`create_if_not_exists`) and `:368`
(`create_trial_local_if_absent`). It is written today and **never read for a
tenancy decision**. The five Category-A sites are a shortcut around a field
that is already there.

Introduce one predicate, and route every Category-A site through it:

```python
def is_org_tenancy(account: Mapping[str, Any] | None) -> bool:
    """Whether this billing account is an organization.

    Reads the recorded owner_type. Does NOT infer tenancy from how owner_id is
    spelled — a converted org keeps its founder's user_... owner_id forever.
    """
```

It belongs beside `has_always_on_entitlement` in `core/services/provision_gate.py`,
which already exists as the single home for predicates every site must agree on.

### Backfill

`owner_type` is written on row creation, so rows predating it may lack the
field. The predicate must therefore treat a **missing** `owner_type` as
"fall back to the id's shape" rather than defaulting to personal — otherwise
an existing org with no `owner_type` silently loses seat billing. Prod
currently holds exactly one billing row (personal, `trial_local`), so the
practical exposure is nil; the fallback exists so dev and any future restore
cannot be wrong.

## Behaviour change: none, today

This is the property that makes it safe to ship alone. Every org today has
`owner_id` starting with `org_` **and** `owner_type == "org"`, so both the old
and new predicates return identical answers for every existing row. The change
is inert until conversion exists.

Verify rather than assume: a test asserting old and new agree across every
combination of (`owner_id` shape × `owner_type` value × missing field).

## Sites needing a billing row they don't currently have

Three Category-A sites have no `account` in scope and must obtain one:

- `routers/container.py:184` — on the `/container/status` poll. This path is
  hot (~every 3s while pre-ready), so it must not add a DynamoDB read per poll.
  Fold the tenancy check into `sync_team_seat_quantity`, which **already reads
  the billing row as its first action** and already returns early for non-team
  owners. The router then calls it unconditionally and the read is not
  duplicated.
- `core/services/owner_facts.py:88` — already assembles account state to write
  the facts file; pass the tenancy in rather than re-deriving it.
- `core/services/friday_identity.py:832` — reached during provisioning, where
  the billing row is available upstream; thread it in.

`routers/billing.py:139` and `:638` already have the account in hand.

## Testing

- The equivalence test above (old predicate ≡ new predicate for every existing
  row shape), which is what licenses shipping this on its own.
- Per Category-A site: an org whose `owner_id` starts with `user_` and whose
  `owner_type` is `"org"` is treated as an org — the converted-org case, which
  cannot occur yet and is precisely what this enables.
- A regression guard that Category-B sites still key on shape: a converted org
  (`user_` owner, `owner_type: "org"`) must still resolve its Paperclip row
  through the base-table `get`, not the `by-org-id` GSI.
- `routers/container.py`: assert the poll does not add a DynamoDB read.

## Follow-up (separate PR, not designed here)

The Settings entry point and the conversion flow itself: create the Clerk org,
flip `owner_type`, and modify the existing Stripe subscription in place —
swapping the flat $50 item for base + seat items on the **same** subscription,
so Stripe prorates and there is no cancel-and-resubscribe window.

Open question deferred with it: an `ultra_pro` personal owner ($150/mo) cannot
become a team — `create_ultra_upgrade` refuses per-seat subs outright, so
conversion would be a silent downgrade. Block it with a clear message, or
allow the drop to standard team pricing?
