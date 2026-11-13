"""What a session link's holder can do (spec docs/superpowers/specs/2026-10-05-public-session-links.md
§3.2-§3.5): open its page, read its events, and take a copy to continue. The owner's side
(create, audience, turn off) is local_sessions.put_link/get_link/delete_link.

Anyone holding an "anyone" link reaches open and events with no sign-in. Nothing here returns an
owner, user, workspace or session id, or a path, to someone outside the session's workspace."""

from __future__ import annotations

import asyncio
import codecs
import gzip
import hashlib
import json
import logging
import secrets
import tarfile
import tempfile
import time
import uuid

from botocore.exceptions import ClientError

from core.auth import AuthContext
from core.dynamodb import run_in_thread
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions, session_pickup as pickup
from core.services.messaging import ApiProblem, ContractModel
from core.services.session_redact import redact, redact_value
from core.services.session_screen import _still_a_member

logger = logging.getLogger(__name__)

GONE = "This link was turned off or is not for you."


def _gone() -> ApiProblem:
    return ApiProblem("LOCAL_SESSION_NOT_FOUND", GONE)


async def _member(auth: AuthContext | None, row: dict) -> bool:
    if auth is None or auth.org_id != row["ownerId"]:
        return False
    try:
        await _still_a_member(row, auth.user_id)
    except ApiProblem:
        return False
    return True


def _turned_off() -> ApiProblem:
    return ApiProblem("LOCAL_SESSION_NOT_FOUND", "The owner turned this link off.")


async def _live(row: dict | None, digest: str) -> dict:
    """The link's shareLink when this session still shares it: not paused, hash matches META, the
    Mac still enrolled, inside the snapshot retention. Each refusal has its own code and message
    for redeem (the taker's CLI, past PKCE); resolve turns every one into the same GONE."""
    link = (row or {}).get("shareLink") or {}
    # Plain !=: the lookup row was found by this hash, so the comparison reveals nothing new.
    # A removed session (`sessions.remove_session`) is refused here on its own: its link is
    # turned off after the stamp, and that cleanup may not have run yet.
    if row is None or row.get("removedAt") is not None or link.get("tokenHash") != digest:
        raise _turned_off()
    if row.get("unshared"):
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "The owner stopped sharing this session.")
    try:
        await sessions._enrolled(row["ownerId"], row["humanUserId"], row["deviceId"])
    except ApiProblem as exc:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "The owner's Mac stopped sharing sessions.") from exc
    if sessions.pickup_expired(row):
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "This session's snapshot expired.")
    return link


async def resolve(auth: AuthContext | None, token: str) -> tuple[dict, dict, bool]:
    """(META row, its shareLink, whether the caller is a current member). Every refusal reads the
    same: no row, a hash META does not hold, paused, the Mac no longer shares, past the snapshot
    retention, or a workspace link for someone outside it. The route bounds the token's length."""
    digest = sessions._token_hash(token)
    lookup = await store._get(sessions._share_pk(digest), "META")
    row = await repo.get_session(lookup["ownerId"], lookup["sessionId"]) if lookup else None
    try:
        link = await _live(row, digest)
    except ApiProblem as exc:
        raise _gone() from exc
    member = await _member(auth, row)
    if not sessions.public_id(link, row) and not member:
        raise _gone()
    return row, link, member


# open and redeem are unauthenticated: one Clerk call per request would let a reload loop spend the
# Clerk budget webhooks and provisioning share. Per process, like messaging.human_display_name.
_NAME_TTL_S, _NO_NAME_TTL_S = 300.0, 60.0
_names: dict[str, tuple[float, str]] = {}


async def _name(user_id: str, *, first: bool) -> str:
    """The owner's real name (Clerk first/last), never an id, username or email prefix."""
    from core.services.clerk_admin import get_user

    now = time.monotonic()
    hit = _names.get(user_id)
    if hit and now < hit[0]:
        name = hit[1]
    else:
        try:
            user = await get_user(user_id) or {}
        except Exception:
            user = {}
        name = " ".join(p.strip() for p in (user.get("first_name") or "", user.get("last_name") or "") if p.strip())
        _names[user_id] = (now + (_NAME_TTL_S if name else _NO_NAME_TTL_S), name)
    if not name:
        return "Someone"
    return name.split()[0][:40] if first else name[:80]


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
    # Not a member, so the link is "Anyone with the link" and they watch the whole terminal: the
    # title hides nothing (owner ruling 2026-10-08). Likely secrets and a private repo's folder name do.
    shown = redact(title, _project(row))
    if not _known(row):
        # No snapshot yet, so whether the repo is private (and its folder's name) is not known.
        # No path shape is guessed at: every word holding "/", "\\" or "~" goes, whatever
        # punctuation is around it. Nothing left: no title (the page says "A shared session").
        shown = " ".join(word for word in shown.split() if not set(word) & set("/\\~"))
    return shown or None


# --- Ask to join from the page (06 §2, D1/D2) ---------------------------------------------------
#
# The one ask to join left (owner ruling 2026-10-26: members never ask), made by a signed-in person outside
# the workspace on an "Anyone with the link" page. The row is the session's own `joinRequests`,
# marked `outsider` and carrying the label the owner sees. The owner's Allow
# (local_sessions.decide_join) makes them a workspace member, then lets them use the session.


# --- Watching (owner ruling 2026-10-08: an "Anyone" link shows the live terminal, view only) ---
#
# PUBWATCH#<publicId> / VIEWER#<sha256(key)>   one link viewer's 45 s lease; `connectedAt` and
#                                              `subscribedAt` once it opened its one connection and
#                                              its one subscription (the authorizer writes them)
# PUBWATCH#<publicId> / WATCHED                when the newest lease lapses (`until`), when the Mac
#                                              was last asked for a picture (`askedAt`), and the
#                                              seats taken in the channel's life (`seats`)
# SHARELINK#<tokenHash> / SEATS                the seats the LINK gave out in this ten minutes
#                                              (`mintWindow`, `minted`), whatever its channel
#
# The lease `<publicId>.<key>` is the viewer's whole credential on AppSync: the `pub` namespace's
# Lambda authorizer (apps/infra/lambda/pub-authorizer) admits a subscribe to `/pub/<publicId>` and
# nothing else while that row is unexpired. Frames reach the channel only while the link is public
# and has a live lease (`session_screen.forward` reads the row on every frame), so narrowing the
# link, Only me, ending or removing the session stops them at once; an open subscription then hears
# nothing, and the next renewal is refused. A viewer gets screen frames and nothing else: no input,
# chat, events or lists, and neither the session's nor the workspace's id (frames carry the publicId).

# ponytail: counted, not locked: two viewers racing for the last seat both get in, and a viewer
# who stops renewing keeps an open subscription uncounted until the page closes.
# T18 load test on dev, 2026-10-15 (scripts/load/k6/link_viewers.js): 100 viewers on one link held
# p95 frame latency ~300 ms and 99.99% delivery; the backend did not reach its ceiling.
MAX_LINK_VIEWERS = 100
# A lease connects once and subscribes once (the authorizer consumes it), so every connection
# and every subscription costs a seat taken here, and a link gives out at most this many per ten minutes: what bounds the sockets a link
# holder can pile up on one channel (AppSync never asks the authorizer again once a subscription
# is open). An honest viewer takes one per page load, reconnect or return to the tab; 20 of them
# on bad networks fit. Narrowing the link silences every socket at once: the channel is renamed.
MINT_WINDOW_S = 600
MINTS_PER_WINDOW = 120
# That budget is a rate, and an open subscription never ends by itself. So a channel also has a
# life total: the seat after its last moves the LINK to a new `publicId` (same token, same
# audience), and `forward` publishes only to the current one, so no channel ever has more
# subscriptions than this and every socket on the old one goes silent at once. Viewers there follow
# at their next 15 s renewal: it answers with a seat on the new channel. The window budget is the
# link's, counted first and kept across a move, so a refused seat costs the channel nothing and a
# link changes channel at most once per window (a channel needs more seats than a window gives).
# No key exchange, no sweeper: the old channel's rows lapse by TTL.
MAX_CHANNEL_SEATS = 200
# How long a channel's seat count is kept after its last viewer: longer than any socket subscribed
# to it can live (AppSync ends a connection after 24 h). Were the count forgotten sooner, the next
# seat would start the same channel at one, with its old sockets still listening.
CHANNEL_MEMORY_S = 25 * 3600
# A link's viewers together get the Mac asked for history and a snapshot at most this often: each
# ask is a burst out of the session's frame budget, which workspace viewers share, and `fresh` is
# only a stranger's word. Per link, not per lease: taking new seats buys no more asks. A viewer
# who joins inside the window has no picture yet: its page asks again with each 15 s renewal until
# it draws a snapshot, and the first of those is past the window, so it waits at most one renewal
# (15 s, plus delivery) on an idle session. Sooner when the Mac sends a snapshot of its own (every
# 10 s while the screen prints) or another viewer's ask is granted: every viewer hears that picture.
ASK_EVERY_S = 15


def public_pk(public_id: str) -> str:
    return f"PUBWATCH#{public_id}"


class Viewing(ContractModel):
    lease: str
    channel: str
    watch_id: str  # the publicId: what this viewer's frames carry in place of the session's ids
    http_endpoint: str
    realtime_endpoint: str


async def _count_mint(table, link: dict, now: int) -> None:
    """One more seat out of this link's ten-minute budget, or the same answer as a full session.
    On the link's own row, so the count outlives a channel."""
    window = now // MINT_WINDOW_S
    budget = {"pk": sessions._share_pk(link["tokenHash"]), "sk": "SEATS"}
    ttl = {":ttl": now + 3600}
    count = (
        "SET minted = minted + :one, expiresAt = :ttl",
        "mintWindow = :w AND minted < :max",
        {":max": MINTS_PER_WINDOW, **ttl},
    )
    start = (
        "SET mintWindow = :w, minted = :one, expiresAt = :ttl",
        "attribute_not_exists(mintWindow) OR mintWindow <> :w",
        ttl,
    )
    # Count again after a lost start: two seats racing to open a window both belong in it.
    for expression, condition, values in (count, start, count):
        try:
            await run_in_thread(
                table.update_item,
                Key=budget,
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeValues={":w": window, ":one": 1, **values},
            )
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise ApiProblem(
        "LOCAL_SESSION_VIEWERS_FULL", "Too many people opened this link just now. Try again in a few minutes."
    )


async def _seat_in_channel(table, watched: dict) -> bool:
    """One more seat out of this channel's life total; False when it has given out its last."""
    try:
        await run_in_thread(
            table.update_item,
            Key=watched,
            UpdateExpression="SET seats = if_not_exists(seats, :zero) + :one",
            ConditionExpression="attribute_not_exists(seats) OR seats < :max",
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":max": MAX_CHANNEL_SEATS},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def _new_channel(row: dict, public: str) -> None:
    """Move the link to a new publicId. Conditional on the old one, so of two seats racing past the
    total one moves it and the other finds it moved (or narrowed): both then read the same row.
    The revision moves too, so a writer holding the row from before this starts over."""
    try:
        await run_in_thread(
            store._table().update_item,
            Key={"pk": row["pk"], "sk": "META"},
            UpdateExpression="SET shareLink.publicId = :new, revision = :rev",
            ConditionExpression="shareLink.publicId = :old",
            ExpressionAttributeValues={":new": str(uuid.uuid4()), ":rev": str(uuid.uuid4()), ":old": public},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def viewing(token: str, lease: str | None, fresh: bool, retake: bool = False, *, moved: bool = False) -> Viewing:
    """A link holder has the terminal on screen: every 15 s, no sign-in. The token is checked on
    every call, so a link that stopped being public refuses the next renewal."""
    from core.config import settings
    from core.services import session_presence, session_screen

    row, link, _ = await resolve(None, token)
    public = sessions.public_id(link, row)
    if not public:
        raise _gone()
    now = int(time.time())
    # A running session only. An ended one's kept final screen is for the workspace.
    if row.get("endedAt") is not None or int(row.get("onlineUntil", 0)) <= now:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "This session is not running.")
    if sessions.session_terminal(row) is None:
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This session has no live terminal.")
    table = store._table()
    expires = now + session_screen.VIEWER_TTL_S

    def key_of(secret: str) -> dict:
        return {"pk": public_pk(public), "sk": f"VIEWER#{sessions._token_hash(secret)}"}

    watched = {"pk": public_pk(public), "sk": "WATCHED"}
    renewed = seated = False
    held = lease.partition(".")[2] if lease and lease.startswith(public + ".") else ""
    if held:
        # `retake`: the viewer is about to connect, and a lease connects and subscribes once (the
        # authorizer marks it). The server decides: an unused lease is only renewed, so a connect
        # that never reached AppSync costs no seat; a used one is traded.
        unused = " AND attribute_not_exists(connectedAt) AND attribute_not_exists(subscribedAt)" if retake else ""
        try:
            await run_in_thread(
                table.update_item,
                Key=key_of(held),
                UpdateExpression="SET expiresAt = :exp",
                ConditionExpression="attribute_exists(pk) AND expiresAt > :now" + unused,
                ExpressionAttributeValues={":exp": expires, ":now": now},
            )
            renewed = True
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            if retake:
                # One viewer, one seat: the old row goes before the new one is counted against the cap.
                await run_in_thread(table.delete_item, Key=key_of(held))
    if not renewed:
        seats = await store._query(public_pk(public), begins_with="VIEWER#")
        lapsed = [{"pk": r["pk"], "sk": r["sk"]} for r in seats if int(r["expiresAt"]) <= now]
        if lapsed:
            # Their TTL delete lags up to 48 h: dropped here, so this read stays as small as the cap.
            def drop() -> None:
                with table.batch_writer() as batch:
                    for key in lapsed:
                        batch.delete_item(Key=key)

            await run_in_thread(drop)
        watching = len(seats) - len(lapsed)
        if watching >= MAX_LINK_VIEWERS:
            # `watching` is a count, nothing else: the page says "Full: N people are watching".
            raise ApiProblem(
                "LOCAL_SESSION_VIEWERS_FULL",
                f"This session already has {watching} people watching by link.",
                details={"watching": watching},
            )
        # The link's budget first: a seat it refuses costs the channel nothing. Counted once, so
        # not again on the way to a new channel.
        if not moved:
            await _count_mint(table, link, now)
        if not await _seat_in_channel(table, watched):
            if moved:  # the new channel filled while this call was on its way to it
                raise ApiProblem("LOCAL_SESSION_VIEWERS_FULL", "Too many people opened this link just now. Try again.")
            await _new_channel(row, public)
            return await viewing(token, None, fresh, moved=True)
        secret = secrets.token_urlsafe(32)
        lease = f"{public}.{secret}"
        await run_in_thread(table.put_item, Item={**key_of(secret), "expiresAt": expires})
        seated = True
    # One row says whether anyone watches by link, so `forward` reads one item per frame. It
    # outlives its viewers by CHANNEL_MEMORY_S, and with it the channel's seat count.
    await run_in_thread(
        table.update_item,
        Key=watched,
        UpdateExpression="SET #u = :exp, expiresAt = :ttl",
        ExpressionAttributeNames={"#u": "until"},
        ExpressionAttributeValues={":exp": expires, ":ttl": now + CHANNEL_MEMORY_S},
    )
    if fresh:
        try:
            await run_in_thread(
                table.update_item,
                Key=watched,
                UpdateExpression="SET askedAt = :now",
                ConditionExpression="attribute_not_exists(askedAt) OR askedAt <= :cut",
                ExpressionAttributeValues={":now": now, ":cut": now - ASK_EVERY_S},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            fresh = False
    await session_screen.tell_mac(row, now, fresh)
    if seated:
        # The owner's band counts the new link viewer now, not at the next heartbeat ack.
        await session_presence.push(row)
    return Viewing(
        lease=lease,
        channel=f"/pub/{public}",
        watch_id=public,
        http_endpoint=settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
        realtime_endpoint=settings.APPSYNC_EVENTS_REALTIME_ENDPOINT,
    )


# --- Copies (spec §3.5) ------------------------------------------------------------------------

_redacting = asyncio.Semaphore(1)  # one redaction at a time per backend task
_running: set[asyncio.Task] = set()  # held so a running preparation is not garbage-collected
_working: set[str] = set()  # copyIds this task is preparing or has queued


def _code_mode(manifest: dict, member: bool) -> str:
    if member:
        return "bundle" if manifest.get("code") else "none"
    info = manifest.get("repoInfo") or {}
    return "clone" if info.get("public") and info.get("pushed") else "none"


def _start(item: dict) -> None:
    _working.add(item["copyId"])
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
    try:
        await _prepare_locked(item)
    finally:
        _working.discard(item["copyId"])


async def _prepare_locked(item: dict) -> None:
    async with _redacting:
        try:
            row = await repo.get_session(item["ownerId"], item["sessionId"])
            manifest = await store._get(row["pk"], "PICKUP") if row else None
            if not row or not manifest or row.get("pickupEpoch") != item["epoch"]:
                raise RuntimeError("snapshot gone")
            info = manifest.get("repoInfo") or {}
            # Fail closed: no repoInfo (an older Mac, no origin) is treated as private.
            private = not info.get("public")
            project = info.get("folder") if private else None
            # From the manifest as it is now: a commit since Continue may say the repo went private.
            mode = _code_mode(manifest, False)
            out = await run_in_thread(_redact_copy, row, manifest, item["copyId"], project, private)
            out.update(
                state="ready",
                readyAt=int(time.time()),
                codeMode=mode,
                conversationId=manifest["conversationId"],
                claudeVersion=manifest["claudeVersion"],
                cwdRelative=manifest["cwdRelative"] if mode == "clone" else "",
                repo=info if mode == "clone" else None,
            )
            await _settle(item, out)
        except Exception as exc:
            logger.warning("session copy failed: %s", type(exc).__name__, extra={"sid": item["sessionId"]})
            try:
                await _settle(item, {"state": "failed"})
            except Exception:
                pass  # a newer run owns it, or the row expired


LINE_CAP = 8 * 1024 * 1024
CHUNK = 1 << 20
OMITTED = b'{"type":"redacted","note":"line omitted: too long"}\n'
_THINKING = ("thinking", "redacted_thinking")


def _scrub(value, private: bool):
    """Outsider copies: no thinking blocks (their signatures cover text we would rewrite), and no
    branch name when the repository is private (Claude writes gitBranch on every line)."""
    if isinstance(value, list):
        return [_scrub(v, private) for v in value if not (isinstance(v, dict) and v.get("type") in _THINKING)]
    if isinstance(value, dict):
        return {k: _scrub(v, private) for k, v in value.items() if not (private and k == "gitBranch")}
    return value


def _redact_line(line: bytes, project: str | None, private: bool = False) -> bytes:
    end = b"\n" if line.endswith(b"\n") else b""
    text = line[: len(line) - len(end)].decode("utf-8", errors="replace")
    try:
        body = json.dumps(
            redact_value(_scrub(json.loads(text), private), project), ensure_ascii=False, separators=(",", ":")
        )
    except ValueError:
        body = redact(text, project)
    return body.encode("utf-8") + end


class _Lines:
    """Feed it chunks (readline(CHUNK) results, never more than CHUNK bytes each); it returns the
    redacted bytes for each completed line. A line past LINE_CAP is skipped with a one-line marker, so
    memory is bounded by LINE_CAP + CHUNK however long a line is. State carries across transcript
    parts, because the Mac cuts a long line at its chunk size."""

    def __init__(self, project: str | None, private: bool) -> None:
        self.project, self.private, self.carry, self.skipping = project, private, b"", False

    def feed(self, chunk: bytes) -> bytes:
        done = chunk.endswith(b"\n")
        if self.skipping:
            self.skipping = not done
            return b""
        line, self.carry = self.carry + chunk, b""
        if len(line) > LINE_CAP:
            self.skipping = not done
            return OMITTED
        if not done:
            self.carry = line
            return b""
        return _redact_line(line, self.project, self.private)

    def finish(self) -> bytes:
        line, self.carry = self.carry, b""
        return _redact_line(line, self.project, self.private) if line and not self.skipping else b""


def _measure(file) -> tuple[int, str]:
    file.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(1 << 20), b""):
        digest.update(chunk)
    size = file.tell()
    file.seek(0)
    return size, digest.hexdigest()


def _redact_copy(row: dict, manifest: dict, copy_id: str, project: str | None, private: bool = False) -> dict:
    """Stream every transcript part through the redactor, one part at a time (parts are up to
    64 MiB, the snapshot up to 1 GiB): gunzip, redact each JSONL line, gzip, upload. A line the Mac
    split across parts is carried whole into the next part (up to LINE_CAP). Sync: call through
    run_in_thread."""
    client, bucket = pickup._s3(), pickup._bucket()
    base = f"{pickup.COPIES_PREFIX}/{copy_id}/"
    parts, last = [], len(manifest["parts"]) - 1
    lines = _Lines(project, private)
    for n, part in enumerate(manifest["parts"]):
        source = client.get_object(Bucket=bucket, Key=pickup._part_key(row, manifest["series"], part))["Body"]
        with tempfile.TemporaryFile() as out:
            with gzip.GzipFile(fileobj=source, mode="rb") as reader, gzip.GzipFile(fileobj=out, mode="wb") as gz:
                for chunk in iter(lambda: reader.readline(CHUNK), b""):
                    gz.write(lines.feed(chunk))
                if n == last:
                    gz.write(lines.finish())
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
                    elif member.isfile():
                        _redact_entry(tin.extractfile(member), member, tout, project, private)
            size, digest = _measure(out)
            key = f"{base}sidecars.tgz"
            client.upload_fileobj(out, bucket, key)
        sidecars = {"bytes": size, "sha256": digest, "key": key}
    return {"parts": parts, "sidecars": sidecars}


def _redact_entry(source, member, tout, project: str | None, private: bool) -> None:
    """One sidecar file, line by line into a temp file (never whole in memory). An entry that is not
    UTF-8 is dropped, never shown unredacted."""
    decoder, lines = codecs.getincrementaldecoder("utf-8")(), _Lines(project, private)
    with tempfile.TemporaryFile() as body:
        try:
            for chunk in iter(lambda: source.readline(CHUNK), b""):
                decoder.decode(chunk)
                body.write(lines.feed(chunk))
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return
        body.write(lines.finish())
        info = tarfile.TarInfo(member.name)
        info.size, info.mode, info.mtime = body.tell(), member.mode, member.mtime
        body.seek(0)
        tout.addfile(info, body)
