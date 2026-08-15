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
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.containers import get_ecs_manager, get_gateway_pool
from core.encryption import decrypt  # noqa: F401 — re-exported for tests + downstream tasks.
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import service_token
from core.services.paperclip_adapter_config import (
    OPENCLAW_GATEWAY_TYPE,
    synthesize_openclaw_adapter,
)
from core.services.paperclip_admin_client import PaperclipAdminClient, shared_admin_client
from core.services.agent_budget import enforce_agent_budget
from core.services.agent_redaction import strip_agent_secrets
from core.services.paperclip_openclaw_pairing import (
    ensure_operator_device_paired,
    get_seeded_agent_ids,
    is_seeded_agent,
    pair_with_openclaw,
)
from core.services.paperclip_provisioning import _ws_gateway_url  # noqa: F401 — re-exported for downstream tasks.
from core.services.paperclip_user_session import get_user_session_cookie
from core.services.provision_gate import evaluate_provision_gate
from core.services.user_provision import ensure_user_ready

from . import deps as deps_mod
from ._guards import require_trial_runway
from .deps import TeamsContext, envelope_list
from .schemas import (
    CreateAgentBody,
    PatchAgentBody,
    ResetAgentSessionBody,
    WakeAgentBody,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# Adapter fields are synthesized server-side and must never reach the
# browser. ``adapterConfig`` carries the user's own service token in
# ``authToken``; ``adapterType`` is invariant (``openclaw_gateway`` for
# everyone) and conveys nothing useful to the UI. Detail panels in the
# frontend currently render ``JSON.stringify(<BFF response>)``, so a leak
# at the BFF boundary becomes a token-in-DOM exposure. Strip on every
# read path as defense in depth — the frontend should never need these.
# The field set + stripper are shared with the Paperclip proxy via
# core.services.agent_redaction so neither surface can drift on what's secret.


def _mark_seeded(agent, seeded_ids):
    """Stamp ``seeded: bool`` onto an agent record for the browser.

    ``seeded`` is the single source of truth the Teams UI uses to lock
    Friday/Atlas (hide Delete, show meta read-only). Determined from the
    persisted seeded-agent IDs — never role/name — so a user-created
    agent is never falsely locked. Non-dicts pass through unchanged.
    """
    if not isinstance(agent, dict):
        return agent
    return {**agent, "seeded": agent.get("id") in seeded_ids}


def _redact_agent(agent):
    """Strip adapter-related fields from an agent record before returning to the browser.

    Defense in depth: the BFF synthesizes adapter config from server-side
    state; the browser never names or sees it. Stripping on read prevents
    a current/future panel that pretty-prints the agent dict from leaking
    ``authToken`` (the user's own service token) into the DOM.

    Returns the input unchanged if it isn't a dict (so the function is
    safe to call on any upstream payload shape).
    """
    return strip_agent_secrets(agent)


# --- Indirection helpers (Tasks 7-12 import these) ---
#
# Each helper is a free function so unit tests can monkeypatch them
# without touching the FastAPI Depends graph. The underlying behavior
# stays trivial; the goal is to keep the security-critical wiring
# (adapter synthesis) easy to mock in isolation.


def _admin() -> PaperclipAdminClient:
    """Return the process-wide shared admin client.

    Thin indirection over ``paperclip_admin_client.shared_admin_client`` —
    kept because tests monkeypatch this function
    (``monkeypatch.setattr(agents_mod, "_admin", lambda: admin)``) and the
    sibling Teams routers route through it.
    """
    return shared_admin_client()


def _repo() -> PaperclipRepo:
    """Build the paperclip-companies DDB repo.

    Short table name — ``core.dynamodb.get_table`` prepends the
    ``isol8-{env}-`` prefix.
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

    Thin indirection over ``clerk_admin.get_primary_email`` — kept because
    tests monkeypatch this name and the sibling Teams routers call it.
    """
    from core.services.clerk_admin import get_primary_email

    return await get_primary_email(user_id)


async def build_principal_to_clerk(ctx: TeamsContext) -> dict[str, str]:
    """Map Paperclip principalId → Clerk user_id for the caller's org.

    For a personal context the caller is the only member (seed their own
    pairing); for an org, build the map from the ``paperclip-companies``
    ``by-org-id`` GSI rows. GSI failures degrade to an empty map so the
    caller can ship a best-effort response rather than 5xx.

    Lives here (rather than in ``feed.py``) because both the activity feed's
    actor enrichment and the ``/members`` roster need the same principal map;
    keeping one copy stops the two from drifting on org/personal handling.
    """
    principal_to_clerk: dict[str, str] = {}
    if ctx.org_id is None:
        if ctx.paperclip_user_id:
            principal_to_clerk[ctx.paperclip_user_id] = ctx.user_id
        return principal_to_clerk
    try:
        org_rows = await _repo().list_by_org_id(ctx.org_id)
    except Exception:
        logger.exception("teams: list_by_org_id failed for org=%s", ctx.org_id)
        return principal_to_clerk
    for row in org_rows:
        paperclip_uid = row.get("paperclip_user_id")
        clerk_uid = row.get("user_id")
        if paperclip_uid and clerk_uid:
            principal_to_clerk[paperclip_uid] = clerk_uid
    return principal_to_clerk


async def get_user_session_cookie_for(user_id: str) -> str:
    """Mint a fresh per-user Better Auth session cookie (cold path only).

    The per-user sign-in wired with the production repo / admin client /
    email resolver. Factored to module scope so BOTH the normal context
    builder (``_ctx`` via ``resolve_session_cookie``'s ``mint`` callback)
    and the 401 self-heal handler use the exact same sign-in — they must
    not drift on how a fresh cookie is obtained. Tests monkeypatch the
    indirection helpers (``_repo`` / ``_admin`` / ``_resolve_user_email``)
    rather than this function.
    """
    return await get_user_session_cookie(
        user_id=user_id,
        repo=_repo(),
        admin_client=_admin(),
        clerk_email_resolver=_resolve_user_email,
    )


async def _ctx(
    request: Request,
    response: Response,
    auth: AuthContext = Depends(get_current_user),
) -> TeamsContext:
    """Resolve the Teams BFF context for the current request.

    Wires together: Clerk auth -> paperclip-companies row lookup ->
    browser-held Paperclip session cookie (reused; minted only on cold
    start / 401) -> ``TeamsContext`` for the handler. Using
    ``deps_mod.resolve_teams_context`` (rather than a direct import) so
    test code that monkeypatches the deps module Just Works.

    Self-heal: a ``reconcile`` callback (``ensure_user_ready(auth)``) is
    wired into ``resolve_teams_context`` so a missing Paperclip row drives
    provisioning itself and returns 202 (the UI polls until active), instead
    of wedging the member on a 409. ``/teams`` never polled
    ``/container/status``, so without this a member could only get
    provisioned by visiting ``/chat``. The 202 / 503 status codes from
    ``resolve_teams_context`` otherwise propagate as-is.

    ``request.state.teams_user_id`` is stashed for the 401 self-heal
    exception handler (registered in ``main.py``): when an upstream
    PaperclipAdminClient call 401s, the handler re-mints + reseeds the
    browser cookie for this user, and ``request.state`` is the only place
    it can recover whose cookie to refresh.
    """
    # Recorded before any upstream call so the 401 handler can refresh
    # THIS user's cookie even if the handler raised before ctx was built.
    request.state.teams_user_id = auth.user_id

    return await deps_mod.resolve_teams_context(
        auth=auth,
        repo=_repo(),
        session_factory=get_user_session_cookie_for,
        cookies=request.cookies,
        response=response,
        # Self-heal: if this user has no Paperclip row yet, /teams drives
        # reconciliation itself (it never polls /container/status). Fired in
        # the background by resolve_teams_context; ensure_user_ready is
        # idempotent + gate-first, so it no-ops when the org isn't ready and
        # the frontend keeps polling the 202 until the row goes active.
        reconcile=lambda: _self_heal_reconcile(auth),
    )


async def _self_heal_reconcile(auth: AuthContext) -> dict | None:
    """Drive provisioning for a /teams visitor with no Paperclip row.

    Returns the blocked-gate payload (-> 402 "subscribe required") when a
    provision gate is up, else None (-> 202 "keep polling") after launching
    provisioning in the background.

    **Gate first (awaited).** If the visitor has no subscription / credits /
    provider, ``ensure_user_ready`` would no-op forever and the UI would spin
    on 202 indefinitely. So evaluate the same gate ``/container/status`` uses;
    when it's up, surface its structured payload so the frontend renders the
    subscribe/setup CTA (it maps 402 -> subscribe_required) instead of an
    endless provisioning spinner. (Codex P2.)

    **Provisioning in the background.** Once the gate clears, the actual
    provisioning (``_provision_after_gate`` -> ``ensure_user_ready``) is fired
    off the request path, mirroring ``/container/status``. ``ensure_user_ready``
    can fail transiently (Paperclip admin 5xx/timeout, owner-email lookup), and
    the Teams frontend only auto-retries the **202** sentinel — awaiting it
    inline would let such a failure escape ``resolve_teams_context`` as a 500
    and strand the user on the error overlay. Backgrounding it keeps these
    failures retryable: the next /teams poll re-fires. (Codex P2.)
    """
    owner_id = resolve_owner_id(auth)
    is_admin = auth.is_org_admin if auth.is_org_context else True

    gate = await evaluate_provision_gate(
        owner_id=owner_id,
        is_admin=is_admin,
    )
    if gate is not None:
        # Blocked — don't launch provisioning (it would no-op); tell the
        # caller to return 402 so the UI shows the subscribe/setup CTA.
        return gate.to_payload()

    asyncio.create_task(_provision_after_gate(auth, owner_id))
    return None


async def _provision_after_gate(auth: AuthContext, owner_id: str) -> None:
    """Background half of the self-heal (runs after the gate has cleared).

    Off the request path so transient failures stay retryable rather than
    500-ing /teams (see ``_self_heal_reconcile``).

    This used to derive a ``provision_container`` flag from live ECS state
    (skip the container start when the box is already up, and again when it is
    a deliberately parked trial box that a /teams READ must not wake). That
    decision now lives inside ``ensure_user_ready`` itself
    (``_provision_or_reconcile``), which reads the same row and additionally
    keeps the SEED running in the cases where no provision is dispatched —
    the half the flag silently skipped, which is why an owner entering via
    /teams on a running container could sit on 202 forever.

    Org members are excluded from container provisioning by ensure_user_ready
    itself (the role guard at user_provision.py: members ride the owner's
    shared container).
    """
    try:
        await ensure_user_ready(auth)
    except Exception:
        # Background task: log and swallow so a transient failure doesn't
        # surface as an unretrieved-task error. The next /teams poll re-fires.
        logger.exception("teams self-heal: background provisioning failed for owner_id=%s", owner_id)


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
    seeded_ids = await get_seeded_agent_ids(owner_id=_resolve_owner_id(ctx), repo=_repo())
    return {"agents": [_mark_seeded(_redact_agent(a), seeded_ids) for a in envelope["agents"]]}


@router.get("/members")
async def list_members(ctx: TeamsContext = Depends(_ctx)):
    """List the caller's company members, enriched with Clerk identity.

    Upstream's ``GET /api/companies/{co}/members`` rows carry only a
    ``principalId`` (the Better Auth user id) — no email, name, or avatar.
    The human-user assignee picker on the task detail rail needs those, so
    we join each principal back to Clerk via the same principal→Clerk map
    the activity feed uses, then resolve name/email/imageUrl from Clerk.

    Returns the ``{members: [...]}`` envelope shape every Teams panel reads.
    Each row matches the frontend ``CompanyMember`` type
    (``userId``/``name``/``email``/``imageUrl``). Enrichment is best-effort:
    a member whose Clerk lookup fails is dropped rather than shipped as a
    raw principal id (the picker only wants resolvable humans).
    """
    upstream = await _admin().list_members(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    rows = envelope_list(upstream, "members")["members"]
    principal_to_clerk = await build_principal_to_clerk(ctx)

    members: list[dict] = []
    seen_clerk_ids: set[str] = set()
    for row in rows:
        clerk_user_id = principal_to_clerk.get(row.get("principalId"))
        if not clerk_user_id or clerk_user_id in seen_clerk_ids:
            continue
        try:
            member = await _resolve_member_identity(clerk_user_id)
        except Exception:
            logger.exception(
                "teams.list_members: clerk lookup failed for clerk_user_id=%s",
                clerk_user_id,
            )
            continue
        if member is None:
            continue
        seen_clerk_ids.add(clerk_user_id)
        members.append(member)
    return {"members": members}


async def _resolve_member_identity(user_id: str) -> dict | None:
    """Build a ``CompanyMember`` row from a Clerk user.

    Returns ``None`` when Clerk has no such user. Name falls back from the
    full name → email → the raw id so the picker always has a label to show.
    """
    from core.services.clerk_admin import get_user

    user = await get_user(user_id)
    if not user:
        return None
    email = _primary_email(user)
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part).strip()
    return {
        "userId": user_id,
        "name": name or email or user_id,
        "email": email,
        "imageUrl": user.get("image_url"),
    }


def _primary_email(user: dict) -> str | None:
    """Pull the primary email off a Clerk user dict, else the first address."""
    primary_id = user.get("primary_email_address_id")
    addrs = user.get("email_addresses") or []
    for entry in addrs:
        if entry.get("id") == primary_id and entry.get("email_address"):
            return entry["email_address"]
    if addrs and addrs[0].get("email_address"):
        return addrs[0]["email_address"]
    return None


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
    # Plan agent budget, BEFORE anything is minted or created upstream — a
    # refusal here leaves no Paperclip row to reconcile.
    await enforce_agent_budget(owner_id)
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
            # Register the new agent's operator device into the running
            # container's paired.json + bind its authToken to the gateway
            # token, so its first relayed connect isn't rejected NOT_PAIRED.
            # Provision-time pairing only covers agents that exist at provision.
            await ensure_operator_device_paired(
                owner_id=owner_id,
                paperclip_agent=paperclip_agent,
                gateway_token=token,
                ip=ip,
            )
        except Exception as exc:  # noqa: BLE001 — pairing failure is deferred.
            logger.warning(
                "teams.create_agent: pair_with_openclaw failed for owner=%s agent=%s — deferring to next provision: %s",
                owner_id,
                paperclip_agent.get("id"),
                exc,
            )

    # Redact adapterConfig/adapterType before returning, exactly like the read
    # paths and the /chat websocket create path. Paperclip echoes the full
    # adapterConfig on create — including the freshly-minted devicePrivateKeyPem
    # (the operator device's SIGNING key) and authToken — and those must never
    # reach the browser/DOM, or anyone with the response could impersonate the
    # agent's operator device.
    return _redact_agent(paperclip_agent)


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
    seeded_ids = await get_seeded_agent_ids(owner_id=_resolve_owner_id(ctx), repo=_repo())
    return _mark_seeded(_redact_agent(upstream), seeded_ids)


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
    # Drop unset/null fields by default. ``reportsTo`` is the one field that is
    # nullable upstream (`uuid().nullable()`) — an explicit null clears the
    # manager (root). exclude_none would drop that null, so re-insert it only
    # when the client explicitly sent reportsTo=null. The other optional fields
    # (title/capabilities/budget) are NOT nullable upstream, so keep dropping
    # their nulls to avoid Paperclip validation errors.
    payload = body.model_dump(exclude_none=True, by_alias=True)
    if "reports_to" in body.model_fields_set and body.reports_to is None:
        payload["reportsTo"] = None
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
    """Delete an agent. Idempotent — 404 from upstream is swallowed.

    Seeded agents (Friday/Atlas) are part of the provisioning contract and
    must not be deleted. The OpenClaw gateway delete path enforces this in
    ``websocket_chat._guard_seeded_delete``; guard the REST path too
    (defense in depth — the UI deletes via the gateway, but this endpoint
    is still reachable directly).
    """
    if await is_seeded_agent(owner_id=_resolve_owner_id(ctx), agent_id=agent_id):
        raise HTTPException(status_code=403, detail="Seeded agents (Friday/Atlas) cannot be deleted.")
    return await _admin().delete_agent(
        agent_id=agent_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/agents/{agent_id}/pause")
async def pause_agent(agent_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Pause an agent (stops it running on events)."""
    # Paperclip echoes the updated agent record, which can include
    # adapterConfig/adapterType (the OpenClaw service token). Redact for the
    # same reason as the GET/PATCH handlers — never leak it to the browser.
    return _redact_agent(
        await _admin().pause_agent(
            agent_id=agent_id,
            session_cookie=ctx.session_cookie,
        )
    )


@router.post("/agents/{agent_id}/resume")
async def resume_agent(agent_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Resume a paused agent."""
    return _redact_agent(
        await _admin().resume_agent(
            agent_id=agent_id,
            session_cookie=ctx.session_cookie,
        )
    )


@router.post("/agents/{agent_id}/reset-session")
async def reset_agent_session(
    agent_id: str,
    body: ResetAgentSessionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Reset a wedged agent's runtime session state — recovery lever.

    Maps to upstream ``POST /api/agents/{id}/runtime-state/reset-session``. An
    empty body resets all of the agent's sessions; an optional ``taskKey``
    scopes it to one task. Unlike ``wakeup`` this does NOT dispatch an
    inference run, so there's no trial-runway gate. Redact defensively — a
    future upstream change that echoes adapterConfig must never leak the
    service token to the browser.
    """
    return _redact_agent(
        await _admin().reset_agent_session(
            agent_id=agent_id,
            body=body.model_dump(exclude_none=True, by_alias=True),
            session_cookie=ctx.session_cookie,
        )
    )


@router.post("/agents/{agent_id}/clear-error")
async def clear_agent_error(agent_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Clear an agent's error status — recovery lever.

    Maps to upstream ``POST /api/agents/{id}/clear-error``. Upstream flips an
    ``error``-status agent back to ``idle`` (409 if it isn't in error). No run
    is dispatched, so no trial-runway gate. Paperclip echoes the updated agent
    record, which can include adapterConfig/adapterType (the OpenClaw service
    token) — redact like the pause/resume handlers before returning.
    """
    return _redact_agent(
        await _admin().clear_agent_error(
            agent_id=agent_id,
            session_cookie=ctx.session_cookie,
        )
    )


@router.post("/agents/{agent_id}/wakeup")
async def wake_agent(
    agent_id: str,
    body: WakeAgentBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Wake an agent to (re)attempt work — drives the failed-run Retry action.

    Maps to upstream ``POST /api/agents/{id}/wakeup``. The wakeup response is
    a run/ack envelope (no agent record), but redact defensively so a future
    upstream change that echoes adapterConfig can never leak the service token.

    Waking an agent dispatches a run immediately server-side inside
    Paperclip, so this is a spend front door. The free-tier $3/day
    cap is checked here, BEFORE the proxy call reaches Paperclip.
    """
    await require_trial_runway(ctx.owner_id)
    return _redact_agent(
        await _admin().wake_agent(
            agent_id=agent_id,
            body=body.model_dump(exclude_none=True, by_alias=True),
            session_cookie=ctx.session_cookie,
        )
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single run by id — legacy alias for ``/heartbeat-runs/{id}``.

    The frontend's RunDetailPanel still reads this path as a fallback next to
    ``/heartbeat-runs/{id}``; upstream never had ``GET /api/runs/{id}``, so
    this proxies the heartbeat-run route instead of 404ing.
    """
    return await _admin().get_heartbeat_run(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
    )
