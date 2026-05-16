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
import fcntl
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from cryptography.hazmat.primitives import serialization


logger = logging.getLogger(__name__)


# In-process per-owner serialization for paired.json mutators. POSIX
# ``fcntl`` locks are process-scoped, NOT thread-scoped: two
# ``asyncio.to_thread`` mutators on the same paired.json in the same
# backend worker can both "acquire" the fcntl lock and race the
# read-modify-write, dropping each other's writes. The per-owner
# ``threading.Lock`` here serializes those in-process callers before
# they ever reach the file lock. The fcntl lock STILL matters — it
# guards against cross-process writers (the in-container node
# self-registering during first connect runs in a different OS process).
_paired_locks: dict[str, threading.Lock] = {}
_paired_locks_guard = threading.Lock()


def _get_paired_lock(owner_id: str) -> threading.Lock:
    """Return (and lazily create) the per-owner write lock.

    The guard mutex is contended only on first-time lookup; steady-state
    callers acquire the inner per-owner lock directly.
    """
    with _paired_locks_guard:
        lock = _paired_locks.get(owner_id)
        if lock is None:
            lock = threading.Lock()
            _paired_locks[owner_id] = lock
        return lock


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


# Mutator contract: receives the current paired.json state as a FLAT dict
# (``Record<deviceId, PairedDevice>`` per upstream pairing-files.ts), mutates
# it in place, and returns True iff a write is needed.
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

    def _mutate(paired: dict) -> bool:
        # paired is a FLAT Record<deviceId, PairedDevice> per upstream's
        # device-pairing.ts:181 (writeJson writes the inner record, not the
        # `pairedByDeviceId` wrapper). Any prior operator / node / Mac entry
        # from ecs_manager or a previous pair call lives at the top level —
        # leave it alone unless its publicKey matches what we'd write.
        changed = False
        for entry in (mac_entry, cli_entry):
            device_id = entry["deviceId"]
            existing = paired.get(device_id)
            if existing and existing.get("publicKey") == entry["publicKey"]:
                # Idempotent: existing entry has the same key — keep its tokens.
                continue
            paired[device_id] = entry
            changed = True
        return changed

    await workspace.mutate_paired_json(_mutate)

    return PairResult(
        gateway_url=workspace.gateway_url,
        gateway_token=await workspace.get_gateway_token(),
        device_id_mac=mac_entry["deviceId"],
        device_id_cli=cli_entry["deviceId"],
        commands_mac=DESKTOP_MAC_COMMANDS,
        commands_cli=DESKTOP_CLI_COMMANDS,
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
      (``pairing-files.ts:6-13``) and the gateway reads on container start.
      Storing at ``<owner>/paired.json`` would be invisible to the gateway.
    - On disk the file is a FLAT ``Record<deviceId, PairedDevice>`` —
      upstream's ``writeJson(pairedPath, state.pairedByDeviceId)`` persists
      only the inner record (``device-pairing.ts:181,186``). The
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
        # Outermost: per-owner threading.Lock guards in-process callers.
        # Innermost (acquired inside _do_rmw): fcntl.lockf guards
        # cross-process callers (in-container node self-registration).
        # See _paired_locks docstring at the top of this module.
        in_process_lock = _get_paired_lock(self.owner_id)

        def _do_rmw():
            current: dict = {}
            paired_exists = os.path.exists(paired_path)

            lock_fd = None
            try:
                if paired_exists:
                    # Acquire the cross-process lock BEFORE reading so we
                    # see a consistent view across concurrent writers
                    # (in-container node self-registration, admin
                    # workers, /desktop/pair).
                    lock_fd = self._open_lock_fd(devices_dir, lock_path)
                    fcntl.lockf(lock_fd, fcntl.LOCK_EX)
                    try:
                        with open(paired_path, "r") as f:
                            loaded = json.load(f)
                        if isinstance(loaded, dict):
                            current = loaded
                        else:
                            logger.warning(
                                "paired.json for owner %s was not a dict (got %s); resetting",
                                self.owner_id,
                                type(loaded).__name__,
                            )
                    except (OSError, ValueError) as exc:
                        logger.warning(
                            "Failed to parse paired.json for owner %s (%s); resetting",
                            self.owner_id,
                            exc,
                        )

                changed = mutator(current)
                if not changed:
                    return

                # Phase 2 — write. If paired.json didn't exist we haven't
                # opened the lock yet; create devices/ first, then lock.
                if lock_fd is None:
                    os.makedirs(devices_dir, exist_ok=True)
                    self._chown_uid_1000_best_effort(devices_dir)
                    lock_fd = self._open_lock_fd(devices_dir, lock_path)
                    fcntl.lockf(lock_fd, fcntl.LOCK_EX)

                # Validate serializability before touching disk.
                serialized = json.dumps(current, indent=2)

                fd, tmp_path = tempfile.mkstemp(dir=devices_dir, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as f:
                        f.write(serialized)
                    self._chown_uid_1000_best_effort(tmp_path)
                    os.rename(tmp_path, paired_path)
                except Exception:
                    if os.path.exists(tmp_path):
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                    raise

                logger.info(
                    "Wrote paired.json for owner %s (%d entries)",
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

        A separate ``.lock`` file (not ``paired.json`` itself) is
        required because the write path swaps paired.json's inode via
        ``os.rename`` — a lock held on the old inode wouldn't protect
        against a new writer racing the rename.
        """
        if not os.path.exists(lock_path):
            os.makedirs(devices_dir, exist_ok=True)
            self._chown_uid_1000_best_effort(devices_dir)
            # ``open(..., "a")`` creates the file if missing without
            # truncating concurrent locks.
            with open(lock_path, "a"):
                pass
            self._chown_uid_1000_best_effort(lock_path)
        return open(lock_path, "r+")

    def _chown_uid_1000_best_effort(self, path: str) -> None:
        """Match OpenClaw container ownership (node uid=1000 gid=1000).

        Only chown if running as root (backend container runs as root;
        tests run as the local user where chown(other) is denied). Mirrors
        ``config_patcher._locked_rmw``.
        """
        try:
            if os.getuid() == 0:
                os.chown(path, 1000, 1000)
        except OSError:
            pass

    async def get_gateway_token(self) -> str:
        container = await self.container_repo.get_by_owner_id(self.owner_id)
        if not container or not container.get("gateway_token"):
            raise RuntimeError(f"No gateway_token for owner {self.owner_id}")
        return container["gateway_token"]
