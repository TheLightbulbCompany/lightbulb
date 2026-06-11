from __future__ import annotations

import logging
import subprocess

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.repositories import google_accounts_repo, integrations_repo
from core.services import config_patcher
from core.services.credentials import (
    apply_connection,
    integration_secret_store,
    oauth_dance,
    registry,
)
from core.services.credentials.materializers import MATERIALIZER_REMOVERS
from core.services.google_identity import seed_agent_google_identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["connections"])


def _retains_secret(p: registry.Provider) -> bool:
    """Whether the backend keeps the credential (in Secrets Manager).

    ``refresh_owner == "actor"`` means a CLI/keyring inside the container owns
    and self-refreshes the token (e.g. gog), so the backend stores nothing
    durable and the container EFS keyring is the sole custodian. Every other
    provider is backend-custodian: the secret is retained so it can be
    re-injected (marketplace slot-fill, reconnect)."""
    return p.refresh_owner != "actor"


def _remove_materialized_artifacts(owner_id: str, p: registry.Provider, credential: dict) -> None:
    """On a FAILED apply, remove any partially-materialized on-disk artifacts so a
    failed connect leaves no durable credential residue on EFS (the row is deleted
    separately). A materializer can write one file before a later step raises — e.g.
    gog's `credentials set` succeeds but `tokens import` fails — and without this the
    artifact lingers while the UI shows disconnected and no row remains to clean it up
    on a later disconnect. No-op for openclaw_paths/paste providers (no materializer)."""
    if not p.wiring.materializer:
        return
    remover = MATERIALIZER_REMOVERS.get(p.wiring.materializer)
    if not remover:
        return
    try:
        remover(owner_id, p.key, credential)
    except Exception:
        logger.warning(
            "failed to remove materialized artifacts for owner=%s provider=%s during rollback",
            owner_id,
            p.key,
            exc_info=True,
        )


class PasteBody(BaseModel):
    token: str


def _get_provider_or_404(provider: str) -> registry.Provider:
    try:
        return registry.get_provider(provider)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown integration provider: {provider}")


async def _validate_token(p: registry.Provider, token: str) -> bool:
    if not p.validation:
        return True
    # Reject a token shape the wired server cannot use, before spending an HTTP
    # call: some validate endpoints accept token types the env var doesn't (Slack
    # auth.test oks a bot `xoxb-` token, but the server gets the user-token slot).
    if p.validation.token_prefix and not token.startswith(p.validation.token_prefix):
        return False
    scheme = p.validation.auth_scheme
    value = f"{scheme} {token}".strip() if scheme else token
    # Static spec headers first, then the auth header — the auth header wins on a
    # name collision so a misconfigured static header can't clobber the credential.
    headers = {**p.validation.headers, p.validation.auth_header: value}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            kwargs: dict = {"headers": headers}
            if p.validation.body is not None:
                kwargs["json"] = p.validation.body
            resp = await client.request(p.validation.method, p.validation.url, **kwargs)
        if resp.status_code >= 400:
            return False
        # Providers that answer 200 even for a bad token (Slack auth.test,
        # Linear GraphQL) gate validity on a body field instead of the status.
        if p.validation.success_json_field:
            return _json_path_truthy(resp, p.validation.success_json_field)
        return True
    except httpx.HTTPError:
        return False


def _json_path_truthy(resp: httpx.Response, dotted_path: str) -> bool:
    """Walk a dotted path into the response JSON and report whether it resolves
    to a truthy value. A non-JSON body or a missing key means invalid (not an
    error) — a malformed response is never a valid credential."""
    try:
        cur: object = resp.json()
    except ValueError:
        return False
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return bool(cur)


@router.get("")
async def list_connections(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    rows = await integrations_repo.list_for_owner(owner_id)
    connected = {r["provider"]: r.get("status", "connected") for r in rows}
    # `internal` providers stay out of the catalog for everyone except
    # allowlisted owners. The UI is server-driven, so filtering here also hides
    # any dependent surface — e.g. the Google per-agent assignment panel only
    # renders when google reads back as connected, which it can't if google
    # isn't in the list at all.
    allowlisted = owner_id in settings.internal_integration_owners
    return {
        "connections": [
            {
                "provider": k,
                "display_name": v.display_name,
                "acquisition": v.acquisition,
                "status": connected.get(k, "disconnected"),
            }
            for k, v in registry.all_providers().items()
            if allowlisted or not v.internal
        ]
    }


@router.post("/{provider}")
async def connect_paste(provider: str, body: PasteBody, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    if p.acquisition != "paste_token":
        raise HTTPException(status_code=400, detail=f"{provider} is not a paste-key integration")
    if not await _validate_token(p, body.token):
        raise HTTPException(status_code=422, detail="token failed validation against the provider")
    credential = {"token": body.token}
    # Paste tokens are backend-custodian: store the credential in Secrets
    # Manager (KMS-encrypted at rest) — never in DynamoDB — then deliver the
    # literal into the container. Mark the integration connected only after
    # wiring succeeds, so status never reports a false "connected".
    await integration_secret_store.put(owner_id, provider, credential)
    try:
        await apply_connection.apply_connection(owner_id, provider, credential)
    except Exception:
        _remove_materialized_artifacts(owner_id, p, credential)
        await integration_secret_store.delete(owner_id, provider)
        raise HTTPException(
            status_code=502,
            detail="failed to apply the connection to your container; please retry",
        )
    await integrations_repo.put_status(owner_id, provider, "connected")
    return {"status": "connected", "provider": provider}


async def _seed_identity_best_effort(owner_id: str, agent_id: str) -> None:
    """Re-seed an agent's IDENTITY.md gog-account block, swallowing failures.

    Called on the disconnect path after an assignment is removed. The keyring +
    inventory removal is already done and authoritative, so a failure to rewrite
    the on-disk instruction (e.g. EFS hiccup) must not fail the disconnect — the
    block is stale at worst and gets corrected on the next assign/disconnect."""
    try:
        await seed_agent_google_identity(owner_id, agent_id)
    except Exception:
        logger.warning(
            "failed to re-seed google identity for owner=%s agent=%s after disconnect",
            owner_id,
            agent_id,
            exc_info=True,
        )


async def _disconnect_one_google_account(owner_id: str, p: registry.Provider, account: str) -> None:
    """Disconnect a single Google account: drop its keyring token, its inventory
    row, and any per-agent assignments pointing at it. The provider-level status
    and other accounts are left intact — one provider holds many accounts.

    Idempotent: a repeat disconnect of an already-gone account is a success. We
    skip the keyring remove when the account isn't in the inventory, and also
    tolerate the remover's not-found error (gog exits non-zero for an account it
    can't find) for the case where the keyring drifted from the inventory."""
    accounts = await google_accounts_repo.list_accounts(owner_id)
    if any(a["email"] == account for a in accounts):
        remover = MATERIALIZER_REMOVERS.get(p.wiring.materializer) if p.wiring.materializer else None
        if remover:
            try:
                remover(owner_id, p.key, {"email": account})
            except subprocess.CalledProcessError:
                # Account already absent from the keyring — treat as success so a
                # double-disconnect doesn't surface a 500.
                pass
    await google_accounts_repo.remove_account(owner_id, account)
    assignments = await google_accounts_repo.list_assignments(owner_id)
    for agent_id, email in assignments.items():
        if email == account:
            await google_accounts_repo.remove_assignment(owner_id, agent_id)
            # Re-seed so the agent's IDENTITY.md reflects life after the account:
            # with its explicit assignment now gone, the gog-account block is
            # removed (the seeder reflects explicit assignments only). Best-effort
            # — a seed failure here must not fail the disconnect, since the
            # authoritative keyring + inventory removal has already happened.
            await _seed_identity_best_effort(owner_id, agent_id)


@router.delete("/{provider}")
async def disconnect(provider: str, account: str | None = None, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    # ?account= scopes the disconnect to one Google account. It is Google-specific
    # (one provider, many accounts), so reject it on any other provider rather
    # than silently honoring it.
    if account is not None:
        if provider != "google":
            raise HTTPException(status_code=400, detail="the account parameter is only supported for google")
        await _disconnect_one_google_account(owner_id, p, account)
        return {"status": "disconnected", "provider": provider, "account": account}
    # Materializer-backed providers wrote credential files onto EFS; remove
    # those first so disconnect leaves no on-disk residue. The remover deletes
    # the artifact by owner/provider and does not need the secret value.
    if p.wiring.materializer:
        remover = MATERIALIZER_REMOVERS.get(p.wiring.materializer)
        if remover:
            # Some removers locate the on-disk artifact by a credential field
            # (e.g. toml_cli uses credential["cli"]), so fetch the retained
            # secret before deleting it. Actor-refresh providers have none
            # (get -> {}), and their removers (gog) ignore the credential.
            cred = await integration_secret_store.get(owner_id, provider) or {}
            remover(owner_id, provider, cred)
    # Drop the retained secret (no-op for actor providers, which never stored
    # one) and the status row, then blank any per-agent openclaw.json slots.
    await integration_secret_store.delete(owner_id, provider)
    await integrations_repo.delete(owner_id, provider)
    # Google holds a per-account inventory alongside the provider status row;
    # a provider-level disconnect must clear every account + assignment so no
    # stale record outlives the dropped keyring.
    if provider == "google":
        for acct in await google_accounts_repo.list_accounts(owner_id):
            await google_accounts_repo.remove_account(owner_id, acct["email"])
        for agent_id in await google_accounts_repo.list_assignments(owner_id):
            await google_accounts_repo.remove_assignment(owner_id, agent_id)
            # No accounts remain, so re-seeding removes each agent's gog-account
            # block. Best-effort: a seed failure must not fail the disconnect.
            await _seed_identity_best_effort(owner_id, agent_id)
    if p.wiring.openclaw_paths:
        for op in p.wiring.openclaw_paths:
            await config_patcher.set_openclaw_config_path(owner_id, op.path.split("."), "")
    if p.wiring.mcp_server:
        # The integration owns mcp.servers.<key> as a whole block, so remove the
        # entire server entry — leaving only the env leaf would strand a
        # command-less (invalid) server, and a blanked token would keep a dead
        # entry around.
        await config_patcher.delete_openclaw_config_path(owner_id, ["mcp", "servers", p.wiring.mcp_server.key])
    return {"status": "disconnected", "provider": provider}


def _redirect_uri(provider: str) -> str:
    # Server-derived, never caller-supplied. Must match the URI registered with
    # the provider and be identical in both authorize + callback legs.
    return f"{settings.PUBLIC_API_BASE}/api/v1/oauth/{provider}/callback"


@router.get("/{provider}/authorize")
async def authorize(provider: str, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    if p.acquisition != "direct_oauth":
        raise HTTPException(status_code=400, detail=f"{provider} is not an OAuth integration")
    url = await oauth_dance.start(owner_id, provider, redirect_uri=_redirect_uri(provider))
    return {"authorize_url": url}


# Public callback — the OAuth provider redirects the (unauthenticated) browser here.
# Identity comes from the server-side state record, NOT from the request. Mounted at /api/v1/oauth/...
# No org-admin gate here: the single-use `state` it consumes could only have been
# minted by `authorize`, which is admin-gated — so this write path is admin-gated transitively.
oauth_router = APIRouter(prefix="/oauth", tags=["connections"])


@oauth_router.get("/{provider}/callback")
async def oauth_callback(provider: str, code: str, state: str):
    try:
        owner_id, prov, credential = await oauth_dance.exchange(state, code, redirect_uri=_redirect_uri(provider))
    except ValueError:
        return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?error=oauth_state")
    p = registry.get_provider(prov)
    # Retain the credential in Secrets Manager only when the backend is the
    # custodian. Actor-refresh providers (e.g. gog) self-refresh inside the
    # container, so the backend stores nothing and the EFS keyring written by
    # apply_connection is the sole durable copy. Mark connected only after
    # wiring succeeds.
    if _retains_secret(p):
        await integration_secret_store.put(owner_id, prov, credential)
    try:
        connection_identity = await apply_connection.apply_connection(owner_id, prov, credential)
    except Exception:
        _remove_materialized_artifacts(owner_id, p, credential)
        if _retains_secret(p):
            await integration_secret_store.delete(owner_id, prov)
        return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?error=apply_failed")
    await integrations_repo.put_status(owner_id, prov, "connected")
    # Google supports multiple accounts under one provider-level "connected"
    # status: each connect adds a distinct account to the owner's inventory so an
    # agent can later be bound to a specific one. The account identity is the
    # email that apply_connection surfaced from the gog keyring seed.
    if prov == "google" and connection_identity:
        await google_accounts_repo.add_account(owner_id, connection_identity)
    return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?connected={prov}")
