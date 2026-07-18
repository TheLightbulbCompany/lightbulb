"""Unit tests for the reapply-openclaw-config ops script.

The AWS write itself (``write_user_configs`` -> EFS) is a thin wrapper
exercised manually with ``--apply`` and real creds. What matters here is the
dry-run-vs-apply gate (a dry run resolves everything but NEVER writes) and
the runtime-state preservation: the full regen renders a fresh config that
omits any subtree the generator does not emit, so the script must re-add
those dropped subtrees via a deep-merge patch — otherwise linked channel
DMs (and any other runtime-appended config) stop working until re-pair.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scripts.reapply_openclaw_config as mod


def test_module_imports_and_exposes_main():
    assert hasattr(mod, "main")


def test_argparse_parses_owner_env_apply():
    parser = mod.argparse.ArgumentParser()
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--apply", action="store_true")
    # Mirror the script's own parser shape; assert the flags parse as expected.
    args = parser.parse_args(["--owner-id", "org_x", "--env", "prod", "--apply"])
    assert args.owner_id == "org_x"
    assert args.env == "prod"
    assert args.apply is True

    args = parser.parse_args(["--owner-id", "user_y", "--env", "dev"])
    assert args.owner_id == "user_y"
    assert args.env == "dev"
    assert args.apply is False


# --------------------------------------------------------------------------- #
# _runtime_only_delta() — pure deep-diff of "what the regen dropped"
# --------------------------------------------------------------------------- #


def test_runtime_only_delta_keeps_keys_absent_from_fresh():
    """A top-level subtree present in existing but absent from the fresh
    regen (e.g. runtime-appended ``identity``) is preserved verbatim."""
    existing = {"identity": {"runtimeOnly": "keep"}, "gateway": {"mode": "token"}}
    fresh = {"gateway": {"mode": "token"}}
    delta = mod._runtime_only_delta(existing, fresh)
    assert delta == {"identity": {"runtimeOnly": "keep"}}


def test_runtime_only_delta_keeps_nested_keys_the_regen_omits():
    """Within a subtree the regen DOES emit (``channels.slack``), only the
    nested keys the regen omits (``accounts``) are carried into the delta —
    the regenerated siblings (``enabled``/``dmPolicy``) are not, so the
    deep-merge can never revert them."""
    existing = {
        "channels": {
            "slack": {
                "enabled": True,
                "dmPolicy": "pairing",
                "accounts": {"agent_friday": {"allowFrom": ["U123"]}},
            }
        }
    }
    fresh = {"channels": {"slack": {"enabled": True, "dmPolicy": "pairing"}}}
    delta = mod._runtime_only_delta(existing, fresh)
    assert delta == {"channels": {"slack": {"accounts": {"agent_friday": {"allowFrom": ["U123"]}}}}}


def test_runtime_only_delta_drops_keys_the_regen_owns():
    """Any key the regen DOES set (even with a changed value) is excluded —
    the fresh write is authoritative for it. A scalar the generator owns
    must never leak into the restore patch and revert the regen."""
    existing = {"agents": {"defaults": {"model": {"primary": "stale-old-model"}}}}
    fresh = {"agents": {"defaults": {"model": {"primary": "new-cheaper-model"}}}}
    delta = mod._runtime_only_delta(existing, fresh)
    assert delta == {}


def test_runtime_only_delta_empty_when_fresh_superset():
    existing = {"gateway": {"mode": "token"}}
    fresh = {"gateway": {"mode": "token"}, "models": {"providers": {}}}
    assert mod._runtime_only_delta(existing, fresh) == {}


def test_runtime_only_delta_handles_missing_existing():
    assert mod._runtime_only_delta(None, {"a": 1}) == {}
    assert mod._runtime_only_delta({}, {"a": 1}) == {}


def test_runtime_only_delta_does_not_descend_into_lists():
    """A list the existing config carries but the regen omits is preserved
    whole; a list the regen owns is left to the fresh write."""
    existing = {"bindings": [{"type": "route"}], "agents": {"list": [{"id": "old"}]}}
    fresh = {"agents": {"list": [{"id": "new"}]}}
    delta = mod._runtime_only_delta(existing, fresh)
    # bindings preserved (regen omits it); agents.list NOT (regen owns it).
    assert delta == {"bindings": [{"type": "route"}]}


def test_runtime_only_delta_does_not_resurrect_stale_generator_fields():
    """A nested field the CURRENT generator deliberately no longer emits must
    NOT be classified as runtime-only. The canonical case: the pre-#1118
    ``memorySearch`` block pinned ``model`` to Bedrock Titan; the per-provider
    generator emits ``{enabled, provider: "none"}`` with NO ``model`` key.
    Recursing into generator-owned subtrees would merge the stale Titan model
    back over the fresh block — the exact stale state a regen exists to
    clear."""
    existing = {
        "agents": {
            "defaults": {
                "memorySearch": {
                    "enabled": True,
                    "provider": "amazon-bedrock",
                    "model": "amazon.titan-embed-text-v2:0",
                }
            }
        }
    }
    fresh = {"agents": {"defaults": {"memorySearch": {"enabled": True, "provider": "none"}}}}
    assert mod._runtime_only_delta(existing, fresh) == {}


def test_runtime_only_delta_recurses_only_into_co_owned_subtrees():
    """Runtime additions nested inside co-owned subtrees (marketplace
    ``skills.entries``/``allowBundled``, deployed-agent ``mcp.servers``)
    survive the regen; nested keys under any other generator-owned subtree
    (e.g. ``gateway``) do not."""
    existing = {
        "skills": {
            "install": {"nodeManager": "npm"},
            "entries": {"market-skill": {"enabled": True}},
            "allowBundled": ["market-skill"],
        },
        "mcp": {"servers": {"deployed-agent-mcp": {"command": "npx"}}},
        "gateway": {"mode": "token", "staleRuntimeKey": "drop-me"},
    }
    fresh = {
        "skills": {"install": {"nodeManager": "npm"}, "workshop": {"autonomous": {"enabled": True}}},
        "mcp": {"servers": {}},
        "gateway": {"mode": "token"},
    }
    delta = mod._runtime_only_delta(existing, fresh)
    assert delta == {
        "skills": {
            "entries": {"market-skill": {"enabled": True}},
            "allowBundled": ["market-skill"],
        },
        "mcp": {"servers": {"deployed-agent-mcp": {"command": "npx"}}},
    }


# --------------------------------------------------------------------------- #
# main() — dry-run vs apply, with the write seam mocked
# --------------------------------------------------------------------------- #


def _patches(
    *,
    container,
    provider=("bedrock_claude", None),
    existing_config=None,
    fresh_config=None,
    patch_mock=None,
):
    """Patch the collaborators the script lazy-imports inside main.

    Returns (write_user_configs_mock, list_of_patch_context_managers).

    ``existing_config`` is the on-disk config BEFORE the regen (the first
    ``read_openclaw_config_from_efs`` call). ``fresh_config`` is what the
    file holds AFTER ``write_user_configs`` re-renders it (the second read).
    The script diffs the two to compute the runtime-only restore patch and
    applies it via ``patch_openclaw_config`` (``patch_mock``).
    """
    write_mock = AsyncMock()
    ecs_manager = MagicMock()
    ecs_manager.write_user_configs = write_mock

    # read_openclaw_config_from_efs is called twice on --apply: once before
    # the regen (existing) and once after (fresh). On dry-run it is not
    # called at all.
    read_mock = AsyncMock(side_effect=[existing_config, fresh_config])

    cms = [
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value=container),
        ),
        patch(
            "core.services.container_provision.resolve_provider_choice",
            AsyncMock(return_value=provider),
        ),
        patch(
            "core.containers.get_ecs_manager",
            MagicMock(return_value=ecs_manager),
        ),
        patch(
            "core.containers.config.read_openclaw_config_from_efs",
            read_mock,
        ),
        patch(
            "core.services.config_patcher.patch_openclaw_config",
            patch_mock if patch_mock is not None else AsyncMock(),
        ),
    ]
    return write_mock, cms


@pytest.mark.asyncio
async def test_dry_run_does_not_call_write_path():
    write_mock, cms = _patches(
        container={"gateway_token": "gw", "hook_token": "hk", "owner_type": "org"},
    )
    for cm in cms:
        cm.start()
    try:
        rc = await mod.main(apply=False, env="prod", owner_id="org_x")
    finally:
        for cm in cms:
            cm.stop()

    assert rc == 0
    write_mock.assert_not_called()


@pytest.mark.asyncio
async def test_main_sets_environment_from_env_flag(monkeypatch):
    """--env sets ENVIRONMENT so the generator's version-gated keys (e.g.
    mcp.runtimeScope) resolve THIS env's deployed image tag — not dev's via the
    fallback, which could stamp a 6.1-only key onto a still-pre-6.1 prod owner."""
    monkeypatch.setenv("ENVIRONMENT", "dev")  # start wrong; main must correct it
    write_mock, cms = _patches(
        container={"gateway_token": "gw", "hook_token": "hk", "owner_type": "org"},
    )
    for cm in cms:
        cm.start()
    try:
        await mod.main(apply=False, env="prod", owner_id="org_x")
    finally:
        for cm in cms:
            cm.stop()

    assert os.environ["ENVIRONMENT"] == "prod"


@pytest.mark.asyncio
async def test_apply_calls_write_path_with_resolved_inputs():
    write_mock, cms = _patches(
        container={"gateway_token": "gw-tok", "hook_token": "hk-tok", "owner_type": "org"},
        provider=("byo_key", "anthropic"),
        existing_config={},
        fresh_config={},
    )
    for cm in cms:
        cm.start()
    try:
        rc = await mod.main(apply=True, env="prod", owner_id="org_x")
    finally:
        for cm in cms:
            cm.stop()

    assert rc == 0
    write_mock.assert_awaited_once()
    args, kwargs = write_mock.call_args
    # owner_id is positional arg 0; gateway_token positional arg 1.
    assert args[0] == "org_x"
    assert args[1] == "gw-tok"
    assert kwargs["provider_choice"] == "byo_key"
    assert kwargs["byo_provider"] == "anthropic"
    assert kwargs["hook_token"] == "hk-tok"
    assert kwargs["owner_type"] == "org"


@pytest.mark.asyncio
async def test_apply_threads_bedrock_aip_arns_from_container_row():
    """The generator hard-requires aip_arns on BOTH Bedrock paths
    (build_openclaw_config_dict raises without a 'sonnet' entry), and the
    per-owner AIP map is persisted on the containers row at provision time.
    The regen must read it off the row and thread it through — otherwise
    every bedrock_claude/bedrock_trial owner fails the reapply."""
    aips = {"sonnet": "arn:aws:bedrock:us-east-1:1:application-inference-profile/aip-s"}
    write_mock, cms = _patches(
        container={
            "gateway_token": "gw-tok",
            "hook_token": "hk-tok",
            "owner_type": "personal",
            "bedrock_aip_arns": aips,
        },
        provider=("bedrock_claude", None),
        existing_config={},
        fresh_config={},
    )
    for cm in cms:
        cm.start()
    try:
        rc = await mod.main(apply=True, env="prod", owner_id="user_bedrock")
    finally:
        for cm in cms:
            cm.stop()

    assert rc == 0
    _, kwargs = write_mock.call_args
    assert kwargs["aip_arns"] == aips


@pytest.mark.asyncio
async def test_missing_container_aborts_without_write():
    write_mock, cms = _patches(container=None)
    for cm in cms:
        cm.start()
    try:
        rc = await mod.main(apply=True, env="prod", owner_id="org_missing")
    finally:
        for cm in cms:
            cm.stop()

    assert rc == 1
    write_mock.assert_not_called()


@pytest.mark.asyncio
async def test_missing_gateway_token_refuses_even_with_apply():
    """Legacy rows without a gateway_token must be refused — minting a fresh
    token here would diverge from the running task-def and break auth."""
    write_mock, cms = _patches(container={"owner_type": "personal"})
    for cm in cms:
        cm.start()
    try:
        rc = await mod.main(apply=True, env="prod", owner_id="org_legacy")
    finally:
        for cm in cms:
            cm.stop()

    assert rc == 1
    write_mock.assert_not_called()


@pytest.mark.asyncio
async def test_apply_preserves_runtime_state_and_keeps_regen_values():
    """The headline case: the full regen renders a fresh config (correct
    agents.list, new model tiers) that DROPS runtime-appended subtrees
    (channel pairing accounts/allowFrom, route bindings, identity). After
    the regen the script must re-add the dropped subtrees via a deep-merge
    patch while NEVER reverting a regenerated value (the stale model default
    must NOT survive into the patch)."""
    existing = {
        "channels": {
            "slack": {
                "enabled": True,
                "dmPolicy": "pairing",
                "accounts": {
                    "agent_friday": {"allowFrom": ["U123", "U456"]},
                },
            },
        },
        "bindings": [
            {
                "type": "route",
                "agentId": "agent_friday",
                "match": {"channel": "slack", "accountId": "agent_friday"},
            }
        ],
        "identity": {"runtimeOnly": "preserve-me"},
        # A scalar the generator owns, here at its STALE pre-regen value.
        "agents": {"defaults": {"model": {"primary": "stale-old-model"}}},
    }
    # What write_user_configs lands on disk: the new model tier + channel
    # provider defaults, but none of the runtime-appended subtrees.
    fresh = {
        "channels": {"slack": {"enabled": True, "dmPolicy": "pairing"}},
        "agents": {"defaults": {"model": {"primary": "new-cheaper-model"}}},
    }
    restore_patch = AsyncMock()
    write_mock, cms = _patches(
        container={"gateway_token": "gw", "hook_token": "hk", "owner_type": "org"},
        existing_config=existing,
        fresh_config=fresh,
        patch_mock=restore_patch,
    )
    for cm in cms:
        cm.start()
    try:
        rc = await mod.main(apply=True, env="prod", owner_id="org_x")
    finally:
        for cm in cms:
            cm.stop()

    assert rc == 0
    # Full regen ran (applies the new model tiers/params/aux + correct agents.list).
    write_mock.assert_awaited_once()
    # The dropped runtime subtrees were restored over the fresh config.
    restore_patch.assert_awaited_once()
    p_args, _ = restore_patch.call_args
    assert p_args[0] == "org_x"
    restored = p_args[1]
    # channels.*.accounts survived (regen omitted it).
    assert restored["channels"]["slack"]["accounts"]["agent_friday"]["allowFrom"] == ["U123", "U456"]
    # The regenerated channel siblings are NOT in the patch — fresh owns them.
    assert "enabled" not in restored["channels"]["slack"]
    assert "dmPolicy" not in restored["channels"]["slack"]
    # top-level bindings survived (regen omitted it).
    assert restored["bindings"] == existing["bindings"]
    # top-level identity (runtime-only) survived.
    assert restored["identity"] == {"runtimeOnly": "preserve-me"}
    # The generator-owned model must NOT be in the restore patch — the regen
    # is authoritative for it; restoring would revert the new cheaper tier.
    assert "agents" not in restored


@pytest.mark.asyncio
async def test_apply_on_trial_owner_does_not_restore_channels_or_bindings():
    """The trial generator DELIBERATELY omits carried-forward channel
    accounts and route bindings (trial containers must not re-arm channel
    bots). The restore must mirror that lockdown instead of undoing it —
    while still restoring trial-safe runtime state like identity."""
    existing = {
        "channels": {"slack": {"accounts": {"agent_friday": {"allowFrom": ["U123"]}}}},
        "bindings": [{"type": "route", "agentId": "agent_friday"}],
        "identity": {"runtimeOnly": "preserve-me"},
    }
    fresh = {"channels": {}}
    restore_patch = AsyncMock()
    write_mock, cms = _patches(
        container={"gateway_token": "gw", "hook_token": "hk", "owner_type": "personal"},
        provider=("bedrock_trial", None),
        existing_config=existing,
        fresh_config=fresh,
        patch_mock=restore_patch,
    )
    for cm in cms:
        cm.start()
    try:
        rc = await mod.main(apply=True, env="prod", owner_id="user_trial")
    finally:
        for cm in cms:
            cm.stop()

    assert rc == 0
    write_mock.assert_awaited_once()
    restore_patch.assert_awaited_once()
    p_args, _ = restore_patch.call_args
    restored = p_args[1]
    assert "channels" not in restored
    assert "bindings" not in restored
    assert restored["identity"] == {"runtimeOnly": "preserve-me"}


@pytest.mark.asyncio
async def test_apply_with_no_dropped_subtrees_skips_restore():
    """When the fresh regen is a superset of the existing config (nothing
    runtime-only was dropped), no restore patch is applied."""
    existing = {
        "channels": {"slack": {"enabled": True, "dmPolicy": "pairing"}},
        "agents": {"defaults": {"model": {"primary": "old"}}},
    }
    fresh = {
        "channels": {"slack": {"enabled": True, "dmPolicy": "pairing"}},
        "agents": {"defaults": {"model": {"primary": "new"}}},
    }
    restore_patch = AsyncMock()
    write_mock, cms = _patches(
        container={"gateway_token": "gw", "hook_token": "hk", "owner_type": "org"},
        existing_config=existing,
        fresh_config=fresh,
        patch_mock=restore_patch,
    )
    for cm in cms:
        cm.start()
    try:
        rc = await mod.main(apply=True, env="prod", owner_id="org_x")
    finally:
        for cm in cms:
            cm.stop()

    assert rc == 0
    write_mock.assert_awaited_once()
    restore_patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_does_not_read_or_restore():
    """Dry run must resolve everything but write NOTHING — neither the full
    regen nor the runtime-subtree restore, nor even a config read."""
    restore_patch = AsyncMock()
    existing = {
        "channels": {"slack": {"accounts": {"agent_friday": {"allowFrom": ["U1"]}}}},
    }
    write_mock, cms = _patches(
        container={"gateway_token": "gw", "hook_token": "hk", "owner_type": "org"},
        existing_config=existing,
        patch_mock=restore_patch,
    )
    for cm in cms:
        cm.start()
    try:
        rc = await mod.main(apply=False, env="prod", owner_id="org_x")
    finally:
        for cm in cms:
            cm.stop()

    assert rc == 0
    write_mock.assert_not_called()
    restore_patch.assert_not_awaited()
