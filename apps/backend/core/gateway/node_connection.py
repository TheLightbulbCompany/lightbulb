"""
Dedicated per-user upstream WebSocket for node connections -- PURE RELAY.

The backend does not hold device private keys. Clients (Tauri Rust +
the openclaw subprocess) sign locally using keys stored in macOS Keychain.

The relay's role is strictly protocol-level. There is no proxy-injected
wake-up frame; the backend starts the handshake itself on `$connect` for
node-host clientIds (see `ws_connect`):

  1. `handle_node_connect` constructs this object and calls `connect()`
     immediately after `$connect` for any node-host WS.
  2. This class opens upstream WSS to the user's container with role:"node".
  3. Container sends `connect.challenge` (nonce).
  4. We forward the challenge DOWN to the client via the push callback
     (Management API into the client's WS connection).
  5. Client signs locally with its Keychain key, sends signed `connect` back
     up the WS. The relay's $default handler routes that frame here via
     `submit_signed_connect`. We capture `signed_req_id` so the eventual
     `hello-ok` can be returned to the client keyed to the client's own
     pending request id (not some proxy-generated wake-up id, which used
     to silently break JSON-RPC correlation in the subprocess).
  6. We forward the signed connect upstream.
  7. Container returns `hello-ok` upstream; we return it so handle_node_connect
     can forward to the client (rewriting res.id to `signed_req_id`).

After handshake, steady-state relay: upstream -> client via push callback
(`start_reader`), client -> upstream via `relay_to_upstream`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from websockets import connect as ws_connect

logger = logging.getLogger(__name__)

GATEWAY_PORT = 18789

# Timeouts for each phase of the handshake. The "wait for signed connect"
# timeout is the round-trip budget: backend pushes challenge to client (~50ms
# Management API), Tauri proxy forwards to subprocess, subprocess signs
# (Ed25519 is microseconds), reply traverses the same path back. Real-world
# we expect ~300ms; 10s gives generous headroom for cold paths / paused
# subprocesses without hanging $default forever.
_UPSTREAM_RECV_TIMEOUT = 10.0
_SIGNED_CONNECT_TIMEOUT = 10.0


class NodeUpstreamConnection:
    """Pure protocol relay for one per-user node upstream connection."""

    def __init__(
        self,
        *,
        user_id: str,
        owner_id: str,
        container_ip: str,
        node_connect_params: dict,
        gateway_token: str,
    ):
        # user_id is the MEMBER (Alice, Bob...) whose Mac this is.
        # owner_id is the org/container owner -- who owns the EFS tree and
        # the Fargate task. They're equal in solo mode, different in
        # multi-member orgs.
        self.user_id = user_id
        self.owner_id = owner_id
        self.container_ip = container_ip
        self.node_connect_params = node_connect_params
        self.gateway_token = gateway_token
        # device_id is set from the SIGNED connect's params.device.id once
        # the client returns it. The backend no longer derives it from a
        # locally-held private key.
        self.device_id: str | None = None
        # signed_req_id is the JSON-RPC `id` from the client's signed
        # `connect` request. handle_node_connect uses this to key the
        # `hello-ok` it forwards DOWN so the subprocess's pending-request
        # map can match the response. Without this, hello-ok would be
        # keyed to a wake-up id and the subprocess would silently time out.
        self.signed_req_id: str | None = None
        self._ws = None
        self._connected = False
        self._reader_task: asyncio.Task | None = None
        # Push callback: deliver a frame DOWN to the client via Management API.
        # Set by handle_node_connect before connect() is called.
        self._client_push_callback: Callable[[dict], Awaitable[None]] | None = None
        # Future awaited by connect() while the client signs the challenge.
        # Resolved by submit_signed_connect when the $default handler routes
        # the client's signed reply here.
        self._signed_connect_future: asyncio.Future | None = None
        self._on_upstream_closed: Callable[[], Awaitable[None]] | None = None

    # ---------------------------------------------------------------- setters

    def set_client_push_callback(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        """Register the function used to deliver upstream frames to the client.

        The callback typically wraps `management_api.send_message(connection_id, ...)`.
        """
        self._client_push_callback = callback

    def set_on_upstream_closed(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback invoked when the upstream WS dies independently
        of the desktop side (e.g. container restart, transient network drop).

        Without this, node_proxy state (_user_nodes / _node_count / patched
        sessions) remains populated as long as the desktop is still
        connected, and the agent keeps binding sessions to a dead nodeId.
        """
        self._on_upstream_closed = callback

    # -------------------------------------------------------- relay callback

    def submit_signed_connect(self, signed_payload: dict) -> None:
        """Called by the relay's $default handler when the client's signed
        connect arrives. Resolves the future awaited inside connect().

        Safe to call:
          - before connect() has allocated the future (no-op),
          - after the future is already resolved (no-op).
        """
        fut = self._signed_connect_future
        if fut is None:
            logger.debug(
                "submit_signed_connect for %s with no pending future; dropping",
                self.user_id,
            )
            return
        if fut.done():
            logger.debug(
                "submit_signed_connect for %s but future already done; dropping",
                self.user_id,
            )
            return
        fut.set_result(signed_payload)

    # --------------------------------------------------------------- connect

    async def connect(self) -> dict:
        """Open upstream WS, run the relay handshake, return the hello-ok dict.

        Raises RuntimeError on any failure (unexpected upstream frame, client
        timeout, upstream rejection of the signed connect).
        """
        if self._client_push_callback is None:
            raise RuntimeError("set_client_push_callback must be called before connect()")

        uri = f"ws://{self.container_ip}:{GATEWAY_PORT}"
        self._ws = await ws_connect(uri, open_timeout=10, close_timeout=5)

        # Step 1: read connect.challenge from upstream.
        raw = await asyncio.wait_for(self._ws.recv(), timeout=_UPSTREAM_RECV_TIMEOUT)
        challenge = json.loads(raw)
        if challenge.get("event") != "connect.challenge":
            raise RuntimeError(f"Expected connect.challenge, got {challenge}")

        # Step 2: allocate the signed-connect future BEFORE pushing the
        # challenge. The push can wake a fast local client whose signed
        # reply traverses submit_signed_connect synchronously inside the
        # push callback (or on the very next event-loop tick) — if the
        # future doesn't exist yet, submit_signed_connect logs "no
        # pending future" and drops the response, and connect() then
        # hangs the full _SIGNED_CONNECT_TIMEOUT. Allocating first
        # closes that race deterministically. Use get_running_loop()
        # (not the deprecated get_event_loop()) so we attach to the
        # loop actually driving this coroutine.
        self._signed_connect_future = asyncio.get_running_loop().create_future()

        # Step 3: forward the challenge DOWN to the client. The push happens
        # well past the ~37ms GoneException race window because the inbound
        # wake-up `connect` frame already flowed through Management API
        # (~150-200ms elapsed since $connect).
        await self._client_push_callback(challenge)

        # Step 4: wait for the client's signed reply to land via
        # submit_signed_connect.
        try:
            signed = await asyncio.wait_for(self._signed_connect_future, timeout=_SIGNED_CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"client did not return signed connect within {_SIGNED_CONNECT_TIMEOUT}s") from exc

        # Capture the client's request id BEFORE forwarding upstream so
        # handle_node_connect can rewrite the eventual hello-ok to match
        # the subprocess's pending request. The upstream container
        # generates its own ids for its own RPC bookkeeping; if we sent
        # the container's response id down to the subprocess verbatim,
        # the subprocess's `pending.get(parsed.id)` lookup would miss
        # and the handshake would silently time out.
        signed_id = signed.get("id")
        if isinstance(signed_id, str) and signed_id:
            self.signed_req_id = signed_id

        # Step 4: forward signed connect upstream verbatim. The container's
        # connect handler verifies device.id against paired.json and the
        # signature against the registered public key.
        await self._ws.send(json.dumps(signed))

        # Step 5: read hello-ok upstream. Skip any intermediate events (the
        # container occasionally interleaves `tick` etc. before res lands).
        msg = None
        for _ in range(20):
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_UPSTREAM_RECV_TIMEOUT)
            msg = json.loads(raw)
            if msg.get("type") == "res":
                break
            logger.debug("Skipping pre-handshake message: %s", msg.get("type"))
        else:
            raise RuntimeError("Node handshake: too many non-res messages before hello-ok")

        if not msg.get("ok"):
            error_detail = json.dumps(msg.get("error", msg), default=str)[:500]
            raise RuntimeError(f"Node handshake rejected: {error_detail}")

        # Record device_id from the SIGNED connect (client owns the key, so
        # this is the authoritative source). Used by node_proxy for
        # session-binding and force_close_device.
        self.device_id = (signed.get("params") or {}).get("device", {}).get("id")

        self._connected = True
        logger.info(
            "Node upstream connected for user %s device %s",
            self.user_id,
            (self.device_id or "?")[:16],
        )
        return msg

    # ------------------------------------------------------ steady-state I/O

    async def start_reader(self) -> None:
        """Start forwarding upstream frames to the client via the push callback."""
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        try:
            async for raw in self._ws:
                if self._client_push_callback is None:
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    logger.warning(
                        "non-JSON frame from upstream for %s: %r",
                        self.user_id,
                        (raw[:120] if isinstance(raw, (str, bytes)) else raw),
                    )
                    continue
                try:
                    await self._client_push_callback(data)
                except Exception:
                    logger.warning(
                        "client push failed for %s",
                        self.user_id,
                        exc_info=True,
                    )
        except Exception as e:
            logger.warning("Node upstream reader error for %s: %s", self.user_id, e)
        finally:
            self._connected = False
            # Notify node_proxy that the upstream died so it can clear
            # per-user routing state even if the desktop side is still
            # connected. Swallow callback errors -- we're already shutting
            # down this reader, failing here would just hide the original
            # disconnect cause from the log above.
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
        """Forward a message from the client to the container."""
        if self._ws and self._connected:
            await self._ws.send(json.dumps(message))

    async def close(self) -> None:
        self._connected = False
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                logger.warning("close error for %s", self.user_id, exc_info=True)
        self._ws = None
        logger.info("Node upstream closed for user %s", self.user_id)
