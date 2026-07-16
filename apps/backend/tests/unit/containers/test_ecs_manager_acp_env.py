"""ACP coding-harness Bedrock env: points Claude Code at the owner's AIP ARNs."""

from unittest.mock import patch

from core.containers.ecs_manager import _build_bedrock_acp_harness_env

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877:application-inference-profile/aip-haiku",
}


def _env_map(**kwargs) -> dict[str, str]:
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        env = _build_bedrock_acp_harness_env(_AIPS, **kwargs)
    return {e["name"]: e["value"] for e in env}


def test_enables_bedrock_for_claude_code():
    assert _env_map()["CLAUDE_CODE_USE_BEDROCK"] == "1"


def test_region_set_explicitly():
    # Claude Code's AWS SDK resolves region from env; base task def omits it.
    assert _env_map()["AWS_REGION"] == "us-east-1"


def test_models_point_at_owner_aip_arns_not_base_ids():
    """Billing is AIP-attributed and IAM grants application-inference-profile/*
    only — the model env MUST be the owner's AIP ARN, sonnet=main / haiku=fast."""
    env = _env_map()
    assert env["ANTHROPIC_MODEL"] == _AIPS["sonnet"]
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == _AIPS["haiku"]
    assert env["ANTHROPIC_MODEL"].startswith("arn:aws:bedrock:")


def test_no_gateway_vars_by_default():
    """Without the gateway cutover, the harness talks to Bedrock directly — no
    ANTHROPIC_BEDROCK_BASE_URL / skip-auth / auth-token."""
    env = _env_map()
    for k in ("ANTHROPIC_BEDROCK_BASE_URL", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "ANTHROPIC_AUTH_TOKEN"):
        assert k not in env


def test_gateway_vars_added_with_url_and_token():
    """With a gate URL + token, Claude Code routes through the gate with the SAME per-owner
    bearer token as the OpenClaw provider (ANTHROPIC_BEDROCK_BASE_URL binding —
    AWS-SDK endpoint vars don't work; skip its SigV4; token as auth header)."""
    env = _env_map(gate_token="TOK", gate_url="https://gate/")
    assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "https://gate/"
    assert env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "TOK"
    # The direct-mode vars are still present (model routing is unchanged).
    assert env["ANTHROPIC_MODEL"] == _AIPS["sonnet"]


def test_gateway_vars_omitted_without_url_or_token():
    # Guard: break-glass/bootstrap direct mode (no URL) or a missing token must
    # not emit a half-configured gateway env.
    assert "ANTHROPIC_BEDROCK_BASE_URL" not in _env_map(gate_url="", gate_token="TOK")
    assert "ANTHROPIC_BEDROCK_BASE_URL" not in _env_map(gate_url="https://gate/", gate_token=None)
