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
the JWT from the ``?token=`` query param. That authorizer (and FastAPI) validate
a Clerk **session JWT** via JWKS — NOT a sign-in ticket. A sign-in token from
the Clerk Backend API (``/v1/sign_in_tokens``) is only a one-time *ticket*; the
frontend SDK exchanges it via ``signIn.create({strategy:'ticket'})`` against the
Clerk Frontend API (FAPI) to obtain a session, then reads the session JWT. We
replicate that exchange headlessly server-side (see ``mint_test_token`` below)
so the harness presents a real session JWT the authorizer accepts.
"""

import asyncio
import json
import os
import uuid

import httpx
import websockets

WS_URL = "wss://ws-dev.isol8.co"


async def mint_test_token() -> str:
    """Return a real Clerk **session JWT** for the dedicated chaos test user.

    The WS gateway authorizer + FastAPI validate Clerk *session JWTs* via JWKS.
    A Backend-API sign-in token is only a one-time *ticket*; presenting it on
    ``?token=`` fails auth. So we complete the full exchange the frontend SDK
    does (``signIn.create({strategy:'ticket'})`` + ``setActive``), headlessly:

      1. Mint a one-time ticket via the Clerk **Backend API**
         (``POST https://api.clerk.com/v1/sign_in_tokens`` with
         ``CLERK_SECRET_KEY`` + ``CHAOS_FIS_USER_ID``).
      2. Create a sign-in on the Clerk **Frontend API** (FAPI base = the Clerk
         instance domain = ``CLERK_ISSUER``, e.g.
         ``https://up-moth-55.clerk.accounts.dev``):
         ``POST {CLERK_ISSUER}/v1/client/sign_ins?_clerk_js_version=5``,
         form-encoded ``strategy=ticket&ticket={ticket}``. FAPI returns the
         ``__client`` cookie (Set-Cookie) and ``response.created_session_id``.
      3. Read the session JWT for that session:
         ``POST {CLERK_ISSUER}/v1/client/sessions/{session_id}/tokens
         ?_clerk_js_version=5`` carrying the ``__client`` cookie. The JSON
         ``jwt`` field IS the session JWT the authorizer accepts on ``?token=``.

    All env (``CLERK_SECRET_KEY``, ``CHAOS_FIS_USER_ID``, ``CLERK_ISSUER``) is
    read lazily here at call time, never at import, so importing this module
    never raises and collection of importing scenario modules can skip cleanly.

    Game-day note: this is exercised live for the first time at game-day against
    the real dev ``CHAOS_FIS_USER_ID`` user — the exact FAPI cookie/field shapes
    cannot be validated offline (no dev test user / network in CI).
    """
    secret = os.environ["CLERK_SECRET_KEY"]
    chaos_user_id = os.environ["CHAOS_FIS_USER_ID"]
    # FAPI base = the Clerk instance domain. CLERK_ISSUER already carries the
    # scheme + host (e.g. https://up-moth-55.clerk.accounts.dev).
    fapi_base = os.environ["CLERK_ISSUER"].rstrip("/")
    # Clerk's FAPI requires the JS-version query param on client endpoints.
    js_ver = {"_clerk_js_version": "5"}

    async with httpx.AsyncClient(timeout=30) as c:
        # 1) Mint the one-time sign-in ticket via the Backend API.
        ticket_resp = await c.post(
            "https://api.clerk.com/v1/sign_in_tokens",
            headers={"Authorization": f"Bearer {secret}"},
            json={"user_id": chaos_user_id},
        )
        ticket_resp.raise_for_status()
        ticket = ticket_resp.json()["token"]

        # 2) Exchange the ticket for a session on the Frontend API. Form-encoded,
        # matching what signIn.create({strategy:'ticket'}) sends. The __client
        # cookie returned here authenticates the token read in step 3.
        sign_in_resp = await c.post(
            f"{fapi_base}/v1/client/sign_ins",
            params=js_ver,
            data={"strategy": "ticket", "ticket": ticket},
        )
        sign_in_resp.raise_for_status()
        # The created session id lives under response; some FAPI shapes also put
        # it at the top level. Prefer the documented response.created_session_id.
        body = sign_in_resp.json()
        session_id = body.get("response", {}).get("created_session_id") or body.get("created_session_id")
        if not session_id:
            raise RuntimeError(f"Clerk sign_in returned no created_session_id: {body!r}")
        # httpx carries the __client cookie forward on the shared client jar.

        # 3) Read the session JWT for that session (POST mints a fresh token).
        token_resp = await c.post(
            f"{fapi_base}/v1/client/sessions/{session_id}/tokens",
            params=js_ver,
        )
        token_resp.raise_for_status()
        jwt = token_resp.json().get("jwt")
        if not jwt:
            raise RuntimeError(f"Clerk session tokens returned no jwt: {token_resp.json()!r}")
        return jwt


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
