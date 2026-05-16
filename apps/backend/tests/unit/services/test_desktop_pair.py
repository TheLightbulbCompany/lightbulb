from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.services.desktop_pair import (
    DESKTOP_CLI_COMMANDS,
    DESKTOP_MAC_COMMANDS,
    PairResult,
    assert_command_sets_disjoint,
    build_paired_entry,
    derive_device_id,
    pair_desktop_devices,
)


def test_mac_and_cli_command_sets_disjoint():
    overlap = set(DESKTOP_MAC_COMMANDS) & set(DESKTOP_CLI_COMMANDS)
    assert overlap == set(), f"Mac/CLI command overlap: {overlap}"


def test_mac_commands_include_native_only():
    assert "system.notify" in DESKTOP_MAC_COMMANDS
    assert "device.info" in DESKTOP_MAC_COMMANDS
    assert "screen.snapshot" in DESKTOP_MAC_COMMANDS
    assert "system.run" not in DESKTOP_MAC_COMMANDS
    assert "browser.proxy" not in DESKTOP_MAC_COMMANDS


def test_cli_commands_include_cross_platform_only():
    assert "system.run" in DESKTOP_CLI_COMMANDS
    assert "browser.proxy" in DESKTOP_CLI_COMMANDS
    assert "file.fetch" in DESKTOP_CLI_COMMANDS
    assert "system.notify" not in DESKTOP_CLI_COMMANDS


def test_assert_disjoint_raises_on_overlap():
    with pytest.raises(AssertionError):
        assert_command_sets_disjoint(["a", "b"], ["b", "c"])


def test_build_paired_entry_derives_device_id_from_pubkey():
    pubkey_pem = "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAvFlqQ1klsx1pYz3MNvgQzJ6h0aV4mLLPi/Yzo2VzZ1U=\n-----END PUBLIC KEY-----\n"
    entry = build_paired_entry(
        pubkey_pem=pubkey_pem,
        client_id="openclaw-macos",
        display_name="Alice | Isol8 Mac",
        commands=list(DESKTOP_MAC_COMMANDS),
    )
    assert entry["role"] == "node"
    assert entry["clientId"] == "openclaw-macos"
    assert entry["displayName"] == "Alice | Isol8 Mac"
    assert entry["commands"] == list(DESKTOP_MAC_COMMANDS)
    assert len(entry["deviceId"]) == 64
    assert all(c in "0123456789abcdef" for c in entry["deviceId"])
    assert "tokens" in entry
    assert "node" in entry["tokens"]
    assert entry["tokens"]["node"]["token"]
    assert entry["createdAtMs"] > 0
    assert entry["approvedAtMs"] > 0


def test_build_paired_entry_rejects_unknown_client_id():
    with pytest.raises(ValueError, match="clientId must be"):
        build_paired_entry(
            pubkey_pem="-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAO2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoYtZ2ik=\n-----END PUBLIC KEY-----\n",
            client_id="not-allowed",
            display_name="X",
            commands=[],
        )


# Fixture: deterministic Ed25519 public key (from all-zero private bytes).
# Expected deviceId computed once and pinned — guards against silent drift
# in derive_device_id from upstream's deriveDeviceIdFromPublicKey.
_FIXTURE_PEM = """\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAO2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoYtZ2ik=
-----END PUBLIC KEY-----
"""
_FIXTURE_EXPECTED_DEVICE_ID = "139e3940e64b5491722088d9a0d741628fc826e09475d341a780acde3c4b8070"


def test_derive_device_id_matches_upstream_spki_strip():
    assert derive_device_id(_FIXTURE_PEM) == _FIXTURE_EXPECTED_DEVICE_ID


async def test_pair_desktop_devices_writes_two_paired_entries():
    mock_workspace = AsyncMock()
    mock_workspace.read_paired_json = AsyncMock(return_value={"pairedByDeviceId": {}})
    mock_workspace.write_paired_json = AsyncMock()
    mock_workspace.get_gateway_token = AsyncMock(return_value="container-gw-token")
    mock_workspace.gateway_url = "wss://ws-dev.isol8.co"

    # Use the same fixture PEM from earlier tests; for the cli side use a
    # different pubkey to get a different deviceId.
    tauri_mac_pubkey = _FIXTURE_PEM
    # Different key for cli: generate from another deterministic seed
    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    cli_pubkey = cli_priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    result: PairResult = await pair_desktop_devices(
        user_id="user_alice",
        owner_id="owner_org_42",
        display_name_base="Alice Smith",
        tauri_mac_pubkey=tauri_mac_pubkey,
        cli_pubkey=cli_pubkey,
        workspace=mock_workspace,
    )

    assert result.gateway_url == "wss://ws-dev.isol8.co"
    assert result.gateway_token == "container-gw-token"
    assert result.device_id_mac != result.device_id_cli
    assert result.commands_mac == DESKTOP_MAC_COMMANDS
    assert result.commands_cli == DESKTOP_CLI_COMMANDS

    mock_workspace.write_paired_json.assert_awaited_once()
    written = mock_workspace.write_paired_json.await_args.args[0]
    assert len(written["pairedByDeviceId"]) == 2

    mac_entry = written["pairedByDeviceId"][result.device_id_mac]
    assert mac_entry["clientId"] == "openclaw-macos"
    assert mac_entry["displayName"] == "Alice Smith | Isol8 Mac"
    assert mac_entry["commands"] == list(DESKTOP_MAC_COMMANDS)
    assert mac_entry["tokens"]["node"]["token"]

    cli_entry = written["pairedByDeviceId"][result.device_id_cli]
    assert cli_entry["clientId"] == "node-host"
    assert cli_entry["displayName"] == "Alice Smith | Isol8 Desktop (CLI)"
    assert cli_entry["commands"] == list(DESKTOP_CLI_COMMANDS)


async def test_pair_desktop_devices_idempotent_on_existing_entries():
    """Calling pair twice with same pubkeys must not duplicate or rewrite."""
    tauri_priv = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
    tauri_pub = tauri_priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32)
    cli_pub = cli_priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    # In-memory state shared across two calls
    state = {"pairedByDeviceId": {}}

    mock_workspace = AsyncMock()
    mock_workspace.read_paired_json = AsyncMock(side_effect=lambda: state)

    async def _write(d):
        state.update(d)

    mock_workspace.write_paired_json = AsyncMock(side_effect=_write)
    mock_workspace.get_gateway_token = AsyncMock(return_value="t")
    mock_workspace.gateway_url = "wss://ws-dev.isol8.co"

    first = await pair_desktop_devices(
        user_id="u",
        owner_id="o",
        display_name_base="Alice",
        tauri_mac_pubkey=tauri_pub,
        cli_pubkey=cli_pub,
        workspace=mock_workspace,
    )
    second = await pair_desktop_devices(
        user_id="u",
        owner_id="o",
        display_name_base="Alice",
        tauri_mac_pubkey=tauri_pub,
        cli_pubkey=cli_pub,
        workspace=mock_workspace,
    )

    assert first.device_id_mac == second.device_id_mac
    assert first.device_id_cli == second.device_id_cli
    # Write should only be called ONCE (first call); second is idempotent no-op
    assert mock_workspace.write_paired_json.await_count == 1
