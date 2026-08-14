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


def test_posthog_inbound_is_bearer_guided():
    from core.services.credentials.registry import get_provider

    p = get_provider("posthog")
    assert p.inbound is not None
    # Reverse-guided: still guided_paste (the owner creates the HTTP Webhook
    # destination by hand in PostHog's UI), but the secret flows Isol8 →
    # provider — PostHog sends no signature, only the Authorization header
    # the owner configures, verified by Paperclip's native bearer mode.
    assert p.inbound.webhook_registration == "guided_paste"
    assert p.inbound.signing_mode == "bearer"
    assert p.inbound.signature_header is None
    # No paste-back step, so no secret_hint — but the destination form is
    # buried enough that the instructions are load-bearing.
    assert p.inbound.secret_hint is None
    assert p.inbound.instructions
    assert {e.id for e in p.inbound.events} == {"$identify", "$pageview", "$exception", "any"}
    assert "{event_label}" in p.inbound.template.title


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


def test_vercel_inbound_is_auto_registering():
    from core.services.credentials.registry import get_provider

    p = get_provider("vercel_cli")
    assert p.inbound is not None
    assert p.inbound.webhook_registration == "auto_vercel"
    # Vercel signs HMAC-SHA1 over the raw body in x-vercel-signature —
    # Paperclip's hmac_sha1 mode reads it via the proxy's header copy.
    assert p.inbound.signing_mode == "hmac_sha1"
    assert p.inbound.signature_header == "x-vercel-signature"
    # The auto path has no paste step, so no guided copy or secret hint.
    assert p.inbound.instructions is None
    assert p.inbound.secret_hint is None
    # Every curated event names the Vercel webhook events to subscribe.
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

    native_modes = {"bearer", "hmac_sha256", "hmac_sha1", "github_hmac", "none"}
    for key, p in all_providers().items():
        if p.inbound is None:
            continue
        assert p.inbound.signing_mode in native_modes, key
        # signature_header is the proxy's raw-body copy shim — only meaningful
        # for the raw-body HMAC modes (hmac_sha256 providers need timestamp
        # splitting, which is bespoke, like Stripe's).
        if p.inbound.signature_header:
            assert p.inbound.signing_mode in {"github_hmac", "hmac_sha1"}, key
        assert p.inbound.template.suggested_agent == "friday", key
        if p.inbound.instructions:
            # Must interpolate cleanly with exactly these placeholders.
            p.inbound.instructions.format(proxy_url="https://x", event_label="L", event_id="e")


def test_event_catalogs_are_well_formed():
    """The full catalogs are generated from provider specs/docs — pin the shape
    every consumer assumes: unique ids, non-empty labels, no whitespace inside
    an id (ids ride into hook `events` arrays and instruction interpolation),
    and every auto-registering github/vercel event naming its provider-side
    subscription."""
    from core.services.credentials.registry import all_providers

    for key, p in all_providers().items():
        if p.inbound is None:
            continue
        ids = [e.id for e in p.inbound.events]
        assert len(ids) == len(set(ids)), key
        for e in p.inbound.events:
            assert e.id and not any(c.isspace() for c in e.id), (key, e.id)
            assert e.label and e.label.strip(), (key, e.id)
        if p.inbound.webhook_registration in ("auto_github", "auto_vercel"):
            for e in p.inbound.events:
                assert e.provider_events, (key, e.id)


def test_event_catalogs_meet_size_floors():
    """Hard floors so a truncated source fetch can't silently ship a gutted
    catalog — each floor sits under the fetched count (2026-08-13: stripe 265,
    vercel 98, github 53, attio 27, linear 15) but far above the old 3-event
    curated lists."""
    from core.services.credentials.registry import get_provider

    floors = {"stripe": 200, "vercel_cli": 10, "github": 35, "attio": 20, "linear": 12}
    for key, floor in floors.items():
        assert len(get_provider(key).inbound.events) >= floor, key


def test_google_inbound_is_auto_registering_via_watch():
    from core.services.credentials.registry import get_provider

    p = get_provider("google")
    assert p.inbound is not None
    assert p.inbound.webhook_registration == "auto_google_watch"
    # Google sends no body signature — deliveries echo the channel token in
    # X-Goog-Channel-Token, mapped by the proxy into Authorization: Bearer
    # for Paperclip's native bearer mode. No signature_header shim.
    assert p.inbound.signing_mode == "bearer"
    assert p.inbound.signature_header is None
    # The auto path has no paste step, so no guided copy or secret hint.
    assert p.inbound.instructions is None
    assert p.inbound.secret_hint is None
    assert {e.id for e in p.inbound.events} == {
        "calendar.changed",
        "drive.changed",
        "gmail.poll",
        "gmail.message",
    }
    # No provider_events: the watch call itself picks the surface — there is
    # no provider-side event array to subscribe.
    for e in p.inbound.events:
        assert e.provider_events is None, e.id
    # The seed formats the starter prompt with the picked account email (the
    # push notification carries no payload, so the description is the only
    # carrier of which -a the agent should use) — it must interpolate cleanly.
    rendered = p.inbound.template.starter_prompt.format(event_label="L", account="ada@example.com")
    assert "ada@example.com" in rendered


def test_gmail_events_carry_per_event_registration():
    """The gmail events override the google block's registration per-event —
    the seed and the connections DTO both dispatch on the effective value, so
    these exact strings are load-bearing. The watch events must stay None
    (inherit auto_google_watch)."""
    from core.services.credentials.registry import get_provider

    events = {e.id: e for e in get_provider("google").inbound.events}
    assert events["calendar.changed"].registration is None
    assert events["drive.changed"].registration is None
    assert events["gmail.poll"].registration == "poll"
    assert events["gmail.message"].registration == "owner_setup"
    # Both gmail events override the push-notification template with their
    # own starter prompt, which must interpolate the account cleanly.
    for event_id in ("gmail.poll", "gmail.message"):
        rendered = events[event_id].starter_prompt.format(event_label="L", account="ada@example.com")
        assert "ada@example.com" in rendered
