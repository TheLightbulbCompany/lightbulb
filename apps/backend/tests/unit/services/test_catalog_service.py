"""Tests for catalog_service."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

from core.services.catalog_service import CatalogService  # noqa: E402


def _make_service():
    """Construct a CatalogService with a fully mocked workspace + deploy
    mutation, so tests focus on the orchestration logic in
    ``deploy_from_artifact`` and not the EFS / config-patcher side effects.
    """
    workspace = MagicMock()
    workspace.extract_tarball_to_workspace = MagicMock(return_value=None)
    workspace.write_template_sidecar = MagicMock(return_value=None)
    workspace.read_cron_jobs = MagicMock(return_value=[])
    workspace.cleanup_agent_dirs = MagicMock(return_value=None)
    apply_deploy = AsyncMock(return_value=None)
    svc = CatalogService(workspace=workspace, apply_deploy_mutation=apply_deploy)
    return svc, workspace, apply_deploy


@pytest.mark.asyncio
async def test_deploy_from_artifact_uses_supplied_new_agent_id():
    """When the caller supplies new_agent_id, that id MUST be used for the
    workspace dir + the agent entry deep-merged into openclaw.json. The
    Paperclip-first agent-id flow depends on this — Paperclip mints the
    UUID; catalog_service must respect it instead of generating its own.
    """
    svc, workspace, apply_deploy = _make_service()
    result = await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tar_bytes=b"fake-tar",
        new_agent_id="paperclip-uuid-1234",
    )
    assert result["agent_id"] == "paperclip-uuid-1234"
    # extract_tarball_to_workspace was called with the supplied id.
    workspace.extract_tarball_to_workspace.assert_called_once()
    extract_kwargs = workspace.extract_tarball_to_workspace.call_args.kwargs or {}
    extract_args = workspace.extract_tarball_to_workspace.call_args.args or ()
    # The implementation may pass via args or kwargs — accept either.
    assert "paperclip-uuid-1234" in (extract_kwargs.get("agent_id"), *extract_args)
    # apply_deploy_mutation called with the supplied id in the agent_entry.
    apply_deploy.assert_awaited_once()
    agent_entry = apply_deploy.await_args.args[1]
    assert agent_entry["id"] == "paperclip-uuid-1234"


@pytest.mark.asyncio
async def test_deploy_from_artifact_substitutes_agent_dir_sentinel_in_mcp_args():
    """Spec change #3: at export time, agent_export replaces the seller's
    agent_id in mcp.servers.X.args with __AGENT_DIR__. At deploy time,
    catalog_service must substitute it back to the buyer's new_agent_id
    before applying the slice. Otherwise the MCP starts up pointing at
    a workspace that doesn't exist on the buyer's container.
    """
    svc, _workspace, apply_deploy = _make_service()
    slice_ = {
        "agent": {"name": "Demo"},
        "plugins": {},
        "tools": {},
        "cron_jobs": [],
        "mcp": {
            "servers": {
                "resy": {
                    "command": "node",
                    "args": [
                        "/home/node/.openclaw/workspaces/__AGENT_DIR__/mcp-resy/index.js"
                    ],
                    "env": {"RESY_EMAIL": ""},
                }
            }
        },
    }
    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=slice_,
        tar_bytes=b"fake-tar",
        new_agent_id="agent_xyz",
    )
    # apply_deploy_mutation receives mcp_patch with the sentinel substituted.
    apply_deploy.assert_awaited_once()
    mcp_patch = apply_deploy.await_args.kwargs.get("mcp_patch")
    assert mcp_patch is not None
    args = mcp_patch["servers"]["resy"]["args"]
    assert "__AGENT_DIR__" not in args[0], args[0]
    assert "agent_xyz" in args[0], args[0]
    assert args[0].endswith("/mcp-resy/index.js"), args[0]


@pytest.mark.asyncio
async def test_deploy_from_artifact_generates_uuid_when_new_agent_id_omitted():
    """Backwards-compat: callers that don't pass new_agent_id still get a
    locally-generated UUID. Direct catalog deploys (tests, future paths)
    should keep working.
    """
    svc, _workspace, apply_deploy = _make_service()
    result = await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tar_bytes=b"fake-tar",
    )
    assert result["agent_id"].startswith("agent_")
    assert len(result["agent_id"]) > len("agent_")
