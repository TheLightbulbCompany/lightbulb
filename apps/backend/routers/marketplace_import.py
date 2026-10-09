"""Import a Claude Code context (a tar of ``~/.claude``) into one roster agent.

  POST /api/v1/marketplace/import/presign {filename, size_bytes}
       → presigned S3 PUT for imports/{owner}/{import_id}.tar.gz (the browser
         uploads straight to the artifacts bucket; API Gateway caps bodies at 10 MB).
  POST /api/v1/marketplace/import {key}
       → detection + allowlist normalize into a scratch dir, purely to compute the
         preview (collections / skills / user memory / safety flags); the scratch dir
         is dropped and the RAW OBJECT IS KEPT as the staging → ImportPreviewResponse.
  POST /api/v1/marketplace/import/{import_id}/confirm ImportConfirmRequest
       → re-stream the same object, apply the selected collections/skills + user
         memory into ``target_agent_id``'s Hermes profile, delete the object.
         No Ultra gate — nothing here mints an agent.
  DELETE /api/v1/marketplace/import/{import_id}

**S3 is the staging.** It used to be an EFS dir (``workspace-<import-id>/`` under the
owner's root) that the preview wrote and a later request read back, plus a 24h sweep
for abandoned ones. There is no shared mount now: the backend runs 2-4 tasks, so a
scratch dir written by the task that served the preview is not there for the task that
serves the confirm. Keeping the uploaded object and re-streaming it on confirm makes
the staging something every task can see, and the bucket's own lifecycle rule replaces
the sweep. The cost is one extra GET + normalize pass per confirm.

**The OpenClaw-backup import rail is gone.** It restored an ``openclaw backup create``
archive (or a bare agent workspace) by staging it onto EFS and minting agents through
the Atlas register path. Both halves of that — the EFS staging and the openclaw.json
slice a minted agent needed — went with the ECS runtime. Claude Code import is a
separate, live feature and is what this router is now.

Clerk-authed (owner = resolve_owner_id), unlike the hook-token Atlas bridge.
"""

from __future__ import annotations

import logging
import re
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Response

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services import agent_export, claude_code_import, context_import, hermes_profiles
from schemas import marketplace as schemas

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/marketplace/import", tags=["marketplace"])

PRESIGN_TTL_S = 900
_KEY_RE = re.compile(r"^imports/([A-Za-z0-9_-]+)/(import-[0-9a-f]{12})\.tar\.gz$")

#: What the apply half needs to see of the agent's CURRENT profile: ``skills/``, so an existing
#: skill is reported ``skipped_exists`` instead of being overwritten. ``memory/imports/claude-code/`` is
#: deliberately absent — a re-import REPLACES a selected collection in place, so pulling the
#: old copy down first would only make the rmtree slower.
_APPLY_CONTEXT_PREFIXES = ("skills/",)


def _s3():
    return boto3.client("s3")


def _bucket() -> str:
    return settings.MARKETPLACE_ARTIFACTS_BUCKET


def _upload_key(owner_id: str, import_id: str) -> str:
    return f"imports/{owner_id}/{import_id}.tar.gz"


def _check_import_id(import_id: str) -> None:
    if not claude_code_import.IMPORT_ID_RE.match(import_id):
        raise HTTPException(status_code=400, detail="invalid import id")


async def _require_container(owner_id: str) -> dict:
    row = await container_repo.get_by_owner_id(owner_id)
    if not row:
        raise HTTPException(status_code=404, detail="no workspace yet — finish setup, then import")
    return row


def _delete_raw(key: str, import_id: str) -> None:
    try:
        _s3().delete_object(Bucket=_bucket(), Key=key)
    except ClientError:
        logger.warning("import %s: could not delete raw upload %s (lifecycle will)", import_id, key)


def _open_stream(key: str, etag: str | None):
    """A fresh readable over the upload, pinned to ``etag``.

    Every pass is its own GET (``r|gz`` streams cannot seek), and every one is pinned to the
    ETag ``head_object`` saw so a re-upload landing between passes fails loud (412) instead of
    silently splicing two different archives together.
    """

    def _get():
        kwargs = {"IfMatch": etag} if etag else {}
        return _s3().get_object(Bucket=_bucket(), Key=key, **kwargs)["Body"]

    return _get


def _normalize(key: str, etag: str | None, dst: Path) -> claude_code_import.ClaudeNormalized:
    """Detection pass, then the allowlist normalize into ``dst``.

    The detection pass reads NAMES only and bounds the decompressed bytes it streams through:
    ``MAX_MEMBERS`` caps the header count, but one member with a huge declared size would
    otherwise force an unbounded decompress-and-discard.
    """
    get = _open_stream(key, etag)
    names: list[str] = []
    declared = 0
    body = get()
    try:
        with tarfile.open(fileobj=body, mode="r|gz") as tf:
            for member in tf:
                names.append(member.name)
                declared += member.size
                if declared > claude_code_import.CLAUDE_MAX_TOTAL_BYTES:
                    raise claude_code_import.ClaudeImportError("archive expands past the extraction cap")
                if len(names) >= claude_code_import.MAX_MEMBERS:
                    break  # the normalizer raises on its own member cap
    except tarfile.TarError as exc:
        raise claude_code_import.ClaudeImportError("not a valid .tar.gz archive") from exc
    finally:
        # tarfile.close() does not close an externally supplied fileobj — drop the S3
        # connection here instead of holding it until GC.
        body.close()

    if claude_code_import.detect_claude_root(names) is None:
        raise claude_code_import.ClaudeImportError(
            "this does not look like a Claude Code context — from your home directory run "
            "`tar czf claude-code.tar.gz .claude/CLAUDE.md .claude/projects/*/memory .claude/skills` "
            "(whichever exist)"
        )
    return claude_code_import.extract_claude_archive(get, dst)


@router.post("/presign", response_model=schemas.ImportPresignResponse)
async def presign_import(
    payload: schemas.ImportPresignRequest, auth: Annotated[AuthContext, Depends(get_current_user)]
):
    """The import id is minted HERE and carried in the key, so the confirm can address the
    staged object without a listing or a server-side copy."""
    owner_id = resolve_owner_id(auth)
    await _require_container(owner_id)
    max_bytes = claude_code_import.MAX_UPLOAD_BYTES
    if payload.size_bytes > max_bytes:
        raise HTTPException(status_code=413, detail=f"archive is over the {max_bytes // 1024**3} GB limit")
    key = _upload_key(owner_id, f"import-{uuid.uuid4().hex[:12]}")
    url = await run_in_thread(
        _s3().generate_presigned_url,
        "put_object",
        Params={"Bucket": _bucket(), "Key": key, "ContentType": "application/gzip"},
        ExpiresIn=PRESIGN_TTL_S,
    )
    return {"upload_url": url, "key": key, "max_bytes": max_bytes, "expires_in": PRESIGN_TTL_S}


def _preview(key: str, etag: str | None) -> claude_code_import.ClaudeNormalized:
    """Normalize into a throwaway dir purely to compute the preview.

    Nothing staged here survives the request: the uploaded object is the staging, and the
    confirm re-streams it. The scratch dir exists only because the normalizer's contract is a
    filesystem one.
    """
    with tempfile.TemporaryDirectory(prefix="claude-import-") as tmp:
        return _normalize(key, etag, Path(tmp))


@router.post("", response_model=schemas.ImportPreviewResponse)
async def start_import(payload: schemas.ImportStartRequest, auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    await _require_container(owner_id)
    m = _KEY_RE.match(payload.key)
    if not m or m.group(1) != owner_id:
        raise HTTPException(status_code=400, detail="invalid upload key")
    import_id = m.group(2)
    try:
        head = await run_in_thread(_s3().head_object, Bucket=_bucket(), Key=payload.key)
    except ClientError:
        raise HTTPException(status_code=404, detail="upload not found — it may have expired; upload again")
    if int(head.get("ContentLength") or 0) > claude_code_import.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="archive is over the upload limit")
    etag = head.get("ETag")

    try:
        staged = await run_in_thread(_preview, payload.key, etag)
    except claude_code_import.ClaudeImportError as exc:
        # The archive itself is bad — no retry reuses this object, and it can hold
        # credentials, so drop it now instead of waiting on the bucket lifecycle rule. The
        # 412/ClientError path below deliberately KEEPS it: that flow tells the client to
        # re-upload and retry against the same key.
        await run_in_thread(_delete_raw, payload.key, import_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except claude_code_import.BackupImportError as exc:
        await run_in_thread(_delete_raw, payload.key, import_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "412"):
            raise HTTPException(status_code=409, detail="upload changed while importing — upload it again") from exc
        raise

    return {
        "import_id": import_id,
        "name": "Claude Code context",
        "claude": {
            "collections": [{"id": c.id, "label": c.label, "file_count": c.file_count} for c in staged.collections],
            "has_user_memory": staged.has_user_memory,
            "skills": staged.skills,
            "skipped": staged.skipped,
            # snippet deliberately dropped — never echo a flagged source line back to the client.
            "safety_flags": [
                {"pattern": f.pattern, "severity": f.severity, "file": f.file, "line": f.line}
                for f in staged.safety_flags
            ],
        },
    }


def _apply_into_profile(
    owner_id: str,
    agent_id: str,
    key: str,
    etag: str | None,
    collections: list[str] | None,
    skills: list[str] | None,
) -> list[dict]:
    """Re-stage the upload, validate the selection against it, apply, upload what changed.

    The selection is checked against the RE-NORMALIZED staging rather than a stored manifest:
    the archive is the only record of what this import contains, and the ETag pin proves it is
    the same bytes the preview described. Checked BEFORE the apply, so a typo'd id cannot half
    apply an import.

    The agent's CURRENT ``skills/`` are pulled down first so the ``skipped_exists`` rule sees
    real state rather than an empty tree. The staged ``CLAUDE.md`` goes through the context paste
    door's merge into ``memories/USER.md`` -- Hermes's own user-profile store, entry-deduped, so a
    re-import adds only what is new rather than replacing what the agent learned since. Only files the apply
    actually wrote or changed are uploaded, and ``write_profile_subtree`` bumps ``.revision``
    last — which IS the delivery: the bridge re-pulls the changed objects on the agent's next
    turn, so there is no restart to trigger.
    """
    with tempfile.TemporaryDirectory(prefix="claude-import-") as tmp:
        root = Path(tmp)
        staging, profile = root / "staging", root / "profile"
        staging.mkdir()
        profile.mkdir()
        normalized = _normalize(key, etag, staging)

        known = {c.id for c in normalized.collections} | set(normalized.skills)
        unknown = [name for name in (*(collections or ()), *(skills or ())) if name not in known]
        if unknown:
            raise claude_code_import.ClaudeImportError(f"not in this import: {', '.join(unknown)}")

        hermes_profiles.fetch_profile_subtree(owner_id, agent_id, _APPLY_CONTEXT_PREFIXES, profile)
        before = _snapshot(profile)
        items = claude_code_import.apply_to_workspace(staging, profile, collections, skills)
        changed = {rel: body for rel, body in _snapshot(profile).items() if before.get(rel) != body}
        if changed:
            hermes_profiles.write_profile_subtree(owner_id, agent_id, changed, ())
        claude_md = staging / "claude" / "CLAUDE.md"
        if claude_md.is_file():
            try:
                context_import.merge_context(owner_id, agent_id, claude_md.read_text(errors="replace"))
                items.append({"item": "user-memory", "status": "applied"})
            except ValueError:
                pass  # a heading-only CLAUDE.md holds no entries: nothing to merge, nothing to report
        return items


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@router.post("/{import_id}/confirm", response_model=schemas.ImportConfirmResponse)
async def confirm_import(
    import_id: str, payload: schemas.ImportConfirmRequest, auth: Annotated[AuthContext, Depends(get_current_user)]
):
    """Apply a staged Claude import to one existing roster agent. No Ultra gate — nothing
    here mints an agent."""
    owner_id = resolve_owner_id(auth)
    _check_import_id(import_id)
    await _require_container(owner_id)
    if not payload.target_agent_id:
        raise HTTPException(status_code=400, detail="target_agent_id is required for a Claude Code import")
    if claude_code_import.IMPORT_ID_RE.match(payload.target_agent_id):
        # An import id is not a roster agent; applying "onto" one would be a misleading 200
        # that changes nothing any agent reads.
        raise HTTPException(status_code=400, detail="target_agent_id must be a roster agent, not an import id")
    try:
        agent_id = await run_in_thread(agent_export.resolve_workspace_agent_id, owner_id, payload.target_agent_id)
    except agent_export.InvalidAgentIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    key = _upload_key(owner_id, import_id)
    try:
        head = await run_in_thread(_s3().head_object, Bucket=_bucket(), Key=key)
    except ClientError as exc:
        raise HTTPException(
            status_code=404, detail="import not found — it may already be applied or discarded"
        ) from exc

    try:
        items = await run_in_thread(
            _apply_into_profile,
            owner_id,
            agent_id,
            key,
            head.get("ETag"),
            payload.claude_collections,
            payload.claude_skills,
        )
    except (claude_code_import.ClaudeImportError, claude_code_import.BackupImportError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await run_in_thread(_delete_raw, key, import_id)
    # The profile write IS the delivery (``.revision`` bumped last, re-pulled on the agent's
    # next turn) — there is no container to bounce, so nothing to report but the items.
    return {"claude_items": items, "warnings": []}


@router.delete("/{import_id}", status_code=204)
async def discard_import(import_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    _check_import_id(import_id)
    await run_in_thread(_delete_raw, _upload_key(owner_id, import_id), import_id)
    return Response(status_code=204)
