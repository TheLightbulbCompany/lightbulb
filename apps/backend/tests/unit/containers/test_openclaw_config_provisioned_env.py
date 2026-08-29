import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.containers.config import build_openclaw_config_dict  # noqa: E402


# bedrock_claude refuses to build without per-owner AIP ARNs (config.py:656).
_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


def _cfg(**kw):
    return build_openclaw_config_dict(
        user_id="org_a", gateway_token="gwt", provider_choice="bedrock_claude", aip_arns=_AIPS, **kw
    )


def test_provisioned_env_lands_in_env_vars():
    cfg = _cfg(provisioned_env={"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"})
    assert cfg["env"]["vars"]["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"] == "phc_live_abc"


def test_absent_provisioned_env_writes_no_env_key():
    cfg = _cfg()
    assert (cfg.get("env") or {}).get("vars") in (None, {})
