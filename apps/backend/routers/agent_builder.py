"""Agent-callable bridge for Atlas to register a built agent into the owner's
container — the backend-mediated "create" Atlas cannot do gateway-free in-container.

Atlas (the seeded recruiter, running inside the owner's OpenClaw container) builds
a new specialist agent's files into a *staging* workspace dir on EFS, then POSTs
here. The backend snapshots that dir (``agent_export``), mints a Paperclip agent
(``create_paperclip_for_marketplace``), and deploys the snapshot into the owner's
container (``deploy_from_artifact`` — extracts files into ``workspaces/{uuid}/`` and
registers the agent live in ``openclaw.json`` + the Paperclip roster). The new agent
then shows up in the owner's roster and the seller publish wizard, where the **owner**
publishes it — Atlas never publishes directly.

Auth: per-container ``hook_token`` (Bearer, ``hmac.compare_digest``), the same model
as ``hooks_proxy``. This router does NOT use ``Depends(get_current_user)`` — the
in-container caller has no Clerk token. The token scopes the call to the owner's own
container, so an agent can only register into the workspace it already runs in.
"""

from __future__ import annotations

import hmac
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.containers import get_ecs_manager
from core.repositories import container_repo
from core.services import agent_export
from core.services.catalog_service import get_catalog_service
from core.services.paperclip_agent_lifecycle import (
    create_paperclip_for_marketplace,
    delete_paperclip_for_marketplace,
)
from core.services.paperclip_meta import derive_from_listing

logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterBuiltAgentRequest(BaseModel):
    """Body for ``POST /agent-builder/u/{owner_id}/register-built-agent``."""

    # The workspace dir name (``workspaces/{staging_agent_id}/``) Atlas built the
    # new agent's files into. The backend snapshots this dir.
    staging_agent_id: str = Field(..., min_length=4, max_length=64)
    # Display name for the new agent.
    name: str = Field(..., min_length=1, max_length=120)
    # Optional org placement (Paperclip agent id the new hire reports to).
    reports_to: str | None = None


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64] or "agent"


async def _require_hook_token(owner_id: str, request: Request) -> dict:
    """Authorize via the owner's per-container ``hook_token`` (timing-safe).

    Returns the ``containers`` row so the caller can reuse it. Mirrors
    ``hooks_proxy.proxy_hook`` — same token, same compare.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = auth[len("Bearer ") :]
    container = await container_repo.get_by_owner_id(owner_id)
    expected = (container or {}).get("hook_token") or ""
    if not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid hook token")
    return container


@router.post("/u/{owner_id}/register-built-agent")
async def register_built_agent(owner_id: str, payload: RegisterBuiltAgentRequest, request: Request):
    """Register an Atlas-built agent into the owner's container.

    Snapshot the staging workspace → mint a Paperclip agent (sharing the UUID) →
    deploy the snapshot into the container (files + openclaw.json registration).
    Returns the new ``agent_id`` so Atlas can report it and the owner can publish it.
    """
    await _require_hook_token(owner_id, request)

    # 1. Snapshot the staging workspace Atlas built. Friday guard + credential
    #    scrub run inside export_agent_from_efs. EFS is keyed by owner_id.
    try:
        package = agent_export.export_agent_from_efs(
            seller_id=owner_id,
            agent_id=payload.staging_agent_id,
        )
    except agent_export.InvalidAgentIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except agent_export.AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except agent_export.AgentNotInConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except agent_export.CredentialLeakError as e:
        raise HTTPException(status_code=400, detail=str(e))

    slice_ = package.openclaw_slice or {}
    meta = derive_from_listing(name=payload.name, description_md=None)

    # 2. Mint the Paperclip agent (shares the UUID threaded into deploy as
    #    new_agent_id). buyer_user_id = owner_id (the container's manual-wake key).
    paperclip_agent = await create_paperclip_for_marketplace(
        buyer_user_id=owner_id,
        owner_id=owner_id,
        name=payload.name,
        role=None,
        meta=meta,
        reports_to=payload.reports_to,
    )
    new_agent_id = paperclip_agent["id"]

    # 3. Deploy the snapshot into the running container (write files + register
    #    live in openclaw.json). Roll back the orphan Paperclip agent on any
    #    post-mint failure — same envelope as the marketplace deploy path.
    try:
        ecs = get_ecs_manager()
        container, container_ip = await ecs.resolve_running_container(owner_id)
        if container is None or container_ip is None:
            raise HTTPException(status_code=503, detail="container starting — retry in a moment")

        catalog = get_catalog_service()
        await catalog.deploy_from_artifact(
            owner_id=owner_id,
            slug=_slugify(payload.name),
            manifest=package.manifest,
            slice_=slice_,
            tar_bytes=package.tarball_bytes,
            new_agent_id=new_agent_id,
            container_ip=container_ip,
            container_token=container["gateway_token"],
        )
    except Exception as exc:  # noqa: BLE001 — roll back the orphan, re-raise
        logger.exception(
            "register_built_agent: post-mint step failed for owner=%s staging=%s — rolling back %s",
            owner_id,
            payload.staging_agent_id,
            new_agent_id,
        )
        try:
            await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=new_agent_id)
        except Exception:
            logger.exception("register_built_agent: rollback failed for agent %s", new_agent_id)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail="container unavailable — retry the build in a moment") from exc

    return {
        "agent_id": new_agent_id,
        "name": payload.name,
        "registered": True,
        # The owner publishes this via the seller wizard; surface the agent_id so
        # Atlas can tell the operator exactly which agent to publish.
        "publish_hint": "Open the marketplace publish wizard and pick this agent to list it.",
    }
