"""Tests for the image-update apply path + the pre-roll config migration.

A container rolled to a newer image reuses its existing on-disk
openclaw.json. ``migrate_removed_config_keys`` strips keys the new image's
strict schema no longer accepts (e.g. ``agents.defaults.embeddedHarness``,
removed in 2026.5.28) before the roll, so the gateway doesn't crash-loop on
boot. These tests pin that the migration runs, targets the right path, and
runs BEFORE the ECS image roll.
"""

import asyncio

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import update_service


@pytest.fixture(autouse=True)
def _ecs_owner_by_default():
    """The write doors below ask ``is_agentcore_owner(fail_closed=True)``, which raises rather
    than guessing when the container read fails -- and there is no DynamoDB here. Answer ECS;
    the AgentCore tests override it per test."""
    with patch("core.services.update_service.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=False)):
        yield


def _claimed_repo(update_item: dict) -> MagicMock:
    repo = MagicMock()
    repo.claim_apply = AsyncMock(return_value={**update_item, "status": "applying"})
    repo.heartbeat_apply = AsyncMock(return_value=True)
    repo.set_apply_target = AsyncMock(return_value=True)
    repo.finish_claimed_apply = AsyncMock(return_value=True)
    return repo


@pytest.mark.asyncio
async def test_queue_fleet_task_definition_refresh_is_atomic_and_skips_conflicts():
    rows = [
        {
            "owner_id": "owner-a",
            "service_name": "service-a",
            "access_point_id": "fsap-a",
            "task_definition_arn": "arn:a",
            "status": "running",
        },
        {
            "owner_id": "owner-b",
            "service_name": "service-b",
            "access_point_id": "fsap-b",
            "task_definition_arn": "arn:b",
            "status": "stopped",
        },
        {
            "owner_id": "owner-provisioning",
            "service_name": "service-provisioning",
            "access_point_id": "fsap-provisioning",
            "task_definition_arn": "arn:provisioning",
            "status": "provisioning",
        },
    ]
    rows.extend(
        [
            {
                "owner_id": "owner-error",
                "service_name": "service-error",
                "access_point_id": "fsap-error",
                "task_definition_arn": "arn:error",
                "status": "error",
            },
            {
                "owner_id": "owner-claimed-error",
                "service_name": "service-claimed-error",
                "access_point_id": "fsap-claimed-error",
                "task_definition_arn": "arn:claimed-error",
                "status": "error",
                "provision_claim_id": "live-provision",
            },
            {
                "owner_id": "owner-incomplete-error",
                "service_name": "service-incomplete-error",
                "status": "error",
            },
        ]
    )
    repo = MagicMock()
    repo.get_in_flight = AsyncMock(
        side_effect=[
            [],
            [{"changes": {"new_image": "image:already-rolling"}}],
            [],
        ]
    )
    repo.create = AsyncMock()

    with (
        patch.object(update_service, "container_repo") as container_repo,
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "utc_now_iso", return_value="2026-08-15T12:00:00+00:00"),
    ):
        container_repo.scan_all = AsyncMock(return_value=rows)
        queued, skipped = await update_service.queue_fleet_task_definition_refresh("Protocol rollout")

    assert (queued, skipped) == (2, 4)
    assert [call.kwargs["owner_id"] for call in repo.create.await_args_list] == ["owner-a", "owner-error"]
    for call in repo.create.await_args_list:
        assert call.kwargs == {
            "owner_id": call.kwargs["owner_id"],
            "update_type": update_service.TASK_DEFINITION_REFRESH_KIND,
            "description": "Protocol rollout",
            "changes": {update_service.TASK_DEFINITION_REFRESH_CHANGE: True},
            "scheduled_at": "2026-08-15T12:00:00+00:00",
        }


@pytest.mark.asyncio
async def test_apply_task_definition_refresh_waits_for_protocol_rollout():
    owner_id, update_id = "owner-refresh", "upd-refresh"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": update_service.TASK_DEFINITION_REFRESH_KIND,
        "changes": {update_service.TASK_DEFINITION_REFRESH_CHANGE: True},
    }
    repo = _claimed_repo(update_item)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock(return_value="arn:new")
    ecs.rollout_config_lock_protocol = AsyncMock(return_value="arn:new")
    ecs.resize_user_container = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    ecs.prepare_config_lock_protocol_task_definition.assert_awaited_once_with(owner_id)
    repo.set_apply_target.assert_awaited_once()
    ecs.rollout_config_lock_protocol.assert_awaited_once_with(
        owner_id,
        target_task_definition_arn="arn:new",
    )
    ecs.resize_user_container.assert_not_awaited()
    assert repo.finish_claimed_apply.await_args.kwargs["status"] == "applied"


def _refresh_item(*, target: str | None = None) -> dict:
    item = {
        "owner_id": "owner-refresh",
        "update_id": "upd-refresh",
        "update_type": update_service.TASK_DEFINITION_REFRESH_KIND,
        "changes": {update_service.TASK_DEFINITION_REFRESH_CHANGE: True},
        "status": "applying",
    }
    if target is not None:
        item["target_task_definition_arn"] = target
    return item


@pytest.mark.asyncio
async def test_refresh_crash_before_register_leaves_claim_for_stale_recovery():
    item = _refresh_item()
    repo = _claimed_repo(item)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock(side_effect=asyncio.CancelledError)
    ecs.rollout_config_lock_protocol = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        pytest.raises(asyncio.CancelledError),
    ):
        await update_service._apply_claimed_update(item, "claim-old")

    repo.set_apply_target.assert_not_awaited()
    repo.finish_claimed_apply.assert_not_awaited()
    ecs.rollout_config_lock_protocol.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_crash_after_register_before_target_persist_never_touches_service():
    item = _refresh_item()
    repo = _claimed_repo(item)
    repo.set_apply_target = AsyncMock(side_effect=asyncio.CancelledError)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock(return_value="arn:orphan")
    ecs.rollout_config_lock_protocol = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        pytest.raises(asyncio.CancelledError),
    ):
        await update_service._apply_claimed_update(item, "claim-old")

    ecs.rollout_config_lock_protocol.assert_not_awaited()
    repo.finish_claimed_apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_recovery_reuses_stored_target_after_service_update_interruption():
    item = _refresh_item()
    repo = _claimed_repo(item)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock(return_value="arn:target")
    ecs.rollout_config_lock_protocol = AsyncMock(side_effect=asyncio.CancelledError)

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        pytest.raises(asyncio.CancelledError),
    ):
        await update_service._apply_claimed_update(item, "claim-old")

    repo.set_apply_target.assert_awaited_once()

    reclaimed = _refresh_item(target="arn:target")
    repo.finish_claimed_apply = AsyncMock(return_value=True)
    ecs.prepare_config_lock_protocol_task_definition.reset_mock()
    ecs.rollout_config_lock_protocol = AsyncMock(return_value="arn:target")
    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        assert await update_service._apply_claimed_update(reclaimed, "claim-new") is True

    ecs.prepare_config_lock_protocol_task_definition.assert_not_awaited()
    ecs.rollout_config_lock_protocol.assert_awaited_once_with(
        "owner-refresh",
        target_task_definition_arn="arn:target",
    )


@pytest.mark.asyncio
async def test_refresh_recovery_after_stability_before_terminal_write_reuses_target():
    item = _refresh_item(target="arn:target")
    repo = _claimed_repo(item)
    repo.finish_claimed_apply = AsyncMock(side_effect=asyncio.CancelledError)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock()
    ecs.rollout_config_lock_protocol = AsyncMock(return_value="arn:target")

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        pytest.raises(asyncio.CancelledError),
    ):
        await update_service._apply_claimed_update(item, "claim-old")

    repo.finish_claimed_apply = AsyncMock(return_value=True)
    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        assert await update_service._apply_claimed_update(item, "claim-new") is True
    assert ecs.rollout_config_lock_protocol.await_count == 2
    assert all(
        call.kwargs["target_task_definition_arn"] == "arn:target"
        for call in ecs.rollout_config_lock_protocol.await_args_list
    )


@pytest.mark.asyncio
async def test_stale_refresh_worker_runs_only_the_winning_reclaim():
    stale = _refresh_item(target="arn:target")
    lost = {**stale, "owner_id": "owner-lost", "update_id": "upd-lost"}
    repo = MagicMock()
    repo.list_stale_applying = AsyncMock(return_value=[stale, lost])
    repo.claim_stale_apply = AsyncMock(side_effect=[stale, None])

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "_apply_claimed_update", new=AsyncMock()) as apply_claimed,
    ):
        await update_service._recover_stale_task_definition_refreshes()

    assert repo.claim_stale_apply.await_count == 2
    apply_claimed.assert_awaited_once()
    assert apply_claimed.await_args.args[0] == stale


@pytest.mark.asyncio
async def test_migrate_removed_config_keys_scrubs_defaults_and_entries():
    with patch.object(update_service, "strip_removed_agent_keys", new=AsyncMock()) as strip_mock:
        await update_service.migrate_removed_config_keys("owner_1")

    strip_mock.assert_awaited_once()
    _args, kwargs = strip_mock.await_args
    assert kwargs["defaults_keys"] == ("embeddedHarness",)
    # the AgentEntrySchema removals are scrubbed from agents.list[] entries too
    assert {
        "embeddedHarness",
        "embeddedPi",
        "agentRuntime",
        "systemPromptOverride",
    } <= set(kwargs["entry_keys"])


@pytest.mark.asyncio
async def test_apply_update_regenerates_config_before_image_roll():
    """An image roll must REGENERATE the on-disk config BEFORE resizing onto
    the new image.

    It used to only strip a blacklist of removed keys, which held while every
    roll was same-upstream (2026.7.2-isol8.N -> isol8.M, identical schema).
    Across an upstream bump the blacklist is structurally insufficient — it
    reaches only inside agents.*, and 7.2 -> 8.1 leaves 10 rejected keys at
    the ROOT. Both schema roots are additionalProperties:false, so the rolled
    gateway crash-loops at boot. The generator is version-aware, so a regen
    needs no blacklist to maintain."""
    owner_id, update_id = "owner_1", "upd_1"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "image_update",
        "changes": {"new_image": "ecr/openclaw-extended:2026.5.28-abc"},
    }

    repo = _claimed_repo(update_item)

    order: list[str] = []
    ecs = MagicMock()
    ecs._size_for_owner = AsyncMock(return_value=("512", "2048"))
    ecs.resize_user_container = AsyncMock(side_effect=lambda *a, **k: order.append("resize"))
    regen = AsyncMock(side_effect=lambda *a, **k: order.append("regen"))
    migrate = AsyncMock(side_effect=lambda *a, **k: order.append("migrate"))

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "reapply_owner_config", regen),
        patch.object(update_service, "migrate_removed_config_keys", migrate),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_workspace", return_value=MagicMock()),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    # VALUE-checked, not just "was awaited": deployed_tag defaults to None, and
    # None makes reapply_owner_config read the owner's CURRENT task-def. So a
    # dropped kwarg still compiles, still passes an await-only assertion, and
    # silently inverts every fork-level gate on the roll. The update_item's
    # new_image is :2026.5.28-abc, deliberately unlike any tag an owner is on.
    regen.assert_awaited_once_with(owner_id, deployed_tag="2026.5.28-abc")
    # The strip is now only the legacy-row fallback, not the normal path.
    migrate.assert_not_awaited()
    ecs.resize_user_container.assert_awaited_once()
    assert order == ["regen", "resize"]


@pytest.mark.asyncio
async def test_apply_update_carries_memory_wiki_bridge_keys_onto_existing_owners():
    """The roll must deep-fill memory-wiki's config keys, not just the entry.

    ``ensure_plugin_entries`` is additive at the ENTRY level, so it skips every
    owner who already has a memory-wiki entry — precisely the cohort that
    predates the bridge keys, which are what import memory-core's artifacts
    into the brain. A failure here is a safety-net miss, never a reason to
    strand an owner on an old image, so the roll still completes.
    """
    from core.containers.config import memory_plugin_entries

    owner_id, update_id = "owner_wiki", "upd_wiki"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "image_update",
        "changes": {"new_image": "ecr/openclaw-extended:2026.5.28-abc"},
    }

    ecs = MagicMock()
    ecs._size_for_owner = AsyncMock(return_value=("512", "2048"))
    ecs.resize_user_container = AsyncMock()
    # Raises: the same call proves both the wiring and that the roll survives it.
    fill = AsyncMock(side_effect=RuntimeError("EFS unavailable"))

    with (
        patch.object(update_service, "update_repo", _claimed_repo(update_item)),
        patch.object(update_service, "reapply_owner_config", AsyncMock()),
        patch.object(update_service, "migrate_removed_config_keys", AsyncMock()),
        patch("core.services.config_patcher.ensure_plugin_config_keys", fill),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_workspace", return_value=MagicMock()),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    # VALUE-checked: the bridge keys are the whole point, and a call that
    # passed the wrong subtree would still satisfy an await-only assertion.
    fill.assert_awaited_once_with(owner_id, "memory-wiki", memory_plugin_entries()["memory-wiki"]["config"])
    assert result is True
    ecs.resize_user_container.assert_awaited_once()


async def test_apply_update_falls_back_to_strip_for_legacy_rows():
    """A legacy row with no gateway_token cannot be regenerated —
    reapply_owner_config is the same code the admin Regenerate button 409s
    them from. The roll must fall back to the historical strip rather than
    fail, so a SAME-upstream roll keeps working for them exactly as before."""
    owner_id, update_id = "owner_legacy", "upd_1"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "image_update",
        "changes": {"new_image": "ecr/openclaw-extended:2026.5.28-abc"},
    }

    repo = _claimed_repo(update_item)

    order: list[str] = []
    ecs = MagicMock()
    ecs._size_for_owner = AsyncMock(return_value=("512", "2048"))
    ecs.resize_user_container = AsyncMock(side_effect=lambda *a, **k: order.append("resize"))
    regen = AsyncMock(side_effect=ValueError("owner has no gateway_token"))
    migrate = AsyncMock(side_effect=lambda *a, **k: order.append("migrate"))

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "reapply_owner_config", regen),
        patch.object(update_service, "migrate_removed_config_keys", migrate),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_workspace", return_value=MagicMock()),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    migrate.assert_awaited_once_with(owner_id)
    # The roll still happened — a legacy owner is not stranded on an old image.
    ecs.resize_user_container.assert_awaited_once()
    assert order == ["migrate", "resize"]


async def _apply_image_only_update(owner_id, update_id, size):
    """Drive apply_update for an image-ONLY change (no cpu/memory) with a
    _size_for_owner stub returning ``size``; return the resize call kwargs."""
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "image_update",
        "changes": {"new_image": "ecr/openclaw-extended:2026.6.1-abc"},
    }
    repo = _claimed_repo(update_item)

    ecs = MagicMock()
    ecs._size_for_owner = AsyncMock(return_value=size)
    ecs.resize_user_container = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "reapply_owner_config", AsyncMock()),
        patch.object(update_service, "migrate_removed_config_keys", AsyncMock()),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_workspace", return_value=MagicMock()),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    ecs._size_for_owner.assert_awaited_once_with(owner_id)
    ecs.resize_user_container.assert_awaited_once()
    return ecs.resize_user_container.await_args.kwargs


@pytest.mark.asyncio
async def test_apply_image_only_update_preserves_ultra_size():
    """An image-only roll for an ultra_pro owner must re-register the Ultra box,
    not the standard one — otherwise every routine image bump / fleet-force-apply
    silently shrinks the container back to 1 vCPU / 4 GB."""
    kwargs = await _apply_image_only_update("owner_ultra", "upd_ultra", ("2048", "8192"))
    assert kwargs["new_cpu"] == "2048"
    assert kwargs["new_memory"] == "8192"
    assert kwargs["new_image"] == "ecr/openclaw-extended:2026.6.1-abc"


@pytest.mark.asyncio
async def test_apply_image_only_update_keeps_standard_size():
    """An image-only roll for a standard owner stays on the standard box."""
    kwargs = await _apply_image_only_update("owner_std", "upd_std", ("512", "2048"))
    assert kwargs["new_cpu"] == "512"
    assert kwargs["new_memory"] == "2048"


@pytest.mark.asyncio
async def test_apply_update_skips_migration_for_resize_only_update():
    """A cpu/memory-only resize (no new_image) keeps the same image, so
    there's no schema change to migrate for — the migration must not run."""
    owner_id, update_id = "owner_2", "upd_2"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "resize",
        "changes": {"new_cpu": 1024, "new_memory": 2048},
    }

    repo = _claimed_repo(update_item)

    ecs = MagicMock()
    ecs.resize_user_container = AsyncMock()
    migrate = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "migrate_removed_config_keys", migrate),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    migrate.assert_not_awaited()
    ecs.resize_user_container.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_update_returns_false_when_claim_is_not_acquired():
    owner_id, update_id = "owner_x", "upd_x"

    repo = MagicMock()
    repo.claim_apply = AsyncMock(return_value=None)

    with (
        patch.object(update_service, "update_repo", repo),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is False
    repo.claim_apply.assert_awaited_once()


# ---------------------------------------------------------------------------
# Row #14 — a Hermes image bump is not an ECS task-definition change.
# queue_fleet_image_update skips AgentCore owners at enqueue time;
# _apply_claimed_update refuses defensively for any ECS-shaped change that
# reaches it anyway (e.g. a single-owner admin POST bypassing the fleet filter).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_fleet_image_update_skips_agentcore_owners():
    scan_result = {
        "Items": [{"owner_id": "owner_ecs"}, {"owner_id": "owner_agentcore"}],
    }

    async def fake_is_agentcore(owner_id):
        return owner_id == "owner_agentcore"

    with (
        patch("core.dynamodb.get_table"),
        patch("core.dynamodb.run_in_thread", new=AsyncMock(return_value=scan_result)),
        patch.object(update_service.runtime_transport, "is_agentcore_owner", new=fake_is_agentcore),
        patch.object(update_service, "queue_image_update", new=AsyncMock()) as mock_queue,
    ):
        queued, skipped = await update_service.queue_fleet_image_update("ecr/openclaw-extended:new")

    assert queued == 1
    assert skipped == 1
    mock_queue.assert_awaited_once_with("owner_ecs", "ecr/openclaw-extended:new", None)


@pytest.mark.asyncio
async def test_apply_claimed_update_refuses_image_roll_for_agentcore_owner():
    owner_id, update_id = "owner_agentcore", "upd_agentcore"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "image_update",
        "changes": {"new_image": "ecr/openclaw-extended:2026.9.1-abc"},
    }
    repo = _claimed_repo(update_item)
    ecs = MagicMock()
    ecs.resize_user_container = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service.runtime_transport, "is_agentcore_owner", new=AsyncMock(return_value=True)),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is False
    ecs.resize_user_container.assert_not_awaited()
    repo.finish_claimed_apply.assert_awaited_once()
    assert repo.finish_claimed_apply.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_apply_claimed_update_config_patch_only_is_not_refused_for_agentcore_owner():
    """A config_patch-only change (not this row's ECS-shaped set) must not be
    refused just because the owner happens to be AgentCore -- it applies via the
    S3-profile fan-out instead (fix round 1, item 3), not the ECS-shaped refusal."""
    owner_id, update_id = "owner_agentcore", "upd_cfg"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "config_patch",
        "changes": {"config_patch": {"tools": {"profile": "full"}}},
    }
    repo = _claimed_repo(update_item)
    ecs_patcher = AsyncMock()
    roster_patcher = AsyncMock(return_value={"config": {}, "hash": "h1"})

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service.runtime_transport, "is_agentcore_owner", new=AsyncMock(return_value=True)),
        patch.object(update_service, "patch_openclaw_config", ecs_patcher),
        patch.object(update_service, "config_patch_roster", roster_patcher),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    roster_patcher.assert_awaited_once_with(owner_id, {"tools": {"profile": "full"}}, None)
    ecs_patcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_claimed_update_config_patch_only_ecs_owner_unchanged():
    """The ECS branch stays byte-identical: patch_openclaw_config, not the roster fan-out."""
    owner_id, update_id = "owner_ecs", "upd_cfg_ecs"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "config_patch",
        "changes": {"config_patch": {"tools": {"profile": "full"}}},
    }
    repo = _claimed_repo(update_item)
    ecs_patcher = AsyncMock()
    roster_patcher = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service.runtime_transport, "is_agentcore_owner", new=AsyncMock(return_value=False)),
        patch.object(update_service, "patch_openclaw_config", ecs_patcher),
        patch.object(update_service, "config_patch_roster", roster_patcher),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    ecs_patcher.assert_awaited_once_with(owner_id, {"tools": {"profile": "full"}})
    roster_patcher.assert_not_awaited()
