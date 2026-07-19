"""Tests for the image-update apply path + the pre-roll config migration.

A container rolled to a newer image reuses its existing on-disk
openclaw.json. ``migrate_removed_config_keys`` strips keys the new image's
strict schema no longer accepts (e.g. ``agents.defaults.embeddedHarness``,
removed in 2026.5.28) before the roll, so the gateway doesn't crash-loop on
boot. These tests pin that the migration runs, targets the right path, and
runs BEFORE the ECS image roll.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import update_service


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
async def test_apply_update_migrates_removed_keys_before_image_roll():
    """An image roll must strip removed-upstream keys from the existing
    on-disk config BEFORE resizing onto the new image — otherwise the
    rolled container boots against a stale key the strict schema rejects
    and crash-loops."""
    owner_id, update_id = "owner_1", "upd_1"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "image_update",
        "changes": {"new_image": "ecr/openclaw-extended:2026.5.28-abc"},
    }

    repo = MagicMock()
    repo.set_status_conditional = AsyncMock(return_value=True)
    repo.mark_applied = AsyncMock()

    order: list[str] = []
    ecs = MagicMock()
    ecs._size_for_owner = AsyncMock(return_value=("512", "2048"))
    ecs.resize_user_container = AsyncMock(side_effect=lambda *a, **k: order.append("resize"))
    migrate = AsyncMock(side_effect=lambda *a, **k: order.append("migrate"))

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "migrate_removed_config_keys", migrate),
        patch("core.dynamodb.get_table", return_value=MagicMock()),
        patch("core.dynamodb.run_in_thread", new=AsyncMock(return_value={"Items": [update_item]})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    migrate.assert_awaited_once_with(owner_id)
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
    repo = MagicMock()
    repo.set_status_conditional = AsyncMock(return_value=True)
    repo.mark_applied = AsyncMock()

    ecs = MagicMock()
    ecs._size_for_owner = AsyncMock(return_value=size)
    ecs.resize_user_container = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "migrate_removed_config_keys", AsyncMock()),
        patch("core.dynamodb.get_table", return_value=MagicMock()),
        patch("core.dynamodb.run_in_thread", new=AsyncMock(return_value={"Items": [update_item]})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
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
    silently shrinks the container back to 0.5 vCPU / 2 GB."""
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

    repo = MagicMock()
    repo.set_status_conditional = AsyncMock(return_value=True)
    repo.mark_applied = AsyncMock()

    ecs = MagicMock()
    ecs.resize_user_container = AsyncMock()
    migrate = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "migrate_removed_config_keys", migrate),
        patch("core.dynamodb.get_table", return_value=MagicMock()),
        patch("core.dynamodb.run_in_thread", new=AsyncMock(return_value={"Items": [update_item]})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    migrate.assert_not_awaited()
    ecs.resize_user_container.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_update_clears_stuck_row_when_refetch_empty():
    """If the post-acquire re-fetch returns no row (should not happen with the
    strongly-consistent read, but defensively), the row was already flipped to
    'applying' — clear it to 'failed' so it can't wedge the owner's in-flight
    guard and block future rolls."""
    owner_id, update_id = "owner_x", "upd_x"

    repo = MagicMock()
    # acquire -> True; the follow-up cleanup write also returns True
    repo.set_status_conditional = AsyncMock(return_value=True)

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.dynamodb.get_table", return_value=MagicMock()),
        patch("core.dynamodb.run_in_thread", new=AsyncMock(return_value={"Items": []})),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is False
    # Two writes: acquire (-> "applying") then the stuck-row cleanup (-> "failed").
    assert repo.set_status_conditional.await_count == 2
    cleanup = repo.set_status_conditional.await_args
    assert cleanup.args[2] == "failed"
    assert cleanup.args[3] == ["applying"]
