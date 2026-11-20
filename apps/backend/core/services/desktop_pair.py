"""Desktop pair / revoke business logic for the two-node architecture.

The desktop ships two openclaw nodes per Mac:
  - Tauri-mac process (clientId="openclaw-macos"): macOS-native commands
  - openclaw CLI subprocess (clientId="node-host"): cross-platform commands

Command sets MUST be disjoint so that openclaw's gateway routing is
unambiguous and a single-process compromise can't pivot to the other's
command surface. Backend derives these sets here — clients do NOT submit
them on /desktop/pair.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable


from core.repositories import container_repo

logger = logging.getLogger(__name__)


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
# on at pairing. The CLI
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


def granted_commands(devices: dict, entry: dict) -> set[str]:
    """A paired device's granted commands; a paired Mac also gets ``browser.wipe.v1``, derived so
    a Mac paired before it existed needs no re-pair."""
    granted = set(entry.get("commands") or [])
    if entry.get("clientId") == "openclaw-macos" and entry.get("pairingGroupId"):
        granted.add("browser.wipe.v1")
    return granted


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


_revocations: set = set()


def revoke_member_soon(owner: str, member_id: str, still_current=None) -> None:
    """A removed member's paired devices, in the background (the webhook never waits): mark every
    device they paired to this workspace REVOKING (refused from then on), then unpair and
    disconnect them.

    Every device write is conditional on the entry still being this member's (and, for the
    removal, still REVOKING), so a re-add and re-pair meanwhile survives. ``still_current``
    (async, True while this delete is still the member's lifecycle epoch) is checked before
    touching anything: a superseded delete does nothing."""

    async def current() -> bool:
        if still_current is None:
            return True
        try:
            if await still_current():
                return True
            logger.info("device revocation superseded owner=%s", owner)
        except Exception as exc:  # noqa: BLE001 -- unknown: stop; REVOKING is not set, nothing changed
            logger.warning("device revocation lifecycle unreadable owner=%s: %s", owner, type(exc).__name__)
        return False

    async def run() -> None:
        from core.services.connection_service import ConnectionService  # it imports this module

        if not await current():
            return
        try:
            row = await container_repo.get_by_owner_id(owner, consistent=True) or {}
            devices = [
                device_id
                for device_id, entry in (row.get("paired_devices") or {}).items()
                if isinstance(entry, dict)
                and entry.get("userId") == member_id
                and await container_repo.mark_paired_device_revoking(owner, device_id, user_id=member_id)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("device revocation could not list devices owner=%s: %s", owner, type(exc).__name__)
            return
        for device_id in devices:
            try:
                if await container_repo.remove_revoked_paired_device(owner, device_id, member_id):
                    await ConnectionService().delete_relay_rows_for_device(owner, device_id)
            except Exception as exc:  # noqa: BLE001 -- REVOKING already refuses it
                logger.warning("device pairing not removed owner=%s: %s", owner, type(exc).__name__)

    task = asyncio.create_task(run())
    _revocations.add(task)
    task.add_done_callback(_revocations.discard)
