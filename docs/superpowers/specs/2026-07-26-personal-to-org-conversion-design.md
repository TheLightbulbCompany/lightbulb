# Personal → org conversion

**Status:** design in progress 2026-07-26
**Depends on:** this branch's Ultra-for-teams work — `create_ultra_upgrade`
swaps only a team subscription's base item, leaving the seat item alone (see
"Ultra-tier owners" at the end).

## What a user gets

A **"Create a team"** entry point in Settings. A personal owner clicks it,
names the org, and their existing workspace — agents, files, chat history,
Paperclip company — comes with them. They pay $50 base + $20/seat and can
invite people.

Today there is **no button anywhere in the product**. The only "Create a team"
CTA lives on the logged-out marketing page and points at `/sign-up`. The
routing behind it already supports an existing signed-in user
(`useOnboardingGate.ts:102-105` says so explicitly); nothing reaches it.

## The model: the ORG owns the workspace

After conversion the container is keyed by `org_id`, exactly like an org-first
signup. `owner_id` for that workspace becomes `org_...` everywhere.

The alternative — keep the founder's `user_...` key and record tenancy in a
separate field — was considered and rejected. It made conversion nearly free,
but left DynamoDB rows and ECS services reading `user_...` for organizations,
and orphaned the key when the founding user leaves the org. Companies outlive
individuals; the key should too.

**Consequence worth stating:** because a converted org ends up with an `org_`
owner id, every existing `is_org_owner_id(owner_id)` prefix check keeps
answering correctly. No tenancy refactor is required.

## What conversion must produce

Traced from what an org-first signup creates today.

| Resource | Personal | After conversion |
|---|---|---|
| ECS service | `openclaw-{user_id}-{hash}` | `openclaw-{org_id}-{hash}` — **new; names are immutable** |
| EFS access point | rooted `/users/{user_id}` | rooted `/users/{org_id}` — **new; immutable** |
| EFS tree | `/users/{user_id}/` | copied to `/users/{org_id}/` |
| `containers` | `owner_id = user_id` | new row on `org_id` |
| `billing-accounts` | `owner_id = user_id` | new row on `org_id`, `owner_type = "org"` |
| ~9 other owner-keyed tables | `user_id` | re-keyed to `org_id` |
| `paperclip-companies` | row PK = `user_id` | **same row, updated in place** |
| Bedrock gate token | issued for `user_id` | reissued for `org_id` |
| Stripe subscription | flat $50 item | **same subscription**, items swapped |

### Paperclip: update the existing row, do not create one

`paperclip-companies` is partitioned by `user_id` and carries `org_id` as a
plain attribute on the `by-org-id` GSI (`projectionType: ALL`,
`database-stack.ts:286-290`). The table is a **per-member** record — an org has
one row per member, all sharing a single `company_id`; that is why
`list_by_org_id` exists and `find_owner_row` picks the *oldest active* of
several.

So the founder already has a row holding the right `company_id`. Conversion is
a single `UpdateItem` setting `org_id` on it: the row enters the `by-org-id`
GSI and becomes the org's owner row (the only one, hence the oldest). **The
Aurora company does not move.** Members who join later get their own rows
carrying the same `org_id` and `company_id`, via the existing membership path.

Do not write a second row. A duplicate would compete in `find_owner_row`'s
oldest-active selection and split the org's membership view.

### Stripe: modify in place, never cancel-and-resubscribe

The billing row moves to `org_id`, but the Stripe **subscription object stays
the same**. Swap the flat $50 item for base + seat items on that subscription
so Stripe prorates the difference itself. An `ultra_pro` personal owner's base
item carries `STRIPE_ULTRA_PRICE_ID` instead of `STRIPE_TEAM_BASE_PRICE_ID` —
same swap, different base price — so they land on an Ultra team rather than
being downgraded. Cancel-and-resubscribe would open a window where the owner
has no active subscription — the provision gate would lock them out of their
own workspace mid-conversion.

A `trial_local` owner has no Stripe customer at all, so this step is a normal
first checkout rather than a modify.

## The hard part: it cannot be atomic

DynamoDB, EFS, ECS, Aurora and Stripe cannot commit together. Conversion is
therefore a **resumable state machine with a recorded position**, not a script
that hopes. This is the bulk of the work — more than any individual copy.

Ordering principle: **do everything additive and reversible before anything
destructive, and never destroy until the new side is proven healthy.**

```
1. claim          conditional write; one conversion per owner, ever
2. create org     Clerk org (idempotent on retry via the claim)
3. copy EFS       /users/{user_id}/ -> /users/{org_id}/   [additive]
4. provision      new access point + ECS service on org_id  [additive]
5. health         new container reaches gateway_healthy     [GATE]
6. paperclip      UpdateItem: set org_id on the founder's row
7. billing        write org-keyed row; modify the Stripe subscription
8. re-key         remaining owner-keyed tables
9. verify         org context resolves end-to-end           [GATE]
10. teardown      stop old service, delete old access point [destructive]
```

Steps 1-5 leave the personal container untouched and fully working: a failure
anywhere in them is a no-op the user can retry. Step 10 is the only
irreversible one and runs last, behind two health gates.

The EFS copy is the long pole and must be **idempotent and restartable** —
re-running it must converge rather than duplicate.

## What the user sees

Conversion costs a provisioning cycle of downtime. The UI must say that
plainly — "Moving your workspace to the team, this takes a few minutes" with
real progress — not a bare spinner. That is the exact failure #1468 documented:
the system knowing what is happening and showing none of it.

## Testing

- Each step idempotent: run twice, converge.
- Interrupt at every step boundary; assert the personal container still works
  for steps 1-5, and that resume completes for 6-10.
- The claim admits exactly one conversion under concurrent attempts.
- After conversion: chat history present, agents present, Paperclip resolves
  through `by-org-id`, seat billing correct, and **exactly one** Paperclip row
  for the org.
- A `trial_local` owner converts without a Stripe modify.

## Ultra-tier owners

Resolved by this branch's Ultra-for-teams work — there is no longer a
block-or-downgrade choice to make. `create_ultra_upgrade` no longer refuses
per-seat subscriptions; it swaps a team subscription's base item and leaves
the seat item untouched, which is exactly the shape a converting Ultra owner
needs.

So conversion for an `ultra_pro` personal owner ($150/mo) is mechanical, not a
product decision: when step 7 swaps the single flat item for `[base, seat]`,
an `ultra_pro` owner's base item is written with `STRIPE_ULTRA_PRICE_ID`
instead of `STRIPE_TEAM_BASE_PRICE_ID`. They convert to an Ultra team — $150
base + $20/seat × N — and keep their container size instead of being silently
downgraded.

## Not in scope

Org → personal (reverse conversion). Merging two orgs. Moving a workspace
between orgs.
