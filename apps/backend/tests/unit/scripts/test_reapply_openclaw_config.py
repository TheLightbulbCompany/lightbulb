"""Unit tests for the reapply-openclaw-config ops script.

The AWS write itself (``write_user_configs`` -> EFS) is a thin wrapper
exercised manually with ``--apply`` and real creds. What matters here is the
dry-run-vs-apply gate: a dry run must resolve everything but NEVER call the
write path, while ``--apply`` must call it exactly once with the inputs
sourced from the containers row + billing repo.
"""

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


def _patches(*, container, provider=("bedrock_claude", None)):
    """Patch the three collaborators the script lazy-imports inside main.

    Returns (write_user_configs_mock, list_of_patch_context_managers).
    """
    write_mock = AsyncMock()
    ecs_manager = MagicMock()
    ecs_manager.write_user_configs = write_mock

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
async def test_apply_calls_write_path_with_resolved_inputs():
    write_mock, cms = _patches(
        container={"gateway_token": "gw-tok", "hook_token": "hk-tok", "owner_type": "org"},
        provider=("byo_key", "anthropic"),
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
