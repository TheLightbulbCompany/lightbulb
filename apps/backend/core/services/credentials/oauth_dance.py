"""Generic 3-legged OAuth (authorization_code + PKCE). Endpoint metadata from
the registry. Two client-acquisition modes:

- BYO app (e.g. google): the owner stores their own client_id + client_secret in
  Secrets Manager; the dance reads it on every connect/reconnect.
- Dynamic Client Registration (e.g. attio): the provider exposes a registration
  endpoint, so the dance registers a fresh PUBLIC client (no secret, PKCE-only)
  the first time the owner connects and caches it in the same store, keyed
  client_secret="" to mark it public.
"""

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


async def _register_dcr_client(owner_id: str, provider_key: str, p: registry.Provider, *, redirect_uri: str) -> dict:
    """Dynamically register a fresh PUBLIC OAuth client (RFC 7591) and cache it.

    For a provider whose OAuthSpec carries a registration_url and that has no BYO
    app stored, POST the registration endpoint to mint a client_id we then reuse.
    token_endpoint_auth_method="none" requests a public client (no secret), which
    matches a headless container owner who can't safeguard a confidential secret —
    PKCE is the only client proof. The full registration response (the
    OAuthClientInformationMixed) is cached so re-connects reuse the same client;
    client_secret is stored as "" to flag the client public to exchange()."""
    body = {
        "client_name": "Isol8",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": p.oauth.scope_separator.join(p.oauth.scopes),
    }
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(p.oauth.registration_url, json=body)
    resp.raise_for_status()
    info = resp.json()
    # Cache the bare client_id/secret (what _oauth_client reads), the full
    # registration JSON (what the materializer writes as store.clientInformation),
    # and the scope this client was registered under (so start() can detect a scope
    # change and re-register without deleting the secret).
    await oauth_app_store.put(
        owner_id,
        provider_key,
        client_id=info["client_id"],
        client_secret=info.get("client_secret") or "",
        client_information=info,
        scope=body["scope"],
    )
    return info


async def _oauth_client(owner_id: str, provider_key: str) -> tuple[str, str]:
    """The OAuth client for this provider: the owner's BYO app, or a DCR-registered
    public client. A missing/half-populated app is an error — start() and exchange()
    both call this, so a connect attempt without a usable client fails fast.

    A public client legitimately has an empty secret (client_secret=""), so only the
    client_id is required here; the secret may be empty."""
    byo = await oauth_app_store.get(owner_id, provider_key)
    if not (byo and byo.get("client_id")):
        raise ValueError(f"{provider_key} requires your own OAuth app; none configured for this owner")
    return byo["client_id"], byo.get("client_secret") or ""


def _expiry_iso(expires_in: int | None) -> str:
    if not expires_in:
        return ""
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


async def start(owner_id: str, provider_key: str, *, redirect_uri: str) -> str:
    p = registry.get_provider(provider_key)
    if not p.oauth:
        raise ValueError(f"{provider_key} has no oauth spec")
    # DCR providers register a public client on first connect, and RE-register
    # (overwriting the cached one) whenever the requested scope set has changed since
    # it was registered — the DCR registration body pins the scope, so a client
    # registered under a stale scope list would cap the grant. We compare the stored
    # scope and overwrite via put() rather than delete+recreate the secret, which
    # would race AWS's async secret-name release on a quick reconnect. BYO providers
    # (no registration_url) skip this and rely on the owner's stored app.
    if p.oauth.registration_url:
        cached = await oauth_app_store.get(owner_id, provider_key)
        requested_scope = p.oauth.scope_separator.join(p.oauth.scopes)
        if not (cached and cached.get("client_id")) or cached.get("scope") != requested_scope:
            await _register_dcr_client(owner_id, provider_key, p, redirect_uri=redirect_uri)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    # Resolve the client BEFORE persisting the state so the state records the exact
    # client_id this authorization is issued to. exchange() then reuses it rather
    # than re-reading the app store, which a concurrent connect could have replaced
    # with a different DCR-registered client (the code↔client binding stays atomic).
    client_id, _ = await _oauth_client(owner_id, provider_key)
    await oauth_state_repo.put(state, owner_id=owner_id, provider=provider_key, verifier=verifier, client_id=client_id)
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
    # RFC 8707 resource indicator: a remote MCP server (mcp_remote) wants the
    # canonical server URI on the authorization request so the token is audienced
    # to it. Only set for MCP-remote providers — a BYO OAuth provider (google) has
    # no mcp_remote and must not receive an unexpected resource param.
    if p.mcp_remote is not None:
        params["resource"] = p.mcp_remote.url
    return f"{p.oauth.authorization_url}?{urlencode(params)}"


async def exchange(state: str, code: str, *, redirect_uri: str) -> tuple[str, str, dict]:
    rec = await oauth_state_repo.consume(state)
    if not rec:
        raise ValueError("invalid or expired OAuth state")
    p = registry.get_provider(rec["provider"])
    # The client this code was issued to. Prefer the state-bound client_id (written
    # by start()) so a concurrent connect that re-registered a different DCR client
    # in the app store can't make us exchange against the wrong client. The secret
    # still comes from the store (a public DCR client has none; a BYO app's secret
    # is keyed to its client_id). Fall back to the store's client_id for states
    # minted before this binding existed (back-compat for in-flight authorizations).
    store_client_id, client_secret = await _oauth_client(rec["owner_id"], rec["provider"])
    client_id = rec.get("client_id") or store_client_id
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": rec["verifier"],
    }
    # A public (PKCE-only) client has no secret — sending an empty client_secret
    # makes some token endpoints reject the request, so omit the field entirely.
    # The code_verifier is the client's proof for a public client.
    if client_secret:
        data["client_secret"] = client_secret
    # RFC 8707 resource indicator: mirror the authorize request so the issued token
    # is audienced to the canonical MCP server URI. Only for MCP-remote providers.
    if p.mcp_remote is not None:
        data["resource"] = p.mcp_remote.url
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(p.oauth.token_url, data=data)
    resp.raise_for_status()
    tok = resp.json()
    expires_in = tok.get("expires_in")
    credential = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "token_expiry": _expiry_iso(expires_in),
        "expires_in": int(expires_in) if expires_in else None,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    # The mcp_oauth materializer needs the full registered-client JSON to write
    # store.clientInformation. It was cached at registration; carry it through so
    # the materializer doesn't re-read the store. A BYO provider (google) has no
    # cached client_information — synthesize the minimal {client_id[, client_secret]}
    # so the field is always present and shaped like OAuthClientInformationMixed.
    app = await oauth_app_store.get(rec["owner_id"], rec["provider"])
    if app and app.get("client_information"):
        credential["client_information"] = app["client_information"]
    else:
        info: dict = {"client_id": client_id}
        if client_secret:
            info["client_secret"] = client_secret
        credential["client_information"] = info
    return rec["owner_id"], rec["provider"], credential
