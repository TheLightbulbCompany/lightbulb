"""Shared naming for the Stripe-edge webhook-verification scheme.

ONE source of truth for both the ``integration_secret_store`` key scheme
and the credential dict's field name, so the read side (the trigger proxy,
``routers/paperclip_trigger_proxy.py``) and the write side (``PUT
/automations/{public_id}/webhook-secret`` — ``routers/automations.py``,
T3/T6) can never drift apart on either.

**Scoped per TRIGGER, not per owner.** An owner can wire several Stripe
triggers (different events, different Stripe Dashboard webhook endpoints),
each with its own ``whsec_``. Storing under the owner alone would let a
second trigger's secret silently clobber the first's.

**Also owns the trigger proxy's resolved-secret cache.** The proxy caches
every (owner_id, public_id) -> whsec_ lookup, but a POSITIVE result (a
secret IS stored) and a NEGATIVE one ("nothing stored yet") get different
TTLs: positive stays cached for an hour (rotation is rare and re-reading a
still-valid secret buys nothing); negative is cached for only 60s (a live
Secrets Manager call on every ordinary/unsigned trigger's every fire would
otherwise be the common case — see ``paperclip_trigger_proxy``'s module
docstring — but caching "nothing stored" for a full hour would leave a
just-armed trigger forwarding UNSIGNED for up to that hour on any task the
invalidate below doesn't reach). Living here — rather than as
module-private state in the proxy — is what lets the WRITE side invalidate
it: ``routers/automations.py``'s webhook-secret PUT calls
``invalidate_stripe_secret_cache`` right after every store. On the common
single-task deployment that's immediate; on a multi-task fleet the
invalidate is local to the task that served the write, so a sibling task's
stale "nothing stored" verdict is bounded by the 60s negative TTL instead
of the hour-long positive one — never unbounded. The proxy module imports
this same dict by reference (not a copy), so both sides see one cache.
"""

WEBHOOK_SECRET_FIELD = "webhook_secret"

# TTL for a POSITIVE cached (owner_id, public_id) -> whsec_ lookup. See the
# module docstring — every trigger fire of every kind hits this cache, so an
# uncached negative would mean a live Secrets Manager call per fire.
STRIPE_SECRET_TTL_SECONDS = 3600

# TTL for a NEGATIVE cached lookup ("no secret stored for this trigger yet").
# Kept short relative to the positive TTL above so the just-armed-trigger
# fail-open window (see module docstring) stays bounded even when the
# invalidate-on-write doesn't reach every task in a multi-task fleet.
STRIPE_SECRET_NEGATIVE_TTL_SECONDS = 60

STRIPE_SECRET_CACHE: dict[tuple[str, str], tuple[float, str | None]] = {}


def stripe_trigger_secret_provider(public_id: str) -> str:
    """The ``integration_secret_store`` ``provider`` slot for one
    stripe_edge trigger's whsec_. Hyphenated, not colon-joined — Secrets
    Manager names only allow ``/_+=.@-``.
    """
    return f"stripe-trigger-{public_id}"


def invalidate_stripe_secret_cache(owner_id: str, public_id: str) -> None:
    """Drop the cached whsec_ lookup for one trigger. MUST be called by
    every writer of that trigger's secret (initial guided-paste store,
    rotate) — the proxy's ``_stripe_trigger_secret`` only re-reads Secrets
    Manager on a cache miss, so skipping this leaves a stale verdict live
    for up to ``STRIPE_SECRET_TTL_SECONDS`` (a stale positive) or
    ``STRIPE_SECRET_NEGATIVE_TTL_SECONDS`` (a stale "nothing stored") on
    whichever task's cache still holds it. A pop of an absent key is a
    no-op, so this is safe to call unconditionally.
    """
    STRIPE_SECRET_CACHE.pop((owner_id, public_id), None)
