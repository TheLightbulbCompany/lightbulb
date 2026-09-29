"""Desktop pair / revoke business logic for the two-node architecture.

The desktop ships two openclaw nodes per Mac:
  - Tauri-mac process (clientId="openclaw-macos"): macOS-native commands
  - openclaw CLI subprocess (clientId="node-host"): cross-platform commands

Command sets MUST be disjoint so that openclaw's gateway routing is
unambiguous and a single-process compromise can't pivot to the other's
command surface. Backend derives these sets here — clients do NOT submit
them on /desktop/pair.
"""

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from cryptography.hazmat.primitives import serialization

from core.config import settings
from core.repositories import container_repo

logger = logging.getLogger(__name__)


# NOTE: the retired-brand display-name normalizer went with the EFS ``paired.json`` rail.
# It existed because a same-key re-pair PRESERVED the stored entry, so nothing would ever
# refresh a device's name. ``pair_desktop_devices`` now REWRITES each device's map entry on
# every pair (the non-relabel ConditionExpression is the only thing it preserves), so the
# composed name is current by construction.


DESKTOP_MAC_COMMANDS: tuple[str, ...] = (
    "desktop.operation.approve.v1",
    "system.run.approve.v1",
    "file.access.policy.v1",
    "system.notify",
    "screen.snapshot",
    "device.info",
    "device.status",
    "device.health",
    "device.permissions",
    # Owner-only "start a Claude Code / Codex session here" (desktop_session_start):
    # the Mac approves each start natively and launches only its fixed CLI.
    "session.options.v1",
    "session.start.v1",
    # Raw CDP to the Mac's own Chrome over the node socket (node_broker.cdp_open). Rides Browser.
    "browser.cdp.v1",
    # Quit the agent Chrome and delete its profile. Always granted: it only removes data.
    "browser.wipe.v1",
)

DESKTOP_MAC_BASE_COMMANDS: tuple[str, ...] = (
    "desktop.operation.approve.v1",
    "system.notify",
    "device.info",
    "device.status",
    "device.health",
    "device.permissions",
    "browser.wipe.v1",
)

DESKTOP_CLI_SHELL_COMMANDS: tuple[str, ...] = (
    "system.run",
    "system.run.prepare",
    "system.which",
    # Local MCP servers hosted by the node host on the user's own Mac. Rides
    # the shell group: upstream puts it in the same DESKTOP_HOST_COMMANDS
    # bucket as system.run (gateway/node-command-policy.ts), it is served by
    # the same process, and an MCP server is arbitrary local code by proxy —
    # the same trust decision the user already made for system.run.
    #
    # Omitting it fails SILENTLY: the gateway drops every node MCP tool
    # descriptor whose command is not in the node's effective command list
    # (gateway/node-plugin-tool-snapshot.ts `allowedCommands.has(command)`),
    # so the agent simply never sees the tools. No error anywhere.
    "mcp.tools.call.v1",
    # The 2026.8.1 node host declares these four UNCONDITIONALLY in its connect
    # manifest (upstream src/node-host/runtime.ts:374-378), regardless of the
    # owner's permission grant. Withholding any of them makes
    # `declared <= allowed` false on every reconnect
    # (gateway/node-connect-reconcile.ts `hasCommandUpgrade`), so the gateway
    # re-files a pending node-surface request on every reconnect and
    # `desktop_pair_live.approve_pending_node_surface` refuses it forever —
    # the node connects but its effective commands are stripped to [].
    "system.execApprovals.get",
    "system.execApprovals.set",
    "fs.listDir",
    "terminal.upload",
)

# The pair's stored Browser grant: the CLI node entry carries this literal when Browser was
# on at pairing, and granted_commands() derives the Mac app's browser.cdp.v1 from it. The CLI
# node no longer serves it (the old node-host browser proxy is gone), so at the relay it
# grants nothing; it is only the marker. Renaming it strands every existing pair's grant.
BROWSER_GRANT_MARKER = "browser.proxy"
DESKTOP_CLI_BROWSER_COMMANDS: tuple[str, ...] = (BROWSER_GRANT_MARKER,)

DESKTOP_CLI_FILE_COMMANDS: tuple[str, ...] = (
    "file.fetch",
    "file.write",
    "dir.list",
    "dir.fetch",
)

# Derived, not hand-copied: a full grant is exactly SHELL + BROWSER + FILE in
# that order, and cli_commands() below emits the segments in the same order.
DESKTOP_CLI_COMMANDS: tuple[str, ...] = (
    DESKTOP_CLI_SHELL_COMMANDS + DESKTOP_CLI_BROWSER_COMMANDS + DESKTOP_CLI_FILE_COMMANDS
)

# Capabilities the 2026.8.1 node host declares UNCONDITIONALLY in its connect
# manifest (node-host/runtime.ts `buildManifest`: caps ["system", "mcp", …]).
#
# They must be approved unconditionally too. The gateway treats any declared
# cap missing from the approved set as a capability upgrade and files a fresh
# pending pairing request on EVERY reconnect
# (gateway/node-connect-reconcile.ts `hasCapabilityUpgrade`). Withholding the
# command does not help: `retainFulfilledNodeCapabilities` only drops caps in
# its CAPABILITY_COMMAND_FAMILIES map (camera/computer/location/screen), and
# neither "system" nor "mcp" is in it.
#
# Approving them costs nothing. Caps are advertisement — used for node
# discovery/filtering only. Execution is gated separately by
# `isNodeCommandAllowed` against the node's EFFECTIVE command list
# (approved ∩ declared), so a cap without its command grants no authority.
NODE_HOST_DECLARED_CAPS: tuple[str, ...] = ("system", "mcp")

ALLOWED_CLIENT_IDS = {"openclaw-macos", "node-host"}

# Legacy (permission-less) pairs never consented to a native approval handler.
_EXPLICIT_CONSENT_COMMANDS = frozenset(
    {"system.run.approve.v1", "desktop.operation.approve.v1", "session.options.v1", "session.start.v1"}
)


def assert_command_sets_disjoint(
    mac: list[str] | tuple[str, ...],
    cli: list[str] | tuple[str, ...],
) -> None:
    overlap = set(mac) & set(cli)
    assert not overlap, f"Mac and CLI command sets must be disjoint; overlap: {overlap}"


# Module-load defensive assertion: protect against future drift in the constants above.
assert_command_sets_disjoint(DESKTOP_MAC_COMMANDS, DESKTOP_CLI_COMMANDS)


def paired_physical_mac(row: dict, member_id: str, device_id: str) -> tuple[dict, dict] | None:
    """Return the selected device and its reciprocal native/CLI peer.

    Legacy and partially-written pairs have no authority for desktop execution. The old
    relay remains compatible with them; an explicit re-pair adds the linkage.
    """
    devices = (row or {}).get("paired_devices") or {}
    selected = devices.get(device_id)
    group_id = selected.get("pairingGroupId") if isinstance(selected, dict) else None
    if not group_id or selected.get("userId") != member_id:
        return None
    linked = [
        entry
        for entry in devices.values()
        if isinstance(entry, dict) and entry.get("pairingGroupId") == group_id and entry.get("userId") == member_id
    ]
    by_client = {entry.get("clientId"): entry for entry in linked}
    if len(linked) != 2 or set(by_client) != ALLOWED_CLIENT_IDS:
        return None
    peer_client = "node-host" if selected.get("clientId") == "openclaw-macos" else "openclaw-macos"
    peer = by_client.get(peer_client)
    return (selected, peer) if peer is not None else None


@dataclass(frozen=True)
class DesktopPermissionGrant:
    """User-granted desktop command groups.

    `None` from the API layer is treated as legacy/full access so older
    desktop clients keep working. New clients send this explicitly after the
    user accepts the first-run permission screen.
    """

    screen: bool = False
    shell: bool = False
    browser: bool = False
    files: bool = False

    @classmethod
    def full(cls) -> "DesktopPermissionGrant":
        return cls(screen=True, shell=True, browser=True, files=True)

    def mac_commands(self) -> tuple[str, ...]:
        allowed = set(DESKTOP_MAC_BASE_COMMANDS)
        if self.screen:
            allowed.add("screen.snapshot")
        if self.shell:
            allowed |= {"system.run.approve.v1", "session.options.v1", "session.start.v1"}
        if self.files:
            allowed.add("file.access.policy.v1")
        if self.browser:
            allowed.add("browser.cdp.v1")
        return tuple(command for command in DESKTOP_MAC_COMMANDS if command in allowed)

    def cli_commands(self) -> tuple[str, ...]:
        commands: list[str] = []
        if self.shell:
            commands.extend(DESKTOP_CLI_SHELL_COMMANDS)
        if self.browser:
            commands.extend(DESKTOP_CLI_BROWSER_COMMANDS)
        if self.files:
            commands.extend(DESKTOP_CLI_FILE_COMMANDS)
        return tuple(commands)

    def mac_caps(self) -> list[str]:
        caps = ["system"]
        if self.screen:
            caps.append("screen")
        return caps

    def cli_caps(self) -> list[str]:
        # NODE_HOST_DECLARED_CAPS is unconditional — see its comment. The
        # grant-gated caps below come from the node host's PLUGIN manifest, so
        # they are only ever declared when that plugin is present.
        caps: list[str] = list(NODE_HOST_DECLARED_CAPS)
        if self.files:
            caps.append("file")
        return caps


async def declared_commands_for(owner_id: str, device_id: str) -> set[str]:
    """The node broker's command allowlist for one paired device.

    Reads the backend-written ``paired_devices`` map on the owner's containers row -- the
    same record ``machine_auth`` verifies the device's WS-upgrade token against, so a device
    cannot be trusted for auth and unknown for commands. A missing row, a missing map or an
    unpaired device id reads as an empty set and never raises: the relay intersects a
    connecting client's declared commands with this set, so an unreadable or not-yet-paired
    record simply grants nothing rather than failing the connect.
    """
    from core.repositories import container_repo

    try:
        row = await container_repo.get_by_owner_id(owner_id)
    except Exception:  # noqa: BLE001 — an unreadable record grants nothing, never fails the connect
        return set()
    devices = (row or {}).get("paired_devices") or {}
    entry = devices.get(device_id)
    commands = entry.get("commands") if isinstance(entry, dict) else None
    return granted_commands(devices, entry) if isinstance(commands, list) else set()


def _browser_granted(devices: dict, entry: dict) -> bool:
    """The pair's Browser grant, read off the reciprocal CLI node's stored
    ``BROWSER_GRANT_MARKER`` (same pairing group, same member)."""
    group = entry.get("pairingGroupId")
    return bool(group) and any(
        isinstance(peer, dict)
        and peer.get("clientId") == "node-host"
        and peer.get("pairingGroupId") == group
        and peer.get("userId") == entry.get("userId")
        and BROWSER_GRANT_MARKER in (peer.get("commands") or [])
        for peer in devices.values()
    )


def granted_commands(devices: dict, entry: dict) -> set[str]:
    """A paired device's granted commands. The Mac app's browser commands are DERIVED so a Mac
    paired before they existed gets them with no re-pair and no second consent (consent is
    given once, at pairing): ``browser.wipe.v1`` always, and ``browser.cdp.v1`` from the pair's
    Browser grant. The Mac still refuses it locally while Browser is switched off."""
    granted = set(entry.get("commands") or [])
    if entry.get("clientId") == "openclaw-macos" and entry.get("pairingGroupId"):
        granted.add("browser.wipe.v1")
        if _browser_granted(devices, entry):
            granted.add("browser.cdp.v1")
    return granted


def _public_key_raw_bytes(pubkey_pem: str) -> bytes:
    """Parse PEM-encoded Ed25519 SPKI and return the 32 raw key bytes.

    Upstream strips the 12-byte ED25519 SPKI prefix; cryptography's
    ``serialization`` parses the PEM into the raw form natively, avoiding
    manual prefix-stripping fragility.
    """
    pubkey = serialization.load_pem_public_key(pubkey_pem.encode())
    return pubkey.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _public_key_raw_base64url(pubkey_pem: str) -> str:
    """Return the Ed25519 raw 32-byte key encoded as base64url-no-pad.

    Mirrors upstream's ``publicKeyRawBase64UrlFromPem`` (openclaw
    src/infra/device-identity.ts). This is the EXACT wire format the
    gateway sees in the signed connect frame and the EXACT shape it
    string-compares against paired.json (message-handler.ts
    `paired?.publicKey === devicePublicKey`). Writing PEM here breaks the
    equality and gets every device flagged NOT_PAIRED.
    """
    return base64.urlsafe_b64encode(_public_key_raw_bytes(pubkey_pem)).decode().rstrip("=")


def derive_device_id(pubkey_pem: str) -> str:
    """Mirror openclaw's deriveDeviceIdFromPublicKey.

    Upstream SHA-256s the 32 raw key bytes (post-SPKI-strip).
    """
    return hashlib.sha256(_public_key_raw_bytes(pubkey_pem)).hexdigest()


def build_paired_entry(
    *,
    pubkey_pem: str,
    client_id: str,
    display_name: str,
    commands: list[str],
    user_id: str | None = None,
) -> dict:
    """Construct a paired.json entry per upstream PairedDevice schema.

    The ``publicKey`` field is the base64url-no-pad 32-byte raw key, NOT
    the PEM. Upstream's gateway does strict string equality of paired.json's
    ``publicKey`` against the wire-format pubkey in the signed connect
    frame (message-handler.ts); the wire format is
    base64url-no-pad raw 32 bytes (server.auth.shared.ts
    `publicKeyRawBase64UrlFromPem`). PEM here would always mismatch.

    tokens.node is load-bearing — listEffectivePairedDeviceRoles returns []
    for tokenless paired records, leaving the device connected but stripped
    of effective commands like system.run. The ``role``/``scopes`` inside
    tokens.node mirror the canonical shape the in-container counterpart writes.
    """
    if client_id not in ALLOWED_CLIENT_IDS:
        raise ValueError(f"clientId must be one of {ALLOWED_CLIENT_IDS}; got {client_id!r}")
    now_ms = int(time.time() * 1000)
    entry: dict = {
        "deviceId": derive_device_id(pubkey_pem),
        "publicKey": _public_key_raw_base64url(pubkey_pem),
        "role": "node",
        "roles": ["node"],
        "clientId": client_id,
        "clientMode": "node",
        "displayName": display_name,
        "commands": commands,
        "scopes": [],
        "approvedScopes": [],
        "tokens": {
            "node": {
                "token": secrets.token_urlsafe(32),
                "role": "node",
                "scopes": [],
                "createdAtMs": now_ms,
            },
        },
        "createdAtMs": now_ms,
        "approvedAtMs": now_ms,
    }
    if user_id is not None:
        # Custom Isol8 field. Upstream openclaw's `coercePairingStateRecord`
        # (pairing-files.ts) does a permissive cast — unknown fields are
        # preserved across read/write cycles. We use this on re-pair to
        # distinguish "this user's prior stale entries" (safe to prune)
        # from "another org member's still-valid entry" (must preserve).
        entry["userId"] = user_id
    return entry


# Mutator contract: receives the current pairing-state file as a FLAT dict
# (``Record<id, Entry>`` per upstream pairing-files.ts), mutates it in place,
# and returns True iff a write is needed.
PairedMutator = Callable[[dict], bool]


class DeviceRegisteredToAnotherMember(Exception):
    """A submitted device id is already paired to a DIFFERENT member of this owner.

    The Keychain identity is per app, not per Clerk user, so a Mac handed to (or shared
    with) another member re-submits the previous member's keys. The repository's
    non-relabel condition correctly refuses the write; this names the refusal so the
    desktop can mint fresh keys and pair again instead of retrying the same collision.
    """

    code = "device_registered_to_another_member"

    def __init__(self, device_id: str, other_user_id: str):
        super().__init__(f"device {device_id} is registered to another member")
        self.device_id = device_id
        self.other_user_id = other_user_id


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
    permissions: DesktopPermissionGrant | None = None,
) -> PairResult:
    """Idempotently register the user's two devices on the owner's containers row.

    The record is ONE map (``paired_devices``) rather than the old pair of EFS
    ``paired.json`` files: the gateway that read those is gone, and the row is what
    ``machine_auth`` already verifies a device's WS-upgrade token against and what
    ``declared_commands_for`` reads its command allowlist from.

    Returns the connection material the desktop needs to reach the node relay.
    """
    grant = permissions or DesktopPermissionGrant.full()
    mac_commands = grant.mac_commands()
    if permissions is None:
        # Legacy full access was never explicit consent to the new approval handler.
        mac_commands = tuple(command for command in mac_commands if command not in _EXPLICIT_CONSENT_COMMANDS)
    cli_commands = grant.cli_commands()
    pairing_group_id = secrets.token_urlsafe(18)

    mac_entry = build_paired_entry(
        pubkey_pem=tauri_mac_pubkey,
        client_id="openclaw-macos",
        display_name=f"{display_name_base} | Lightbulb Mac",
        commands=list(mac_commands),
        user_id=user_id,
    )
    cli_entry = build_paired_entry(
        pubkey_pem=cli_pubkey,
        client_id="node-host",
        display_name=f"{display_name_base} | Lightbulb Desktop (CLI)",
        commands=list(cli_commands),
        user_id=user_id,
    )
    # One scoped, server-atomic UpdateItem per device (see
    # ``container_repo.upsert_paired_devices``): touching only that device's map key means a
    # concurrent status/hook-token writer, or a concurrent write of another device, cannot
    # clobber it or resurrect a revoked one. Its ConditionExpression is also the NON-RELABEL
    # guard -- ``deviceId = sha256(pubkey)`` and this route does not verify private-key
    # possession, so a co-tenant submitting another member's PUBLIC key must not be able to
    # relabel their device. ``publicKey`` is the base64url-raw key ``machine_auth`` verifies
    # the device's WS-upgrade signature against; ``commands`` is what
    # ``declared_commands_for`` intersects a connecting client's declared set with.
    await container_repo.upsert_paired_devices(
        owner_id,
        {
            entry["deviceId"]: {
                "publicKey": entry["publicKey"],
                "userId": user_id,
                "clientId": entry["clientId"],
                "displayName": entry["displayName"],
                "commands": list(entry["commands"]),
                "pairingGroupId": pairing_group_id,
            }
            for entry in (mac_entry, cli_entry)
        },
    )

    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    devices = row.get("paired_devices") or {}
    for device_id in (mac_entry["deviceId"], cli_entry["deviceId"]):
        holder = (devices.get(device_id) or {}).get("userId")
        if holder and holder != user_id:
            raise DeviceRegisteredToAnotherMember(device_id, holder)
    if not paired_physical_mac(row, user_id, mac_entry["deviceId"]) or not paired_physical_mac(
        row, user_id, cli_entry["deviceId"]
    ):
        raise ValueError("desktop pairing was incomplete; pair again")
    return PairResult(
        gateway_url=settings.WS_GATEWAY_URL,
        gateway_token=row.get("gateway_token") or "",
        device_id_mac=mac_entry["deviceId"],
        device_id_cli=cli_entry["deviceId"],
        commands_mac=mac_commands,
        commands_cli=cli_commands,
    )


async def revoke_desktop_device(
    *,
    device_id: str,
    owner_id: str,
    force_close_device: Callable[[str], Awaitable[bool]],
) -> bool:
    """Remove the device from the owner's ``paired_devices`` map AND force-close any live
    WS session.

    Returns True if the device existed (was removed); False if it wasn't paired. The
    ``force_close_device`` callback force-closes the device's live relay connection by
    deleting its TTL presence row -- the relay pins a device's socket to whichever backend
    instance accepted it, so there is no in-process map to pop, and the relay's own refresh
    loop notices the row is gone and closes the socket. That is the security-review HIGH fix
    for stolen-Mac scenarios, where removing the trust record alone only blocks future
    reconnects. ``force_close_device`` is always attempted, even for an unpaired id, to
    defend against a connected device whose record was wiped out-of-band.
    """
    row = await container_repo.get_by_owner_id(owner_id)
    existed = device_id in ((row or {}).get("paired_devices") or {})
    await container_repo.remove_paired_device(owner_id, device_id)
    await force_close_device(device_id)
    return existed
