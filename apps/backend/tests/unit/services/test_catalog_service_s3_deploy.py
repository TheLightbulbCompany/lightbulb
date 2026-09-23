"""Tests for catalog_service's AgentCore (Hermes) deploy path.

An AgentCore buyer has no EFS workspace -- their Hermes profile lives in S3
under ``<owner>/hermes/<agent_id>/`` instead. ``deploy_from_artifact``
branches on ``runtime_transport.host_for(container)`` into
``_install_agentcore_deploy`` (Paperclip mint-if-needed -> Hermes profile ->
tarball content, M6-R9). ``host_for`` is now pinned to ``agentcore``, so this
is the ONLY install path -- the EFS extract + gateway-pairing branch is dead
and its tests are gone.
"""

import contextlib
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import io  # noqa: E402
import tarfile  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

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
    # AGENTS.md/skills/** are genuine content -- not in BACKEND_OWNED_FILES (F3), so they land
    # verbatim. SOUL.md is exercised separately below: it IS backend-owned and must be skipped.
    tar_bytes = _make_tar_gz({"AGENTS.md": b"# Agents", "skills/foo.md": b"# Foo"})

    await catalog_service.deploy_tarball_to_profile(
        owner_id="owner_1", agent_id="agent_new", tar_bytes=tar_bytes, container={"runtime_host": "agentcore"}
    )

    keys = _uploaded_keys(s3_hermes_bucket, "owner_1/hermes/agent_new/")
    assert "owner_1/hermes/agent_new/AGENTS.md" in keys
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

    tar_bytes = _make_tar_gz({"AGENTS.md": b"# Agents", "config.yaml": b"model: x"})
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
    # config.yaml is backend-owned (skipped); AGENTS.md is genuine content, so .revision lands
    # after it -- not merely after nothing, as it would if the tarball held only skipped names.
    assert uploaded == ["owner_1/hermes/agent_new/AGENTS.md", "owner_1/hermes/agent_new/.revision"]


@pytest.mark.asyncio
async def test_deploy_tarball_to_profile_skips_every_backend_owned_file(s3_hermes_bucket):
    """F3 (predeploy review, M6-R36): the profile writer (Task 17) owns config.yaml/.env/
    SOUL.md/.revision/lightbulb-facts.json for a freshly deployed agent -- a tarball that
    happens to carry any of them (e.g. an older export, or a seller's own persona/facts file)
    must never clobber the writer's own copy. Before this fix the skip list named only two of
    the five, so a tarball carrying SOUL.md or lightbulb-facts.json silently overwrote them."""
    tar_bytes = _make_tar_gz(
        {
            "SOUL.md": b"# Soul",
            ".env": b"SECRET=1",
            "config.yaml": b"model: seller-pinned",
            "lightbulb-facts.json": b'{"fake": true}',
            "skills/foo.md": b"# Foo",
        }
    )

    await catalog_service.deploy_tarball_to_profile(
        owner_id="owner_1", agent_id="agent_new", tar_bytes=tar_bytes, container={"runtime_host": "agentcore"}
    )

    keys = _uploaded_keys(s3_hermes_bucket, "owner_1/hermes/agent_new/")
    assert "owner_1/hermes/agent_new/skills/foo.md" in keys
    assert not any(k.endswith("/SOUL.md") for k in keys)
    assert not any(k.endswith("/.env") for k in keys)
    assert not any(k.endswith("/config.yaml") for k in keys)
    assert not any(k.endswith("/lightbulb-facts.json") for k in keys)
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
# _build_and_upload_agent_profile -- a thin resolve-then-delegate wrapper
# (M6-R23): resolves this agent's own Paperclip roster row by id, then hands
# off to hermes_provisioning.write_profile_for_agent, the shared per-agent
# seam whose own sourcing/error-handling coverage now lives in
# test_hermes_provisioning.py.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_and_upload_agent_profile_resolves_the_row_and_delegates():
    write_profile = AsyncMock()
    roster = [{"id": "agent_new", "name": "Demo", "role": "general"}]
    container = {"bedrock_aip_arns": {"sonnet": "arn:aip:sonnet"}}
    with (
        patch("core.services.messaging.load_agent_roster", new=AsyncMock(return_value=roster)),
        patch("core.services.hermes_provisioning.write_profile_for_agent", new=write_profile),
    ):
        await catalog_service._build_and_upload_agent_profile(
            owner_id="owner_1", agent_id="agent_new", container=container
        )

    write_profile.assert_awaited_once_with("owner_1", roster[0], soul_md_override=None)


@pytest.mark.asyncio
async def test_build_and_upload_agent_profile_raises_when_roster_read_fails():
    """M6-R10: catalog hard-fails on a roster read failure exactly like
    hermes_provisioning.write_profiles does -- the old manifest-derived
    fallback agent row is gone."""
    write_profile = AsyncMock()
    with (
        patch("core.services.messaging.load_agent_roster", new=AsyncMock(side_effect=RuntimeError("paperclip down"))),
        patch("core.services.hermes_provisioning.write_profile_for_agent", new=write_profile),
    ):
        with pytest.raises(RuntimeError, match="paperclip down"):
            await catalog_service._build_and_upload_agent_profile(
                owner_id="owner_1", agent_id="agent_new", container={}
            )

    write_profile.assert_not_awaited()


# ---------------------------------------------------------------------------
# M6-R41 -- the seller's own SOUL.md becomes the AgentCore profile identity.
# The ECS install path already ships a marketplace agent's SOUL.md verbatim
# (via the buyer's EFS extract); F3 widened _DEPLOY_PROFILE_SKIP_NAMES so
# deploy_tarball_to_profile's own content pass now skips SOUL.md, which would
# otherwise silently leave the roster-derived default in place instead.
# ---------------------------------------------------------------------------


def test_read_soul_md_from_tar_extracts_the_sellers_text():
    tar_bytes = _make_tar_gz({"SOUL.md": b"# Ada\n\nI am the seller's own persona.\n", "skills/foo.md": b"# Foo"})
    assert catalog_service._read_soul_md_from_tar(tar_bytes) == "# Ada\n\nI am the seller's own persona.\n"


def test_read_soul_md_from_tar_matches_by_basename_at_any_path():
    tar_bytes = _make_tar_gz({"workspace/SOUL.md": b"# Nested\n"})
    assert catalog_service._read_soul_md_from_tar(tar_bytes) == "# Nested\n"


def test_read_soul_md_from_tar_returns_none_without_one():
    tar_bytes = _make_tar_gz({"skills/foo.md": b"# Foo"})
    assert catalog_service._read_soul_md_from_tar(tar_bytes) is None


def test_read_soul_md_from_tar_returns_none_for_empty_bytes():
    assert catalog_service._read_soul_md_from_tar(b"") is None


def _wire_agentcore_profile_writer(monkeypatch, roster):
    """The real write_agent_profile/upload_profile path, minus the network-touching edges
    (Paperclip roster read, API key mint, admin client, facts staging) -- mirrors
    test_hermes_provisioning.py's own write_profile_for_agent integration tests."""
    from core.services.hermes_provisioning import ProfileInputs

    inputs = ProfileInputs(
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        byo_api_key=None,
        integrations={},
        env_vars={},
    )
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr("core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pk-123"))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr("core.services.hermes_provisioning._stage_owner_facts", AsyncMock())


@pytest.mark.asyncio
async def test_agentcore_install_uses_the_sellers_soul_md_config_and_env_untouched(s3_hermes_bucket, monkeypatch):
    roster = [{"id": "minted_agent_1", "name": "Ada", "role": "general"}]
    _wire_agentcore_profile_writer(monkeypatch, roster)
    tar_bytes = _make_tar_gz(
        {
            "SOUL.md": b"# Ada\n\nI am the seller's own persona, not the roster default.\n",
            ".env": b"SECRET=should-never-land",
            "config.yaml": b"model: seller-pinned",
            "skills/foo.md": b"# Foo",
        }
    )
    container = {"runtime_host": "agentcore"}

    soul_md = catalog_service._read_soul_md_from_tar(tar_bytes)
    await catalog_service._build_and_upload_agent_profile(
        owner_id="owner_1", agent_id="minted_agent_1", container=container, soul_md_override=soul_md
    )
    await catalog_service.deploy_tarball_to_profile(
        owner_id="owner_1", agent_id="minted_agent_1", tar_bytes=tar_bytes, container=container
    )

    prefix = "owner_1/hermes/minted_agent_1/"
    uploaded_soul = s3_hermes_bucket.get_object(Bucket="test-replicas", Key=f"{prefix}SOUL.md")["Body"].read()
    assert uploaded_soul == b"# Ada\n\nI am the seller's own persona, not the roster default.\n"
    uploaded_env = s3_hermes_bucket.get_object(Bucket="test-replicas", Key=f"{prefix}.env")["Body"].read()
    assert b"should-never-land" not in uploaded_env  # the writer's own .env survives, not the tarball's
    uploaded_config = s3_hermes_bucket.get_object(Bucket="test-replicas", Key=f"{prefix}config.yaml")["Body"].read()
    assert b"seller-pinned" not in uploaded_config  # the writer's own config.yaml survives
    keys = _uploaded_keys(s3_hermes_bucket, prefix)
    assert f"{prefix}skills/foo.md" in keys  # genuine content still lands


@pytest.mark.asyncio
async def test_agentcore_install_without_a_seller_soul_md_keeps_the_roster_derived_identity(
    s3_hermes_bucket, monkeypatch
):
    roster = [{"id": "minted_agent_1", "name": "Ada", "role": "general"}]
    _wire_agentcore_profile_writer(monkeypatch, roster)
    tar_bytes = _make_tar_gz({"skills/foo.md": b"# Foo"})  # no SOUL.md in the artifact
    container = {"runtime_host": "agentcore"}

    soul_md = catalog_service._read_soul_md_from_tar(tar_bytes)
    assert soul_md is None
    await catalog_service._build_and_upload_agent_profile(
        owner_id="owner_1", agent_id="minted_agent_1", container=container, soul_md_override=soul_md
    )
    await catalog_service.deploy_tarball_to_profile(
        owner_id="owner_1", agent_id="minted_agent_1", tar_bytes=tar_bytes, container=container
    )

    prefix = "owner_1/hermes/minted_agent_1/"
    uploaded_soul = s3_hermes_bucket.get_object(Bucket="test-replicas", Key=f"{prefix}SOUL.md")["Body"].read().decode()
    assert "Ada" in uploaded_soul  # build_soul_md's own roster-derived identity, unchanged
    assert "seller" not in uploaded_soul.lower()


@pytest.mark.asyncio
async def test_install_agentcore_deploy_reads_the_tarball_soul_md_before_writing_the_profile():
    """_install_agentcore_deploy itself: the seller's SOUL.md is read out of the tarball and
    threaded into _build_and_upload_agent_profile's soul_md_override BEFORE the content pass
    (deploy_tarball_to_profile) runs -- ordering unchanged from M6-R9."""
    tar_bytes = _make_tar_gz({"SOUL.md": b"# From the tarball\n"})
    call_order: list[str] = []
    captured = {}

    async def _create(**_kwargs):
        call_order.append("create")
        return {"id": "minted_agent_1"}

    async def _profile(**kwargs):
        call_order.append("profile")
        captured["soul_md_override"] = kwargs.get("soul_md_override")

    async def _content(**_kwargs):
        call_order.append("content")

    with (
        patch("core.services.paperclip_agent_lifecycle.create_paperclip_for_marketplace", side_effect=_create),
        patch("core.services.paperclip_agent_lifecycle.delete_paperclip_for_marketplace", new=AsyncMock()),
        patch("core.services.catalog_service._build_and_upload_agent_profile", side_effect=_profile),
        patch("core.services.catalog_service.deploy_tarball_to_profile", side_effect=_content),
    ):
        await catalog_service._install_agentcore_deploy(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            tar_bytes=tar_bytes,
            container={"runtime_host": "agentcore"},
            new_agent_id=None,
            listing_id=None,
            seller_id=None,
        )

    assert call_order == ["create", "profile", "content"]
    assert captured["soul_md_override"] == "# From the tarball\n"


# ---------------------------------------------------------------------------
# _install_agentcore_deploy / deploy_from_artifact -- ordering + rollback
# ---------------------------------------------------------------------------


def _make_service_for_deploy():
    """``CatalogService`` is stateless now. Its three injected collaborators -- a ``Workspace``
    (EFS tarball extraction + the ``.template`` sidecar + cron reads), the openclaw.json aux
    merger, and the gateway pair fn -- have no successor: the install is two S3 writes."""
    return CatalogService()


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
    svc = _make_service_for_deploy()
    call_order: list[str] = []
    with contextlib.ExitStack() as stack:
        delete_mock = _patch_agentcore_collaborators(stack, call_order)
        result = await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_={"agent": {"name": "Demo"}},
            tar_bytes=_make_tar_gz({}),
            new_agent_id=None,
            container={"runtime_host": "agentcore"},
        )

    assert call_order == ["create", "profile", "content"]
    assert result["agent_id"] == "minted_agent_1"
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_deploy_from_artifact_reads_the_row_when_no_container_is_threaded():
    """M6 task-14 row 19: the branch used to key on the ``container`` PARAMETER, defaulting to the
    EFS path on ``None`` -- correct only while every caller remembered to pass it, and a caller
    that forgot extracted a marketplace agent onto a tree no migrated agent reads and reported a
    successful deploy. There is one path now, and the parameter is a pure optimisation: ``None``
    is READ here rather than deciding anything."""
    svc = _make_service_for_deploy()
    call_order: list[str] = []
    with contextlib.ExitStack() as stack:
        _patch_agentcore_collaborators(stack, call_order)
        read_row = stack.enter_context(
            patch(
                "core.repositories.container_repo.get_by_owner_id",
                new=AsyncMock(return_value={"agentcore_runtime_arn": "arn:r1"}),
            )
        )
        result = await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_={"agent": {"name": "Demo"}},
            tar_bytes=_make_tar_gz({}),
            new_agent_id=None,
        )

    assert result["agent_id"] == "minted_agent_1"
    read_row.assert_awaited_once_with("user_buyer")


@pytest.mark.asyncio
async def test_deploy_from_artifact_does_not_re_mint_a_pre_supplied_agentcore_id():
    """Every real caller (marketplace deploy, hire, agent builder) already
    minted the Paperclip agent before calling deploy_from_artifact -- a
    second mint here would orphan a different id than the one being
    deployed."""
    svc = _make_service_for_deploy()
    call_order: list[str] = []
    with contextlib.ExitStack() as stack:
        delete_mock = _patch_agentcore_collaborators(stack, call_order)
        result = await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_={"agent": {"name": "Demo"}},
            tar_bytes=_make_tar_gz({}),
            new_agent_id="agent_abc",
            container={"runtime_host": "agentcore"},
        )

    assert call_order == ["profile", "content"]  # no "create"
    assert result["agent_id"] == "agent_abc"
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_deploy_from_artifact_rolls_back_self_minted_agent_on_profile_failure():
    svc = _make_service_for_deploy()
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
                tar_bytes=_make_tar_gz({}),
                new_agent_id=None,
                container={"runtime_host": "agentcore"},
            )

    delete_mock.assert_awaited_once_with(owner_id="user_buyer", agent_id="minted_agent_1")


@pytest.mark.asyncio
async def test_deploy_from_artifact_never_rolls_back_a_pre_supplied_agentcore_id_on_failure():
    """A failure on a pre-minted agent is the CALLER's rollback envelope
    (mirrors every ECS caller's existing unwind) -- this function must not
    delete an agent it didn't create."""
    svc = _make_service_for_deploy()
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
                tar_bytes=_make_tar_gz({}),
                new_agent_id="agent_abc",
                container={"runtime_host": "agentcore"},
            )

    delete_mock.assert_not_awaited()
