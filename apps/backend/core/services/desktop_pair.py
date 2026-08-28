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
import base64
import fcntl
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from cryptography.hazmat.primitives import serialization

from core.services.efs_file_lock import (
    chown_uid_1000_best_effort,
    get_file_lock,
    open_lock_fd,
)


logger = logging.getLogger(__name__)


# In-process per-owner serialization for paired.json mutators. POSIX
# ``fcntl`` locks are process-scoped, NOT thread-scoped: two
# ``asyncio.to_thread`` mutators on the same paired.json in the same
# backend worker can both "acquire" the fcntl lock and race the
# read-modify-write, dropping each other's writes. This in-process lock
# serializes those callers before they ever reach the file lock. The fcntl
# lock STILL matters — it guards against cross-process writers (the
# in-container node self-registering during first connect runs in a
# different OS process). Shared implementation in ``efs_file_lock``.
def _get_paired_lock(owner_id: str):
    """Return the per-owner in-process write lock (both paired.json files)."""
    return get_file_lock(f"desktop_pair:{owner_id}")


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

DESKTOP_MAC_BASE_COMMANDS: tuple[str, ...] = (
    "system.notify",
    "device.info",
    "device.status",
    "device.health",
    "device.permissions",
)

DESKTOP_CLI_SHELL_COMMANDS: tuple[str, ...] = (
    "system.run",
    "system.run.prepare",
    "system.which",
)

DESKTOP_CLI_BROWSER_COMMANDS: tuple[str, ...] = ("browser.proxy",)

DESKTOP_CLI_FILE_COMMANDS: tuple[str, ...] = (
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
        caps: list[str] = []
        if self.shell:
            caps.append("system")
        if self.browser:
            caps.append("browser")
        if self.files:
            caps.append("file")
        return caps


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
    tokens.node mirror the canonical shape ensure_node_paired_entry's
    in-container counterpart writes (core/containers/config.py).
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


class WorkspaceLike(Protocol):
    """Minimal workspace surface the pair/revoke flow needs.

    ``mutate_paired_json`` is the SINGLE serialized read-modify-write
    primitive: it acquires the same advisory lock used by other writers
    (e.g. ``core.containers.config.ensure_node_paired_entry``), reads the
    current flat ``Record<deviceId, PairedDevice>`` (or {} if missing),
    invokes the mutator in place, and writes only if it returned True.
    """

    gateway_url: str

    async def mutate_paired_json(self, mutator: PairedMutator) -> None: ...
    async def mutate_node_paired_json(self, mutator: PairedMutator) -> None: ...
    async def get_gateway_token(self) -> str: ...
    async def upsert_paired_devices(self, entries: dict) -> None: ...
    async def remove_paired_device(self, device_id: str) -> None: ...


@dataclass(frozen=True)
class PairResult:
    gateway_url: str
    gateway_token: str
    device_id_mac: str
    device_id_cli: str
    commands_mac: tuple[str, ...]
    commands_cli: tuple[str, ...]


def build_node_paired_entry(
    *,
    device_entry: dict,
    caps: list[str],
    platform: str = "macos",
    device_family: str | None = None,
) -> dict:
    """Construct OpenClaw's approved ``nodes/paired.json`` entry.

    OpenClaw 2026.3.31+ has TWO gates:

    1. ``devices/paired.json`` authenticates the connecting device identity.
    2. ``nodes/paired.json`` approves the live command/capability surface.

    Isol8 already performs an authenticated Clerk-backed desktop pair flow and
    derives the command set server-side, so that pair call is our approval
    moment for first-party desktop nodes. Without this entry the device can
    connect, but OpenClaw strips live commands to ``[]`` and agents see errors
    like "node requires system.run".
    """
    now_ms = int(time.time() * 1000)
    entry = {
        "nodeId": device_entry["deviceId"],
        # Required by upstream NodePairingPairedNode. Device-auth reconnects do
        # not use this legacy pairing token, but keep the canonical shape.
        "token": secrets.token_urlsafe(32),
        "clientId": device_entry.get("clientId"),
        "clientMode": device_entry.get("clientMode", "node"),
        "displayName": device_entry.get("displayName"),
        "platform": platform,
        "caps": caps,
        "commands": list(device_entry.get("commands") or []),
        "createdAtMs": now_ms,
        "approvedAtMs": now_ms,
    }
    if device_family is not None:
        entry["deviceFamily"] = device_family
    return entry


async def pair_desktop_devices(
    *,
    user_id: str,
    owner_id: str,
    display_name_base: str,
    tauri_mac_pubkey: str,
    cli_pubkey: str,
    permissions: DesktopPermissionGrant | None = None,
    workspace: WorkspaceLike,
) -> PairResult:
    """Idempotently register the user's two devices in the owner's container paired.json.

    Returns connection material the desktop needs to spawn the openclaw subprocess
    and connect both nodes to the gateway.
    """
    grant = permissions or DesktopPermissionGrant.full()
    mac_commands = grant.mac_commands()
    cli_commands = grant.cli_commands()

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
    mac_node_entry = build_node_paired_entry(
        device_entry=mac_entry,
        caps=grant.mac_caps(),
    )
    cli_node_entry = build_node_paired_entry(
        device_entry=cli_entry,
        caps=grant.cli_caps(),
        device_family="Mac",
    )

    new_device_ids = {mac_entry["deviceId"], cli_entry["deviceId"]}
    node_approval_allowed_ids: set[str] = set()
    # Personal workspaces have owner_id == user_id (only one human owns
    # the container); org workspaces have a distinct owner_id. In the
    # personal case there's no other user who could own a legacy
    # unstamped entry, so we can safely attribute it to the caller for
    # pruning. In the org case we preserve unstamped entries (the
    # attribution is ambiguous and stamping by pubkey alone is unsafe
    # — POST /desktop/pair doesn't verify possession).
    is_personal_workspace = owner_id == user_id

    def _mutate(paired: dict) -> bool:
        # paired is a FLAT Record<deviceId, PairedDevice> per upstream's
        # device-pairing.ts (writeJson writes the inner record, not the
        # `pairedByDeviceId` wrapper).
        #
        # Two responsibilities:
        #   1. PRUNE this user's prior stale desktop entries. A previous
        #      Tauri install with a different Keychain would have
        #      registered (deviceId, pubkey) under the same clientId we
        #      use today (openclaw-macos / node-host). Those entries are
        #      orphaned — their private keys are gone with the old
        #      Keychain — and they show up as ghost nodes in node
        #      enumeration.
        #
        #      In an ORG context the workspace is shared, so paired.json
        #      can hold multiple members' desktop entries under the same
        #      clientId. We MUST NOT delete a sibling member's still-valid
        #      entry. The prune is gated on three conditions, all
        #      required:
        #        * clientId is one of ours (openclaw-macos / node-host)
        #        * the entry's userId matches the caller's user_id
        #        * the deviceId is NOT the one we're about to write
        #
        #      Entries without a userId (legacy, pre-this-PR) are
        #      conservatively left in place — we can't safely attribute
        #      them and erring on preserve is correct in the multi-tenant
        #      shared-workspace case.
        #
        #   2. UPSERT the two current desktop entries. Idempotent on
        #      publicKey match — preserves the existing entry's tokens.
        changed = False
        for did in list(paired.keys()):
            if did in new_device_ids:
                continue
            entry = paired[did]
            if entry.get("clientId") not in ALLOWED_CLIENT_IDS:
                continue
            entry_user_id = entry.get("userId")
            if entry_user_id == user_id:
                # Same user, stamped match — safe to prune.
                pass
            elif entry_user_id is None and is_personal_workspace:
                # Legacy unstamped entry in a personal workspace. No
                # other user can own it (owner_id == user_id), so the
                # attribution is unambiguous. Prune. In an org context
                # we'd preserve it (ambiguous and possibly a teammate's).
                pass
            else:
                continue
            del paired[did]
            changed = True
        for entry in (mac_entry, cli_entry):
            device_id = entry["deviceId"]
            existing = paired.get(device_id)
            if existing and existing.get("publicKey") == entry["publicKey"]:
                entry_user_id = existing.get("userId")
                if entry_user_id == user_id or (entry_user_id is None and is_personal_workspace):
                    # Same user's existing paired device (or personal legacy
                    # entry) is safe to node-approve. Do not stamp userId here;
                    # see the security note below.
                    node_approval_allowed_ids.add(device_id)
                # Idempotent: existing entry has the same key — keep its
                # tokens.
                #
                # We deliberately do NOT stamp/overwrite userId here, even
                # though the matching publicKey LOOKS like proof of
                # ownership. `POST /desktop/pair` accepts a PEM pubkey
                # without verifying possession of the corresponding
                # private key (that proof happens later in the WS
                # handshake). If Alice submitted Bob's pubkey, this
                # branch would relabel Bob's stamped entry as Alice's,
                # and Alice's next re-pair would satisfy the prune check
                # and delete Bob's still-valid device. Refusing to stamp
                # here means legacy unstamped entries stay unstamped forever
                # and aren't pruned by the userId-scoped prune above —
                # purely cosmetic ghosts. The alternative is a real
                # cross-member privilege escalation in shared org
                # workspaces. Worth the trade.
                continue
            paired[device_id] = entry
            node_approval_allowed_ids.add(device_id)
            changed = True
        return changed

    await workspace.mutate_paired_json(_mutate)

    def _mutate_node_pairing(paired_nodes: dict) -> bool:
        """Pre-approve OpenClaw's live node command surface for our devices.

        OpenClaw 2026.3.31+ requires node pairing approval in addition to
        device pairing. We only auto-approve nodes that the device-pair mutator
        proved are new/current for this caller; a caller who merely submits
        another org member's public key does not get to mutate that member's
        node approval.
        """
        changed = False
        for node_entry in (mac_node_entry, cli_node_entry):
            node_id = node_entry["nodeId"]
            if node_id not in node_approval_allowed_ids:
                continue
            existing = paired_nodes.get(node_id)
            if existing:
                # Preserve the legacy node-pair token and creation timestamp,
                # but refresh server-owned metadata/commands. This repairs old
                # entries when our derived command surface changes.
                next_entry = {
                    **node_entry,
                    "token": existing.get("token") or node_entry["token"],
                    "createdAtMs": existing.get("createdAtMs") or node_entry["createdAtMs"],
                    "approvedAtMs": existing.get("approvedAtMs") or node_entry["approvedAtMs"],
                }
                if existing == next_entry:
                    continue
                paired_nodes[node_id] = next_entry
                changed = True
                continue
            paired_nodes[node_id] = node_entry
            changed = True
        return changed

    await workspace.mutate_node_paired_json(_mutate_node_pairing)

    # Project both device pubkeys onto the owner's containers DDB row so the
    # WebSocket Lambda authorizer can point-look-up deviceId -> pubkey for
    # device-key WS-upgrade auth (the authorizer can't read the EFS paired.json).
    # publicKey is the SAME base64url-raw key the gateway string-compares, and
    # userId is the member — the authorizer derives (userId=member, orgId=owner).
    await workspace.upsert_paired_devices(
        {
            entry["deviceId"]: {
                "publicKey": entry["publicKey"],
                "userId": user_id,
                "clientId": entry["clientId"],
            }
            for entry in (mac_entry, cli_entry)
        }
    )

    return PairResult(
        gateway_url=workspace.gateway_url,
        gateway_token=await workspace.get_gateway_token(),
        device_id_mac=mac_entry["deviceId"],
        device_id_cli=cli_entry["deviceId"],
        commands_mac=mac_commands,
        commands_cli=cli_commands,
    )


async def revoke_desktop_device(
    *,
    device_id: str,
    workspace: WorkspaceLike,
    force_close_device: Callable[[str], Awaitable[bool]],
) -> bool:
    """Remove the device from paired.json AND force-close any live WS session.

    Returns True if the device existed (was removed); False if it wasn't paired.
    The force_close_device callback terminates the live API Gateway WS connection
    keyed by deviceId — this is the security-review HIGH fix for stolen-Mac
    scenarios where paired.json removal alone only blocks future reconnects.
    """
    removed = {"value": False}

    def _mutate(paired: dict) -> bool:
        # Flat record: deviceId is a top-level key.
        if device_id not in paired:
            return False
        del paired[device_id]
        removed["value"] = True
        return True

    await workspace.mutate_paired_json(_mutate)
    await workspace.mutate_node_paired_json(_mutate)
    # Remove the authorizer-reachable projection too, or a revoked device could
    # keep self-minting valid WS-upgrade tokens against a surviving pubkey row.
    await workspace.remove_paired_device(device_id)
    # Always attempt force_close — defends against a connected device whose
    # paired entry has already been wiped out-of-band.
    await force_close_device(device_id)
    return removed["value"]


# ---------------------------------------------------------------------------
# EFS adapter: file-locked RMW against <EFS>/users/{owner_id}/devices/paired.json
# ---------------------------------------------------------------------------


@dataclass
class EfsWorkspaceAdapter:
    """Concrete WorkspaceLike implementation against the real EFS workspace
    + container repo.

    - paired.json lives at ``<EFS>/users/{owner_id}/devices/paired.json`` —
      this is the path openclaw's ``resolvePairingPaths`` resolves
      (``pairing-files.ts``) and the gateway reads on container start.
      Storing at ``<owner>/paired.json`` would be invisible to the gateway.
    - On disk the file is a FLAT ``Record<deviceId, PairedDevice>`` —
      upstream's ``writeJson(pairedPath, state.pairedByDeviceId)`` persists
      only the inner record (``device-pairing.ts``). The
      ``pairedByDeviceId`` wrapper exists ONLY as an in-memory type and
      MUST NOT be written.
    - The gateway_token is the per-container token minted by ECS provisioning
      and stored on the containers DDB row.

    Concurrency: ``mutate_paired_json`` takes an exclusive ``fcntl.lockf``
    on ``<owner>/devices/paired.json.lock`` for the FULL read-modify-write
    window. Matches the canonical pattern in ``core/services/config_patcher.py``
    (``_locked_rmw`` / ``append_cron_jobs``) so cross-process writers — the
    in-container node re-registering itself during first connect, the
    desktop pair flow, the admin update workers — serialize on the same lock
    and never clobber each other's writes.

    Why a separate ``.lock`` file (instead of locking ``paired.json``
    directly): ``fcntl.lockf`` is keyed on the file inode, but our write
    path swaps the inode out via ``os.rename(tmp, paired.json)``. A second
    writer locking the (now-stale) original inode would race the rename.
    A dedicated lock file is never renamed, so concurrent writers always
    contend on the same inode.
    """

    owner_id: str
    workspace: object  # core.containers.workspace.Workspace
    container_repo: object  # core.repositories.container_repo (module)
    gateway_ws_url: str

    @property
    def gateway_url(self) -> str:
        return self.gateway_ws_url

    def _paths(self) -> tuple[str, str, str, str]:
        """Resolve ``(devices_dir, paired_path, lock_path, owner_dir)``."""
        owner_dir = str(self.workspace.user_path(self.owner_id))
        devices_dir = os.path.join(owner_dir, "devices")
        paired_path = os.path.join(devices_dir, "paired.json")
        lock_path = os.path.join(devices_dir, "paired.json.lock")
        return devices_dir, paired_path, lock_path, owner_dir

    def _pairing_paths(self, subdir: str, filename: str) -> tuple[str, str, str]:
        owner_dir = str(self.workspace.user_path(self.owner_id))
        target_dir = os.path.join(owner_dir, subdir)
        json_path = os.path.join(target_dir, filename)
        lock_path = os.path.join(target_dir, f"{filename}.lock")
        return target_dir, json_path, lock_path

    async def mutate_paired_json(self, mutator: PairedMutator) -> None:
        """Locked read-modify-write of ``devices/paired.json``.

        The mutator runs synchronously inside the lock — keep it cheap.
        If it returns False, no write occurs (idempotent path) and no
        directories are created. This is what makes "revoke against a
        never-paired owner" safe to call on hosts where the EFS mount
        path is read-only or absent (e.g. unit tests on macOS where the
        default ``/mnt/efs/users`` doesn't exist).
        """
        devices_dir, paired_path, lock_path, _ = self._paths()
        await self._mutate_pairing_json(
            target_dir=devices_dir,
            json_path=paired_path,
            lock_path=lock_path,
            log_label="devices/paired.json",
            mutator=mutator,
        )

    async def mutate_node_paired_json(self, mutator: PairedMutator) -> None:
        """Locked read-modify-write of ``nodes/paired.json``.

        OpenClaw stores device identity trust in ``devices/paired.json`` and
        node command-surface approval in ``nodes/paired.json``. Desktop pair
        must write both, using the same lock/write discipline.
        """
        target_dir, json_path, lock_path = self._pairing_paths("nodes", "paired.json")
        await self._mutate_pairing_json(
            target_dir=target_dir,
            json_path=json_path,
            lock_path=lock_path,
            log_label="nodes/paired.json",
            mutator=mutator,
        )

    async def _mutate_pairing_json(
        self,
        *,
        target_dir: str,
        json_path: str,
        lock_path: str,
        log_label: str,
        mutator: PairedMutator,
    ) -> None:
        """Locked read-modify-write of one OpenClaw flat pairing file."""
        # Outermost: per-owner threading.Lock guards in-process callers.
        # Innermost (acquired inside _do_rmw): fcntl.lockf guards
        # cross-process callers (in-container node self-registration).
        # See _get_paired_lock / efs_file_lock for the two-layer rationale.
        in_process_lock = _get_paired_lock(self.owner_id)

        def _do_rmw():
            current: dict = {}
            paired_exists = os.path.exists(json_path)

            lock_fd = None
            try:
                if paired_exists:
                    # Acquire the cross-process lock BEFORE reading so we
                    # see a consistent view across concurrent writers
                    # (in-container node self-registration, admin
                    # workers, /desktop/pair).
                    lock_fd = self._open_lock_fd(target_dir, lock_path)
                    fcntl.lockf(lock_fd, fcntl.LOCK_EX)
                    try:
                        with open(json_path, "r") as f:
                            loaded = json.load(f)
                        if isinstance(loaded, dict):
                            current = loaded
                        else:
                            logger.warning(
                                "%s for owner %s was not a dict (got %s); resetting",
                                log_label,
                                self.owner_id,
                                type(loaded).__name__,
                            )
                    except (OSError, ValueError) as exc:
                        logger.warning(
                            "Failed to parse %s for owner %s (%s); resetting",
                            log_label,
                            self.owner_id,
                            exc,
                        )

                changed = mutator(current)
                if not changed:
                    return

                # Phase 2 — write. If paired.json didn't exist we haven't
                # opened the lock yet; create devices/ first, then lock.
                if lock_fd is None:
                    os.makedirs(target_dir, exist_ok=True)
                    self._chown_uid_1000_best_effort(target_dir)
                    lock_fd = self._open_lock_fd(target_dir, lock_path)
                    fcntl.lockf(lock_fd, fcntl.LOCK_EX)

                # Validate serializability before touching disk.
                serialized = json.dumps(current, indent=2)

                fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as f:
                        f.write(serialized)
                    self._chown_uid_1000_best_effort(tmp_path)
                    os.rename(tmp_path, json_path)
                except Exception:
                    if os.path.exists(tmp_path):
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                    raise

                logger.info(
                    "Wrote %s for owner %s (%d entries)",
                    log_label,
                    self.owner_id,
                    len(current),
                )
            finally:
                if lock_fd is not None:
                    fcntl.lockf(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()

        def _do_rmw_locked():
            # Acquire the per-owner threading lock first; this is the
            # in-process gate. Inside, _do_rmw takes the cross-process
            # fcntl lock. Two layers because POSIX fcntl is process-
            # scoped — without this gate, threads in the same backend
            # worker would race the read-modify-write.
            with in_process_lock:
                _do_rmw()

        await asyncio.to_thread(_do_rmw_locked)

    def _open_lock_fd(self, devices_dir: str, lock_path: str):
        """Open the dedicated ``.lock`` file in r+ mode, creating it if missing.

        A separate ``.lock`` file (not ``paired.json`` itself) is required
        because the write path swaps paired.json's inode via ``os.rename`` — a
        lock held on the old inode wouldn't protect against a new writer racing
        the rename. Shared implementation in ``efs_file_lock``.
        """
        return open_lock_fd(devices_dir, lock_path)

    def _chown_uid_1000_best_effort(self, path: str) -> None:
        """Match OpenClaw container ownership (node uid=1000 gid=1000).

        Shared implementation in ``efs_file_lock``.
        """
        chown_uid_1000_best_effort(path)

    async def get_gateway_token(self) -> str:
        container = await self.container_repo.get_by_owner_id(self.owner_id)
        if not container or not container.get("gateway_token"):
            raise RuntimeError(f"No gateway_token for owner {self.owner_id}")
        return container["gateway_token"]

    async def upsert_paired_devices(self, entries: dict) -> None:
        await self.container_repo.upsert_paired_devices(self.owner_id, entries)

    async def remove_paired_device(self, device_id: str) -> None:
        await self.container_repo.remove_paired_device(self.owner_id, device_id)
