"""Tests for hooks-ingress block rendering in build_openclaw_config_dict.

T3 of the openclaw-hook-token-provisioning plan: the rendered openclaw.json
must carry a ``hooks`` block that opts in to the gateway's hook ingress with
the OPENCLAW_HOOK_TOKEN env-var placeholder, a 1 MB body cap, and the safe
``allowedSessionKeyPrefixes`` list.

The single most load-bearing assertion is that ``allowedSessionKeyPrefixes``
includes ``"hook:"``. OpenClaw v2026.5.7 (gateway/hooks.ts:75-95) crashes at
boot when ``defaultSessionKey`` is unset and ``"hook:"`` is missing from the
allowed list — every newly-provisioned container would refuse to start.
"""

import json

from core.containers.config import build_openclaw_config_dict


def _cfg(**kwargs) -> dict:
    """Build a config dict with sensible test defaults."""
    defaults = {
        "user_id": "u_1",
        "gateway_token": "gw-token",
        "hook_token": "hk-token",
        "provider_choice": "bedrock_claude",
    }
    defaults.update(kwargs)
    return build_openclaw_config_dict(**defaults)


def test_hooks_block_renders_enabled_with_token_var():
    cfg = _cfg()
    assert cfg["hooks"]["enabled"] is True
    # The literal ``${OPENCLAW_HOOK_TOKEN}`` placeholder is what OpenClaw
    # expands at boot from the env var injected by the per-user ECS task
    # definition (T2). The plaintext ``hook_token`` is NEVER written into
    # this file.
    assert cfg["hooks"]["token"] == "${OPENCLAW_HOOK_TOKEN}"
    assert cfg["hooks"]["path"] == "/hooks"


def test_hooks_max_body_bytes_overrides_upstream_64kb_default():
    """Stripe/GitHub payloads regularly exceed 64KB; we must override.

    Upstream's pre-auth body cap defaults to 64 KB which is far too small
    for real-world webhook traffic. 1 MB matches the upper bound documented
    by the largest providers (Stripe, GitHub) we plan to ingest from.
    """
    cfg = _cfg()
    assert cfg["hooks"]["maxBodyBytes"] == 1_048_576  # 1 MB


def test_hooks_session_key_safety_settings():
    cfg = _cfg()
    assert cfg["hooks"]["allowRequestSessionKey"] is False
    prefixes = cfg["hooks"]["allowedSessionKeyPrefixes"]
    assert "isol8-" in prefixes
    # OpenClaw v2026.5.7 (gateway/hooks.ts:75-95) crashes at boot when
    # defaultSessionKey is unset and "hook:" is not in the allowed list.
    # Without this prefix every newly-provisioned container refuses to start.
    # DO NOT WEAKEN OR REMOVE THIS ASSERTION.
    assert "hook:" in prefixes


def test_hooks_internal_block_preserved():
    """The new ingress block must coexist with the existing ``hooks.internal``."""
    cfg = _cfg()
    assert "internal" in cfg["hooks"]
    assert cfg["hooks"]["internal"]["entries"]["session-memory"]["enabled"] is True
    assert cfg["hooks"]["internal"]["entries"]["command-logger"]["enabled"] is True


def test_hook_token_param_does_not_leak_into_rendered_config():
    """Tripwire: hook_token MUST NOT be interpolated into the rendered config.

    The literal ``${OPENCLAW_HOOK_TOKEN}`` placeholder is what OpenClaw
    expands at boot from the ECS task-def env var (Task 2). If a future
    engineer "fixes" the unused hook_token parameter by writing
    ``"token": hook_token`` instead of the placeholder string, the plaintext
    token would land on EFS — a security regression. This test catches that
    by asserting the rendered config is byte-identical regardless of the
    hook_token value, AND that the secret value never appears anywhere in
    the serialized output.
    """
    secret = "some-very-secret-token-do-not-leak"
    cfg_with = build_openclaw_config_dict(
        user_id="u_1",
        gateway_token="gw-token",
        provider_choice="bedrock_claude",
        hook_token=secret,
    )
    cfg_without = build_openclaw_config_dict(
        user_id="u_1",
        gateway_token="gw-token",
        provider_choice="bedrock_claude",
        hook_token=None,
    )
    # Byte-identical: hook_token has zero observable effect on the rendered
    # config. The placeholder is what's rendered, not the token value.
    assert cfg_with == cfg_without
    # Belt-and-suspenders: the secret string must not appear anywhere in the
    # serialized output (catches even a hypothetical embed under a new key).
    serialized = json.dumps(cfg_with)
    assert secret not in serialized
