"""Tests for CatalogService — deploy_from_artifact + list_deployed_for_user.

publish/unpublish/list/list_all/list_versions/deploy were removed when the
admin-catalog path was deleted; the marketplace listings router owns the
publish surface now and calls deploy_from_artifact directly with the
artifact tarball + slice fetched from the marketplace S3 bucket.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.catalog_service import CatalogService


@pytest.fixture
def mock_workspace():
    m = MagicMock()
    # Defaults exercised by most tests; individual tests override as needed.
    m.read_openclaw_config.return_value = {}
    m.read_cron_jobs.return_value = []
    return m


@pytest.fixture
def mock_apply_deploy():
    return AsyncMock()


@pytest.fixture
def service(mock_workspace, mock_apply_deploy):
    return CatalogService(
        workspace=mock_workspace,
        apply_deploy_mutation=mock_apply_deploy,
    )


# ---------------------------------------------------------------------------
# deploy_from_artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_from_artifact_extracts_tar_merges_config_writes_sidecar(
    service, mock_workspace, mock_apply_deploy
):
    """Happy path: tarball extracts, slice deep-merges, sidecar written."""
    manifest = {"slug": "pitch", "version": 3, "name": "Pitch"}
    slice_ = {
        "agent": {"name": "Pitch", "skills": ["web-search"]},
        "plugins": {"entries": {"memory": {"enabled": True}}},
        "tools": {"profile": "default"},
    }

    result = await service.deploy_from_artifact(
        owner_id="user_u",
        slug="pitch",
        manifest=manifest,
        slice_=slice_,
        tar_bytes=b"<gz>",
    )

    assert result["slug"] == "pitch"
    assert result["agent_id"].startswith("agent_")
    assert result["skills_added"] == ["web-search"]
    assert result["plugins_enabled"] == ["memory"]
    assert result["config_registered"] is True

    # Tarball extracted to the buyer's workspace under the new agent_id.
    mock_workspace.extract_tarball_to_workspace.assert_called_once()
    _, kwargs = mock_workspace.extract_tarball_to_workspace.call_args
    assert kwargs["user_id"] == "user_u"
    assert kwargs["agent_id"] == result["agent_id"]
    assert kwargs["tar_bytes"] == b"<gz>"

    # Slice merged into buyer's openclaw.json with the new agent_id stamped in.
    mock_apply_deploy.assert_awaited_once()
    owner_id, agent_entry, plugins_patch = mock_apply_deploy.call_args.args
    assert owner_id == "user_u"
    assert agent_entry["id"] == result["agent_id"]
    assert agent_entry["workspace"] == f".openclaw/workspaces/{result['agent_id']}"
    assert plugins_patch == {"entries": {"memory": {"enabled": True}}}


@pytest.mark.asyncio
async def test_deploy_from_artifact_writes_template_sidecar(service, mock_workspace, mock_apply_deploy):
    written = {}

    def _write(user_id, agent_id, content):
        written.update(user_id=user_id, agent_id=agent_id, content=content)

    mock_workspace.write_template_sidecar = _write

    result = await service.deploy_from_artifact(
        owner_id="user_u",
        slug="pitch",
        manifest={"slug": "pitch", "version": 3},
        slice_={"agent": {"name": "Pitch"}, "plugins": {}, "tools": {}},
        tar_bytes=b"<gz>",
    )

    assert written["user_id"] == "user_u"
    assert written["agent_id"] == result["agent_id"]
    assert written["content"]["template_slug"] == "pitch"
    assert written["content"]["template_version"] == 3
    assert "deployed_at" in written["content"]


@pytest.mark.asyncio
async def test_deploy_from_artifact_skips_config_patch_when_slice_has_no_agent(
    service, mock_workspace, mock_apply_deploy
):
    """Legacy marketplace listings published before slice-synthesis shipped
    have ``slice_={}`` (or a slice missing ``agent``). The deploy must
    extract files but leave openclaw.json untouched — buyer wires it by hand.
    """
    result = await service.deploy_from_artifact(
        owner_id="user_u",
        slug="legacy",
        manifest={"slug": "legacy", "version": 1},
        slice_={},
        tar_bytes=b"<gz>",
    )

    assert result["config_registered"] is False
    mock_workspace.extract_tarball_to_workspace.assert_called_once()
    mock_workspace.write_template_sidecar.assert_called_once()
    mock_apply_deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_deploy_from_artifact_rolls_back_workspace_on_patch_failure(service, mock_workspace, mock_apply_deploy):
    mock_apply_deploy.side_effect = RuntimeError("config patch boom")

    with pytest.raises(RuntimeError, match="config patch boom"):
        await service.deploy_from_artifact(
            owner_id="user_u",
            slug="pitch",
            manifest={"slug": "pitch", "version": 1},
            slice_={"agent": {"name": "Pitch"}, "plugins": {}, "tools": {}},
            tar_bytes=b"<gz>",
        )

    # Workspace cleaned up so a failed deploy doesn't leave orphan files.
    mock_workspace.cleanup_agent_dirs.assert_called_once()


@pytest.mark.asyncio
async def test_deploy_from_artifact_carries_cron_jobs_with_remapped_id_and_agent(
    service, mock_workspace, mock_apply_deploy
):
    appended = []

    async def _append_cron(owner_id, jobs):
        appended.append((owner_id, jobs))

    # Patch core.services.config_patcher.append_cron_jobs at the import site
    # used inside deploy_from_artifact. The function does an in-method import,
    # so monkey-patching the attr on the module suffices.
    import core.services.config_patcher as cp

    original = cp.append_cron_jobs
    cp.append_cron_jobs = _append_cron
    try:
        slice_ = {
            "agent": {"name": "Pitch"},
            "plugins": {},
            "tools": {},
            "cron_jobs": [
                {
                    "agentId": "publisher-agent",
                    "name": "morning standup",
                    "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                    "delivery": {"channel": "telegram", "accountId": "publisher-agent"},
                }
            ],
        }
        result = await service.deploy_from_artifact(
            owner_id="user_u",
            slug="pitch",
            manifest={"slug": "pitch", "version": 1},
            slice_=slice_,
            tar_bytes=b"<gz>",
        )
    finally:
        cp.append_cron_jobs = original

    assert appended, "cron jobs should be appended after the agent entry lands"
    owner_id, jobs = appended[0]
    assert owner_id == "user_u"
    assert len(jobs) == 1
    job = jobs[0]
    # agentId rewritten to the new buyer-side agent id; delivery.accountId follows.
    assert job["agentId"] == result["agent_id"]
    assert job["delivery"]["accountId"] == result["agent_id"]
    # sessionKey regenerated to OpenClaw's `agent:{id}:{user_lower}` shape.
    assert job["sessionKey"] == f"agent:{result['agent_id']}:user_u"


@pytest.mark.asyncio
async def test_deploy_from_artifact_skips_cron_append_when_no_jobs(service, mock_workspace, mock_apply_deploy):
    import core.services.config_patcher as cp

    calls = []

    async def _append_cron(owner_id, jobs):
        calls.append((owner_id, jobs))

    original = cp.append_cron_jobs
    cp.append_cron_jobs = _append_cron
    try:
        await service.deploy_from_artifact(
            owner_id="user_u",
            slug="pitch",
            manifest={"slug": "pitch", "version": 1},
            slice_={"agent": {"name": "Pitch"}, "plugins": {}, "tools": {}, "cron_jobs": []},
            tar_bytes=b"<gz>",
        )
    finally:
        cp.append_cron_jobs = original

    assert calls == []


@pytest.mark.asyncio
async def test_deploy_from_artifact_uses_passed_owner_id_for_all_efs_writes(service, mock_workspace, mock_apply_deploy):
    """``owner_id`` is the partition key for every EFS + config-patch operation;
    a typo here would scatter the deploy across two users' partitions.
    """
    await service.deploy_from_artifact(
        owner_id="org_xyz",
        slug="pitch",
        manifest={"slug": "pitch", "version": 1},
        slice_={"agent": {"name": "Pitch"}, "plugins": {}, "tools": {}},
        tar_bytes=b"<gz>",
    )

    assert mock_workspace.extract_tarball_to_workspace.call_args.kwargs["user_id"] == "org_xyz"
    assert mock_workspace.write_template_sidecar.call_args.kwargs["user_id"] == "org_xyz"
    assert mock_apply_deploy.call_args.args[0] == "org_xyz"


# ---------------------------------------------------------------------------
# list_deployed_for_user
# ---------------------------------------------------------------------------


def test_list_deployed_for_user_keyed_by_owner_id(service, mock_workspace):
    """Sidebar's already-deployed filter scans ``workspaces/`` sidecars by
    owner_id (org_id for orgs, user_id for personal-mode). Each sidecar
    becomes ``{agent_id, template_slug, template_version}``.
    """
    mock_workspace.list_workspace_agent_dirs.return_value = ["agent_a", "agent_b", "agent_c"]
    mock_workspace.read_template_sidecar.side_effect = [
        {"template_slug": "pitch", "template_version": 3},
        None,  # agent_b has no sidecar (manually-added agent) — skipped.
        {"template_slug": "scout", "template_version": 1},
    ]

    out = service.list_deployed_for_user("org_xyz")

    mock_workspace.list_workspace_agent_dirs.assert_called_once_with("org_xyz")
    assert out == [
        {"agent_id": "agent_a", "template_slug": "pitch", "template_version": 3},
        {"agent_id": "agent_c", "template_slug": "scout", "template_version": 1},
    ]
