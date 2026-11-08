"""Teams BFF — Agents and Members.

Every ``/agents`` route reads and writes ``agent_registry``; none calls Paperclip. The body schemas
(``CreateAgentBody`` / ``PatchAgentBody``) forbid extra keys, so client-supplied
``adapterType`` / ``adapterConfig`` / ``url`` / ``headers`` return 422 at the FastAPI boundary.

This module also defines the shared ``_ctx`` Depends helper (and the
indirection helper it leans on — ``_repo``) that the rest
of the Teams BFF (Tasks 7-12) imports.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.dynamodb import run_in_thread
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import agent_registry, hermes_profiles, voice_calls
from core.services.voice_persona import agent_voice
from core.services.agent_provenance import _load_agent_sidecars, stamp_agent_provenance
from core.services.agent_redaction import strip_agent_secrets
from core.services.paperclip_openclaw_pairing import (
    get_seeded_agent_ids,
    get_starter_agent_ids,
)
from core.services.agent_lifecycle import (
    AgentLifecycleConflict,
    delete_agent_everywhere,
    ensure_root_ceo_survives,
)
from core.services.person_label import NEW_MEMBER, is_relay_email
from core.services.provision_gate import evaluate_provision_gate
from core.services.user_provision import (
    ensure_user_ready,
    ensure_workspace_trial_default,
)

from . import deps as deps_mod
from .deps import TeamsContext
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


async def _ctx(auth: AuthContext = Depends(get_current_user)) -> TeamsContext:
    """Resolve the Teams BFF context for the current request: Clerk auth -> workspace META row ->
    membership. ``deps_mod.resolve_teams_context`` (not a direct import) so tests that monkeypatch
    the deps module Just Works.

    Self-heal: a ``reconcile`` callback (``ensure_user_ready(auth)``) makes a non-member drive
    provisioning and get 202 (the UI polls until active) instead of a 409. ``/teams`` never polled
    ``/container/status``, so without this a member could only get provisioned by visiting ``/chat``.
    """
    return await deps_mod.resolve_teams_context(
        auth=auth,
        repo=_repo(),
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

    Registry ``active`` goes out as ``idle`` (``_web``).
    """
    owner_id = resolve_owner_id(auth)
    agents = [_web(a) for a in await _roster(owner_id)]
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
    who = SimpleNamespace(user_id=auth.user_id, org_id=auth.org_id)
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


def _web(agent: dict) -> dict:
    """Registry ``active`` goes out as ``idle``: the web roster reads ``active`` as a live run."""
    return {**agent, "status": "paused" if agent.get("status") == "paused" else "idle"}


async def _roster(owner_id: str) -> list[dict]:
    try:
        return await agent_registry.roster(owner_id)
    except agent_registry.RegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail="The agent roster is temporarily unavailable.") from exc


@router.post("/agents")
async def create_agent(body: CreateAgentBody, auth: AuthContext = Depends(get_current_user)):
    """Create an agent in the registry and write its Hermes profile.

    The S3 profile is what makes the agent runnable, so a failed profile write deletes the row
    again and answers 502: an agent with no profile could never run a turn.
    """
    from core.services.hermes_provisioning import write_profile_for_agent

    owner_id = resolve_owner_id(auth)
    try:
        agent = await agent_registry.create(
            owner_id,
            name=body.name,
            role=body.role,
            title=body.title,
            persona=body.capabilities,
            reports_to=body.reports_to,
            budget_monthly_cents=body.budget_monthly_cents,
        )
    except agent_registry.RegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail="The agent roster is temporarily unavailable.") from exc
    try:
        await write_profile_for_agent(owner_id, agent)
    except Exception as exc:  # noqa: BLE001 — every failure mode rolls back the same way.
        logger.warning(
            "teams.create_agent: profile write failed owner=%s agent=%s", owner_id, agent["id"], exc_info=True
        )
        await agent_registry.delete(owner_id, agent["id"])
        raise HTTPException(
            status_code=502,
            detail="The agent's runtime profile could not be created, so the agent was not kept. Try again.",
        ) from exc
    return _mark_seeded(_web(agent), set())


SafeId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]


async def _workspace_agent(agent_id: SafeId, auth: AuthContext = Depends(get_current_user)) -> dict:
    """The agent's roster row; 404 unless it is a live agent of the caller's workspace."""
    agent = next((a for a in await _roster(resolve_owner_id(auth)) if a["id"] == agent_id), None)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


WorkspaceAgent = Annotated[dict, Depends(_workspace_agent)]


async def _update(auth: AuthContext, agent_id: str, fields: dict) -> dict:
    try:
        return _web(await agent_registry.update(resolve_owner_id(auth), agent_id, fields))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc


@router.get("/agents/{agent_id}")
async def get_agent(agent: WorkspaceAgent, auth: AuthContext = Depends(get_current_user)):
    """One agent, shaped like a ``GET /agents`` row."""
    owner_id = resolve_owner_id(auth)
    repo = _repo()
    seeded_ids, starter_ids, sidecars = await asyncio.gather(
        get_seeded_agent_ids(owner_id=owner_id, repo=repo),
        get_starter_agent_ids(owner_id=owner_id, repo=repo),
        _load_agent_sidecars(owner_id, [agent["id"]]),
    )
    (named,) = await _front_named(owner_id, [_web(agent)])
    return _mark_seeded(named, seeded_ids, sidecars.get(agent["id"]), starter_ids)


@router.patch("/agents/{agent_id}")
async def patch_agent(agent: WorkspaceAgent, body: PatchAgentBody, auth: AuthContext = Depends(get_current_user)):
    """Patch an agent's name, title, persona, manager or budget. Unset and null fields are left alone,
    except an explicit ``reportsTo: null``, which makes the agent a root."""
    payload = body.model_dump(exclude_none=True, by_alias=True)
    if "reports_to" in body.model_fields_set and body.reports_to is None:
        payload["reportsTo"] = None
    owner_id = resolve_owner_id(auth)
    if payload.get("reportsTo"):
        try:
            ensure_root_ceo_survives(target=agent, agents=await _roster(owner_id))
        except AgentLifecycleConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    soul_changed = bool(_SOUL_FIELDS.intersection(payload))
    if "capabilities" in payload:
        payload["persona"] = payload.pop("capabilities")
    updated = await _update(auth, agent["id"], payload)
    if soul_changed:
        # M6-R59: an agent's identity reaches it ONLY through its S3 ``SOUL.md``. Same helper and
        # generated-marker rule as ``runtime_rpc``'s ``agents.update`` door. Non-fatal: the row landed.
        from core.services.hermes_provisioning import refresh_agent_soul

        try:
            await refresh_agent_soul(owner_id, agent["id"], by=auth.user_id)
        except Exception:  # noqa: BLE001 — the roster row landed; SOUL is best effort.
            logger.warning(
                "teams.patch_agent: SOUL refresh failed owner=%s agent=%s", owner_id, agent["id"], exc_info=True
            )
    return updated


class VoiceNotesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str = Field(max_length=hermes_profiles.VOICE_NOTES_MAX)


async def _voice_notes_reply(agent: dict, auth: AuthContext, body: VoiceNotesBody | None) -> dict:
    """C9. The name is always the registry row's; a PUT rewrites the file's name line from it."""
    owner_id = resolve_owner_id(auth)
    agent_id, name = agent["id"], agent.get("name")
    if body is None:
        _, notes = hermes_profiles.parse_voice_notes_md(
            await run_in_thread(hermes_profiles.read_voice_notes, owner_id, agent_id)
        )
    else:
        notes = body.notes.strip()
        text = hermes_profiles.build_voice_notes_md(name, notes)
        await run_in_thread(hermes_profiles.upload_voice_notes, owner_id, agent_id, text)
        await voice_calls.send_note_saved(owner_id, agent_id, name, notes, auth.user_id)
    row = await _repo().resolve_owner_row(owner_id)
    return {
        "name": name,
        "notes": notes,
        "voice": agent_voice(
            agent_id, getattr(row, "starter_agent_ids", None), agent.get("title") or agent.get("role")
        ),
    }


@router.get("/agents/{agent_id}/voice-notes")
async def get_voice_notes(agent: WorkspaceAgent, auth: AuthContext = Depends(get_current_user)):
    return await _voice_notes_reply(agent, auth, None)


@router.put("/agents/{agent_id}/voice-notes")
async def put_voice_notes(agent: WorkspaceAgent, body: VoiceNotesBody, auth: AuthContext = Depends(get_current_user)):
    return await _voice_notes_reply(agent, auth, body)


@router.delete("/agents/{agent_id}")
async def delete_agent(agent: WorkspaceAgent, auth: AuthContext = Depends(get_current_user)):
    """Delete an agent; the last root CEO answers 409."""
    try:
        return await delete_agent_everywhere(owner_id=resolve_owner_id(auth), agent_id=agent["id"])
    except AgentLifecycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/agents/{agent_id}/pause")
async def pause_agent(agent: WorkspaceAgent, auth: AuthContext = Depends(get_current_user)):
    """Display state only: nothing at runtime reads an agent's status (rooms pause, agents don't)."""
    return await _update(auth, agent["id"], {"status": "paused"})


@router.post("/agents/{agent_id}/resume")
async def resume_agent(agent: WorkspaceAgent, auth: AuthContext = Depends(get_current_user)):
    return await _update(auth, agent["id"], {"status": "active"})


@router.post("/agents/{agent_id}/reset-session")
async def reset_agent_session(agent: WorkspaceAgent, body: ResetAgentSessionBody):
    """No runtime session to reset since Paperclip runs went away; answers the agent row for old clients."""
    return _web(agent)


@router.post("/agents/{agent_id}/clear-error")
async def clear_agent_error(agent: WorkspaceAgent):
    """No agent error state since Paperclip runs went away; answers the agent row for old clients."""
    return _web(agent)


@router.post("/agents/{agent_id}/heartbeat", status_code=410)
@router.post("/agents/{agent_id}/wakeup", status_code=410)
async def agent_heartbeat_gone(agent_id: str):
    """Paperclip heartbeats and wakes are gone (super-agent roster, Phase 1 task 6)."""
    raise HTTPException(status_code=410, detail="Agent heartbeats and wakes were removed")
