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

import io  # noqa: E402
import json  # noqa: E402
import tarfile  # noqa: E402
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
async def test_deploy_pair_extras_limited_to_agents_update_schema():
    """OpenClaw's ``agents.update`` AJV schema is narrow
    (``additionalProperties: false`` — only
    ``agentId, name, workspace, model, emoji, avatar``). Forwarding the
    seller's full agent dict (``identity`` as a nested object,
    ``skills``, ``reasoningDefault``, ``agentRuntime``, etc.) makes the
    RPC 422 and leaves the agent stuck with name=<uuid>. ``extras``
    must contain ONLY ``emoji`` / ``avatar`` (extracted from the
    nested ``identity`` block) — everything else rides on the file-merge
    path (the ``agent_entry`` arg to ``apply_deploy_aux_mutations``).
    """
    svc, _ws, _aux, pair_fn, gateway_pool = _make_service()

    slice_ = _slice_with_agent()
    # Seller stored these fields on their agent — they MUST NOT leak
    # into agents.update's params.
    slice_["agent"]["identity"] = {"name": "Demo", "emoji": "🍃", "avatar": "monstera"}
    slice_["agent"]["reasoningDefault"] = "high"
    slice_["agent"]["agentRuntime"] = {"maxTurns": 50}

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

    # extras: ONLY emoji + avatar from the seller's identity block.
    # The narrow schema is the load-bearing invariant.
    extras = kw["extra_update_fields"]
    assert extras == {"emoji": "🍃", "avatar": "monstera"}


@pytest.mark.asyncio
async def test_deploy_file_merges_full_agent_entry_into_agents_list():
    """The valid per-agent fields the RPC can't carry (skills binding,
    reasoningDefault, identity, tts, etc.) must land via the file-merge
    path so chokidar (1-30s) reloads them onto the buyer's in-memory
    agent. Keys upstream removed from the strict AgentEntrySchema
    (embeddedHarness, agentRuntime, systemPromptOverride) must instead be
    STRIPPED at deploy so an older immutable artifact can't write a
    now-invalid key into the buyer's openclaw.json and crash-loop it.
    """
    svc, _ws, apply_aux, _pair, _gw = _make_service()

    slice_ = _slice_with_agent()
    slice_["agent"]["identity"] = {"name": "Demo", "emoji": "🍃"}
    slice_["agent"]["reasoningDefault"] = "high"
    # Removed-upstream keys a seller's pre-upgrade config may still carry.
    slice_["agent"]["agentRuntime"] = {"maxTurns": 50}
    slice_["agent"]["systemPromptOverride"] = "be terse"
    slice_["agent"]["embeddedHarness"] = {}

    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=slice_,
        tar_bytes=b"<gz>",
        container_ip="10.0.0.5",
        container_token="tok",
        new_agent_id="agent_new",
    )

    apply_aux.assert_awaited_once()
    kw = apply_aux.await_args.kwargs
    # agent_entry threaded through with the buyer's resolved id + workspace
    # and the full per-agent config preserved.
    entry = kw["agent_entry"]
    assert entry["id"] == "agent_new"
    assert entry["workspace"] == ".openclaw/workspace-agent_new"
    assert entry["skills"] == ["search"]
    assert entry["reasoningDefault"] == "high"
    assert entry["identity"] == {"name": "Demo", "emoji": "🍃"}
    # Removed-upstream keys must NOT survive into the buyer's config.
    assert "agentRuntime" not in entry
    assert "systemPromptOverride" not in entry
    assert "embeddedHarness" not in entry


@pytest.mark.asyncio
async def test_deploy_falls_back_to_identity_name_for_display():
    """If the seller's agent has ``identity.name`` but no top-level
    ``name`` (older snapshot shape), use the nested name for the
    sidebar display instead of falling back to the raw UUID.
    """
    svc, _ws, _aux, pair_fn, _gw = _make_service()

    slice_ = _slice_with_agent()
    del slice_["agent"]["name"]
    slice_["agent"]["identity"] = {"name": "Ray"}

    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=slice_,
        tar_bytes=b"<gz>",
        container_ip="10.0.0.5",
        container_token="tok",
        new_agent_id="agent_xyz",
    )

    pair_fn.assert_awaited_once()
    assert pair_fn.await_args.kwargs["paperclip_agent"]["name"] == "Ray"


@pytest.mark.asyncio
async def test_deploy_pair_failure_propagates_and_skips_aux():
    """If ``pair_with_openclaw`` raises, we MUST NOT proceed to the file
    merge — leaving plugins/mcp/skills for an agent the runtime doesn't
    know about would silently corrupt the buyer's config. The exception
    propagates; we do not roll back the extracted workspace (see
    ``deploy_from_artifact`` docstring "Rollback posture (v0)").
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
    ws.cleanup_agent_dirs.assert_not_called()


@pytest.mark.asyncio
async def test_deploy_aux_failure_leaves_workspace_intact_for_live_agent():
    """If the aux merge fails AFTER a successful pair, the agent is live
    in the container's in-memory state and its workspace files MUST stay
    on EFS. Wiping them would phantom the agent. The exception propagates
    so the caller can retry; partial state is functionally invisible
    (no sidecar → not in installed-templates UI).
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

    ws.cleanup_agent_dirs.assert_not_called()


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


def _make_tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _tar_names(tar_bytes: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        return [m.name for m in tf.getmembers()]


def _extracted_tar_bytes(workspace) -> bytes:
    return workspace.extract_tarball_to_workspace.call_args.kwargs["tar_bytes"]


async def _deploy_minimal(svc, tar_bytes: bytes) -> dict:
    return await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_={},
        tar_bytes=tar_bytes,
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_abc",
    )


@pytest.mark.asyncio
async def test_deploy_strips_boot_and_plugin_manifest_members():
    """Already-published artifacts predate the export-side filter, so the
    buyer-side deploy must ALSO strip BOOT.md (autonomous LLM run on every
    gateway restart) and openclaw.plugin.json (loads workspace code
    in-process) — at any depth — without failing the deploy.
    """
    svc, ws, _aux, _pair, _gw = _make_service()
    tar = _make_tar_gz(
        {
            "./README.md": b"# hi",
            "./BOOT.md": b"do things on boot",
            "./openclaw.plugin.json": b"{}",
            "./nested/BOOT.md": b"nested boot",
            "./nested/openclaw.plugin.json": b"{}",
        }
    )
    await _deploy_minimal(svc, tar)
    names = _tar_names(_extracted_tar_bytes(ws))
    assert "./README.md" in names
    assert not any(n.rsplit("/", 1)[-1] == "BOOT.md" for n in names), names
    assert not any(n.rsplit("/", 1)[-1] == "openclaw.plugin.json" for n in names), names


@pytest.mark.asyncio
async def test_deploy_strips_package_json_only_when_it_declares_extensions():
    """package.json is a legitimate workspace file UNLESS it carries an
    ``openclaw.extensions`` key (workspace-plugin discovery). Malformed JSON
    is stripped too — fail-closed, since we can't prove it's inert."""
    svc, ws, _aux, _pair, _gw = _make_service()
    tar = _make_tar_gz(
        {
            "./package.json": json.dumps({"name": "benign", "version": "1.0.0"}).encode(),
            "./evil/package.json": json.dumps({"openclaw": {"extensions": ["./dist/index.js"]}}).encode(),
            "./dotted/package.json": json.dumps({"openclaw.extensions": ["./x.js"]}).encode(),
            "./broken/package.json": b"{not json",
        }
    )
    await _deploy_minimal(svc, tar)
    names = _tar_names(_extracted_tar_bytes(ws))
    assert "./package.json" in names
    assert "./evil/package.json" not in names, names
    assert "./dotted/package.json" not in names, names
    assert "./broken/package.json" not in names, names


@pytest.mark.asyncio
async def test_deploy_clean_tarball_passes_through_unchanged():
    """No banned members → the exact original bytes reach the workspace
    (no re-encode; preserves the artifact's deterministic byte identity)."""
    svc, ws, _aux, _pair, _gw = _make_service()
    tar = _make_tar_gz({"./README.md": b"# hi", "./skills/foo.md": b"skill"})
    await _deploy_minimal(svc, tar)
    assert _extracted_tar_bytes(ws) == tar


@pytest.mark.asyncio
async def test_deploy_unparseable_tar_bytes_pass_through():
    """The strip filter is defense-in-depth, not a validity gate — bytes the
    stdlib can't parse go through unchanged so untar_to_directory stays the
    parser of record (and its error surfaces, not ours)."""
    svc, ws, _aux, _pair, _gw = _make_service()
    await _deploy_minimal(svc, b"<gz>")
    assert _extracted_tar_bytes(ws) == b"<gz>"


@pytest.mark.asyncio
async def test_deploy_pins_containment_on_agent_entry():
    """Marketplace agents carry third-party-authored instructions; the
    on-disk agents.list entry must pin subagents.allowAgents to the agent's
    own id (else it can sessions_spawn under ANY sibling id, including the
    premium-pinned CEO) and deny the gateway + sessions_send tools."""
    svc, _ws, apply_aux, _pair, _gw = _make_service()
    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_new",
    )
    entry = apply_aux.await_args.kwargs["agent_entry"]
    assert entry["subagents"] == {"allowAgents": ["agent_new"]}
    assert set(entry["tools"]["deny"]) >= {"gateway", "sessions_send"}
    # Deny, never a narrowing allow — an allow-list resolving to zero
    # callable tools kills runs.
    assert "allow" not in entry["tools"]


@pytest.mark.asyncio
async def test_deploy_containment_overrides_seller_authored_values():
    """A seller-authored subagents.allowAgents (e.g. ["*"]) must be
    overwritten, and seller deny entries survive alongside ours; unrelated
    sibling keys on both blocks are preserved."""
    svc, _ws, apply_aux, _pair, _gw = _make_service()
    slice_ = _slice_with_agent()
    slice_["agent"]["subagents"] = {"allowAgents": ["*"], "maxConcurrent": 3}
    slice_["agent"]["tools"] = {"deny": ["browser"], "profile": "default"}
    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=slice_,
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_new",
    )
    entry = apply_aux.await_args.kwargs["agent_entry"]
    assert entry["subagents"]["allowAgents"] == ["agent_new"]
    assert entry["subagents"]["maxConcurrent"] == 3
    assert set(entry["tools"]["deny"]) >= {"browser", "gateway", "sessions_send"}
    assert entry["tools"]["profile"] == "default"


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
                "args": ["/home/node/.openclaw/workspace-__AGENT_DIR__/mcp-resy/index.js"],
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
async def test_deploy_normalizes_legacy_workspaces_sentinel_form():
    """Artifacts published before the workspace-layout migration carry
    ``workspaces/__AGENT_DIR__`` paths in their slices. Deploy must
    normalize that OLD spelling to ``workspace-<buyer_agent_id>`` — same
    result as a new-form artifact — so every published listing keeps
    deploying after the layout change.
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
    assert "workspaces/" not in args[0], args[0]
    assert "workspace-agent_xyz" in args[0], args[0]
    assert args[0].endswith("/mcp-resy/index.js"), args[0]


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
    """Sidebar's already-deployed filter scans ``workspace-{id}`` sidecars by
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


@pytest.mark.asyncio
async def test_deploy_reconciles_integration_servers(monkeypatch):
    """The deep-merge lands the seller's bundled integration servers with their
    credentials scrubbed blank. After it, deploy re-asserts the buyer's own
    connected integrations from the durable sources so the hired agent gets a
    canonical, credentialed server block — not the empty slot the slice carried.

    The obsolete per-slice slot-fill helper + its canonical-key guard are gone;
    a single owner-scoped reconcile replaces them.
    """
    import core.services.catalog_service as cs

    assert not hasattr(cs, "_fill_buyer_credential_slots")
    assert not hasattr(cs, "_slice_exposes_canonical_key")

    called: dict[str, str] = {}

    async def _reconcile(owner_id):
        called["owner"] = owner_id

    monkeypatch.setattr(cs, "reconcile_integrations", _reconcile)
    # The pre-deploy gate reads the owner's connected set; the bundled stripe
    # server derives a stripe requirement, so mark it connected to clear the gate
    # (the reconcile, not the gate, is under test here).
    monkeypatch.setattr(
        "core.repositories.integrations_repo.list_for_owner",
        AsyncMock(return_value=[{"provider": "stripe", "status": "connected"}]),
    )

    svc, _ws, _aux, _pair, _gw = _make_service()
    # The slice bundles the Stripe registry server with its credential scrubbed
    # blank (catalog_slice) — the realistic hire path the reconcile re-credentials.
    slice_ = _slice_with_agent(mcp={"servers": {"stripe": {"command": "stripe-mcp", "env": {"STRIPE_SECRET_KEY": ""}}}})

    await svc.deploy_from_artifact(
        owner_id="org_1",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=slice_,
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_abc",
    )

    assert called["owner"] == "org_1"


@pytest.mark.asyncio
async def test_deploy_reconcile_failure_does_not_abort(monkeypatch):
    """A reconcile hiccup at deploy time (e.g. a Secrets-Manager blip) must not
    unwind an already-deployed agent. The slot is left uncredentialed for the
    buyer to re-connect; the deploy still completes and writes its sidecar.
    """
    import core.services.catalog_service as cs

    async def _boom(owner_id):
        raise RuntimeError("secrets manager unavailable")

    monkeypatch.setattr(cs, "reconcile_integrations", _boom)

    svc, ws, _aux, _pair, _gw = _make_service()

    await svc.deploy_from_artifact(
        owner_id="org_1",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_abc",
    )

    # Deploy completed despite the reconcile failure.
    ws.write_template_sidecar.assert_called_once()


# ---------------------------------------------------------------------------
# ACP coding-harness runtime enablement (manifest.requires_acp)
# ---------------------------------------------------------------------------


def _patch_acp_deps(monkeypatch, *, provider=("bedrock_claude", None)):
    """Wire up the three deploy-side ACP dependencies and return the
    AsyncMock standing in for ``patch_openclaw_config`` so tests can assert
    whether (and with what) the buyer's openclaw.json was patched.
    """
    import core.services.config_patcher as cp
    import core.services.container_provision as cprov

    monkeypatch.setattr(cprov, "resolve_provider_choice", AsyncMock(return_value=provider))
    patch_cfg = AsyncMock(return_value=None)
    monkeypatch.setattr(cp, "patch_openclaw_config", patch_cfg)
    return patch_cfg


@pytest.mark.asyncio
async def test_deploy_enables_acp_when_required(monkeypatch):
    """requires_acp + a harness-capable provider → the buyer's
    container-root acp block + acpx plugin are patched in, and the result
    reports acp_enabled."""
    patch_cfg = _patch_acp_deps(monkeypatch)
    svc, _ws, _aux, _pair, _gw = _make_service()

    result = await svc.deploy_from_artifact(
        owner_id="org_1",
        slug="demo",
        manifest={"name": "Demo", "version": 1, "requires_acp": True},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_abc",
    )

    patch_cfg.assert_awaited_once()
    owner_arg, patch_arg = patch_cfg.await_args.args
    assert owner_arg == "org_1"
    # Container-root acp capability (sibling of agents), not a per-agent key.
    assert patch_arg["acp"]["enabled"] is True
    assert patch_arg["acp"]["allowedAgents"] == ["claude"]  # bedrock_claude
    assert "acpx" in patch_arg["plugins"]["entries"]
    assert result["acp_requested"] is True
    assert result["acp_enabled"] is True


@pytest.mark.asyncio
async def test_deploy_skips_acp_when_provider_has_no_harness(monkeypatch):
    """ACP on, but the buyer's provider can't authenticate any harness
    (derive_enabled_harnesses → []) → no patch, acp_enabled False."""
    patch_cfg = _patch_acp_deps(monkeypatch, provider=("byo_key", "google"))
    svc, _ws, _aux, _pair, _gw = _make_service()

    result = await svc.deploy_from_artifact(
        owner_id="org_1",
        slug="demo",
        manifest={"name": "Demo", "version": 1, "requires_acp": True},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_abc",
    )

    patch_cfg.assert_not_awaited()
    assert result["acp_enabled"] is False


@pytest.mark.asyncio
async def test_deploy_no_acp_when_not_required(monkeypatch):
    """A listing that doesn't declare requires_acp never touches the ACP path,
    even with the flag on."""
    patch_cfg = _patch_acp_deps(monkeypatch)
    svc, _ws, _aux, _pair, _gw = _make_service()

    result = await svc.deploy_from_artifact(
        owner_id="org_1",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_abc",
    )

    patch_cfg.assert_not_awaited()
    assert result["acp_requested"] is False
    assert result["acp_enabled"] is False


@pytest.mark.asyncio
async def test_deploy_acp_failure_does_not_abort(monkeypatch):
    """A config-patch hiccup while enabling ACP must not unwind an
    already-deployed agent — the deploy completes, just without harness
    delegation."""
    import core.services.config_patcher as cp
    import core.services.container_provision as cprov

    monkeypatch.setattr(cprov, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))

    async def _boom(owner_id, patch):
        raise RuntimeError("EFS lock contention")

    monkeypatch.setattr(cp, "patch_openclaw_config", _boom)

    svc, ws, _aux, _pair, _gw = _make_service()

    result = await svc.deploy_from_artifact(
        owner_id="org_1",
        slug="demo",
        manifest={"name": "Demo", "version": 1, "requires_acp": True},
        slice_=_slice_with_agent(),
        tar_bytes=b"<gz>",
        container_ip="10.0.0.1",
        container_token="tok",
        new_agent_id="agent_abc",
    )

    ws.write_template_sidecar.assert_called_once()
    assert result["acp_enabled"] is False


@pytest.mark.asyncio
async def test_unwind_partial_deploy_removes_config_entry_and_efs_dirs(monkeypatch):
    """A failed deploy's unwind must drop the openclaw.json agents.list entry
    (matched by id) AND clean the on-EFS agent dirs, so no name=<uuid> ghost
    is stranded in the buyer's container."""
    svc, ws, _aux, _pair, _gw = _make_service()
    removed = AsyncMock(return_value=None)
    monkeypatch.setattr("core.services.config_patcher.remove_from_openclaw_config_list", removed)

    await svc.unwind_partial_deploy("user_buyer", "agent_ghost")

    removed.assert_awaited_once()
    owner_arg, path_arg, predicate = removed.await_args.args
    assert owner_arg == "user_buyer"
    assert path_arg == ["agents", "list"]
    # predicate removes only the ghost entry, by id, and tolerates non-dicts.
    assert predicate({"id": "agent_ghost"}) is True
    assert predicate({"id": "agent_other"}) is False
    assert predicate("not-a-dict") is False

    ws.cleanup_agent_dirs.assert_called_once_with("user_buyer", "agent_ghost")


@pytest.mark.asyncio
async def test_unwind_partial_deploy_is_best_effort(monkeypatch):
    """Cleanup failures must not raise — the caller has to surface the
    ORIGINAL deploy error, not a rollback error. Both legs are attempted."""
    svc, ws, _aux, _pair, _gw = _make_service()
    monkeypatch.setattr(
        "core.services.config_patcher.remove_from_openclaw_config_list",
        AsyncMock(side_effect=RuntimeError("config locked")),
    )
    ws.cleanup_agent_dirs = MagicMock(side_effect=OSError("efs gone"))

    # Must not raise despite both legs failing.
    await svc.unwind_partial_deploy("user_buyer", "agent_ghost")

    # EFS cleanup is still attempted even after the config removal raised.
    ws.cleanup_agent_dirs.assert_called_once_with("user_buyer", "agent_ghost")
