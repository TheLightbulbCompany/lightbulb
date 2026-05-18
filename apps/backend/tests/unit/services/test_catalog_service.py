"""Tests for catalog_service.deploy_from_artifact.

The deploy path is split: the agent entry goes through the OpenClaw gateway's
``agents.create`` / ``agents.update`` RPCs (live container state, no
chokidar reload race) while the rest of the slice (plugins / mcp / skills)
is deep-merged into ``openclaw.json`` via ``apply_deploy_aux_mutations``.

These tests pin the ordering, the kwargs threading, and the rollback
posture; they do NOT exercise the EFS / config-patcher side effects (those
live in their own tests).
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from decimal import Decimal  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

from core.services.catalog_service import CatalogService  # noqa: E402


def _make_service(call_order: list[str] | None = None):
    """Construct a CatalogService with fully mocked collaborators.

    When ``call_order`` is passed, the workspace + pair + aux mocks append
    a short tag to it so tests can pin the deploy step ordering.
    """
    workspace = MagicMock()
    pair_fn = AsyncMock(return_value=None)
    apply_aux = AsyncMock(return_value=None)

    if call_order is not None:
        workspace.extract_tarball_to_workspace = MagicMock(side_effect=lambda **_: call_order.append("extract"))
        pair_fn.side_effect = lambda **_: call_order.append("pair")
        apply_aux.side_effect = lambda *_a, **_k: call_order.append("aux")
        workspace.write_template_sidecar = MagicMock(side_effect=lambda **_: call_order.append("sidecar"))
    else:
        workspace.extract_tarball_to_workspace = MagicMock(return_value=None)
        workspace.write_template_sidecar = MagicMock(return_value=None)

    workspace.read_cron_jobs = MagicMock(return_value=[])
    workspace.cleanup_agent_dirs = MagicMock(return_value=None)
    gateway_pool = MagicMock()
    gateway_pool_factory = MagicMock(return_value=gateway_pool)

    svc = CatalogService(
        workspace=workspace,
        apply_deploy_aux_mutations=apply_aux,
        pair_fn=pair_fn,
        gateway_pool_factory=gateway_pool_factory,
    )
    return svc, workspace, apply_aux, pair_fn, gateway_pool


def _slice_with_agent(**overrides):
    """Minimal slice that triggers the slice-has-agent code path."""
    out = {
        "agent": {"name": "Demo", "skills": ["search"], "model": "claude-3-haiku"},
        "plugins": {"entries": {"memory": {"enabled": True}}},
        "tools": {},
        "cron_jobs": [],
    }
    out.update(overrides)
    return out


@pytest.mark.asyncio
async def test_deploy_calls_pair_before_aux_merge():
    """Agent entry must register via the gateway BEFORE the aux file merge —
    that's the whole point of the refactor. If aux runs first (or the only
    path), the runtime would see plugins/mcp/skills for an agent it doesn't
    know about yet.
    """
    order: list[str] = []
    svc, _ws, _aux, _pair, _gw = _make_service(call_order=order)

    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_abc",
    )

    assert order == ["extract", "pair", "aux", "sidecar"], order


@pytest.mark.asyncio
async def test_deploy_threads_agent_entry_through_pair_extra_update_fields():
    """The slice's agent dict (minus ``id`` and ``workspace`` which the
    helper owns) must arrive at ``pair_with_openclaw`` as
    ``extra_update_fields``. The helper force-overrides ``agentId`` +
    ``name`` regardless, so callers can leave id/name in but we MUST NOT
    forward the synthesized ``workspace`` path (it would be wrong shape
    for ``agents.update`` and is derived from the create RPC's name).
    """
    svc, _ws, _aux, pair_fn, gateway_pool = _make_service()

    slice_ = _slice_with_agent()
    # Inject a few extra fields the slice typically carries
    slice_["agent"]["identity"] = {"role": "concierge"}
    slice_["agent"]["reasoningDefault"] = "high"

    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=slice_,
        tar_bytes=b"<gz>",
        container_ip="10.0.0.5",
        container_token="tok-xyz",
        new_agent_id="paperclip-uuid-7777",
    )

    pair_fn.assert_awaited_once()
    kw = pair_fn.await_args.kwargs
    assert kw["owner_id"] == "user_buyer"
    assert kw["ip"] == "10.0.0.5"
    assert kw["token"] == "tok-xyz"
    assert kw["gateway_pool"] is gateway_pool
    assert kw["paperclip_agent"] == {
        "id": "paperclip-uuid-7777",
        "name": "Demo",
        "model": "claude-3-haiku",
    }

    # extras: contains the agent fields except the helper-owned id + workspace.
    extras = kw["extra_update_fields"]
    assert "id" not in extras
    assert "workspace" not in extras
    # Forwarded slice fields are present:
    assert extras["identity"] == {"role": "concierge"}
    assert extras["reasoningDefault"] == "high"
    assert extras["skills"] == ["search"]
    assert extras["name"] == "Demo"
    assert extras["model"] == "claude-3-haiku"


@pytest.mark.asyncio
async def test_deploy_pair_failure_skips_aux_and_rolls_back_workspace():
    """If ``pair_with_openclaw`` raises, we MUST NOT proceed to the file
    merge — leaving plugins/mcp/skills for an agent the runtime doesn't
    know about would silently corrupt the buyer's config. Workspace dir
    cleanup must still run idempotently.
    """
    svc, ws, apply_aux, pair_fn, _gw = _make_service()
    pair_fn.side_effect = RuntimeError("gateway boom")

    with pytest.raises(RuntimeError, match="gateway boom"):
        await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_=_slice_with_agent(),
            tar_bytes=b"<gz>",
            container_ip="10.0.0.1",
            container_token="tok",
            new_agent_id="agent_abc",
        )

    apply_aux.assert_not_awaited()
    ws.cleanup_agent_dirs.assert_called_once_with("user_buyer", "agent_abc")


@pytest.mark.asyncio
async def test_deploy_aux_failure_cleans_workspace_but_agent_stays_paired():
    """If the aux merge fails AFTER a successful pair, the workspace dir is
    cleaned up but the agent REMAINS registered in the container's
    in-memory state — no inverse ``agents.delete`` RPC is wired today
    (see ``deploy_from_artifact`` docstring "Rollback posture"). On-call
    incident keyword: phantom agent after failed marketplace deploy.
    """
    svc, ws, apply_aux, _pair, _gw = _make_service()
    apply_aux.side_effect = RuntimeError("aux boom")

    with pytest.raises(RuntimeError, match="aux boom"):
        await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_=_slice_with_agent(),
            tar_bytes=b"<gz>",
            container_ip="10.0.0.1",
            container_token="tok",
            new_agent_id="agent_abc",
        )

    ws.cleanup_agent_dirs.assert_called_once_with("user_buyer", "agent_abc")


@pytest.mark.asyncio
async def test_deploy_legacy_slice_with_no_agent_skips_pair_and_aux():
    """Legacy listings published before slice-synthesis shipped have an
    empty slice. The deploy must still extract files + write the sidecar
    but skip BOTH the pair RPC and the aux file merge — the buyer wires
    the agent by hand.
    """
    svc, ws, apply_aux, pair_fn, _gw = _make_service()
    result = await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="legacy",
        manifest={"name": "Legacy", "version": 1},
        slice_={},
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_legacy",
    )
    assert result["config_registered"] is False
    pair_fn.assert_not_awaited()
    apply_aux.assert_not_awaited()
    # Sidecar is written either way so the buyer can still see it in the
    # installed-templates list.
    ws.write_template_sidecar.assert_called_once()


@pytest.mark.asyncio
async def test_deploy_uses_supplied_new_agent_id_in_sentinel_substitution():
    """When a buyer-side id is pinned (marketplace flow passes Paperclip's
    UUID), the __AGENT_DIR__ sentinel in mcp args must be substituted with
    that exact id — not a freshly-generated one.
    """
    svc, _ws, apply_aux, _pair, _gw = _make_service()
    slice_ = _slice_with_agent()
    slice_["mcp"] = {
        "servers": {
            "resy": {
                "command": "node",
                "args": ["/home/node/.openclaw/workspaces/__AGENT_DIR__/mcp-resy/index.js"],
                "env": {"RESY_EMAIL": ""},
            }
        }
    }

    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=slice_,
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_xyz",
    )

    apply_aux.assert_awaited_once()
    mcp_patch = apply_aux.await_args.kwargs.get("mcp_patch")
    args = mcp_patch["servers"]["resy"]["args"]
    assert "__AGENT_DIR__" not in args[0]
    assert "agent_xyz" in args[0]


@pytest.mark.asyncio
async def test_deploy_generates_uuid_when_new_agent_id_omitted():
    """Direct callers (tests, future code paths) that don't pin an id get a
    locally-generated ``agent_<hex>`` UUID. The marketplace flow always
    pins; this path keeps non-marketplace deploys ergonomic.
    """
    svc, _ws, _aux, _pair, _gw = _make_service()
    result = await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
    )
    assert result["agent_id"].startswith("agent_")
    assert len(result["agent_id"]) > len("agent_")


@pytest.mark.asyncio
async def test_deploy_carries_cron_jobs_with_remapped_id_and_agent():
    """Slice cron jobs are appended AFTER the agent is registered, with
    a fresh per-deploy id, the buyer's agentId stamped in, and the
    sessionKey rebuilt to OpenClaw's ``agent:{id}:{user_lower}`` shape.
    """
    appended: list[tuple] = []

    async def _append_cron(owner_id, jobs):
        appended.append((owner_id, jobs))

    import core.services.config_patcher as cp

    original = cp.append_cron_jobs
    cp.append_cron_jobs = _append_cron
    try:
        svc, _ws, _aux, _pair, _gw = _make_service()
        slice_ = _slice_with_agent()
        slice_["cron_jobs"] = [
            {
                "agentId": "publisher-agent",
                "name": "morning standup",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "delivery": {"channel": "telegram", "accountId": "publisher-agent"},
            }
        ]
        result = await svc.deploy_from_artifact(
            owner_id="user_u",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_=slice_,
            tar_bytes=b"<gz>",
            container_ip="10.0.0.1",
            container_token="tok",
        )
    finally:
        cp.append_cron_jobs = original

    assert appended, "cron jobs should be appended after the agent is registered"
    owner_id, jobs = appended[0]
    assert owner_id == "user_u"
    assert len(jobs) == 1
    job = jobs[0]
    assert job["agentId"] == result["agent_id"]
    assert job["delivery"]["accountId"] == result["agent_id"]
    assert job["sessionKey"] == f"agent:{result['agent_id']}:user_u"


@pytest.mark.asyncio
async def test_deploy_uses_owner_id_for_all_efs_writes():
    """``owner_id`` is the partition key for every EFS + config-patch
    operation; a typo here would scatter the deploy across two users'
    partitions.
    """
    svc, ws, apply_aux, pair_fn, _gw = _make_service()
    await svc.deploy_from_artifact(
        owner_id="org_xyz",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_abc",
    )
    assert ws.extract_tarball_to_workspace.call_args.kwargs["user_id"] == "org_xyz"
    assert ws.write_template_sidecar.call_args.kwargs["user_id"] == "org_xyz"
    assert apply_aux.await_args.args[0] == "org_xyz"
    assert pair_fn.await_args.kwargs["owner_id"] == "org_xyz"


def test_list_deployed_for_user_keyed_by_owner_id():
    """Sidebar's already-deployed filter scans ``workspaces/`` sidecars by
    owner_id (org_id for orgs, user_id for personal-mode). Each sidecar
    becomes ``{agent_id, template_slug, template_version}``.
    """
    workspace = MagicMock()
    workspace.list_workspace_agent_dirs.return_value = ["agent_a", "agent_b", "agent_c"]
    workspace.read_template_sidecar.side_effect = [
        {"template_slug": "pitch", "template_version": 3},
        None,
        {"template_slug": "scout", "template_version": 1},
    ]
    svc = CatalogService(
        workspace=workspace,
        apply_deploy_aux_mutations=AsyncMock(),
        pair_fn=AsyncMock(),
    )

    out = svc.list_deployed_for_user("org_xyz")

    workspace.list_workspace_agent_dirs.assert_called_once_with("org_xyz")
    assert out == [
        {"agent_id": "agent_a", "template_slug": "pitch", "template_version": 3},
        {"agent_id": "agent_c", "template_slug": "scout", "template_version": 1},
    ]


@pytest.mark.asyncio
async def test_deploy_coerces_decimal_version_in_sidecar():
    """Regression for the dev-tested marketplace deploy 500: boto3 returns
    DDB Numbers as ``Decimal``, so the listing row's ``version`` arrives
    as ``Decimal``. The sidecar write goes through ``json.dumps`` which
    raises ``TypeError: Object of type Decimal is not JSON serializable``.
    Coerce to int at the write boundary.
    """
    svc, ws, _aux, _pair, _gw = _make_service()
    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": Decimal("1")},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_test",
    )
    ws.write_template_sidecar.assert_called_once()
    content = ws.write_template_sidecar.call_args.kwargs["content"]
    assert isinstance(content["template_version"], int)
    assert content["template_version"] == 1
