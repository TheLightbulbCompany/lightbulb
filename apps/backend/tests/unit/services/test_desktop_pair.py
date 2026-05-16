import pytest

from core.services.desktop_pair import (
    DESKTOP_CLI_COMMANDS,
    DESKTOP_MAC_COMMANDS,
    assert_command_sets_disjoint,
    build_paired_entry,
    derive_device_id,
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
