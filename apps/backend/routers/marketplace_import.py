"""Import an existing OpenClaw setup (an ``openclaw backup create`` archive, a
plain tar of ``~/.openclaw``, or a bare agent workspace) into the owner's roster.

  POST /api/v1/marketplace/import/presign {filename, size_bytes}
       → presigned S3 PUT for imports/{owner}/{uuid}.tar.gz (browser uploads
         straight to the artifacts bucket; API Gateway caps bodies at 10 MB).
  POST /api/v1/marketplace/import {key}
       → stream the object through the two-pass extractor into EFS
         workspace-{import_id}/ (core/services/openclaw_backup_import.py),
         dry-run staging export, delete the raw object → ImportPreviewResponse.
  POST /api/v1/marketplace/import/{import_id}/confirm ImportConfirmRequest
       → Ultra gate (same predicate as the company-deploy branch) → for each
         selected agent: restage → staging export → register_staged_agent_under_claim
         (the Atlas mint → deploy_from_artifact → pair → rollback envelope) →
         plugins (Task 7) → history hand-off (Task 8) → one container bounce if
         either ran → staging removed only when every agent succeeded.
  DELETE /api/v1/marketplace/import/{import_id}

Clerk-authed (owner = resolve_owner_id), unlike the hook-token Atlas bridge.
Staging dirs follow the Atlas convention (``workspace-<id>/`` under the owner's
EFS root) because ``export_built_agent_from_staging`` resolves them that way;
``raw/`` and the sidecars sit at the staging root so the tarball never carries
them. Abandoned staging dirs older than 24h are swept on the next import.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Annotated

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Response

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.containers import get_ecs_manager, get_workspace
from core.dynamodb import run_in_thread
from core.openclaw_version import deployed_upstream_version
from core.repositories import billing_repo, container_repo
from core.services import agent_export
from core.services import openclaw_backup_import as backup_import
from core.services.company_package import COMPANY_STANDARD_PLAN_MAX_AGENTS
from core.services.openclaw_backup_history import stage_history
from core.services.openclaw_backup_plugins import apply_plugins
from core.services.paperclip_openclaw_pairing import count_paperclip_agents_for_owner
from routers.agent_builder import RegisterBuiltAgentRequest, register_staged_agent_under_claim
from schemas import marketplace as schemas

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/marketplace/import", tags=["marketplace"])

PRESIGN_TTL_S = 900
STALE_STAGING_S = 24 * 3600
_KEY_RE = re.compile(r"^imports/([A-Za-z0-9_-]+)/([0-9a-f]{32})\.tar\.gz$")
_USER_ERRORS = (
    backup_import.BackupImportError,
    agent_export.BuiltAgentConfigError,
    agent_export.CredentialLeakError,
    agent_export.InvalidAgentIdError,
    agent_export.AgentNotFoundError,
)


def _s3():
    return boto3.client("s3")


def _staging_dir(owner_id: str, import_id: str) -> Path:
    return get_workspace().user_path(owner_id) / f"workspace-{import_id}"


def _check_import_id(import_id: str) -> None:
    if not backup_import.IMPORT_ID_RE.match(import_id):
        raise HTTPException(status_code=400, detail="invalid import id")


async def _require_container(owner_id: str) -> dict:
    row = await container_repo.get_by_owner_id(owner_id)
    if not row:
        raise HTTPException(status_code=404, detail="no workspace container yet — finish setup, then import")
    return row


def _sweep_stale(owner_id: str) -> None:
    """Drop abandoned workspace-import-* dirs older than STALE_STAGING_S."""
    root = get_workspace().user_path(owner_id)
    if not root.is_dir():
        return
    cutoff = time.time() - STALE_STAGING_S
    for d in root.glob("workspace-import-*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def _selection(payload: schemas.ImportConfirmRequest) -> backup_import.ImportSelection:
    return backup_import.ImportSelection(
        skills=set(payload.skills) if payload.skills is not None else None,
        mcp_servers=set(payload.mcp_servers) if payload.mcp_servers is not None else None,
        include_memory=payload.include_memory,
        include_cron=payload.include_cron,
    )


def _export(owner_id: str, import_id: str, normalized: backup_import.ImportNormalized):
    package = agent_export.export_built_agent_from_staging(
        owner_id=owner_id,
        staging_agent_id=import_id,
        selected_mcps=normalized.mcp_names,
        selected_skills=normalized.skill_names,
    )
    package.openclaw_slice["cron_jobs"] = normalized.cron_jobs
    return package


def _staging_id_for(import_id: str, agent_id: str) -> str:
    """Per-agent staging id → distinct Paperclip idempotency intent per agent."""
    return f"{import_id}-{re.sub(r'[^A-Za-z0-9._-]', '-', agent_id)[:40]}"


async def _ultra_facts(owner_id: str) -> tuple[int, str | None]:
    existing = await count_paperclip_agents_for_owner(owner_id)
    account = await billing_repo.get_by_owner_id(owner_id)
    return existing, (account or {}).get("plan")


@router.post("/presign", response_model=schemas.ImportPresignResponse)
async def presign_import(
    payload: schemas.ImportPresignRequest, auth: Annotated[AuthContext, Depends(get_current_user)]
):
    owner_id = resolve_owner_id(auth)
    await _require_container(owner_id)
    if payload.size_bytes > backup_import.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"archive is over the {backup_import.MAX_UPLOAD_BYTES // 1024**3} GB limit"
        )
    key = f"imports/{owner_id}/{uuid.uuid4().hex}.tar.gz"
    url = await run_in_thread(
        _s3().generate_presigned_url,
        "put_object",
        Params={"Bucket": settings.MARKETPLACE_ARTIFACTS_BUCKET, "Key": key, "ContentType": "application/gzip"},
        ExpiresIn=PRESIGN_TTL_S,
    )
    return {"upload_url": url, "key": key, "max_bytes": backup_import.MAX_UPLOAD_BYTES, "expires_in": PRESIGN_TTL_S}


def _stage_from_s3(owner_id: str, import_id: str, key: str, etag: str | None):
    s3 = _s3()
    bucket = settings.MARKETPLACE_ARTIFACTS_BUCKET
    staging_dir = _staging_dir(owner_id, import_id)

    # extract_archive calls open_stream() TWICE (two independent GETs) — pin
    # both to the object head_object saw so a re-upload landing between pass
    # 1 and pass 2 fails loud (412) instead of silently splicing two
    # different archives together.
    def _get():
        kwargs = {"IfMatch": etag} if etag else {}
        return s3.get_object(Bucket=bucket, Key=key, **kwargs)["Body"]

    normalized, report = backup_import.stage_upload(_get, staging_dir, fleet_version=deployed_upstream_version())
    package = _export(owner_id, import_id, normalized)
    try:
        s3.delete_object(Bucket=bucket, Key=key)
    except ClientError:
        logger.warning("import %s: could not delete raw upload %s (lifecycle will)", import_id, key)
    return normalized, report, package


@router.post("", response_model=schemas.ImportPreviewResponse)
async def start_import(payload: schemas.ImportStartRequest, auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    await _require_container(owner_id)
    m = _KEY_RE.match(payload.key)
    if not m or m.group(1) != owner_id:
        raise HTTPException(status_code=400, detail="invalid upload key")
    try:
        head = await run_in_thread(_s3().head_object, Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET, Key=payload.key)
    except ClientError:
        raise HTTPException(status_code=404, detail="upload not found — it may have expired; upload again")
    if int(head.get("ContentLength") or 0) > backup_import.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="archive is over the upload limit")
    etag = head.get("ETag")

    workspace = get_workspace()
    workspace.ensure_user_dir(owner_id)
    await run_in_thread(_sweep_stale, owner_id)
    import_id = f"import-{uuid.uuid4().hex[:12]}"
    staging_dir = _staging_dir(owner_id, import_id)
    try:
        normalized, report, package = await run_in_thread(_stage_from_s3, owner_id, import_id, payload.key, etag)
    except agent_export.ExportTooLargeError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except _USER_ERRORS as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ClientError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "412"):
            raise HTTPException(status_code=409, detail="upload changed while importing — upload it again") from exc
        raise
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    if report.skipped_links:
        normalized.warnings.append(
            f"{report.skipped_links} symlinked file(s) in the archive were skipped — symlinks cannot be "
            "imported; copy the real files into the workspace and re-export if you need them"
        )
    existing, plan = await _ultra_facts(owner_id)
    return {
        "import_id": import_id,
        "kind": normalized.kind,
        "agent_id": normalized.agent_id,
        "name": normalized.name,
        "description": package.manifest.get("description") or "",
        "agents": [
            {
                "id": c.id,
                "name": c.name,
                "has_history": c.history is not None,
                "history_bytes": c.history.bytes if c.history else 0,
            }
            for c in normalized.agents
        ],
        "plugins": [
            {"id": p.id, "spec": f"{p.package_name}@{p.version}", "has_dependencies": p.has_dependencies}
            for p in normalized.plugins
        ],
        "file_count": len(package.tarball_contents),
        "files": package.tarball_contents[:200],
        "mcp_servers": normalized.mcp_names,
        "skills": normalized.skill_names,
        "cron_jobs": len(normalized.cron_jobs),
        "safety_flags": package.manifest.get("safety_flags") or [],
        "warnings": normalized.warnings,
        "skipped_big_files": report.skipped_big,
        "existing_agents": existing,
        "standard_max_agents": COMPANY_STANDARD_PLAN_MAX_AGENTS,
        "plan": plan,
    }


@router.post("/{import_id}/confirm", response_model=schemas.ImportConfirmResponse)
async def confirm_import(
    import_id: str, payload: schemas.ImportConfirmRequest, auth: Annotated[AuthContext, Depends(get_current_user)]
):
    owner_id = resolve_owner_id(auth)
    _check_import_id(import_id)
    container = await _require_container(owner_id)
    staging_dir = _staging_dir(owner_id, import_id)
    if not (staging_dir / "raw").is_dir():
        raise HTTPException(status_code=404, detail="import not found — it may already be applied or discarded")

    try:
        source = await run_in_thread(backup_import.locate_source, staging_dir / "raw")
    except backup_import.BackupImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    candidates = backup_import.list_agents(source)
    wanted = payload.agent_ids if payload.agent_ids is not None else [c.id for c in candidates]
    known = {c.id for c in candidates}
    unknown = [a for a in wanted if a not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"agents not in this backup: {', '.join(unknown)}")
    if not wanted:
        raise HTTPException(status_code=400, detail="select at least one agent")

    # Ultra gate — same predicate as the company-deploy branch: the SUM of the
    # owner's live agents and what this import adds must fit the standard plan.
    existing, plan = await _ultra_facts(owner_id)
    if plan != "ultra_pro" and existing + len(wanted) > COMPANY_STANDARD_PLAN_MAX_AGENTS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ultra_required",
                "existing_agents": existing,
                "importing": len(wanted),
                "max": COMPANY_STANDARD_PLAN_MAX_AGENTS,
            },
        )

    selection = _selection(payload)
    fleet = deployed_upstream_version()
    results: list[dict] = []
    registered: list[tuple[str, str]] = []  # (source_agent_id, new_agent_id)
    for source_agent_id in wanted:
        try:
            normalized = await run_in_thread(
                backup_import.restage, staging_dir, agent_id=source_agent_id, selection=selection, fleet_version=fleet
            )
            package = await run_in_thread(_export, owner_id, import_id, normalized)
            result = await register_staged_agent_under_claim(
                owner_id=owner_id,
                payload=RegisterBuiltAgentRequest(
                    staging_agent_id=_staging_id_for(import_id, source_agent_id),
                    name=normalized.name,
                    reports_to=payload.reports_to,
                ),
                auth_container=container,
                claim_label=f"import:{import_id}",
                package=package,
            )
            registered.append((source_agent_id, result["agent_id"]))
            results.append(
                {
                    "source_agent_id": source_agent_id,
                    "agent_id": result["agent_id"],
                    "name": result["name"],
                    "ok": True,
                    "warnings": [*normalized.warnings, *(result.get("warnings") or [])],
                }
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            results.append(
                {
                    "source_agent_id": source_agent_id,
                    "agent_id": None,
                    "name": source_agent_id,
                    "ok": False,
                    "error": detail,
                }
            )
        except agent_export.ExportTooLargeError as exc:
            results.append(
                {
                    "source_agent_id": source_agent_id,
                    "agent_id": None,
                    "name": source_agent_id,
                    "ok": False,
                    "error": str(exc),
                }
            )
        except _USER_ERRORS as exc:
            results.append(
                {
                    "source_agent_id": source_agent_id,
                    "agent_id": None,
                    "name": source_agent_id,
                    "ok": False,
                    "error": str(exc),
                }
            )

    warnings: list[str] = []
    plugins_installed: list[str] = []
    history_staged: list[str] = []
    restarting = False

    if registered and payload.plugins != []:
        plugin_res = await apply_plugins(
            owner_id=owner_id,
            plugins=backup_import.list_plugins(source),
            chosen=set(payload.plugins) if payload.plugins is not None else None,
        )
        plugins_installed = plugin_res.installed
        warnings.extend(plugin_res.warnings)
        if plugins_installed:
            restarting = True

    if payload.include_history and registered:
        by_source = {c.id: c for c in candidates}
        for source_agent_id, new_agent_id in registered:
            hist = by_source[source_agent_id].history
            if hist is None:
                continue
            warnings.extend(
                await run_in_thread(
                    stage_history,
                    owner_id=owner_id,
                    source_agent_id=source_agent_id,
                    new_agent_id=new_agent_id,
                    history=hist,
                )
            )
            history_staged.append(new_agent_id)
        if history_staged:
            restarting = True

    if restarting:
        try:
            await get_ecs_manager().force_redeploy_user_service(owner_id)
        except Exception:
            # The bounce is a convenience, not a correctness requirement —
            # plugins/history are already staged on EFS and the container
            # picks them up on its NEXT restart regardless. Failing the whole
            # confirm here would make the caller think nothing landed.
            logger.exception("import %s: could not start a workspace restart", import_id)
            restarting = False
            warnings.append(
                "workspace restart could not be started — imported plugins/history will apply on its next restart"
            )
    if all(r["ok"] for r in results):
        shutil.rmtree(staging_dir, ignore_errors=True)
    return {
        "agents": results,
        "plugins_installed": plugins_installed,
        "history_staged": history_staged,
        "restarting": restarting,
        "warnings": warnings,
    }


@router.delete("/{import_id}", status_code=204)
async def discard_import(import_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    _check_import_id(import_id)
    shutil.rmtree(_staging_dir(owner_id, import_id), ignore_errors=True)
    return Response(status_code=204)
