"""Drive a real chat against the dev WS gateway as the chaos test user.

Frame shapes verified against the live code (NOT assumed):
  * Outbound chat frame — ``apps/frontend/src/hooks/useGateway.tsx`` sendChat
    (line 537) sends ``{type, agent_id, message, clientRunId}``; the backend
    handler ``routers/websocket_chat.py`` ws_message (line 659) reads
    ``agent_id`` + ``message`` and treats ``clientRunId`` as optional (mints a
    UUID when absent). We include a clientRunId to match the real client and to
    correlate the terminal frame.
  * Inbound terminal frames — ``core/gateway/connection_pool.py``
    ``_transform_agent_event`` / the chat-state handler emit ``{type:"done",
    runId, agent_id?}`` on chat ``final`` (line 1342) and ``{type:"error",
    message, code?, runId?, agent_id?}`` on chat ``error``/``aborted``
    (lines 1373, 1382). The plan's done/error assumption is correct.

Auth: the WS URL is ``wss://ws-dev.isol8.co`` and the gateway authorizer reads
the JWT from the ``?token=`` query param. We mint a session token for the
dedicated chaos test user via Clerk's sign-in-token API using CLERK_SECRET_KEY.
"""

import asyncio
import json
import os
import uuid

import httpx
import websockets

WS_URL = "wss://ws-dev.isol8.co"
CHAOS_USER_ID = os.environ["CHAOS_FIS_USER_ID"]  # dedicated dev test user (org or user id)


async def mint_test_token() -> str:
    """Mint a session token for the chaos test user via Clerk's API.

    CLERK_SECRET_KEY (dev) must be set. Uses the sign-in-token flow, which
    returns a JWT the gateway authorizer accepts on the ``?token=`` query param.
    """
    secret = os.environ["CLERK_SECRET_KEY"]
    async with httpx.AsyncClient(base_url="https://api.clerk.com/v1", timeout=30) as c:
        r = await c.post(
            "/sign_in_tokens",
            headers={"Authorization": f"Bearer {secret}"},
            json={"user_id": CHAOS_USER_ID},
        )
        r.raise_for_status()
        return r.json()["token"]


async def drive_one_chat_turn(token: str, agent_id: str, *, timeout_s: float = 60.0) -> bool:
    """Send one message, return True if a 'done' frame arrives before timeout.

    Returns False on an 'error' frame or if the deadline elapses with no
    terminal. Frame shapes match the live client/backend (see module docstring).
    """
    async with websockets.connect(f"{WS_URL}?token={token}", open_timeout=20) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "agent_chat",
                    "agent_id": agent_id,
                    "message": "ping: chaos steady-state check",
                    "clientRunId": str(uuid.uuid4()),
                }
            )
        )

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            evt = json.loads(raw)
            if evt.get("type") == "done":
                return True
            if evt.get("type") == "error":
                return False
        return False
