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

import asyncio
import json
import logging
import time
import uuid
from typing import Annotated, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import Field, model_serializer, model_validator

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.product_analytics import capture_product_event
from core.repositories import container_repo, local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions
from core.services.local_sessions import DeviceId, Digest, OpaqueId, PICKUP_RETENTION_SECONDS, _drop_none, iso
from core.services.messaging import ApiProblem, ContractModel, validate_uuid
from core.services.session_screen import _still_a_member

logger = logging.getLogger(__name__)

PREFIX = "session-pickup"
# Redacted copies for a link's holder live under an id-free prefix, so a presigned URL names no owner
# or session. The sweeper deletes objects older than COPY_TTL_S; retire deletes a session's own.
COPIES_PREFIX = "session-copies"
COPY_TTL_S = 3600
_ANALYTICS_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:session-pickup")
URL_SECONDS = 15 * 60
# Short: an upload link signed just before the author turns pick-up off can still land an object,
# which then waits for the sweeper. The Mac uploads within seconds of asking.
UPLOAD_URL_SECONDS = 5 * 60
MIB = 1024 * 1024
PART_CAPS = {"transcript": 64 * MIB, "code": 200 * MIB, "sidecars": 200 * MIB}
SNAPSHOT_CAP = 1024 * MIB
# ponytail: the manifest is one DynamoDB item (400 KB); past this many parts the Mac is sent a 409
# and starts a new series, which re-uploads the transcript compacted into a few large parts.
MAX_PARTS = 1000
#: Acknowledgement entries, newest kept: the relay envelope is 16 KiB.
ACK_CAP = 40
SWEEP_INTERVAL_S = 3600
SWEEP_TICK_S = 300
# An upload nobody committed this long after it landed is abandoned: the Mac commits seconds after
# uploading, within one UPLOAD_URL_SECONDS link. The sweeper deletes it even while pick-up stays on,
# so a Mac that asks for links and never commits cannot grow the bucket past this age.
# ponytail: a commit arriving more than this long after its own upload can race the sweeper and
# lose its objects; fence commit on the objects' age if a Mac ever commits that late.
ABANDONED_UPLOAD_S = 3600

Series = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
GitSha = Annotated[str, Field(pattern=r"^[a-f0-9]{40}([a-f0-9]{24})?$")]


class DeviceRefused(Exception):
    """A device route's answer: a bare status, or the 409 continuity body. Never echoes input."""

    def __init__(self, status: int, body: dict | None = None):
        super().__init__(status)
        self.status, self.body = status, body


# --- Wire models -------------------------------------------------------------------------------


class PickupRequest(ContractModel):
    pickup_id: str


class PickupOutcome(ContractModel):
    ok: bool
    code: Annotated[str, Field(max_length=64, pattern=r"^[A-Z0-9_]+$")] | None = None
    branch: Annotated[str, Field(max_length=256)] | None = None
    device_id: DeviceId | None = None
    profile_fingerprint: DeviceId | None = None
    conversation_id: OpaqueId | None = None


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


class TranscriptDownload(Download):
    index: int


class Downloads(ContractModel):
    transcript: list[TranscriptDownload]
    code: Download | None
    sidecars: Download | None


class TicketSession(ContractModel):
    session_id: str
    human_user_id: str
    title: str | None = None
    provider: Literal["claude"]

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("title",))


class PickupTicket(ContractModel):
    pickup_id: str
    session: TicketSession
    manifest: Manifest
    downloads: Downloads
    expires_at: str


class UploadPart(ContractModel):
    kind: Literal["transcript", "code", "sidecars"]
    index: int | None = Field(default=None, ge=0, le=999_999)
    bytes: int = Field(ge=1)
    sha256: Digest

    @model_validator(mode="after")
    def bounded(self):
        if (self.kind == "transcript") != (self.index is not None):
            raise ValueError("index belongs to transcript parts")
        if self.bytes > PART_CAPS[self.kind]:
            raise ValueError("part too large")
        return self


class UploadsRequest(ContractModel):
    series: Series
    parts: list[UploadPart] = Field(min_length=1, max_length=64)


class CommitTranscript(ContractModel):
    parts: list[ManifestPart] = Field(max_length=64)
    total_bytes: int = Field(ge=0)


class RepoInfo(ContractModel):
    """Where the session's code lives, as the author's Mac sees it (spec §3.3). The Mac is the
    owner's own device, so a wrong answer only mis-describes the owner's own repository."""

    remote: Annotated[str, Field(min_length=1, max_length=256)]
    branch: Annotated[str, Field(max_length=256)] | None = None
    head: GitSha
    pushed: bool
    public: bool
    # The repository's top-level folder name: hidden from outsiders when the repo is private.
    # Null only for a repository at `/`.
    folder: Annotated[str, Field(min_length=1, max_length=255)] | None = None


class CommitRequest(ContractModel):
    series: Series
    conversation_id: OpaqueId
    claude_version: Annotated[str, Field(min_length=1, max_length=64)]
    cwd_name: Annotated[str, Field(max_length=128)] | None = None
    cwd_relative: Annotated[str, Field(max_length=1024)]
    ended: bool
    transcript: CommitTranscript
    code: ManifestCode | None = None
    # Drop what the series holds: the bundle grew too large, or the repository (or its sidecar
    # files) is gone. Null alone keeps it.
    code_skipped: Literal["too-large", "none"] | None = None
    sidecars: ManifestSidecars | None = None
    sidecars_skipped: Literal["none"] | None = None
    repo: RepoInfo | None = None


class UnavailableRequest(ContractModel):
    reason: Literal["transcript-missing", "too-large", "unsupported-version"]


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


def _uploaded_parts(
    row: dict, series: str, parts: list[ManifestPart], objects: list[tuple[str, int]]
) -> list[bool] | None:
    client, bucket = _s3(), _bucket()

    def matches(key: str, size: int) -> bool:
        try:
            return client.head_object(Bucket=bucket, Key=key)["ContentLength"] == size
        except ClientError:
            return False

    hashed = []
    for part in parts:
        digest_key = _key(row, series, "transcript", index=part.index, sha=part.sha256)
        legacy_key = _key(row, series, "transcript", index=part.index)
        if matches(digest_key, part.bytes):
            hashed.append(True)
        elif matches(legacy_key, part.bytes):
            hashed.append(False)
        else:
            return None
    return hashed if all(matches(key, size) for key, size in objects) else None


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


async def ready_manifest(row: dict) -> dict:
    """The committed manifest a pick-up or a copy may use, or the reason it may not."""
    if not row.get("pickupAllowed"):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Pick-up is off for this session.")
    if _expired(row):
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "This session's snapshot has expired.")
    manifest = await store._get(row["pk"], "PICKUP")
    if (row.get("pickupSnapshot") or {}).get("state") != "ready" or manifest is None:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "The snapshot is not ready yet.")
    if manifest.get("pickupEpoch", row["pickupEpoch"]) != row["pickupEpoch"]:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Snapshot permission changed. Try again.")
    return manifest


async def request_pickup(auth: AuthContext, session_id: str, pickup_id: str) -> PickupTicket:
    """Any workspace member, the author included. A paused session reads as not found."""
    pickup_id = validate_uuid(pickup_id, label="pick-up ID")
    row, _ = await sessions.authorize_session(auth, None, session_id)
    # A removed member's Clerk JWT outlives their membership: the same fence as watch/type/resume.
    await _still_a_member(row, auth.user_id)
    manifest = await ready_manifest(row)
    table = store._table()
    now = int(time.time())
    try:
        # One pickupId is one person's: a replay returns fresh URLs, a stranger's reuse is refused.
        await run_in_thread(
            table.put_item,
            Item={
                "pk": row["pk"],
                "sk": f"PICKUPREQ#{pickup_id}",
                "pickupId": pickup_id,
                "userId": auth.user_id,
                "series": manifest["series"],
                "at": now,
                "expiresAt": now + PICKUP_RETENTION_SECONDS,
            },
            ConditionExpression="attribute_not_exists(sk) OR userId = :me",
            ExpressionAttributeValues={":me": auth.user_id},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        raise ApiProblem("IDEMPOTENCY_CONFLICT", "pickupId was already used.") from exc
    capture_product_event(
        distinct_id=auth.user_id,
        event="session_picked_up",
        properties={
            "workspace_id": row["ownerId"],
            "session_id": row["sessionId"],
            "provider": row["provider"],
            "author_user_id": row["humanUserId"],
        },
        # The web client reuses pickup_id on a retry after a failed download or launch; the
        # same id is the same pick-up, so PostHog dedupes it to one row.
        event_uuid=str(uuid.uuid5(_ANALYTICS_NS, f"{row['ownerId']}:{row['sessionId']}:{pickup_id}:session_picked_up")),
    )
    from core.services import handoff_analytics

    handoff_analytics.handoff(workspace_id=row["ownerId"], user_id=auth.user_id, kind="session_picked_up")
    return await run_in_thread(_ticket, row, manifest, pickup_id)


def downloads(row: dict, manifest: dict) -> Downloads:
    series = manifest["series"]
    client, bucket = _s3(), _bucket()

    def url(key: str) -> str:
        return client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=URL_SECONDS)

    code, sidecars = manifest.get("code"), manifest.get("sidecars")
    return Downloads(
        transcript=[
            TranscriptDownload(
                index=p["index"],
                url=url(_part_key(row, series, p)),
                bytes=p["bytes"],
                sha256=p["sha256"],
            )
            for p in manifest["parts"]
        ],
        code=Download(url=url(_key(row, series, "code", sha=code["sha256"])), **_blob(code)) if code else None,
        sidecars=Download(url=url(_key(row, series, "sidecars", sha=sidecars["sha256"])), **_blob(sidecars))
        if sidecars
        else None,
    )


def _ticket(row: dict, manifest: dict, pickup_id: str) -> PickupTicket:
    return PickupTicket(
        pickup_id=pickup_id,
        session=TicketSession(
            session_id=row["sessionId"],
            human_user_id=row["humanUserId"],
            title=row.get("title"),
            provider=row["provider"],
        ),
        manifest=_manifest_public(manifest),
        downloads=downloads(row, manifest),
        expires_at=iso(int(time.time()) + URL_SECONDS),
    )


def _blob(stored: dict) -> dict:
    return {"bytes": stored["bytes"], "sha256": stored["sha256"]}


def _ticket_remote(remote: str) -> str:
    """The author's Mac stores the remote already reduced to `host/org/repo`. Every desktop's
    ticket parser runs that through `normalize_remote`, which reads only URL and scp forms, so
    the bare form was refused as "malformed" (every code pick-up since #3330). An https URL
    reduces to the same `host/org/repo` on every installed desktop."""
    return remote if "://" in remote or ":" in remote else f"https://{remote}"


def _manifest_public(manifest: dict) -> Manifest:
    code = manifest["code"]
    return Manifest.model_validate(
        {
            **{key: manifest[key] for key in ("series", "sidecars", "ended")},
            "code": {**code, "remote": _ticket_remote(code["remote"])} if code else None,
            "parts": [{key: value for key, value in part.items() if key != "hashed"} for part in manifest["parts"]],
            **{key: manifest[key] for key in ("claudeVersion", "conversationId", "cwdName", "cwdRelative")},
            "snapshotAt": iso(manifest["snapshotAt"]),
        }
    )


async def report_outcome(auth: AuthContext, session_id: str, pickup_id: str, outcome: PickupOutcome) -> None:
    from core.services.local_session_relay import publish_resources_changed

    pickup_id = validate_uuid(pickup_id, label="pick-up ID")
    row, _ = await sessions.authorize_session(auth, None, session_id)
    owner, sid = row["ownerId"], row["sessionId"]
    request = await store._get(row["pk"], f"PICKUPREQ#{pickup_id}")
    if request is None or request.get("userId") != auth.user_id:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Pick-up not found.")
    if not outcome.ok:
        logger.info("session pick-up failed", extra={"sid": sid, "pickupId": pickup_id, "code": outcome.code})
        return
    table = store._table().name
    items = [
        # Reported once: a retried outcome must not audit the same pick-up twice.
        {
            "Update": {
                "TableName": table,
                "Key": {"pk": row["pk"], "sk": request["sk"]},
                "UpdateExpression": "SET reportedAt = :now",
                "ConditionExpression": "attribute_not_exists(reportedAt)",
                "ExpressionAttributeValues": {":now": int(time.time())},
            }
        },
        _audit(owner, sid, auth, "picked_up", request_id=pickup_id, detail=outcome.branch),
    ]
    if outcome.device_id and outcome.profile_fingerprint and outcome.conversation_id:
        try:
            # Lineage names only the caller's own Mac; it never lands on someone else's session.
            await sessions._paired(owner, auth.user_id, outcome.device_id, None)
            future = repo.session_id(
                owner, outcome.device_id, "claude", outcome.profile_fingerprint, outcome.conversation_id
            )
            lineage = {
                "pk": repo.session_pk(owner, future),
                "sk": "LINEAGE",
                "sourceSessionId": sid,
                "sourceHumanUserId": row["humanUserId"],
                "pickupId": pickup_id,
                "at": int(time.time()),
            }
            items.append({"Put": {"TableName": table, "Item": lineage}})
        except ApiProblem:
            logger.warning("session pick-up lineage skipped: device not paired to the caller", extra={"sid": sid})
    try:
        await store._transact(items)
    except ClientError as exc:
        if not store._is_cancelled(exc):
            raise
        return  # a retried outcome: already audited and already announced
    # An open session view refetches its audit on this, so the owner sees the pick-up at once.
    await publish_resources_changed(owner, row["humanUserId"], session_id=sid)


async def lineage(owner: str, sid: str) -> dict | None:
    """``pickedUpFrom`` for a session's first registration, when a pick-up started it."""
    found = await store._get(repo.session_pk(owner, sid), "LINEAGE")
    return {"sessionId": found["sourceSessionId"], "humanUserId": found["sourceHumanUserId"]} if found else None


# --- The author's Mac ---------------------------------------------------------------------------


async def ack_with_pickup(ack: dict, owner: str, human: str, device: str) -> dict:
    """Add this device's wanted sessions to a relay acknowledgement. Extra keys in an existing
    acknowledgement are ignored by every installed desktop; an empty list is omitted."""
    try:
        rows = await store._query(repo.user_pk(owner, human), begins_with=f"PICKUPWANT#{device}#")
    except Exception:
        logger.warning("Session pick-up wants unavailable")  # never costs the heartbeat
        return ack
    entries = [
        {"sessionId": r["sessionId"], "provider": r["provider"], "conversationId": r["conversationId"]}
        for r in sorted(rows, key=lambda r: r.get("allowedAt", 0))[-ACK_CAP:]
    ]
    while entries:
        candidate = {**ack, "pickup": entries}
        if len(json.dumps(candidate, separators=(",", ":")).encode("utf-8")) <= sessions.MAX_ENVELOPE_BYTES:
            return candidate
        entries = entries[1:]  # oldest first
    return ack


async def authorize_device(token: str, session_id: str) -> tuple[dict, dict] | None:
    """The paired Mac that owns this session, for its own human, sharing on, pick-up allowed.
    None on any failure: the route answers a bare 403."""
    from core.services.machine_auth import _device

    identity = await _device(token, "openclaw-macos")
    if identity is None or identity.device_id is None:
        return None
    try:
        sid = validate_uuid(session_id, label="local session ID")
        # Pairing is this user's openclaw-macos entry, and sharing is enabled on it.
        enrollment = await sessions._enrolled(identity.owner_id, identity.user_id, identity.device_id)
    except ApiProblem:
        return None
    row = await repo.get_session(identity.owner_id, sid)
    if (
        row is None
        or row.get("ownerId") != identity.owner_id
        or row["humanUserId"] != identity.user_id
        or row["deviceId"] != identity.device_id
        or row["provider"] != "claude"
        or not row.get("pickupAllowed")
        # Paused sharing stores nothing, the transcript relay included.
        or row.get("unshared")
    ):
        return None
    if _expired(row):
        # A session allowed but never uploaded has no prefix for the sweeper to find; its Mac
        # asking again is what retires it, so its want row cannot outlive it.
        try:
            await retire(row["ownerId"], row["sessionId"], row)
        except Exception as exc:
            logger.warning("session pick-up expiry failed: %s", type(exc).__name__, extra={"sid": sid})
        return None
    return row, enrollment


def _continuity(stored: dict | None) -> DeviceRefused:
    return DeviceRefused(
        409,
        {
            "series": (stored or {}).get("series"),
            "nextIndex": (stored or {}).get("nextIndex", 0),
            "offset": (stored or {}).get("offset", 0),
        },
    )


async def presign_uploads(row: dict, request: UploadsRequest) -> dict:
    stored = await store._get(row["pk"], "PICKUP")
    same = stored is not None and stored["series"] == request.series
    held = sum(p["bytes"] for p in stored["parts"]) if same else 0
    if held + sum(p.bytes for p in request.parts) > SNAPSHOT_CAP:
        raise DeviceRefused(413)
    # Below nextIndex would overwrite a committed part: the Mac resynchronises before uploading.
    floor = stored["nextIndex"] if same else 0
    if any(p.index is not None and not floor <= p.index < MAX_PARTS for p in request.parts):
        raise _continuity(stored)

    def sign() -> list[dict]:
        client, bucket = _s3(), _bucket()
        out = []
        for part in request.parts:
            key = _key(row, request.series, part.kind, index=part.index, sha=part.sha256)
            url = client.generate_presigned_url(
                # ContentLength is signed: a part cannot be larger than it was declared.
                "put_object",
                Params={"Bucket": bucket, "Key": key, "ContentLength": part.bytes},
                ExpiresIn=UPLOAD_URL_SECONDS,
            )
            out.append({"kind": part.kind, **({"index": part.index} if part.index is not None else {}), "url": url})
        return out

    return {"uploads": await run_in_thread(sign), "expiresIn": UPLOAD_URL_SECONDS}


async def commit(row: dict, enrollment: dict, request: CommitRequest) -> None:
    from core.services.local_session_relay import publish_resources_changed

    owner, sid, series = row["ownerId"], row["sessionId"], request.series
    if request.conversation_id != row["nativeConversationId"]:
        raise DeviceRefused(400)
    for _ in range(6):
        current = await repo.get_session(owner, sid)
        if current is None or not current.get("pickupAllowed") or current["deviceId"] != row["deviceId"]:
            raise DeviceRefused(403)
        # Named under this read's epoch: objects uploaded for a retired permission never count.
        named = [
            (_key(current, series, kind, sha=blob.sha256), blob.bytes)
            for kind, blob in (("code", request.code), ("sidecars", request.sidecars))
            if blob is not None
        ]
        # Checked under this read's revision: an off/on between check and write (which deleted the
        # objects) cancels the write, and the re-check refuses the missing objects.
        hashed = await run_in_thread(_uploaded_parts, current, series, request.transcript.parts, named)
        if hashed is None:
            raise DeviceRefused(400)
        stored = await store._get(row["pk"], "PICKUP")
        same = stored is not None and stored["series"] == series
        parts = list(stored["parts"]) if same else []
        index, offset = (stored["nextIndex"], stored["offset"]) if same else (0, 0)
        if not same and not request.transcript.parts:
            raise _continuity(stored)
        for part, digest_key in zip(request.transcript.parts, hashed):
            if part.index != index or part.from_ != offset or part.to <= part.from_:
                raise _continuity(stored)
            stored_part = part.model_dump(by_alias=True)
            if digest_key:
                stored_part["hashed"] = True
            parts.append(stored_part)
            index, offset = index + 1, part.to
        if request.transcript.total_bytes != offset or index > MAX_PARTS:
            raise _continuity(stored)
        # Null keeps what this series already holds; a new series holds only what it names.
        if request.code is not None:
            code = request.code.model_dump(by_alias=True, exclude_none=True)
        else:
            code = stored.get("code") if same and request.code_skipped is None else None
        if request.sidecars is not None:
            sidecars = request.sidecars.model_dump(by_alias=True)
        else:
            sidecars = stored.get("sidecars") if same and request.sidecars_skipped is None else None
        total = sum(p["bytes"] for p in parts) + (code or {}).get("bytes", 0) + (sidecars or {}).get("bytes", 0)
        if total > SNAPSHOT_CAP:
            raise DeviceRefused(413)
        now = int(time.time())
        manifest = {
            "pk": row["pk"],
            "sk": "PICKUP",
            "series": series,
            "pickupEpoch": current["pickupEpoch"],
            "parts": parts,
            "nextIndex": index,
            "offset": offset,
            "code": code,
            "sidecars": sidecars,
            "claudeVersion": request.claude_version,
            "conversationId": request.conversation_id,
            "cwdName": request.cwd_name,
            "cwdRelative": request.cwd_relative,
            "snapshotAt": now,
            "ended": request.ended,
            "repoInfo": request.repo.model_dump(by_alias=True) if request.repo else None,
        }
        # Objects this commit stops naming (an old series, a replaced or skipped blob) are left for
        # the sweeper, which spares them until URL_SECONDS after this: a ticket signed just before
        # still downloads them.
        dropped = stored is not None and bool(_manifest_keys(current, stored) - _manifest_keys(current, manifest))
        replaced_at = now if dropped else (stored or {}).get("replacedAt")
        if replaced_at is not None:
            manifest["replacedAt"] = replaced_at
        snapshot = {"at": now, "state": "ready", "bytes": total, "hasCode": code is not None}
        if code:
            snapshot["repo"] = code["remote"]
            if code.get("branch"):
                snapshot["branch"] = code["branch"]
        if request.repo and not request.repo.public:
            snapshot["privateFolder"] = request.repo.folder or ""  # read by session_links without a manifest read
        updated = {**current, "pickupSnapshot": snapshot, "revision": str(uuid.uuid4())}
        extra = [{"Put": {"TableName": store._table().name, "Item": manifest}}]
        if request.ended:
            extra.append(_want_delete(current))  # the last snapshot of an ended session is final
        try:
            await repo.put_session(updated, current, enrollment, extra=extra)
            break
        except ClientError as exc:
            if not store._is_cancelled(exc):
                raise
    else:
        raise DeviceRefused(503)
    await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])


async def mark_unavailable(row: dict, enrollment: dict, reason: str) -> None:
    from core.services.local_session_relay import publish_resources_changed

    owner, sid = row["ownerId"], row["sessionId"]
    for _ in range(6):
        current = await repo.get_session(owner, sid)
        if current is None or not current.get("pickupAllowed") or current["deviceId"] != row["deviceId"]:
            raise DeviceRefused(403)
        # Read after META, on every attempt: a commit landing after this read moves the revision
        # and cancels the write below, so the retry sees its manifest and never hides it.
        stored = await store._get(row["pk"], "PICKUP")
        if stored and stored["parts"]:
            # A committed snapshot outlives the Mac's transcript (Claude deletes it after 30 days, a
            # Mac can be wiped): it stays pick-up-able, and the Mac only stops being asked.
            await store._transact([_want_delete(row)])
            return
        snapshot = {"at": int(time.time()), "state": "unavailable", "reason": reason}
        updated = {**current, "pickupSnapshot": snapshot, "revision": str(uuid.uuid4())}
        try:
            await repo.put_session(updated, current, enrollment, extra=[_want_delete(current)])
            break
        except ClientError as exc:
            if not store._is_cancelled(exc):
                raise
    else:
        raise DeviceRefused(503)
    await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])


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


async def run_pickup_sweeper(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            if await container_repo.try_claim_pass("session_pickup_sweep", interval_s=SWEEP_INTERVAL_S):
                await sweep_once()
        except Exception as exc:
            logger.warning("session pick-up sweep failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SWEEP_TICK_S)
        except asyncio.TimeoutError:
            pass
