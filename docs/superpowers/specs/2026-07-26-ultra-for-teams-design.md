# Ultra for teams

**Status:** design approved 2026-07-26
**Why now:** unblocks personal → org conversion for `ultra_pro` owners, who would
otherwise be silently downgraded. Ships independently of conversion.

## The principle

**Ultra is a container size, nothing else.** Verified — it is exactly two
constants:

```
ULTRA_CPU = "2048"        ULTRA_MEMORY_MIB = "8192"    # ecs_manager.py:72-73
PER_USER_CPU = "1024"     PER_USER_MEMORY_MIB = "4096" # ecs_manager.py:53-54
```

Size and tenancy are orthogonal axes. Any owner — personal or org — should be
able to run either size. Today they are entangled, purely as an artifact of
billing shape.

## What blocks it today

Two places, both billing-shape assumptions rather than anything real:

1. **`create_ultra_upgrade` (`billing_service.py`)** refuses per-seat subs
   outright: *"Ultra Pro isn't available for per-seat team plans yet"*. Ultra
   is a flat $150 price that **replaces** the flat $50 — and a team
   subscription has no single flat item to replace.
2. **The webhook tier branch (`stripe_webhook_handlers.py:808`)** is
   `if items and not is_team_seat:` — team subs are excluded from tier logic
   entirely, so a team could never *become* Ultra even if sold one.

## The shape

A team subscription is `[base, seat × N]`. Ultra swaps the **base** item and
leaves the seat item untouched:

| | line items | 4-person total |
|---|---|---|
| Personal standard | `flat $50` | — |
| Personal ultra | `ultra $150` | — |
| Team standard | `team_base $50` + `seat $20 × N` | $130 |
| **Team ultra** | **`ultra $150`** + `seat $20 × N` | **$230** |

Seat billing stays completely independent of tier. That is the whole point:
one axis for size, one for headcount.

Rejected alternative: keep the $50 base and **add** a $100 uplift item. Three
line items, and every "which item is which" question gets harder for no gain.

## No new Stripe price

The existing Ultra price already has the right shape in both environments —
audited 2026-07-26:

| Env | Price | Shape |
|---|---|---|
| prod | `price_1Tv1mXI54BysGS3r28E4hWOS` | $150.00 USD, recurring, licensed, monthly, active |
| dev | `price_1Tv1lSI54BysGS3rTDizqnP2` | same, test mode |

The same price object serves as the whole subscription for a personal owner and
as the base item for a team. So: **no new price, no new secret, no new config,
no CDK wiring.**

`is_ultra` detection also needs **no change** — it already looks for
`STRIPE_ULTRA_PRICE_ID` via `subscription_item_on_price`, and that lookup
answers correctly for a team the moment a team can carry that price.

Cosmetic consequence: the line reads "Isol8" on a team invoice rather than
"Isol8 Teams", because the price lives on the personal product. Fixable with a
nickname if it matters; not worth a second price object.

## Scope

1. **`create_ultra_upgrade` upgrades a team** instead of refusing it. For a
   team sub it must select the **base** item **by price ID**
   (`STRIPE_TEAM_BASE_PRICE_ID`) and swap that one — not `items_data[0]`.
   That positional read is currently unreachable behind the multi-item guard;
   removing the refusal makes it reachable, so it must be fixed in the same
   change. Use `subscription_item_on_price`.
2. **Relax the multi-item guard.** `len(items_data) > 1` currently means "any
   multi-item subscription is a shape I don't recognise — refuse." It must
   become "refuse *unrecognised* multi-item shapes, allow the known
   `[base, seat]` team shape." Do not simply delete it: it is the
   defence-in-depth that stops an unknown add-on subscription being repriced.
3. **Drop `not is_team_seat`** from the webhook tier branch so team subs are
   tiered and resized like any other owner.
4. **Downgrade path**: ultra team → standard team swaps the ultra price back to
   `STRIPE_TEAM_BASE_PRICE_ID`, keeping the seat item. The existing
   oversized-company deferral (>10 agents ⇒ do not shrink the container)
   applies unchanged.

## The interaction to get right

With the tier branch no longer excluding teams, an ultra team's webhook runs
**both** the seat-persistence branch and the tier branch. They must not fight:

- Seat persistence writes `subscription_item_id` + `seat_count`, selected by
  the **seat** price.
- Tier persistence writes `plan` and resizes the container, keyed on the
  **ultra** price.

They touch different items and different fields, so they compose — but this is
the first time both run on one subscription, and it is exactly the class of
overlap that produced the `items[0]` bug. Test both branches firing together.

Also: `sync_team_seat_quantity` modifies the stored `subscription_item_id`,
which is the seat item. A base swap must never change it. It cannot, because
the item is selected by price ID — but assert it.

## Testing

- Team standard → ultra: base item swaps, **seat item id and quantity
  unchanged**, container resizes up, `plan = "ultra_pro"`.
- Ultra team → standard: base swaps back, seats unchanged, resize down honours
  the >10-agent deferral.
- Both webhook branches firing on one ultra-team subscription: seat state and
  tier state both persist correctly.
- An unrecognised multi-item subscription is still refused.
- Personal ultra unchanged — the regression this must not cause.
- Seat sync after an ultra upgrade still targets the seat item.

## Follow-up

Once this ships, personal → org conversion has no Ultra special case: an ultra
personal owner converts to an ultra team, keeping their container size.
