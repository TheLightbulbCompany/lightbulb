"""What a session link's holder can do (spec docs/superpowers/specs/2026-10-05-public-session-links.md
§3.2-§3.5): open its page, read its events, and take a copy to continue. The owner's side
(create, audience, turn off) is local_sessions.put_link/get_link/delete_link.

Anyone holding an "anyone" link reaches open and events with no sign-in. Nothing here returns an
owner, user, workspace or session id, or a path, to someone outside the session's workspace."""

from __future__ import annotations

import asyncio
import base64
import codecs
import gzip
import hashlib
import hmac
import json
import logging
import re
import secrets
import tarfile
import tempfile
import time
import uuid
from typing import Annotated, Literal

from botocore.exceptions import ClientError
from pydantic import Field, model_serializer

from core import encryption
from core.auth import AuthContext, resolve_owner_id
from core.dynamodb import run_in_thread
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions, session_pickup as pickup
from core.services.local_sessions import _drop_none
from core.services.messaging import ApiProblem, ContractModel
from core.services.session_redact import redact, redact_value
from core.services.session_screen import _still_a_member

logger = logging.getLogger(__name__)

GONE = "This link was turned off or is not for you."
Token = Annotated[str, Field(min_length=16, max_length=128)]


def _gone() -> ApiProblem:
    return ApiProblem("LOCAL_SESSION_NOT_FOUND", GONE)


class LinkTokenRequest(ContractModel):
    """The token travels in a body, never a path or query: those land in access logs."""

    token: Token


class LinkOpenRequest(LinkTokenRequest):
    refresh: bool = False  # the page's poll: same answer, no "opened" event


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


async def _audit_name(user_id: str) -> str:
    """For the owner's own audit trail: the workspace's usual display name, never a raw user id
    (human_display_name falls back to one)."""
    from core.services.messaging import human_display_name

    try:
        name = (await human_display_name(user_id)).strip()
    except Exception:
        return "Someone"
    return name[:80] if name and name != user_id else "Someone"


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
    # Not a member, so the link is "Anyone with the link" and they watch the whole terminal: the
    # title hides nothing (owner ruling 2026-10-08). Likely secrets and a private repo's folder name do.
    shown = redact(title, _project(row))
    if not _known(row):
        # No snapshot yet, so whether the repo is private (and its folder's name) is not known:
        # every path goes, the words stay.
        shown = " ".join(re.sub(r"(?<!\S)~?/\S*", "", shown).split())
    return shown or None


async def open_page(auth: AuthContext | None, token: str, *, refresh: bool = False) -> LinkPage:
    row, _, member = await resolve(auth, token)
    manifest = await store._get(row["pk"], "PICKUP") if row.get("pickupAllowed") else None
    continuable = (
        row["provider"] == "claude"
        and manifest is not None
        and (row.get("pickupSnapshot") or {}).get("state") == "ready"
        and not pickup._expired(row)
    )
    if auth is not None and not refresh:  # the page's poll re-opens every few seconds: one open, one event
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
MAX_LINK_VIEWERS = 20
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


class ViewingRequest(LinkTokenRequest):
    lease: Annotated[str, Field(max_length=128)] | None = None
    fresh: bool = False  # subscribed and has no screen: a request, granted at most once per ASK_EVERY_S per link
    retake: bool = False  # about to connect: trade this seat for a new one if its lease was already used


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
    from core.services import session_screen

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
    renewed = False
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
        if len(seats) - len(lapsed) >= MAX_LINK_VIEWERS:
            raise ApiProblem(
                "LOCAL_SESSION_VIEWERS_FULL", f"This session already has {MAX_LINK_VIEWERS} people watching by link."
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
    # One row says whether anyone watches by link, so `forward` reads one item per frame. It
    # outlives its viewers by CHANNEL_MEMORY_S, and with it the channel's seat count.
    await run_in_thread(
        table.update_item,
        Key=watched,
        UpdateExpression="SET #u = :exp, expiresAt = :ttl",
        ExpressionAttributeNames={"#u": "until"},
        ExpressionAttributeValues={":exp": expires, ":ttl": now + CHANNEL_MEMORY_S},
    )
    if sessions.public_settling(row, now):
        # No picture is asked of the Mac for a link viewer yet (`forward` would drop it): the page
        # asks again at its next renewal, and that ask is not spent here.
        fresh = False
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
    return Viewing(
        lease=lease,
        channel=f"/pub/{public}",
        watch_id=public,
        http_endpoint=settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
        realtime_endpoint=settings.APPSYNC_EVENTS_REALTIME_ENDPOINT,
    )


# --- Copies (spec §3.5) ------------------------------------------------------------------------

COPY_TTL_S = pickup.COPY_TTL_S
CODE_WINDOW_S = 600
RESTART_AFTER_S = 300
COPIES_PER_HOUR = 20  # per session
TAKER_COPIES_PER_HOUR = 5  # per session per taker, so one outsider cannot use up the session's cap
_redacting = asyncio.Semaphore(1)  # one redaction at a time per backend task
_running: set[asyncio.Task] = set()  # held so a running preparation is not garbage-collected
_working: set[str] = set()  # copyIds this task is preparing or has queued

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
    now = int(time.time())
    # ponytail: read-then-write, so two simultaneous presses can both pass; the per-session cap and
    # the one-redaction-at-a-time semaphore bound the cost. Add a conditional claim if it is abused.
    live = [p for p in await store._query(row["pk"], begins_with="COPY#") if int(p.get("expiresAt", 0)) > now]
    if not member:
        existing = await _pending_copy(live, auth.user_id, link["tokenHash"])
        if existing:
            code = await _reuse(existing, challenge, now)
            if code:
                return CopyCode(code=code)
    mine = sum(1 for p in live if p.get("userId") == auth.user_id)
    if mine >= TAKER_COPIES_PER_HOUR or len(live) >= COPIES_PER_HOUR:
        raise ApiProblem("RATE_LIMITED", "Too many copies of this session. Try again later.", retryable=True)
    code = secrets.token_urlsafe(32)
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
        detail=await _audit_name(auth.user_id),
        subject_user_id=auth.user_id,
    )
    # The per-session pointer retire follows to delete this copy's objects.
    pointer = {
        "pk": row["pk"],
        "sk": f"COPY#{item['copyId']}",
        "copyId": item["copyId"],
        "userId": auth.user_id,
        "itemPk": item["pk"],
        "codeEnc": encryption.encrypt(code),
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


async def _pending_copy(live: list[dict], user_id: str, link_hash: str) -> dict | None:
    """This taker's copy of the session that is still being prepared, if any."""
    for pointer in live:
        if pointer.get("userId") != user_id:
            continue
        item = await store._get(pointer["itemPk"], "META")
        if item and item["state"] == "preparing" and item["linkHash"] == link_hash:
            return {**item, "codeEnc": pointer["codeEnc"]}
    return None


async def _reuse(existing: dict, challenge: str, now: int) -> str | None:
    """Press Continue again while a copy is preparing: the same copy, with this press's verifier."""
    try:
        code = encryption.decrypt(existing["codeEnc"])
    except ValueError:
        return None
    await run_in_thread(
        store._table().update_item,
        Key={"pk": existing["pk"], "sk": "META"},
        UpdateExpression="SET challenge = :c, createdAt = :now",
        ExpressionAttributeValues={":c": challenge, ":now": now},
    )
    return code


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


async def redeem(code: str, verifier: str) -> CopyTicket | Preparing:
    """The CLI's poll. The code alone is useless: it needs the verifier whose hash the confirm
    page sent, so a code left in browser history redeems nothing."""
    gone = ApiProblem("LOCAL_SESSION_NOT_FOUND", "This continue code is no longer valid. Press Continue again.")
    item = await store._get(_copy_pk(code), "META")
    now = int(time.time())
    if (
        item is None
        or now >= int(item["expiresAt"])
        or now >= max(int(item["createdAt"]), int(item.get("readyAt", 0))) + CODE_WINDOW_S
    ):
        raise gone
    if not hmac.compare_digest(item["challenge"], challenge_of(verifier)):
        # Its own code: the CLI reads it as "another program answered the confirm page first".
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "This continue code was confirmed for another program.")
    row = await repo.get_session(item["ownerId"], item["sessionId"])
    link = await _live(row, item["linkHash"])  # turned off, paused, Mac stopped: each says so
    if row.get("pickupEpoch") != item["epoch"]:
        raise _turned_off()  # the snapshot was retired (and maybe allowed again): this copy is gone
    try:
        # The same door Continue went through: a workspace link opens only for a current member.
        if item["member"]:
            await _still_a_member(row, item["userId"])
        elif not sessions.public_id(link, row):
            raise _gone()
    except ApiProblem as exc:
        raise _gone() from exc
    if item["state"] == "failed":
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "This copy could not be prepared. Press Continue again.")
    if item["state"] == "preparing":
        if now - int(item["startedAt"]) > RESTART_AFTER_S and item["copyId"] not in _working:
            try:
                await _settle(item, {"startedAt": now})
                _start({**item, "startedAt": now})
            except Exception:
                pass  # another poll restarted it first (a worker on another task is not visible here)
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
