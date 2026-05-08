"""Teams BFF — Agents and Runs.

Spec §5: every mutating call synthesizes the openclaw_gateway adapter
config server-side. The body schemas (``CreateAgentBody`` /
``PatchAgentBody``) forbid extra keys, so client-supplied
``adapterType`` / ``adapterConfig`` / ``url`` / ``headers`` return 422
at the FastAPI boundary.

This module also defines the shared ``_ctx`` Depends helper (and the
indirection helpers it leans on — ``_admin``, ``_repo``,
``_gateway_url_for_env``, ``_decrypt_service_token``) that the rest of
the Teams BFF (Tasks 7-12) imports.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.containers import get_ecs_manager, get_gateway_pool
from core.encryption import decrypt  # noqa: F401 — re-exported for tests + downstream tasks.
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import service_token
from core.services.paperclip_adapter_config import (
    OPENCLAW_GATEWAY_TYPE,
    synthesize_openclaw_adapter,
)
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_openclaw_pairing import pair_with_openclaw
from core.services.paperclip_provisioning import _ws_gateway_url  # noqa: F401 — re-exported for downstream tasks.
from core.services.paperclip_user_session import get_user_session_cookie

from . import deps as deps_mod
from .deps import TeamsContext, envelope_list
from .schemas import CreateAgentBody, PatchAgentBody

logger = logging.getLogger(__name__)

router = APIRouter()


# Adapter fields are synthesized server-side and must never reach the
# browser. ``adapterConfig`` carries the user's own service token in
# ``authToken``; ``adapterType`` is invariant (``openclaw_gateway`` for
# everyone) and conveys nothing useful to the UI. Detail panels in the
# frontend currently render ``JSON.stringify(<BFF response>)``, so a leak
# at the BFF boundary becomes a token-in-DOM exposure. Strip on every
# read path as defense in depth — the frontend should never need these.
_AGENT_REDACTED_FIELDS = ("adapterConfig", "adapterType")


def _redact_agent(agent):
    """Strip adapter-related fields from an agent record before returning to the browser.

    Defense in depth: the BFF synthesizes adapter config from server-side
    state; the browser never names or sees it. Stripping on read prevents
    a current/future panel that pretty-prints the agent dict from leaking
    ``authToken`` (the user's own service token) into the DOM.

    Returns the input unchanged if it isn't a dict (so the function is
    safe to call on any upstream payload shape).
    """
    if not isinstance(agent, dict):
        return agent
    return {k: v for k, v in agent.items() if k not in _AGENT_REDACTED_FIELDS}


# --- Indirection helpers (Tasks 7-12 import these) ---
#
# Each helper is a free function so unit tests can monkeypatch them
# without touching the FastAPI Depends graph. The underlying behavior
# stays trivial; the goal is to keep the security-critical wiring
# (adapter synthesis) easy to mock in isolation.


# Module-level shared httpx client. Created lazily on first use, reused
# for the lifetime of the process. Not closed explicitly — the OS reclaims
# socket FDs at process exit and the connection pool releases idle
# connections via httpx's own internal keepalive logic. The previous
# implementation built a fresh ``httpx.AsyncClient`` per request without
# closing it, which leaks a file descriptor + connection-pool task on
# every call. Tasks 7-12 reuse this helper, so fixing once here applies
# fleet-wide.
_shared_http_client: httpx.AsyncClient | None = None
_shared_admin: PaperclipAdminClient | None = None


def _admin() -> PaperclipAdminClient:
    """Return a process-wide shared admin client.

    Lazily constructs a single ``httpx.AsyncClient`` + ``PaperclipAdminClient``
    on first call and reuses them for every subsequent request. This
    avoids the FD/connection-pool leak the per-request construction had.

    Tests monkeypatch this function (``monkeypatch.setattr(agents_mod,
    "_admin", lambda: admin)``) so the singleton state never gets touched
    in the unit suite.
    """
    global _shared_http_client, _shared_admin
    if _shared_admin is None:
        _shared_http_client = httpx.AsyncClient(
            base_url=settings.PAPERCLIP_INTERNAL_URL,
            timeout=15.0,
        )
        _shared_admin = PaperclipAdminClient(http_client=_shared_http_client)
    return _shared_admin


def _repo() -> PaperclipRepo:
    """Build the paperclip-companies DDB repo.

    Short table name — ``core.dynamodb.get_table`` prepends the
    ``isol8-{env}-`` prefix. See PR #414 review F1.
    """
    return PaperclipRepo(table_name="paperclip-companies")


def _gateway_url_for_env() -> str:
    """Resolve the WS gateway URL for the running environment.

    Pure function — reads ``settings.ENVIRONMENT`` only. Mocked in tests.
    """
    return _ws_gateway_url(settings.ENVIRONMENT or "")


async def _decrypt_service_token(user_id: str) -> str:
    """Look up the user's paperclip-companies row and decrypt the
    Fernet-encrypted OpenClaw service token.

    Async because the DDB read goes through ``run_in_thread`` under the
    hood. Tests monkeypatch with an ``AsyncMock``.
    """
    company = await _repo().get(user_id)
    if company is None:
        raise RuntimeError(f"no paperclip company row for user {user_id}")
    return decrypt(company.service_token_encrypted)


def _creator_user_id(ctx) -> str:
    """Return the creator's Clerk user_id (used as the adapterConfig
    ``sessionKey`` fallback for non-issue runs).

    Tolerates both real ``TeamsContext`` (flat ``user_id`` field) and
    ``MagicMock`` test fixtures that carry the auth on ``ctx.auth.user_id``
    — the pairing tests use the latter shape.
    """
    auth = getattr(ctx, "auth", None)
    if auth is not None:
        uid = getattr(auth, "user_id", None)
        if isinstance(uid, str):
            return uid
    return ctx.user_id


def _resolve_owner_id(ctx) -> str:
    """Container owner: org_id when the user is in an org, else user_id.

    The OpenClaw container is keyed by owner — for org members the owner
    is the org_id, NOT the member's user_id. Personal users are their
    own owner. Mirrors ``core.auth.resolve_owner_id`` but works on the
    Teams ``ctx`` shape (which already has ``owner_id`` for the
    production path) plus the ``MagicMock`` ctx the pairing tests use
    (which exposes ``ctx.auth.org_id`` / ``ctx.auth.user_id``).
    """
    auth = getattr(ctx, "auth", None)
    if auth is not None:
        org_id = getattr(auth, "org_id", None)
        if isinstance(org_id, str):
            return org_id
        uid = getattr(auth, "user_id", None)
        if isinstance(uid, str):
            return uid
    # TeamsContext production path — owner_id is computed in
    # ``deps.resolve_teams_context`` via ``core.auth.resolve_owner_id``.
    return getattr(ctx, "owner_id", None) or ctx.user_id


async def _user_session(ctx) -> str:
    """Return the user-session cookie for ``ctx``.

    Thin indirection so the pairing tests can monkeypatch the I/O without
    re-implementing the full ``_ctx`` dependency chain. The production
    path resolves the cookie in ``deps.resolve_teams_context``; this
    helper just reads it back off the ``TeamsContext``.
    """
    return ctx.session_cookie


async def _service_token_for(ctx) -> str:
    """Return the user's decrypted OpenClaw service token.

    Wrapper around ``_decrypt_service_token`` keyed by the creator's
    user_id. Pairing tests monkeypatch this helper directly so they can
    skip the DDB read entirely.
    """
    return await _decrypt_service_token(_creator_user_id(ctx))


async def _container_is_reachable(
    owner_id: str,
) -> tuple[bool, str | None, str | None]:
    """Return (reachable, ip, gateway_token) for the owner's container.

    Uses ``ecs_manager.resolve_running_container`` to discover the
    container's task IP via ECS DescribeTasks (we do NOT store
    ``private_ip`` on the container DDB row — it's discovered fresh).
    The gateway_token IS stored on the DDB row.

    Reachable means: container row exists in provisioning/running
    status, ECS reports a running task, and a gateway_token is
    present. We don't ping the gateway — pair_with_openclaw will
    fail gracefully if the task isn't actually serving yet, and the
    next reprovision will pick up the new agent via list_agents.
    """
    try:
        ecs = get_ecs_manager()
        container, ip = await ecs.resolve_running_container(owner_id)
    except Exception:  # noqa: BLE001 — defensive, treat as unreachable.
        # Defensive: in unit-test contexts without AWS creds, ECS calls
        # raise. Treat as not reachable; pairing falls back to next
        # reprovision via list_agents.
        return False, None, None
    if container is None or not ip:
        return False, None, None
    token = container.get("gateway_token")
    if not token:
        return False, None, None
    return True, ip, token


async def _resolve_user_email(user_id: str) -> str:
    """Fetch the user's primary email from Clerk.

    Used as the ``clerk_email_resolver`` callback for the user-session
    sign-in. Inline rather than living in ``clerk_admin`` because the
    "primary-email-from-id" idiom is only used here; if a third caller
    wants it we can promote it.
    """
    from core.services.clerk_admin import get_user

    user = await get_user(user_id)
    if not user:
        raise RuntimeError(f"clerk has no user with id {user_id}")
    primary_id = user.get("primary_email_address_id")
    for entry in user.get("email_addresses") or []:
        if entry.get("id") == primary_id:
            email = entry.get("email_address")
            if email:
                return email
    # Fall back to the first email if the primary id pointer is unset.
    addrs = user.get("email_addresses") or []
    if addrs and addrs[0].get("email_address"):
        return addrs[0]["email_address"]
    raise RuntimeError(f"clerk user {user_id} has no email address")


async def _ctx(auth: AuthContext = Depends(get_current_user)) -> TeamsContext:
    """Resolve the Teams BFF context for the current request.

    Wires together: Clerk auth -> paperclip-companies row lookup ->
    per-user Better Auth sign-in -> ``TeamsContext`` for the handler.
    Using ``deps_mod.resolve_teams_context`` (rather than a direct
    import) so test code that monkeypatches the deps module Just Works.

    The 409 / 202 / 503 status codes from ``resolve_teams_context``
    propagate as-is. After Task 9, this function does NOT autoprovision
    on the no-DDB-row branch — provisioning is owned by the canonical
    ``/container/status`` trigger (Task 8) which polls every 3s during
    pre-ready state. A 409 here therefore indicates an operator-level
    error rather than a self-heal opportunity.
    """

    async def session_factory(user_id: str) -> str:
        return await get_user_session_cookie(
            user_id=user_id,
            repo=_repo(),
            admin_client=_admin(),
            clerk_email_resolver=_resolve_user_email,
        )

    return await deps_mod.resolve_teams_context(
        auth=auth,
        repo=_repo(),
        session_factory=session_factory,
    )


# --- Routes ---


@router.get("/agents")
async def list_agents(ctx: TeamsContext = Depends(_ctx)):
    """List agents in the caller's company.

    Redacts ``adapterConfig`` + ``adapterType`` per-item before returning
    — see ``_redact_agent`` for rationale.
    """
    upstream = await _admin().list_agents(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    envelope = envelope_list(upstream, "agents")
    return {"agents": [_redact_agent(a) for a in envelope["agents"]]}


@router.post("/agents")
async def create_agent(body: CreateAgentBody, ctx: TeamsContext = Depends(_ctx)):
    """Create an agent in Paperclip and pair it with the owner's
    OpenClaw container.

    Spec §5 security invariant: the ``adapter_type`` + ``adapter_config``
    fields passed to the admin client are synthesized HERE, server-side,
    using ``synthesize_openclaw_adapter``. The client body never carries
    them — ``CreateAgentBody`` rejects extra keys with 422.

    After Paperclip's create succeeds, we call ``pair_with_openclaw`` to
    register the new agent in the OWNER's running container (org_id for
    org members, user_id for personal users) so ``/chat`` sees the same
    agent. If the container isn't reachable (provisioning in progress
    or scaled to zero), pairing is silently skipped — the next container
    provision will pick up the new agent via ``list_agents``. Pairing
    failures are non-fatal for the same reason.
    """
    creator_user_id = _creator_user_id(ctx)
    owner_id = _resolve_owner_id(ctx)
    # Mint the service token with owner_id (NOT the creator's user_id).
    # The OpenClaw container is keyed by owner_id (org_id for org members,
    # user_id for personal owners). The WS Lambda authorizer puts service
    # tokens into the connection context with ``orgId=""`` and routes by
    # the JWT ``sub`` claim — so a token minted with the member's user_id
    # would route subsequent Paperclip wakes to the member's PERSONAL
    # owner instead of the org's shared owner, missing the container the
    # agent was just paired into. We mint fresh here rather than reading
    # the persisted ``service_token_encrypted`` row so legacy rows with a
    # member-keyed sub can't poison new agents.
    minted_token = service_token.mint(owner_id)
    adapter_config = synthesize_openclaw_adapter(
        gateway_url=_gateway_url_for_env(),
        service_token=minted_token,
        user_id=creator_user_id,
    )
    paperclip_agent = await _admin().create_agent(
        session_cookie=await _user_session(ctx),
        company_id=ctx.company_id,
        name=body.name,
        role=body.role,
        adapter_type=OPENCLAW_GATEWAY_TYPE,
        adapter_config=adapter_config,
        title=body.title,
        capabilities=body.capabilities,
        reports_to=body.reports_to,
        budget_monthly_cents=body.budget_monthly_cents,
    )

    # Pair with the owner's OpenClaw container if reachable. Failure is
    # non-fatal — the next container provision picks the agent up via
    # ``fetch_paperclip_agents_for_owner``. ``get_gateway_pool()`` itself
    # touches process-wide state (a singleton lazily wired to the AWS
    # Management API client) so we resolve it inside the try block: if
    # the env isn't fully wired (e.g. unit tests, CI without
    # ``WS_MANAGEMENT_API_URL``) we still surface a clean "deferred"
    # warning instead of a 500 from the route.
    reachable, ip, token = await _container_is_reachable(owner_id)
    if reachable and ip and token:
        try:
            gateway_pool = get_gateway_pool()
            await pair_with_openclaw(
                owner_id=owner_id,
                paperclip_agent=paperclip_agent,
                gateway_pool=gateway_pool,
                ip=ip,
                token=token,
            )
        except Exception as exc:  # noqa: BLE001 — pairing failure is deferred.
            logger.warning(
                "teams.create_agent: pair_with_openclaw failed for owner=%s agent=%s — deferring to next provision: %s",
                owner_id,
                paperclip_agent.get("id"),
                exc,
            )

    return paperclip_agent


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single agent by id.

    Redacts ``adapterConfig`` + ``adapterType`` before returning — see
    ``_redact_agent``.
    """
    upstream = await _admin().get_agent(
        agent_id=agent_id,
        session_cookie=ctx.session_cookie,
    )
    return _redact_agent(upstream)


@router.patch("/agents/{agent_id}")
async def patch_agent(
    agent_id: str,
    body: PatchAgentBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch an agent.

    Adapter fields are deliberately not in ``PatchAgentBody``; rotating
    a service token is an admin-side operation handled by the
    provisioning path, not user-facing PATCH.
    """
    payload = body.model_dump(exclude_none=True)
    upstream = await _admin().patch_agent(
        agent_id=agent_id,
        body=payload,
        session_cookie=ctx.session_cookie,
    )
    # Paperclip may echo the full agent record (including adapterConfig)
    # in PATCH responses; redact for the same reason as the GETs.
    return _redact_agent(upstream)


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Delete an agent. Idempotent — 404 from upstream is swallowed."""
    return await _admin().delete_agent(
        agent_id=agent_id,
        session_cookie=ctx.session_cookie,
    )


@router.get("/agents/{agent_id}/runs")
async def list_runs(agent_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List runs for a given agent."""
    return await _admin().list_runs(
        agent_id=agent_id,
        session_cookie=ctx.session_cookie,
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single run (transcript / metadata) by id."""
    return await _admin().get_run(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
    )
