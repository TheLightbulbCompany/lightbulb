"""Desktop pair / revoke business logic for the two-node architecture.

The desktop ships two openclaw nodes per Mac:
  - Tauri-mac process (clientId="openclaw-macos"): macOS-native commands
  - openclaw CLI subprocess (clientId="node-host"): cross-platform commands

Command sets MUST be disjoint so that openclaw's gateway routing is
unambiguous and a single-process compromise can't pivot to the other's
command surface. Backend derives these sets here — clients do NOT submit
them on /desktop/pair.
"""

DESKTOP_MAC_COMMANDS: tuple[str, ...] = (
    "system.notify",
    "screen.snapshot",
    "device.info",
    "device.status",
    "device.health",
    "device.permissions",
)

DESKTOP_CLI_COMMANDS: tuple[str, ...] = (
    "system.run",
    "system.run.prepare",
    "system.which",
    "browser.proxy",
    "file.fetch",
    "file.write",
    "dir.list",
    "dir.fetch",
)


def assert_command_sets_disjoint(mac: list[str] | tuple[str, ...], cli: list[str] | tuple[str, ...]) -> None:
    overlap = set(mac) & set(cli)
    assert not overlap, f"Mac and CLI command sets must be disjoint; overlap: {overlap}"


# Module-load defensive assertion: protect against future drift in the constants above.
assert_command_sets_disjoint(DESKTOP_MAC_COMMANDS, DESKTOP_CLI_COMMANDS)


import base64
import hashlib
import secrets
import time

ALLOWED_CLIENT_IDS = {"openclaw-macos", "node-host"}


def derive_device_id(pubkey_pem: str) -> str:
    """Mirror openclaw's deriveDeviceIdFromPublicKey: SHA-256 of the raw key bytes.

    The pem is text but the SHA is computed over the canonical bytes; we strip
    PEM headers and base64-decode to match upstream's `Buffer.from(pubKeyRaw)`.
    """
    body = "".join(
        line for line in pubkey_pem.strip().splitlines()
        if not line.startswith("-----")
    )
    raw = base64.b64decode(body)
    return hashlib.sha256(raw).hexdigest()


def build_paired_entry(
    *,
    pubkey_pem: str,
    client_id: str,
    display_name: str,
    commands: list[str],
) -> dict:
    """Construct a paired.json entry per upstream PairedDevice schema.

    tokens.node is load-bearing — listEffectivePairedDeviceRoles returns []
    for tokenless paired records, leaving the device connected but stripped
    of effective commands like system.run.
    """
    if client_id not in ALLOWED_CLIENT_IDS:
        raise ValueError(f"clientId must be one of {ALLOWED_CLIENT_IDS}; got {client_id!r}")
    now_ms = int(time.time() * 1000)
    return {
        "deviceId": derive_device_id(pubkey_pem),
        "publicKey": pubkey_pem,
        "role": "node",
        "clientId": client_id,
        "clientMode": "node",
        "displayName": display_name,
        "commands": commands,
        "tokens": {
            "node": {
                "token": secrets.token_urlsafe(32),
                "createdAtMs": now_ms,
            },
        },
        "createdAtMs": now_ms,
        "approvedAtMs": now_ms,
    }
