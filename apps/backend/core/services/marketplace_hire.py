"""Marketplace hires that route through Paperclip's approval surface.

A headless Friday cannot hire. She can only ask. This module turns that ask
into a Paperclip ``hire_agent`` approval linked to the issue that prompted it,
which surfaces in the Teams Inbox "Needs Attention" band
(``InboxPage`` -> ``NeedsAttentionBand`` -> ``ApprovalRow``, which already
labels ``hire_agent`` as "Hire Agent"). The roster changes only on approve.

**This does not touch the direct-create flow.** Seeding Friday/Atlas,
marketplace deploys, and Atlas builds keep using ``POST /companies/{id}/agents``
unchanged. In particular it needs no ``require_board_approval_for_new_agents``
flip — that flag turns the direct-create route into a fleet-wide 409, which
would break signup seeding, where no human is awake to approve Friday and Atlas.

The shape, and why each step is there:

1. **Isol8 creates the agent** (``create_paperclip_for_marketplace``) so it
   carries the ed25519 operator device keypair Isol8 mints and owns. Paperclip
   cannot mint that, and without it the agent is rejected NOT_PAIRED on its
   first relayed connect.
2. **Isol8 PATCHes it to ``pending_approval``** — inert: Paperclip lists that
   status in both ``NON_ASSIGNABLE`` and ``NON_INVOKABLE``, so it cannot be
   given work or woken while the owner decides.
3. **The approval carries ``payload.agentId``.** Load-bearing: the approve
   RESPONSE returns only the approval row and never the id of an agent it
   created, so an approval WITHOUT ``agentId`` would leave the deploy step with
   nothing to address. With it, approve runs ``activatePendingApproval`` (sets
   ``idle``, guarded on the row still being pending) and reject runs
   ``terminate``.
4. **Approve installs the workspace** (``deploy_for_approval``, called from
   ``routers/teams/approvals.py``). Paperclip owns the roster entry; the EFS
   workspace + ``openclaw.json`` merge is Isol8's half, and it runs on the
   approving user's own authenticated request — no privileged write path, no
   event plumbing.

The keypair is deliberately NOT copied into the approval payload: the agent row
already holds it, and a duplicate at-rest copy of a private key buys nothing,
because ``activatePendingApproval`` ignores the payload's ``adapterConfig``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Markers on the approval payload's metadata identifying a marketplace hire
# (rather than an agent someone hand-created in Teams). Their presence is what
# arms the post-approve install, so every other approval type flows through the
# approve handler untouched.
LISTING_SLUG_KEY = "isol8_listing_slug"
# The canonical identity — slugs are non-unique, so the install resolves by this.
LISTING_ID_KEY = "isol8_listing_id"

PENDING_APPROVAL_STATUS = "pending_approval"

_TITLE_MAX = 120


def build_hire_approval_payload(*, listing: dict, agent_id: str) -> dict:
    """Payload for a ``hire_agent`` approval over a PRE-CREATED agent.

    ``agentId`` is the whole point. With it set, approve calls
    ``activatePendingApproval(agentId)`` and reject calls ``terminate(agentId)``
    — both addressing an agent Isol8 already created and therefore already knows
    the id of. Without it, approve would CREATE an agent and return only the
    approval row, leaving the install step unable to say which agent to give a
    workspace to.

    ``adapterType``/``adapterConfig`` are deliberately absent: they are read
    only on the create branch, which ``agentId`` bypasses, and the config holds
    a private key that should not get a second at-rest copy.

    name/title/capabilities are what the Teams inbox renders — its
    ``approvalSubject`` reads ``payload.title`` first, then ``payload.name``.
    """
    tags = listing.get("tags") or []
    return {
        "agentId": agent_id,
        "name": listing.get("name", ""),
        "role": "general",
        "title": (listing.get("summary") or "")[:_TITLE_MAX],
        "capabilities": ", ".join(tags),
        "budgetMonthlyCents": 0,
        "metadata": {
            LISTING_SLUG_KEY: listing.get("slug", ""),
            LISTING_ID_KEY: listing.get("listing_id", ""),
        },
    }


def listing_id_of(payload: dict | None) -> str | None:
    """The marketplace listing id on an approval payload, or None when the
    approval is not a marketplace hire.

    Returning None for everything else is what keeps the post-approve install
    invisible to every other approval type.
    """
    return ((payload or {}).get("metadata") or {}).get(LISTING_ID_KEY) or None


def listing_slug_of(payload: dict | None) -> str | None:
    """The marketplace slug on an approval payload — display/logging only. The
    install resolves by :func:`listing_id_of`, since slugs are non-unique."""
    return ((payload or {}).get("metadata") or {}).get(LISTING_SLUG_KEY) or None


def hired_agent_id_of(payload: dict | None) -> str | None:
    """The pre-created agent id this approval governs."""
    agent_id = (payload or {}).get("agentId")
    return agent_id if isinstance(agent_id, str) and agent_id else None


async def create_hire_approval(
    *, owner_id: str, issue_id: str, listing: dict, requested_by_user_id: str | None = None
) -> str | None:
    """Pre-create the agent inert, then file a ``hire_agent`` approval on
    ``issue_id``.

    ``issueIds`` is what makes the request show up ON the task that prompted it
    (upstream links them via ``issueApprovalsSvc.linkManyForApproval``) rather
    than only in a global list the owner has no reason to open.

    Rolls the agent back if anything after the create fails, so a failed ask
    never leaves an orphan in the roster. Returns the approval id, or None when
    the owner has no Paperclip company — a degraded state a recommendation must
    not raise on, since the listings are still worth returning.
    """
    import httpx

    from core.config import settings
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie
    from core.services.paperclip_agent_lifecycle import create_paperclip_for_marketplace
    from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        logger.info("create_hire_approval: no paperclip company for owner=%s", owner_id)
        return None

    # Step 1 — create the agent so it carries Isol8's device keypair.
    agent = await create_paperclip_for_marketplace(
        buyer_user_id=requested_by_user_id or owner_id,
        owner_id=owner_id,
        name=listing.get("name") or listing.get("slug") or "New agent",
        role=None,
    )
    agent_id = agent["id"]

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        # Step 2 — park it inert before it can be assigned work or woken.
        await admin.update_agent(
            session_cookie=cookie,
            agent_id=agent_id,
            status=PENDING_APPROVAL_STATUS,
        )
        # Step 3 — the ask itself, pinned to the issue that prompted it.
        created = await admin.create_approval(
            session_cookie=cookie,
            company_id=company_id,
            body={
                "type": "hire_agent",
                "payload": build_hire_approval_payload(listing=listing, agent_id=agent_id),
                "issueIds": [issue_id],
            },
        )
        return created.get("id")
    except Exception:
        logger.exception(
            "create_hire_approval: failed after creating agent %s for owner=%s — rolling back",
            agent_id,
            owner_id,
        )
        await _rollback_agent(owner_id=owner_id, agent_id=agent_id)
        raise
    finally:
        await http.aclose()


async def deploy_for_approval(*, approval: dict, owner_id: str) -> None:
    """Install the marketplace workspace for a just-approved hire.

    A no-op for every approval that is not a marketplace hire, which is how
    this stays invisible to the existing approve flow.

    Paperclip has already flipped the agent to ``idle``; this adds the half it
    knows nothing about — the EFS workspace, the ``openclaw.json`` merge, and
    the operator-device pairing without which the agent is rejected NOT_PAIRED
    on its first relayed connect.

    Serialized against every other agent install for this owner by the SAME
    owner-wide deploy claim the buyer-facing hire takes. This is a marketplace
    install like any other, and the Teams "Needs Attention" band lists several
    approvals at once — approving three in a row otherwise reproduces the
    2026-08-06 incident (three concurrent installs against one container, two
    500s) on a different surface.

    On failure the agent is removed rather than left in the roster with no
    workspace behind it, and the error re-raised so the approving user learns
    the hire did not complete.
    """
    payload = (approval or {}).get("payload") or {}
    listing_id = listing_id_of(payload)
    agent_id = hired_agent_id_of(payload)
    if not listing_id or not agent_id:
        if listing_slug_of(payload) or listing_id:
            # Recognisably a marketplace hire, yet missing something the install
            # needs. Never let that pass as a silent no-op: the owner would see
            # "approved" and get an agent with no workspace.
            logger.error(
                "deploy_for_approval: marketplace hire approval %s is missing "
                "listing_id/agentId (listing_id=%r agent_id=%r) — NOT installed",
                (approval or {}).get("id"),
                listing_id,
                agent_id,
            )
        return

    from core.containers import get_ecs_manager
    from core.repositories import container_repo
    from core.services import marketplace_service
    from core.services.catalog_service import get_catalog_service
    from core.services.paperclip_openclaw_pairing import ensure_operator_device_paired

    listing = await marketplace_service.get_published_by_id(listing_id=listing_id)
    if not listing or listing.get("status") != "published":
        # Retired between the ask and the decision.
        await _rollback_agent(owner_id=owner_id, agent_id=agent_id)
        raise RuntimeError(f"listing {listing_id} is no longer published")

    claim_token, _held = await container_repo.try_claim_owner_deploy(owner_id, listing_id=listing_id)
    if not claim_token:
        # Fast refusal, never a server-side wait. The agent is left ALONE — it is
        # ``idle`` in the roster with nothing installed, so there is no partial
        # state to clean, and deleting it would throw away an approval the owner
        # already granted because some unrelated install happened to be running.
        # The owner re-runs the hire from the marketplace drawer, or removes the
        # agent from Teams.
        raise RuntimeError("another agent install is already running for this workspace — try again in a moment")

    try:
        try:
            ecs = get_ecs_manager()
            container, container_ip = await ecs.resolve_running_container(owner_id)
            if container is None or container_ip is None:
                raise RuntimeError("container is not running — retry the approval in a moment")

            tar_bytes, slice_ = _fetch_artifact(listing)
            await get_catalog_service().deploy_from_artifact(
                owner_id=owner_id,
                slug=listing.get("slug"),
                manifest=_manifest_for(listing),
                slice_=slice_,
                tar_bytes=tar_bytes,
                new_agent_id=agent_id,
                container_ip=container_ip,
                container_token=container["gateway_token"],
            )
        except Exception as exc:
            logger.exception(
                "deploy_for_approval: install failed for agent=%s listing=%s — rolling back",
                agent_id,
                listing_id,
            )
            await _rollback_agent(owner_id=owner_id, agent_id=agent_id, cause=exc)
            raise

        # Trust the agent's operator device AFTER the install is committed, and
        # best-effort like every other create path: the pairing helper needs the
        # FULL agent record (adapterConfig.devicePrivateKeyPem — Paperclip echoes
        # it to admin actors), and a pairing hiccup must not roll back a completed
        # install — the paired.json fallback pairs the device at next container
        # boot instead. Still under the claim: it talks to the same container.
        try:
            import httpx

            from core.config import settings
            from core.services.paperclip_admin_client import PaperclipAdminClient
            from core.services.paperclip_admin_session import get_admin_session_cookie

            async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
                admin = PaperclipAdminClient(http_client=http)
                cookie = await get_admin_session_cookie(http)
                full_agent = await admin.get_agent(session_cookie=cookie, agent_id=agent_id)
            await ensure_operator_device_paired(
                owner_id=owner_id,
                paperclip_agent=full_agent,
                gateway_token=container["gateway_token"],
                ip=container_ip,
            )
        except Exception:  # noqa: BLE001 — pairing must not undo a committed install
            logger.exception(
                "deploy_for_approval: device pairing failed for agent=%s (pairs at next container boot)",
                agent_id,
            )
    finally:
        await container_repo.release_owner_deploy_claim(owner_id, listing_id=listing_id, token=claim_token)


def _fetch_artifact(listing: dict) -> tuple[bytes, dict]:
    """Tarball + openclaw slice for a published listing.

    Thin seam so ``deploy_for_approval`` reads linearly and can be patched in
    tests without S3. Delegates to the SAME helper the buyer-facing deploy uses,
    so the two install paths cannot drift.
    """
    from routers.marketplace_listings import _fetch_agent_artifact

    return _fetch_agent_artifact(listing)


def _manifest_for(listing: dict) -> dict:
    """The manifest the installer passes through.

    Mirrors ``deploy_listing``: the manifest on the listing ROW is authoritative
    for slug/name/version, not anything in S3. ``deploy_from_artifact`` reads
    only ``version`` and ``name`` from it.
    """
    manifest = dict(listing.get("manifest_json") or {})
    manifest.setdefault("version", listing.get("version", 1))
    manifest.setdefault("name", listing.get("name", listing.get("slug")))
    return manifest


async def _rollback_agent(*, owner_id: str, agent_id: str, cause: BaseException | None = None) -> None:
    """Best-effort removal; a cleanup failure must not replace the real error.

    Unwinds the container/EFS side FIRST (openclaw.json ``agents.list`` entry +
    the extracted ``workspace-{id}/`` dir, sidecar included), then the Paperclip
    row — the same two-sided rollback the buyer-facing deploy does. This path
    used to delete only the Paperclip row, which on a failed install stranded
    the workspace and a possible half-created container-side agent with nothing
    left to see or remove them by.

    ``cause`` carries the failure into ``unwind_partial_deploy``, which skips the
    unwind on a client-side ``TimeoutError`` (the container may still be
    mid-write). When it skips, the Paperclip row is kept too, so the half-install
    stays a visible, deletable agent in Teams instead of a ghost — see that
    method's docstring. Called with no ``cause`` for the "listing retired between
    ask and decision" path, where nothing was installed at all.
    """
    from core.services.catalog_service import get_catalog_service
    from core.services.paperclip_agent_lifecycle import delete_paperclip_for_marketplace

    try:
        unwound = await get_catalog_service().unwind_partial_deploy(
            owner_id=owner_id, new_agent_id=agent_id, cause=cause
        )
    except Exception:
        logger.exception("marketplace_hire: unwind failed for agent %s", agent_id)
        unwound = True  # container side is unknown; still drop the Paperclip row

    if not unwound:
        logger.warning(
            "marketplace_hire: keeping paperclip agent %s for owner=%s — container-side install was NOT "
            "unwound (client-side timeout), so it stays visible + deletable in Teams",
            agent_id,
            owner_id,
        )
        return

    try:
        await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=agent_id)
    except Exception:
        logger.exception("marketplace_hire: rollback failed for agent %s", agent_id)
