"""Marketplace hires that route through Paperclip's approval surface.

A headless Friday cannot hire. She can only ask. This module turns that ask
into a Paperclip ``hire_agent`` approval linked to the issue that prompted it,
which surfaces in the Teams Inbox "Needs Attention" band
(``InboxPage`` -> ``NeedsAttentionBand`` -> ``ApprovalRow``). The roster
changes only when the owner approves.

Split of responsibility on approve, which is the thing to keep straight:

* **Paperclip** creates the ROSTER entry (``services/approvals.ts``, the
  ``hire_agent`` branch) and applies any budget policy.
* **Isol8** deploys the marketplace ARTIFACT into the container — the EFS
  workspace plus the ``openclaw.json`` merge. Paperclip knows nothing about
  either.

Both halves run on the approving user's own authenticated request
(``routers/teams/approvals.py``), so there is no privileged write path and no
event plumbing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Marker on the approval payload's metadata that identifies a hire as a
# marketplace deploy (rather than a hand-rolled agent someone created in
# Teams). Its presence is what arms the post-approve artifact deploy, so every
# non-marketplace approval flows through the approve handler untouched.
LISTING_SLUG_KEY = "isol8_listing_slug"
# The canonical identity — slugs are non-unique, so the post-approve deploy
# resolves the artifact by this.
LISTING_ID_KEY = "isol8_listing_id"

_TITLE_MAX = 120


def build_hire_approval_payload(*, listing: dict, adapter_config: dict) -> dict:
    """Payload for a Paperclip ``hire_agent`` approval.

    On approve, Paperclip builds the agent FROM THIS PAYLOAD
    (``services/approvals.ts``: ``agentsSvc.create(companyId, {...
    adapterConfig: payload.adapterConfig ...})``). So ``adapter_config`` must
    be the full ``synthesize_openclaw_adapter`` output — in particular the
    ed25519 ``devicePrivateKeyPem``. Isol8 mints and owns that keypair so the
    deviceId is stable and can be pre-registered in the container's
    ``paired.json``; an operator device that is not pre-paired is rejected
    NOT_PAIRED, and the agent would sit broken until the next reprovision.
    Passing a bare ``{"url": ...}`` here is exactly that bug.

    It survives the round trip: Paperclip's
    ``normalizeHireApprovalPayloadForPersistence`` only rewrites
    ``adapterConfig.env`` and passes every other key through, and
    ``redactApprovalPayload`` shapes the HTTP RESPONSE only — approve reads the
    unredacted stored row.

    ``adapterType`` is set EXPLICITLY and must stay that way: the approve path
    does ``String(payload.adapterType ?? "process")``, and a ``"process"``
    agent is unreachable over the openclaw gateway.

    ``agentId`` is deliberately ABSENT. With it set, approve merely activates a
    pre-created agent; without it, approve CREATES the agent — so nothing
    exists in the owner's roster until they say yes.
    """
    tags = listing.get("tags") or []
    return {
        "name": listing.get("name", ""),
        "role": "general",
        "title": (listing.get("summary") or "")[:_TITLE_MAX],
        "capabilities": ", ".join(tags),
        "adapterType": "openclaw_gateway",
        "adapterConfig": adapter_config,
        "budgetMonthlyCents": 0,
        "metadata": {
            LISTING_SLUG_KEY: listing.get("slug", ""),
            LISTING_ID_KEY: listing.get("listing_id", ""),
        },
    }


def listing_slug_of(payload: dict | None) -> str | None:
    """The marketplace slug on an approval payload, or None if it is not a
    marketplace hire.

    Returning None for everything else is what keeps the post-approve deploy
    hook invisible to the existing approve flow.
    """
    slug = ((payload or {}).get("metadata") or {}).get(LISTING_SLUG_KEY)
    return slug or None


async def create_hire_approval(
    *, owner_id: str, issue_id: str, listing: dict, requested_by_user_id: str | None = None
) -> str | None:
    """Create a PENDING ``hire_agent`` approval linked to ``issue_id``.

    This is the headless half of hiring. In chat the owner gets a card with a
    Deploy button; when Friday is working a Paperclip task with nobody
    watching, she files this instead. Creating it changes nothing — the roster
    moves only when the owner approves in the Teams Inbox.

    ``issueIds`` is what makes the request show up ON the task that prompted
    it (upstream links them via ``issueApprovalsSvc.linkManyForApproval``)
    rather than only in a global list the owner has no reason to open.

    **This does NOT touch the direct-create flow.** Seeding Friday/Atlas,
    marketplace deploys, and Atlas builds keep using
    ``POST /companies/{id}/agents`` untouched. In particular this needs no
    ``require_board_approval_for_new_agents`` flip — that flag switches the
    direct-create route to a hard 409 fleet-wide, which would break signup
    seeding (no human is awake to approve Friday and Atlas).

    .. note:: NOT CALLED YET. Two pieces land with it:

       1. **Pre-create + ``pending_approval``.** The approve RESPONSE returns
          only the approval row, never the created agent's id, so the deploy
          step would have nothing to address. Create the agent first (this
          keeps the keypair ours), PATCH it to ``pending_approval`` — genuinely
          inert, it is in both ``NON_ASSIGNABLE`` and ``NON_INVOKABLE`` — and
          pass its id as ``payload.agentId``. Approve then runs
          ``activatePendingApproval`` (sets ``idle``, guarded on the row still
          being pending) and reject runs ``terminate``. Needs a ``status``
          argument on ``PaperclipAdminClient.update_agent``, which it lacks.
       2. **The post-approve artifact deploy** in
          ``routers/teams/approvals.py::approve`` — ``deploy_from_artifact``
          plus ``ensure_operator_device_paired``, with rollback. Paperclip makes
          the roster entry; the EFS workspace is Isol8's half.

    Returns the approval id, or None when the owner has no Paperclip company —
    a degraded state a recommendation must not raise on, since the listings
    are still worth returning.
    """
    import httpx

    from core.config import settings
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services import service_token
    from core.services.paperclip_adapter_config import synthesize_openclaw_adapter
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie
    from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner
    from core.services.paperclip_provisioning import _ws_gateway_url

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
        if company_id is None:
            logger.info("create_hire_approval: no paperclip company for owner=%s", owner_id)
            return None
        # The full adapter config — including the ed25519 device keypair Isol8
        # mints and owns — because Paperclip builds the agent FROM the payload
        # on approve. See build_hire_approval_payload.
        adapter_config = synthesize_openclaw_adapter(
            gateway_url=_ws_gateway_url(settings.ENVIRONMENT or ""),
            service_token=service_token.mint(owner_id),
            user_id=requested_by_user_id or owner_id,
        )
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        created = await admin.create_approval(
            session_cookie=cookie,
            company_id=company_id,
            body={
                "type": "hire_agent",
                "payload": build_hire_approval_payload(listing=listing, adapter_config=adapter_config),
                "issueIds": [issue_id],
            },
        )
        return created.get("id")
    finally:
        await http.aclose()
