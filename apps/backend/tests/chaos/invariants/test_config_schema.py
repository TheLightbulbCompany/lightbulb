"""Config-brick invariants over build_openclaw_config_dict.

Two properties:
  * test_builder_never_raises — for any valid provider/owner combo the builder
    must not raise and must return a non-empty dict. Runs ALWAYS (no bundle
    needed); catches the "builder raises / emits half-built config" brick slice.
  * test_emitted_config_passes_upstream_schema — the emitted JSON validates
    against upstream's zod schema. Requires the vendored bundle
    (scripts/vendor_openclaw_schema.sh); auto-skips at collection when absent."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings

from core.containers.config import build_openclaw_config_dict
from core.services.config_schema import _SCHEMA_PATHS, _normalized_for_validation
from tests.chaos import strategies as ss

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "_schema"
_VALIDATE = _SCHEMA_DIR / "validate.mjs"
_LEGACY_ID = "2026.8.1-isol8.3"
_IMAGE_QUALIFICATION_ID = "2026.9.3-isol8.2"


def _bundle_path(schema_id: str) -> Path:
    if schema_id == _LEGACY_ID:
        return _SCHEMA_DIR / "openclaw-config-schema.mjs"
    return _SCHEMA_DIR / "releases" / schema_id / "openclaw-config-schema.mjs"


_DUMMY_AIP = {
    "opus": "arn:aws:bedrock:us-east-1:0:inference-profile/opus",
    "sonnet": "arn:aws:bedrock:us-east-1:0:inference-profile/sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:0:inference-profile/haiku",
}
# A trial owner holds exactly ONE AIP — ensure_owner_aips mints one tier and the
# IAM policy pins them to it.
_DUMMY_TRIAL_AIP = {"sonnet": _DUMMY_AIP["sonnet"]}

_AIPS_BY_CHOICE = {
    "bedrock_claude": _DUMMY_AIP,
    "bedrock_trial": _DUMMY_TRIAL_AIP,
}


def _build(
    provider_choice: str,
    byo_provider: str,
    owner_type: str,
    paperclip_agents: list[dict] | None = None,
    schema_id: str = _LEGACY_ID,
    acp_enabled: bool = True,
    include_mcp: bool = True,
) -> dict:
    return build_openclaw_config_dict(
        user_id="user_chaos_0",
        gateway_token="tok_chaos",
        provider_choice=provider_choice,
        byo_provider=byo_provider if provider_choice == "byo_key" else None,
        owner_type=owner_type,
        aip_arns=_AIPS_BY_CHOICE.get(provider_choice),
        paperclip_agents=paperclip_agents,
        deployed_tag=schema_id,
        # Production write_openclaw_config defaults ACP on. Keep schema and
        # built-image fixtures on that same path rather than validating the
        # builder's lower-level, opt-out default.
        acp_enabled=acp_enabled,
        # An mcp block is what makes mcp.runtimeScope reachable; without one the
        # key is never written and the schema never sees it.
        integration_mcp_servers=(
            {"stripe": {"command": "npx", "args": ["-y", "@stripe/mcp"]}} if include_mcp else None
        ),
    )


@pytest.mark.chaos
@settings(max_examples=50)
@given(
    provider_choice=ss.provider_choices,
    byo_provider=ss.byo_providers,
    owner_type=ss.owner_types,
    paperclip_agents=ss.paperclip_rosters,
)
def test_builder_never_raises(provider_choice, byo_provider, owner_type, paperclip_agents):
    config = _build(provider_choice, byo_provider, owner_type, paperclip_agents)
    assert isinstance(config, dict) and config, "builder returned empty/non-dict config"


@pytest.mark.chaos
@pytest.mark.parametrize("schema_id", tuple(_SCHEMA_PATHS))
@pytest.mark.parametrize(
    "provider_choice,byo_provider,owner_type,paperclip_agents",
    [
        pytest.param("bedrock_trial", "anthropic", "personal", [], id="trial"),
        pytest.param(
            "bedrock_claude",
            "anthropic",
            "org",
            [
                {"id": "agent_friday", "name": "Friday", "role": "ceo"},
                {"id": "agent_atlas", "name": "Atlas", "role": "general"},
            ],
            id="managed-multi-agent",
        ),
        pytest.param("byo_key", "openai", "org", [], id="byo"),
        pytest.param("chatgpt_oauth", "anthropic", "personal", [], id="chatgpt-oauth"),
    ],
)
def test_emitted_config_passes_selected_schema(schema_id, provider_choice, byo_provider, owner_type, paperclip_agents):
    bundle = _bundle_path(schema_id)
    assert bundle.is_file(), f"registered schema bundle is missing: {bundle}"
    config = _build(provider_choice, byo_provider, owner_type, paperclip_agents, schema_id)
    assert config["mcp"]["runtimeScope"] == "shared"
    assert config["browser"]["sessionState"]["enabled"] is True
    assert any(entry.get("enabled") is True for entry in config["plugins"]["entries"].values())
    # OpenClaw NEVER safeParses a raw on-disk config: validateConfigObject
    # (validation-core.ts:423) runs migratePersistedImplicitMainRoster FIRST,
    # converting the legacy agents.list[] roster into 8.1's agents.entries map,
    # then re-projects a list view onto the result. The backend still writes the
    # list shape, so parsing raw is STRICTER than the loader and rejects a
    # config every container boots fine -- which is exactly what it did when the
    # base moved to 8.1. Validate the migrated form, through the same mirror the
    # production write-gate uses, so the harness and the gate agree.
    proc = subprocess.run(
        ["node", str(_VALIDATE), str(bundle)],
        input=json.dumps(_normalized_for_validation(config)),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"Emitted openclaw.json failed selected schema {schema_id}:\n{proc.stderr}"


def _emit_image_qualification_configs(output_dir: Path) -> None:
    """Emit hermetic production-shape configs for the built-image CI gate."""
    cases = {
        "bedrock.json": (
            "bedrock_claude",
            "anthropic",
            "org",
            [
                {"id": "agent_friday", "name": "Friday", "role": "ceo"},
                {"id": "agent_atlas", "name": "Atlas", "role": "general"},
            ],
        ),
        "byo-openai.json": ("byo_key", "openai", "personal", []),
        "chatgpt-oauth.json": ("chatgpt_oauth", "anthropic", "personal", []),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, (provider_choice, byo_provider, owner_type, paperclip_agents) in cases.items():
        config = _build(
            provider_choice,
            byo_provider,
            owner_type,
            paperclip_agents,
            _IMAGE_QUALIFICATION_ID,
            include_mcp=False,
        )
        assert set(config["channels"]) == {"lightbulb"}, (
            "qualification fixtures must enable only the native Lightbulb channel"
        )
        assert config["acp"]["enabled"] is True, "qualification fixtures must use the production ACP default"
        (output_dir / filename).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} OUTPUT_DIR")
    _emit_image_qualification_configs(Path(sys.argv[1]))
