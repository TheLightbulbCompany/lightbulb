"""ACP coding-harness Bedrock env: points Claude Code at the owner's AIP ARNs."""

from unittest.mock import patch

from core.containers.ecs_manager import _build_bedrock_acp_harness_env

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877:application-inference-profile/aip-haiku",
}


def _env_map() -> dict[str, str]:
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        env = _build_bedrock_acp_harness_env(_AIPS)
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
