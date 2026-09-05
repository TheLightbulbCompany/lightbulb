"""Shared test helpers for tests/unit/containers/."""

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


def minimal_build_kwargs() -> dict:
    """Minimal required kwargs for ``build_openclaw_config_dict`` — a paid
    ``bedrock_claude`` provider so ``is_bedrock_trial`` is False."""
    return {
        "user_id": "u_1",
        "gateway_token": "test-token",
        "provider_choice": "bedrock_claude",
        "aip_arns": _AIPS,
    }
