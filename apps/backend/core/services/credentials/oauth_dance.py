"""Generic 3-legged OAuth (authorization_code + PKCE). Endpoint metadata from
the registry. Three client-acquisition modes:

- BYO app (e.g. google): the owner stores their own client_id + client_secret in
  Secrets Manager; the dance reads it on every connect/reconnect.
- Dynamic Client Registration (e.g. attio): the provider exposes a registration
  endpoint, so the dance registers a fresh PUBLIC client (no secret, PKCE-only)
  the first time the owner connects and caches it in the same store, keyed
  client_secret="" to mark it public.
- Platform app (e.g. instagram): Isol8 hosts ONE reviewed OAuth client
  (oauth.platform_app names it in platform_oauth_app_store); every owner
  authorizes against it and grants access to their own assets. The per-owner
  app store is never consulted.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt

from core.repositories import oauth_state_repo
from core.services.credentials import oauth_app_store, platform_oauth_app_store, registry

# Instagram's short→long-lived token upgrade endpoint (Business Login for
# Instagram / "Instagram API with Instagram Login"). GET with the app secret;
# returns a ~60-day token. See OAuthSpec.long_lived_exchange.
_IG_LONG_LIVED_URL = "https://graph.instagram.com/access_token"


def _long_lived_request(
    p: registry.Provider, *, client_id: str, client_secret: str, short_token: str
) -> tuple[str, dict] | None:
    """The second, secret-bearing call that upgrades a Meta short-lived token to ~60 days.

    Both Meta surfaces hand back a SHORT-lived token from the authorization-code
    exchange ("expiring in hours" per Meta's own docs), and NEITHER ever issues a
    refresh_token — Meta does not have them. So this upgrade is the only thing
    standing between a connect and a token that dies the same afternoon: with no
    refresh_token there is nothing for the MCP SDK's OAuthClientProvider (or
    anything else) to renew from, and the provider simply starts 401ing.

    Returns None for providers that declare no upgrade.
    """
    kind = p.oauth.long_lived_exchange
    if kind == "ig_exchange_token":
        return _IG_LONG_LIVED_URL, {
            "grant_type": "ig_exchange_token",
            "client_secret": client_secret,
            "access_token": short_token,
        }
    if kind == "fb_exchange_token":
        # Facebook Login for Business — the Meta Ads MCP path. Unlike Instagram's,
        # this one also requires client_id. It reuses the SAME endpoint as the code
        # exchange, so the Graph version rides p.oauth.token_url and there is no
        # second place to bump when it moves.
        return p.oauth.token_url, {
            "grant_type": "fb_exchange_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "fb_exchange_token": short_token,
        }
    return None


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
        "client_name": "Lightbulb",
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
        redirect_uri=redirect_uri,
    )
    return info


def _registered_redirect_uris(cached: dict | None) -> list[str]:
    """Redirect URIs a cached DCR client is known to be registered under.

    Empty means UNKNOWN (a pre-``redirect_uri`` record whose registration
    response did not echo ``redirect_uris``), not "none" — callers must treat
    it as no-drift rather than as a mismatch.
    """
    if not cached:
        return []
    stored = cached.get("redirect_uri")
    if stored:
        return [stored]
    echoed = (cached.get("client_information") or {}).get("redirect_uris")
    return list(echoed) if isinstance(echoed, list) else []


async def _oauth_client(owner_id: str, provider_key: str) -> tuple[str, str]:
    """The OAuth client for this provider: the Isol8 platform app (when the
    provider declares one), the owner's BYO app, or a DCR-registered public
    client. A missing/half-populated app is an error — start() and exchange()
    both call this, so a connect attempt without a usable client fails fast.

    A public client legitimately has an empty secret (client_secret=""), so only the
    client_id is required here; the secret may be empty."""
    p = registry.get_provider(provider_key)
    if p.oauth and p.oauth.platform_app:
        app = await platform_oauth_app_store.get(p.oauth.platform_app)
        if not app:
            # Operator-side gap, not a user error: the shared client was never
            # seeded into Secrets Manager for this environment.
            raise ValueError(f"{provider_key}: platform OAuth app '{p.oauth.platform_app}' is not provisioned")
        return app["client_id"], app["client_secret"]
    byo = await oauth_app_store.get(owner_id, provider_key)
    if not (byo and byo.get("client_id")):
        raise ValueError(f"{provider_key} requires your own OAuth app; none configured for this owner")
    return byo["client_id"], byo.get("client_secret") or ""


def _expiry_iso(expires_in: int | None) -> str:
    if not expires_in:
        return ""
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


async def start(owner_id: str, provider_key: str, *, redirect_uri: str, origin: str | None = None) -> str:
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
        # ...and re-register on a REDIRECT-URI change too. Registration pins
        # ``redirect_uris`` (see the body in _register_dcr_client), so after a
        # callback-domain move every cached client rejects the new URI and no
        # amount of "the owner reconnects" heals it. Prefer the uri we now
        # record on the app-store record; records written before that field
        # existed fall back to the registration response's own echo of
        # ``redirect_uris``. When neither is present the uri is unknown, and we
        # deliberately do NOT churn the client on a guess.
        registered_uris = _registered_redirect_uris(cached)
        if (
            not (cached and cached.get("client_id"))
            or cached.get("scope") != requested_scope
            or (registered_uris and redirect_uri not in registered_uris)
        ):
            await _register_dcr_client(owner_id, provider_key, p, redirect_uri=redirect_uri)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    # Resolve the client BEFORE persisting the state so the state records the exact
    # client_id this authorization is issued to. exchange() then reuses it rather
    # than re-reading the app store, which a concurrent connect could have replaced
    # with a different DCR-registered client (the code↔client binding stays atomic).
    client_id, _ = await _oauth_client(owner_id, provider_key)
    # `origin` is written for agent-started ("container") and native-app ("app") flows.
    await oauth_state_repo.put(
        state,
        owner_id=owner_id,
        provider=provider_key,
        verifier=verifier,
        client_id=client_id,
        **({"origin": origin} if origin else {}),
    )
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


async def exchange(
    rec: dict, code: str, *, redirect_uri: str, realm_id: str | None = None
) -> tuple[str, str, dict, str | None]:
    """Exchange a consumed state record, returning (owner, provider, credential, origin).

    The callback consumes the record so it retains the return destination even
    when the provider exchange fails. Identity and PKCE still come only from it.
    """
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
    # A confidential one authenticates the way its provider declares: in the form
    # body (the default every provider but X accepts) or via HTTP Basic, which X
    # requires and which it 401s without.
    auth = None
    if client_secret:
        if p.oauth.token_endpoint_auth_method == "client_secret_basic":
            auth = httpx.BasicAuth(client_id, client_secret)
            # The Basic header already identifies the client, so client_id must not
            # ALSO ride the body: Intuit counts that as two client-authentication
            # methods and answers 401 invalid_client — an error naming the
            # credential rather than the duplication. X, the only other Basic
            # provider, documents the same expectation ("You don't need client id
            # for confidential clients with a valid Authorization Header").
            data.pop("client_id", None)
        else:
            data["client_secret"] = client_secret
    # RFC 8707 resource indicator: mirror the authorize request so the issued token
    # is audienced to the canonical MCP server URI. Only for MCP-remote providers.
    if p.mcp_remote is not None:
        data["resource"] = p.mcp_remote.url
    async with httpx.AsyncClient(timeout=15) as http:
        # GitHub's token endpoint answers form-encoded unless asked for JSON;
        # every other provider returns JSON regardless, so this is universal.
        resp = await http.post(p.oauth.token_url, data=data, headers={"Accept": "application/json"}, auth=auth)
        resp.raise_for_status()
        tok = resp.json()
        # Meta's code exchange yields a token measured in hours; agents need the
        # ~60d one. The upgrade requires the app secret, so it happens here — the
        # ONLY place outside Secrets Manager that ever sees the platform secret.
        long_lived = _long_lived_request(
            p, client_id=client_id, client_secret=client_secret, short_token=tok["access_token"]
        )
        if long_lived is not None:
            url, params = long_lived
            # GET with the secret in the query string is what Meta documents for
            # both grants (no POST/form-body variant is offered), so it stays a
            # GET. The reason that no longer leaks it: configure_logging pins the
            # httpx logger to WARNING, which drops the request-URL INFO line.
            ll = await http.get(url, params=params)
            ll.raise_for_status()
            upgraded = ll.json()
            tok["access_token"] = upgraded["access_token"]
            tok["expires_in"] = upgraded.get("expires_in")
    expires_in = tok.get("expires_in")
    credential = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "token_expiry": _expiry_iso(expires_in),
        "expires_in": int(expires_in) if expires_in else None,
        "client_id": client_id,
        "client_secret": client_secret,
        # The callback this code was issued against. Carried through because some
        # materializers persist the CLI's own app registration (xurl's ~/.xurl
        # records a redirect_uri per app), and re-deriving it downstream would be
        # a second source of truth that can drift from the authorize leg.
        "redirect_uri": redirect_uri,
    }
    # Instagram's exchange also returns the app-scoped IG user id — materializers
    # need it for Graph calls, so carry it through when present.
    if tok.get("user_id"):
        credential["provider_user_id"] = str(tok["user_id"])
    # QuickBooks identifies the connected COMPANY with a realmId that rides the
    # authorize REDIRECT, not the token response — it is the only provider here
    # whose credential is incomplete without a callback query param, and every QBO
    # API path is /v3/company/{realmId}/..., so a connect without it cannot work.
    if realm_id:
        credential["realm_id"] = realm_id
    # OIDC providers (Microsoft) name the signed-in account and its tenant in the
    # id_token. It came straight from the token endpoint over TLS, which OIDC Core
    # §3.1.3.7 accepts in place of a signature check. Only the claims are kept.
    if tok.get("id_token"):
        try:
            claims = jwt.decode(tok["id_token"], options={"verify_signature": False})
        except jwt.PyJWTError:
            claims = {}  # identity is cosmetic; a malformed id_token must not fail the connect
        if claims.get("tid"):
            credential["tenant_id"] = claims["tid"]
        if account := claims.get("preferred_username") or claims.get("email"):
            credential["account"] = account
    # The platform app's secret is Isol8's, not the owner's. It must exist only in
    # its own Secrets Manager entry — never inside a per-owner credential payload,
    # where downstream rails (materializers, client_information) would carry it
    # toward agent-readable surfaces.
    if p.oauth.platform_app:
        credential["client_secret"] = ""
    # The mcp_oauth materializer needs the full registered-client JSON to write
    # store.clientInformation. It was cached at registration; carry it through so
    # the materializer doesn't re-read the store. A BYO provider (google) has no
    # cached client_information — synthesize the minimal {client_id[, client_secret]}
    # so the field is always present and shaped like OAuthClientInformationMixed.
    app = None if p.oauth.platform_app else await oauth_app_store.get(rec["owner_id"], rec["provider"])
    if app and app.get("client_information"):
        credential["client_information"] = app["client_information"]
    else:
        # Synthesize from the credential (not the local client_secret) so a
        # platform app's blanked secret stays blank here too.
        info: dict = {"client_id": client_id}
        if credential["client_secret"]:
            info["client_secret"] = credential["client_secret"]
        credential["client_information"] = info
    return rec["owner_id"], rec["provider"], credential, rec.get("origin")


async def refresh(owner_id: str, provider_key: str, credential: dict) -> dict:
    """Trade a backend-held refresh grant for a fresh access token.

    Returns the updated credential; the caller persists it. Microsoft (and Intuit)
    ROTATE the refresh token on every use, so a returned one replaces the old; a
    provider that returns none keeps the old. The client secret is re-read from its
    own store every time — a platform app's is never inside the owner's credential.
    """
    p = registry.get_provider(provider_key)
    if p.oauth.long_lived_exchange == "ig_exchange_token":
        return await refresh_ig(credential)
    store_id, client_secret = await _oauth_client(owner_id, provider_key)
    # The grant belongs to the client it was issued to; a later DCR re-registration
    # must not swap it out from under an existing refresh token.
    client_id = credential.get("client_id") or store_id
    data = {
        "grant_type": "refresh_token",
        "refresh_token": credential["refresh_token"],
        "client_id": client_id,
        "scope": p.oauth.scope_separator.join(p.oauth.scopes),
    }
    auth = None
    if client_secret:
        if p.oauth.token_endpoint_auth_method == "client_secret_basic":
            auth = httpx.BasicAuth(client_id, client_secret)
            data.pop("client_id")
        else:
            data["client_secret"] = client_secret
    if p.mcp_remote is not None:
        data["resource"] = p.mcp_remote.url
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(p.oauth.token_url, data=data, headers={"Accept": "application/json"}, auth=auth)
        resp.raise_for_status()
        tok = resp.json()
    expires_in = tok.get("expires_in")
    return {
        **credential,
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or credential["refresh_token"],
        "token_expiry": _expiry_iso(expires_in),
        "expires_in": int(expires_in) if expires_in else None,
    }


_IG_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"


async def refresh_ig(credential: dict) -> dict:
    """Extend a long-lived Instagram token (it has no refresh token: the token refreshes itself).
    Bearer header, never the token in the URL (same as ``instagram_cli._fetch_identity``), so it
    cannot land in an access log."""
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.get(
            _IG_REFRESH_URL,
            params={"grant_type": "ig_refresh_token"},
            headers={"Authorization": f"Bearer {credential['access_token']}"},
        )
        resp.raise_for_status()
        tok = resp.json()
    expires_in = tok.get("expires_in")
    return {
        **credential,
        "access_token": tok["access_token"],
        "token_expiry": _expiry_iso(expires_in),
        "expires_in": int(expires_in) if expires_in else None,
    }
