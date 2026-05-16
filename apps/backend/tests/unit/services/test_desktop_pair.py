from core.services.desktop_pair import (
    DESKTOP_MAC_COMMANDS,
    DESKTOP_CLI_COMMANDS,
    assert_command_sets_disjoint,
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
    import pytest
    with pytest.raises(AssertionError):
        assert_command_sets_disjoint(["a", "b"], ["b", "c"])


import pytest
from core.services.desktop_pair import build_paired_entry


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
    # deviceId is SHA-256 of the raw key bytes; for this test just check it's hex of length 64
    assert len(entry["deviceId"]) == 64
    assert all(c in "0123456789abcdef" for c in entry["deviceId"])
    # tokens.node load-bearing — see spec
    assert "tokens" in entry
    assert "node" in entry["tokens"]
    assert entry["tokens"]["node"]["token"]
    assert entry["createdAtMs"] > 0
    assert entry["approvedAtMs"] > 0


def test_build_paired_entry_rejects_unknown_client_id():
    with pytest.raises(ValueError, match="clientId must be"):
        build_paired_entry(
            pubkey_pem="-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----\n",
            client_id="not-allowed",
            display_name="X",
            commands=[],
        )
