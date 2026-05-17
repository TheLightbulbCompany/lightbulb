import base64
import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.containers.workspace import Workspace
from core.services.desktop_pair import (
    DESKTOP_CLI_COMMANDS,
    DESKTOP_MAC_COMMANDS,
    EfsWorkspaceAdapter,
    PairResult,
    assert_command_sets_disjoint,
    build_paired_entry,
    derive_device_id,
    pair_desktop_devices,
    revoke_desktop_device,
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


def test_build_paired_entry_public_key_is_base64url_raw():
    """REGRESSION: paired.json's ``publicKey`` field must be the
    base64url-no-pad raw 32 bytes — upstream's gateway does strict string
    equality of paired.json against the wire-format pubkey in the signed
    connect frame (message-handler.ts:1151-1153
    ``paired?.publicKey === devicePublicKey``). Writing PEM here always
    mismatches and the gateway treats every paired device as NOT_PAIRED.
    """
    # Generate a fresh keypair so the test is self-contained.
    priv = Ed25519PrivateKey.from_private_bytes(b"\x07" * 32)
    pubkey_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    entry = build_paired_entry(
        pubkey_pem=pubkey_pem,
        client_id="openclaw-macos",
        display_name="X",
        commands=[],
    )

    pubkey = entry["publicKey"]
    # 32 bytes encoded as base64url-no-pad is exactly 43 chars.
    assert len(pubkey) == 43, f"expected 43 chars, got {len(pubkey)}: {pubkey!r}"
    # base64url alphabet only — no '+' / '/' / '=' / newlines.
    assert re.fullmatch(r"[A-Za-z0-9_-]+", pubkey), f"non-base64url chars: {pubkey!r}"
    # Decodes to exactly 32 raw bytes (Ed25519 public-key length).
    assert "-----BEGIN" not in pubkey
    raw = base64.urlsafe_b64decode(pubkey + "==")
    assert len(raw) == 32

    # And the bytes match what the cryptography lib produces directly.
    expected_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    assert raw == expected_raw


def test_build_paired_entry_canonical_fields():
    """Pin the canonical PairedDevice shape we write — clientMode, roles,
    scopes, approvedScopes at the top level plus role/scopes inside
    tokens.node. This mirrors what ``_build_node_paired_entry`` in
    core/containers/config.py already writes for the in-container node
    entry; downstream role / scope resolution (listEffectivePairedDeviceRoles,
    listActiveTokenRoles) depends on these fields being present.
    """
    priv = Ed25519PrivateKey.from_private_bytes(b"\x08" * 32)
    pubkey_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    entry = build_paired_entry(
        pubkey_pem=pubkey_pem,
        client_id="node-host",
        display_name="X | CLI",
        commands=["system.run"],
    )

    assert entry["clientMode"] == "node"
    assert entry["role"] == "node"
    assert entry["roles"] == ["node"]
    assert entry["scopes"] == []
    assert entry["approvedScopes"] == []

    token_entry = entry["tokens"]["node"]
    assert token_entry["role"] == "node"
    assert token_entry["scopes"] == []
    assert token_entry["token"]
    assert token_entry["createdAtMs"] > 0


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


# ---------------------------------------------------------------------------
# WorkspaceLike helper: a tiny in-memory adapter that records calls and
# exposes the final paired.json state. Used to verify service-layer shape
# without coupling to fcntl / EFS in unit tests.
# ---------------------------------------------------------------------------


class _FakeWorkspace:
    def __init__(self, initial: dict | None = None):
        # ``None`` mirrors "no paired.json yet" from a fresh container.
        self.state: dict | None = None if initial is None else dict(initial)
        self.mutate_calls = 0
        self.write_count = 0
        self.gateway_url = "wss://ws-dev.isol8.co"

    async def mutate_paired_json(self, mutator):
        self.mutate_calls += 1
        current = dict(self.state) if isinstance(self.state, dict) else {}
        changed = mutator(current)
        if changed:
            self.state = current
            self.write_count += 1

    async def get_gateway_token(self) -> str:
        return "container-gw-token"


async def test_pair_desktop_devices_writes_two_paired_entries_flat():
    """Persisted dict must be FLAT — keyed directly by deviceId, no
    `pairedByDeviceId` wrapper. Upstream openclaw writes/reads the flat shape
    on disk (device-pairing.ts:181,186); a wrapper makes our entries invisible
    to the gateway's coercePairingStateRecord load path.
    """
    workspace = _FakeWorkspace(initial={})

    tauri_mac_pubkey = _FIXTURE_PEM
    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    cli_pubkey = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    result: PairResult = await pair_desktop_devices(
        user_id="user_alice",
        owner_id="owner_org_42",
        display_name_base="Alice Smith",
        tauri_mac_pubkey=tauri_mac_pubkey,
        cli_pubkey=cli_pubkey,
        workspace=workspace,
    )

    assert result.gateway_url == "wss://ws-dev.isol8.co"
    assert result.gateway_token == "container-gw-token"
    assert result.device_id_mac != result.device_id_cli
    assert result.commands_mac == DESKTOP_MAC_COMMANDS
    assert result.commands_cli == DESKTOP_CLI_COMMANDS

    assert workspace.write_count == 1
    written = workspace.state
    # REGRESSION GUARD: no wrapper, deviceIds are top-level keys.
    assert "pairedByDeviceId" not in written, (
        "paired.json must be a FLAT Record<deviceId, PairedDevice> — "
        "wrapper would make entries invisible to openclaw's load path"
    )
    assert result.device_id_mac in written
    assert result.device_id_cli in written
    assert len(written) == 2

    mac_entry = written[result.device_id_mac]
    assert mac_entry["clientId"] == "openclaw-macos"
    assert mac_entry["displayName"] == "Alice Smith | Isol8 Mac"
    assert mac_entry["commands"] == list(DESKTOP_MAC_COMMANDS)
    assert mac_entry["tokens"]["node"]["token"]

    cli_entry = written[result.device_id_cli]
    assert cli_entry["clientId"] == "node-host"
    assert cli_entry["displayName"] == "Alice Smith | Isol8 Desktop (CLI)"
    assert cli_entry["commands"] == list(DESKTOP_CLI_COMMANDS)


async def test_pair_desktop_devices_preserves_existing_operator_entry():
    """ecs_manager / config.py writes the operator + in-container node entries
    during provisioning — desktop pair must NOT clobber them. This is the
    "don't nuke ecs_manager's writes" property the original wrapped-shape bug
    silently violated.
    """
    existing_operator_id = "deadbeef" * 8  # 64-char hex
    existing_operator_entry = {
        "deviceId": existing_operator_id,
        "publicKey": "-----BEGIN PUBLIC KEY-----\noperator-key\n-----END PUBLIC KEY-----\n",
        "role": "operator",
        "clientId": "isol8-operator",
        "displayName": "Isol8 backend operator",
        "commands": [],
        "tokens": {"node": {"token": "operator-token", "createdAtMs": 1}},
        "createdAtMs": 1,
        "approvedAtMs": 1,
    }
    workspace = _FakeWorkspace(initial={existing_operator_id: existing_operator_entry})

    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x04" * 32)
    cli_pubkey = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    await pair_desktop_devices(
        user_id="u",
        owner_id="o",
        display_name_base="Alice",
        tauri_mac_pubkey=_FIXTURE_PEM,
        cli_pubkey=cli_pubkey,
        workspace=workspace,
    )

    written = workspace.state
    # The existing operator entry is still there, untouched.
    assert existing_operator_id in written
    assert written[existing_operator_id] == existing_operator_entry
    # AND the two new desktop entries got added.
    assert len(written) == 3


async def test_pair_desktop_devices_idempotent_on_existing_entries():
    """Calling pair twice with the same pubkeys must not duplicate or rewrite."""
    tauri_priv = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
    tauri_pub = (
        tauri_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32)
    cli_pub = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    workspace = _FakeWorkspace(initial={})

    first = await pair_desktop_devices(
        user_id="u",
        owner_id="o",
        display_name_base="Alice",
        tauri_mac_pubkey=tauri_pub,
        cli_pubkey=cli_pub,
        workspace=workspace,
    )
    second = await pair_desktop_devices(
        user_id="u",
        owner_id="o",
        display_name_base="Alice",
        tauri_mac_pubkey=tauri_pub,
        cli_pubkey=cli_pub,
        workspace=workspace,
    )

    assert first.device_id_mac == second.device_id_mac
    assert first.device_id_cli == second.device_id_cli
    # mutate is called twice (we always probe), but the second call's mutator
    # returns False so write_count stays at 1.
    assert workspace.mutate_calls == 2
    assert workspace.write_count == 1


async def test_pair_desktop_devices_prunes_stale_desktop_entries_for_same_user():
    """When a previous Tauri install paired a different keypair under the same
    clientId AND the same userId, the old entry is orphaned (its private key
    is gone with the old Keychain). Pair must prune those ghosts on re-pair,
    otherwise node enumeration shows duplicates and openclaw's gateway can
    round-robin invokes to a dead conn.

    Only entries that are (a) one of our desktop clientIds, (b) stamped with
    the calling user_id, and (c) NOT the device we're about to write get
    pruned. Everything else stays untouched.
    """
    stale_mac_id = "a" * 64
    stale_cli_id = "b" * 64
    in_container_node_id = "c" * 64
    legacy_operator_id = "d" * 64
    workspace = _FakeWorkspace(
        initial={
            stale_mac_id: {
                "deviceId": stale_mac_id,
                "clientId": "openclaw-macos",
                "userId": "user_alice",
                "publicKey": "-----BEGIN PUBLIC KEY-----\nstale-mac\n-----END PUBLIC KEY-----\n",
                "displayName": "Old Mac (orphan)",
            },
            stale_cli_id: {
                "deviceId": stale_cli_id,
                "clientId": "node-host",
                "userId": "user_alice",
                "publicKey": "-----BEGIN PUBLIC KEY-----\nstale-cli\n-----END PUBLIC KEY-----\n",
                "displayName": "Old CLI (orphan)",
            },
            in_container_node_id: {
                "deviceId": in_container_node_id,
                "clientId": "node",
                "role": "node",
                "displayName": "in-container node",
            },
            legacy_operator_id: {
                "deviceId": legacy_operator_id,
                "clientId": "gateway-client",
                "role": "operator",
                "displayName": "legacy operator",
            },
        }
    )

    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x07" * 32)
    cli_pubkey = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    result = await pair_desktop_devices(
        user_id="user_alice",
        owner_id="o",
        display_name_base="Alice",
        tauri_mac_pubkey=_FIXTURE_PEM,
        cli_pubkey=cli_pubkey,
        workspace=workspace,
    )

    written = workspace.state
    # Stale desktop entries pruned.
    assert stale_mac_id not in written, "stale openclaw-macos entry must be pruned"
    assert stale_cli_id not in written, "stale node-host entry must be pruned"
    # Non-desktop entries preserved.
    assert in_container_node_id in written, "in-container node must NOT be pruned"
    assert legacy_operator_id in written, "non-desktop operator must NOT be pruned"
    # Current entries written, and stamped with the caller's userId.
    assert result.device_id_mac in written
    assert result.device_id_cli in written
    assert written[result.device_id_mac]["userId"] == "user_alice"
    assert written[result.device_id_cli]["userId"] == "user_alice"
    # Total: 2 preserved + 2 new = 4. The 2 stale ones are gone.
    assert len(written) == 4


async def test_pair_desktop_devices_preserves_other_org_members_entries():
    """SHARED-WORKSPACE GUARD (P1 from Codex on PR #683): in an org context
    paired.json lives on the org's workspace, so multiple members'
    desktop entries coexist under the same clientId. When Alice re-pairs,
    we MUST NOT delete Bob's still-valid entries.
    """
    bob_mac_id = "1" * 64
    bob_cli_id = "2" * 64
    workspace = _FakeWorkspace(
        initial={
            bob_mac_id: {
                "deviceId": bob_mac_id,
                "clientId": "openclaw-macos",
                "userId": "user_bob",
                "publicKey": "-----BEGIN PUBLIC KEY-----\nbob-mac\n-----END PUBLIC KEY-----\n",
                "displayName": "Bob | Isol8 Mac",
            },
            bob_cli_id: {
                "deviceId": bob_cli_id,
                "clientId": "node-host",
                "userId": "user_bob",
                "publicKey": "-----BEGIN PUBLIC KEY-----\nbob-cli\n-----END PUBLIC KEY-----\n",
                "displayName": "Bob | Isol8 Desktop (CLI)",
            },
        }
    )

    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x0a" * 32)
    cli_pubkey = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    result = await pair_desktop_devices(
        user_id="user_alice",
        owner_id="org_42",
        display_name_base="Alice",
        tauri_mac_pubkey=_FIXTURE_PEM,
        cli_pubkey=cli_pubkey,
        workspace=workspace,
    )

    written = workspace.state
    # Bob's entries survive verbatim.
    assert bob_mac_id in written
    assert bob_cli_id in written
    assert written[bob_mac_id]["userId"] == "user_bob"
    assert written[bob_cli_id]["userId"] == "user_bob"
    # Alice's new entries land alongside.
    assert result.device_id_mac in written
    assert result.device_id_cli in written
    assert written[result.device_id_mac]["userId"] == "user_alice"
    assert written[result.device_id_cli]["userId"] == "user_alice"
    assert len(written) == 4


async def test_pair_desktop_devices_does_not_stamp_userid_on_idempotent_match():
    """SECURITY (Codex round-2 P2 on commit 8bc3c157): the idempotent
    branch must NOT stamp / overwrite userId based on a publicKey match.
    `POST /desktop/pair` accepts the pubkey unsigned — it does not
    verify possession of the matching private key. If we stamped here,
    Alice could submit Bob's pubkey, relabel Bob's entry as Alice's,
    and then trigger her own re-pair to prune Bob's device on the very
    next call.

    Behavior we DO accept: legacy entries without a userId stamp stay
    unstamped forever, so the userId-scoped prune above won't remove
    them. That's a cosmetic-only ghost — clearly preferable to a real
    cross-member privilege escalation in shared org workspaces.
    """
    # Build the keypair first so we can compute its deviceId for the
    # legacy fixture (same deviceId the new pair call will produce).
    tauri_priv = Ed25519PrivateKey.from_private_bytes(b"\x0c" * 32)
    tauri_pub = (
        tauri_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x0d" * 32)
    cli_pub = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    # Compute the deviceIds + raw pubkeys the way build_paired_entry does,
    # so the fixture matches what _mutate's idempotent check compares.
    from core.services.desktop_pair import (
        _public_key_raw_base64url,
        derive_device_id,
    )

    mac_device_id = derive_device_id(tauri_pub)
    cli_device_id = derive_device_id(cli_pub)
    mac_raw_pub = _public_key_raw_base64url(tauri_pub)
    cli_raw_pub = _public_key_raw_base64url(cli_pub)

    workspace = _FakeWorkspace(
        initial={
            # Pre-existing entries with matching deviceId + publicKey,
            # but no userId stamp (legacy / pre-this-PR write).
            mac_device_id: {
                "deviceId": mac_device_id,
                "publicKey": mac_raw_pub,
                "clientId": "openclaw-macos",
                "displayName": "Legacy Mac",
                "tokens": {"node": {"token": "preserve-me", "createdAtMs": 1}},
            },
            cli_device_id: {
                "deviceId": cli_device_id,
                "publicKey": cli_raw_pub,
                "clientId": "node-host",
                "displayName": "Legacy CLI",
                "tokens": {"node": {"token": "preserve-me-too", "createdAtMs": 1}},
            },
        }
    )

    await pair_desktop_devices(
        user_id="user_alice",
        owner_id="o",
        display_name_base="Alice",
        tauri_mac_pubkey=tauri_pub,
        cli_pubkey=cli_pub,
        workspace=workspace,
    )

    written = workspace.state
    # userId stays absent — we refuse to stamp without proof of
    # possession (this is the security-critical invariant).
    assert "userId" not in written[mac_device_id]
    assert "userId" not in written[cli_device_id]
    # Tokens are preserved (idempotent didn't clobber them).
    assert written[mac_device_id]["tokens"]["node"]["token"] == "preserve-me"
    assert written[cli_device_id]["tokens"]["node"]["token"] == "preserve-me-too"
    # Idempotent — nothing changed, no write needed.
    assert workspace.write_count == 0


async def test_pair_desktop_devices_does_not_overwrite_other_users_stamp_on_pubkey_match():
    """SECURITY (Codex round-2 P2): if a different user's stamped entry
    happens to match the caller's submitted pubkey, we MUST NOT relabel
    it as the caller's. The dev path: Alice steals/observes Bob's pubkey
    from a shared workspace and submits it via POST /desktop/pair.
    Without this guard, Bob's entry gets userId=user_alice, and Alice's
    next re-pair (with her own keys) would satisfy the prune and delete
    Bob's device.

    Defense: refuse to touch userId in the idempotent branch entirely.
    """
    tauri_priv = Ed25519PrivateKey.from_private_bytes(b"\x0e" * 32)
    tauri_pub = (
        tauri_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x0f" * 32)
    cli_pub = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    from core.services.desktop_pair import (
        _public_key_raw_base64url,
        derive_device_id,
    )

    mac_device_id = derive_device_id(tauri_pub)
    cli_device_id = derive_device_id(cli_pub)

    # Bob already paired these keypairs (stamped with his userId).
    workspace = _FakeWorkspace(
        initial={
            mac_device_id: {
                "deviceId": mac_device_id,
                "publicKey": _public_key_raw_base64url(tauri_pub),
                "clientId": "openclaw-macos",
                "userId": "user_bob",
                "displayName": "Bob | Isol8 Mac",
            },
            cli_device_id: {
                "deviceId": cli_device_id,
                "publicKey": _public_key_raw_base64url(cli_pub),
                "clientId": "node-host",
                "userId": "user_bob",
                "displayName": "Bob | Isol8 Desktop (CLI)",
            },
        }
    )

    # Alice submits Bob's pubkeys (the attack).
    await pair_desktop_devices(
        user_id="user_alice",
        owner_id="org_42",
        display_name_base="Alice",
        tauri_mac_pubkey=tauri_pub,
        cli_pubkey=cli_pub,
        workspace=workspace,
    )

    written = workspace.state
    # Bob's userId is preserved — Alice cannot relabel by re-submitting his pubkey.
    assert written[mac_device_id]["userId"] == "user_bob"
    assert written[cli_device_id]["userId"] == "user_bob"
    # No new entries written (the deviceIds collide with Bob's existing entries).
    assert len(written) == 2


async def test_pair_desktop_devices_preserves_legacy_unstamped_entries_in_org_workspace():
    """In an org workspace (owner_id != user_id), entries written before
    this PR don't have a userId field. We can't safely attribute them
    to a specific user (could be a teammate's). Preserve them.
    """
    legacy_mac_id = "9" * 64
    workspace = _FakeWorkspace(
        initial={
            legacy_mac_id: {
                "deviceId": legacy_mac_id,
                "clientId": "openclaw-macos",
                # No userId — pre-this-PR entry.
                "publicKey": "-----BEGIN PUBLIC KEY-----\nlegacy\n-----END PUBLIC KEY-----\n",
                "displayName": "Pre-fix Mac",
            },
        }
    )

    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x0b" * 32)
    cli_pubkey = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    await pair_desktop_devices(
        user_id="user_alice",
        owner_id="org_42",  # ORG context: owner_id != user_id
        display_name_base="Alice",
        tauri_mac_pubkey=_FIXTURE_PEM,
        cli_pubkey=cli_pubkey,
        workspace=workspace,
    )

    written = workspace.state
    assert legacy_mac_id in written, (
        "userId-less legacy entry in an org workspace must be preserved "
        "(could belong to a teammate)"
    )
    assert len(written) == 3  # 1 legacy + 2 new


async def test_pair_desktop_devices_prunes_legacy_unstamped_entries_in_personal_workspace():
    """Codex round-3 P2 on PR #683: in a personal workspace
    (owner_id == user_id), there's no other user who could own a
    legacy unstamped entry. The attack vector that drove the org-side
    preserve doesn't exist here. Safely prune so personal users who
    upgrade from pre-this-PR builds get clean enumeration.
    """
    legacy_mac_id = "9" * 64
    legacy_cli_id = "8" * 64
    workspace = _FakeWorkspace(
        initial={
            legacy_mac_id: {
                "deviceId": legacy_mac_id,
                "clientId": "openclaw-macos",
                "publicKey": "-----BEGIN PUBLIC KEY-----\nlegacy-mac\n-----END PUBLIC KEY-----\n",
                "displayName": "Pre-fix Mac",
                # No userId.
            },
            legacy_cli_id: {
                "deviceId": legacy_cli_id,
                "clientId": "node-host",
                "publicKey": "-----BEGIN PUBLIC KEY-----\nlegacy-cli\n-----END PUBLIC KEY-----\n",
                "displayName": "Pre-fix CLI",
                # No userId.
            },
        }
    )

    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x10" * 32)
    cli_pubkey = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    result = await pair_desktop_devices(
        user_id="user_alice",
        owner_id="user_alice",  # PERSONAL context: owner_id == user_id
        display_name_base="Alice",
        tauri_mac_pubkey=_FIXTURE_PEM,
        cli_pubkey=cli_pubkey,
        workspace=workspace,
    )

    written = workspace.state
    assert legacy_mac_id not in written, "legacy unstamped mac must be pruned in personal workspace"
    assert legacy_cli_id not in written, "legacy unstamped cli must be pruned in personal workspace"
    assert result.device_id_mac in written
    assert result.device_id_cli in written
    assert len(written) == 2  # legacy pruned, 2 new written


async def test_pair_desktop_devices_idempotent_does_not_trigger_prune():
    """When the entries already match the keys we're about to write, the
    mutator must NOT mark itself changed — pruning of "stale" entries
    matching our own current clientIds must respect the new_device_ids
    skip list."""
    tauri_priv = Ed25519PrivateKey.from_private_bytes(b"\x08" * 32)
    tauri_pub = (
        tauri_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x09" * 32)
    cli_pub = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    workspace = _FakeWorkspace(initial={})
    await pair_desktop_devices(
        user_id="u",
        owner_id="o",
        display_name_base="Alice",
        tauri_mac_pubkey=tauri_pub,
        cli_pubkey=cli_pub,
        workspace=workspace,
    )
    assert workspace.write_count == 1

    # Second call with identical pubkeys: the existing entries match
    # new_device_ids and pass the publicKey-equality idempotent check.
    # write_count must stay at 1.
    await pair_desktop_devices(
        user_id="u",
        owner_id="o",
        display_name_base="Alice",
        tauri_mac_pubkey=tauri_pub,
        cli_pubkey=cli_pub,
        workspace=workspace,
    )
    assert workspace.write_count == 1


async def test_revoke_desktop_device_removes_paired_entry():
    """Revoke deletes a TOP-LEVEL deviceId key from the flat record."""
    workspace = _FakeWorkspace(
        initial={
            "dev_abc": {"role": "node"},
            "dev_xyz": {"role": "node"},
        }
    )

    mock_force_close = AsyncMock(return_value=True)

    removed = await revoke_desktop_device(
        device_id="dev_abc",
        workspace=workspace,
        force_close_device=mock_force_close,
    )
    assert removed is True
    assert workspace.write_count == 1
    written = workspace.state
    assert "dev_abc" not in written
    assert "dev_xyz" in written
    # REGRESSION: no wrapper.
    assert "pairedByDeviceId" not in written
    mock_force_close.assert_awaited_once_with("dev_abc")


async def test_revoke_desktop_device_no_op_on_missing():
    workspace = _FakeWorkspace(initial={})
    mock_force_close = AsyncMock(return_value=False)

    removed = await revoke_desktop_device(
        device_id="dev_none",
        workspace=workspace,
        force_close_device=mock_force_close,
    )
    assert removed is False
    assert workspace.write_count == 0
    # Still attempt force_close in case device is connected without a paired entry
    mock_force_close.assert_awaited_once_with("dev_none")


# ---------------------------------------------------------------------------
# EfsWorkspaceAdapter — real-filesystem integration via tmp_path
# ---------------------------------------------------------------------------
# Uses a real ``Workspace`` against a tmp directory so we exercise the full
# locked RMW path (fcntl.lockf on devices/paired.json.lock, tempfile-rename,
# chown-skip when not root). MagicMock-only tests can't catch path / lock
# bugs because they don't touch the filesystem.


async def test_efs_workspace_adapter_writes_to_devices_paired_json(tmp_path):
    """Path regression: paired.json must live at <owner>/devices/paired.json,
    not <owner>/paired.json. Openclaw's resolvePairingPaths reads
    <stateDir>/devices/paired.json (pairing-files.ts:6-13).
    """
    workspace = Workspace(mount_path=str(tmp_path))
    mock_container_repo = MagicMock()
    mock_container_repo.get_by_owner_id = AsyncMock(return_value={"gateway_token": "tok_123"})

    adapter = EfsWorkspaceAdapter(
        owner_id="owner_42",
        workspace=workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://ws-dev.isol8.co",
    )

    def _add(paired: dict) -> bool:
        paired["new_device_id"] = {"role": "node", "publicKey": "pk"}
        return True

    await adapter.mutate_paired_json(_add)

    # File lands at <owner>/devices/paired.json, NOT <owner>/paired.json.
    devices_path = tmp_path / "owner_42" / "devices" / "paired.json"
    legacy_path = tmp_path / "owner_42" / "paired.json"
    assert devices_path.exists(), f"paired.json should exist at {devices_path}"
    assert not legacy_path.exists(), (
        "paired.json must NOT live at the legacy <owner>/paired.json path; openclaw reads <owner>/devices/paired.json"
    )

    # Shape is flat (single key, deviceId at top level).
    data = json.loads(devices_path.read_text())
    assert data == {"new_device_id": {"role": "node", "publicKey": "pk"}}


async def test_efs_workspace_adapter_mutator_sees_existing_dict(tmp_path):
    """Mutator receives the current flat-dict state — a previously written
    operator entry should be visible so the desktop pair flow can preserve it.
    """
    workspace = Workspace(mount_path=str(tmp_path))
    devices_dir = tmp_path / "owner_42" / "devices"
    devices_dir.mkdir(parents=True)
    initial = {
        "operator_id": {
            "deviceId": "operator_id",
            "role": "operator",
            "publicKey": "pk_op",
        }
    }
    (devices_dir / "paired.json").write_text(json.dumps(initial))

    mock_container_repo = MagicMock()
    adapter = EfsWorkspaceAdapter(
        owner_id="owner_42",
        workspace=workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://x",
    )

    seen = {}

    def _inspect(paired: dict) -> bool:
        seen.update(paired)
        paired["new_id"] = {"role": "node", "publicKey": "pk_new"}
        return True

    await adapter.mutate_paired_json(_inspect)
    assert seen == initial

    data = json.loads((devices_dir / "paired.json").read_text())
    assert "operator_id" in data
    assert "new_id" in data
    assert len(data) == 2


async def test_efs_workspace_adapter_no_op_when_mutator_returns_false(tmp_path):
    """If the mutator returns False, no write happens (idempotent path)."""
    workspace = Workspace(mount_path=str(tmp_path))
    devices_dir = tmp_path / "owner_42" / "devices"
    devices_dir.mkdir(parents=True)
    initial = {"k": {"role": "node"}}
    paired_file = devices_dir / "paired.json"
    paired_file.write_text(json.dumps(initial))
    mtime_before = paired_file.stat().st_mtime_ns

    mock_container_repo = MagicMock()
    adapter = EfsWorkspaceAdapter(
        owner_id="owner_42",
        workspace=workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://x",
    )

    def _no_op(_paired: dict) -> bool:
        return False

    await adapter.mutate_paired_json(_no_op)
    # File untouched.
    assert json.loads(paired_file.read_text()) == initial
    assert paired_file.stat().st_mtime_ns == mtime_before


async def test_efs_workspace_adapter_creates_devices_dir_if_missing(tmp_path):
    """Fresh container: <owner>/devices/ doesn't exist yet. Adapter must
    create it on first write (otherwise the very first /desktop/pair call
    would crash on FileNotFoundError).
    """
    workspace = Workspace(mount_path=str(tmp_path))
    mock_container_repo = MagicMock()
    adapter = EfsWorkspaceAdapter(
        owner_id="brand_new_owner",
        workspace=workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://x",
    )

    def _seed(paired: dict) -> bool:
        paired["d1"] = {"role": "node"}
        return True

    # Pre-condition: nothing exists.
    assert not (tmp_path / "brand_new_owner").exists()
    await adapter.mutate_paired_json(_seed)
    # Post-condition: devices/paired.json exists with the entry.
    paired_file = tmp_path / "brand_new_owner" / "devices" / "paired.json"
    assert paired_file.exists()
    assert json.loads(paired_file.read_text()) == {"d1": {"role": "node"}}


async def test_efs_workspace_adapter_handles_missing_paired_json_as_empty(tmp_path):
    """A fresh container has no paired.json yet — mutator should see an
    empty dict (not None), so callers can unconditionally `paired[k] = entry`.
    """
    workspace = Workspace(mount_path=str(tmp_path))
    mock_container_repo = MagicMock()
    adapter = EfsWorkspaceAdapter(
        owner_id="o",
        workspace=workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://x",
    )

    seen = {"value": None}

    def _inspect(paired: dict) -> bool:
        seen["value"] = dict(paired)
        return False

    await adapter.mutate_paired_json(_inspect)
    assert seen["value"] == {}


async def test_efs_workspace_adapter_raises_when_no_gateway_token():
    mock_workspace = MagicMock()
    mock_container_repo = MagicMock()
    mock_container_repo.get_by_owner_id = AsyncMock(return_value={"gateway_token": ""})
    adapter = EfsWorkspaceAdapter(
        owner_id="o",
        workspace=mock_workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://x",
    )
    with pytest.raises(RuntimeError, match="No gateway_token"):
        await adapter.get_gateway_token()


async def test_efs_workspace_adapter_raises_when_container_missing():
    """No container row at all (e.g. pre-provision) — must also raise."""
    mock_workspace = MagicMock()
    mock_container_repo = MagicMock()
    mock_container_repo.get_by_owner_id = AsyncMock(return_value=None)
    adapter = EfsWorkspaceAdapter(
        owner_id="o",
        workspace=mock_workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://x",
    )
    with pytest.raises(RuntimeError, match="No gateway_token"):
        await adapter.get_gateway_token()


async def test_efs_workspace_adapter_serializes_concurrent_mutators_in_process(
    tmp_path,
):
    """In-process concurrency regression: POSIX fcntl locks are
    process-scoped, not thread-scoped. Two ``mutate_paired_json`` calls
    running inside ``asyncio.to_thread`` on the same worker can both
    "acquire" the fcntl lock, both load the same on-disk state, and the
    last ``os.rename`` drops the other's edits.

    Defense: a per-owner ``threading.Lock`` wraps the read-modify-write
    so threads in the same process serialize before they ever reach the
    fcntl lock. This test fires N mutators concurrently; each adds its
    own unique key; with the fix, all N keys survive.
    """
    workspace = Workspace(mount_path=str(tmp_path))
    mock_container_repo = MagicMock()
    adapter = EfsWorkspaceAdapter(
        owner_id="race_owner",
        workspace=workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://x",
    )

    n = 20

    def _make_mutator(idx: int):
        def _mutator(paired: dict) -> bool:
            # Force a context-switch window to broaden the race: read
            # the file, do a small sleep, then write back. Without
            # threading.Lock multiple threads will collide here.
            import time as _time

            _time.sleep(0.01)
            paired[f"dev_{idx}"] = {"role": "node", "publicKey": f"pk_{idx}"}
            return True

        return _mutator

    import asyncio as _asyncio

    await _asyncio.gather(*[adapter.mutate_paired_json(_make_mutator(i)) for i in range(n)])

    data = json.loads((tmp_path / "race_owner" / "devices" / "paired.json").read_text())
    # Without the threading.Lock fix, some keys will be missing because
    # concurrent writers overwrote each other. With the fix, all N
    # survive.
    assert len(data) == n, f"lost writes from concurrent mutators: {sorted(data.keys())}"
    for i in range(n):
        assert f"dev_{i}" in data


async def test_efs_workspace_adapter_per_owner_lock_independent(tmp_path):
    """Per-owner locks must NOT serialize across owners — Alice's pair
    flow shouldn't block Bob's. Otherwise we'd serialize the entire
    paired.json writer fleet behind a single mutex.
    """
    workspace = Workspace(mount_path=str(tmp_path))
    mock_container_repo = MagicMock()
    adapter_a = EfsWorkspaceAdapter(
        owner_id="owner_a",
        workspace=workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://x",
    )
    adapter_b = EfsWorkspaceAdapter(
        owner_id="owner_b",
        workspace=workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://x",
    )

    def _add(key: str):
        def _mutator(paired: dict) -> bool:
            paired[key] = {"role": "node"}
            return True

        return _mutator

    import asyncio as _asyncio

    await _asyncio.gather(
        adapter_a.mutate_paired_json(_add("dev_a")),
        adapter_b.mutate_paired_json(_add("dev_b")),
    )

    data_a = json.loads((tmp_path / "owner_a" / "devices" / "paired.json").read_text())
    data_b = json.loads((tmp_path / "owner_b" / "devices" / "paired.json").read_text())
    assert "dev_a" in data_a
    assert "dev_b" in data_b


async def test_efs_workspace_adapter_pair_then_revoke_round_trip(tmp_path):
    """End-to-end against the real Workspace + a real tmp EFS root: pair
    writes the two entries flat under devices/paired.json, then revoke
    removes one. Catches any path / shape drift in a single integration test.
    """
    workspace = Workspace(mount_path=str(tmp_path))
    mock_container_repo = MagicMock()
    mock_container_repo.get_by_owner_id = AsyncMock(return_value={"gateway_token": "t"})
    adapter = EfsWorkspaceAdapter(
        owner_id="owner",
        workspace=workspace,
        container_repo=mock_container_repo,
        gateway_ws_url="wss://ws-dev.isol8.co",
    )

    tauri_priv = Ed25519PrivateKey.from_private_bytes(b"\x05" * 32)
    tauri_pub = (
        tauri_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    cli_priv = Ed25519PrivateKey.from_private_bytes(b"\x06" * 32)
    cli_pub = (
        cli_priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    result = await pair_desktop_devices(
        user_id="u",
        owner_id="owner",
        display_name_base="Bob",
        tauri_mac_pubkey=tauri_pub,
        cli_pubkey=cli_pub,
        workspace=adapter,
    )

    paired_file = tmp_path / "owner" / "devices" / "paired.json"
    data = json.loads(paired_file.read_text())
    assert "pairedByDeviceId" not in data
    assert result.device_id_mac in data
    assert result.device_id_cli in data
    assert len(data) == 2

    removed = await revoke_desktop_device(
        device_id=result.device_id_mac,
        workspace=adapter,
        force_close_device=AsyncMock(return_value=True),
    )
    assert removed is True

    data2 = json.loads(paired_file.read_text())
    assert result.device_id_mac not in data2
    assert result.device_id_cli in data2
    assert len(data2) == 1
