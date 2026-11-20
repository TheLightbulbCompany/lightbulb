"""Desktop device pairing: the command grants, the device-id derivation, and the row.

WHAT WENT WITH THE ECS RUNTIME. Pairing used to maintain two EFS files per owner
(``devices/paired.json`` and ``nodes/paired.json``) under an fcntl lock, through a
``WorkspaceLike`` adapter, with a whole preserve/prune/relabel state machine over their
contents — plus a projection of the pubkeys onto the containers row for the WS authorizer.
The gateway that read those files is gone, so only the projection is left: ``paired_devices``
on the owner's row IS the trust record now, and ``container_repo.upsert_paired_devices``'
ConditionExpression (covered in that module's tests) is the non-relabel guard the paired.json
state machine used to duplicate.

So the ~35 tests over that state machine and the ``EfsWorkspaceAdapter``'s real-filesystem
locking are deleted. What stays is what was never about EFS.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.desktop_pair import (
    BROWSER_GRANT_MARKER,
    DESKTOP_CLI_BROWSER_COMMANDS,
    DESKTOP_CLI_COMMANDS,
    DESKTOP_CLI_FILE_COMMANDS,
    DESKTOP_CLI_SHELL_COMMANDS,
    DESKTOP_MAC_COMMANDS,
    DesktopPermissionGrant,
    assert_command_sets_disjoint,
    declared_commands_for,
    granted_commands,
    revoke_desktop_device,
)


# ---------------------------------------------------------------------------
# Command grants
# ---------------------------------------------------------------------------


def test_mac_and_cli_command_sets_disjoint():
    assert_command_sets_disjoint(DESKTOP_MAC_COMMANDS, DESKTOP_CLI_COMMANDS)


def test_assert_disjoint_raises_on_overlap():
    with pytest.raises(AssertionError):
        assert_command_sets_disjoint(("a", "b"), ("b", "c"))


def test_cli_commands_are_exactly_the_three_declared_groups():
    """The CLI's declared surface is composed from the three groups the sidecar declares
    unconditionally; a command in neither the union nor the mac set is one nothing grants."""
    assert set(DESKTOP_CLI_COMMANDS) == set(DESKTOP_CLI_BROWSER_COMMANDS) | set(DESKTOP_CLI_FILE_COMMANDS) | set(
        DESKTOP_CLI_SHELL_COMMANDS
    )


def test_a_basic_grant_withholds_the_remote_command_groups():
    basic = DesktopPermissionGrant()
    assert set(basic.cli_commands()) < set(DESKTOP_CLI_COMMANDS)
    assert set(DesktopPermissionGrant.full().cli_commands()) == set(DESKTOP_CLI_COMMANDS)


def _pair(browser: bool, *, mac_commands=("device.info",)) -> dict:
    """A pair stored before the browser commands existed: the Mac entry has none of them."""
    return {
        "mac-1": {"clientId": "openclaw-macos", "pairingGroupId": "g1", "userId": "u1", "commands": list(mac_commands)},
        "cli-1": {
            "clientId": "node-host",
            "pairingGroupId": "g1",
            "userId": "u1",
            "commands": ["system.run", *(["browser.proxy"] if browser else [])],
        },
    }


def test_the_browser_grant_adds_no_mac_command_and_only_the_cli_marker():
    grant = DesktopPermissionGrant(browser=True)
    assert set(grant.mac_commands()) == set(DesktopPermissionGrant().mac_commands())
    assert "browser.wipe.v1" in grant.mac_commands()
    assert DESKTOP_CLI_BROWSER_COMMANDS == (BROWSER_GRANT_MARKER,)
    assert "browser" not in grant.cli_caps()


def test_wipe_is_derived_for_every_native_entry():
    for browser in (True, False):
        devices = _pair(browser=browser)
        assert "browser.wipe.v1" in granted_commands(devices, devices["mac-1"])
        assert "browser.wipe.v1" not in granted_commands(devices, devices["cli-1"])
    groupless = {"clientId": "openclaw-macos", "userId": "u1", "commands": ["device.info"]}
    assert granted_commands({"mac-1": groupless}, groupless) == {"device.info"}


async def test_declared_commands_for_includes_derived_commands():
    row = {"paired_devices": _pair(browser=True)}
    with patch("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=row)):
        declared = await declared_commands_for("org_1", "mac-1")
    assert declared == {"device.info", "browser.wipe.v1"}


# ---------------------------------------------------------------------------
# Device identity
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# revoke against the row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_removes_the_device_and_force_closes_its_socket():
    """Removing the trust record alone only blocks future reconnects — the security-review
    HIGH fix is that a live session is closed too (stolen-Mac scenario)."""
    remove = AsyncMock()
    force_close = AsyncMock(return_value=True)
    with (
        patch(
            "core.services.desktop_pair.container_repo.get_by_owner_id",
            AsyncMock(return_value={"paired_devices": {"dev_abc": {"publicKey": "k"}}}),
        ),
        patch("core.services.desktop_pair.container_repo.remove_paired_device", remove),
    ):
        removed = await revoke_desktop_device(device_id="dev_abc", owner_id="org_42", force_close_device=force_close)

    assert removed is True
    remove.assert_awaited_once_with("org_42", "dev_abc")
    force_close.assert_awaited_once_with("dev_abc")


@pytest.mark.asyncio
async def test_revoke_an_unpaired_device_still_force_closes():
    """Defends against a connected device whose record was wiped out-of-band."""
    force_close = AsyncMock(return_value=False)
    with (
        patch("core.services.desktop_pair.container_repo.get_by_owner_id", AsyncMock(return_value={})),
        patch("core.services.desktop_pair.container_repo.remove_paired_device", AsyncMock()),
    ):
        removed = await revoke_desktop_device(device_id="dev_none", owner_id="org_42", force_close_device=force_close)

    assert removed is False
    force_close.assert_awaited_once_with("dev_none")
