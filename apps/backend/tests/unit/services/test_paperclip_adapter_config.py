import pytest

from core.services.paperclip_adapter_config import (
    synthesize_openclaw_adapter,
    validate_gateway_url,
    AdapterConfigError,
    OPENCLAW_GATEWAY_TYPE,
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


def test_synthesize_returns_canonical_shape():
    cfg = synthesize_openclaw_adapter(
        gateway_url="wss://ws-dev.isol8.co",
        service_token="JWT_TOKEN_HERE",
        user_id="user_123",
    )
    assert cfg == {
        "url": "wss://ws-dev.isol8.co",
        "authToken": "JWT_TOKEN_HERE",
        "sessionKeyStrategy": "issue",
        "sessionKey": "user_123",
    }


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
    assert cfg["url"] == "wss://ws.isol8.co"
    assert cfg["authToken"] == "t" * 100
