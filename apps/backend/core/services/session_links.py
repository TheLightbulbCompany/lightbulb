"""What a session link's holder can do (spec docs/superpowers/specs/2026-10-05-public-session-links.md
§3.2-§3.5): open its page, read its events, and take a copy to continue. The owner's side
(create, audience, turn off) is local_sessions.put_link/get_link/delete_link.

Anyone holding an "anyone" link reaches open and events with no sign-in. Nothing here returns an
owner, user, workspace or session id, or a path, to someone outside the session's workspace."""

from __future__ import annotations

import logging
import time
from typing import Annotated, Literal

from pydantic import Field, model_serializer

from core.auth import AuthContext
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
