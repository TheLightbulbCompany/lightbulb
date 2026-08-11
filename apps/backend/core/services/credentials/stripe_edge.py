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

**Also owns the trigger proxy's resolved-credential cache.** The proxy
caches every (owner_id, public_id) -> credential-dict lookup. Three states,
two TTLs: an ARMED trigger (a whsec_ IS stored) caches for 5 minutes — long
enough that the per-fire Secrets Manager read is amortized, short enough to
bound cross-task staleness when the owner ROTATES the secret (see
``STRIPE_SECRET_TTL_SECONDS``). A NON-armed trigger — no record ("ordinary
unsigned"), or a record carrying only the stripe-edge-seeded marker ("seeded,
whsec_ not pasted yet") — caches for only 60s, so a just-armed trigger starts
verifying (and a just-seeded one keeps failing CLOSED) promptly even on a task
the invalidate below never reaches. A seeded-but-unarmed trigger is never
forwarded unsigned: the proxy 401s it until the whsec_ lands, so there is no
fail-OPEN window to bound — only the two fail-closed windows above. Living
here — rather than as module-private state in the proxy — is what lets the
WRITE side invalidate it: ``routers/automations.py``'s webhook-secret PUT
calls ``invalidate_stripe_secret_cache`` right after every store. On the
common single-task deployment that's immediate; on a multi-task fleet the
invalidate is local to the task that served the write, so a sibling task's
stale verdict is bounded by whichever TTL above applies — never unbounded.
The proxy module imports this same dict by reference (not a copy), so both
sides see one cache.

**Write-during-read TOCTOU.** A cache-miss read of Secrets Manager can
start BEFORE a concurrent ``put()`` + ``invalidate_stripe_secret_cache()``
(the webhook-secret PUT) and complete AFTER it — the read's own (now-stale)
result would then unconditionally overwrite the cache, silently restoring
the pre-write "nothing stored" verdict the invalidate just cleared, for up
to the negative TTL. ``_STRIPE_SECRET_CACHE_GENERATION`` closes this: every
invalidate bumps the target key's generation, and a reader captures the
generation BEFORE issuing its Secrets Manager call and only commits its
result to the cache if the generation is UNCHANGED when the call returns —
otherwise it discards its own stale read (the cache stays as the
invalidate left it, and the NEXT read re-fetches for real). See
``stripe_secret_cache_generation`` and the reader in
``paperclip_trigger_proxy._stripe_trigger_cred``.
"""

WEBHOOK_SECRET_FIELD = "webhook_secret"

# Written by ``core.services.inbound_triggers.seed_trigger_routine`` into
# THIS SAME per-trigger Secrets Manager slot (``stripe_trigger_secret_provider``)
# the instant a stripe_edge trigger is created — before the owner has pasted
# anything back. Its only job is presence: ``PUT
# /automations/{public_id}/webhook-secret`` (``routers/automations.py``) gates
# on "does ANY record exist at this slot" rather than on Paperclip's
# signingMode == "none", because "none" is ALSO Phase 1's own deliberately-
# unsigned, user-selectable option (``webhookSigning.ts``) — gating on
# signingMode alone would let that PUT arm (and thereby brick, by making it
# demand a Stripe-Signature header it will never receive) a trigger no Stripe
# integration will ever sign for. Nothing else ever writes to this slot, so
# presence — this marker, or an already-pasted whsec_ that later overwrote it
# — is an unambiguous "this trigger was seeded stripe_edge" signal.
STRIPE_EDGE_SEEDED_FIELD = "stripe_edge_seeded"

# TTL for a POSITIVE cached (owner_id, public_id) -> armed-whsec_ lookup. See
# the module docstring — every trigger fire of every kind hits this cache, so
# an uncached read would mean a live Secrets Manager call per fire. Kept to 5
# minutes (not an hour) so that when an owner ROTATES a Stripe endpoint secret,
# a sibling API task holding the OLD armed value re-reads within 5 minutes even
# if the invalidate-on-write (below) never reaches it — bounding the cross-task
# rotation-staleness window. Fails closed the whole time (a stale old secret
# rejects new-secret-signed events, which Stripe then retries), never open.
STRIPE_SECRET_TTL_SECONDS = 300

# TTL for a NON-ARMED cached lookup — no record at all ("ordinary/unsigned
# trigger"), or a record carrying only the stripe-edge-seeded marker ("seeded
# but the owner hasn't pasted the whsec_ yet"). Kept short relative to the
# positive TTL above so a just-armed trigger starts verifying (and a
# just-seeded one starts failing closed) promptly even when the
# invalidate-on-write doesn't reach every task in a multi-task fleet.
STRIPE_SECRET_NEGATIVE_TTL_SECONDS = 60

# Caches the RESOLVED credential dict (or None = no record), not just the
# secret string: the proxy must tell a seeded-but-unarmed trigger (marker
# only -> fail CLOSED) apart from an ordinary unsigned trigger (no record ->
# forward), and both would collapse to a bare None.
STRIPE_SECRET_CACHE: dict[tuple[str, str], tuple[float, dict | None]] = {}

# Per-key monotonic counter, bumped by every ``invalidate_stripe_secret_cache``
# call. Lets a cache-miss reader detect a write that raced ahead of it and
# discard its own now-stale result instead of caching it — see the module
# docstring's "Write-during-read TOCTOU" section.
_STRIPE_SECRET_CACHE_GENERATION: dict[tuple[str, str], int] = {}


def stripe_secret_cache_generation(owner_id: str, public_id: str) -> int:
    """Current generation for one trigger's cached whsec_ lookup. A reader
    must capture this BEFORE issuing its Secrets Manager call, and only
    write its result into ``STRIPE_SECRET_CACHE`` if this is still the same
    value afterward — see ``invalidate_stripe_secret_cache``."""
    return _STRIPE_SECRET_CACHE_GENERATION.get((owner_id, public_id), 0)


def stripe_trigger_secret_provider(public_id: str) -> str:
    """The ``integration_secret_store`` ``provider`` slot for one
    stripe_edge trigger's whsec_. Hyphenated, not colon-joined — Secrets
    Manager names only allow ``/_+=.@-``.
    """
    return f"stripe-trigger-{public_id}"


def invalidate_stripe_secret_cache(owner_id: str, public_id: str) -> None:
    """Drop the cached whsec_ lookup for one trigger. MUST be called by
    every writer of that trigger's secret (initial guided-paste store,
    rotate) — the proxy's ``_stripe_trigger_cred`` only re-reads Secrets
    Manager on a cache miss, so skipping this leaves a stale verdict live
    for up to ``STRIPE_SECRET_TTL_SECONDS`` (a stale positive) or
    ``STRIPE_SECRET_NEGATIVE_TTL_SECONDS`` (a stale "nothing stored") on
    whichever task's cache still holds it. A pop of an absent key is a
    no-op, so this is safe to call unconditionally.

    Also bumps the key's generation counter — see the module docstring's
    "Write-during-read TOCTOU" section. This must happen even when the pop
    above is a no-op: a concurrent read may not have written its (stale)
    result into the cache YET, and the generation bump is what makes that
    still-in-flight write discard itself instead of re-creating the entry
    this call just cleared.
    """
    key = (owner_id, public_id)
    _STRIPE_SECRET_CACHE_GENERATION[key] = _STRIPE_SECRET_CACHE_GENERATION.get(key, 0) + 1
    STRIPE_SECRET_CACHE.pop(key, None)
