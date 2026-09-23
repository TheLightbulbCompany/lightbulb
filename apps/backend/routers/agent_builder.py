"""Agent-callable bridge for Atlas to register a built agent into the owner's
container — the backend-mediated "create" Atlas cannot do gateway-free in-container.

Atlas (the seeded recruiter, running inside the owner's OpenClaw container) builds
a new specialist agent's files into a *staging* workspace dir on EFS, then POSTs
here. The backend snapshots that dir (``agent_export``), mints a Paperclip agent
(``create_paperclip_for_marketplace``), and deploys the snapshot into the owner's
container (``deploy_from_artifact`` — extracts files into ``workspace-{uuid}/`` and
registers the agent live in ``openclaw.json`` + the Paperclip roster). The new agent
then shows up in the owner's roster and the seller publish wizard, where the **owner**
publishes it — Atlas never publishes directly.

Auth: per-container ``hook_token`` (Bearer, ``hmac.compare_digest``), the same model
as the agent-mail routes. This router does NOT use ``Depends(get_current_user)`` — the
in-container caller has no Clerk token. The token scopes the call to the owner's own
container, so an agent can only register into the workspace it already runs in.
"""

from __future__ import annotations

import hmac
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.repositories import container_repo
from core.services import agent_export
from core.services.catalog_package import CatalogPackage
from core.services.catalog_service import emit_agent_deployed, get_catalog_service
from core.services.paperclip_agent_lifecycle import (
    create_paperclip_for_marketplace,
    delete_paperclip_for_marketplace,
)
from core.services.paperclip_agent_idempotency import build_agent_create_idempotency_key
from core.services.paperclip_meta import derive_from_listing
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterBuiltAgentRequest(BaseModel):
    """Body for ``POST /agent-builder/u/{owner_id}/register-built-agent``."""

    # The workspace dir name (``workspace-{staging_agent_id}/``) Atlas built the
    # new agent's files into. The backend snapshots this dir. min_length=3 to
    # match the path-validation regex (_AGENT_ID_RE) inside _resolve_agent_dir,
    # so a short-but-valid id reaches that gate instead of a confusing 422.
    #
    # This is a build-scoped STAGING TOKEN (``atlas-staging-{build_id}``), not a
    # proposed agent id: it selects a directory and nothing else. The agent's id
    # is minted here (Paperclip) and returned in the response — the caller has no
    # way to propose one, by design.
    staging_agent_id: str = Field(..., min_length=3, max_length=64)
    # Display name for the new agent. The ONE identity field the builder chooses.
    name: str = Field(..., min_length=1, max_length=120)
    # Optional org placement (Paperclip agent id the new hire reports to).
    # None defaults to the owner's apex CEO (Friday) so built agents never
    # land unparented in the org chart.
    reports_to: str | None = None


class RegisterBuiltAgentResponse(BaseModel):
    """Response for ``POST /agent-builder/u/{owner_id}/register-built-agent``."""

    agent_id: str
    name: str
    registered: bool
    # Hint Atlas relays to the owner: the agent is registered but not yet listed;
    # the owner publishes it via the seller wizard.
    publish_hint: str
    # Non-fatal degradations Atlas should relay verbatim (empty description →
    # blank org-chart capabilities, sanitizer-dropped config keys, unresolved
    # CEO default). Registration still succeeded when these are present.
    warnings: list[str] = []


def _slugify(name: str) -> str:
    """Slug for a NEWLY built agent's deploy — capped, with a non-empty fallback.

    Deliberately NOT shared with the two other slugifiers in this codebase;
    they answer different questions and merging them would need a cap flag and
    a fallback flag to keep all three callers correct:

    - ``core.services.company_identity.slugify`` builds an *email local part*,
      so it must NFKD-decompose and drop non-ASCII (``é`` is not legal there).
      This one leaves that to the ``[^a-z0-9]+`` collapse.
    - ``core.services.agent_export._slugify`` is a frozen compatibility shim
      that reproduces a historical derivation so legacy marketplace listings
      still resolve to a live workspace. It must not gain a cap or a fallback,
      because changing its output changes which listings resolve.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64] or "agent"


async def _require_hook_token(owner_id: str, request: Request) -> dict:
    """Authorize via the owner's per-container ``hook_token`` (timing-safe).

    Returns the ``containers`` row so the caller can reuse it (avoids a second
    DDB read for ``gateway_token``). Timing-safe token compare.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = auth[len("Bearer ") :]
    container = await container_repo.get_by_owner_id(owner_id)
    expected = (container or {}).get("hook_token") or ""
    # Compare bytes, not str: hmac.compare_digest raises TypeError on non-ASCII
    # str, turning a garbage header into a 500 at the trust boundary.
    if not expected or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid hook token")
    return container


@router.post("/u/{owner_id}/register-built-agent", response_model=RegisterBuiltAgentResponse)
async def register_built_agent(owner_id: str, payload: RegisterBuiltAgentRequest, request: Request):
    """Register an Atlas-built agent into the owner's container.

    Snapshot the staging workspace → mint a Paperclip agent (sharing the UUID) →
    deploy the snapshot into the container (files + openclaw.json registration).
    Returns the new ``agent_id`` so Atlas can report it and the owner can publish it.

    Serialized against every other agent install for this owner by the SAME
    owner-wide deploy claim the marketplace hire takes. This path calls
    ``deploy_from_artifact`` (and through it ``pair_with_openclaw``) against the
    one running container, so an Atlas registration racing a marketplace Hire is
    the 2026-08-06 incident with a different second caller. The claim is taken
    before the Paperclip mint, so a refusal costs nothing to undo.
    """
    auth_container = await _require_hook_token(owner_id, request)
    return await register_staged_agent_under_claim(
        owner_id=owner_id,
        payload=payload,
        auth_container=auth_container,
        claim_label=f"builder:{payload.staging_agent_id}",
    )


async def register_staged_agent_under_claim(
    *,
    owner_id: str,
    payload: RegisterBuiltAgentRequest,
    auth_container: dict,
    claim_label: str,
    package: CatalogPackage | None = None,
) -> dict:
    """Owner-wide deploy claim around ``_register_built_agent``.

    Shared by the Atlas bridge (hook-token) and the OpenClaw backup import
    (Clerk; routers/marketplace_import.py): both call deploy_from_artifact (→
    pair_with_openclaw) against the one running container, so they serialize on
    the SAME claim a marketplace Hire takes. ``package`` lets a caller that
    already built the CatalogPackage skip the staging export.
    """
    # ``listing_id`` is a diagnostics label on this claim, not a key (the claim
    # is owner-wide) — tag it so a held claim reads as which caller owns it.
    claim_token, held = await container_repo.try_claim_owner_deploy(owner_id, listing_id=claim_label)
    if not claim_token:
        # Same split as the marketplace route: a refusal by THIS build is
        # "already running" because the first attempt still owns the container
        # mutation. A refusal by a different install is "retry after a beat";
        # once this claim clears, the stable Paperclip key safely resumes this
        # staging operation instead of minting a second roster row.
        raise HTTPException(
            status_code=409,
            detail=(
                {"error": "deploy_busy", "retry_after_s": 20}
                if held is not None and held != claim_label
                else {"error": "deploy_in_progress"}
            ),
        )
    # A client-side TimeoutError HOLDS the claim rather than releasing it — the
    # container may still be writing, and the release is what would let the next
    # install start on top of it (see release_owner_deploy_claim).
    # ``_register_built_agent`` wraps the failure in a 502, so the timeout rides
    # down the ``__cause__`` chain the release walks.
    deploy_cause: BaseException | None = None
    try:
        return await _register_built_agent(
            owner_id=owner_id,
            payload=payload,
            auth_container=auth_container,
            package=package,
        )
    except Exception as exc:
        deploy_cause = exc
        raise
    finally:
        await container_repo.release_owner_deploy_claim(
            owner_id, listing_id=claim_label, token=claim_token, cause=deploy_cause
        )


async def _register_built_agent(
    *,
    owner_id: str,
    payload: RegisterBuiltAgentRequest,
    auth_container: dict,
    package: CatalogPackage | None = None,
) -> dict:
    """The registration itself, run under the caller's deploy claim."""
    # 1. Snapshot the staging workspace Atlas built. The built agent is
    #    self-describing — its slice comes from the staging dir's OWN
    #    openclaw.json (under workspace/), not the owner's root config (which
    #    doesn't list the not-yet-registered staging agent). EFS is keyed by
    #    owner_id. Credential scrub gate runs inside the export. Skipped when
    #    the caller already built the package (e.g. the OpenClaw backup import).
    if package is None:
        try:
            package = await run_in_thread(
                agent_export.export_built_agent_from_staging,
                owner_id=owner_id,
                staging_agent_id=payload.staging_agent_id,
                # Fix round 1, item 1: auth_container is already resolved
                # (the hook-token dependency above) -- thread it through so an
                # AgentCore owner's staging build reads its Hermes S3 profile
                # instead of assuming EFS.
                container=auth_container,
            )
        except agent_export.InvalidAgentIdError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except agent_export.AgentNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except agent_export.BuiltAgentConfigError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except agent_export.ExportTooLargeError as e:
            raise HTTPException(status_code=413, detail=str(e))
        except agent_export.CredentialLeakError as e:
            raise HTTPException(status_code=400, detail=str(e))

    slice_ = package.openclaw_slice or {}
    warnings: list[str] = []
    # Carry the built agent's description (from its entry's ``description``
    # field, surfaced in the manifest) into the Paperclip mint so capabilities
    # aren't empty. Warn instead of silently minting a blank org-chart card —
    # the founders' first built agent shipped with null capabilities and
    # nobody knew until a roster audit.
    if not (package.manifest.get("description") or "").strip():
        warnings.append(
            "built agent has no description — its org-chart capabilities will be blank; "
            "add a `description` to its agents.list entry (or a vibe in IDENTITY.md) and rebuild, "
            "or set capabilities later in the Teams UI"
        )
    meta = derive_from_listing(
        name=payload.name,
        description_md=package.manifest.get("description") or None,
    )

    # 2. Check the workspace is provisioned BEFORE minting Paperclip: a not-ready workspace
    #    returns 503 without minting, otherwise every retry would create a Paperclip agent and
    #    immediately roll it back until provisioning settles.
    if not auth_container.get("agentcore_runtime_arn"):
        raise HTTPException(status_code=503, detail="workspace still provisioning — retry in a moment")

    # Default org placement: report to the owner's apex CEO (Friday) so built
    # agents never land unparented. Resolved only once the container is ready
    # (after the 503 gates above) so start-up retry polls don't each cost a
    # Paperclip fetch. Best-effort — an unreachable Paperclip degrades to the
    # old unparented behavior with a warning, never a failure.
    reports_to = payload.reports_to
    if not reports_to:
        reports_to = await agent_export.resolve_apex_ceo_agent_id(owner_id=owner_id)
        if not reports_to:
            warnings.append(
                "could not resolve the company CEO — the agent is unparented in the org chart; "
                "set its manager in the Teams UI"
            )

    # 3. Mint the Paperclip agent (shares the UUID threaded into deploy as
    #    new_agent_id). Wrap the mint so a failure (or a malformed response
    #    missing "id") 503s cleanly instead of a naked 500 — and so new_agent_id
    #    is read only on success, keeping the deploy rollback envelope coherent.
    #    Mirrors marketplace_listings.deploy_listing.
    try:
        paperclip_agent = await create_paperclip_for_marketplace(
            buyer_user_id=owner_id,
            owner_id=owner_id,
            name=payload.name,
            role=None,
            meta=meta,
            reports_to=reports_to,
            idempotency_key=build_agent_create_idempotency_key(
                operation="agent-builder",
                owner_id=owner_id,
                # The staging workspace is the durable build identity. A retry
                # with edited display metadata must resume this build, not mint
                # a second roster row.
                intent={"stagingAgentId": payload.staging_agent_id},
            ),
        )
        new_agent_id = paperclip_agent["id"]
    except Exception as exc:  # noqa: BLE001 — mint failure / malformed response
        logger.exception(
            "register_built_agent: Paperclip mint failed for owner=%s staging=%s",
            owner_id,
            payload.staging_agent_id,
        )
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail="paperclip unavailable — retry the build in a moment") from exc

    # 4. Deploy the snapshot into the container (write files + register live in
    #    openclaw.json). A response can be lost after the first attempt commits;
    #    the stable Paperclip key then returns the same UUID. Reconcile that
    #    existing install instead of appending its cron jobs a second time.
    catalog = get_catalog_service()
    try:
        deploy_result = await catalog.deploy_from_artifact(
            owner_id=owner_id,
            slug=_slugify(payload.name),
            manifest=package.manifest,
            slice_=slice_,
            tar_bytes=package.tarball_bytes,
            new_agent_id=new_agent_id,
            container=auth_container,
        )
    except Exception as exc:  # noqa: BLE001 — roll back the orphan, re-raise
        logger.exception(
            "register_built_agent: deploy failed for owner=%s staging=%s — rolling back %s",
            owner_id,
            payload.staging_agent_id,
            new_agent_id,
        )
        # The orphan to remove is the Paperclip row: any S3 profile objects a failed install
        # wrote are overwritten by the next attempt, and no install record is keyed on them
        # (see catalog_service's module docstring).
        #
        # EXCEPT on a timeout, where the row is what makes the half-install disposable. A
        # TimeoutError means "no answer yet", not "failed": the profile write may still be
        # landing, and the Paperclip agent may already exist and work. Deleting only the row then
        # leaves a live profile with no row for the owner to see or delete it by, and nothing
        # that would ever clean it up -- keeping it makes that an ordinary agent in Teams.
        if isinstance(exc, TimeoutError):
            logger.warning(
                "register_built_agent: timed out for owner=%s staging=%s — KEEPING paperclip agent %s so the "
                "half-install stays ownable",
                owner_id,
                payload.staging_agent_id,
                new_agent_id,
            )
        else:
            try:
                await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=new_agent_id)
            except Exception:
                logger.exception("register_built_agent: rollback failed for agent %s", new_agent_id)
        if isinstance(exc, HTTPException):
            raise
        # Carry the failure class + message in the detail. The caller is the
        # owner's OWN container (hook-token auth), Atlas's protocol is to
        # report exact errors verbatim, and a bare "deploy failed" turns every
        # incident into a log-diving session — the founders' first live
        # registration retried a blind 502 for hours. Capped, no traceback;
        # exception messages on this path are gateway RPC / tar / config-merge
        # errors, not secrets.
        reason = f"{type(exc).__name__}: {exc}"[:300]
        raise HTTPException(
            status_code=502,
            detail=f"deploy into the workspace failed ({reason}) — retry the build in a moment",
        ) from exc

    # Committed: the deploy envelope above survived. Emitting from inside
    # deploy_from_artifact counted builds the rollback had undone.
    emit_agent_deployed(owner_id=owner_id, slug=_slugify(payload.name), agent_id=new_agent_id)

    # Surface sanitizer-dropped keys so Atlas learns its bundle emitted
    # non-schema config instead of the loss dying in a CloudWatch warn.
    dropped = deploy_result.get("agent_entry_keys_dropped") or []
    if dropped:
        warnings.append(
            f"non-schema agent-entry keys were dropped at deploy: {', '.join(sorted(dropped))} — "
            "remove them from the generated openclaw.json"
        )

    return {
        "agent_id": new_agent_id,
        "name": payload.name,
        "registered": True,
        # The owner publishes this via the seller wizard; surface the agent_id so
        # Atlas can tell the operator exactly which agent to publish.
        "publish_hint": "Open the marketplace publish wizard and pick this agent to list it.",
        "warnings": warnings,
    }
