import re

import pytest

from core.config import apex_domain, settings, url_host
from core.crypto.operator_device import device_identity_from_pem
from core.services import paperclip_adapter_config as cfg_module
from core.services.paperclip_adapter_config import (
    build_gateway_url_re,
    synthesize_openclaw_adapter,
    validate_gateway_url,
    AdapterConfigError,
    HTTP_ADAPTER_TYPE,
    OPENCLAW_GATEWAY_TYPE,
    PAPERCLIP_OPERATOR_CLIENT_ID,
    claimed_api_key_path,
)


# The allowlist apex is DERIVED from settings.GATEWAY_RELAY_URL (see
# build_gateway_url_re), so these tests must derive it too. Hardcoding a domain
# here passed only while the configured domain happened to match the literal —
# the moment the product domain moved, 21 tests went red on main.
APEX = apex_domain(url_host(settings.GATEWAY_RELAY_URL))


def test_canonical_adapter_type_is_underscored():
    assert OPENCLAW_GATEWAY_TYPE == "openclaw_gateway"


@pytest.mark.parametrize(
    "url",
    [
        f"wss://bv.{APEX}/api/v1/gateway/ws",
        f"wss://bv-dev.{APEX}/api/v1/gateway/ws",
        f"wss://bv-staging.{APEX}/api/v1/gateway/ws",
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
        f"wss://bv.{APEX}.evil/api/v1/gateway/ws",
        "wss://169.254.169.254",
        f"wss://bv.{APEX}/api/v1/gateway/ws/extra",
        f"wss://bv-dev.{APEX}.evil/api/v1/gateway/ws",
        "ws://attacker.local:8000",
        f"wss://bv.{APEX}/api/v1/gateway/ws\n",
        f"wss://bv.{APEX}/api/v1/gateway/ws\r\n",
        # The retired API Gateway relay door — no longer allowlisted.
        f"wss://ws-dev.{APEX}",
        "",
        None,
    ],
)
def test_validate_gateway_url_rejects_unknown_formats(url):
    with pytest.raises(AdapterConfigError):
        validate_gateway_url(url)


def test_gateway_allowlist_follows_the_configured_gateway_host():
    """The allowlist tracks GATEWAY_RELAY_URL's apex, so moving the product
    domain is a config change, not a code change. It is fail-CLOSED: an
    unconfigured gateway allows localhost only, never a looser hosted
    pattern."""
    live = build_gateway_url_re(f"wss://bv-dev.{APEX}/api/v1/gateway/ws")
    assert live.pattern == (rf"\A(?:wss://bv(?:-[a-z]+)?\.{re.escape(APEX)}/api/v1/gateway/ws|ws://localhost:[0-9]+)\Z")

    moved = build_gateway_url_re("wss://bv.example.ai/api/v1/gateway/ws")
    assert moved.match("wss://bv.example.ai/api/v1/gateway/ws")
    assert moved.match("wss://bv-dev.example.ai/api/v1/gateway/ws")
    assert moved.match("ws://localhost:8000")
    assert not moved.match(f"wss://bv.{APEX}/api/v1/gateway/ws")
    assert not moved.match("wss://bv.example.ai.evil/api/v1/gateway/ws")

    unset = build_gateway_url_re("")
    assert unset.match("ws://localhost:8000")
    assert not unset.match(f"wss://bv.{APEX}/api/v1/gateway/ws")
    assert not unset.match("wss://bv.")


def test_synthesize_returns_canonical_shape(monkeypatch):
    # With no internal URL configured, no paperclipApiUrl override is added.
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "")
    monkeypatch.setattr(settings, "PUBLIC_API_BASE", settings.PUBLIC_API_BASE)
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://bv-dev.{APEX}/api/v1/gateway/ws",
        service_token="JWT_TOKEN_HERE",
        user_id="user_123",
    )
    # Token must live in the URL query string — the gateway relay's upgrade
    # auth reads it from there. clientId tells the relay to admit this
    # connection as an operator.
    assert cfg["url"] == f"wss://bv-dev.{APEX}/api/v1/gateway/ws?token=JWT_TOKEN_HERE&clientId=paperclip-operator"
    # authToken retained for adapter compatibility (the duplicate header is harmless).
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
        gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws",
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
            gateway_url=f"wss://bv-dev.{APEX}/api/v1/gateway/ws",
            service_token="",
            user_id="u",
        )


def test_synthesize_rejects_empty_user_id():
    with pytest.raises(AdapterConfigError):
        synthesize_openclaw_adapter(
            gateway_url=f"wss://bv-dev.{APEX}/api/v1/gateway/ws",
            service_token="x",
            user_id="",
        )


def test_strategy_is_issue_for_per_issue_session_isolation():
    """We want each issue assigned to an agent to be its own OpenClaw
    session so transcripts don't bleed across unrelated issues. The
    sessionKey field becomes the fallback for manual wakes (no issue
    context); the issue-driven path uses paperclip:issue:<id>."""
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws",
        service_token="t" * 100,
        user_id="user_abc",
    )
    assert cfg["sessionKeyStrategy"] == "issue"
    assert cfg["sessionKey"] == "paperclip:user_abc"
    assert cfg["url"] == f"wss://bv.{APEX}/api/v1/gateway/ws?token=" + "t" * 100 + "&clientId=paperclip-operator"
    assert cfg["authToken"] == "t" * 100


def test_url_routes_to_operator_relay_via_client_id():
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws", service_token="tok", user_id="u"
    )
    assert f"clientId={PAPERCLIP_OPERATOR_CLIENT_ID}" in cfg["url"]
    assert PAPERCLIP_OPERATOR_CLIENT_ID == "paperclip-operator"


def test_mints_a_fresh_device_key_per_agent_when_absent():
    # Each create with no existing PEM gets its OWN keypair (stable per agent,
    # distinct across agents) — required so paired.json entries don't collide.
    a = synthesize_openclaw_adapter(gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws", service_token="t", user_id="u")
    b = synthesize_openclaw_adapter(gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws", service_token="t", user_id="u")
    assert a["devicePrivateKeyPem"] != b["devicePrivateKeyPem"]
    # The minted PEM is a usable ed25519 key whose deviceId derivation works.
    ident = device_identity_from_pem(a["devicePrivateKeyPem"])
    assert len(ident.device_id) == 64  # hex sha256
    assert ident.public_key_b64


def test_reuses_existing_device_key_on_update():
    # On update we MUST pass the agent's existing PEM so the deviceId — and its
    # already-seeded paired.json entry — stay stable. The synth returns it as-is.
    existing = synthesize_openclaw_adapter(
        gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws", service_token="t", user_id="u"
    )["devicePrivateKeyPem"]
    updated = synthesize_openclaw_adapter(
        gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws",
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
        gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws",
        service_token="t",
        user_id="u",
    )
    assert "claimedApiKeyPath" not in cfg
    assert "agentId" not in cfg


def test_synthesize_sets_per_agent_claimed_path_when_agent_id_given():
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws",
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
        gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws",
        service_token="t",
        user_id="u",
        agent_id="agent-xyz",
    )
    assert cfg["agentId"] == "agent-xyz"


def test_fallback_session_key_cannot_collide_with_a_human_general_session():
    """The no-issue Paperclip fallback must not equal a human General key.

    Once ``agentId`` is set the adapter prefixes the fallback sessionKey with
    ``agent:<agentId>:`` (openclaw-gateway execute.ts). A bare ``user_id``
    fallback would therefore collide with ``agent:<agentId>:<user_id>`` and
    load a human's private session history as Paperclip context.
    """
    from core.services.session_keys import parse_session_key as _parse_session_key

    agent_id, user_id = "agent-xyz", "user_2abc"
    cfg = synthesize_openclaw_adapter(
        gateway_url=f"wss://bv.{APEX}/api/v1/gateway/ws",
        service_token="t",
        user_id=user_id,
        agent_id=agent_id,
    )
    # What the adapter will actually put on the wire for a no-issue wake.
    resolved = f"agent:{agent_id}:{cfg['sessionKey']}"

    assert resolved != f"agent:{agent_id}:{user_id}"
    # It classifies as a Paperclip session rather than human-owned runtime state.
    parsed = _parse_session_key(resolved)
    assert parsed["source"] == "paperclip"
    assert parsed["agent_id"] == agent_id


def test_an_agentcore_owner_gets_the_http_adapter_not_the_openclaw_gateway():
    """Hermes has no gateway for an operator socket to connect to -- an
    AgentCore-hosted owner's Paperclip agents get the fork's own first-party
    `http` webhook adapter instead (fork docs/adapters/http.md,
    confirmed against cli/src/adapters/registry.ts at v2026.813-isol8.6).
    Absent/unknown `runtime_host` stays the pre-migration ECS default."""
    assert cfg_module.adapter_type_for({"runtime_host": "agentcore"}) == HTTP_ADAPTER_TYPE
    assert cfg_module.adapter_type_for({"runtime_host": "ecs"}) == OPENCLAW_GATEWAY_TYPE
    assert cfg_module.adapter_type_for({}) == OPENCLAW_GATEWAY_TYPE
    assert cfg_module.adapter_type_for(None) == OPENCLAW_GATEWAY_TYPE


def test_the_http_adapter_config_points_at_the_backend_door_with_the_hook_token(monkeypatch):
    # NOTE: the brief for this task named the setting `settings.API_URL`; the
    # actual field (core/config.py) is `PUBLIC_API_BASE` -- the same "public
    # origin of this API" setting every other absolute-URL builder in this repo
    # uses (routers/hooks_credentials.py, routers/connections.py, ...).
    monkeypatch.setattr(settings, "PUBLIC_API_BASE", "https://api-dev.flickmeon.ai")
    built = cfg_module.build_http_adapter_config("org_1", "ceo", hook_token="hook-abc")
    assert built["url"] == "https://api-dev.flickmeon.ai/api/v1/internal/paperclip/runs/org_1"
    assert built["headers"]["Authorization"] == "Bearer hook-abc"
    assert built["headers"]["X-Lightbulb-Agent"] == "ceo"
    assert built["timeoutSec"] == 900


def test_the_http_adapter_config_strips_a_trailing_slash_on_the_base_url(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_API_BASE", "https://api-dev.flickmeon.ai/")
    built = cfg_module.build_http_adapter_config("org_1", "ceo", hook_token="hook-abc")
    assert built["url"] == "https://api-dev.flickmeon.ai/api/v1/internal/paperclip/runs/org_1"
