"""Tests for catalog_service's AgentCore (Hermes) deploy path.

An AgentCore buyer has no EFS workspace -- their Hermes profile lives in S3
under ``<owner>/hermes/<agent_id>/`` instead. ``deploy_from_artifact``
branches on ``runtime_transport.host_for(container)`` into
``_install_agentcore_deploy`` (Paperclip mint-if-needed -> Hermes profile ->
tarball content, M6-R9) instead of the EFS extract + gateway-pairing path
exercised by test_catalog_service.py.
"""

import contextlib
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
    """Moto-backed S3 bucket wired in as settings.OWNER_STATE_BUCKET -- the
    profile writer's (hermes_profiles.upload_profile) single source of truth
    for this bucket."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-replicas")
        with patch.object(settings, "OWNER_STATE_BUCKET", "test-replicas"):
            yield client


def _uploaded_keys(s3, prefix: str) -> list[str]:
    resp = s3.list_objects_v2(Bucket="test-replicas", Prefix=prefix)
    return [obj["Key"] for obj in resp.get("Contents", [])]


# ---------------------------------------------------------------------------
# deploy_tarball_to_profile -- pure file placement (no registration)
# ---------------------------------------------------------------------------


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
        patch.object(catalog_service.settings, "OWNER_STATE_BUCKET", "test-replicas"),
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
        patch.object(catalog_service.settings, "OWNER_STATE_BUCKET", "test-replicas"),
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


# ---------------------------------------------------------------------------
# _build_and_upload_agent_profile -- sources write_agent_profile's inputs
# ---------------------------------------------------------------------------


def _patch_profile_inputs(
    stack: contextlib.ExitStack, *, provider_choice="bedrock_claude", byo_provider=None, roster=None
):
    """Enter (on ``stack``) a patch for every collaborator
    _build_and_upload_agent_profile reads, at the module paths it imports
    them from (all local imports inside the function)."""
    stack.enter_context(
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": provider_choice, "byo_provider": byo_provider}),
        )
    )
    stack.enter_context(patch("core.services.messaging.load_agent_roster", new=AsyncMock(return_value=roster or [])))
    stack.enter_context(
        patch(
            "core.services.credentials.mcp_server.build_connected_integration_servers", new=AsyncMock(return_value={})
        )
    )
    stack.enter_context(
        patch("core.services.provisioning_landing.build_provisioned_env", new=AsyncMock(return_value={}))
    )
    stack.enter_context(
        patch("core.services.catalog_service._resolve_paperclip_api_key", new=AsyncMock(return_value="pk-123"))
    )


@pytest.mark.asyncio
async def test_build_and_upload_agent_profile_writes_and_uploads():
    written = {}

    def fake_write_agent_profile(owner_id, agent, **kwargs):
        written["owner_id"] = owner_id
        written["agent"] = agent
        written["kwargs"] = kwargs
        return "PROFILE_FILES_SENTINEL"

    upload_mock = AsyncMock(return_value=None)
    with contextlib.ExitStack() as stack:
        _patch_profile_inputs(stack, roster=[{"id": "agent_new", "name": "Demo", "role": "general"}])
        stack.enter_context(
            patch("core.services.hermes_profiles.write_agent_profile", side_effect=fake_write_agent_profile)
        )
        stack.enter_context(patch("core.services.hermes_profiles.upload_profile", new=upload_mock))

        await catalog_service._build_and_upload_agent_profile(
            owner_id="owner_1",
            agent_id="agent_new",
            container={"bedrock_aip_arns": {"sonnet": "arn:aip:sonnet"}},
            manifest={"name": "Demo"},
        )

    assert written["owner_id"] == "owner_1"
    assert written["agent"]["id"] == "agent_new"
    assert written["kwargs"]["provider_choice"] == "bedrock_claude"
    assert written["kwargs"]["aip_arns"] == {"sonnet": "arn:aip:sonnet"}
    assert written["kwargs"]["paperclip_api_key"] == "pk-123"
    upload_mock.assert_awaited_once_with("owner_1", "agent_new", "PROFILE_FILES_SENTINEL")


@pytest.mark.asyncio
async def test_build_and_upload_agent_profile_falls_back_when_roster_read_fails():
    """A Paperclip outage on the roster read must not fail the whole
    profile build -- it degrades to a minimal agent row from the manifest."""
    upload_mock = AsyncMock(return_value=None)
    write_mock = MagicMock(return_value="FILES")
    with contextlib.ExitStack() as stack:
        _patch_profile_inputs(stack)
        stack.enter_context(
            patch(
                "core.services.messaging.load_agent_roster", new=AsyncMock(side_effect=RuntimeError("paperclip down"))
            )
        )
        stack.enter_context(patch("core.services.hermes_profiles.write_agent_profile", new=write_mock))
        stack.enter_context(patch("core.services.hermes_profiles.upload_profile", new=upload_mock))

        await catalog_service._build_and_upload_agent_profile(
            owner_id="owner_1", agent_id="agent_new", container={}, manifest={"name": "Fallback Name"}
        )

    agent_row = write_mock.call_args.args[1]
    assert agent_row == {"id": "agent_new", "name": "Fallback Name", "role": "general"}


# ---------------------------------------------------------------------------
# _install_agentcore_deploy / deploy_from_artifact -- ordering + rollback
# ---------------------------------------------------------------------------


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


def _patch_agentcore_collaborators(
    stack: contextlib.ExitStack, call_order: list[str], *, minted_id: str = "minted_agent_1", profile_raises=None
) -> AsyncMock:
    """Enter (on ``stack``) patches for the three collaborators
    _install_agentcore_deploy calls, each appending its name to
    ``call_order``. Returns the delete (rollback) mock so callers can assert
    on it."""

    async def _create(**_kwargs):
        call_order.append("create")
        return {"id": minted_id}

    async def _profile(**_kwargs):
        if profile_raises is not None:
            raise profile_raises
        call_order.append("profile")

    async def _content(**_kwargs):
        call_order.append("content")

    delete_mock = AsyncMock(return_value=None)
    stack.enter_context(
        patch("core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace", side_effect=_create)
    )
    stack.enter_context(
        patch("core.services.paperclip_agent_lifecycle.delete_paperclip_for_marketplace", new=delete_mock)
    )
    stack.enter_context(patch("core.services.catalog_service._build_and_upload_agent_profile", side_effect=_profile))
    stack.enter_context(patch("core.services.catalog_service.deploy_tarball_to_profile", side_effect=_content))
    return delete_mock


@pytest.mark.asyncio
async def test_deploy_from_artifact_mints_profiles_then_content_for_a_fresh_agentcore_install():
    """No pre-minted new_agent_id -- the AgentCore branch mints through
    Paperclip itself (same helper every ECS caller already uses), then
    writes the profile, then places the content. Order: create -> profile ->
    content."""
    svc, workspace, pair_fn, apply_aux = _make_service_for_deploy()
    call_order: list[str] = []
    with contextlib.ExitStack() as stack:
        delete_mock = _patch_agentcore_collaborators(stack, call_order)
        result = await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_={"agent": {"name": "Demo"}},
            tar_bytes=b"<gz>",
            new_agent_id=None,
            container={"runtime_host": "agentcore"},
        )

    assert call_order == ["create", "profile", "content"]
    assert result["agent_id"] == "minted_agent_1"
    assert result["runtimeHost"] == "agentcore"
    delete_mock.assert_not_awaited()
    workspace.extract_tarball_to_workspace.assert_not_called()
    pair_fn.assert_not_called()
    apply_aux.assert_not_called()


@pytest.mark.asyncio
async def test_deploy_from_artifact_does_not_re_mint_a_pre_supplied_agentcore_id():
    """Every real caller (marketplace deploy, hire, agent builder) already
    minted the Paperclip agent before calling deploy_from_artifact -- a
    second mint here would orphan a different id than the one being
    deployed."""
    svc, *_ = _make_service_for_deploy()
    call_order: list[str] = []
    with contextlib.ExitStack() as stack:
        delete_mock = _patch_agentcore_collaborators(stack, call_order)
        result = await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_={"agent": {"name": "Demo"}},
            tar_bytes=b"<gz>",
            new_agent_id="agent_abc",
            container={"runtime_host": "agentcore"},
        )

    assert call_order == ["profile", "content"]  # no "create"
    assert result["agent_id"] == "agent_abc"
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_deploy_from_artifact_rolls_back_self_minted_agent_on_profile_failure():
    svc, *_ = _make_service_for_deploy()
    call_order: list[str] = []
    with contextlib.ExitStack() as stack:
        delete_mock = _patch_agentcore_collaborators(
            stack, call_order, profile_raises=RuntimeError("profile write failed")
        )
        with pytest.raises(RuntimeError, match="profile write failed"):
            await svc.deploy_from_artifact(
                owner_id="user_buyer",
                slug="demo",
                manifest={"name": "Demo", "version": 1},
                slice_={"agent": {"name": "Demo"}},
                tar_bytes=b"<gz>",
                new_agent_id=None,
                container={"runtime_host": "agentcore"},
            )

    delete_mock.assert_awaited_once_with(owner_id="user_buyer", agent_id="minted_agent_1")


@pytest.mark.asyncio
async def test_deploy_from_artifact_never_rolls_back_a_pre_supplied_agentcore_id_on_failure():
    """A failure on a pre-minted agent is the CALLER's rollback envelope
    (mirrors every ECS caller's existing unwind) -- this function must not
    delete an agent it didn't create."""
    svc, *_ = _make_service_for_deploy()
    call_order: list[str] = []
    with contextlib.ExitStack() as stack:
        delete_mock = _patch_agentcore_collaborators(
            stack, call_order, profile_raises=RuntimeError("profile write failed")
        )
        with pytest.raises(RuntimeError, match="profile write failed"):
            await svc.deploy_from_artifact(
                owner_id="user_buyer",
                slug="demo",
                manifest={"name": "Demo", "version": 1},
                slice_={"agent": {"name": "Demo"}},
                tar_bytes=b"<gz>",
                new_agent_id="agent_abc",
                container={"runtime_host": "agentcore"},
            )

    delete_mock.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_deploy_from_artifact_keeps_efs_path_for_an_explicit_ecs_container():
    svc, workspace, pair_fn, _apply_aux = _make_service_for_deploy()

    with patch("core.services.catalog_service.deploy_tarball_to_profile", new=AsyncMock(return_value=None)) as mocked:
        await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_={"agent": {"name": "Demo"}},
            tar_bytes=b"<gz>",
            new_agent_id="agent_abc",
            container={"runtime_host": "ecs"},
        )

    mocked.assert_not_awaited()
    workspace.extract_tarball_to_workspace.assert_called_once()
    pair_fn.assert_awaited_once()
