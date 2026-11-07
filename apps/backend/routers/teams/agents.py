"""Teams BFF — Agents and Runs.

Spec §5: every mutating call synthesizes the adapter config server-side, picked by the
owner's runtime_host via ``paperclip_agent_lifecycle.resolve_agent_adapter`` (openclaw_gateway
on ECS, the fork's http adapter on AgentCore/Hermes). The body schemas (``CreateAgentBody`` /
``PatchAgentBody``) forbid extra keys, so client-supplied
``adapterType`` / ``adapterConfig`` / ``url`` / ``headers`` return 422
at the FastAPI boundary.

This module also defines the shared ``_ctx`` Depends helper (and the
indirection helpers it leans on — ``_admin``, ``_repo``) that the rest
of the Teams BFF (Tasks 7-12) imports.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import container_repo
from core.dynamodb import run_in_thread
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import agent_registry, hermes_profiles, voice_calls
from core.services.voice_persona import agent_voice
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    shared_admin_client,
)
from core.services.paperclip_agent_idempotency import (
    build_agent_create_idempotency_key,
    paperclip_agent_intent,
)
from core.services.agent_provenance import _load_agent_sidecars, stamp_agent_provenance
from core.services.agent_redaction import strip_agent_secrets
from core.services.paperclip_openclaw_pairing import (
    get_seeded_agent_ids,
    get_starter_agent_ids,
)
from core.services.paperclip_agent_lifecycle import (
    AgentLifecycleConflict,
    delete_agent_everywhere,
    ensure_root_ceo_survives,
    resolve_agent_adapter,
)
from core.services.paperclip_user_session import get_user_session_cookie
from core.services.person_label import NEW_MEMBER, is_relay_email
from core.services.provision_gate import evaluate_provision_gate
from core.services.user_provision import (
    ensure_user_ready,
    ensure_workspace_trial_default,
)

from . import deps as deps_mod
from .deps import TeamsContext, envelope_list
from .schemas import (
    CreateAgentBody,
    PatchAgentBody,
    ResetAgentSessionBody,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: The fields (camelCase, as ``model_dump(by_alias=True)`` emits them) that
#: ``hermes_profiles.build_soul_md`` renders — i.e. the ones whose change has to reach an
#: AgentCore agent's ``SOUL.md``. ``reportsTo``/``budgetMonthlyCents``/``icon`` are never
#: rendered into SOUL and stay out.
#:
#: ``role`` IS rendered (two lines: "You are {name}, the {role} agent…" plus the CEO line) and is
#: listed here even though ``PatchAgentBody`` does not expose it today — the intersection below
#: simply never matches, at no cost. It is listed because the failure mode of forgetting it is
#: invisible (M6 task-15 item 4): the moment this door learns to patch ``role``, an omission here
#: would leave every migrated agent introducing itself by its old role forever while the UI, which
#: reads the Paperclip row, shows the new one. This set must track ``build_soul_md``, not the body
#: schema. ``refresh_agent_soul`` still declines to overwrite an owner-edited SOUL.md
#: (``SOUL_GENERATED_MARKER``, M6-R48), so adding a field cannot clobber owner content.
_SOUL_FIELDS = frozenset({"name", "title", "capabilities", "role"})


# Adapter fields are synthesized server-side and must never reach the
# browser. ``adapterConfig`` carries the user's own service token in
# ``authToken``; ``adapterType`` is invariant (``openclaw_gateway`` for
# everyone) and conveys nothing useful to the UI. Detail panels in the
# frontend currently render ``JSON.stringify(<BFF response>)``, so a leak
# at the BFF boundary becomes a token-in-DOM exposure. Strip on every
# read path as defense in depth — the frontend should never need these.
# The field set + stripper are shared with the Paperclip proxy via
# core.services.agent_redaction so neither surface can drift on what's secret.


def _mark_seeded(agent, seeded_ids, sidecar=None, starter_ids=()):
    """Stamp backend-authored seed and origin fields for the browser.

    ``seeded`` is backend-authored starter provenance. Determined from the
    persisted seeded-agent IDs — never role/name — so a user-created
    agent is never falsely locked. Marketplace identity requires a complete
    server-written deployment sidecar; ambiguous legacy rows become Custom.
    """
    return stamp_agent_provenance(
        agent,
        seeded_ids=seeded_ids,
        starter_ids=starter_ids,
        sidecar=sidecar,
    )


async def _front_named(owner_id: str, agents: list) -> list:
    """Under the super-agent switch the front agent is "Lightbulb" (contracts-v2 §9) wherever it shows, as
    messaging's roster names it; Paperclip still calls it "CEO" (dev walk R6: the Team page and the DM header)."""
    from core.services import messaging

    front = await messaging._front_agent_id(owner_id)
    handles: dict[str, str] = {}
    if front:  # each agent's @handle as rooms seat it: Team › Ask drafts "@growth " (dev walk 7, W7-7)
        try:
            handles = await messaging._handles_for(owner_id, await messaging.load_agent_roster(owner_id))
        except Exception:  # noqa: BLE001 -- no handles beats no Team page
            logger.warning("teams: agent handles unavailable for %s", owner_id, exc_info=True)
    out = []
    for a in agents:
        if front and isinstance(a, dict):
            a = {**a, **({"name": messaging.FRONT_AGENT_NAME} if a.get("id") == front else {})}
            a["mentionHandle"] = handles.get(a.get("id"))
        out.append(a)
    return out


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


async def _container_is_reachable(owner_id: str) -> tuple[bool, str | None, str | None]:
    """Return ``(reachable, address, gateway_token)`` for the owner's workspace.

    Reachable means the containers row exists and carries a minted runtime ARN plus a gateway
    token. Nothing is pinged and nothing is discovered: any ``InvokeAgentRuntime`` wakes the
    microVM, so there is no task IP and no "is the task serving yet" question to answer.
    """
    from core.repositories import container_repo

    try:
        container = await container_repo.get_by_owner_id(owner_id)
    except Exception:  # noqa: BLE001 — defensive: a DDB blip reads as not reachable.
        return False, None, None
    token = (container or {}).get("gateway_token")
    if not container or not container.get("agentcore_runtime_arn") or not token:
        return False, None, None
    return True, "agentcore", token


async def _write_hermes_profile_or_rollback(ctx, owner_id: str, paperclip_agent: dict, container: dict) -> None:
    """The AgentCore half of ``create_agent``: this agent's Hermes profile, or no agent at all.

    An AgentCore workspace has no container for ``pair_with_openclaw`` to reach — the S3 profile
    IS this transport's pairing (config.yaml + .env + SOUL.md + the agent's own Paperclip key),
    and without one the roster row can never run a turn. Writes it through the SAME seam
    ``runtime_rpc``'s ``agents.create`` door uses (``hermes_provisioning.write_profile_for_agent``,
    M6-R23), so there stays exactly one profile writer.

    Unlike the ECS branch, a failure here is FATAL and is rolled back (M6-R59): deferring to "the
    next provision" is an ECS-only escape hatch — nothing re-walks the roster on AgentCore, so a
    swallowed failure leaves a permanently dead agent in the roster, which is the bug this fixes.
    The Paperclip row is deleted and the request answers 502, which also frees the create's
    idempotency key (Paperclip drops the mapping with the agent), so the client's retry is a real
    retry rather than a replay of the dead id. A rollback that itself fails is logged and still
    surfaces as 502 — an agent the client can see and delete beats a silent success.
    """
    from core.services.hermes_provisioning import write_profile_for_agent

    agent_id = paperclip_agent.get("id")
    try:
        await write_profile_for_agent(owner_id, paperclip_agent)
    except Exception as exc:  # noqa: BLE001 — every failure mode rolls back the same way.
        logger.warning(
            "teams.create_agent: hermes profile write failed for owner=%s agent=%s — rolling back",
            owner_id,
            agent_id,
            exc_info=True,
        )
        try:
            if agent_id:
                await _admin().delete_agent(session_cookie=await _user_session(ctx), agent_id=agent_id)
        except Exception:  # noqa: BLE001 — best effort; the 502 below is the contract.
            logger.warning(
                "teams.create_agent: rollback delete failed for owner=%s agent=%s",
                owner_id,
                agent_id,
                exc_info=True,
            )
        raise HTTPException(
            status_code=502,
            detail="The agent's runtime profile could not be created, so the agent was not kept. Try again.",
        ) from exc


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
    pairing); for an org, map every human who joined the workspace's company
    (``member_user_ids``) through their identity row. Read failures degrade
    to what is known so the caller can ship a best-effort response.

    Lives here (rather than in ``feed.py``) because both the activity feed's
    actor enrichment and the ``/members`` roster need the same principal map.
    """
    principal_to_clerk: dict[str, str] = {}
    if ctx.paperclip_user_id:
        principal_to_clerk[ctx.paperclip_user_id] = ctx.user_id
    if ctx.org_id is None:
        return principal_to_clerk
    repo = _repo()
    try:
        ws = await repo.get_workspace(ctx.org_id)
        for clerk_uid in sorted(ws.member_user_ids) if ws is not None else []:
            identity = await repo.get_identity(clerk_uid)
            if identity is not None and identity.paperclip_user_id:
                principal_to_clerk[identity.paperclip_user_id] = clerk_uid
    except Exception:
        logger.exception("teams: member principal map failed for org=%s", ctx.org_id)
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
        # Self-heal: if this user has no Paperclip row yet, /teams runs the
        # lazy JOIN itself. Fired in the background by resolve_teams_context;
        # ensure_user_ready is idempotent, so it no-ops when the org isn't
        # ready and the frontend keeps polling the 202 until the row goes active.
        reconcile=lambda: _self_heal_reconcile(auth),
    )


async def _self_heal_reconcile(auth: AuthContext) -> dict | None:
    """The lazy Paperclip JOIN for a /teams visitor with no Paperclip row.

    Returns the blocked-gate payload (-> 402 "subscribe required") when a
    provision gate is up, else None (-> 202 "keep polling") after launching
    ``ensure_user_ready`` in the background. It never provisions a workspace:
    that is the ``organization.created`` webhook's (and the user's Retry's) job.

    **Gate first (awaited)**, so a visitor with no subscription / credits /
    provider gets the subscribe/setup CTA instead of an endless 202.

    **Join in the background**: ``ensure_user_ready`` can fail transiently
    (Paperclip admin 5xx/timeout, owner-email lookup), and the Teams frontend
    only auto-retries the **202** sentinel — awaiting it inline would let such a
    failure escape as a 500. The next /teams request re-fires it.
    """
    owner_id = resolve_owner_id(auth)
    is_admin = auth.is_org_admin

    await ensure_workspace_trial_default(auth, owner_id)
    gate = await evaluate_provision_gate(
        owner_id=owner_id,
        is_admin=is_admin,
        actor_user_id=auth.user_id,
    )
    if gate is not None:
        # Blocked — don't launch provisioning (it would no-op); tell the
        # caller to return 402 so the UI shows the subscribe/setup CTA.
        return gate.to_payload()

    asyncio.create_task(_provision_after_gate(auth, owner_id))
    return None


async def _provision_after_gate(auth: AuthContext, owner_id: str) -> None:
    """Background half of the self-heal (runs after the gate has cleared): the
    Paperclip JOIN, plus the owner seed reconcile on a running workspace — see
    ``ensure_user_ready``. Off the request path so transient failures stay
    retryable rather than 500-ing /teams.
    """
    try:
        await ensure_user_ready(auth)
    except Exception:
        # Background task: log and swallow so a transient failure doesn't
        # surface as an unretrieved-task error. The next /teams poll re-fires.
        logger.exception("teams self-heal: background provisioning failed for owner_id=%s", owner_id)


# --- Routes ---


@router.get("/agents")
async def list_agents(auth: AuthContext = Depends(get_current_user)):
    """The workspace's agents from the registry (``agent_registry.roster``); nothing here calls Paperclip.

    Registry ``active`` goes out as ``idle``: the web roster reads ``active`` as a live run.
    """
    owner_id = resolve_owner_id(auth)
    try:
        rows = await agent_registry.roster(owner_id)
    except agent_registry.RegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail="The agent roster is temporarily unavailable.") from exc
    agents = [{**a, "status": "paused" if a["status"] == "paused" else "idle"} for a in rows]
    repo = _repo()
    seeded_ids, starter_ids, sidecars = await asyncio.gather(
        get_seeded_agent_ids(owner_id=owner_id, repo=repo),
        get_starter_agent_ids(owner_id=owner_id, repo=repo),
        _load_agent_sidecars(owner_id, [a["id"] for a in agents]),
    )
    return {
        "agents": [
            _mark_seeded(a, seeded_ids, sidecars.get(a["id"]), starter_ids)
            for a in await _front_named(owner_id, agents)
        ]
    }


@router.get("/members")
async def list_members(auth: AuthContext = Depends(get_current_user)):
    """List the humans in the caller's workspace.

    The source is Clerk org membership, NOT Paperclip's company members —
    the same set ``messaging._validate_human_in_owner`` accepts as room
    members, so the new-conversation picker never hides someone the backend
    would let you add. A personal workspace has exactly one human: the caller.

    Returns the ``{members: [...]}`` envelope shape every Teams panel reads.
    Each row matches the frontend ``CompanyMember`` type
    (``userId``/``name``/``email``/``imageUrl``/``paperclipUserId``).
    ``paperclipUserId`` comes from the stored identity rows (no Paperclip call),
    null for a member with no Paperclip identity.
    """
    if auth.org_id is None:
        members = [m for m in [await _resolve_member_identity(auth.user_id)] if m]
    else:
        from core.services.clerk_admin import list_organization_members

        members = [
            _member_row(m["user_id"], m["first_name"], m["last_name"], m["email"], m["image_url"])
            for m in await list_organization_members(auth.org_id)
        ]
    who = SimpleNamespace(user_id=auth.user_id, org_id=auth.org_id, paperclip_user_id=None)
    clerk_to_principal = {clerk: pc for pc, clerk in (await build_principal_to_clerk(who)).items()}
    return {"members": [{**m, "paperclipUserId": clerk_to_principal.get(m["userId"])} for m in members]}


def _member_row(user_id: str, first: str | None, last: str | None, email: str | None, image_url) -> dict:
    """The ``CompanyMember`` row. Name falls back from the full name → email →
    the raw id so the picker always has a label to show. An Apple relay address is
    never that label: every client renders ``name || email``, so this is the one fix."""
    name = " ".join(part for part in (first, last) if part).strip()
    fallback = NEW_MEMBER if is_relay_email(email) else email
    return {"userId": user_id, "name": name or fallback or user_id, "email": email or None, "imageUrl": image_url}


async def _resolve_member_identity(user_id: str) -> dict | None:
    """Build a ``CompanyMember`` row from a Clerk user, or None when Clerk has no such user."""
    from core.services.clerk_admin import get_user

    user = await get_user(user_id)
    if not user:
        return None
    return _member_row(
        user_id, user.get("first_name"), user.get("last_name"), _primary_email(user), user.get("image_url")
    )


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
    """Create an agent in Paperclip and write its Hermes profile.

    Spec §5 security invariant: the ``adapter_type`` + ``adapter_config`` fields passed to the
    admin client are resolved HERE, server-side, through ``resolve_agent_adapter``. The client
    body never carries them — ``CreateAgentBody`` rejects extra keys with 422.

    The agent's S3 Hermes profile is what makes it runnable, so a failure to
    write one is fatal + rolled back (see ``_write_hermes_profile_or_rollback``).
    """
    owner_id = _resolve_owner_id(ctx)
    # The adapter is keyed by OWNER, never by the creating member: the workspace's runtime and
    # its hook token are the org's, so a member-keyed credential would authenticate the wrong
    # workspace's facade. One shared resolver, so every Paperclip agent-creation door agrees.
    adapter_type, adapter_config = await resolve_agent_adapter(owner_id)
    idempotency_key = build_agent_create_idempotency_key(
        operation="teams",
        owner_id=owner_id,
        intent=paperclip_agent_intent(
            name=body.name,
            role=body.role,
            adapter_type=adapter_type,
            title=body.title,
            capabilities=body.capabilities,
            reports_to=body.reports_to,
            budget_monthly_cents=body.budget_monthly_cents,
        ),
    )
    paperclip_agent = await _admin().create_agent(
        session_cookie=await _user_session(ctx),
        company_id=ctx.company_id,
        name=body.name,
        role=body.role,
        adapter_type=adapter_type,
        adapter_config=adapter_config,
        title=body.title,
        capabilities=body.capabilities,
        reports_to=body.reports_to,
        budget_monthly_cents=body.budget_monthly_cents,
        idempotency_key=idempotency_key,
    )

    container = await container_repo.get_by_owner_id(owner_id) or {}
    await _write_hermes_profile_or_rollback(ctx, owner_id, paperclip_agent, container)

    # Redact adapterConfig/adapterType before returning, exactly like the read
    # paths and the /chat websocket create path. Paperclip echoes the full
    # adapterConfig on create — including the freshly-minted devicePrivateKeyPem
    # (the operator device's SIGNING key) and authToken — and those must never
    # reach the browser/DOM, or anyone with the response could impersonate the
    # agent's operator device.
    return _mark_seeded(_redact_agent(paperclip_agent), set())


SafeId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]


async def _company_agent(agent_id: SafeId, ctx: TeamsContext = Depends(_ctx)) -> str:
    """404 unless ``agent_id`` belongs to the caller's workspace company.

    Paperclip authorizes by the member's session, and a Paperclip user can hold
    memberships in more than one company, so the session alone does not pin an
    agent to THIS workspace (a wake would pass this workspace's trial gate for
    another's agent). Same fence as issues' ``_require_company_issue``.
    """
    agent = await _admin().get_agent(agent_id=agent_id, session_cookie=ctx.session_cookie)
    if not isinstance(agent, dict) or agent.get("companyId") != ctx.company_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent_id


CompanyAgentId = Annotated[str, Depends(_company_agent)]


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: SafeId, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single agent by id (404 outside the caller's workspace company).

    Redacts ``adapterConfig`` + ``adapterType`` before returning — see
    ``_redact_agent``.
    """
    upstream = await _admin().get_agent(
        agent_id=agent_id,
        session_cookie=ctx.session_cookie,
    )
    if not isinstance(upstream, dict) or upstream.get("companyId") != ctx.company_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    owner_id = _resolve_owner_id(ctx)
    repo = _repo()
    seeded_ids, starter_ids = await asyncio.gather(
        get_seeded_agent_ids(owner_id=owner_id, repo=repo),
        get_starter_agent_ids(owner_id=owner_id, repo=repo),
    )
    sidecars = await _load_agent_sidecars(owner_id, [agent_id])
    (named,) = await _front_named(owner_id, [upstream])
    return _mark_seeded(_redact_agent(named), seeded_ids, sidecars.get(agent_id), starter_ids)


@router.patch("/agents/{agent_id}")
async def patch_agent(
    agent_id: CompanyAgentId,
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
    admin = _admin()
    if payload.get("reportsTo"):
        target = await admin.get_agent(
            agent_id=agent_id,
            session_cookie=ctx.session_cookie,
        )
        listed = await admin.list_agents(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
        )
        try:
            ensure_root_ceo_survives(target=target, agents=envelope_list(listed, "agents")["agents"])
        except AgentLifecycleConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    upstream = await admin.patch_agent(
        agent_id=agent_id,
        body=payload,
        session_cookie=ctx.session_cookie,
    )
    if _SOUL_FIELDS.intersection(payload):
        # M6-R59: on AgentCore an agent's identity reaches it ONLY through its S3 ``SOUL.md``
        # (there is no openclaw.json), so a rename/retitle that stops at the Paperclip row
        # leaves the agent introducing itself by its old name forever. Same shared helper —
        # and the same generated-marker rule — as ``runtime_rpc``'s ``agents.update`` door.
        # Non-fatal, unlike create: the Paperclip row (what the UI reads) is already correct.
        owner_id = _resolve_owner_id(ctx)
        from core.services.hermes_provisioning import refresh_agent_soul

        try:
            await refresh_agent_soul(owner_id, agent_id, by=ctx.user_id)
        except Exception:  # noqa: BLE001 — the roster row landed; SOUL is best effort.
            logger.warning(
                "teams.patch_agent: SOUL refresh failed for owner=%s agent=%s",
                owner_id,
                agent_id,
                exc_info=True,
            )
    # Paperclip may echo the full agent record (including adapterConfig)
    # in PATCH responses; redact for the same reason as the GETs.
    return _redact_agent(upstream)


class VoiceNotesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str = Field(max_length=hermes_profiles.VOICE_NOTES_MAX)


async def _voice_notes_reply(agent_id: str, ctx: TeamsContext, body: VoiceNotesBody | None) -> dict:
    """C9. The name is always the Paperclip row's; a PUT rewrites the file's name line from it."""
    agent = await _admin().get_agent(agent_id=agent_id, session_cookie=ctx.session_cookie)
    if not isinstance(agent, dict) or agent.get("companyId") != ctx.company_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    owner_id = _resolve_owner_id(ctx)
    name = agent.get("name")
    if body is None:
        _, notes = hermes_profiles.parse_voice_notes_md(
            await run_in_thread(hermes_profiles.read_voice_notes, owner_id, agent_id)
        )
    else:
        notes = body.notes.strip()
        text = hermes_profiles.build_voice_notes_md(name, notes)
        await run_in_thread(hermes_profiles.upload_voice_notes, owner_id, agent_id, text)
        await voice_calls.send_note_saved(owner_id, agent_id, name, notes, ctx.user_id)
    row = await _repo().resolve_owner_row(owner_id)
    return {
        "name": name,
        "notes": notes,
        "voice": agent_voice(
            agent_id, getattr(row, "starter_agent_ids", None), agent.get("title") or agent.get("role")
        ),
    }


@router.get("/agents/{agent_id}/voice-notes")
async def get_voice_notes(agent_id: CompanyAgentId, ctx: TeamsContext = Depends(_ctx)):
    return await _voice_notes_reply(agent_id, ctx, None)


@router.put("/agents/{agent_id}/voice-notes")
async def put_voice_notes(agent_id: CompanyAgentId, body: VoiceNotesBody, ctx: TeamsContext = Depends(_ctx)):
    return await _voice_notes_reply(agent_id, ctx, body)


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: CompanyAgentId, ctx: TeamsContext = Depends(_ctx)):
    """Delete an agent. Idempotent — 404 from upstream is swallowed.

    The shared lifecycle preserves a final root CEO while allowing any other
    agent, including starter specialists, to be removed.
    """
    owner_id = _resolve_owner_id(ctx)
    try:
        return await delete_agent_everywhere(owner_id=owner_id, agent_id=agent_id)
    except AgentLifecycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/agents/{agent_id}/pause")
async def pause_agent(agent_id: CompanyAgentId, ctx: TeamsContext = Depends(_ctx)):
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
async def resume_agent(agent_id: CompanyAgentId, ctx: TeamsContext = Depends(_ctx)):
    """Resume a paused agent."""
    return _redact_agent(
        await _admin().resume_agent(
            agent_id=agent_id,
            session_cookie=ctx.session_cookie,
        )
    )


@router.post("/agents/{agent_id}/reset-session")
async def reset_agent_session(
    agent_id: CompanyAgentId,
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
async def clear_agent_error(agent_id: CompanyAgentId, ctx: TeamsContext = Depends(_ctx)):
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


@router.post("/agents/{agent_id}/heartbeat", status_code=410)
@router.post("/agents/{agent_id}/wakeup", status_code=410)
async def agent_heartbeat_gone(agent_id: str):
    """Paperclip heartbeats and wakes are gone (super-agent roster, Phase 1 task 6)."""
    raise HTTPException(status_code=410, detail="Agent heartbeats and wakes were removed")
