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
    assert p.inbound.event_in_body is True
    assert p.inbound.signature_header is None
    # No paste-back step, so no secret_hint — but the destination form is
    # buried enough that the instructions are load-bearing.
    assert p.inbound.secret_hint is None
    assert p.inbound.instructions
    assert "properties.code" in p.inbound.instructions
    assert "Never add URLs" in p.inbound.instructions
    assert {e.id for e in p.inbound.events} == {
        "$identify",
        "$pageview",
        "$exception",
        "onboarding_gate_blocked",
        "any",
    }
    event = next(e for e in p.inbound.events if e.id == "onboarding_gate_blocked")
    assert event.starter_prompt is not None
    assert "duplicate-activation-blocker" in event.starter_prompt
    assert "CloudWatch" in event.starter_prompt
    assert "{{code, service=lightbulb-backend, env=prod}}" in event.starter_prompt
    assert "conversation data" in event.starter_prompt
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
        # event_in_body is what gates multi-event triggers, and it is only
        # honest for a provider whose BODY names the fired event. Pin the two
        # header-identified providers as False so a future "just flip it"
        # cannot quietly arm several events onto an agent that can't tell them
        # apart (Isol8AI/isol8#1916 is the durable fix).
        if key in ("github", "sentry"):
            assert p.inbound.event_in_body is False, key
        if p.inbound.instructions:
            # Must interpolate cleanly with exactly the kwargs the seed passes
            # (inbound_triggers._seed_trigger_routine's guided_paste branch) —
            # the plural {event_labels}/{event_ids} and the older singular
            # spellings, which stay bound to the same joined rendering.
            p.inbound.instructions.format(
                proxy_url="https://x",
                event_label="L",
                event_id="e",
                event_labels="L and M",
                event_ids="e and f",
            )


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


# ── Tier 2: the backend-readable REST keys ────────────────────────────────
#
# `attio`/`apollo` are MCP OAuth connections the AGENT reaches from inside the
# container; a backend route cannot. `retain_secret` would not fix that, because
# the grants are MCP-SCOPED — Attio's is `openid offline_access mcp` while a
# record read needs `record` + `object configuration` — so retaining that token
# would retain something that 403s. The REST key is a separate credential.
#
# These tests pin the property the whole design exists for. If it regresses,
# nothing throws: Leads just silently falls back to having no backend data,
# which reads as "no leads" rather than as a broken credential path.


def test_tier2_rest_keys_are_retained_backend_side():
    from core.services.credentials.registry import all_providers
    from routers.connections import _retains_secret

    providers = all_providers()
    for key in ("attio_api", "apollo_api"):
        p = providers[key]
        assert p.acquisition == "paste_token", key
        # `refresh_owner: none` is what makes _retains_secret true. An actor
        # refresh_owner would hand custody to the container keyring and the
        # backend would store nothing durable.
        assert p.refresh_owner == "none", key
        assert _retains_secret(p) is True, f"{key} must be readable by a backend route"


def test_brokered_remote_mcp_rows_retain_refresh_grants_in_production(monkeypatch):
    """The runtime receives per-turn bearers; Secrets Manager owns refresh grants."""
    from core.services.credentials import registry
    from core.services.credentials.registry import all_providers
    from routers.connections import _retains_secret

    providers = all_providers()
    monkeypatch.setattr(registry.settings, "ENVIRONMENT", "prod")
    for key in ("stripe", "notion", "sentry", "linear", "attio", "intercom", "posthog", "apollo", "cloudflare"):
        assert _retains_secret(providers[key]) is True, key


def test_remote_mcp_providers_are_brokered_in_production(monkeypatch):
    """AgentCore receives a per-turn bearer; refresh grants stay in Secrets Manager."""
    from core.services.credentials import registry
    from core.services.credentials.apply_connection import runtime_supported

    monkeypatch.setattr(registry.settings, "ENVIRONMENT", "prod")
    for key in ("stripe", "notion", "sentry", "linear", "attio", "intercom", "posthog", "apollo", "cloudflare"):
        provider = registry.get_provider(key)
        assert registry.available_for(provider, "org_test"), key
        assert registry.refresh_owner_for(provider) == "backend", key
        assert runtime_supported(provider), key


def test_paired_providers_never_share_a_bare_name():
    """Two rows for one vendor with the same label let a status on one read as
    a status on the other — the confusion #2262 fixes for Stripe. Each pair
    must say who the credential is for."""
    from core.services.credentials.registry import all_providers

    providers = all_providers()
    for agent_key, api_key in (("attio", "attio_api"), ("apollo", "apollo_api")):
        agent_name = providers[agent_key].display_name
        api_name = providers[api_key].display_name
        assert agent_name != api_name
        assert "agents" in agent_name.lower(), agent_name
        assert "dashboard" in api_name.lower(), api_name


def test_backend_only_wiring_delivers_nothing_to_the_container():
    """The tier-2 REST keys must NOT be written into openclaw.json.

    An earlier version wired them to `env.vars.*`. That is not durable: a
    reprovision rebuilds openclaw.json wholesale and re-injects only the
    MCP-server merge and `provisioning_landing.build_provisioned_env`, which
    selects Projects rows by key prefix AND actor stamp — so the key would
    vanish on the next image roll while the integration row still read
    "connected". A credential that disappears without telling anyone is worse
    than one the agent never had.
    """
    from core.services.credentials.registry import all_providers

    for key in ("attio_api", "apollo_api"):
        w = all_providers()[key].wiring
        assert w.backend_only is True, key
        assert w.openclaw_paths is None, key
        assert w.materializer is None, key
        assert w.mcp_server is None, key


def test_backend_only_refuses_a_delivery_target():
    """`backend_only` must be explicit, never inferred from an empty wiring —
    and must not silently coexist with a target that would still be written."""
    import pytest
    from core.services.credentials.registry import McpServer, Wiring

    with pytest.raises(ValueError, match="must not set a delivery target"):
        Wiring(backend_only=True, materializer="skill_cli")

    # And a wiring with no target and no flag is still a mistake, not a
    # backend-only provider by accident.
    with pytest.raises(ValueError, match="exactly one"):
        Wiring()

    # Sanity: the ordinary shapes still validate.
    assert Wiring(materializer="skill_cli").backend_only is False
    assert Wiring(mcp_server=McpServer(key="x", url="https://e.example", transport="streamable-http"))


def test_tier2_keys_are_validated_before_they_are_stored():
    """Without a validate block the paste rail accepts anything: a typo or a
    wrongly-scoped token is stored, shown as Connected, and 403s only at first
    read with nothing surfacing to the owner."""
    from core.services.credentials.registry import all_providers

    providers = all_providers()
    attio = providers["attio_api"].validation
    assert attio is not None and attio.url.startswith("https://api.attio.com/")

    apollo = providers["apollo_api"].validation
    assert apollo is not None
    # Apollo answers 200 with `is_logged_in: false` for a bad key, so the HTTP
    # status alone would pass an invalid credential.
    assert apollo.success_json_field == "is_logged_in"
    assert apollo.auth_scheme == "", "Apollo takes a bare x-api-key, not a Bearer scheme"
