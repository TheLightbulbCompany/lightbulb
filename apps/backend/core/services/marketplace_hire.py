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

import asyncio
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

    Reuses both the agent and an already-filed approval when a caller retries
    after losing a response. Definitive failures roll the agent back; ambiguous
    transport/upstream failures keep it visible so the retry can reconcile.
    Returns the approval id, or None when the owner has no Paperclip company —
    a degraded state a recommendation must not raise on, since the listings are
    still worth returning.
    """
    import httpx

    from core.config import settings
    from core.repositories import container_repo
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import (
        PaperclipAdminClient,
        PaperclipAgentCreateReplayMiss,
    )
    from core.services.paperclip_admin_session import get_admin_session_cookie
    from core.services.agent_budget import is_at_agent_budget
    from core.services.paperclip_agent_idempotency import build_agent_create_idempotency_key
    from core.services.paperclip_agent_lifecycle import create_paperclip_for_marketplace
    from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

    # Paid listings never ride the approval path unpurchased: filing the ask
    # arms deploy_for_approval, which installs on approve — a free install of
    # a paid artifact. Seller-self is exempt, mirroring the buyer route's
    # bypass. The passed ``listing`` is the compact search projection (no
    # seller_id), so re-read the full row. Returning None (not raising) keeps
    # the recommend response degraded-but-useful, same as the no-company case.
    if int(listing.get("price_cents", 0) or 0) > 0:
        from core.repositories import marketplace_repo
        from core.services import marketplace_service

        row = await marketplace_service.get_published_by_id(listing_id=listing.get("listing_id"))
        if not row or row.get("status") != "published":
            return None
        if row.get("seller_id") != owner_id and not await marketplace_repo.has_unrevoked_purchase(
            buyer_id=owner_id, listing_id=row["listing_id"]
        ):
            logger.info(
                "create_hire_approval: owner=%s holds no purchase for paid listing %s — not filing",
                owner_id,
                row["listing_id"],
            )
            return None

    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        logger.info("create_hire_approval: no paperclip company for owner=%s", owner_id)
        return None

    listing_id = listing["listing_id"]
    listing_version = int(listing.get("version", 1) or 1)
    idempotency_key = build_agent_create_idempotency_key(
        operation="marketplace-approval",
        owner_id=owner_id,
        intent={
            "issueId": issue_id,
            "listingId": listing_id,
            "version": listing_version,
        },
    )

    # The same owner-wide claim used by installs also closes the concurrent
    # list-then-create approval race. Unlike workspace deploys this short
    # critical section fails CLOSED on an unprovable DynamoDB claim; /recommend
    # can safely omit the hire suggestion and retry later.
    claim_label = f"approval:{issue_id}:{listing_id}:{listing_version}"
    claim_token, _held = await container_repo.try_claim_owner_deploy(
        owner_id,
        listing_id=claim_label,
        fail_open=False,
    )
    if not claim_token:
        logger.info(
            "create_hire_approval: owner=%s has another agent operation in flight — not filing",
            owner_id,
        )
        return None

    operation_cause: BaseException | None = None
    http: httpx.AsyncClient | None = None
    agent_id: str | None = None
    try:
        # At the cap, only an existing operation may replay. A missing key is a
        # normal degraded recommendation, not a roster mutation or API failure.
        at_agent_budget = await is_at_agent_budget(owner_id)
        try:
            agent = await create_paperclip_for_marketplace(
                buyer_user_id=requested_by_user_id or owner_id,
                owner_id=owner_id,
                name=listing.get("name") or listing.get("slug") or "New agent",
                role=None,
                idempotency_key=idempotency_key,
                idempotency_replay_only=at_agent_budget,
            )
        except PaperclipAgentCreateReplayMiss:
            logger.info(
                "create_hire_approval: owner=%s is at the agent budget with no matching operation — not filing",
                owner_id,
            )
            return None
        agent_id = agent["id"]

        http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        approvals_payload = await admin.list_approvals(
            session_cookie=cookie,
            company_id=company_id,
        )
        approvals = approvals_payload if isinstance(approvals_payload, list) else approvals_payload.get("approvals", [])
        existing = next(
            (
                approval
                for approval in approvals
                if isinstance(approval, dict)
                and approval.get("type") == "hire_agent"
                and hired_agent_id_of(approval.get("payload")) == agent_id
            ),
            None,
        )
        if existing:
            return existing.get("id")

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
    except Exception as exc:
        operation_cause = exc
        if agent_id is not None:
            retryable = isinstance(exc, (httpx.RequestError, TimeoutError)) or bool(getattr(exc, "retryable", False))
            if retryable:
                logger.warning(
                    "create_hire_approval: ambiguous failure after creating agent %s for owner=%s — preserving for retry",
                    agent_id,
                    owner_id,
                )
            else:
                logger.exception(
                    "create_hire_approval: definitive failure after creating agent %s for owner=%s — rolling back",
                    agent_id,
                    owner_id,
                )
                await _rollback_agent(owner_id=owner_id, agent_id=agent_id, cause=exc)
        raise
    finally:
        if http is not None:
            await http.aclose()
        await container_repo.release_owner_deploy_claim(
            owner_id,
            listing_id=claim_label,
            token=claim_token,
            cause=operation_cause,
        )


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
    from core.repositories import container_repo, marketplace_repo
    from core.services import marketplace_service
    from core.services.catalog_service import emit_agent_deployed, get_catalog_service
    from core.services.paperclip_openclaw_pairing import ensure_operator_device_paired

    listing = await marketplace_service.get_published_by_id(listing_id=listing_id)
    if not listing or listing.get("status") != "published":
        # Retired between the ask and the decision.
        await _rollback_agent(owner_id=owner_id, agent_id=agent_id)
        raise RuntimeError(f"listing {listing_id} is no longer published")

    # Entitlement re-check, mirroring the buyer route's 403: defense in depth
    # against an approval filed before the gate in ``create_hire_approval``
    # existed (or a purchase revoked while the ask sat in the inbox). Seller-
    # self exempt, like the buyer route.
    if int(listing.get("price_cents", 0) or 0) > 0 and listing.get("seller_id") != owner_id:
        if not await marketplace_repo.has_unrevoked_purchase(buyer_id=owner_id, listing_id=listing_id):
            await _rollback_agent(owner_id=owner_id, agent_id=agent_id)
            raise RuntimeError("purchase required to deploy this listing")

    try:
        claim_token, _held = await container_repo.try_claim_owner_deploy(owner_id, listing_id=listing_id)
    except Exception:
        # ``try_claim_owner_deploy`` fails OPEN and is documented never to raise,
        # so this is belt-and-braces — but the belt has to exist HERE, because
        # this is the one call site where escaping the function before the
        # rollback envelope strands something: the approval is already spent
        # (see below), so an approved agent would sit in the roster with no
        # workspace and no route left to install one.
        logger.exception(
            "deploy_for_approval: deploy claim raised for agent=%s listing=%s — rolling back",
            agent_id,
            listing_id,
        )
        await _rollback_agent(owner_id=owner_id, agent_id=agent_id)
        raise
    if not claim_token:
        # Fast refusal, never a server-side wait: this runs INSIDE the approving
        # user's HTTP request (POST /teams/approvals/{id}/approve), which must
        # not hold a long server-side wait.
        #
        # The agent goes with it. The approval is already SPENT by the time we
        # get here — the route calls Paperclip's approve first, flipping the
        # agent ``pending_approval`` -> ``idle`` — so leaving the agent standing
        # would strand it: no route will ever install its workspace again, and
        # the re-hire from the marketplace drawer mints a SECOND agent beside
        # permanent roster litter. Rolling back is safe and cheap here: nothing
        # was installed, so ``_rollback_agent`` with no ``cause`` is a no-op on
        # the container side and just drops the Paperclip row. The owner
        # re-hires from the drawer onto a clean roster.
        await _rollback_agent(owner_id=owner_id, agent_id=agent_id)
        raise RuntimeError("another agent install is already running for this workspace — try again in a moment")

    # What the install failed with, threaded into the claim release: a
    # client-side TimeoutError HOLDS the claim rather than releasing it, so the
    # next install can't start while the container may still be writing (see
    # ``container_repo.release_owner_deploy_claim``).
    deploy_cause: BaseException | None = None
    try:
        try:
            ecs = get_ecs_manager()
            container, container_ip = await ecs.resolve_running_container(owner_id)
            if container is None or container_ip is None:
                raise RuntimeError("container is not running — retry the approval in a moment")

            # S3 fetch is blocking boto3 I/O — keep it off the event loop.
            tar_bytes, slice_ = await asyncio.to_thread(_fetch_artifact, listing)
            await get_catalog_service().deploy_from_artifact(
                owner_id=owner_id,
                slug=listing.get("slug"),
                manifest=_manifest_for(listing),
                slice_=slice_,
                tar_bytes=tar_bytes,
                new_agent_id=agent_id,
                container_ip=container_ip,
                container_token=container["gateway_token"],
                listing_id=listing.get("listing_id"),
                seller_id=listing.get("seller_id"),
            )
        except Exception as exc:
            logger.exception(
                "deploy_for_approval: install failed for agent=%s listing=%s — rolling back",
                agent_id,
                listing_id,
            )
            deploy_cause = exc
            await _rollback_agent(owner_id=owner_id, agent_id=agent_id, cause=exc)
            raise

        # Committed: the install survived its rollback envelope. Emitting from
        # inside deploy_from_artifact counted hires the rollback had undone.
        emit_agent_deployed(
            owner_id=owner_id,
            slug=listing.get("slug"),
            agent_id=agent_id,
            seller_id=listing.get("seller_id"),
        )

        # Seller-dashboard counter — never worth failing a committed install.
        try:
            await marketplace_repo.increment_listing_install_count(
                listing_id=listing_id, version=int(listing.get("version", 1) or 1)
            )
        except Exception:
            logger.exception("deploy_for_approval: install_count bump failed for listing %s", listing_id)

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
        await container_repo.release_owner_deploy_claim(
            owner_id, listing_id=listing_id, token=claim_token, cause=deploy_cause
        )


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
        # ``unwind_partial_deploy`` is documented never to raise, so this is the
        # unknown-unknown branch. Treat it like a failed unwind, not a completed
        # one: the container side may still hold the workspace, the agents.list
        # entry and the sidecar, and deleting the Paperclip row on top of that is
        # what manufactures an unreachable ghost. Keeping the row is recoverable
        # (delete it from Teams); a ghost is not.
        logger.exception("marketplace_hire: unwind failed for agent %s", agent_id)
        unwound = False

    if not unwound:
        logger.warning(
            "marketplace_hire: keeping paperclip agent %s for owner=%s — container-side install was NOT "
            "safely unwound (client-side timeout, the sidecar unlink failed, or the unwind itself "
            "raised), so it stays visible + deletable in Teams",
            agent_id,
            owner_id,
        )
        return

    try:
        await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=agent_id)
    except Exception:
        logger.exception("marketplace_hire: rollback failed for agent %s", agent_id)
