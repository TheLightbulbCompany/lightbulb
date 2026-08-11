"""Shared naming for the Stripe-edge webhook-verification scheme.

ONE source of truth for both the ``integration_secret_store`` key scheme
and the credential dict's field name, so the read side (the trigger proxy,
``routers/paperclip_trigger_proxy.py``) and the write sides (the seed
service's guided_paste branch and the paste-secret endpoint — Automations
Phase 2 T3/T6) can never drift apart on either.

**Scoped per TRIGGER, not per owner.** An owner can wire several Stripe
triggers (different events, different Stripe Dashboard webhook endpoints),
each with its own ``whsec_``. Storing under the owner alone would let a
second trigger's secret silently clobber the first's.
"""

WEBHOOK_SECRET_FIELD = "webhook_secret"


def stripe_trigger_secret_provider(public_id: str) -> str:
    """The ``integration_secret_store`` ``provider`` slot for one
    stripe_edge trigger's whsec_. Hyphenated, not colon-joined — Secrets
    Manager names only allow ``/_+=.@-``.
    """
    return f"stripe-trigger-{public_id}"
