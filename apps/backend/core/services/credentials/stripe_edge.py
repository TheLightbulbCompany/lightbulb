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
every (owner_id, public_id) -> whsec_ lookup for an hour (a live Secrets
Manager call on every ordinary/unsigned trigger's every fire would otherwise
be the common case — see ``paperclip_trigger_proxy``'s module docstring).
Living here — rather than as module-private state in the proxy — is what
lets the WRITE side invalidate it: ``routers/automations.py``'s
webhook-secret PUT calls ``invalidate_stripe_secret_cache`` right after
every store, so a just-armed or just-rotated trigger is never left
forwarding UNSIGNED (fail-open) for up to the cache's TTL. The proxy module
imports this same dict by reference (not a copy), so both sides see one cache.
"""

WEBHOOK_SECRET_FIELD = "webhook_secret"

# TTL for the cached (owner_id, public_id) -> whsec_ (or confirmed-absent
# None) lookup. See the module docstring — every trigger fire of every kind
# hits this cache, so an uncached negative would mean a live Secrets Manager
# call per fire.
STRIPE_SECRET_TTL_SECONDS = 3600
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
    Manager on a cache miss, so skipping this leaves a stale (or stale
    "nothing stored") verdict live for up to ``STRIPE_SECRET_TTL_SECONDS``.
    A pop of an absent key is a no-op, so this is safe to call unconditionally.
    """
    STRIPE_SECRET_CACHE.pop((owner_id, public_id), None)
