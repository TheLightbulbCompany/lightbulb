from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from urllib.parse import urlsplit
import secrets
import time
from typing import Literal
from collections import defaultdict, deque

import boto3
import httpx
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.billing import bedrock_aip
from core.billing.bedrock_aip import CONNECTOR_ROLE_NAME
from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.repositories import google_accounts_repo, integrations_repo, oauth_state_repo
from core.services.credentials import (
    apply_connection,
    aws_cfn,
    integration_secret_store,
    oauth_app_store,
    oauth_dance,
    registry,
)

# Bound directly (not via the `apply_connection` module reference above), so
# `except RuntimeUnsupportedError` keeps working even when a test mocks the
# whole `routers.connections.apply_connection` module out — the class object
# itself never changes underneath that patch.
from core.services.credentials.apply_connection import RuntimeUnsupportedError
from core.services.credentials import hermes_cli
from core.services.credentials.mcp_server import reconcile_integrations
from core.services import ideas, paperclip_github_token, rules_builtins
from core.services.google_identity import seed_agent_google_identity
from core.services.owner_facts import write_owner_facts
from core.services import owner_lifecycle
from core.services.owner_lifecycle import OwnerLifecycleBusyError, owner_lifecycle_guard

logger = logging.getLogger(__name__)

# Separator for the two-value paste rail ("<host>|<token>", see
# ValidateSpec.host_pattern) — split on the FIRST one, so a secret may contain it.
_PASTE_SEPARATOR = "|"

router = APIRouter(prefix="/connections", tags=["connections"])


def _retains_secret(p: registry.Provider, row: dict | None = None) -> bool:
    """Whether the backend keeps the credential (in Secrets Manager).

    ``refresh_owner == "actor"`` means a CLI/keyring inside the container owns
    and self-refreshes the token (e.g. gog), so the backend stores nothing
    durable and the container EFS keyring is the sole custodian. Every other
    provider is backend-custodian: the secret is retained so it can be
    re-injected (marketplace slot-fill, reconnect). ``retain_secret: true``
    opts an actor-refresh provider back in — posthog, so the backend can run
    its own HogQL reads (owner-day rollups) without touching the container's
    MCP copy. Disconnect deletes the retained secret unconditionally
    (``_disconnect_teardown``), so the flag needs no teardown wiring.

    A row with ``custody: backend`` (spec 13) is backend-custodian whatever the provider's default.
    """
    return registry.refresh_owner_for(p, row) != "actor" or p.retain_secret


async def _connect_custody(owner_id: str, p: registry.Provider) -> str | None:
    """``"backend"`` for a custody-migratable provider connected in a workspace with the
    super-agent switch on (spec 13: the backend holds the rotating grant from the start), else
    None -- the row gets no custody attribute and today's rule applies.

    Off in code until V11 rolls: the bridge does not yet fill a vended token into the CLI files,
    so a backend-custody connect would leave X/QuickBooks/Instagram with token-free files nothing
    fills (review of #4691). V11 restores:
    ``return "backend" if p.custody_migratable and await flags_for(owner_id) else None``."""
    return None


async def _remove_materialized_artifacts(owner_id: str, p: registry.Provider, credential: dict) -> None:
    """On a FAILED apply, remove any partially-materialized artifacts so a failed connect
    leaves no durable credential residue in the Hermes profiles ``hermes_cli`` projects
    into (the row is deleted separately). A materializer can write one file before a later
    step raises — e.g. gog's `credentials set` succeeds but `tokens import` fails — and
    without this the artifact lingers while the UI shows disconnected and no row remains to
    clean it up on a later disconnect. No-op for openclaw_paths/paste providers (no
    materializer)."""
    if not p.wiring.materializer:
        return
    # The residue is in S3 — and hermes_cli.clear is already best-effort.
    await hermes_cli.clear(owner_id, p.key, p.wiring.materializer, credential)


class PasteBody(BaseModel):
    token: str = ""
    # A paste_fields provider's labelled inputs, keyed by field key; joined server-side.
    fields: dict[str, str] | None = None


class ByoAppRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=512)


class AwsCompleteBody(BaseModel):
    role_arn: str
    region: str | None = None


class AwsCredentialsBody(BaseModel):
    # Bounded AND charset-restricted because this endpoint is unauthenticated:
    # the owner_id flows into a DynamoDB key, a Secrets Manager name lookup, and
    # an STS RoleSessionName. The pattern matches the Clerk id shape
    # resolve_owner_id produces (org_/user_ + alphanumerics), so nothing outside
    # that set — whitespace, newlines, path separators — ever reaches those
    # sinks; it makes the confused-deputy reasoning explicit rather than relying
    # on Clerk id shape as an implicit assumption.
    owner_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


# A concrete IAM role ARN, capturing the 12-digit owning account. Deliberately
# strict: the role-path portion is the IAM name charset (letters, digits and
# +=,.@-_ plus / for paths), which excludes wildcards — so ``role/*`` is
# rejected rather than granting sts:AssumeRole on every role in the account. The
# account capture group is the confused-deputy guard — aws-complete rejects any
# ARN inside our own account (bedrock_aip.OUR_ACCOUNT_ID) before granting.
_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::(\d{12}):role/[\w+=,.@/-]+$")

# AWS region names are lowercase alphanumerics + hyphens. ``region`` is
# owner-supplied and gets materialized into ~/.aws/config (an INI file), so it
# is validated against this charset — a newline would otherwise inject a second
# profile/section. Matched with fullmatch so a trailing newline can't slip past
# the ``$`` anchor.
_REGION_RE = re.compile(r"^[a-z0-9-]+$")

# --- container credential_process endpoint machinery -------------------------
# The helper on the owner's EFS calls POST /aws/credentials with a per-owner
# bearer token (minted at aws-complete; only its SHA-256 hash is retained in
# the SM credential). Legitimate use is ~1 fetch per 50 minutes of active use,
# so the per-owner limit is generous. In-process sliding window, same pattern
# and caveats as routers/_public_ingress.py (per-task buckets — RULING D17: at
# desiredCount 2-4 (D's autoscale range) each instance keeps its own window,
# so the effective per-owner ceiling is 2-4x this value, not a hard cap).
_AWS_CLI_RATE_LIMIT_PER_MINUTE = 10
_aws_cli_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
# The SM credential, cached per owner so a wrong-token spray is refused WITHOUT a Secrets
# Manager fetch and without touching the rate bucket (which only correct tokens fill, so
# someone else's junk can never get the real helper a 429). A mismatch refetches at most
# once per floor, which is how a reconnect's new token is picked up. Every entry, matching
# or not, is refetched after the TTL, so a revoked token works at most that long on a task
# that cached it (this task drops its entry on disconnect).
_AWS_CLI_REFETCH_FLOOR_S = 10.0
_AWS_CLI_RECORD_TTL_S = 60.0
_aws_cli_records: dict[str, tuple[float, dict]] = {}
# One refresh per owner at a time: concurrent misses wait for the first fetch, then re-check.
_aws_cli_refresh_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _aws_cli_record_is_stale(owner_id: str, presented_hash: str) -> bool:
    cached = _aws_cli_records.get(owner_id)
    if cached is None:
        return True
    age = time.monotonic() - cached[0]
    return age >= _AWS_CLI_RECORD_TTL_S or (
        age >= _AWS_CLI_REFETCH_FLOOR_S and not _cli_token_matches(cached[1], presented_hash)
    )


def _cli_token_matches(rec: dict, presented_hash: str) -> bool:
    expected = rec.get("cli_token_hash") or ""
    return bool(expected) and hmac.compare_digest(presented_hash, expected)


def _check_aws_cli_rate_limit(owner_id: str) -> bool:
    now = time.monotonic()
    cutoff = now - 60.0
    q = _aws_cli_rate_buckets[owner_id]
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= _AWS_CLI_RATE_LIMIT_PER_MINUTE:
        return False
    q.append(now)
    return True


def _hash_cli_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _sts_client():
    return boto3.client("sts", region_name=settings.AWS_REGION)


async def _self_account_role_is_pinned(role_arn: str, owner_id: str, external_id: str) -> bool:
    """True iff a role IN OUR account is a legitimate connector for THIS owner:
    its trust policy allows the platform trust principal (our account root) AND
    requires the owner's ExternalId. Only we can create such a role, and our
    infra roles (which trust service principals, never root+ExternalId) never
    satisfy it — so this blocks connecting an arbitrary our-account role
    (account-takeover) without an owner-id allowlist. Any failure (GetRole
    error, shape mismatch) -> False."""
    role_name = role_arn.split(":role/", 1)[1]
    try:
        resp = await run_in_thread(bedrock_aip._iam_client().get_role, RoleName=role_name)
        trust = resp["Role"]["AssumeRolePolicyDocument"]  # boto3 returns a decoded dict
        expected_principal = aws_cfn.platform_trust_principal_arn()
        # ``Statement`` is a list per the IAM policy grammar, but a single-object
        # form is also valid — normalize so iteration can't blow up on a dict.
        statements = trust.get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]
        for stmt in statements:
            if not isinstance(stmt, dict) or stmt.get("Effect") != "Allow":
                continue
            # ``Principal`` may be a dict ({"AWS": ...}), a bare string ("*"), or
            # absent. Only the dict form can name our owner principal; anything
            # else just can't match, so treat it as a non-match rather than raise.
            principal_block = stmt.get("Principal")
            if not isinstance(principal_block, dict):
                continue
            principal = principal_block.get("AWS")
            principals = principal if isinstance(principal, list) else [principal]
            if expected_principal not in principals:
                continue
            cond = stmt.get("Condition", {}).get("StringEquals", {})
            if cond.get("sts:ExternalId") == external_id:
                return True
        return False
    except Exception:
        # Any GetRole error OR unexpected trust-doc shape -> fail closed. The
        # caller turns a False here into a clean 400, never a 500.
        return False


def _available_for(p: registry.Provider, owner_id: str | None) -> bool:
    """Router-local alias; the policy itself lives in registry.available_for so
    that ownerless surfaces (routers/platform_info.py) share the env half of the
    gate instead of re-deriving it and drifting."""
    return registry.available_for(p, owner_id)


def _get_provider_or_404(provider: str, owner_id: str | None = None) -> registry.Provider:
    try:
        p = registry.get_provider(provider)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown integration provider: {provider}")
    if not _available_for(p, owner_id):
        # Env-gated provider hit outside its envs by an owner not on its allowlist
        # (e.g. dev-only Instagram in prod): 404 like an unknown provider so it's
        # neither connectable nor discoverable.
        raise HTTPException(status_code=404, detail=f"unknown integration provider: {provider}")
    return p


def _get_provider_for_teardown(provider: str) -> registry.Provider:
    """Like `_get_provider_or_404` but WITHOUT the availability gate.

    Revoking access can only ever remove capability, so the gate must not block
    it. An owner who connected while allowlisted, and whose allowlist entry was
    then removed, would otherwise hold a credential the backend keeps refreshing
    and the UI cannot delete. Existence is still checked, so an unknown provider
    is still a 404.
    """
    try:
        return registry.get_provider(provider)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown integration provider: {provider}")


async def _byo_active(owner_id: str, provider_key: str) -> bool:
    """True when the owner has stored their OWN OAuth app (client id + secret)
    for a byo-required provider — the prerequisite for connecting it."""
    prov = registry.all_providers().get(provider_key)
    if not prov or not prov.byo_required:
        return False
    app = await oauth_app_store.get(owner_id, provider_key)
    return bool(app and app.get("client_id") and app.get("client_secret"))


def _connection_row(
    key: str,
    v: registry.Provider,
    connected: dict[str, str],
    byo: bool,
    owner_id: str | None,
) -> dict:
    """One row of GET /connections.

    `requestable` means "this owner cannot START a connection" — the authorize
    leg would 404 — so the UI offers Request access (emails support) instead of a
    Connect button. `status` is then "unavailable" rather than "disconnected", so
    no surface mistakes it for one click away.

    An ALREADY-CONNECTED owner is never requestable, even if the gate has since
    closed on them (allowlist edited, env changed). Masking a live credential as
    "unavailable" would hide a working integration AND strip its only Disconnect
    affordance — an unrevokable credential the owner is told they don't have,
    still live and still usable by their agents. Revocation must
    always survive a gate change, so teardown does not consult the gate either
    (see `_get_provider_for_teardown`).
    """
    live = connected.get(key)
    requestable = not live and not registry.available_for(v, owner_id)
    runtime_supported = apply_connection.runtime_supported(v)
    runtime_unavailable = not live and not runtime_supported
    return {
        "provider": key,
        "display_name": v.display_name,
        "acquisition": v.acquisition,
        # actor: the integration's rail — "mcp" surfaces as tools in the
        # agent's tool list, "cli" is an authenticated binary in the agent's
        # shell (gh/aws/gog) that NEVER appears as tools, "skill" is read by a
        # bundled skill. The UI badges the row with it so a connected CLI
        # integration isn't mistaken for a broken MCP one.
        "actor": v.actor,
        "status": "unavailable" if requestable or runtime_unavailable else (live or "disconnected"),
        "runtime_supported": runtime_supported,
        "requestable": requestable,
        # byo_required: provider has no managed app, owner must bring one.
        # byo_configured: whether THIS owner has stored their own app.
        # byo_setup_url: where to register one.
        "byo_required": v.byo_required,
        "byo_configured": byo,
        "byo_setup_url": v.byo_setup_url,
        # Where a paste_token provider's key is minted, so an API-key row isn't
        # just a blank box the owner has to go hunting for.
        "token_setup_url": v.token_setup_url,
        # Placeholder for the paste box, when the provider packs two values into
        # the one input (Shopify's "<shop>|<token>"). None = the generic hint.
        "token_paste_hint": v.token_paste_hint,
        # Labelled inputs for a multi-value credential (Shopify: store domain, client
        # id, client secret). None = the single paste box.
        "paste_fields": [f.model_dump() for f in v.paste_fields] if v.paste_fields else None,
        # can_trigger: this provider's inbound event labels (registry `inbound`,
        # T1) — the Integrations card's "Can trigger" line. Empty when the
        # provider isn't (yet) wired as an Automations trigger source.
        # `automations_count` (how many routines already fire off it) is
        # deferred — it needs a per-owner routines scan, not pure registry data.
        "can_trigger": [e.label for e in v.inbound.events] if v.inbound else [],
        # inbound: the Add-flow's "A connected app" rung (T6) needs event IDS
        # (not just labels) plus whether hookup is guided_paste (owner pastes
        # the URL into the provider's own dashboard, e.g. Stripe) or auto
        # (we register the hook ourselves, e.g. GitHub — the frontend shows a
        # repo field first). Additive alongside `can_trigger` rather than
        # replacing it, so the already-shipped Integrations card DTO is
        # untouched.
        "inbound": (
            {
                "webhook_registration": v.inbound.webhook_registration,
                # Whether the DELIVERY BODY names which event fired. Multi-
                # select is gated on it: one trigger hands the agent only the
                # payload, so a provider that names its event in an HTTP
                # header (GitHub, Sentry) has to stay one event per trigger —
                # the picker renders single-select for it, and the seed 400s a
                # multi-event body. See registry.Inbound.event_in_body.
                "event_in_body": v.inbound.event_in_body,
                # Per-event `registration` overrides the block-level mode
                # (google: gmail.poll/gmail.message differ from the watch
                # events) — the frontend branches per-event on it (the
                # owner_setup topic input). None = inherit.
                "events": [
                    {
                        "id": e.id,
                        "label": e.label,
                        "registration": e.registration,
                        # Per-event override of template.starter_prompt — the
                        # draft description the create dialog pre-fills has to
                        # apply the same precedence the seed does.
                        "starter_prompt": e.starter_prompt,
                    }
                    for e in v.inbound.events
                ],
                # The registry template the seed renders server-side. The
                # create dialog shows it as an EDITABLE draft (same
                # {event_label}/{account} placeholders), so wiring an app no
                # longer means accepting boilerplate you can't rewrite. Sent
                # back as an override only when the owner actually edits it.
                "template": {
                    "title": v.inbound.template.title,
                    "starter_prompt": v.inbound.template.starter_prompt,
                },
            }
            if v.inbound
            else None
        ),
    }


async def _validate_token(p: registry.Provider, token: str) -> bool:
    if not p.validation:
        return True
    url = p.validation.url
    grant_url = p.validation.token_grant_url
    if p.validation.host_pattern:
        # Two-value paste ("<host>|<token>", Shopify): the left half builds the
        # per-store validate URL, the right half is the credential every check
        # below then applies to. Both halves must be present and the host must
        # full-match the provider's own domain pattern — this is the one validate
        # URL a caller can influence, so an unmatched host is refused, never
        # fetched.
        host, separator, token = token.partition(_PASTE_SEPARATOR)
        if not separator or not re.fullmatch(p.validation.host_pattern, host):
            return False
        url = url.format(host=host)
        grant_url = grant_url and grant_url.format(host=host)
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
            if grant_url:
                # The paste is an app's "<client_id>:<client_secret>" (Shopify Dev Dashboard):
                # mint a token with it, then run the normal check with THAT token.
                client_id, _, client_secret = token.partition(":")
                grant = await client.post(
                    grant_url,
                    data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
                )
                minted = grant.json().get("access_token") if grant.status_code < 400 else None
                if not minted:
                    return False
                headers[p.validation.auth_header] = f"{scheme} {minted}".strip() if scheme else minted
            kwargs: dict = {"headers": headers}
            if p.validation.body is not None:
                kwargs["json"] = p.validation.body
            resp = await client.request(p.validation.method, url, **kwargs)
        if resp.status_code >= 400:
            return False
        # Providers that answer 200 even for a bad token (Slack auth.test,
        # Linear GraphQL) gate validity on a body field instead of the status.
        if p.validation.success_json_field:
            return _json_path_truthy(resp, p.validation.success_json_field)
        return True
    except (httpx.HTTPError, ValueError, AttributeError):
        # ValueError/AttributeError: a grant that answered non-JSON or a non-object body.
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
    reasons = {r["provider"]: r.get("reconnect_reason") for r in rows if r.get("status") == "reconnect_required"}
    connections = []
    for k, v in registry.all_providers().items():
        # A gate still means HIDDEN unless the provider opts into being requested,
        # or this owner is already connected (never hide a live credential — they
        # need the row to revoke it).
        if not registry.is_listable(v, owner_id) and k not in connected:
            continue
        byo = await _byo_active(owner_id, k)
        row = _connection_row(k, v, connected, byo, owner_id)
        # Why the grant died (an OAuth error code or our own short code; never a token).
        row["reconnect_reason"] = reasons.get(k)
        connections.append(row)
    return {"connections": connections}


@router.post("/aws/credentials")
async def aws_credentials(body: AwsCredentialsBody, request: Request):
    """credential_process endpoint for the container helper. NO Clerk auth —
    the caller is the owner's container; identity is the per-owner bearer
    token minted at aws-complete, compared in constant time against the
    SHA-256 hash retained in the SM credential. The ExternalId never leaves
    this process. Every fetch is metered + logged (the audit trail the old
    EcsContainer design could not offer)."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_or_malformed_authorization")
    presented = auth_header[len("Bearer ") :]

    # Cheap DynamoDB status gate FIRST, so an unauthenticated spray burns a DDB
    # read rather than a Secrets Manager fetch. Both this gate and the token
    # check below fail with the SAME 401 invalid_token — a caller must not be
    # able to distinguish "owner not connected" from "wrong token".
    status_row = await integrations_repo.get(body.owner_id, "aws")
    if not status_row or status_row.get("status") != "connected":
        put_metric("connections.aws_credentials.fetch", dimensions={"result": "unauthorized"})
        raise HTTPException(status_code=401, detail="invalid_token")

    presented_hash = _hash_cli_token(presented)
    if _aws_cli_record_is_stale(body.owner_id, presented_hash):
        async with _aws_cli_refresh_locks[body.owner_id]:
            # Re-check: a concurrent request may have refreshed while this one waited.
            if _aws_cli_record_is_stale(body.owner_id, presented_hash):
                try:
                    fetched = await integration_secret_store.get(body.owner_id, "aws") or {}
                except ClientError:
                    # Secrets Manager throttling / validation errors must surface as a
                    # clean 503 (the helper falls back to its stale cache), never an
                    # unhandled 500.
                    logger.exception("aws credential secret fetch failed for owner=%s", body.owner_id)
                    put_metric("connections.aws_credentials.fetch", dimensions={"result": "store_error"})
                    raise HTTPException(status_code=503, detail="credential_service_unavailable")
                _aws_cli_records[body.owner_id] = (time.monotonic(), fetched)
    rec = _aws_cli_records[body.owner_id][1]
    if not _cli_token_matches(rec, presented_hash):
        put_metric("connections.aws_credentials.fetch", dimensions={"result": "unauthorized"})
        raise HTTPException(status_code=401, detail="invalid_token")

    # Rate limit only AUTHENTICATED vends (bounds STS calls per owner).
    if not _check_aws_cli_rate_limit(body.owner_id):
        put_metric("connections.aws_credentials.fetch", dimensions={"result": "rate_limited"})
        return Response(
            status_code=429,
            headers={"Retry-After": "60"},
            content=b'{"detail":"rate_limited"}',
            media_type="application/json",
        )

    try:
        # RoleSessionName charset is [\w+=,.@-]; owner ids (org_/user_ + base62)
        # fit. Truncate to the 64-char API limit.
        session_name = f"lightbulb-{body.owner_id}"[:64]
        resp = await run_in_thread(
            _sts_client().assume_role,
            RoleArn=rec["role_arn"],
            RoleSessionName=session_name,
            ExternalId=rec["external_id"],
            DurationSeconds=3600,
        )
    except Exception:
        logger.exception("aws credential vend failed for owner=%s", body.owner_id)
        put_metric("connections.aws_credentials.fetch", dimensions={"result": "sts_error"})
        raise HTTPException(status_code=502, detail="credential_vend_failed")

    creds = resp["Credentials"]
    put_metric("connections.aws_credentials.fetch", dimensions={"result": "ok"})
    logger.info("aws credentials vended", extra={"owner_id": body.owner_id})
    return {
        "Version": 1,
        "AccessKeyId": creds["AccessKeyId"],
        "SecretAccessKey": creds["SecretAccessKey"],
        "SessionToken": creds["SessionToken"],
        "Expiration": creds["Expiration"].isoformat(),
    }


@router.post("/{provider}")
async def connect_paste(provider: str, body: PasteBody, auth: AuthContext = Depends(get_current_user)):
    started_ms = int(time.time() * 1000)
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider, owner_id)
    if p.acquisition != "paste_token":
        raise HTTPException(status_code=400, detail=f"{provider} is not a paste-key integration")
    token = body.token
    if p.paste_fields:
        values = {f.key: (body.fields or {}).get(f.key, "").strip() for f in p.paste_fields}
        for f in p.paste_fields:
            if f.host and values[f.key]:
                raw = values[f.key] if "://" in values[f.key] else f"//{values[f.key]}"
                values[f.key] = urlsplit(raw).hostname or ""  # .hostname is already lowercased
        if missing := [f.label for f in p.paste_fields if not values[f.key]]:
            raise HTTPException(status_code=422, detail=f"{p.display_name} needs: {', '.join(missing)}")
        token = p.paste_format.format(**values)
    # A two-value provider gets its own 422. The likeliest mistake is pasting
    # only the token — which is exactly right for every other integration — and
    # the generic message below would answer that by blaming the token.
    if p.validation and p.validation.host_pattern and _PASTE_SEPARATOR not in token:
        hint = p.token_paste_hint or f"<host>{_PASTE_SEPARATOR}<token>"
        raise HTTPException(
            status_code=422,
            detail=f"{p.display_name} needs two values in one box — paste {hint}",
        )
    if not await _validate_token(p, token):
        raise HTTPException(status_code=422, detail="token failed validation against the provider")
    credential = {"token": token}
    # Paste tokens are backend-custodian: store the credential in Secrets
    # Manager (KMS-encrypted at rest) — never in DynamoDB — then deliver the
    # literal into the container. Mark the integration connected only after
    # wiring succeeds, so status never reports a false "connected". Under the lease revoke-all
    # reads under, and refused when it ran after this request began.
    async with owner_lifecycle.revoke_fence(owner_id, started_ms):
        await integration_secret_store.put(owner_id, provider, credential)
        try:
            await apply_connection.apply_connection(owner_id, provider, credential)
        except RuntimeUnsupportedError as exc:
            # M6-R31: a clear, specific answer instead of the generic apply_failed/502 --
            # this provider has no known Hermes/AgentCore equivalent yet. Same rollback as
            # the generic failure path: the row must never read connected.
            await _remove_materialized_artifacts(owner_id, p, credential)
            await integration_secret_store.delete(owner_id, provider)
            raise HTTPException(status_code=422, detail=f"RUNTIME_UNSUPPORTED: {exc}") from exc
        except Exception:
            await _remove_materialized_artifacts(owner_id, p, credential)
            await integration_secret_store.delete(owner_id, provider)
            raise HTTPException(
                status_code=502,
                detail="failed to apply the connection to your container; please retry",
            )
        await integrations_repo.put_status(owner_id, provider, "connected")
    await write_owner_facts(owner_id)  # best-effort: refresh the agents' connected_integrations manifest
    await _after_connect(owner_id, provider)
    return {"status": "connected", "provider": provider}


@router.get("/{provider}/aws-setup")
async def aws_setup(provider: str, auth: AuthContext = Depends(get_current_user)):
    """Stage the AWS read-only connect: mint (or reuse) the per-owner STS
    ExternalId and return everything the frontend needs to render the
    CloudFormation quick-create flow. Does NOT mark the integration connected —
    that is aws-complete's job, once the owner has stood up the cross-account
    role and pasted its ARN back."""
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider, owner_id)
    if p.acquisition != "aws_sts":
        raise HTTPException(status_code=400, detail=f"{provider} is not an AWS STS integration")

    # Reuse a previously-minted ExternalId so a repeated setup (or a half-finished
    # connect) keeps the SAME id the owner already baked into their CloudFormation
    # trust policy — minting a fresh one would silently break an already-created
    # role's trust. The pending record is NOT a connected status: leave the
    # integrations index untouched until aws-complete succeeds.
    rec = await integration_secret_store.get(owner_id, provider)
    external_id = (rec or {}).get("external_id")
    if not external_id:
        external_id = aws_cfn.gen_external_id()
        await integration_secret_store.put(owner_id, provider, {"external_id": external_id})

    principal_arn = aws_cfn.platform_trust_principal_arn()
    region = p.aws_sts.default_region

    def _quick_create(policy_arn: str) -> str | None:
        # Prefer the env-wired hosted template URL (CFN_TEMPLATE_URL — the
        # public-read S3 object the CDK service-stack uploads), falling back to
        # the provider's static cfn_template_url. With neither set the template
        # is unhosted, so return None rather than crash on a None template_url —
        # the frontend renders the "Create role in AWS" button disabled.
        template_url = settings.CFN_TEMPLATE_URL or p.aws_sts.cfn_template_url
        if not template_url:
            return None
        return aws_cfn.quick_create_url(
            external_id=external_id,
            principal_arn=principal_arn,
            policy_arn=policy_arn,
            region=region,
            template_url=template_url,
        )

    return {
        "external_id": external_id,
        "principal_arn": principal_arn,
        "our_account_id": bedrock_aip.OUR_ACCOUNT_ID,
        "default_region": region,
        "view_only": {
            "policy_arn": p.aws_sts.view_only_policy_arn,
            "quick_create_url": _quick_create(p.aws_sts.view_only_policy_arn),
        },
        "read_only": {
            "policy_arn": p.aws_sts.read_only_policy_arn,
            "quick_create_url": _quick_create(p.aws_sts.read_only_policy_arn),
        },
        "power_user": {
            "policy_arn": p.aws_sts.power_user_policy_arn,
            "quick_create_url": _quick_create(p.aws_sts.power_user_policy_arn),
        },
    }


@router.post("/{provider}/aws-complete")
async def aws_complete(provider: str, body: AwsCompleteBody, auth: AuthContext = Depends(get_current_user)):
    """Finish the AWS read-only connect: the owner pastes back the ARN of the
    cross-account role their CloudFormation stack created. We mint the
    container's credential-fetch bearer token, materialize the ~/.aws
    credential_process bundle, and mark the integration connected.

    SECURITY: ``role_arn`` is owner-supplied, so it is validated HERE — before
    it is stored or ever assumed — against two failure modes that would
    otherwise turn the vend endpoint into a confused deputy:
      * a malformed / wildcard ARN (the backend role's broad sts:AssumeRole
        Allow only denies our own account, so a wildcard that slips through
        could reach arbitrary external accounts), and
      * an ARN inside OUR OWN account (877352799272), which would point the
        vend-time AssumeRole back at Isol8's own infrastructure.
    The vend endpoint (POST /aws/credentials) assumes EXACTLY the stored ARN;
    this validation is the gate that keeps a bad ARN from ever being stored."""
    started_ms = int(time.time() * 1000)
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider, owner_id)
    if p.acquisition != "aws_sts":
        raise HTTPException(status_code=400, detail=f"{provider} is not an AWS STS integration")

    role_arn = body.role_arn.strip()
    match = _ROLE_ARN_RE.fullmatch(role_arn)
    if not match:
        raise HTTPException(status_code=400, detail="invalid role ARN")

    # The backend's sts:AssumeRole IAM grant is scoped to this exact role name
    # (service-stack.ts StsAssumeConnectorRole), and the quick-create template
    # pins the RoleName. Reject other names HERE so a hand-rolled role fails
    # with a clear 400 at connect time, not an AccessDenied at vend time.
    if role_arn.split(":role/", 1)[1] != CONNECTOR_ROLE_NAME:
        raise HTTPException(
            status_code=400,
            detail=f"role must be named {CONNECTOR_ROLE_NAME} (use the quick-create stack from aws-setup)",
        )

    # ``region`` is owner-supplied and gets written into ~/.aws/config. Validate
    # it (when given) before storing or using it so a newline can't inject a
    # second INI section. An empty/omitted region falls through to the default.
    if body.region and not _REGION_RE.fullmatch(body.region):
        raise HTTPException(status_code=400, detail="invalid region")

    # Fetch the pending record ONCE. A missing/setup-less record yields the 409
    # "run aws-setup first" BEFORE the trust-pin check, so an our-account ARN
    # pasted without a prior setup gets the accurate 409 (not the misleading
    # "not Isol8's" 400). The same external_id feeds the trust-pin verification.
    rec = await integration_secret_store.get(owner_id, provider)
    external_id = (rec or {}).get("external_id")
    if not external_id:
        raise HTTPException(status_code=409, detail="run aws-setup first")

    if match.group(1) == bedrock_aip.OUR_ACCOUNT_ID:
        # Our own account: allowed ONLY if the pasted role is trust-pinned to this
        # owner (trusts the platform principal + requires their ExternalId) —
        # verified against the live role via iam:GetRole. Blocks connecting an
        # arbitrary our-account role (account takeover). No owner-id allowlist.
        # Any failure -> reject.
        if not await _self_account_role_is_pinned(role_arn, owner_id, external_id):
            raise HTTPException(status_code=400, detail="role must be in your own AWS account, not Lightbulb's")

    # Mint the container's bearer token for the credential_process endpoint.
    # Only its hash is retained (SM readers must never gain a usable bearer);
    # the plaintext rides the materialize call onto the owner's EFS and
    # nowhere else.
    cli_token = secrets.token_urlsafe(32)
    credential = {
        "role_arn": role_arn,
        "external_id": external_id,
        "region": body.region or p.aws_sts.default_region,
        "cli_token_hash": _hash_cli_token(cli_token),
    }
    # Overwrite the pending {external_id} record with the full credential
    # (retained for the vend endpoint + reconcile). Then materialize -> mark
    # connected, with the status row written ONLY after a clean apply so the
    # integration never reports a false "connected". No IAM grant step: the
    # backend's own role performs the AssumeRole at vend time.
    # Under the lease revoke-all reads under; refused when it ran after this request began.
    async with owner_lifecycle.revoke_fence(owner_id, started_ms):
        await integration_secret_store.put(owner_id, provider, credential)
        try:
            await apply_connection.apply_connection(owner_id, provider, {**credential, "cli_token": cli_token})
            await integrations_repo.put_status(owner_id, provider, "connected")
            await write_owner_facts(owner_id)  # best-effort: refresh the agents' connected_integrations manifest
        except Exception:
            # Best-effort rollback: a failed connect must leave no on-disk ~/.aws
            # bundle. Each teardown step is wrapped independently so one failure
            # doesn't skip the others. Rather than deleting the whole secret,
            # RE-PUT the pending {external_id} record: the owner has already baked
            # this ExternalId into their CloudFormation trust policy, and aws-setup
            # is documented to reuse it — dropping it would silently break the
            # already-created role on a retry. Then always re-raise as a 5xx.
            try:
                await _remove_materialized_artifacts(owner_id, p, credential)
            except Exception:
                logger.warning(
                    "aws-complete rollback: failed to remove materialized artifacts for owner=%s",
                    owner_id,
                    exc_info=True,
                )
            try:
                await integration_secret_store.put(owner_id, provider, {"external_id": external_id})
            except Exception:
                logger.warning(
                    "aws-complete rollback: failed to reset pending secret for owner=%s",
                    owner_id,
                    exc_info=True,
                )
            raise HTTPException(
                status_code=502,
                detail="failed to apply the AWS connection to your container; please retry",
            )
    await _after_connect(owner_id, provider)
    return {"status": "connected", "provider": provider}


async def _after_connect(owner_id: str, provider: str) -> None:
    """Once a service reads connected: tell an open Activity view to drop a stale Reconnect item
    (``access_token._announce_reconnect`` filed it; the item closes when the row flips back), and
    wake the idea starts waiting on it. Best-effort and outside the revoke fence: a slow Paperclip
    must not spend the lease, and a connect never fails because a nudge did."""
    try:
        from core.services.live_events import publish_owner_users
        from core.services.messaging import make_live_event

        await publish_owner_users(owner_id, make_live_event("runtime.resource.invalidated", {"resource": "activity"}))
    except Exception:
        logger.warning("activity invalidation after connect failed owner=%s provider=%s", owner_id, provider)
    try:
        await ideas.resume_after_connect(owner_id, provider)
    except Exception:
        logger.warning("ideas: resume after connect failed owner=%s provider=%s", owner_id, provider, exc_info=True)


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


async def _reseed_surviving_google_assignments(owner_id: str) -> None:
    """Re-project every assignment that SURVIVES an account-level google change.

    On AgentCore the binding lives as ``GOG_ACCOUNT`` in each agent's profile
    ``cli-env.json``, and that file is owned by the PROVIDER-keyed projection
    (``credentials/hermes_cli``): a connect rewrites it wholesale from the gog keyring
    password, and a disconnect deletes it outright. Either therefore strips the binding of
    an agent the change never named — disconnect account A and an agent bound to B loses B,
    with no row removed to drive a re-seed. Re-seeding the survivors is what puts it back.

    Idempotent and cheap on ECS, where the seeder skips the write when the block is already
    current. Best-effort per agent: the authoritative keyring/inventory change has already
    committed."""
    for agent_id in await google_accounts_repo.list_assignments(owner_id):
        await _seed_identity_best_effort(owner_id, agent_id)


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
        # The keyring lives in every Hermes profile. Running gog against a scratch copy
        # of it is what keeps the owner's OTHER Google accounts (the founder has four)
        # when one is disconnected.
        await hermes_cli.clear(owner_id, p.key, p.wiring.materializer or "", {"email": account})
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
    # The projection's cli-env.json is provider-keyed, so this disconnect also wiped the
    # GOG_ACCOUNT of agents bound to the accounts that REMAIN. Put those back.
    await _reseed_surviving_google_assignments(owner_id)


#: Providers whose registry row is gone but whose stored credentials may not be. Only these keys
#: get the tolerant teardown below. Empty since ``shopify`` came back as a client-credentials
#: provider (its old container_login rows now take the normal teardown).
RETIRED_PROVIDERS: frozenset[str] = frozenset()


@router.delete("/{provider}")
async def disconnect(provider: str, account: str | None = None, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    # Teardown deliberately skips the availability gate — see
    # _get_provider_for_teardown. Revocation must outlive a gate change.
    try:
        p = _get_provider_for_teardown(provider)
    except HTTPException:
        if provider not in RETIRED_PROVIDERS:
            raise
        # The provider ROW is gone from the registry, but owners who connected before it was
        # deleted still hold our copies -- and revocation must outlive the provider definition
        # (same reasoning as the availability gate above; a credential the UI cannot delete is
        # the worse state). Named explicitly rather than "any unknown key", so a typo'd or
        # probed provider is still a 404.
        await integration_secret_store.delete(owner_id, provider)
        await integrations_repo.delete(owner_id, provider)
        await write_owner_facts(owner_id)  # best-effort: refresh the agents' connected_integrations manifest
        return {"status": "disconnected", "provider": provider}
    # ?account= scopes the disconnect to one Google account. It is Google-specific
    # (one provider, many accounts), so reject it on any other provider rather
    # than silently honoring it.
    if account is not None:
        if provider != "google":
            raise HTTPException(status_code=400, detail="the account parameter is only supported for google")
        await _disconnect_one_google_account(owner_id, p, account)
        return {"status": "disconnected", "provider": provider, "account": account}
    # Serialize the teardown against an in-flight connect: the OAuth callback
    # takes the same per-owner guard around its write section, so whichever runs
    # second sees the other's completed state and a disconnect can no longer be
    # undone by a callback still finishing its writes. Cross-process (a DynamoDB
    # lease) and per-OWNER now, not per-(owner, provider) -- a disconnect
    # also serializes against a concurrent provision or resize for the same
    # owner, which is deliberate: that race is the same bug class the old
    # per-provider lock was too narrow to catch.
    try:
        async with owner_lifecycle_guard(owner_id):
            await _disconnect_teardown(owner_id, provider, p)
    except OwnerLifecycleBusyError:
        # Nothing is written before the guard, so this is a clean no-op --
        # tell the caller to retry rather than surface a raw 500.
        raise HTTPException(status_code=409, detail="busy")
    if provider == "github":
        # The workspace token is gone, so Paperclip's copy goes too. After the guard, not
        # inside it -- see the connect twin in oauth_callback. A failed cleanup is retried in
        # the background, then swept at the next backend start.
        await paperclip_github_token.sync_after_disconnect(owner_id)
    return {"status": "disconnected", "provider": provider}


async def _disconnect_teardown(owner_id: str, provider: str, p: registry.Provider) -> None:
    if provider == "aws":
        _aws_cli_records.pop(owner_id, None)
    # A pure CLI-skill materializer (gh_cli/xurl_cli/skill_cli/instagram_cli/
    # gog_cli/aws_cli) has no Hermes equivalent and is skipped outright; an
    # MCP-shaped one (mcp_oauth/qbo_dotenv) needs no remover call either — its
    # mcp-tokens/*.json cleanup happens inside reconcile_integrations below,
    # once the row this function deletes is gone from the connected set.
    if p.wiring.materializer:
        # Symmetric with the connect projection (credentials.hermes_cli): run the materializer's
        # own remover against a scratch copy of the profile state and sync the result, so a
        # provider-level disconnect drops exactly what it owns. google carries the same
        # disconnect_all + inventory the ECS branch below builds.
        if hermes_cli.supports(p.wiring.materializer):
            cred = await integration_secret_store.get(owner_id, provider) or {}
            if provider == "google":
                cred = {
                    **cred,
                    "disconnect_all": True,
                    "accounts": [a["email"] for a in await google_accounts_repo.list_accounts(owner_id)],
                }
            await hermes_cli.clear(owner_id, provider, p.wiring.materializer, cred)
        else:
            logger.info(
                "agentcore: skipping %s materializer teardown for owner=%s — no Hermes projection for this rail",
                provider,
                owner_id,
            )
    # aws_sts revocation is server-side: deleting the secret below drops the
    # cli_token_hash, so the container's next credential fetch 401s. Already-
    # vended sessions expire within 1h (DurationSeconds cap).
    # Drop the retained secret (no-op for actor providers, which never stored
    # one) and the status row, then blank any per-agent openclaw.json slots.
    await integration_secret_store.delete(owner_id, provider)
    await integrations_repo.delete(owner_id, provider)
    await write_owner_facts(owner_id)  # best-effort: refresh the agents' connected_integrations manifest
    # NB: a DCR provider's cached public client in oauth_app_store is deliberately
    # LEFT in place on disconnect. start() re-registers it (overwriting in place)
    # when the requested scope set changes, so reconnect picks up new scopes without
    # a delete+recreate that races AWS's async secret-name release.
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
    # MCP/OAuth-shaped: re-project every REMAINING connected integration into
    # every Hermes profile in one shot — the row this function just deleted is
    # already gone from the connected set, so it drops out of the rebuilt
    # mcp_servers block. A CLI-skill materializer never touched a profile, so
    # there's nothing to refresh.
    if apply_connection.is_mcp_shaped(p):
        try:
            await reconcile_integrations(owner_id)
        except Exception:  # noqa: BLE001 — a disconnect must never fail because the refresh did
            logger.exception(
                "hermes: profile refresh after disconnect failed for owner=%s provider=%s",
                owner_id,
                provider,
            )
    elif p.wiring.openclaw_paths:
        # M6-R31: reconcile_integrations has no fan-out for an openclaw_paths
        # provider at all (mcp_servers/.env only) — clear this one's own
        # projection (env value + config.yaml selector) directly instead.
        await apply_connection.clear_hermes_openclaw_paths_projection(owner_id, provider)


@router.put("/{provider}/byo-app")
async def put_byo_app(
    provider: str,
    body: ByoAppRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Store the owner's OWN OAuth app for a byo-required provider. Once stored,
    `_byo_active` returns True for this owner — the prerequisite for connecting a
    BYO-required provider via their own app."""
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider, owner_id)
    if not p.byo_required:
        raise HTTPException(status_code=400, detail=f"{provider} does not support a bring-your-own app")
    if "•" in body.client_id or "•" in body.client_secret:  # the form's masked display value
        raise HTTPException(
            status_code=400,
            detail="Paste the real client secret — this looks like the masked value shown after saving.",
        )
    await oauth_app_store.put(
        owner_id,
        provider,
        client_id=body.client_id,
        client_secret=body.client_secret,
    )
    return {"status": "stored"}


@router.delete("/{provider}/byo-app")
async def delete_byo_app(
    provider: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Clear the owner's stored BYO OAuth app. This removes only the app
    credentials — it does NOT disconnect an active connection."""
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    # Mirror put_byo_app's guards so DELETE on an unknown or non-BYO provider
    # 4xxs instead of silently no-opping (a clear signal the route is wrong,
    # not a request that quietly succeeded against a provider that never had a
    # BYO app to clear).
    p = _get_provider_or_404(provider, owner_id)
    if not p.byo_required:
        raise HTTPException(status_code=400, detail=f"{provider} does not support a bring-your-own app")
    await oauth_app_store.delete(owner_id, provider)
    return {"status": "cleared"}


def _done(query: str, origin: str | None = None) -> RedirectResponse:
    # iOS's authentication session closes on our registered callback scheme.
    # Desktop uses a web landing page because it opens the system browser.
    if origin == "ios":
        status = "connect_failed" if query.startswith("error=") else "connected"
        return RedirectResponse(f"flickmeon://checkout_redirect/{status}?{query}")
    if origin == "app":
        status = "connect_failed" if query.startswith("error=") else "connected"
        return RedirectResponse(f"{settings.FRONTEND_URL}/checkout_redirect/{status}?{query}")
    # Public landing page (outside the frontend's Clerk matcher): the browser
    # finishing the dance may be the owner's own tab OR the agent's container
    # Chrome, which has no Clerk session (see authorize_from_container). The
    # page itself bounces a signed-in viewer on to /settings/connections.
    return RedirectResponse(f"{settings.FRONTEND_URL}/oauth/done?{query}")


def _redirect_uri(provider: str) -> str:
    # Server-derived, never caller-supplied. Must match the URI registered with
    # the provider and be identical in both authorize + callback legs.
    return f"{settings.PUBLIC_API_BASE}/api/v1/oauth/{provider}/callback"


@router.get("/{provider}/authorize")
async def authorize(
    provider: str,
    return_to: Literal["web", "app", "ios"] = "web",
    auth: AuthContext = Depends(get_current_user),
):
    require_org_admin(auth)
    # Origin is stored on the OAuth state row, never trusted from the callback.
    return await _start_oauth(resolve_owner_id(auth), provider, origin=return_to if return_to != "web" else None)


@router.get("/u/{owner_id}/{provider}/authorize")
async def authorize_from_container(owner_id: str, provider: str, request: Request):
    """Container-authed sibling of ``authorize``: the owner's agent mints the
    consent URL and opens it in its own (container) Chrome, and the owner
    signs in through the Browser panel — same rails end to end (same owner-
    scoped gating, same state record, same callback, same materializers).

    Auth is the per-container ``hook_token``, compared timing-safely against
    the row of the owner NAMED IN THE PATH (``_require_hook_token``), so a
    container can only mint for its own owner. There is no org-admin gate
    here — whoever drives the container's chat can put a consent screen in
    front of the owner — which is why a live connection is refused outright:
    the agent may start a missing connection, never silently rebind one.

    That rule is enforced twice: here at mint time (409), and again in the
    callback at apply time — the state row carries ``origin="container"`` and
    the callback drops a container-originated credential if the provider went
    live in between (the owner connected via the tab during the state TTL).
    Owner-started flows are untouched: Google's add-another-account relies on
    connecting while already connected.
    """
    from core.services.container_auth import _require_hook_token  # local import, as in marketplace_listings

    await _require_hook_token(owner_id, request)
    if await _is_connected(owner_id, provider):
        raise HTTPException(status_code=409, detail=f"{provider} is already connected")
    return await _start_oauth(owner_id, provider, origin="container")


async def _is_connected(owner_id: str, provider: str) -> bool:
    row = await integrations_repo.get(owner_id, provider)
    return bool(row) and row.get("status", "connected") == "connected"


async def _start_oauth(owner_id: str, provider: str, *, origin: str | None = None) -> dict:
    p = _get_provider_or_404(provider, owner_id)
    if p.acquisition != "direct_oauth":
        raise HTTPException(status_code=400, detail=f"{provider} is not an OAuth integration")
    if not apply_connection.runtime_supported(p):
        raise HTTPException(status_code=503, detail=f"{p.display_name} is not available on this runtime yet")
    if p.byo_required and not await _byo_active(owner_id, provider):
        raise HTTPException(status_code=400, detail=f"Set up your own {p.display_name} app before connecting")
    # The OAuth dance hits the provider's endpoints (incl. Dynamic Client
    # Registration). A 4xx/5xx there — e.g. a provider rejecting our DCR request —
    # must surface as a clean, retryable error, not an uncaught 500 that reads as a
    # crash in a demo. The callback leg already degrades the same way.
    try:
        url = await oauth_dance.start(owner_id, provider, redirect_uri=_redirect_uri(provider), origin=origin)
    except ValueError:
        # No usable OAuth client — e.g. a platform-app provider (instagram) whose
        # shared client hasn't been seeded into Secrets Manager for this env yet
        # (_oauth_client raises). That's an operator gap, not a user error: return
        # a clean 503 the frontend can show as "not available yet" instead of a 500.
        raise HTTPException(
            status_code=503,
            detail=f"{p.display_name} isn't available to connect yet; please try again later.",
        )
    except httpx.HTTPError as exc:
        # Log what the provider said: Intercom's DCR 502'd in prod with nothing
        # but the access-log line, so the refusal reason was unknowable.
        response = exc.response if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning(
            "oauth start failed provider=%s url=%s status=%s body=%r",
            provider,
            response.request.url if response is not None else None,
            response.status_code if response is not None else None,
            response.text[:500] if response is not None else repr(exc),
        )
        raise HTTPException(
            status_code=502,
            detail=f"Couldn't start the {p.display_name} connection; please try again.",
        )
    return {"authorize_url": url}


# Public callback — the OAuth provider redirects the (unauthenticated) browser here.
# Identity comes from the server-side state record, NOT from the request. Mounted at /api/v1/oauth/...
# No org-admin gate here: the single-use `state` it consumes is owner-scoped by
# construction — minted only by `authorize` (Clerk + org-admin) or by
# `authorize_from_container` (the owner's own container, hook-token-gated), and
# the row pins the owner/provider the credential lands on.
oauth_router = APIRouter(prefix="/oauth", tags=["connections"])


@oauth_router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    state: str,
    code: str | None = None,
    realmId: str | None = None,  # noqa: N803 - Intuit's query param name
):
    # Consume once before either the denial or exchange path, retaining the
    # trusted return destination even if the token request fails.
    rec = await oauth_state_repo.consume(state)
    if not rec:
        return _done("error=oauth_state")
    origin = rec.get("origin")
    # RFC 6749 error redirect: the provider bounced the authorize (?error=
    # access_denied|invalid_scope|…&state=…) so no code was issued. Land back on
    # the connections page instead of 422ing raw JSON in the user's browser; the
    # provider's error code is in the access log.
    if not code:
        return _done("error=oauth_provider", origin)
    try:
        owner_id, prov, credential, origin = await oauth_dance.exchange(
            rec, code, redirect_uri=_redirect_uri(provider), realm_id=realmId
        )
    except ValueError:
        return _done("error=oauth_state", origin)
    except httpx.HTTPError as exc:
        # With a BYO app the owner's own client_id/secret is user input: a wrong
        # secret or a misconfigured app makes the provider's token endpoint
        # return 4xx, which exchange()'s raise_for_status turns into an
        # HTTPError. The single-use state is already consumed, so land back on
        # the connections page with a retryable error instead of a 500.
        #
        # LOG THE PROVIDER'S OWN ERROR. Without this the only evidence a failed
        # connect leaves is `?error=oauth_exchange` plus a generic "check your
        # client id and secret" banner, which is wrong often enough to send you
        # hunting the credential when the real cause is the request shape. The
        # body is the provider's OAuth error object ({"error": "invalid_client"}
        # etc.) — no token is present on a failure, and it is truncated in case a
        # provider echoes the request back.
        body = ""
        resp = getattr(exc, "response", None)
        if resp is not None:
            body = f" status={resp.status_code} tid={resp.headers.get('intuit_tid', '-')} body={resp.text[:300]!r}"
        logger.warning("oauth exchange failed for provider=%s:%s", provider, body)
        return _done("error=oauth_exchange", origin)
    p = registry.get_provider(prov)
    # Serialize the connect writes against a concurrent disconnect (both take the
    # same per-owner guard), so a disconnect landing mid-connect cannot be undone
    # by this callback finishing its writes afterwards. Taken AFTER the exchange
    # network call, never around it.
    try:
        async with owner_lifecycle_guard(owner_id):
            # Revoke everything ran after this connect started (its code may already have been
            # exchanged): land nothing. Judged under the lock revoke-all's teardown takes, from
            # the stored state's mint time, never the request.
            if await owner_lifecycle.revoked_since(owner_id, rec):
                return _done("error=revoked", origin)
            # Agent-started flow landing on a provider that went live in between
            # (the owner connected via the tab during the state TTL): drop it. The
            # agent may start a missing connection, never rebind a live one — judged
            # here, under the same lock the writes take, not at mint time.
            if origin == "container" and await _is_connected(owner_id, prov):
                return _done("error=already_connected", origin)
            # Retain the credential in Secrets Manager only when the backend is the
            # custodian. Actor-refresh providers (e.g. gog) self-refresh inside the
            # container, so the backend stores nothing and the EFS keyring written by
            # apply_connection is the sole durable copy. Mark connected only after
            # wiring succeeds.
            custody = await _connect_custody(owner_id, p)
            row_hint = {"custody": custody} if custody else None
            if custody:
                # Carried on the credential so every materialization (this one, a reprovision
                # from the retained secret) writes the access-token-free form.
                credential = {**credential, "custody": custody}
            if _retains_secret(p, row_hint):
                await integration_secret_store.put(owner_id, prov, credential)
            try:
                connection_identity = await apply_connection.apply_connection(owner_id, prov, credential)
            except Exception:
                await _remove_materialized_artifacts(owner_id, p, credential)
                if _retains_secret(p, row_hint):
                    await integration_secret_store.delete(owner_id, prov)
                return _done("error=apply_failed", origin)
            # Secret-free metadata from the id_token (Microsoft: account + tenant).
            await integrations_repo.put_status(
                owner_id,
                prov,
                "connected",
                identity=credential.get("account"),
                tenant_id=credential.get("tenant_id"),
                **({"custody": custody} if custody else {}),
            )
            await write_owner_facts(owner_id)  # best-effort: refresh the agents' connected_integrations manifest
    except OwnerLifecycleBusyError:
        # The OAuth state is already consumed at this point, so nothing is
        # written before the guard -- land back on the connections page with a
        # retryable error instead of a raw 500.
        return _done("error=busy", origin)
    await _after_connect(owner_id, prov)
    if prov == "github":
        # Hand Paperclip the same token (its GITHUB_TOKEN company secret), so the PRs and
        # issues a task mentions resolve authenticated. Best-effort and AFTER the guard: the
        # lease is what a concurrent disconnect or provision waits on (30 s budget), and a slow
        # Paperclip must not spend it. An overlapping disconnect is handled inside the sync,
        # which re-reads the token right before and right after its write (see
        # ``sync_github_token``); every reconcile re-asserts it too.
        await paperclip_github_token.sync_best_effort(owner_id)
    # Google supports multiple accounts under one provider-level "connected"
    # status: each connect adds a distinct account to the owner's inventory so an
    # agent can later be bound to a specific one. The account identity is the
    # email that apply_connection surfaced from the gog keyring seed.
    if prov == "google" and connection_identity:
        await google_accounts_repo.add_account(owner_id, connection_identity)
        # This connect rewrote every profile's cli-env.json from the projection's own keys,
        # dropping each agent's GOG_ACCOUNT. Re-seed the existing assignments.
        await _reseed_surviving_google_assignments(owner_id)
        await rules_builtins.on_google_connect(owner_id)  # invite-prep built-in (§18), behind the switch
    return _done(f"connected={prov}", origin)
