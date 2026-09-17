"""Tests for catalog_service's AgentCore (Hermes) deploy path.

An AgentCore buyer has no EFS workspace -- their Hermes profile lives in S3
under ``<owner>/hermes/<agent_id>/`` instead. ``deploy_tarball_to_profile``
places the artifact's files there, and ``deploy_from_artifact`` branches to
it (via ``runtime_transport.host_for(container)``) instead of the EFS
extract + gateway-pairing path exercised by test_catalog_service.py.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import io  # noqa: E402
import tarfile  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from core.config import settings  # noqa: E402
from core.services import catalog_service  # noqa: E402
from core.services.catalog_service import CatalogService  # noqa: E402


def _make_tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def s3_hermes_bucket():
    """Moto-backed S3 bucket wired in as settings.LITESTREAM_BUCKET -- the
    same bucket the Hermes bridge's sync.py uploads owner profiles into."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-replicas")
        with patch.object(settings, "LITESTREAM_BUCKET", "test-replicas"):
            yield client


def _uploaded_keys(s3, prefix: str) -> list[str]:
    resp = s3.list_objects_v2(Bucket="test-replicas", Prefix=prefix)
    return [obj["Key"] for obj in resp.get("Contents", [])]


@pytest.mark.asyncio
async def test_deploy_tarball_to_profile_uploads_content_files_to_s3(s3_hermes_bucket):
    tar_bytes = _make_tar_gz({"SOUL.md": b"# Soul", "skills/foo.md": b"# Foo"})

    await catalog_service.deploy_tarball_to_profile(
        owner_id="owner_1", agent_id="agent_new", tar_bytes=tar_bytes, container={"runtime_host": "agentcore"}
    )

    keys = _uploaded_keys(s3_hermes_bucket, "owner_1/hermes/agent_new/")
    assert "owner_1/hermes/agent_new/SOUL.md" in keys
    assert "owner_1/hermes/agent_new/skills/foo.md" in keys


@pytest.mark.asyncio
async def test_deploy_tarball_to_profile_uploads_to_s3_with_fake_client():
    """Matches the task-7 brief's own wiring test: a fake S3 client (no
    moto) proves the upload call shape (Filename, Bucket, Key) is what
    _s3_client().upload_file expects, without depending on boto3 behavior."""
    uploaded = []

    def fake_upload(path, bucket, key):
        uploaded.append(key)

    with (
        patch(
            "core.services.catalog_service._s3_client",
            return_value=type("S3", (), {"upload_file": staticmethod(fake_upload)})(),
        ),
        patch.object(catalog_service.settings, "LITESTREAM_BUCKET", "test-replicas"),
    ):
        await catalog_service.deploy_tarball_to_profile(
            owner_id="owner_1", agent_id="agent_new", tar_bytes=b"", container={"runtime_host": "agentcore"}
        )
    assert any(k.startswith("owner_1/hermes/agent_new/") for k in uploaded)


@pytest.mark.asyncio
async def test_deploy_tarball_to_profile_writes_revision_marker_last():
    uploaded = []

    def fake_upload(path, bucket, key):
        uploaded.append(key)

    tar_bytes = _make_tar_gz({"SOUL.md": b"# Soul", "config.yaml": b"model: x"})
    with (
        patch(
            "core.services.catalog_service._s3_client",
            return_value=type("S3", (), {"upload_file": staticmethod(fake_upload)})(),
        ),
        patch.object(catalog_service.settings, "LITESTREAM_BUCKET", "test-replicas"),
    ):
        await catalog_service.deploy_tarball_to_profile(
            owner_id="owner_1", agent_id="agent_new", tar_bytes=tar_bytes, container={"runtime_host": "agentcore"}
        )
    assert uploaded[-1] == "owner_1/hermes/agent_new/.revision"


@pytest.mark.asyncio
async def test_deploy_tarball_to_profile_skips_env_and_config_yaml(s3_hermes_bucket):
    """The profile writer (Task 17) owns .env/config.yaml for a freshly
    deployed agent -- a tarball that happens to carry them (e.g. an older
    export) must not clobber them."""
    tar_bytes = _make_tar_gz({"SOUL.md": b"# Soul", ".env": b"SECRET=1", "config.yaml": b"model: seller-pinned"})

    await catalog_service.deploy_tarball_to_profile(
        owner_id="owner_1", agent_id="agent_new", tar_bytes=tar_bytes, container={"runtime_host": "agentcore"}
    )

    keys = _uploaded_keys(s3_hermes_bucket, "owner_1/hermes/agent_new/")
    assert "owner_1/hermes/agent_new/SOUL.md" in keys
    assert not any(k.endswith("/.env") for k in keys)
    assert not any(k.endswith("/config.yaml") for k in keys)
    assert "owner_1/hermes/agent_new/.revision" in keys


@pytest.mark.asyncio
async def test_deploy_tarball_to_profile_handles_empty_tar_bytes(s3_hermes_bucket):
    """An empty/placeholder tarball still gets a fresh .revision marker --
    deploy_from_artifact's real callers never pass this, but the write path
    must not crash on it."""
    await catalog_service.deploy_tarball_to_profile(
        owner_id="owner_1", agent_id="agent_new", tar_bytes=b"", container={"runtime_host": "agentcore"}
    )
    keys = _uploaded_keys(s3_hermes_bucket, "owner_1/hermes/agent_new/")
    assert keys == ["owner_1/hermes/agent_new/.revision"]


def _make_service_for_deploy():
    workspace = MagicMock()
    workspace.extract_tarball_to_workspace = MagicMock(return_value=None)
    workspace.write_template_sidecar = MagicMock(return_value=None)
    workspace.read_cron_jobs = MagicMock(return_value=[])
    pair_fn = AsyncMock(return_value=None)
    apply_aux = AsyncMock(return_value=None)
    return (
        CatalogService(workspace=workspace, apply_deploy_aux_mutations=apply_aux, pair_fn=pair_fn),
        workspace,
        pair_fn,
        apply_aux,
    )


@pytest.mark.asyncio
async def test_deploy_from_artifact_delegates_to_s3_profile_for_agentcore_buyer():
    """deploy_from_artifact must actually CALL deploy_tarball_to_profile for
    an agentcore buyer -- not just resolve the branch and fall through --
    and must skip every EFS/gateway-RPC side effect below it."""
    svc, workspace, pair_fn, apply_aux = _make_service_for_deploy()

    with patch("core.services.catalog_service.deploy_tarball_to_profile", new=AsyncMock(return_value=None)) as mocked:
        result = await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_={"agent": {"name": "Demo"}},
            tar_bytes=b"<gz>",
            new_agent_id="agent_abc",
            container={"runtime_host": "agentcore"},
        )

    mocked.assert_awaited_once_with(
        owner_id="user_buyer", agent_id="agent_abc", tar_bytes=b"<gz>", container={"runtime_host": "agentcore"}
    )
    assert result["agent_id"] == "agent_abc"
    workspace.extract_tarball_to_workspace.assert_not_called()
    pair_fn.assert_not_called()
    apply_aux.assert_not_called()


@pytest.mark.asyncio
async def test_deploy_from_artifact_keeps_efs_path_when_container_omitted():
    """No container in hand (every existing caller, today) must keep taking
    the EFS/gateway-RPC path byte-for-byte."""
    svc, workspace, pair_fn, apply_aux = _make_service_for_deploy()

    with patch("core.services.catalog_service.deploy_tarball_to_profile", new=AsyncMock(return_value=None)) as mocked:
        await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_={"agent": {"name": "Demo"}},
            tar_bytes=b"<gz>",
            new_agent_id="agent_abc",
        )

    mocked.assert_not_awaited()
    workspace.extract_tarball_to_workspace.assert_called_once()
    pair_fn.assert_awaited_once()
