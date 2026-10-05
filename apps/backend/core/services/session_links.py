"""What a session link's holder can do (spec docs/superpowers/specs/2026-10-05-public-session-links.md
§3.2-§3.5): open its page, read its events, and take a copy to continue. The owner's side
(create, audience, turn off) is local_sessions.put_link/get_link/delete_link.

Anyone holding an "anyone" link reaches open and events with no sign-in. Nothing here returns an
owner, user, workspace or session id, or a path, to someone outside the session's workspace."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import io
import json
import logging
import secrets
import tarfile
import tempfile
import time
import uuid
from typing import Annotated, Literal

from pydantic import Field, model_serializer

from core.auth import AuthContext, resolve_owner_id
from core.dynamodb import run_in_thread
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions, session_pickup as pickup
from core.services.local_sessions import PICKUP_RETENTION_SECONDS, _drop_none
from core.services.messaging import ApiProblem, ContractModel
from core.services.session_redact import MARK, redact, redact_value
from core.services.session_screen import _still_a_member

logger = logging.getLogger(__name__)

GONE = "This link was turned off or is not for you."
Token = Annotated[str, Field(min_length=16, max_length=128)]


def _gone() -> ApiProblem:
    return ApiProblem("LOCAL_SESSION_NOT_FOUND", GONE)


class LinkTokenRequest(ContractModel):
    """The token travels in a body, never a path or query: those land in access logs."""

    token: Token


class LinkEventsRequest(LinkTokenRequest):
    after_seq: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


async def _member(auth: AuthContext | None, row: dict) -> bool:
    if auth is None or auth.org_id != row["ownerId"]:
        return False
    try:
        await _still_a_member(row, auth.user_id)
    except ApiProblem:
        return False
    return True


async def resolve(auth: AuthContext | None, token: str) -> tuple[dict, dict, bool]:
    """(META row, its shareLink, whether the caller is a current member). Every refusal reads the
    same: no row, a hash META does not hold, paused, the Mac no longer shares, past the snapshot
    retention, or a workspace link for someone outside it. The route bounds the token's length."""
    digest = sessions._token_hash(token)
    lookup = await store._get(sessions._share_pk(digest), "META")
    row = await repo.get_session(lookup["ownerId"], lookup["sessionId"]) if lookup else None
    link = (row or {}).get("shareLink") or {}
    # Plain !=: the lookup row was found by this hash, so the comparison reveals nothing new.
    if row is None or row.get("unshared") or link.get("tokenHash") != digest:
        raise _gone()
    try:
        await sessions._enrolled(row["ownerId"], row["humanUserId"], row["deviceId"])
    except ApiProblem as exc:
        raise _gone() from exc
    stopped = sessions.pickup_stopped_at(row)
    if stopped is not None and int(time.time()) >= stopped + PICKUP_RETENTION_SECONDS:
        raise _gone()
    member = await _member(auth, row)
    if link["audience"] != "anyone" and not member:
        raise _gone()
    return row, link, member


async def _name(user_id: str, *, first: bool) -> str:
    """A display name, never an id: human_display_name falls back to the Clerk user id."""
    from core.services.messaging import human_display_name

    try:
        name = (await human_display_name(user_id)).strip()
    except Exception:
        name = ""
    if not name or name == user_id:
        return "Someone"
    return name.split()[0][:40] if first else name[:80]


class LinkCode(ContractModel):
    visibility: Literal["public", "private", "none", "workspace"]
    remote: str | None = None
    branch: str | None = None
    commit: str | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("remote", "branch", "commit"))


class LinkMe(ContractModel):
    signed_in: bool
    member: bool


class LinkPage(ContractModel):
    title: str | None
    owner_first_name: str
    provider: Literal["claude", "codex"]
    state: Literal["live", "ended", "offline"]
    code: LinkCode
    continuable: bool
    me: LinkMe
    # Members only: their in-app paths (Watch, Pick up) need it.
    session_id: str | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("session_id",))


def code_line(row: dict, manifest: dict | None, member: bool) -> LinkCode:
    """Spec §3.3's table. A private repo carries the flag and nothing else."""
    if member:
        snapshot = row.get("pickupSnapshot") or {}
        if snapshot.get("repo"):
            return LinkCode(visibility="workspace", remote=snapshot["repo"], branch=snapshot.get("branch"))
        return LinkCode(visibility="none")
    info = (manifest or {}).get("repoInfo")
    if not info or not info.get("pushed"):
        return LinkCode(visibility="none")
    if not info.get("public"):
        return LinkCode(visibility="private")
    return LinkCode(visibility="public", remote=info["remote"], branch=info.get("branch"), commit=info["head"])


def _state(row: dict) -> str:
    if row.get("endedAt") is not None:
        return "ended"
    return "live" if int(row["onlineUntil"]) > int(time.time()) else "offline"


def _known(row: dict) -> bool:
    """A Claude session's first snapshot has landed, so its repo's privacy is known (Codex has none)."""
    return row["provider"] != "claude" or (row.get("pickupSnapshot") or {}).get("state") == "ready"


def _project(row: dict) -> str | None:
    """The private repo's folder name, hidden from outsiders (controller ruling 2026-10-05)."""
    return (row.get("pickupSnapshot") or {}).get("privateFolder") or None


def _title(row: dict, member: bool) -> str | None:
    title = row.get("displayTitle") or row.get("title")
    if member or title is None:
        return title
    return redact(title, _project(row)) if _known(row) else None


async def open_page(auth: AuthContext | None, token: str) -> LinkPage:
    row, _, member = await resolve(auth, token)
    manifest = await store._get(row["pk"], "PICKUP") if row.get("pickupAllowed") else None
    continuable = (
        row["provider"] == "claude"
        and manifest is not None
        and (row.get("pickupSnapshot") or {}).get("state") == "ready"
        and not pickup._expired(row)
    )
    if auth is not None:
        sessions._capture_growth(row, "share_link_opened", auth.user_id, member=member)
    return LinkPage(
        title=_title(row, member),
        owner_first_name=await _name(row["humanUserId"], first=True),
        provider=row["provider"],
        state=_state(row),
        code=code_line(row, manifest, member),
        continuable=continuable,
        me=LinkMe(signed_in=auth is not None, member=member),
        session_id=row["sessionId"] if member else None,
    )


def _event(stored: dict, member: bool, project: str | None) -> sessions.LocalSessionEvent:
    if member:
        return sessions._public(sessions.LocalSessionEvent, stored)
    data = {k: v for k, v in stored.items() if k not in ("actor", "originRequestId")}
    for key in ("text", "tool"):
        if key in data:
            data[key] = redact_value(data[key], project)
    try:
        return sessions._public(sessions.LocalSessionEvent, data)
    except ValueError:  # redaction grew it past the 16 KiB envelope: one line in its place
        return sessions.LocalSessionEvent(
            seq=stored["seq"], kind=stored["kind"], role=stored["role"], text=MARK, truncated=True
        )


async def list_events(
    auth: AuthContext | None, token: str, after_seq: int, limit: int
) -> sessions.LocalSessionEventPage:
    row, _, member = await resolve(auth, token)
    if not member and not _known(row):
        return sessions.LocalSessionEventPage(events=[], next_after_seq=None)  # Review Focus 6
    rows, next_seq = await repo.list_events(row["ownerId"], row["sessionId"], after_seq, limit)
    project = None if member else _project(row)
    return sessions.LocalSessionEventPage(events=[_event(r, member, project) for r in rows], next_after_seq=next_seq)


# --- Copies (spec §3.5) ------------------------------------------------------------------------

COPY_TTL_S = pickup.COPY_TTL_S
CODE_WINDOW_S = 600
RESTART_AFTER_S = 300
_redacting = asyncio.Semaphore(1)  # one redaction at a time per backend task
_running: set[asyncio.Task] = set()  # held so a running preparation is not garbage-collected

Challenge = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]


class CopyRequest(ContractModel):
    token: Token
    challenge: Challenge


class CopyCode(ContractModel):
    code: str


class RedeemRequest(ContractModel):
    code: Token
    verifier: Annotated[str, Field(min_length=43, max_length=128)]


class CopyCodeMode(ContractModel):
    mode: Literal["clone", "bundle", "none"]
    remote: str | None = None
    commit: str | None = None  # clone: the commit to check out
    snapshot: str | None = None  # bundle: the snapshot commit
    session_id: str | None = None  # bundle: names refs/lightbulb/snap/<id> inside the bundle (members only)

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("remote", "commit", "snapshot", "session_id"))


class CopyTicket(ContractModel):
    """Not a PickupTicket: that one names the session, its human and the folder."""

    state: Literal["ready"] = "ready"
    title: str | None
    from_: str = Field(alias="from")
    conversation_id: str
    claude_version: str
    cwd_relative: str
    downloads: pickup.Downloads
    code: CopyCodeMode


class Preparing(ContractModel):
    state: Literal["preparing"] = "preparing"


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _copy_pk(code: str) -> str:
    return f"SESSIONCOPY#{sessions._token_hash(code)}"


def _code_mode(manifest: dict, member: bool) -> str:
    if member:
        return "bundle" if manifest.get("code") else "none"
    info = manifest.get("repoInfo") or {}
    return "clone" if info.get("public") and info.get("pushed") else "none"


async def create_copy(auth: AuthContext, token: str, challenge: str) -> CopyCode:
    resolve_owner_id(auth)  # the taker's own workspace: the free one sign-up gives (409 without)
    row, link, member = await resolve(auth, token)
    if row["provider"] != "claude":
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Only Claude Code sessions can be continued.")
    manifest = await pickup.ready_manifest(row)
    code, now = secrets.token_urlsafe(32), int(time.time())
    item = {
        "pk": _copy_pk(code),
        "sk": "META",
        "ownerId": row["ownerId"],
        "sessionId": row["sessionId"],
        "copyId": uuid.uuid4().hex,
        "userId": auth.user_id,
        "member": member,
        "challenge": challenge,
        "linkHash": link["tokenHash"],
        "epoch": row["pickupEpoch"],
        "codeMode": _code_mode(manifest, member),
        "state": "ready" if member else "preparing",
        "createdAt": now,
        "startedAt": now,
        "expiresAt": now + COPY_TTL_S,
    }
    table = store._table().name
    audit = sessions.audit_row(
        row["ownerId"],
        row["sessionId"],
        auth,
        "session_copied",
        detail=await _name(auth.user_id, first=False),
        subject_user_id=auth.user_id,
    )
    # The per-session pointer retire follows to delete this copy's objects.
    pointer = {
        "pk": row["pk"],
        "sk": f"COPY#{item['copyId']}",
        "copyId": item["copyId"],
        "expiresAt": item["expiresAt"],
    }
    await store._transact(
        [
            {"Put": {"TableName": table, "Item": item}},
            {"Put": {"TableName": table, "Item": pointer}},
            {"Put": {"TableName": table, "Item": audit}},
        ]
    )
    if not member:
        _start(item)
    sessions._capture_growth(row, "session_copied", auth.user_id, member=member)
    return CopyCode(code=code)


def _start(item: dict) -> None:
    task = asyncio.get_running_loop().create_task(_prepare(item))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _settle(item: dict, values: dict) -> None:
    """Write the outcome only if this run is still the copy's current one (a restart moves startedAt)."""
    names = {f"#{k}": k for k in values}
    await run_in_thread(
        store._table().update_item,
        Key={"pk": item["pk"], "sk": "META"},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in values),
        ConditionExpression="startedAt = :started",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**{f":{k}": v for k, v in values.items()}, ":started": item["startedAt"]},
    )


async def _prepare(item: dict) -> None:
    async with _redacting:
        try:
            row = await repo.get_session(item["ownerId"], item["sessionId"])
            manifest = await store._get(row["pk"], "PICKUP") if row else None
            if not row or not manifest or row.get("pickupEpoch") != item["epoch"]:
                raise RuntimeError("snapshot gone")
            info = manifest.get("repoInfo") or {}
            project = info.get("folder") if info and not info.get("public") else None
            out = await run_in_thread(_redact_copy, row, manifest, item["copyId"], project)
            out.update(
                state="ready",
                readyAt=int(time.time()),
                conversationId=manifest["conversationId"],
                claudeVersion=manifest["claudeVersion"],
                cwdRelative=manifest["cwdRelative"] if item["codeMode"] == "clone" else "",
                repo=manifest.get("repoInfo") if item["codeMode"] == "clone" else None,
            )
            await _settle(item, out)
        except Exception as exc:
            logger.warning("session copy failed: %s", type(exc).__name__, extra={"sid": item["sessionId"]})
            try:
                await _settle(item, {"state": "failed"})
            except Exception:
                pass  # a newer run owns it, or the row expired


def _redact_line(line: bytes, project: str | None) -> bytes:
    end = b"\n" if line.endswith(b"\n") else b""
    text = line[: len(line) - len(end)].decode("utf-8", errors="replace")
    try:
        body = json.dumps(redact_value(json.loads(text), project), ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        body = redact(text, project)
    return body.encode("utf-8") + end


def _measure(file) -> tuple[int, str]:
    file.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(1 << 20), b""):
        digest.update(chunk)
    size = file.tell()
    file.seek(0)
    return size, digest.hexdigest()


def _redact_copy(row: dict, manifest: dict, copy_id: str, project: str | None) -> dict:
    """Stream every transcript part through the redactor, one part at a time (parts are up to
    64 MiB, the snapshot up to 1 GiB): gunzip, redact each JSONL line, gzip, upload. A line the Mac
    split across parts (one longer than its chunk) is carried whole into the next part. Sync:
    call through run_in_thread."""
    client, bucket = pickup._s3(), pickup._bucket()
    base = f"{pickup.COPIES_PREFIX}/{copy_id}/"
    parts, carry, last = [], b"", len(manifest["parts"]) - 1
    for n, part in enumerate(manifest["parts"]):
        source = client.get_object(Bucket=bucket, Key=pickup._part_key(row, manifest["series"], part))["Body"]
        with tempfile.TemporaryFile() as out:
            with gzip.GzipFile(fileobj=source, mode="rb") as lines, gzip.GzipFile(fileobj=out, mode="wb") as gz:
                for line in lines:
                    line, carry = carry + line, b""
                    if not line.endswith(b"\n") and n != last:
                        carry = line
                        continue
                    gz.write(_redact_line(line, project))
            size, digest = _measure(out)
            key = f"{base}t-{part['index']:06d}.gz"
            client.upload_fileobj(out, bucket, key)
        parts.append({"index": part["index"], "bytes": size, "sha256": digest, "key": key})
    sidecars = None
    if manifest.get("sidecars"):
        source = client.get_object(
            Bucket=bucket, Key=pickup._key(row, manifest["series"], "sidecars", sha=manifest["sidecars"]["sha256"])
        )["Body"]
        with tempfile.TemporaryFile() as out:
            with tarfile.open(fileobj=source, mode="r|gz") as tin, tarfile.open(fileobj=out, mode="w:gz") as tout:
                for member in tin:
                    if member.isdir():
                        tout.addfile(member)
                        continue
                    if not member.isfile():
                        continue
                    # ponytail: one sidecar file is read whole; stream it if a single file nears memory limits.
                    data = tin.extractfile(member).read()
                    try:
                        data.decode("utf-8")
                    except UnicodeDecodeError:
                        continue  # binary entries are dropped, never shown unredacted
                    body = b"".join(_redact_line(line, project) for line in data.splitlines(keepends=True))
                    info = tarfile.TarInfo(member.name)
                    info.size, info.mode, info.mtime = len(body), member.mode, member.mtime
                    tout.addfile(info, io.BytesIO(body))
            size, digest = _measure(out)
            key = f"{base}sidecars.tgz"
            client.upload_fileobj(out, bucket, key)
        sidecars = {"bytes": size, "sha256": digest, "key": key}
    return {"parts": parts, "sidecars": sidecars}


async def redeem(code: str, verifier: str) -> CopyTicket | Preparing:
    """The CLI's poll. The code alone is useless: it needs the verifier whose hash the confirm
    page sent, so a code left in browser history redeems nothing."""
    gone = ApiProblem("LOCAL_SESSION_NOT_FOUND", "This continue code is no longer valid. Press Continue again.")
    item = await store._get(_copy_pk(code), "META")
    now = int(time.time())
    if (
        item is None
        or now >= int(item["expiresAt"])
        or not hmac.compare_digest(item["challenge"], challenge_of(verifier))
        or now >= max(int(item["createdAt"]), int(item.get("readyAt", 0))) + CODE_WINDOW_S
    ):
        raise gone
    row = await repo.get_session(item["ownerId"], item["sessionId"])
    if (
        row is None
        or (row.get("shareLink") or {}).get("tokenHash") != item["linkHash"]
        or row.get("pickupEpoch") != item["epoch"]
    ):
        raise gone  # the link was turned off (or the snapshot retired) since Continue was pressed
    if item["state"] == "failed":
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "This copy could not be prepared. Press Continue again.")
    if item["state"] == "preparing":
        if now - int(item["startedAt"]) > RESTART_AFTER_S:
            try:
                await _settle(item, {"startedAt": now})
                _start({**item, "startedAt": now})
            except Exception:
                pass  # another poll restarted it first
        return Preparing()
    manifest = await store._get(row["pk"], "PICKUP") if item["member"] else None
    owner = await _name(row["humanUserId"], first=True)
    return await run_in_thread(_copy_ticket, row, item, manifest, owner)


def _copy_ticket(row: dict, item: dict, manifest: dict | None, owner: str) -> CopyTicket:
    if manifest is not None:  # a member: the snapshot as it is, Pick up's behaviour
        code = manifest.get("code")
        downloads = pickup.downloads(row, manifest)
        mode = (
            CopyCodeMode(mode="bundle", remote=code["remote"], snapshot=code["snapshot"], session_id=row["sessionId"])
            if code
            else CopyCodeMode(mode="none")
        )
        meta = (manifest["conversationId"], manifest["claudeVersion"], manifest["cwdRelative"] if code else "")
    else:
        client, bucket = pickup._s3(), pickup._bucket()

        def url(key: str) -> str:
            return client.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=pickup.URL_SECONDS
            )

        side = item.get("sidecars")
        downloads = pickup.Downloads(
            transcript=[
                pickup.TranscriptDownload(index=p["index"], url=url(p["key"]), bytes=p["bytes"], sha256=p["sha256"])
                for p in item["parts"]
            ],
            code=None,
            sidecars=pickup.Download(url=url(side["key"]), bytes=side["bytes"], sha256=side["sha256"])
            if side
            else None,
        )
        repo_info = item.get("repo") or {}
        mode = (
            CopyCodeMode(mode="clone", remote=repo_info["remote"], commit=repo_info["head"])
            if item["codeMode"] == "clone"
            else CopyCodeMode(mode="none")
        )
        meta = (item["conversationId"], item["claudeVersion"], item.get("cwdRelative", ""))
    return CopyTicket(
        title=_title(row, item["member"]),
        from_=owner,
        conversation_id=meta[0],
        claude_version=meta[1],
        cwd_relative=meta[2],
        downloads=downloads,
        code=mode,
    )
