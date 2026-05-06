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

import asyncio

import httpx
from fastapi import APIRouter, Depends

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.encryption import decrypt  # noqa: F401 — re-exported for tests + downstream tasks.
from core.repositories import container_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_adapter_config import (
    OPENCLAW_GATEWAY_TYPE,
    synthesize_openclaw_adapter,
)
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_autoprovision import ensure_paperclip_workspace
from core.services.paperclip_provisioning import _ws_gateway_url  # noqa: F401 — re-exported for downstream tasks.
from core.services.paperclip_user_session import get_user_session_cookie

from . import deps as deps_mod
from .deps import TeamsContext, TeamsContextError, envelope_list
from .schemas import CreateAgentBody, PatchAgentBody

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
    """Shared Teams Depends helper — Tasks 7-12 reuse this.

    Wires together: Clerk auth -> paperclip-companies row lookup ->
    per-user Better Auth sign-in -> ``TeamsContext`` for the handler.
    Using ``deps_mod.resolve_teams_context`` (rather than a direct
    import) so test code that monkeypatches the deps module Just Works.

    On a "no DDB row" 409, lazy-provision the workspace in the
    background for personal-context users with an active container,
    and re-raise as 202 so the frontend polls. Org context keeps the
    409 — those rows are owned by Clerk's ``organization.created``
    webhook and a missing row is an operator issue, not a self-heal
    case. Personal users with no container get 402 (subscribe first).
    """

    async def session_factory(user_id: str) -> str:
        return await get_user_session_cookie(
            user_id=user_id,
            repo=_repo(),
            admin_client=_admin(),
            clerk_email_resolver=_resolve_user_email,
        )

    try:
        return await deps_mod.resolve_teams_context(
            auth=auth,
            repo=_repo(),
            session_factory=session_factory,
        )
    except TeamsContextError as exc:
        if exc.status_code != 409 or exc.detail != "team workspace not provisioned":
            raise
        if auth.is_org_context:
            raise
        container = await container_repo.get_by_owner_id(auth.user_id)
        if not container:
            raise TeamsContextError(
                status_code=402,
                detail="subscribe to enable Teams",
            ) from exc
        asyncio.create_task(ensure_paperclip_workspace(owner_id=auth.user_id, clerk_user_id=auth.user_id))
        raise TeamsContextError(
            status_code=202,
            detail="team workspace provisioning",
        ) from exc


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
    """Create an agent.

    Spec §5 security invariant: the ``adapter_type`` + ``adapter_config``
    fields passed to the admin client are synthesized HERE, server-side,
    using ``synthesize_openclaw_adapter``. The client body never carries
    them — ``CreateAgentBody`` rejects extra keys with 422.
    """
    service_token = await _decrypt_service_token(ctx.user_id)
    adapter_config = synthesize_openclaw_adapter(
        gateway_url=_gateway_url_for_env(),
        service_token=service_token,
        user_id=ctx.user_id,
    )
    return await _admin().create_agent(
        session_cookie=ctx.session_cookie,
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
