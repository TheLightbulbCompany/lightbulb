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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.services.desktop_pair import (
    DESKTOP_CLI_BROWSER_COMMANDS,
    DESKTOP_CLI_COMMANDS,
    DESKTOP_CLI_FILE_COMMANDS,
    DESKTOP_CLI_SHELL_COMMANDS,
    DESKTOP_MAC_COMMANDS,
    DesktopPermissionGrant,
    PairResult,
    assert_command_sets_disjoint,
    build_paired_entry,
    derive_device_id,
    pair_desktop_devices,
    revoke_desktop_device,
)


def _pem(seed: bytes) -> str:
    return (
        Ed25519PrivateKey.from_private_bytes(seed * 32)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


_MAC_PEM = _pem(b"\x11")
_CLI_PEM = _pem(b"\x12")


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


# ---------------------------------------------------------------------------
# Device identity
# ---------------------------------------------------------------------------


def test_build_paired_entry_derives_device_id_from_pubkey():
    entry = build_paired_entry(
        pubkey_pem=_MAC_PEM,
        client_id="openclaw-macos",
        display_name="Alice | Lightbulb Mac",
        commands=list(DESKTOP_MAC_COMMANDS),
        user_id="user_alice",
    )
    assert entry["deviceId"] == derive_device_id(_MAC_PEM)


def test_build_paired_entry_rejects_unknown_client_id():
    with pytest.raises(ValueError):
        build_paired_entry(
            pubkey_pem=_MAC_PEM,
            client_id="not-a-client",
            display_name="x",
            commands=[],
            user_id="user_alice",
        )


def test_build_paired_entry_public_key_is_base64url_raw():
    """``machine_auth`` verifies a device's WS-upgrade signature against the RAW 32-byte
    Ed25519 key, base64url-encoded — not the PEM/SPKI wrapper it arrives in."""
    entry = build_paired_entry(
        pubkey_pem=_MAC_PEM,
        client_id="openclaw-macos",
        display_name="x",
        commands=[],
        user_id="user_alice",
    )
    import base64

    raw = base64.urlsafe_b64decode(entry["publicKey"] + "=" * (-len(entry["publicKey"]) % 4))
    assert len(raw) == 32
    assert "=" not in entry["publicKey"]


# ---------------------------------------------------------------------------
# pair / revoke against the row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_writes_both_devices_onto_the_owner_row():
    upsert = AsyncMock()
    stored = {}

    async def _upsert(_owner, entries):
        stored.update(entries)

    upsert.side_effect = _upsert
    with (
        patch("core.services.desktop_pair.container_repo.upsert_paired_devices", upsert),
        patch(
            "core.services.desktop_pair.container_repo.get_by_owner_id",
            AsyncMock(side_effect=lambda *_args, **_kwargs: {"gateway_token": "gw-token", "paired_devices": stored}),
        ),
    ):
        result: PairResult = await pair_desktop_devices(
            user_id="user_alice",
            owner_id="org_42",
            display_name_base="Alice Smith",
            tauri_mac_pubkey=_MAC_PEM,
            cli_pubkey=_CLI_PEM,
        )

    assert result.device_id_mac != result.device_id_cli
    assert result.commands_mac == tuple(
        command
        for command in DESKTOP_MAC_COMMANDS
        if command not in {"system.run.approve.v1", "desktop.operation.approve.v1"}
    )
    assert result.commands_cli == DESKTOP_CLI_COMMANDS
    assert result.gateway_token == "gw-token"

    # ONE call carrying both devices, keyed by deviceId — flat, no wrapper, because that map
    # is what the authorizer point-looks-up.
    upsert.assert_awaited_once()
    owner_arg, entries = upsert.await_args.args
    assert owner_arg == "org_42"
    assert set(entries) == {result.device_id_mac, result.device_id_cli}
    for device_id, entry in entries.items():
        assert entry["userId"] == "user_alice"
        assert entry["publicKey"] and "=" not in entry["publicKey"]
        assert entry["commands"]
    assert entries[result.device_id_mac]["clientId"] == "openclaw-macos"
    assert entries[result.device_id_cli]["clientId"] == "node-host"
    assert entries[result.device_id_mac]["pairingGroupId"] == entries[result.device_id_cli]["pairingGroupId"]
    assert entries[result.device_id_mac]["displayName"] == "Alice Smith | Lightbulb Mac"
    assert entries[result.device_id_cli]["displayName"] == "Alice Smith | Lightbulb Desktop (CLI)"


@pytest.mark.asyncio
async def test_pair_honors_an_explicit_permission_grant():
    upsert = AsyncMock()
    stored = {}

    async def _upsert(_owner, entries):
        stored.update(entries)

    upsert.side_effect = _upsert
    grant = DesktopPermissionGrant()
    with (
        patch("core.services.desktop_pair.container_repo.upsert_paired_devices", upsert),
        patch(
            "core.services.desktop_pair.container_repo.get_by_owner_id",
            AsyncMock(side_effect=lambda *_args, **_kwargs: {"paired_devices": stored}),
        ),
    ):
        result = await pair_desktop_devices(
            user_id="user_alice",
            owner_id="org_42",
            display_name_base="Alice",
            tauri_mac_pubkey=_MAC_PEM,
            cli_pubkey=_CLI_PEM,
            permissions=grant,
        )

    assert result.commands_cli == grant.cli_commands()
    _owner, entries = upsert.await_args.args
    assert entries[result.device_id_cli]["commands"] == list(grant.cli_commands())


@pytest.mark.asyncio
async def test_pair_fails_closed_when_the_two_scoped_writes_land_partially():
    """The repository writes each device conditionally, not as one transaction.

    A cross-member collision can therefore leave only one new entry.  Pairing must not
    report that incomplete physical Mac as execution-capable.
    """
    stored = {}

    async def _partial_upsert(_owner, entries):
        device_id, entry = next(iter(entries.items()))
        stored[device_id] = entry

    with (
        patch("core.services.desktop_pair.container_repo.upsert_paired_devices", _partial_upsert),
        patch(
            "core.services.desktop_pair.container_repo.get_by_owner_id",
            AsyncMock(side_effect=lambda *_args, **_kwargs: {"paired_devices": stored}),
        ),
    ):
        with pytest.raises(ValueError, match="pair again"):
            await pair_desktop_devices(
                user_id="user_alice",
                owner_id="org_42",
                display_name_base="Alice",
                tauri_mac_pubkey=_MAC_PEM,
                cli_pubkey=_CLI_PEM,
            )


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
