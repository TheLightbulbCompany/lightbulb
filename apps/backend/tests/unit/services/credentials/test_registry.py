import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def test_stripe_provider_has_inbound_events():
    from core.services.credentials.registry import get_provider

    p = get_provider("stripe")
    assert p.inbound is not None
    assert p.inbound.webhook_registration == "guided_paste"
    # Native Paperclip signingMode now (its {t}.{body} HMAC == Stripe's scheme).
    assert p.inbound.signing_mode == "hmac_sha256"
    assert any(e.id == "invoice.payment_failed" for e in p.inbound.events)
    assert "{event_label}" in p.inbound.template.title


def test_provider_without_inbound_is_none():
    from core.services.credentials.registry import get_provider

    # notion has no inbound: block — the field must default to None, not error.
    assert get_provider("notion").inbound is None
