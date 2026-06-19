"""Generic 3-legged OAuth (authorization_code + PKCE). One BYO OAuth app per
provider (creds in Secrets Manager); endpoint metadata from the registry."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from core.repositories import oauth_state_repo
from core.services.credentials import oauth_app_store, registry


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _oauth_client(owner_id: str, provider_key: str) -> tuple[str, str]:
    """The owner's OWN OAuth app for this provider. There is no Isol8-managed
    shared app, so a missing or half-populated BYO app is an error — start() and
    exchange() both call this, so a connect attempt without a stored app fails
    fast instead of silently using a shared app."""
    byo = await oauth_app_store.get(owner_id, provider_key)
    if not (byo and byo.get("client_id") and byo.get("client_secret")):
        raise ValueError(f"{provider_key} requires your own OAuth app; none configured for this owner")
    return byo["client_id"], byo["client_secret"]


def _expiry_iso(expires_in: int | None) -> str:
    if not expires_in:
        return ""
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


async def start(owner_id: str, provider_key: str, *, redirect_uri: str) -> str:
    p = registry.get_provider(provider_key)
    if not p.oauth:
        raise ValueError(f"{provider_key} has no oauth spec")
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    await oauth_state_repo.put(state, owner_id=owner_id, provider=provider_key, verifier=verifier)
    client_id, _ = await _oauth_client(owner_id, provider_key)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": p.oauth.scope_separator.join(p.oauth.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **p.oauth.extra_authorize_params,
    }
    return f"{p.oauth.authorization_url}?{urlencode(params)}"


async def exchange(state: str, code: str, *, redirect_uri: str) -> tuple[str, str, dict]:
    rec = await oauth_state_repo.consume(state)
    if not rec:
        raise ValueError("invalid or expired OAuth state")
    p = registry.get_provider(rec["provider"])
    client_id, client_secret = await _oauth_client(rec["owner_id"], rec["provider"])
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            p.oauth.token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": rec["verifier"],
            },
        )
    resp.raise_for_status()
    tok = resp.json()
    credential = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "token_expiry": _expiry_iso(tok.get("expires_in")),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    return rec["owner_id"], rec["provider"], credential
