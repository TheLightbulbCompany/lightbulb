import re

import pytest

from core.config import apex_domain, settings, url_host
from core.crypto.operator_device import device_identity_from_pem
from core.services.paperclip_adapter_config import (
    build_gateway_url_re,
    synthesize_openclaw_adapter,
    validate_gateway_url,
    AdapterConfigError,
    OPENCLAW_GATEWAY_TYPE,
    PAPERCLIP_OPERATOR_CLIENT_ID,
    claimed_api_key_path,
)


# The allowlist apex is DERIVED from settings.WS_GATEWAY_URL (see
# build_gateway_url_re), so these tests must derive it too. Hardcoding a domain
# here passed only while the configured domain happened to match the literal —
# the moment the product domain moved, 21 tests went red on main.
APEX = apex_domain(url_host(settings.WS_GATEWAY_URL))


def test_canonical_adapter_type_is_underscored():
    assert OPENCLAW_GATEWAY_TYPE == "openclaw_gateway"


@pytest.mark.parametrize(
    "url",
    [
        f"wss://ws.{APEX}",
        f"wss://ws-dev.{APEX}",
        f"wss://ws-staging.{APEX}",
        "ws://localhost:8000",
        "ws://localhost:18789",
    ],
)
def test_validate_gateway_url_accepts_known_formats(url):
    validate_gateway_url(url)  # does not raise


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com",
        "wss://evil.com",
        f"wss://ws.{APEX}.evil",
        "wss://169.254.169.254",
        f"wss://ws.{APEX}/path",
        f"wss://ws-dev.{APEX}.evil",
        "ws://attacker.local:8000",
        f"wss://ws.{APEX}\n",
        f"wss://ws.{APEX}\r\n",
        "",
        None,
    ],
)
def test_validate_gateway_url_rejects_unknown_formats(url):
    with pytest.raises(AdapterConfigError):
        validate_gateway_url(url)


def test_gateway_allowlist_follows_the_configured_gateway_host():
    """The allowlist tracks WS_GATEWAY_URL's apex, so moving the product domain
    is a config change, not a code change. It is fail-CLOSED: an unconfigured
    gateway allows localhost only, never a looser hosted pattern."""
    live = build_gateway_url_re(f"wss://ws-dev.{APEX}")
    assert live.pattern == (rf"\A(?:wss://ws(?:-[a-z]+)?\.{re.escape(APEX)}|ws://localhost:[0-9]+)\Z")

    moved = build_gateway_url_re("wss://ws.example.ai")
    assert moved.match("wss://ws.example.ai")
    assert moved.match("wss://ws-dev.example.ai")
    assert moved.match("ws://localhost:8000")
    assert not moved.match(f"wss://ws.{APEX}")
    assert not moved.match("wss://ws.example.ai.evil")

    unset = build_gateway_url_re("")
    assert unset.match("ws://localhost:8000")
    assert not unset.match(f"wss://ws.{APEX}")
    assert not unset.match("wss://ws.")


def test_synthesize_returns_canonical_shape(monkeypatch):
    # With no internal URL configured, no paperclipApiUrl override is added.
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "")
    monkeypatch.setattr(settings, "PUBLIC_API_BASE", settings.PUBLIC_API_BASE)
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://ws-dev.{APEX}",
        service_token="JWT_TOKEN_HERE",
        user_id="user_123",
    )
    # Token must live in the URL query string — the Isol8 WS API Gateway
    # authorizer reads it from `route.request.querystring.token`. clientId routes
    # the connection to the operator relay at $connect.
    assert cfg["url"] == f"wss://ws-dev.{APEX}?token=JWT_TOKEN_HERE&clientId=paperclip-operator"
    # authToken retained for adapter compatibility (header is ignored by API GW).
    assert cfg["authToken"] == "JWT_TOKEN_HERE"
    assert cfg["sessionKeyStrategy"] == "issue"
    assert cfg["sessionKey"] == "paperclip:user_123"
    assert cfg["devicePrivateKeyPem"].startswith("-----BEGIN PRIVATE KEY-----")
    # The adapter's 120s default is too short for our agents' substantive runs;
    # we pin 10 min (the API Gateway WS idle ceiling) so a still-working run isn't
    # mislabeled `timed_out` + retried.
    assert cfg["timeoutSec"] == 600
    assert cfg["waitTimeoutMs"] == 600_000
    # Every seeded agent (trial AND paid) carries the pre-run allowance URL so
    # the adapter checks the daily cap before dialing; the endpoint says yes for
    # paid owners, so conversion needs no adapterConfig edit.
    assert set(cfg.keys()) == {
        "url",
        "authToken",
        "sessionKeyStrategy",
        "sessionKey",
        "devicePrivateKeyPem",
        "timeoutSec",
        "waitTimeoutMs",
    }


def test_synthesize_pins_paperclip_api_url_to_internal_host(monkeypatch):
    """When PAPERCLIP_INTERNAL_URL is configured, the adapter carries a
    ``paperclipApiUrl`` override so the agent's wake run curls the in-VPC host
    instead of Paperclip's default origin(PAPERCLIP_PUBLIC_URL) — which is the
    public auth origin, unreachable from inside the VPC."""
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "http://paperclip.lightbulb-prod.local:3100")
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://ws.{APEX}",
        service_token="t" * 40,
        user_id="user_abc",
    )
    assert cfg["paperclipApiUrl"] == "http://paperclip.lightbulb-prod.local:3100"


def test_synthesize_rejects_bad_url():
    with pytest.raises(AdapterConfigError):
        synthesize_openclaw_adapter(
            gateway_url="https://evil.com",
            service_token="x",
            user_id="u",
        )


def test_synthesize_rejects_empty_token():
    with pytest.raises(AdapterConfigError):
        synthesize_openclaw_adapter(
            gateway_url=f"wss://ws-dev.{APEX}",
            service_token="",
            user_id="u",
        )


def test_synthesize_rejects_empty_user_id():
    with pytest.raises(AdapterConfigError):
        synthesize_openclaw_adapter(
            gateway_url=f"wss://ws-dev.{APEX}",
            service_token="x",
            user_id="",
        )


def test_strategy_is_issue_for_per_issue_session_isolation():
    """We want each issue assigned to an agent to be its own OpenClaw
    session so transcripts don't bleed across unrelated issues. The
    sessionKey field becomes the fallback for manual wakes (no issue
    context); the issue-driven path uses paperclip:issue:<id>."""
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://ws.{APEX}",
        service_token="t" * 100,
        user_id="user_abc",
    )
    assert cfg["sessionKeyStrategy"] == "issue"
    assert cfg["sessionKey"] == "paperclip:user_abc"
    assert cfg["url"] == f"wss://ws.{APEX}?token=" + "t" * 100 + "&clientId=paperclip-operator"
    assert cfg["authToken"] == "t" * 100


def test_url_routes_to_operator_relay_via_client_id():
    cfg = synthesize_openclaw_adapter(gateway_url=f"wss://ws.{APEX}", service_token="tok", user_id="u")
    assert f"clientId={PAPERCLIP_OPERATOR_CLIENT_ID}" in cfg["url"]
    assert PAPERCLIP_OPERATOR_CLIENT_ID == "paperclip-operator"


def test_mints_a_fresh_device_key_per_agent_when_absent():
    # Each create with no existing PEM gets its OWN keypair (stable per agent,
    # distinct across agents) — required so paired.json entries don't collide.
    a = synthesize_openclaw_adapter(gateway_url=f"wss://ws.{APEX}", service_token="t", user_id="u")
    b = synthesize_openclaw_adapter(gateway_url=f"wss://ws.{APEX}", service_token="t", user_id="u")
    assert a["devicePrivateKeyPem"] != b["devicePrivateKeyPem"]
    # The minted PEM is a usable ed25519 key whose deviceId derivation works.
    ident = device_identity_from_pem(a["devicePrivateKeyPem"])
    assert len(ident.device_id) == 64  # hex sha256
    assert ident.public_key_b64


def test_reuses_existing_device_key_on_update():
    # On update we MUST pass the agent's existing PEM so the deviceId — and its
    # already-seeded paired.json entry — stay stable. The synth returns it as-is.
    existing = synthesize_openclaw_adapter(gateway_url=f"wss://ws.{APEX}", service_token="t", user_id="u")[
        "devicePrivateKeyPem"
    ]
    updated = synthesize_openclaw_adapter(
        gateway_url=f"wss://ws.{APEX}",
        service_token="t2",
        user_id="u",
        device_private_key_pem=existing,
    )
    assert updated["devicePrivateKeyPem"] == existing


def test_claimed_api_key_path_is_per_agent_plural_workspace():
    # per-agent workspace-{agentId}/ — the agent's own cwd; the singular
    # ~/.openclaw/workspace is container-global and collides across agents.
    assert claimed_api_key_path("agent-123") == "~/.openclaw/workspace-agent-123/paperclip-claimed-api-key.json"
    # Distinct per agent.
    assert claimed_api_key_path("a") != claimed_api_key_path("b")


def test_synthesize_omits_claimed_path_when_no_agent_id():
    """Create-time callers don't know the agent id yet; the per-agent path is
    applied post-create. With no agent_id the wire shape is unchanged."""
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://ws.{APEX}",
        service_token="t",
        user_id="u",
    )
    assert "claimedApiKeyPath" not in cfg
    assert "agentId" not in cfg


def test_synthesize_sets_per_agent_claimed_path_when_agent_id_given():
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://ws.{APEX}",
        service_token="t",
        user_id="u",
        agent_id="agent-xyz",
    )
    assert cfg["claimedApiKeyPath"] == "~/.openclaw/workspace-agent-xyz/paperclip-claimed-api-key.json"


def test_synthesize_sets_agent_id_so_runs_route_to_that_agent():
    """The adapter reads the target agent from config, not from the run's agent
    record (openclaw-gateway execute.ts). Omit it and chat.send carries
    no agent, so OpenClaw falls back to the default (ceo) agent — every agent's
    run then executes as Friday. It also drives the agent:<id>: sessionKey prefix
    that per-agent session isolation and usage attribution depend on.
    """
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://ws.{APEX}",
        service_token="t",
        user_id="u",
        agent_id="agent-xyz",
    )
    assert cfg["agentId"] == "agent-xyz"


def test_fallback_session_key_cannot_collide_with_the_owners_chat_session():
    """REGRESSION: the no-issue fallback key must not equal the /chat General key.

    Once ``agentId`` is set the adapter prefixes the fallback sessionKey with
    ``agent:<agentId>:`` (openclaw-gateway execute.ts). A bare
    ``user_id`` fallback therefore resolves to ``agent:<agentId>:<user_id>`` —
    exactly ``build_chat_session_key(agent_id, user_id, None)``. Every heartbeat
    and manual wake would then be appended to the owner's real /chat transcript,
    load its history as context, and stream into their open chat window.
    """
    from core.gateway.connection_pool import _parse_session_key
    from routers.websocket_chat import build_chat_session_key

    agent_id, user_id = "agent-xyz", "user_2abc"
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://ws.{APEX}",
        service_token="t",
        user_id=user_id,
        agent_id=agent_id,
    )
    # What the adapter will actually put on the wire for a no-issue wake.
    resolved = f"agent:{agent_id}:{cfg['sessionKey']}"

    assert resolved != build_chat_session_key(agent_id, user_id, None)
    # And it must classify as a Paperclip session, so _handle_message treats it
    # as isolated and never fans it out to the member's /chat socket.
    parsed = _parse_session_key(resolved)
    assert parsed["source"] == "paperclip"
    assert parsed["agent_id"] == agent_id
