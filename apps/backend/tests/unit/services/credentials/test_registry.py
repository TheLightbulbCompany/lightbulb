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


def test_tier2_providers_have_inbound_with_signature_headers():
    from core.services.credentials.registry import get_provider

    expected = {
        "sentry": "Sentry-Hook-Signature",
        "linear": "Linear-Signature",
        "attio": "Attio-Signature",
    }
    for key, header in expected.items():
        p = get_provider(key)
        assert p.inbound is not None, key
        assert p.inbound.webhook_registration == "guided_paste"
        # Raw-body HMAC-SHA256/hex — verified by Paperclip's github_hmac mode
        # (its sha256= prefix strip is optional), fed via the proxy's header
        # copy from the provider's own signature header.
        assert p.inbound.signing_mode == "github_hmac", key
        assert p.inbound.signature_header == header, key
        assert p.inbound.events, key
        assert p.inbound.secret_hint, key


def test_github_inbound_is_auto_registering():
    from core.services.credentials.registry import get_provider

    p = get_provider("github")
    assert p.inbound is not None
    assert p.inbound.webhook_registration == "auto_github"
    # GitHub's X-Hub-Signature-256 is read natively by Paperclip's github_hmac
    # mode — no proxy header-copy shim, so no signature_header.
    assert p.inbound.signing_mode == "github_hmac"
    assert p.inbound.signature_header is None
    # The auto path has no paste step, so no guided copy or secret hint.
    assert p.inbound.instructions is None
    assert p.inbound.secret_hint is None
    # Every curated event names the GitHub hook events to subscribe.
    assert p.inbound.events
    for e in p.inbound.events:
        assert e.provider_events, e.id


def test_inbound_invariants_hold_for_every_provider():
    """YAML-only wiring means YAML mistakes are the failure mode — pin the
    invariants the seed service and proxy assume for EVERY inbound block:
    a native Paperclip signingMode, a resolvable suggested agent, and
    instructions whose placeholders actually interpolate (a stray brace
    would otherwise KeyError at seed time, after the routine exists)."""
    from core.services.credentials.registry import all_providers

    native_modes = {"bearer", "hmac_sha256", "github_hmac", "none"}
    for key, p in all_providers().items():
        if p.inbound is None:
            continue
        assert p.inbound.signing_mode in native_modes, key
        # signature_header is the proxy's raw-body copy shim — only meaningful
        # for github_hmac (hmac_sha256 providers need timestamp splitting,
        # which is bespoke, like Stripe's).
        if p.inbound.signature_header:
            assert p.inbound.signing_mode == "github_hmac", key
        assert p.inbound.template.suggested_agent == "friday", key
        if p.inbound.instructions:
            # Must interpolate cleanly with exactly these placeholders.
            p.inbound.instructions.format(proxy_url="https://x", event_label="L", event_id="e")
