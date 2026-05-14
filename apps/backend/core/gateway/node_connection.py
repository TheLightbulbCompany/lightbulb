"""
Dedicated per-user upstream WebSocket for node connections.

The OpenClaw gateway enforces one-role-per-connection, so node traffic
cannot share the operator connection used by GatewayConnectionPool.
This class manages a separate WebSocket to the container that connects
with role:"node", enabling the gateway's NodeRegistry to register the
node and route node.invoke requests to the user's Mac.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid

from websockets import connect as ws_connect

logger = logging.getLogger(__name__)

GATEWAY_PORT = 18789

# Per-member device key layout on EFS:
#   <efs>/<owner_id>/devices/<user_id>/.node-device-key.pem
# One Ed25519 key per MEMBER (user_id), under the container's tree
# (owner_id). Multiple members of the same org get distinct keys → distinct
# device.id values → the NodeRegistry can tell Alice's Mac from Bob's Mac.
# On first connect we lazily generate the key AND idempotently register
# its device_id in paired.json (without the paired.json entry OpenClaw's
# connect handler rejects the handshake with NOT_PAIRED).
_DEVICE_KEY_FILENAME = ".node-device-key.pem"
_DEVICE_KEY_SUBDIR = "devices"


def _base64url_encode(data: bytes) -> str:
    """RFC 7515 base64url encoding (no padding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_private_key(pem: str):
    """Load an Ed25519 private key from PEM string."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    return load_pem_private_key(pem.encode("ascii"), password=None)


def _generate_member_device_key(key_path, user_id: str) -> None:
    """Create a fresh Ed25519 key at ``key_path`` with 0600 perms.

    Write-through-tempfile-then-os.link so the target path only appears
    on disk when fully written. The previous O_EXCL approach created the
    file empty (O_CREAT on first call), then wrote content afterward —
    a concurrent ``_load_node_key`` on another asyncio task could see
    ``exists()==True`` after O_CREAT but before the write completed, and
    read a zero-byte file → PEM parse failure.

    ``os.link`` gives atomic create-if-not-exists: the first writer's tmp
    gets hard-linked into the target path fully-populated, later writers
    get FileExistsError and silently discard their tmp. Readers never see
    a partially-written file.
    """
    import pathlib
    import tempfile

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key_path = pathlib.Path(key_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: if someone else already wrote it, we're done.
    if key_path.exists():
        return

    new_key = Ed25519PrivateKey.generate()
    pem_bytes = new_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())

    fd, tmp_path = tempfile.mkstemp(dir=str(key_path.parent), suffix=".keytmp", prefix=".node-key-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem_bytes)
        os.chmod(tmp_path, 0o600)
        try:
            # Atomic create-if-not-exists. Unlike os.rename, os.link fails
            # if the destination exists, so two concurrent writers don't
            # clobber each other — one wins, the other falls through.
            os.link(tmp_path, str(key_path))
            logger.info("Generated per-member node device key for user %s", user_id)
        except FileExistsError:
            logger.debug(
                "Concurrent key generation for %s; another writer won",
                user_id,
            )
    finally:
        # tmp is a hard-linked inode after successful os.link; unlinking tmp
        # removes only that name, leaving the target intact. If os.link
        # failed (FileExistsError), this just cleans up our unused tmp.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _build_device_identity(private_key, nonce: str, connect_params: dict) -> dict:
    """Build a signed device identity block for the connect handshake.

    Uses the v2 pipe-delimited signing format:
      v2|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{token}|{nonce}
    """
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw_pub = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    device_id = hashlib.sha256(raw_pub).hexdigest()
    signed_at = int(time.time() * 1000)

    client = connect_params.get("client", {})
    client_id = client.get("id", "gateway-client")
    client_mode = client.get("mode", "node")
    role = connect_params.get("role", "node")
    scopes = ",".join(sorted(connect_params.get("scopes", [])))
    token = connect_params.get("auth", {}).get("token", "")

    payload = f"v2|{device_id}|{client_id}|{client_mode}|{role}|{scopes}|{signed_at}|{token}|{nonce}"
    signature = private_key.sign(payload.encode("utf-8"))

    return {
        "id": device_id,
        "publicKey": _base64url_encode(raw_pub),
        "signature": _base64url_encode(signature),
        "signedAt": signed_at,
        "nonce": nonce,
    }


class NodeUpstreamConnection:
    """Manages a single upstream WebSocket connection for a node."""

    def __init__(
        self,
        user_id: str,
        owner_id: str,
        container_ip: str,
        node_connect_params: dict,
        efs_mount_path: str,
        gateway_token: str,
    ):
        # user_id is the MEMBER (Alice, Bob...) whose Mac this is.
        # owner_id is the org/container owner — who owns the EFS tree and
        # the Fargate task. They're equal in solo mode, different in
        # multi-member orgs.
        self.user_id = user_id
        self.owner_id = owner_id
        self.container_ip = container_ip
        self.node_connect_params = node_connect_params
        self.efs_mount_path = efs_mount_path
        self.gateway_token = gateway_token
        self.device_id: str | None = None
        self._ws = None
        self._connected = False
        self._reader_task: asyncio.Task | None = None
        self._on_message = None
        self._on_upstream_closed = None

    def _device_key_path(self):
        import pathlib

        return (
            pathlib.Path(self.efs_mount_path) / self.owner_id / _DEVICE_KEY_SUBDIR / self.user_id / _DEVICE_KEY_FILENAME
        )

    def _load_node_key(self):
        """Read (or lazily generate) the per-member Ed25519 private key,
        AND idempotently register its device_id in paired.json.

        Both steps are safe to re-run on every connect: key generation uses
        O_EXCL so only one writer wins, and paired.json registration no-ops
        if the entry already exists. Running both every time means a
        partial-failure state (key generated but paired.json update failed)
        self-heals on the next connect.
        """
        import pathlib

        from core.containers.config import ensure_node_paired_entry
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        key_path = self._device_key_path()
        if not key_path.exists():
            _generate_member_device_key(key_path, self.user_id)

        pem = pathlib.Path(key_path).read_text(encoding="ascii")
        private_key = _load_private_key(pem)

        # Re-tighten perms if EFS flattened them.
        try:
            mode = os.stat(key_path).st_mode & 0o777
            if mode & 0o077:
                os.chmod(key_path, 0o600)
        except OSError:
            pass

        # Register (or verify registration of) this device_id in paired.json.
        # Idempotent — no-op if the entry already matches what the
        # desktop just declared. This is what gets OpenClaw's connect
        # handler past the NOT_PAIRED check for new per-member keys AND
        # past 2026.5.7's role-upgrade / command-upgrade gates.
        #
        # Forward the desktop's declared commands list so the paired
        # record stays in sync with what the Rust client supports.
        # Without this, reconcileNodePairingOnConnect compares the
        # device's declared commands against an empty paired.commands
        # field, decides every command is an "upgrade", creates a
        # pending pairing request, and zeros out effectiveCommands in
        # the node registry — making `exec host=node` fail with
        # "node doesn't support system.run".
        raw_pub = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        device_id = hashlib.sha256(raw_pub).hexdigest()
        public_key_b64 = _base64url_encode(raw_pub)
        declared_commands = self.node_connect_params.get("commands") or []
        if not isinstance(declared_commands, list):
            declared_commands = []
        ensure_node_paired_entry(
            efs_mount_path=self.efs_mount_path,
            owner_id=self.owner_id,
            device_id=device_id,
            public_key_b64=public_key_b64,
            declared_commands=declared_commands,
        )

        return private_key

    async def connect(self) -> dict:
        """Open upstream WS, complete handshake with role:node. Returns hello-ok."""
        # Load (or lazily generate) per-member device key + register in
        # paired.json. Runs in a thread because the paired.json update
        # takes an fcntl.lockf and would otherwise block the event loop.
        private_key = await asyncio.to_thread(self._load_node_key)
        logger.info("Loaded node device key for user %s", self.user_id)

        uri = f"ws://{self.container_ip}:{GATEWAY_PORT}"
        self._ws = await ws_connect(
            uri,
            open_timeout=10,
            close_timeout=5,
        )

        # Step 1: receive connect.challenge
        raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
        challenge = json.loads(raw)
        if challenge.get("event") != "connect.challenge":
            raise RuntimeError(f"Expected connect.challenge, got {challenge}")

        # Step 2: extract nonce and sign with persistent device key.
        #
        # Security-critical: the v2 device-signature payload bakes in
        # connect_params.auth.token (see _build_device_identity). The Rust
        # desktop client sends auth:{} — if we signed BEFORE overwriting, the
        # signature would be computed with an empty token while the wire
        # message carried the real token, and the container's re-verification
        # would reject the handshake. So we set the real token into params
        # FIRST, then sign, then forward the same params upstream.
        payload = challenge.get("payload", challenge)
        nonce = payload.get("nonce", challenge.get("nonce", ""))
        self.node_connect_params["auth"] = {"token": self.gateway_token}
        device = _build_device_identity(private_key, nonce, self.node_connect_params)
        self.device_id = device["id"]  # SHA-256 hex of Ed25519 public key

        # Step 3: send connect with role:node + device identity. auth already
        # carries the real token from the mutation above.
        req_id = str(uuid.uuid4())
        connect_msg = {
            "type": "req",
            "id": req_id,
            "method": "connect",
            "params": {
                "minProtocol": 3,
                "maxProtocol": 3,
                **self.node_connect_params,
                "device": device,
            },
        }
        await self._ws.send(json.dumps(connect_msg))

        # Step 4: receive hello-ok (skip any intermediate events like tick)
        for _ in range(20):
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") == "res":
                if not msg.get("ok"):
                    error_detail = json.dumps(msg.get("error", msg), default=str)[:500]
                    raise RuntimeError(f"Node handshake failed: {error_detail}")
                break
            logger.debug("Skipping pre-handshake message: %s", msg.get("type"))
        else:
            raise RuntimeError("Node handshake: too many non-res messages before hello-ok")

        self._connected = True
        logger.info("Node upstream connected for user %s", self.user_id)
        return msg

    def set_message_callback(self, callback) -> None:
        """Set callback for messages from upstream (container -> node)."""
        self._on_message = callback

    def set_on_upstream_closed(self, callback) -> None:
        """Set a callback invoked when the upstream WS dies independently of
        the desktop side (e.g. container restart, transient network drop).

        Without this, node_proxy state (_user_nodes / _node_count / patched
        sessions) remains populated as long as the desktop is still
        connected, and the agent keeps binding sessions to a dead nodeId.
        """
        self._on_upstream_closed = callback

    async def start_reader(self) -> None:
        """Start reading from upstream and forwarding to callback."""
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        try:
            async for raw in self._ws:
                if self._on_message:
                    data = json.loads(raw)
                    await self._on_message(data)
        except Exception as e:
            logger.warning("Node upstream reader error for %s: %s", self.user_id, e)
        finally:
            self._connected = False
            # Notify node_proxy that the upstream died so it can clear
            # per-user routing state even if the desktop side is still
            # connected. Swallow callback errors — we're already shutting
            # down this reader, failing here would just hide the
            # original disconnect cause from the log above.
            if self._on_upstream_closed:
                try:
                    await self._on_upstream_closed()
                except Exception as e:
                    logger.warning(
                        "on_upstream_closed callback raised for %s: %s",
                        self.user_id,
                        e,
                    )

    async def relay_to_upstream(self, message: dict) -> None:
        """Forward a message from the node client to the container."""
        if self._ws and self._connected:
            await self._ws.send(json.dumps(message))

    async def close(self) -> None:
        self._connected = False
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws:
            await self._ws.close()
        logger.info("Node upstream closed for user %s", self.user_id)
