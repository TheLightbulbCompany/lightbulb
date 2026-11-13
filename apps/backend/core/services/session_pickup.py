"""Session pick-up: a teammate continues another's Claude Code session on their own Mac.

The author allows it per session. Their desktop uploads the raw transcript (append-only parts),
a git bundle and sidecars straight to S3 through presigned URLs, then commits a manifest; a
receiver gets a ticket of presigned GETs. Spec: docs/superpowers/specs/2026-09-27-session-pickup-design.md.

Rows, all in the messaging table:
  LOCALSESSION#<owner>#<sid>        / PICKUP                  the committed manifest
  LOCALSESSION#<owner>#<sid>        / PICKUPREQ#<pickupId>    who asked for that ticket
  LOCALSESSION#<owner>#<future sid> / LINEAGE                 stamped on its first registration
  LOCALUSER#<owner>#<human>         / PICKUPWANT#<device>#<sid>  what the author's Mac snapshots

Objects live under ``session-pickup/<owner>/``, never under ``<owner>/``: each workspace's agent
runtime role reads ``<owner>/*``, and a teammate's raw transcript must stay out of an agent's shell.
Below the session sits ``<epoch>/<series>/``: the epoch is minted each time pick-up is turned on, so
retiring one permission deletes only its own objects, never those of a later one.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import Field

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions
from core.services.local_sessions import Digest, PICKUP_RETENTION_SECONDS
from core.services.messaging import ApiProblem, ContractModel, validate_uuid

logger = logging.getLogger(__name__)

PREFIX = "session-pickup"
# Redacted copies for a link's holder live under an id-free prefix, so a presigned URL names no owner
# or session. The sweeper deletes objects older than COPY_TTL_S; retire deletes a session's own.
COPIES_PREFIX = "session-copies"
COPY_TTL_S = 3600
_ANALYTICS_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:session-pickup")
URL_SECONDS = 15 * 60
MIB = 1024 * 1024
PART_CAPS = {"transcript": 64 * MIB, "code": 200 * MIB, "sidecars": 200 * MIB}
SWEEP_INTERVAL_S = 3600
SWEEP_TICK_S = 300
# An upload nobody committed this long after it landed is abandoned: the Mac commits seconds after
# uploading, within one UPLOAD_URL_SECONDS link. The sweeper deletes it even while pick-up stays on,
# so a Mac that asks for links and never commits cannot grow the bucket past this age.
# ponytail: a commit arriving more than this long after its own upload can race the sweeper and
# lose its objects; fence commit on the objects' age if a Mac ever commits that late.
ABANDONED_UPLOAD_S = 3600

GitSha = Annotated[str, Field(pattern=r"^[a-f0-9]{40}([a-f0-9]{24})?$")]


# --- Wire models -------------------------------------------------------------------------------


class ManifestPart(ContractModel):
    index: int = Field(ge=0, le=999_999)
    bytes: int = Field(ge=1, le=PART_CAPS["transcript"])
    sha256: Digest
    from_: int = Field(alias="from", ge=0)
    to: int = Field(ge=1)


class ManifestCode(ContractModel):
    bytes: int = Field(ge=1, le=PART_CAPS["code"])
    sha256: Digest
    remote: Annotated[str, Field(min_length=1, max_length=256)]
    branch: Annotated[str, Field(max_length=256)] | None = None
    head: GitSha
    snapshot: GitSha


class ManifestSidecars(ContractModel):
    bytes: int = Field(ge=1, le=PART_CAPS["sidecars"])
    sha256: Digest


class Manifest(ContractModel):
    series: str
    parts: list[ManifestPart]
    code: ManifestCode | None
    sidecars: ManifestSidecars | None
    claude_version: str
    conversation_id: str
    # Null when the session's folder has no name the Mac could read.
    cwd_name: str | None
    cwd_relative: str
    snapshot_at: str
    ended: bool


class Download(ContractModel):
    url: str
    bytes: int
    sha256: str


# --- Storage -----------------------------------------------------------------------------------


def _s3():
    # Per call, like browser_control: a cached client would outlive a test's (or a rotation's) scope.
    return boto3.client("s3", region_name=settings.AWS_REGION, config=Config(signature_version="s3v4"))


def _bucket() -> str:
    if not settings.OWNER_STATE_BUCKET:
        raise RuntimeError("OWNER_STATE_BUCKET is not configured")
    return settings.OWNER_STATE_BUCKET


def session_prefix(owner: str, sid: str) -> str:
    return f"{PREFIX}/{owner}/{sid}/"


def series_prefix(row: dict, series: str) -> str:
    return f"{session_prefix(row['ownerId'], row['sessionId'])}{row['pickupEpoch']}/{series}/"


def _key(row: dict, series: str, kind: str, *, index: int | None = None, sha: str = "") -> str:
    name = {
        "transcript": f"t-{index or 0:06d}{'-' + sha if sha else ''}.gz",
        "code": f"code-{sha[:16]}.bundle",
        "sidecars": f"side-{sha[:16]}.tgz",
    }
    return f"{series_prefix(row, series)}{name[kind]}"


def _part_key(row: dict, series: str, part: dict) -> str:
    # Existing committed parts keep their index-only names. A small stored-only
    # marker lets new digest-addressed parts append without rewriting snapshots.
    return _key(row, series, "transcript", index=part["index"], sha=part["sha256"] if part.get("hashed") else "")


def delete_prefix(prefix: str, *, keep: frozenset[str] = frozenset(), before: float | None = None) -> None:
    """Delete every object under ``prefix`` except ``keep``; with ``before``, only those that landed
    earlier. Sync boto3 -- call via ``run_in_thread``."""
    if not settings.OWNER_STATE_BUCKET:
        return
    client, bucket = _s3(), settings.OWNER_STATE_BUCKET
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys = [
            {"Key": obj["Key"]}
            for obj in page.get("Contents", [])
            if obj["Key"] not in keep and (before is None or obj["LastModified"].timestamp() < before)
        ]
        if keys and client.delete_objects(Bucket=bucket, Delete={"Objects": keys}).get("Errors"):
            raise RuntimeError("Session pick-up deletion was incomplete")


def _want(row: dict) -> dict:
    return {
        "pk": repo.user_pk(row["ownerId"], row["humanUserId"]),
        "sk": f"PICKUPWANT#{row['deviceId']}#{row['sessionId']}",
    }


def _want_put(row: dict) -> dict:
    item = {
        **_want(row),
        "sessionId": row["sessionId"],
        "provider": row["provider"],
        "conversationId": row["nativeConversationId"],
        "allowedAt": int(time.time()),
    }
    return {"Put": {"TableName": store._table().name, "Item": item}}


def _want_delete(row: dict) -> dict:
    return {"Delete": {"TableName": store._table().name, "Key": _want(row)}}


def _audit(owner: str, sid: str, auth, action: str, **kwargs) -> dict:
    return {"Put": {"TableName": store._table().name, "Item": sessions.audit_row(owner, sid, auth, action, **kwargs)}}


def _expired(row: dict, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    stopped = sessions.pickup_stopped_at(row, now)
    return stopped is not None and now >= stopped + PICKUP_RETENTION_SECONDS


# --- The author's switch ------------------------------------------------------------------------


async def set_pickup(
    auth: AuthContext, session_id: str, enabled: bool, *, expect_request: tuple[str, str] | None = None
) -> sessions.LocalSession:
    """Only the session's own human flips it; unlike teammate control, also after it ended.

    ``expect_request`` is ``(user_id, request_id)`` when Allow answers a pick-up request: the
    request must still be pending on the row this conditional write replaces, so a Decline that
    lands first cannot be overridden."""
    from core.services.local_session_relay import publish_resources_changed

    owner = resolve_owner_id(auth)
    if not enabled:
        return await _turn_off(auth, owner, session_id)
    for _ in range(6):
        row, enrollment = await sessions.authorize_session(auth, None, session_id)
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can allow pick-up.")
        if expect_request is not None:
            asked = (row.get("pickupRequests") or {}).get(expect_request[0]) or {}
            if asked.get("state") != "pending" or asked.get("requestId") != expect_request[1]:
                raise ApiProblem("LOCAL_SESSION_CONFLICT", "That request was already answered.")
        if row["provider"] != "claude":
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Only Claude Code sessions can be picked up.")
        if row.get("pickupAllowed"):
            return sessions._session_public(row, auth.user_id)
        if row.get("unshared"):
            # Every device route refuses a paused session, so its Mac would retry forever.
            raise ApiProblem(
                "LOCAL_SESSION_CONFLICT", "Share this session with your workspace before allowing pick-up."
            )
        sid = row["sessionId"]
        updated = {
            **row,
            "pickupAllowed": True,
            "pickupSnapshot": {"state": "preparing"},
            "pickupEpoch": uuid.uuid4().hex,
            "revision": str(uuid.uuid4()),
            # Everyone may pick it up now, so every pending ask is answered (spec 2026-10-05-ask-to-pick-up).
            "pickupRequests": {
                u: r for u, r in (row.get("pickupRequests") or {}).items() if r.get("state") != "pending"
            },
        }
        extra = [_audit(owner, sid, auth, "pickup_on"), _want_put(row)]
        try:
            await repo.put_session(updated, row, enrollment, extra=extra)
        except ClientError as exc:
            if not store._is_cancelled(exc):
                raise
            continue
        await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
        if sessions.pending_pickups(row):
            await sessions._push_presence(updated)  # a pending ask froze the owner's Mac; lift it now
        return sessions._session_public(updated, auth.user_id)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def _turn_off(auth: AuthContext, owner: str, session_id: str) -> sessions.LocalSession:
    """Off depends on identity alone: the author must be able to delete their transcript while
    their Mac is unpaired, sharing is off or the session is paused -- exactly when it matters."""
    sid = validate_uuid(session_id, label="local session ID")
    for _ in range(6):
        row = await repo.get_session(owner, sid)
        if row is None or row.get("ownerId") != owner:
            raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can allow pick-up.")
        if not row.get("pickupAllowed"):
            return sessions._session_public(row, auth.user_id)
        try:
            # An upload in flight may still land; the sweeper deletes a prefix nobody allows.
            return sessions._session_public(
                await retire(owner, sid, row, auth=auth, action="pickup_off", keep_link=True), auth.user_id
            )
        except ClientError as exc:
            if not store._is_cancelled(exc):
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


# --- The receiver -------------------------------------------------------------------------------


# --- The author's Mac ---------------------------------------------------------------------------


# --- Expiry -------------------------------------------------------------------------------------


def _subprefixes(prefix: str) -> list[str]:
    client, bucket = _s3(), _bucket()
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
    return [entry["Prefix"] for page in pages for entry in page.get("CommonPrefixes", [])]


def _manifest_keys(row: dict, manifest: dict | None) -> frozenset[str]:
    if manifest is None:
        return frozenset()
    series = manifest["series"]
    keys = {_part_key(row, series, p) for p in manifest["parts"]}
    for kind in ("code", "sidecars"):
        if manifest.get(kind):
            keys.add(_key(row, series, kind, sha=manifest[kind]["sha256"]))
    return frozenset(keys)


def _session_prefixes() -> list[tuple[str, str]]:
    # ponytail: lists every session prefix hourly; S3 is the index. Add a DynamoDB index of
    # allowed sessions if the listing ever gets slow.
    client, bucket, found = _s3(), _bucket(), []
    pages = client.get_paginator("list_objects_v2")
    for page in pages.paginate(Bucket=bucket, Prefix=f"{PREFIX}/", Delimiter="/"):
        for owner in page.get("CommonPrefixes", []):
            for inner in pages.paginate(Bucket=bucket, Prefix=owner["Prefix"], Delimiter="/"):
                for entry in inner.get("CommonPrefixes", []):
                    _, owner_id, sid, _ = entry["Prefix"].split("/")
                    found.append((owner_id, sid))
    return found


async def sweep_once(*, now: int | None = None) -> int:
    """Delete the stored snapshot of every session that is gone, no longer allowed, or stopped
    (ended, or its lease lapsed) more than PICKUP_RETENTION_SECONDS ago. Idempotent: every backend task may run it."""
    now = int(time.time()) if now is None else now
    expired = 0
    try:
        await run_in_thread(lambda: delete_prefix(f"{COPIES_PREFIX}/", before=now - COPY_TTL_S))
    except Exception as exc:
        logger.warning("session copy cleanup failed: %s", type(exc).__name__)
    for owner, sid in await run_in_thread(_session_prefixes):
        row = await repo.get_session(owner, sid)
        if row is not None and row.get("pickupAllowed") and not _expired(row, now):
            # Still allowed: drop what the committed manifest does not name and nobody committed in
            # time (an abandoned series, a replaced blob, a retired epoch's late upload).
            try:
                manifest = await store._get(row["pk"], "PICKUP")
                if manifest and now < manifest.get("replacedAt", 0) + URL_SECONDS:
                    continue  # a ticket signed before the replacement may still be downloading
                keep = _manifest_keys(row, manifest)
                prefix, before = session_prefix(owner, sid), now - ABANDONED_UPLOAD_S
                await run_in_thread(lambda: delete_prefix(prefix, keep=keep, before=before))
            except Exception as exc:
                logger.warning("session pick-up cleanup failed: %s", type(exc).__name__, extra={"sid": sid})
            continue
        try:
            await retire(owner, sid, row)
        except Exception as exc:
            # The next pass retries: a moved revision or an S3 hiccup costs an hour, nothing more.
            logger.warning("session pick-up expiry failed: %s", type(exc).__name__, extra={"sid": sid})
            continue
        expired += 1
    return expired


async def retire(
    owner: str,
    sid: str,
    row: dict | None,
    *,
    auth: AuthContext | None = None,
    action: str = "pickup_expired",
    keep_link: bool = False,
) -> dict | None:
    """Delete a session's snapshot, and when it was still allowed, clear the flags, drop its want
    row and audit ``action`` in one transaction. Fenced on the META revision alone (never pairing
    or enrolment): of two concurrent callers (tasks, the sweeper, a device route, the author's
    off switch) one wins and the other is cancelled. Returns the updated META when there was one.
    ``keep_link``: the owner's "Let people continue" switch going off; the link still shows the session."""
    from core.services.local_session_relay import publish_resources_changed

    table = store._table().name
    pk = repo.session_pk(owner, sid)
    # Listed before the fence: pick-up turned on after it mints a new epoch this list cannot hold,
    # so the deletion below never touches a snapshot committed later.
    retired = await run_in_thread(_subprefixes, session_prefix(owner, sid))
    # Its outsider copies (session_links.create_copy writes one COPY# row per copy).
    retired += [
        f"{COPIES_PREFIX}/{r['copyId']}/" for r in await store._query(pk, begins_with="COPY#") if r.get("copyId")
    ]
    items = [{"Delete": {"TableName": table, "Key": {"pk": pk, "sk": "PICKUP"}}}]
    allowed = row is not None and bool(row.get("pickupAllowed"))
    updated = None
    # Not allowed (or gone) when read: fenced too, so a sweep never deletes the manifest of a
    # permission granted since.
    if row is None:
        check = {"ConditionExpression": "attribute_not_exists(pk)"}
    else:
        check = {"ConditionExpression": "revision = :old", "ExpressionAttributeValues": {":old": row["revision"]}}
    if not allowed:
        items.append({"ConditionCheck": {"TableName": table, "Key": {"pk": pk, "sk": "META"}, **check}})
    else:
        drop = ("pickupAllowed", "pickupSnapshot", "pickupEpoch") + (() if keep_link else ("shareLink",))
        updated = {key: value for key, value in row.items() if key not in drop}
        updated["revision"] = str(uuid.uuid4())
        items += [
            {
                "Update": {
                    "TableName": table,
                    "Key": {"pk": row["pk"], "sk": "META"},
                    "UpdateExpression": f"REMOVE {', '.join(drop)} SET revision = :new",
                    "ConditionExpression": "revision = :old",
                    "ExpressionAttributeValues": {":new": updated["revision"], ":old": row["revision"]},
                }
            },
            _want_delete(row),
            _audit(owner, sid, auth, action),
        ]
        if row.get("shareLink") and not keep_link:
            # The link dies with its snapshot: expiry and turning the link off are one act (spec §3.1).
            items.append(
                {
                    "Delete": {
                        "TableName": table,
                        "Key": {"pk": sessions._share_pk(row["shareLink"]["tokenHash"]), "sk": "META"},
                    }
                }
            )
    await store._transact(items)
    for prefix in retired:
        await run_in_thread(delete_prefix, prefix)
    if allowed:
        await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
    return updated
