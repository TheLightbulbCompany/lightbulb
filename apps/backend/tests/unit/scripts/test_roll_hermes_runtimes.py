"""--max-concurrent-turns: stores the owner's turn cap on its row, then rolls it."""

from unittest.mock import AsyncMock

import pytest

from scripts import roll_hermes_runtimes as script


@pytest.fixture
def wired(monkeypatch):
    update = AsyncMock()
    monkeypatch.setattr(script.container_repo, "update_fields", update)
    monkeypatch.setattr(script, "prepare_hermes_workspace", AsyncMock(return_value=False))
    monkeypatch.setattr(script, "roll_runtime_image", AsyncMock(return_value="updated"))
    monkeypatch.setattr(script, "sync_paperclip_adapter_config", AsyncMock())
    return update


@pytest.mark.asyncio
@pytest.mark.parametrize("value, stored", [("6", 6), ("0", None)])
async def test_the_cap_is_stored_then_rolled(wired, value, stored):
    assert await script.main(["--owner-id", "org_a", "--max-concurrent-turns", value]) == 0
    wired.assert_awaited_once_with("org_a", {"max_concurrent_turns": stored})
    script.roll_runtime_image.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value, stored", [("on", True), ("off", False)])
async def test_the_network_workers_flag_is_stored_then_rolled(wired, value, stored):
    assert await script.main(["--owner-id", "org_a", "--network-workers", value]) == 0
    wired.assert_awaited_once_with("org_a", {"network_workers": stored})
    script.roll_runtime_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_network_workers_flag_is_refused_for_the_whole_fleet(wired):
    with pytest.raises(SystemExit):
        await script.main(["--all", "--network-workers", "on"])
    wired.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cap_change_that_meets_a_concurrent_roll_exits_1(wired, monkeypatch):
    monkeypatch.setattr(script, "roll_runtime_image", AsyncMock(return_value="update-in-progress"))
    assert await script.main(["--owner-id", "org_a", "--max-concurrent-turns", "6"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ["--owner-id", "org_a", "--max-concurrent-turns", "1"],
        ["--owner-id", "org_a", "--max-concurrent-turns", "6", "--dry-run"],
        ["--all", "--max-concurrent-turns", "6"],
    ],
)
async def test_a_cap_of_one_a_dry_run_or_the_whole_fleet_is_refused(wired, argv):
    with pytest.raises(SystemExit):
        await script.main(argv)
    wired.assert_not_awaited()
