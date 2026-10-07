"""
Desktop app authentication via Clerk sign-in tokens.

Creates a one-time sign-in token that the desktop app's WebView
can consume to establish a Clerk session. This bridges the gap
between the system browser (where OAuth/passkeys work) and the
Tauri WKWebView (which needs its own Clerk session).
"""

import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["desktop"])

CLERK_API_URL = "https://api.clerk.com/v1"


@router.post("/desktop/sign-in-token")
async def create_sign_in_token(
    auth: AuthContext = Depends(get_current_user),
):
    """
    Create a one-time Clerk sign-in token for the desktop app.

    Called by the desktop-callback page after the user authenticates
    via Google OAuth in the system browser. The token is sent to the
    Tauri app via deep link, where the WebView consumes it to establish
    its own Clerk session.
    """
    clerk_secret = os.getenv("CLERK_SECRET_KEY")
    if not clerk_secret:
        raise HTTPException(status_code=500, detail="CLERK_SECRET_KEY not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{CLERK_API_URL}/sign_in_tokens",
            headers={
                "Authorization": f"Bearer {clerk_secret}",
                "Content-Type": "application/json",
            },
            json={
                "user_id": auth.user_id,
                "expires_in_seconds": 60,
            },
        )

    if resp.status_code != 200:
        logger.error("Clerk sign-in token creation failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Failed to create sign-in token")

    data = resp.json()
    return {"token": data["token"]}
