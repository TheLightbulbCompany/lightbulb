import pytest

from core.config import settings
from core.crypto.operator_device import device_identity_from_pem
from core.services.paperclip_adapter_config import (
    synthesize_openclaw_adapter,
    validate_gateway_url,
    AdapterConfigError,
    OPENCLAW_GATEWAY_TYPE,
    PAPERCLIP_OPERATOR_CLIENT_ID,
    claimed_api_key_path,
)


def test_canonical_adapter_type_is_underscored():
    assert OPENCLAW_GATEWAY_TYPE == "openclaw_gateway"


@pytest.mark.parametrize(
    "url",
    [
        "wss://ws.isol8.co",
        "wss://ws-dev.isol8.co",
        "wss://ws-staging.isol8.co",
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
        "wss://ws.isol8.com.evil",
        "wss://169.254.169.254",
        "wss://ws.isol8.co/path",
        "wss://ws-dev.isol8.co.evil",
        "ws://attacker.local:8000",
        "wss://ws.isol8.co\n",
        "wss://ws.isol8.co\r\n",
        "",
        None,
    ],
)
def test_validate_gateway_url_rejects_unknown_formats(url):
    with pytest.raises(AdapterConfigError):
        validate_gateway_url(url)


def test_synthesize_returns_canonical_shape(monkeypatch):
    # With no internal URL configured, no paperclipApiUrl override is added.
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "")
    cfg = synthesize_openclaw_adapter(
        gateway_url="wss://ws-dev.isol8.co",
        service_token="JWT_TOKEN_HERE",
        user_id="user_123",
    )
    # Token must live in the URL query string — the Isol8 WS API Gateway
    # authorizer reads it from `route.request.querystring.token`. clientId routes
    # the connection to the operator relay at $connect.
    assert cfg["url"] == "wss://ws-dev.isol8.co?token=JWT_TOKEN_HERE&clientId=paperclip-operator"
    # authToken retained for adapter compatibility (header is ignored by API GW).
    assert cfg["authToken"] == "JWT_TOKEN_HERE"
    assert cfg["sessionKeyStrategy"] == "issue"
    assert cfg["sessionKey"] == "user_123"
    assert cfg["devicePrivateKeyPem"].startswith("-----BEGIN PRIVATE KEY-----")
    assert set(cfg.keys()) == {
        "url",
        "authToken",
        "sessionKeyStrategy",
        "sessionKey",
        "devicePrivateKeyPem",
    }


def test_synthesize_pins_paperclip_api_url_to_internal_host(monkeypatch):
    """When PAPERCLIP_INTERNAL_URL is configured, the adapter carries a
    ``paperclipApiUrl`` override so the agent's wake run curls the in-VPC host
    instead of Paperclip's default origin(PAPERCLIP_PUBLIC_URL) — which is the
    public auth origin, unreachable from inside the VPC."""
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "http://paperclip.isol8-prod.local:3100")
    cfg = synthesize_openclaw_adapter(
        gateway_url="wss://ws.isol8.co",
        service_token="t" * 40,
        user_id="user_abc",
    )
    assert cfg["paperclipApiUrl"] == "http://paperclip.isol8-prod.local:3100"


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
            gateway_url="wss://ws-dev.isol8.co",
            service_token="",
            user_id="u",
        )


def test_synthesize_rejects_empty_user_id():
    with pytest.raises(AdapterConfigError):
        synthesize_openclaw_adapter(
            gateway_url="wss://ws-dev.isol8.co",
            service_token="x",
            user_id="",
        )


def test_strategy_is_issue_for_per_issue_session_isolation():
    """We want each issue assigned to an agent to be its own OpenClaw
    session so transcripts don't bleed across unrelated issues. The
    sessionKey field becomes the fallback for manual wakes (no issue
    context); the issue-driven path uses paperclip:issue:<id>."""
    cfg = synthesize_openclaw_adapter(
        gateway_url="wss://ws.isol8.co",
        service_token="t" * 100,
        user_id="user_abc",
    )
    assert cfg["sessionKeyStrategy"] == "issue"
    assert cfg["sessionKey"] == "user_abc"
    assert cfg["url"] == "wss://ws.isol8.co?token=" + "t" * 100 + "&clientId=paperclip-operator"
    assert cfg["authToken"] == "t" * 100


def test_url_routes_to_operator_relay_via_client_id():
    cfg = synthesize_openclaw_adapter(gateway_url="wss://ws.isol8.co", service_token="tok", user_id="u")
    assert f"clientId={PAPERCLIP_OPERATOR_CLIENT_ID}" in cfg["url"]
    assert PAPERCLIP_OPERATOR_CLIENT_ID == "paperclip-operator"


def test_mints_a_fresh_device_key_per_agent_when_absent():
    # Each create with no existing PEM gets its OWN keypair (stable per agent,
    # distinct across agents) — required so paired.json entries don't collide.
    a = synthesize_openclaw_adapter(gateway_url="wss://ws.isol8.co", service_token="t", user_id="u")
    b = synthesize_openclaw_adapter(gateway_url="wss://ws.isol8.co", service_token="t", user_id="u")
    assert a["devicePrivateKeyPem"] != b["devicePrivateKeyPem"]
    # The minted PEM is a usable ed25519 key whose deviceId derivation works.
    ident = device_identity_from_pem(a["devicePrivateKeyPem"])
    assert len(ident.device_id) == 64  # hex sha256
    assert ident.public_key_b64


def test_reuses_existing_device_key_on_update():
    # On update we MUST pass the agent's existing PEM so the deviceId — and its
    # already-seeded paired.json entry — stay stable. The synth returns it as-is.
    existing = synthesize_openclaw_adapter(gateway_url="wss://ws.isol8.co", service_token="t", user_id="u")[
        "devicePrivateKeyPem"
    ]
    updated = synthesize_openclaw_adapter(
        gateway_url="wss://ws.isol8.co",
        service_token="t2",
        user_id="u",
        device_private_key_pem=existing,
    )
    assert updated["devicePrivateKeyPem"] == existing


def test_claimed_api_key_path_is_per_agent_plural_workspace():
    # PLURAL workspaces/{agentId}/ — the agent's own cwd; the singular
    # ~/.openclaw/workspace is container-global and collides across agents.
    assert claimed_api_key_path("agent-123") == "~/.openclaw/workspaces/agent-123/paperclip-claimed-api-key.json"
    # Distinct per agent.
    assert claimed_api_key_path("a") != claimed_api_key_path("b")


def test_synthesize_omits_claimed_path_when_no_agent_id():
    """Create-time callers don't know the agent id yet; the per-agent path is
    applied post-create. With no agent_id the wire shape is unchanged."""
    cfg = synthesize_openclaw_adapter(
        gateway_url="wss://ws.isol8.co",
        service_token="t",
        user_id="u",
    )
    assert "claimedApiKeyPath" not in cfg


def test_synthesize_sets_per_agent_claimed_path_when_agent_id_given():
    cfg = synthesize_openclaw_adapter(
        gateway_url="wss://ws.isol8.co",
        service_token="t",
        user_id="u",
        agent_id="agent-xyz",
    )
    assert cfg["claimedApiKeyPath"] == "~/.openclaw/workspaces/agent-xyz/paperclip-claimed-api-key.json"
