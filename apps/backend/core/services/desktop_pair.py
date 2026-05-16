"""Desktop pair / revoke business logic for the two-node architecture.

The desktop ships two openclaw nodes per Mac:
  - Tauri-mac process (clientId="openclaw-macos"): macOS-native commands
  - openclaw CLI subprocess (clientId="node-host"): cross-platform commands

Command sets MUST be disjoint so that openclaw's gateway routing is
unambiguous and a single-process compromise can't pivot to the other's
command surface. Backend derives these sets here — clients do NOT submit
them on /desktop/pair.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives import serialization

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

ALLOWED_CLIENT_IDS = {"openclaw-macos", "node-host"}


def assert_command_sets_disjoint(
    mac: list[str] | tuple[str, ...],
    cli: list[str] | tuple[str, ...],
) -> None:
    overlap = set(mac) & set(cli)
    assert not overlap, f"Mac and CLI command sets must be disjoint; overlap: {overlap}"


# Module-load defensive assertion: protect against future drift in the constants above.
assert_command_sets_disjoint(DESKTOP_MAC_COMMANDS, DESKTOP_CLI_COMMANDS)


def derive_device_id(pubkey_pem: str) -> str:
    """Mirror openclaw's deriveDeviceIdFromPublicKey.

    Upstream strips the 12-byte ED25519 SPKI prefix and SHA-256s the 32 raw
    key bytes. We use cryptography.serialization to parse the PEM into the
    raw form natively — avoids manual prefix-stripping fragility.
    """
    pubkey = serialization.load_pem_public_key(pubkey_pem.encode())
    raw = pubkey.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
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


class WorkspaceLike(Protocol):
    gateway_url: str

    async def read_paired_json(self) -> dict | None: ...
    async def write_paired_json(self, data: dict) -> None: ...
    async def get_gateway_token(self) -> str: ...


@dataclass(frozen=True)
class PairResult:
    gateway_url: str
    gateway_token: str
    device_id_mac: str
    device_id_cli: str
    commands_mac: tuple[str, ...]
    commands_cli: tuple[str, ...]


async def pair_desktop_devices(
    *,
    user_id: str,
    owner_id: str,
    display_name_base: str,
    tauri_mac_pubkey: str,
    cli_pubkey: str,
    workspace: WorkspaceLike,
) -> PairResult:
    """Idempotently register the user's two devices in the owner's container paired.json.

    Returns connection material the desktop needs to spawn the openclaw subprocess
    and connect both nodes to the gateway.
    """
    mac_entry = build_paired_entry(
        pubkey_pem=tauri_mac_pubkey,
        client_id="openclaw-macos",
        display_name=f"{display_name_base} | Isol8 Mac",
        commands=list(DESKTOP_MAC_COMMANDS),
    )
    cli_entry = build_paired_entry(
        pubkey_pem=cli_pubkey,
        client_id="node-host",
        display_name=f"{display_name_base} | Isol8 Desktop (CLI)",
        commands=list(DESKTOP_CLI_COMMANDS),
    )

    paired = (await workspace.read_paired_json()) or {"pairedByDeviceId": {}}
    by_id = paired.setdefault("pairedByDeviceId", {})

    needs_write = False
    for entry in (mac_entry, cli_entry):
        device_id = entry["deviceId"]
        existing = by_id.get(device_id)
        if existing and existing.get("publicKey") == entry["publicKey"]:
            # Idempotent: existing entry has the same key — keep its tokens.
            continue
        by_id[device_id] = entry
        needs_write = True

    if needs_write:
        await workspace.write_paired_json(paired)

    return PairResult(
        gateway_url=workspace.gateway_url,
        gateway_token=await workspace.get_gateway_token(),
        device_id_mac=mac_entry["deviceId"],
        device_id_cli=cli_entry["deviceId"],
        commands_mac=DESKTOP_MAC_COMMANDS,
        commands_cli=DESKTOP_CLI_COMMANDS,
    )
