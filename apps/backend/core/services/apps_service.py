"""An app's lifecycle over the Stripe Projects rail: source -> up -> preview -> publish ->
rollback -> remove, and status.

Nothing here waits. Each call does its writes and returns; readiness moves only on reads
(``up``, ``publish``, ``status``), each of which takes ONE step per pending thing: one
``advance_on_read`` per open workflow, one ``deployment_state`` per pending deployment, one
``verify_domain`` per unverified domain. State lives on the app record. Credentials are read
from Secrets Manager and pushed to Vercel; they never reach a result, an error or a log line
(the record keeps only a fingerprint of what was pushed).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import logging
import random
import re
import secrets as _random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

import httpx

from core.config import settings
from core.dynamodb import plain, utc_now_iso
from core.repositories import apps_repo, messaging_repo
from core.repositories import provisioning_workflow_repo as wf_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import (
    apps_db,
    apps_dns,
    apps_room,
    apps_source,
    clerk_admin,
    email_service,
    messaging,
    network_proof,
    provisioning_workflow,
)
from core.services import stripe_provisioning as api
from core.services import vercel_client as vercel
from core.services.credentials import integration_secret_store
from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.auth import AuthContext

logger = logging.getLogger(__name__)

# (provider, service, environment): day one always files all three.
_RESOURCES = (("vercel", "project", "prod"), ("neon", "postgres", "prod"), ("neon", "postgres", "dev"))
_DEAD = frozenset({"rejected", "cancelled"})  # the owner said no: the next `up` asks again
_BROKEN = frozenset({"failed", "requires_review"})
_PREVIEW_BUILD = "next build"  # previews never migrate
_PROD_BUILD = "next build"  # publish() applies the live migrations itself, before the build
_DESTRUCTIVE = re.compile(
    r"\bDROP\s+TABLE\b|\bDROP\s+COLUMN\b|\bRENAME\b|\bALTER\s+COLUMN\b.*\bTYPE\b|\bTRUNCATE\b|\bDELETE\s+FROM\b",
    re.IGNORECASE | re.DOTALL,
)
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_DB_URL = re.compile(r"postgres(?:ql)?://\S+")
# A line the starter's /api/_lightbulb/errors route printed (plan 2 logs browser errors with this marker).
_CLIENT_MARKS = ("[lightbulb:client]", "/api/_lightbulb/errors")
_UPLOADS = 8  # concurrent file uploads per deploy
_LEASE_S = 60  # one read steps an app at a time (R56)


@dataclass(frozen=True)
class BuilderCaller:
    owner_id: str
    actor_id: str
    actor_kind: Literal["agent", "person"]
    run_id: str | None = None  # X-Lightbulb-Run: the agent run this call came from


class AppsRefused(provisioning_workflow.WorkflowRefused):
    """A refusal the route maps to 409/403; ``next`` tells the builder what to do."""

    def __init__(self, code: str, message: str, next: str = ""):
        super().__init__(code, message)
        self.next = next


class Card(TypedDict):
    workflow_id: str
    provider: str
    service: str
    state: str


class AppError(TypedDict):
    code: str
    message: str
    next: str
    deployment_id: NotRequired[str]  # a failed build: the deployment errors() reads the log of
    at: NotRequired[str]  # when it failed, to tell it from a newer preview


class UpResult(TypedDict):
    app_id: str
    state: Literal["awaiting_approval", "provisioning", "building", "ready", "failed"]
    cards: list[Card]
    canvas_url: str
    sample_data: bool
    preview_url: NotRequired[str]
    live_url: NotRequired[str]
    version: NotRequired[int]
    cost_cents: NotRequired[int]
    error: NotRequired[AppError]
    rebuilding: NotRequired[bool]  # rollback: the version is on its way, not live yet


class AppStatus(TypedDict):
    app_id: str
    name: str
    display_name: str
    room_id: str | None
    conversation_id: str | None  # the app's own conversation (app_conversation), once it has one
    state: str
    open_to: str
    budget_usd: int
    preview_url: str | None
    live_url: str | None
    canvas_url: str
    live_version: int | None
    origin: dict
    versions: list[dict]
    resources: list[dict]
    last_opened_at: str | None
    spend_month_cents: None
    preview: dict | None
    stuck: dict | None
    agent_working: bool  # the newest linked agent run is open and beating: the preview is a first version
    error: NotRequired[AppError]


# --- small helpers ------------------------------------------------------------------------------


def _check_name(name: str) -> str:
    try:
        return apps_dns.check_name(name)
    except ValueError:
        raise AppsRefused(
            "invalid_name", f"{name!r} is not a valid app name.", "use lowercase letters, digits and dashes"
        ) from None


def _display(app: dict) -> str:
    """R74: the stored display name, else the slug title-cased ("crew-schedule" -> "Crew Schedule")."""
    return app.get("display_name") or " ".join(w.capitalize() for w in re.split(r"[-_]", app["name"]) if w)


def can_manage(app: dict, auth: AuthContext) -> bool:
    """R73: the app's creator or an org admin changes, shares, publishes and removes it."""
    return auth.is_org_admin or (bool(app.get("created_by")) and app.get("created_by") == auth.user_id)


async def require_manager(owner_id: str, app_id: str, auth: AuthContext) -> None:
    # Not _owned: a removed app's DELETE is retryable (R10), so a destroyed record still answers here.
    app = await apps_repo.get(app_id, consistent_read=True)
    if not app or app.get("owner_id") != owner_id:
        raise AppsRefused("app_not_found", "That app does not exist.")
    if not can_manage(app, auth):
        raise AppsRefused("not_allowed", "Only the app's creator or a workspace admin can do that.")


async def _owned(owner_id: str, app_id: str) -> dict:
    app = await apps_repo.get(app_id, consistent_read=True)
    if not app or app.get("owner_id") != owner_id:
        raise AppsRefused("app_not_found", "That app does not exist.")
    if app.get("state") == "destroyed":
        raise AppsRefused("app_destroyed", "That app was removed.")
    return plain(app)


async def _check_room(owner_id: str, room_id: str | None) -> None:
    """An app belongs to its workspace; a room is optional (where a request came from). A room that is
    given must be one of the caller's own workspace (membership is not required)."""
    if not room_id:
        return
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("deletedAt") or meta.get("ownerId") != owner_id:
        raise AppsRefused("room_not_found", "That room is not in this workspace.", "pick a room of this workspace")


def _check_open_to(open_to: str | None, room_id: str | None) -> None:
    """ "room" is a convenience for an app that came from one; the rest need no room."""
    if open_to == "room" and not room_id:
        raise AppsRefused(
            "no_room",
            "This app did not come from a room, so it can't be open to one.",
            "pick me, invited, workspace or public",
        )


def _rooms(app: dict) -> list[str]:
    """Where the app is talked about: its conversation, the room it came from, and an older app's room."""
    return list(
        dict.fromkeys(r for r in (app.get("conversation_id"), app.get("origin_room_id"), app.get("room_id")) if r)
    )


def _build_rooms(app: dict) -> list[str]:
    """Where its builder works: the conversation, and an older app's room. A started app's room is only where
    it was asked for (``room_id == origin_room_id``), so a busy agent there never holds its stuck line or wake."""
    room = app.get("room_id") if app.get("room_id") != app.get("origin_room_id") else None
    return list(dict.fromkeys(r for r in (app.get("conversation_id"), room) if r))


def _can_open(app: dict, auth: AuthContext, in_room: bool) -> bool:
    """Who opens it: "me" its managers; "room" and "invited" also the app's room (R35: invitees use their
    links); "workspace" and "public" every member."""
    mode = app["open_to"]
    return mode in ("workspace", "public") or can_manage(app, auth) or (mode in ("room", "invited") and in_room)


def _notify(app: dict) -> None:
    """Tell the owner's open Canvas the app changed. Fire-and-forget; never breaks the write."""

    async def _publish() -> None:
        try:
            from core.services.live_events import publish_owner_users
            from core.services.messaging import make_live_event

            event = make_live_event("runtime.resource.invalidated", {"resource": "apps", "resourceId": app["app_id"]})
            await publish_owner_users(app["owner_id"], event)
        except Exception:  # noqa: BLE001 — a live nudge never breaks its durable write
            logger.warning("apps live event failed for %s", app.get("app_id"), exc_info=True)

    try:
        asyncio.get_running_loop().create_task(_publish())
    except RuntimeError:
        return


async def _patch(app: dict, fields: dict, *, expect: dict | None = None) -> dict | None:
    if expect is None:
        await apps_repo.patch(app["app_id"], fields)
    elif await apps_repo.patch(app["app_id"], fields, expect=expect) is None:
        return None  # another writer changed the record first; ``app`` is left as read
    app.update(fields)
    if set(fields) != {"progress_lease_until"}:  # the lease moves on every step; nothing visible changed
        _notify(app)
    return app


def _preview_app(app: dict) -> str:
    """The ``app`` claim of a preview-only pass. A name is a DNS label, so ``:`` can't be another app's."""
    return f"{app['name']}:preview"


def _canvas_url(app_id: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=canvas&app={app_id}"


def _fp(env: dict) -> str:
    return hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()


async def _files(owner_id: str, app_id: str, source_id: str) -> dict[str, bytes]:
    try:
        data = await apps_source.get(owner_id, app_id, source_id)
    except apps_source.SourceNotFound:
        raise AppsRefused("source_not_found", "That source was not uploaded.", "upload the source again") from None
    return await asyncio.to_thread(lambda: dict(apps_source.iter_files(data)))


def _migrations(files: dict[str, bytes]) -> tuple[list[tuple[str, str]], str]:
    """Top-level ``drizzle/*.sql`` sorted by filename (seed excluded), and the seed."""
    names = sorted(p for p in files if re.fullmatch(r"drizzle/[^/]+\.sql", p) and p != "drizzle/seed.sql")
    try:
        sqls = [(p.removeprefix("drizzle/"), files[p].decode()) for p in names]
        return sqls, files.get("drizzle/seed.sql", b"").decode()
    except UnicodeDecodeError:
        raise AppsRefused(
            "source_refused", "A drizzle/*.sql file is not UTF-8 text.", "save the migration files as UTF-8"
        ) from None


def _migrations_key(files: dict[str, bytes]) -> str:
    """R63: what the preview reset is keyed on — each migration's name and content hash."""
    migrations, _ = _migrations(files)
    return _fp([[name, hashlib.sha256(sql.encode()).hexdigest()] for name, sql in migrations])


def _latest_source(app: dict) -> tuple[str, dict] | None:
    sources = app.get("sources") or {}
    return max(sources.items(), key=lambda kv: kv[1]["at"]) if sources else None


async def resolve_app_id(owner_id: str, ref: str) -> str:
    """A builder route's ``{app_id}``: the id, or the app's name in this workspace (names never start "app_")."""
    if ref.startswith("app_"):
        return ref
    app = await apps_repo.get_by_name(owner_id, ref)
    if not app:
        raise AppsRefused("app_not_found", "That app does not exist.")
    return app["app_id"]


async def latest_source(owner_id: str, app_id: str) -> tuple[bytes, str]:
    """The newest uploaded archive and its commit, for ``app pull``."""
    latest = _latest_source(await _owned(owner_id, app_id))
    if not latest:
        raise AppsRefused("source_not_found", "That app has no source yet.", "wait for its first upload")
    source_id, src = latest
    return await apps_source.get(owner_id, app_id, source_id), src["commit"]


def _source(app: dict, source_id: str) -> dict:
    src = (app.get("sources") or {}).get(source_id)
    if not src:
        raise AppsRefused("source_not_found", "That source was not uploaded.", "upload the source again")
    return src


# --- destructive migrations (R3) ----------------------------------------------------------------


async def _approval_ok(app: dict, card_id: str) -> bool:
    """R57: an approved card of this workspace that names THIS app (payload app_id or app_name)."""
    owner_id = app["owner_id"]
    try:
        company_id = await resolve_company_id_for_owner(
            owner_id=owner_id, repo=PaperclipRepo(table_name="paperclip-companies")
        )
        admin = shared_admin_client()
        cookie = await get_admin_session_cookie(admin._http)
        card = await admin.get_approval(session_cookie=cookie, approval_id=card_id)
    except (PaperclipApiError, httpx.HTTPError) as e:
        logger.warning("apps: approval read failed: %s", type(e).__name__)
        return False
    if not (company_id and isinstance(card, dict) and card.get("status") == "approved"):
        return False
    payload = card.get("payload") if isinstance(card.get("payload"), dict) else {}
    names_app = payload.get("app_id") == app["app_id"] or payload.get("app_name") == app["name"]
    return card.get("companyId") == company_id and names_app


async def _check_destructive(app: dict, files: dict[str, bytes], approved: str | None) -> None:
    """R57: every ``.sql`` in the archive (seeds excepted) not already published is scanned."""
    published = set(app.get("published_migrations") or [])
    sqls = [
        (p.removeprefix("drizzle/"), data.decode(errors="replace"))
        for p, data in sorted(files.items())
        if p.endswith(".sql") and p.rsplit("/", 1)[-1] != "seed.sql" and p.removeprefix("drizzle/") not in published
    ]
    found = [
        (name, f"{name}: {' '.join(stmt.split())[:200]}")
        for name, sql in sqls
        for stmt in _COMMENTS.sub("", sql).split(";")
        if _DESTRUCTIVE.search(stmt)
    ]
    hits = [h for _, h in found]
    if not hits:
        return
    if not approved:
        raise AppsRefused(
            "destructive_migration",
            "This source changes or removes existing data: " + "; ".join(hits),
            "rewrite add-only, or ask the owner and pass the approved card id",
        )
    if not await _approval_ok(app, approved):
        raise AppsRefused(
            "approval_invalid", "That card is not an approved card for this app.", "ask the owner to approve it"
        )
    # Single use: a card covers the destructive files it was first used for (its up and its publish).
    names = sorted({n for n, _ in found})
    used = dict(app.get("used_approvals") or {})
    if approved in used and not set(names) <= set(used[approved]):
        raise AppsRefused(
            "approval_invalid", "That card was already used for another change.", "ask the owner to approve this one"
        )
    if approved not in used:
        # ponytail: read-modify-write of the map; two first uses of different cards at once can drop one record.
        await _patch(app, {"used_approvals": {**used, approved: names}})


# --- workflows ----------------------------------------------------------------------------------


async def _rows(app: dict, all_rows: list[dict] | None = None) -> dict[tuple, dict]:
    """The latest workflow row for each of the app's three resources."""
    out: dict[tuple, dict] = {}
    if all_rows is None:
        all_rows = await wf_repo.list_for_owner(app["owner_id"])
    rows = [r for r in all_rows if r.get("app_id") == app["app_id"]]
    for r in sorted(rows, key=lambda r: r.get("created_at") or ""):
        out[((r.get("provider_name") or "").lower(), r.get("service_ref"), r.get("environment"))] = r
    return out


async def _ensure_workflows(app: dict, issue_id: str | None) -> None:
    rows = await _rows(app)
    display = app.get("display_name") or app["name"]
    # One card per app: hosting's card also covers the data, and names it. An app that already has
    # its cards keeps them.
    card = rows.get(("vercel", "project", "prod"))
    for provider, service, env in _RESOURCES:
        row = rows.get((provider, service, env))
        if row is None or row.get("state") in _DEAD:
            name = app["name"] if env == "prod" else f"{app['name']}-preview"
            carded = provider == "vercel"
            bundle: dict = (
                {
                    "also": [
                        await provisioning_workflow.consent_line(
                            app["owner_id"], "neon", "postgres", f"The data for {display}, live and preview"
                        )
                    ],
                    "title": f"Set up hosting and data for {display}",
                }
                if carded
                else {"bundled_with": card["workflow_id"]}
                if card and card.get("state") not in _DEAD
                else {}
            )
            started = await provisioning_workflow.start(
                owner_id=app["owner_id"],
                provider_name=provider,
                service_id=service,
                configuration={"name": name},
                issue_id=issue_id,
                # The owner reads this on the card: say what it is for, in words.
                reason=f"{display}: hosting and data"
                if carded
                else f"{display}: {'hosting' if provider == 'vercel' else 'a database'} ({'live' if env == 'prod' else 'preview'})",
                app_id=app["app_id"],
                environment=env,
                **bundle,
            )
            if carded:
                card = started


async def _secret(app: dict, rows: dict, provider: str, env: str, key: str) -> dict | None:
    """The resource's credential, only when its workflow is active and the secret landed."""
    service = "project" if provider == "vercel" else "postgres"
    if (rows.get((provider, service, env)) or {}).get("state") != "active":
        return None
    blob = await integration_secret_store.get(app["owner_id"], f"apps/{app['app_id']}/{provider}/{env}")
    return blob if blob and blob.get(key) else None


async def _cookie_secret(app: dict) -> str:
    key = f"apps/{app['app_id']}/lightbulb"
    blob = await integration_secret_store.get(app["owner_id"], key) or {}
    if not blob.get("cookie_secret"):
        blob = {"cookie_secret": base64.urlsafe_b64encode(_random.token_bytes(32)).decode().rstrip("=")}
        await integration_secret_store.put(app["owner_id"], key, blob)
    return blob["cookie_secret"]


class _WorkspaceUnknown(Exception):
    """Clerk didn't answer for an org owner: skip this read rather than push an env that changes later."""


async def _workspace_name(app: dict) -> str:
    """Stored on the record once known (never refreshed on an org rename): each env change redeploys."""
    if "workspace_name" in app:
        return app["workspace_name"]
    name = ""
    if app["owner_id"].startswith("org_"):  # personal workspaces have no Clerk org
        name = " ".join(((await clerk_admin.get_organization(app["owner_id"])) or {}).get("name", "").split())
        if not name:
            raise _WorkspaceUnknown
    await _patch(app, {"workspace_name": name})
    return name


# --- the per-read step --------------------------------------------------------------------------


async def _push_env(app: dict, creds: dict, dev_db: dict | None, live_db: dict | None) -> None:
    pk = network_proof.public_key()
    common = {
        "LIGHTBULB_ISSUER_KEY": json.dumps({"kty": "OKP", "crv": "Ed25519", "x": pk["key"], "kid": pk["kid"]}),
        "LIGHTBULB_APP": app["name"],
        "LIGHTBULB_OPEN_TO": app["open_to"],
        "LIGHTBULB_COOKIE_SECRET": await _cookie_secret(app),
        "LIGHTBULB_APP_ID": app["app_id"],
        "LIGHTBULB_APP_NAME": _display(app),
        "LIGHTBULB_WORKSPACE_NAME": await _workspace_name(app),
        "NEXT_PUBLIC_LIGHTBULB_API": settings.PUBLIC_API_BASE.rstrip("/"),
        # BLOB_READ_WRITE_TOKEN: never set; "content" is a starter switch and no blob store is provisioned (R79).
    }
    # Only the preview names this: a pass for it is refused on the live address (see ``test_link``).
    preview = {**common, "LIGHTBULB_SAMPLE_DATA": "1", "LIGHTBULB_PREVIEW_APP": _preview_app(app)}
    if dev_db:
        preview.update(DATABASE_URL=dev_db["connection_string"], LIGHTBULB_SAMPLE_DATA="0")
    production = {**common, **({"DATABASE_URL": live_db["connection_string"]} if live_db else {})}
    fps = dict(app.get("env_fp") or {})
    for target, env in (("preview", preview), ("production", production)):
        fp = _fp(env)
        if fps.get(target) == fp:
            continue
        await vercel.upsert_env(creds, env, [target])
        # R51: Vercel env reaches only NEW deployments; a running one keeps the old LIGHTBULB_OPEN_TO.
        fields: dict = {}
        if target in fps and target == "preview" and app.get("preview_built"):
            fields["preview_built"] = None  # this read's build step rebuilds it
        if target in fps and target == "production":
            await _redeploy_live(app, creds)  # before the fingerprint: a failed deploy retries next read
        fps[target] = fp
        await _patch(app, {**fields, "env_fp": fps})


async def _redeploy_live(app: dict, creds: dict, *, put_back: dict | None = None) -> None:
    """A production build of what is live (or about to be), through ``publish_pending``: it lands
    as a new version of the same commit. ``put_back``: build this older version instead."""
    pending = app.get("publish_pending") or {}
    live = next((v for v in app.get("versions") or [] if v.get("n") == app.get("live_version")), {})
    source_id = (
        (put_back or {}).get("source_id")
        or pending.get("source_id")
        or app.get("live_source_id")
        or live.get("source_id")
    )
    if not source_id or source_id not in (app.get("sources") or {}):
        if app.get("live_version"):
            logger.warning("apps: %s live source unknown; new settings reach it at the next publish", app["app_id"])
        return
    src = _source(app, source_id)
    dep_id = await _deploy(app, creds, source_id, production=True)
    await _patch(
        app,
        {
            "publish_pending": {
                "deployment_id": dep_id,
                "source_id": source_id,
                "commit": src["commit"],
                "by": pending.get("by") or "lightbulb",
                "reason": f"put back version {put_back['n']}" if put_back else "sharing changed",
                "migrations": src.get("migrations") or [],
                # A rebuilt put-back takes the place of the old version in the history, as an instant one does.
                **({"replaced": _replaced(put_back)} if put_back else {}),
                **({"replaced": pending["replaced"]} if not put_back and "replaced" in pending else {}),
            }
        },
    )


async def _deploy(app: dict, creds: dict, source_id: str, *, production: bool) -> str:
    files = await _files(app["owner_id"], app["app_id"], source_id)
    gate = asyncio.Semaphore(_UPLOADS)

    async def upload(data: bytes) -> str:
        async with gate:
            return await vercel.upload_file(creds, data)

    # R56: concurrent, so a request stays inside API Gateway's 30 s.
    shas = await asyncio.gather(*(upload(data) for data in files.values()))
    listing = [{"file": p, "sha": sha, "size": len(data)} for (p, data), sha in zip(files.items(), shas, strict=True)]
    dep = await vercel.create_deployment(
        creds,
        name=app["name"],
        files=listing,
        target="production" if production else None,
        build_command=_PROD_BUILD if production else _PREVIEW_BUILD,
        commit=_source(app, source_id)["commit"],
    )
    return dep["id"]


async def _check_preview(app: dict, creds: dict) -> None:
    pending = app.get("preview_pending")
    if not pending:
        return
    st = await vercel.deployment_state(creds, pending["deployment_id"])
    if st["readyState"] in ("READY", "ERROR") and st.get("target") == "production":
        # Vercel's docs: a project's first deployment is production whatever was asked, on the live env. Never a
        # preview. It can fail for that reason alone: a page that reads data at build time finds the live database
        # empty. The next deployment is a real one; two tries, in case Vercel keeps answering production.
        tries = int(app.get("preview_rebuilds") or 0)
        if tries < _PREVIEW_REBUILDS:
            await _clear_pending(app, "preview_pending", {"preview_built": None, "preview_rebuilds": tries + 1})
        elif st["readyState"] == "READY":
            refused = {"code": "preview_is_production", "message": "Vercel built the preview as production."}
            await _clear_pending(app, "preview_pending", {"error": {**refused, "next": "run up again"}})
        else:
            await _clear_pending(app, "preview_pending", {"error": await _failure(app, creds, pending, st)})
        return
    if st["readyState"] == "READY":
        await _patch(
            app,
            {
                "preview_pending": None,
                "preview": {
                    "deployment_id": pending["deployment_id"],
                    "commit": pending["commit"],
                    "source_id": pending.get("source_id"),
                    "ready_at": utc_now_iso(),
                },
                # Once protection is scoped (R51) a *.vercel.app url is closed: keep the custom one.
                **({} if app.get("protection_scoped") else {"preview_url": f"https://{st['url']}"}),
            },
        )
    elif st["readyState"] in ("ERROR", "CANCELED"):
        await _clear_pending(app, "preview_pending", {"error": await _failure(app, creds, pending, st)})


async def _check_publish(app: dict, creds: dict) -> None:
    pending = app.get("publish_pending")
    if not pending:
        return
    st = await vercel.deployment_state(creds, pending["deployment_id"])
    if st["readyState"] == "READY":
        if app.get("rolled_back"):
            # After an Instant Rollback Vercel stops auto-assigning production domains: promote first,
            # and record the version only once it is really the one serving.
            await vercel.promote(creds, pending["deployment_id"])
        await _production_moved(app, creds)  # before anything reports this version live
        version = {
            "commit": pending["commit"],
            "deployment_id": pending["deployment_id"],
            "source_id": pending["source_id"],
            "at": utc_now_iso(),
            "by": pending["by"],
            "live": True,
            # _push_env ran first on this copy, so this is what the deployment's env says (see rollback).
            "open_to": app["open_to"],
            "replaced": pending.get("replaced", app.get("live_version")),
        }
        fields = {
            "live_source_id": pending["source_id"],
            "published_migrations": sorted(
                set(app.get("published_migrations") or []) | set(pending.get("migrations") or [])
            ),
            "live_url": f"https://{apps_dns.hostname(app['name'])}",
            "state": "live",
            "rolled_back": False,
            "publish_error": None,
        }
        won = await apps_repo.record_release(
            app["app_id"],
            version,
            pending_deployment_id=pending["deployment_id"],
            known_versions=len(app.get("versions") or []),
            fields=fields,
        )
        # None: another read recorded it first (or a rollback cancelled it); carry on from the record.
        fresh = won or await apps_repo.get(app["app_id"], consistent_read=True)
        app.clear()
        app.update(plain(fresh))
        _notify(app)
    elif st["readyState"] in ("ERROR", "CANCELED"):
        err = {**await _failure(app, creds, pending, st), "next": "fix the error and publish again"}
        await _clear_pending(app, "publish_pending", {"publish_error": err})


async def _clear_pending(app: dict, field: str, fields: dict) -> None:
    # A publish/up may have written a newer pending deployment since this copy was read; keep it.
    if await apps_repo.clear_pending_if(app["app_id"], field, app[field]["deployment_id"], fields) is None:
        await _reload(app)
        return
    app.update({field: None, **fields})
    _notify(app)


async def _reload(app: dict) -> None:
    fresh = plain(await apps_repo.get(app["app_id"], consistent_read=True))
    app.clear()
    app.update(fresh)


def _replaced(version: dict) -> int | None:
    """The version live just before this one. An older record has no stamp: the number before it."""
    return version.get("replaced", version["n"] - 1)


def _previous_version(app: dict) -> dict | None:
    """What "put back the previous version" goes back to: the history, never ``live_version - 1``."""
    versions = app.get("versions") or []
    live = next((v for v in versions if v.get("n") == app.get("live_version")), None)
    return next((v for v in versions if v.get("n") == _replaced(live)), None) if live else None


def _preview_source(app: dict) -> str | None:
    """The source a READY preview shows. A preview from before its source was recorded: the last
    build is that preview when none is pending."""
    preview = app.get("preview")
    if not preview:
        return None
    return preview.get("source_id") or (
        None if app.get("preview_pending") else (app.get("preview_built") or {}).get("source_id")
    )


def _build_failed(st: dict) -> dict:
    return {
        "code": "build_failed",
        "message": st.get("errorMessage") or "The build failed.",
        "next": "fix the error and run up again",
    }


_LOG_TAIL = 2000
_LOG_WAIT_S = 5  # this runs on status reads, inside the 60 s lease: a hung Vercel must not hold them


async def _failure(app: dict, creds: dict, pending: dict, st: dict) -> dict:
    """``_build_failed`` plus the scrubbed tail of the build log. A log that cannot be fetched or scrubbed
    leaves the original error as it was."""
    err = {**_build_failed(st), "deployment_id": pending["deployment_id"], "at": utc_now_iso()}
    try:
        async with asyncio.timeout(_LOG_WAIT_S):
            events = await vercel.deployment_events(creds, pending["deployment_id"], limit=50)
            scrub = await _scrubber(app["owner_id"], app["app_id"])
        tail = scrub("\n".join(e["message"] for e in events))[-_LOG_TAIL:].strip()
    except Exception as e:
        if isinstance(e, vercel.VercelError) and _refused(e):
            raise  # a dead token: `_with_token` renews it and this step runs again
        logger.warning("apps: %s build log unavailable", app["app_id"], exc_info=True)
        return err
    return {**err, "message": f"{err['message']}\n{tail}"} if tail else err


async def _add_domain(app: dict, creds: dict, *, preview: bool) -> dict:
    host = apps_dns.preview_hostname(app["name"]) if preview else apps_dns.hostname(app["name"])
    r = await vercel.add_domain(creds, host)
    await apps_dns.point(app["name"], cname_target=r["cname"], verification=r["verification"], preview=preview)
    return {"name": host, "verified": bool(r["verified"])}


async def _verify(app: dict, creds: dict, field: str) -> None:
    domain = app.get(field)
    if domain and not domain.get("verified") and await vercel.verify_domain(creds, domain["name"]):
        await _patch(app, {field: {**domain, "verified": True}})


def _passed(at: str) -> bool:
    return datetime.fromisoformat(at) <= datetime.now(UTC)


def _host_watch(deployment_id: str, settle_until: str | None = None) -> dict:
    return {
        "deployment_id": deployment_id,
        "attempts": 0,
        "next_at": None,
        "settle_until": settle_until or utc_now_iso(),
    }


def _host_warning(app: dict) -> AppError | None:
    """Only once there is a live app: before that the preview host has nothing else it could be serving."""
    return app.get("preview_host_error") if app.get("live_version") else None


async def _production_moved(app: dict, creds: dict) -> None:
    """Vercel gives the preview host to every production deployment, as it is a project domain. Point it back now, and
    keep checking for a while: Vercel can assign production's domains after it reports the deployment READY."""
    ready, domain = app.get("preview"), app.get("preview_domain")
    if not ready or not (domain or {}).get("verified"):
        return  # no alias yet; _preview_alias makes the first one
    until = (datetime.now(UTC) + _REPOINT_SETTLE).isoformat()
    await _patch(app, {"preview_repoint": _host_watch(ready["deployment_id"], until), "preview_host_error": None})
    await _repoint(app, creds)


async def _repoint(app: dict, creds: dict) -> None:
    """One try at putting the preview host on the watched deployment. Failure is logged, shown, and retried a bounded
    number of times with backoff; it never stops a publish, a read or the live app. A refused token raises for renewal."""
    watch = app.get("preview_repoint")
    if not watch or (watch["next_at"] and not _passed(watch["next_at"])):
        return
    host, target = app["preview_domain"]["name"], watch["deployment_id"]
    try:
        if await vercel.alias_deployment(creds, host) != target:
            await vercel.assign_alias(creds, target, host)
    except vercel.VercelError as e:
        if _refused(e):
            raise
        attempts = int(watch["attempts"]) + 1
        logger.warning("apps: %s preview host not re-pointed (try %s): %s", app["app_id"], attempts, e.code)
        retry_at = datetime.now(UTC) + timedelta(minutes=2 ** (attempts - 1))
        await _patch(
            app,
            {
                "preview_alias_for": None,
                "preview_repoint": None
                if attempts >= _REPOINT_TRIES
                else {**watch, "attempts": attempts, "next_at": retry_at.isoformat()},
                "preview_host_error": {
                    "deployment_id": target,
                    "code": "preview_host_off",
                    "message": "The preview address may be showing the live app and its data.",
                    "next": "run up again; the live app is fine",
                },
            },
        )
        return
    fields = {"preview_alias_for": target, "preview_url": f"https://{host}", "preview_host_error": None}
    if _passed(watch["settle_until"]):
        fields["preview_repoint"] = None
    elif watch["attempts"]:
        fields["preview_repoint"] = {**watch, "attempts": 0, "next_at": None}
    if any(app.get(k) != v for k, v in fields.items()):
        await _patch(app, fields)


async def _preview_host(app: dict, creds: dict) -> None:
    """Runs before anything that can stop a step, and never stops one itself, except a refused token."""
    try:
        await _preview_alias(app, creds)
    except Exception as e:  # noqa: BLE001 - the preview's address must not block a build, a publish or a read
        if isinstance(e, vercel.VercelError) and _refused(e):
            raise
        logger.warning("apps: %s preview host step failed: %s", app["app_id"], getattr(e, "code", type(e).__name__))


async def _preview_alias(app: dict, creds: dict) -> None:
    """R27: the preview answers at {name}-preview.{APPS_DOMAIN} once Vercel has verified it."""
    ready = app.get("preview")
    if not ready:
        return
    if not app.get("preview_domain"):
        await _patch(app, {"preview_domain": await _add_domain(app, creds, preview=True)})
    else:
        await _verify(app, creds, "preview_domain")
    target, watch = ready["deployment_id"], app.get("preview_repoint")
    if app["preview_domain"]["verified"]:
        if watch and watch["deployment_id"] != target:  # a newer preview: fresh tries, same settling
            await _patch(app, {"preview_repoint": _host_watch(target, watch["settle_until"])})
        elif not watch and (
            # New preview, unless every try for this one failed: that waits for `up` or the next release.
            (
                app.get("preview_alias_for") != target
                and (app.get("preview_host_error") or {}).get("deployment_id") != target
            )
            # Published before releases re-pointed the host: look once, as it is probably on the live deployment.
            or ("preview_repoint" not in app and app.get("live_version"))
        ):
            await _patch(app, {"preview_repoint": _host_watch(target)})
        await _repoint(app, creds)
    if app.get("preview_alias_for") and not app.get("protection_scoped"):
        # R51: live and preview both answer on custom domains now; stale deployment urls close.
        await vercel.protect_except_custom_domains(creds)
        await _patch(app, {"protection_scoped": True})


async def _progress(app: dict) -> tuple[dict, AppError | None]:
    """One step on every pending thing. Returns the workflow rows and a transient error, if any."""
    rows = await _rows(app)
    moved = False
    for key, row in rows.items():
        if row.get("state") not in wf_repo.TERMINAL_STATES:
            rows[key] = await provisioning_workflow.advance_on_read(row["workflow_id"]) or row
            moved |= rows[key].get("state") != row.get("state")
    if moved:
        await _moved(app)
    creds = await _secret(app, rows, "vercel", "prod", "token")
    if not creds or any(r.get("state") in _BROKEN for r in rows.values()):
        return rows, None
    dev_db = await _secret(app, rows, "neon", "dev", "connection_string")
    live_db = await _secret(app, rows, "neon", "prod", "connection_string")
    # R56: one read at a time steps an app (no double build/reset); another read reports the record.
    lease = await apps_repo.take_lease(app["app_id"], seconds=_LEASE_S)
    await _reload(app)  # what the last holder left; without the lease, so the caller reports it, not an old copy
    if not lease:
        return rows, None
    try:
        return rows, await _steps(app, rows, creds, dev_db, live_db)
    finally:
        if await apps_repo.release_lease(app["app_id"], lease):
            app["progress_lease_until"] = None


_PREVIEW_REBUILDS = 2
_REPOINT_TRIES = 5  # 1, 2, 4 and 8 minutes apart, then it waits for `up` or the next release
_REPOINT_SETTLE = timedelta(minutes=3)  # how long after a release the host keeps being checked
_REFUSAL_CODE = "vercel_credential_refused"
_RENEWAL_HOLDS = frozenset({"pending", "errored"})  # no rotation until the token works or a person removes the field
_UNSETTLED = _RENEWAL_HOLDS | {"needs_reveal"}
_RENEW_EVERY = timedelta(minutes=10)  # tokens live one hour; this only stops one dying fast from looping
_ROTATION_WINDOW = timedelta(minutes=5)  # a younger pending is a rotation in flight, not yet anyone's problem
_SETTLE_S = 5  # a new token Vercel refuses gets one more look after this long before it counts as refused


def _refused(e: vercel.VercelError | None) -> bool:
    return e is not None and e.invalid_token


def _renewal_age(renewal: dict) -> timedelta:
    """How long since ``at``; unreadable counts as old."""
    try:
        at = datetime.fromisoformat(renewal["at"])
    except (KeyError, TypeError, ValueError):
        return timedelta.max
    return datetime.now(UTC) - (at if at.tzinfo else at.replace(tzinfo=UTC))


def _renewal_hold(app: dict) -> AppError | None:
    renewal = app.get("credential_renewal") or {}
    if renewal.get("state") not in _RENEWAL_HOLDS:
        return None
    if renewal["state"] == "pending" and _renewal_age(renewal) < _ROTATION_WINDOW:
        return None
    return {
        "code": "credential_renewal_stuck",
        "message": "Lightbulb tried to renew the app's hosting credential and the renewal didn't finish, "
        "so it won't try again on its own.",
        "next": "Lightbulb's team needs to look at this.",
    }


def _refusal(app: dict) -> AppError:
    return {
        "code": _REFUSAL_CODE,
        "message": "Vercel no longer accepts the app's hosting credential.",
        "next": "Lightbulb can't replace it on its own, so Lightbulb's team needs to look at this."
        if _renewal_hold(app)
        else "Lightbulb will try to replace it. Try again in a few minutes; "
        "if this keeps happening, Lightbulb's team needs to look at it.",
    }


async def _probe(app: dict, creds: dict) -> vercel.VercelError | None:
    """One read the token must be able to do. The failure, if any: only ``_refused`` of it means the token.
    Never decides a rotation; it clears what is stored and checks a new token."""
    known = app.get("publish_pending") or app.get("preview_pending") or app.get("preview") or {}
    try:
        await vercel.read_probe(creds, known.get("deployment_id"))
    except vercel.VercelError as e:
        return e
    return None


async def _store_revealed(app: dict, account: str, resource: str, stored: dict) -> dict | None:
    """Stripe's current token, stored, when it differs from ours. A reveal never rotates (Stripe's doc)."""
    config = (await api.reveal_access_configuration(account, resource)).get("configuration")
    config = {k: v for k, v in config.items() if isinstance(v, str) and v} if isinstance(config, dict) else {}
    if not config.get("token") or config["token"] == stored.get("token"):
        return None
    fresh = {**stored, **config}
    await integration_secret_store.put(app["owner_id"], f"apps/{app['app_id']}/vercel/prod", fresh)
    logger.info("apps: %s stored a new vercel credential from Stripe", app["app_id"])
    return fresh


async def _settle(app: dict, fresh: dict) -> dict | None:
    """A rotation's token is stored: ``complete`` if Vercel takes it. Refused twice is ``errored``, so a token
    refused at issue doesn't rotate again every ``_RENEW_EVERY``; the next read that finds it working clears that."""
    failure = await _probe(app, fresh)
    if _refused(failure):
        await asyncio.sleep(_SETTLE_S)
        failure = await _probe(app, fresh)
    done = "errored" if _refused(failure) else "complete"
    await _patch(app, {"credential_renewal": {"state": done, "at": utc_now_iso()}})
    return None if done == "errored" else fresh


async def _renew_vercel(app: dict, rows: dict, creds: dict) -> dict | None:
    """After Vercel refused ``creds``: the credentials to retry with, or None to report it. Ordinary: tokens live one
    hour and nothing refreshes them, so an app idle for longer comes through here at its next Vercel call.

    Reveal first; rotate only when Stripe still holds the refused token. The claim on ``credential_renewal``
    (compare-and-set on ``at``) is written as ``pending`` before the POST, so a crash, a timeout or a cancel leaves
    the outcome-unknown state. Stripe's doc: pending -> don't resend; errored -> stop; complete -> read the new
    configuration, which ``needs_reveal`` keeps owed. A changed token means the rotation landed, and settles any
    of them with nothing sent to Stripe. A rotation that never landed stays held: docs/runbooks/provisioning-requires-review.md.
    """
    await _reload(app)
    renewal = app.get("credential_renewal") or {}
    state = renewal.get("state")
    try:
        key = f"apps/{app['app_id']}/vercel/prod"
        stored = await integration_secret_store.get(app["owner_id"], key)
        if not stored:
            return None  # removed since this reader read it: never write it back
        if stored.get("token") and stored["token"] != creds["token"]:
            return stored  # another reader replaced it after this one read its copy
        account = await provisioning_workflow.account_id_for(app["owner_id"])
        resource = rows[("vercel", "project", "prod")]["resource_id"]
        fresh = await _store_revealed(app, account, resource, stored)
    except Exception as e:  # noqa: BLE001 - nothing was rotated; the refusal is reported and the next attempt reveals
        logger.warning("apps: %s vercel credential not read: %s", app["app_id"], type(e).__name__)
        return None
    if fresh:
        if state in _UNSETTLED:
            if _renewal_age(renewal) < _RENEW_EVERY or not _refused(await _probe(app, fresh)):
                await _patch(app, {"credential_renewal": {"state": "needs_reveal", "at": utc_now_iso()}})
                return await _settle(app, fresh)
            state = None  # that older rotation did land (the token changed), and its token has since expired
        elif not _refused(await _probe(app, fresh)):
            return fresh
        stored = fresh  # Stripe still holds a dead token, just not the one we had: rotate it
    # An old needs_reveal is an earlier rotation whose token has since died too.
    recent = state in ("complete", "needs_reveal") and _renewal_age(renewal) < _RENEW_EVERY
    if state in _RENEWAL_HOLDS or recent:
        return None
    claim = {"credential_renewal": {"state": "pending", "at": utc_now_iso()}}
    if await _patch(app, claim, expect={"credential_renewal.at": renewal.get("at")}) is None:
        return None  # another reader holds it; the next attempt finds what it stored
    try:
        status = (await api.rotate_credentials(account, resource)).get("status")
    except api.TransientProvisioningError:
        status = "pending"  # may have been decided: never re-POST
    except api.ProvisioningError:
        status = "errored"
    except Exception:  # noqa: BLE001 - unknown outcome
        status = "pending"
    logger.log(
        logging.INFO if status == "complete" else logging.WARNING,
        "apps: %s vercel credential rotation: %s",
        app["app_id"],
        status,
    )
    if status == "errored":
        await _patch(app, {"credential_renewal": {"state": "errored", "at": utc_now_iso()}})
    if status != "complete":
        return None
    await _patch(app, {"credential_renewal": {"state": "needs_reveal", "at": utc_now_iso()}})
    try:
        fresh = await _store_revealed(app, account, resource, stored)
    except Exception as e:  # noqa: BLE001 - the state says what is owed
        logger.warning("apps: %s rotated vercel credential not read: %s", app["app_id"], type(e).__name__)
        return None
    return await _settle(app, fresh) if fresh else None


async def _with_token[T](app: dict, rows: dict, creds: dict, op: Callable[[dict], Awaitable[T]]) -> T:
    """The one way to run Vercel operations: ``op(creds)``, and if Vercel says the token is dead, renew and run
    it once more. Raises what Vercel said last. ``test_every_vercel_credential_goes_through_with_token`` holds this."""
    try:
        return await op(creds)
    except vercel.VercelError as e:
        if not _refused(e):
            raise
        fresh = await _renew_vercel(app, rows, creds)
        if not fresh:
            raise
    return await op(fresh)


async def _with_token_for_route[T](app: dict, rows: dict, creds: dict, op: Callable[[dict], Awaitable[T]]) -> T:
    """``_with_token`` for a route with no error field: a token still dead is said in the person's words."""
    try:
        return await _with_token(app, rows, creds, op)
    except vercel.VercelError as e:
        if not _refused(e):
            raise
        err = _refusal(app)
        raise AppsRefused(err["code"], err["message"], err["next"]) from None


def _vercel_error(app: dict, e: vercel.VercelError, next_default: str) -> AppError:
    if _refused(e):
        return _refusal(app)
    return {"code": f"vercel_{e.code}", "message": str(e), "next": next_default}


async def _steps(app: dict, rows: dict, creds: dict, dev_db: dict | None, live_db: dict | None) -> AppError | None:
    try:
        return await _with_token(app, rows, creds, lambda c: _run_steps(app, c, dev_db, live_db))
    except vercel.VercelError as e:
        logger.warning("apps: %s vercel step failed: %s", app["app_id"], e.code)
        return _vercel_error(app, e, "run up again in a minute")
    except _WorkspaceUnknown:
        logger.warning("apps: %s workspace name unavailable", app["app_id"])
        return {
            "code": "workspace_unavailable",
            "message": "Couldn't read the workspace.",
            "next": "run up again in a minute",
        }


async def _run_steps(app: dict, creds: dict, dev_db: dict | None, live_db: dict | None) -> AppError | None:
    # A step may make no Vercel call at all, so what is stored about the token is checked by asking. A working token
    # clears it, with nothing sent to Stripe; a dead one goes to renewal, which picks up a rotation that has landed.
    renewal = app.get("credential_renewal") or {}
    refusal_stored = (app.get("publish_error") or {}).get("code") == _REFUSAL_CODE
    if refusal_stored or renewal.get("state") in _UNSETTLED:
        failure = await _probe(app, creds)
        if _refused(failure):
            raise failure
        if not failure:
            if refusal_stored:
                await _patch(app, {"publish_error": None}, expect={"publish_error.code": _REFUSAL_CODE})
            if renewal.get("state") in _UNSETTLED:
                settled = {"credential_renewal": {"state": "complete", "at": utc_now_iso()}}
                await _patch(app, settled, expect={"credential_renewal.at": renewal.get("at")})
    if not app.get("protection_off"):
        await vercel.disable_protection(creds)
        await _patch(app, {"protection_off": True})
    await _push_env(app, creds, dev_db, live_db)
    await _check_preview(app, creds)
    await _check_publish(app, creds)
    await _preview_host(app, creds)
    want = app.get("preview_want")
    wanted = _source(app, want) if want else {}
    if dev_db and want and app.get("preview_migrations") != wanted.get("migrations_key", wanted.get("migrations")):
        if app.get("error"):  # a failed reset/build waits for the next `up`, not every read
            return None
        migrations, seed = _migrations(await _files(app["owner_id"], app["app_id"], want))
        names = [n for n, _ in migrations]
        try:
            await apps_db.reset_preview(
                dev_db["connection_string"], migrations=[sql for _, sql in migrations], seed_sql=seed
            )
        except apps_db.AppsDbError as e:
            await _patch(
                app, {"error": {"code": e.code, "message": str(e), "next": "fix the migration and run up again"}}
            )
            return None
        await _patch(app, {"preview_migrations": wanted.get("migrations_key", names)})
    built = {"source_id": want, "sample": not dev_db}
    if want and app.get("preview_built") != built:
        dep_id = await _deploy(app, creds, want, production=False)
        commit = _source(app, want)["commit"]
        await _patch(
            app,
            {
                "preview_built": built,
                "error": None,
                "preview_pending": {"deployment_id": dep_id, "commit": commit, "source_id": want},
            },
        )
    await _verify(app, creds, "domain")
    return None


# --- results ------------------------------------------------------------------------------------


def _state(app: dict, rows: dict) -> tuple[str, AppError | None]:
    states = {k: r.get("state") for k, r in rows.items()}
    broken = [k for k, s in states.items() if s in _BROKEN | _DEAD]
    if broken:
        provider, service, env = broken[0]
        return "failed", {
            "code": "provisioning_failed",
            "message": f"{provider}/{service} ({'live' if env == 'prod' else 'preview'}) did not finish setting up.",
            "next": "open Inbox",
        }
    if app.get("error") and not app.get("live_version"):  # R63: a live app stays live; the error rides along
        return "failed", app["error"]
    if app.get("publish_error") and not app.get("live_version"):
        return "failed", app["publish_error"]
    if states.get(("vercel", "project", "prod")) != "active":  # the preview waits only on Vercel
        waiting = any(s in ("ask", "awaiting_approval") for s in states.values()) or len(rows) < len(_RESOURCES)
        return ("awaiting_approval" if waiting else "provisioning"), None
    if app.get("preview_pending") or app.get("publish_pending") or not app.get("preview"):
        return "building", _renewal_hold(app)
    # Only the starter's "Being built" page is deployed (the agent files the hosting card before it
    # builds): not ready until the real source is up, so nothing offers Publish on it and the watch
    # keeps running. A live app keeps its state; its live version is the real one.
    shown = _preview_source(app)
    if not app.get("live_version") and ((app.get("sources") or {}).get(shown) or {}).get("placeholder"):
        return "building", _renewal_hold(app)
    # A failed build leaves the live version serving.
    return "ready", app.get("error") or app.get("publish_error") or _host_warning(app) or _renewal_hold(app)


async def _record_state(app: dict, state: str) -> str:
    if app.get("state") == "requested" and not app.get("sources"):
        return "requested"  # reserved by `app start`; the first upload adopts it
    record = "live" if state == "ready" and app.get("live_version") else state
    if app.get("state") != record:
        # Only over the state this copy saw: a reader from before a release must not undo its "live".
        moved = await _patch(app, {"state": record}, expect={"state": app.get("state")})
        if moved and record in ("ready", "live"):
            await apps_room.post_origin_ready_line(app, _display(app), _canvas_url(app["app_id"]))
    return record


async def _result(app: dict, rows: dict, transient: AppError | None, cost_cents: int | None = None) -> UpResult:
    state, err = _state(app, rows)
    await _record_state(app, state)
    built = app.get("preview_built") or {}
    res: UpResult = {
        "app_id": app["app_id"],
        "state": state,
        "cards": [
            {
                "workflow_id": r["workflow_id"],
                "provider": r.get("provider_name"),
                "service": r.get("service_ref"),
                "state": r.get("state"),
            }
            for r in rows.values()
            if r.get("state") != "active" and not (r.get("state") == "awaiting_approval" and r.get("bundled_with"))
        ],
        "canvas_url": _canvas_url(app["app_id"]),
        "sample_data": bool(built.get("sample", True)),
    }
    for key in ("preview_url", "live_url"):
        if app.get(key):
            res[key] = app[key]
    if app.get("live_version"):
        res["version"] = int(app["live_version"])
    if cost_cents is not None:
        res["cost_cents"] = cost_cents
    if err or transient:
        # This attempt's failure first: a stale stored error must not hide why the new publish failed
        # (a Vercel 'forbidden' sat behind an old build error on Demo Lab, 30 Oct 2026).
        res["error"] = transient or err
    return res


# --- public -------------------------------------------------------------------------------------


async def put_source(
    owner_id: str,
    *,
    name: str,
    room_id: str | None = None,
    commit: str,
    archive: bytes,
    actor: BuilderCaller,
    placeholder: bool = False,
    base_commit: str | None = None,
) -> dict:
    """R32: the source upload creates the app record; ``up`` fills in the rest. ``base_commit``: the commit the
    upload was built on; one that is not the app's latest is refused (two writers). Absent: not checked."""
    _check_name(name)
    await _check_room(owner_id, room_id)
    app = await apps_repo.get_by_name(owner_id, name)
    if app is None:
        app = await apps_repo.create(
            owner_id=owner_id,
            name=name,
            room_id=room_id,
            created_by=actor.actor_id if actor.actor_kind == "person" else None,
            actor_id=actor.actor_id,
            actor_kind=actor.actor_kind,
            origin={"kind": "unknown"},
            services=[],
            open_to="room" if room_id else "workspace",
            budget_usd=0,
            display_name=_display({"name": name}),
        )
    if app.get("state") == "destroyed":
        raise AppsRefused("app_destroyed", f"{name} was removed.", "pick another name")
    latest = _latest_source(app)
    # ponytail: check-then-write, so two uploads in the same second can both pass; a conditional patch on
    # `sources` closes it if that ever happens.
    if base_commit and latest and latest[1]["commit"] != base_commit:
        raise AppsRefused(
            "stale_base",
            f"{_display(app)} changed since your copy: its latest version is {latest[1]['commit']}, yours is built"
            f" on {base_commit}.",
            "run lightbulb app pull, merge your change, then lightbulb app up",
        )
    if app.get("state") == "requested":  # reserved by `app start`: its builder's first upload takes it over
        adopted = {"state": "building", "actor_id": actor.actor_id, "actor_kind": actor.actor_kind}
        won = await apps_repo.patch(app["app_id"], adopted, expect={"state": "requested"})
        # Lost to another first upload: carry on with the record as that one left it.
        app = plain(won or await apps_repo.get(app["app_id"], consistent_read=True) or app)
    source_id = await apps_source.put(owner_id, app["app_id"], commit=commit, data=archive)
    files = await asyncio.to_thread(lambda: dict(apps_source.iter_files(archive)))
    names = [n for n, _ in _migrations(files)[0]]
    entry = {"commit": commit, "at": utc_now_iso(), "migrations": names, "migrations_key": _migrations_key(files)}
    if placeholder:
        entry["placeholder"] = True
    sources = {**(app.get("sources") or {}), source_id: entry}
    keep = {app.get("live_source_id"), app.get("preview_want"), (app.get("publish_pending") or {}).get("source_id")}
    recent = sorted(sources, key=lambda k: sources[k]["at"])[-10:]
    sources = {k: v for k, v in sources.items() if k in keep or k in recent}
    await apps_repo.patch(app["app_id"], {"sources": sources, **_progress_fields(app, actor.run_id)})
    watch(owner_id, app["app_id"])
    from core.services import apps_run  # it imports this module

    await apps_run.record_upload(app, actor.actor_id, actor.actor_kind, files=files)
    return {"app_id": app["app_id"], "source_id": source_id}


async def up(
    owner_id: str,
    *,
    name: str,
    room_id: str | None = None,
    services: list[str],
    open_to: str,
    budget_usd: int,
    source_id: str,
    actor: BuilderCaller,
    origin: dict,
    issue_id: str | None = None,
    approved_destructive: str | None = None,
    cost_cents: int | None = None,
    requested_by: str | None = None,
) -> UpResult:
    _check_name(name)
    await _check_room(owner_id, room_id)
    found = await apps_repo.get_by_name(owner_id, name)
    if not found:
        raise AppsRefused("source_not_found", "Upload the app's source first.", "upload the source, then run up")
    app = await _owned(owner_id, found["app_id"])
    _source(app, source_id)
    if app.get("preview_want") != source_id or app.get("error"):
        await _check_destructive(app, await _files(owner_id, app["app_id"], source_id), approved_destructive)
    fields: dict = {"preview_want": source_id}
    if not await _rows(app):  # R52: only a new app takes these from up; afterwards the owner's PATCH does
        # Built in its own conversation: the room stays the origin's.
        if room_id and room_id != app.get("conversation_id"):
            fields["room_id"] = room_id
        if open_to == "room" and not (fields.get("room_id") or app.get("room_id")):
            open_to = "workspace"  # no room to be open to: never an app nobody can open
        fields.update(services=services, open_to=open_to, budget_usd=budget_usd, origin=origin)
        # R73: an agent's app gets its human creator, believed only from a room that person is in.
        if requested_by and room_id and not app.get("created_by"):
            try:
                await messaging.require_room_member(owner_id, requested_by, room_id)
                fields["created_by"] = requested_by
            except messaging.ApiProblem:
                logger.info("apps: %s requested_by is not a member of its room; ignored", app["app_id"])
    if app.get("error"):  # "run up again" after a failure retries the build
        fields.update(error=None, preview_built=None)
    if app.get("preview_host_error"):  # and the preview host, when every try has failed
        fields["preview_host_error"] = None
    await _patch(app, {**fields, **_progress_fields(app, actor.run_id)})
    watch(owner_id, app["app_id"])
    await _ensure_workflows(app, issue_id)
    rows, transient = await _progress(app)
    return await _result(app, rows, transient, cost_cents)


# A person's words that ask for an app to go live. Bare "live" is left out: "the live database"
# is not a request.
_PUBLISH_ASK = re.compile(r"\b(publish|ship|launch|deploy|go live|make it live|put it live)\b", re.IGNORECASE)


async def _asked_to_publish(app: dict) -> bool:
    """A person asked for it to go live since the app was made, in the app's conversation or the room it
    came from. An agent may publish only then: going live is the person's call (Canvas's Publish button is
    theirs too). A relayed line is Lightbulb's (author "system"), never a person's ask."""
    since = str(app.get("created_at") or "")
    for room_id in _rooms(app):
        for m in await messaging_repo.list_messages_before(room_id, 10**18, 100):
            text = m.get("editedText") if int(m.get("messageRevision", 0) or 0) else m.get("text")
            if (
                (m.get("author") or {}).get("kind") == "human"
                and str(m.get("createdAt") or "") >= since
                and _PUBLISH_ASK.search(text or "")
            ):
                return True
    return False


async def publish(
    owner_id: str,
    app_id: str,
    *,
    source_id: str,
    actor: BuilderCaller,
    reason: str = "",
    approved_destructive: str | None = None,
    cost_cents: int | None = None,
) -> UpResult:
    app = await _owned(owner_id, app_id)
    src = _source(app, source_id)
    if actor.actor_kind == "agent" and not await _asked_to_publish(app):
        raise AppsRefused(
            "not_asked",
            "Nobody has asked for this app to go live in its conversation or the room it came from.",
            "post the preview line and end the turn; publish when someone says publish or ship it, or presses Publish",
        )
    await _moved(app, actor.run_id)
    rows, transient = await _progress(app)
    pending = app.get("publish_pending")
    if transient and transient["code"] == _REFUSAL_CODE and not pending:
        await _patch(app, {"publish_error": transient})  # not a blip: keep it visible; clears when the token works
    if (
        transient
        or (pending and pending["source_id"] == source_id)
        or (app.get("live_source_id") == source_id and not pending)
    ):
        return await _result(app, rows, transient, cost_cents)
    if any((rows.get(r) or {}).get("state") != "active" for r in _RESOURCES):
        raise AppsRefused("not_ready", "The app's services are not all set up yet.", "run up and wait for the owner")
    # A row can be active before its secret lands; the live app needs its database, so wait for it.
    live_db = await _secret(app, rows, "neon", "prod", "connection_string")
    if not live_db:
        raise AppsRefused("not_ready", "The live database is still being set up.", "try again in a minute")
    creds = await _secret(app, rows, "vercel", "prod", "token")
    files = await _files(owner_id, app_id, source_id)
    await _check_destructive(app, files, approved_destructive)
    # The live schema moves here, not in the Vercel build: new migrations run now, in order, in one
    # transaction, and are recorded as published before the deploy (a failed build must not re-run them).
    migrations, _ = _migrations(files)
    published = set(app.get("published_migrations") or [])
    new = [(n, sql) for n, sql in migrations if n not in published]
    if new:
        # A dead token must not fail the deploy after the live schema has already moved.
        async def usable(c: dict) -> dict:
            refusal = await _probe(app, c)
            if _refused(refusal):  # anything else (a deleted preview, a blip) is the deploy's to find
                raise refusal
            return c

        try:
            creds = await _with_token(app, rows, creds, usable)
        except vercel.VercelError as e:
            err = _vercel_error(app, e, "publish again")
            await _patch(app, {"publish_error": err})
            return await _result(app, rows, err, cost_cents)
        try:
            await apps_db.apply_live(live_db["connection_string"], migrations=[sql for _, sql in new])
        except apps_db.AppsDbError as e:
            return await _result(
                app,
                rows,
                {"code": e.code, "message": str(e), "next": "fix the migration and publish again"},
                cost_cents,
            )
        await _patch(app, {"published_migrations": sorted(published | {n for n, _ in new})})

    async def deploy(c: dict) -> str:
        if not app.get("domain"):
            await _patch(app, {"domain": await _add_domain(app, c, preview=False)})
        return await _deploy(app, c, source_id, production=True)

    try:
        dep_id = await _with_token(app, rows, creds, deploy)
    except vercel.VercelError as e:
        err = _vercel_error(app, e, "publish again")
        logger.warning("apps: %s publish failed: %s", app_id, e.code)
        await _patch(app, {"publish_error": err})  # Canvas shows the real reason, not an older build error
        return await _result(app, rows, err, cost_cents)
    pending = {
        "deployment_id": dep_id,
        "source_id": source_id,
        "commit": src["commit"],
        "by": actor.actor_id,
        "reason": reason,
        "migrations": [n for n, _ in _migrations(files)[0]],
    }
    await _patch(app, {"publish_pending": pending, "publish_error": None})
    return await _result(app, rows, None, cost_cents)


async def member_publish(owner_id: str, app_id: str, *, user_id: str) -> UpResult:
    """R69: Canvas publishes what the READY preview shows, as the person (the route checked R73)."""
    app = await _owned(owner_id, app_id)
    source_id = _preview_source(app)
    if not source_id:
        raise AppsRefused("not_ready", "The app has no ready preview to publish.", "wait for the preview")
    actor = BuilderCaller(owner_id=owner_id, actor_id=user_id, actor_kind="person")
    try:
        return await publish(owner_id, app_id, source_id=source_id, actor=actor)
    except AppsRefused as e:
        if e.code == "destructive_migration":
            e.next = "ask the builder to get the owner's approval"
        raise


async def rollback(owner_id: str, app_id: str, *, version: int | None, run_id: str | None = None) -> UpResult:
    """Code only: the live database is never touched. Holds the lease, so a read's late promote
    can't leave Vercel serving a different version than the record."""
    app = await _owned(owner_id, app_id)
    await _moved(app, run_id)
    rows = await _rows(app)
    creds = await _secret(app, rows, "vercel", "prod", "token")
    if not creds:
        raise AppsRefused("not_ready", "The app's Vercel project is not set up.")
    lease = await apps_repo.take_lease(app_id, seconds=_LEASE_S)
    if not lease:
        raise AppsRefused("busy", "The app is being updated right now.", "try again in a minute")
    try:
        await _reload(app)
        return await _with_token_for_route(app, rows, creds, lambda c: _put_back(app, rows, c, version))
    finally:
        await apps_repo.release_lease(app_id, lease)


async def _put_back(app: dict, rows: dict, creds: dict, version: int | None) -> UpResult:
    n = version if version is not None else (_previous_version(app) or {"n": 0})["n"]
    target = next((v for v in app.get("versions") or [] if v["n"] == n), None)
    if target is None:
        raise AppsRefused("version_not_found", f"There is no version {n}.", "pick a version from status")
    cancelled = " The publish in progress was cancelled." if app.get("publish_pending") else ""
    # R51: a deployment keeps the LIGHTBULB_OPEN_TO it was built with, and `public` is the door off. Serving
    # one built under other sharing would open or close the live app while the record says otherwise. No
    # stamp (older records): unknown, so different.
    rebuilding = target.get("open_to") != app["open_to"]
    if rebuilding:
        if target.get("source_id") not in (app.get("sources") or {}):
            raise AppsRefused(
                "version_unavailable",
                f"Version {n} was built with different sharing settings, and its source is no longer kept.",
                "publish a new version instead",
            )
        await _redeploy_live(app, creds, put_back=target)  # the current version serves until this lands
        await _patch(app, {"publish_error": None})
        said = f"Version {n} is being rebuilt with the current sharing settings. It goes live in a few minutes."
    else:
        await vercel.rollback(creds, target["deployment_id"])
        # The rollback wins over a publish in flight; `rolled_back` makes the next release promote itself.
        await _patch(
            app,
            {
                "live_version": n,
                "live_source_id": None,
                "rolled_back": True,
                "publish_pending": None,
                "publish_error": None,
            },
        )
        try:
            await _production_moved(app, creds)
        except vercel.VercelError as e:
            # Not raised for renewal: a rerun of this function would go back one version further. The watch is stored,
            # so the sweeper's next step retries through renewal.
            logger.warning("apps: %s preview host not re-pointed after rollback: %s", app["app_id"], e.code)
        said = f"Version {n} is live again."
    res = await _result(app, rows, None)
    res["error"] = {
        "code": "code_only",
        "message": said + cancelled,
        "next": "Data is unchanged. To restore data, use Neon's own restore on the company's account "
        "(Canvas links to it).",
    }
    if rebuilding:
        res["rebuilding"] = True
    return res


async def destroy(owner_id: str, app_id: str, *, actor: BuilderCaller) -> dict:
    """A builder can only ask; the owner removes the app from Canvas."""
    app = await _owned(owner_id, app_id)
    await _patch(app, {"destroy_requested_by": actor.actor_id})
    return {"state": "awaiting_owner", "next": "the owner removes it from Canvas"}


async def remove(owner_id: str, app_id: str, *, confirm_name: str, user_id: str) -> dict:
    """R10: deprovision every resource, then drop our copies. Each step is safe to retry."""
    app = await apps_repo.get(app_id, consistent_read=True)
    if not app or app.get("owner_id") != owner_id:
        raise AppsRefused("app_not_found", "That app does not exist.")
    # Canvas types the display name; the CLI's contract, the slug, keeps working.
    if confirm_name.strip().casefold() not in (app["name"].casefold(), _display(app).casefold()):
        raise AppsRefused("confirm_name_mismatch", f'Type "{_display(app)}" to confirm.')
    rows = [r for r in await wf_repo.list_for_owner(owner_id) if r.get("app_id") == app_id]
    busy = AppsRefused("busy", "A resource for this app is being set up right now.", "try again in a minute")
    open_rows = [r for r in rows if r.get("state") not in wf_repo.TERMINAL_STATES]
    # R54: a row mid-create can't be withdrawn; removing now would orphan what it creates next.
    if any(
        r.get("resource_id")
        or r.get("provider_connection_request_id")
        or r.get("state") not in wf_repo.CANCELLABLE_STATES
        for r in open_rows
    ):
        raise busy
    for r in open_rows:  # an unanswered card must not provision for a removed app
        try:
            await provisioning_workflow.cancel(r["workflow_id"])
        except provisioning_workflow.WorkflowRefused:
            raise busy from None
    for r in rows:
        if r.get("resource_id") and r.get("resource_status") != "removed":
            await api.remove_resource(await provisioning_workflow.account_id_for(owner_id), r["resource_id"])
            secret = f"apps/{app_id}/{r['provider_name'].lower()}/{r.get('environment') or 'prod'}"
            await integration_secret_store.delete(owner_id, secret)
            await wf_repo.patch(r["workflow_id"], {"resource_status": "removed", "removed_at": utc_now_iso()})
    await apps_dns.remove(app["name"])
    await apps_source.delete_all(owner_id, app_id)
    await integration_secret_store.delete(owner_id, f"apps/{app_id}/lightbulb")
    await apps_repo.patch(app_id, {"state": "destroyed", "destroyed_by": user_id})
    return {"state": "destroyed"}


def _view(app: dict, rows: dict, state: str, agent_working: bool = False) -> AppStatus:
    preview = app.get("preview")
    return {
        "app_id": app["app_id"],
        "name": app["name"],
        "display_name": _display(app),
        "room_id": app.get("room_id"),
        "conversation_id": app.get("conversation_id"),
        "state": state,
        "open_to": app["open_to"],
        "budget_usd": app["budget_usd"],
        "preview_url": app.get("preview_url"),
        "live_url": app.get("live_url"),
        "canvas_url": _canvas_url(app["app_id"]),
        "live_version": app.get("live_version"),
        "origin": app.get("origin") or {},
        "versions": [
            {k: v[k] for k in ("n", "commit", "deployment_id", "at", "by")} for v in app.get("versions") or []
        ],
        "resources": [
            {
                "provider": r.get("provider_name"),
                "service": r.get("service_ref"),
                "state": r.get("state"),
                "resource_id": r.get("resource_id"),
                "environment": r.get("environment"),
            }
            for r in rows.values()
        ],
        "last_opened_at": app.get("last_opened_at"),
        "spend_month_cents": None,  # R9: no data source yet
        "preview": (
            {
                **{k: preview[k] for k in ("deployment_id", "commit", "ready_at")},
                "url": app.get("preview_url"),  # R75: the alias host once assigned, else the deployment url
            }
            if preview
            else None
        ),
        "stuck": app.get("stuck"),
        "agent_working": agent_working,
    }


def _moment(app: dict) -> tuple:
    """What a read can move on the record: a deployment started or finished, or the state changed."""
    return (
        app.get("state"),
        (app.get("preview_pending") or {}).get("deployment_id"),
        (app.get("publish_pending") or {}).get("deployment_id"),
        (app.get("preview") or {}).get("deployment_id"),
        app.get("live_version"),
        bool(app.get("error")),
        bool(app.get("publish_error")),
    )


async def status(owner_id: str, app_id: str) -> AppStatus:
    """A read. It stamps progress only when a step really moved on it, so Canvas polling never hides a stuck app."""
    app = await _owned(owner_id, app_id)
    before = _moment(app)
    rows, _ = await _progress(app)
    state, err = _state(app, rows)
    record = await _record_state(app, state)
    if _moment(app) != before:
        await _moved(app)
    elif record not in _DRIVE_STOP:
        watch(owner_id, app_id)  # after a restart, the next read re-arms the watch
    view = _view(app, rows, record, await _agent_working(owner_id, app, record))
    if err:
        view["error"] = err
    return view


# Stripe's Provisioning guide: poll after 1, 2, 4, 8, 16, 30 s, then every 30 s, for up to 15 minutes.
_DRIVE_WAITS = (1, 2, 4, 8, 16, 30)
_DRIVE_TOTAL_S = 15 * 60
_DRIVE_STOP = {"ready", "live", "failed", "destroyed"}
_drives: dict[str, asyncio.Task] = {}


def _alive(task: asyncio.Task | None) -> bool:
    """A finished task, or one from a loop that is not running this call, no longer holds the app's slot."""
    return task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop()


def drive(owner_id: str, app_id: str) -> None:
    """After an approval, step the app on the server until its preview is up.

    Steps otherwise happen only on reads, and the agent that filed the card early is busy building,
    so an approved app sat in ``connecting_provider`` with nobody reading (run 7, 2 Nov 2026).
    Reads still work as before; this only stops the app waiting for one. One drive per app.
    """
    if _alive(_drives.get(app_id)):
        return
    task = asyncio.create_task(_drive(owner_id, app_id))
    _drives[app_id] = task
    task.add_done_callback(lambda t: _drives.pop(app_id, None) if _drives.get(app_id) is t else None)


async def _drive(owner_id: str, app_id: str) -> None:
    # ponytail: in-process; a deploy that restarts the task drops the drive and the next read resumes it.
    elapsed = 0.0
    for i in itertools.count():
        wait = _DRIVE_WAITS[min(i, len(_DRIVE_WAITS) - 1)]
        elapsed += wait
        if elapsed > _DRIVE_TOTAL_S:
            return
        await asyncio.sleep(wait * random.uniform(0.9, 1.1))  # ±10 % jitter
        try:
            view = await status(owner_id, app_id)
        except Exception:
            logger.exception("apps: drive stopped for %s", app_id)
            return
        if view["state"] in _DRIVE_STOP:
            return


# --- the app's own run: progress, linked runs, stuck --------------------------------------------

_WATCH_EVERY_S = 60
_WATCH_TOTAL_S = 3 * 3600
_STUCK_S = 5 * 60
_STUCK_NO_RUNS_S = 20 * 60  # an old CLI links no runs, so a quiet app may still have an agent coding on it
_STUCK_WAITING_S = 45 * 60  # its last run finished cleanly: the agent chose to wait (it delegated, say)
_RUN_FRESH = timedelta(minutes=5)
_RUN_IDS_KEPT = 10
_watches: dict[str, asyncio.Task] = {}


def _progress_fields(app: dict, run_id: str | None = None) -> dict:
    """The app moved: stamp it, clear ``stuck``, and link the agent run that moved it (newest last)."""
    fields: dict = {"progress_at": utc_now_iso(), "stuck": None}
    runs = list(app.get("run_ids") or [])
    if run_id and runs[-1:] != [run_id]:
        fields["run_ids"] = [*(r for r in runs if r != run_id), run_id][-_RUN_IDS_KEPT:]
    return fields


async def _moved(app: dict, run_id: str | None = None) -> None:
    await _patch(app, _progress_fields(app, run_id))
    watch(app["owner_id"], app["app_id"])


def watch(owner_id: str, app_id: str) -> None:
    """Look at the app every minute until it is done, and say once in its room when it has stopped moving.
    One watch per app, like ``drive``."""
    if _alive(_watches.get(app_id)):
        return
    task = asyncio.create_task(_watch(owner_id, app_id))
    _watches[app_id] = task
    task.add_done_callback(lambda t: _watches.pop(app_id, None) if _watches.get(app_id) is t else None)


async def _watch(owner_id: str, app_id: str) -> None:
    # ponytail: in-process; a restart drops the watch and the next read or progress re-arms it.
    for _ in range(_WATCH_TOTAL_S // _WATCH_EVERY_S):
        await asyncio.sleep(_WATCH_EVERY_S)
        try:
            if await _look(owner_id, app_id):
                return
        except Exception:
            logger.exception("apps: watch stopped for %s", app_id)
            return


def _working(run: dict | None, now: datetime) -> bool:
    """An agent run still open whose last heartbeat (or dispatch, before its first one) is under 5 minutes old."""
    if not run or run.get("status") == "ended":
        return False
    return _fresh(run.get("lastHeartbeatAt") or run.get("dispatchedAt"), now, _RUN_FRESH)


def _fresh(stamp: object, now: datetime, within: timedelta) -> bool:
    try:
        return now - datetime.fromisoformat(stamp) < within  # type: ignore[arg-type]
    except (TypeError, ValueError):  # missing, malformed or naive: not a time anyone can trust
        return False


async def _agent_working(owner_id: str, app: dict, state: str) -> bool:
    """Whether an agent is still improving a ready first preview: one read of the newest linked run.
    Never over the starter placeholder (state "building") or a live app's next version."""
    run_ids = app.get("run_ids") or []
    if state != "ready" or app.get("live_version") or not app.get("preview") or not run_ids:
        return False
    return _working(await messaging_repo.get_run(owner_id, run_ids[-1]), datetime.now(UTC))


async def _look(owner_id: str, app_id: str) -> bool:
    """One look. True when there is nothing left to watch: done, removed, or already said stuck."""
    # Step it first, like any read: a build Vercel finished or a row an approval moved is progress, not silence.
    try:
        await status(owner_id, app_id)
    except AppsRefused:
        return True  # removed
    except Exception:  # noqa: BLE001 — a blip in the step is not a judgement; look again next minute
        logger.warning("apps: watch step failed for %s", app_id, exc_info=True)
        return False
    found = await apps_repo.get(app_id, consistent_read=True)
    if not found or found.get("owner_id") != owner_id or found.get("stuck"):
        return True
    app = plain(found)
    rows = await _rows(app)
    state, _ = _state(app, rows)
    if app.get("state") in _DRIVE_STOP or state in _DRIVE_STOP:
        return True
    # Waiting on a person is never stuck. No rows at all: `up` never ran, so nobody has been asked.
    if (state == "awaiting_approval" and rows) or any(r.get("state") == "awaiting_user_action" for r in rows.values()):
        return False
    # A deployment still pending after that step is one Vercel reports queued or building: the server is working.
    if app.get("preview_pending") or app.get("publish_pending"):
        return False
    now = datetime.now(UTC)
    # Stripe still setting a resource up, and the row changed within the drive's window: the server is working.
    setting_up = wf_repo.TERMINAL_STATES | wf_repo.CANCELLABLE_STATES  # the rest wait on a person or are done
    if any(
        r.get("state") not in setting_up and _fresh(r.get("updated_at"), now, timedelta(seconds=_DRIVE_TOTAL_S))
        for r in rows.values()
    ):
        return False
    since = app.get("progress_at") or app.get("updated_at") or app.get("created_at")
    run_ids = app.get("run_ids") or []
    newest = await messaging_repo.get_run(owner_id, run_ids[-1]) if run_ids else None
    if not run_ids:
        quiet = _STUCK_NO_RUNS_S
    elif newest and newest.get("status") == "ended" and newest.get("outcome") == "completed":
        quiet = _STUCK_WAITING_S
    else:
        quiet = _STUCK_S
    if not since or (now - datetime.fromisoformat(since)).total_seconds() < quiet:
        return False
    if _working(newest, now):
        return False
    for run_id in run_ids[:-1]:
        if _working(await messaging_repo.get_run(owner_id, run_id), now):
            return False
    # Any run where it is built (one the app never linked) still beating.
    for room_id in _build_rooms(app):
        if any(_working(r, now) for r in await messaging_repo.list_open_runs_for_room(owner_id, room_id)):
            return False
    step = "setting up hosting" if state == "provisioning" else "building"  # a pending deploy is never stuck
    stuck = {"at": utc_now_iso(), "step": step}
    # Conditional: of two instances (a rolling deploy), one marks it and posts; a new progress since wins.
    if await _patch(app, {"stuck": stuck}, expect={"stuck": None, "progress_at": app.get("progress_at")}) is None:
        return False  # another instance marked it, or it moved: the next look sees which
    await apps_room.post_stuck_line(app, _display(app), step)
    return True


async def update_settings(
    owner_id: str,
    app_id: str,
    *,
    open_to: str | None = None,
    budget_usd: int | None = None,
    display_name: str | None = None,
) -> AppStatus:
    """R36: the budget is only stored (the next approval's provider limit uses it). A new ``open_to``
    reaches Vercel on this same read: the env fingerprint changes, so ``_push_env`` re-pushes."""
    app = await _owned(owner_id, app_id)
    _check_open_to(open_to, app.get("origin_room_id") or app.get("room_id"))
    fields = {
        k: v
        for k, v in (("open_to", open_to), ("budget_usd", budget_usd), ("display_name", display_name))
        if v is not None
    }
    if fields:
        await _patch(app, {**fields, **_progress_fields(app)})
        watch(owner_id, app_id)
    if open_to is not None:
        from core.services import app_conversation  # it imports this module

        await app_conversation.unseat_who_cannot_open({**app, "open_to": open_to})
    return await status(owner_id, app_id)


async def list_apps(owner_id: str, auth: AuthContext) -> list[dict]:
    """Canvas's rows: the record as stored (no provider calls), names resolved here, destroyed apps hidden."""
    # By workspace: every member sees its apps, except one shared with its managers only.
    apps = [
        a
        for a in (plain(x) for x in await apps_repo.list_for_owner(owner_id))
        if a.get("state") != "destroyed" and (a["open_to"] != "me" or can_manage(a, auth))
    ]
    if not apps:
        return []
    all_rows = await wf_repo.list_for_owner(owner_id)
    room_ids = sorted({a["room_id"] for a in apps if a.get("room_id")})
    metas = dict(zip(room_ids, await asyncio.gather(*(messaging_repo.get_room(r) for r in room_ids)), strict=True))
    live = {r: m for r, m in metas.items() if m and not m.get("deletedAt")}
    member = dict(
        zip(
            live,
            await asyncio.gather(
                *(messaging.still_reads_room(owner_id, auth.user_id, r, m["ownerId"]) for r, m in live.items())
            ),
            strict=True,
        )
    )
    people = sorted({a["created_by"] for a in apps if a.get("created_by")})
    names = dict(zip(people, await asyncio.gather(*(messaging.human_display_name(u) for u in people)), strict=True))
    agents: dict[str, str] = {}
    if any(a.get("actor_kind") == "agent" for a in apps):
        try:
            agents = {r["id"]: r["name"] for r in await messaging.load_agent_roster(owner_id)}
        except messaging.ApiProblem:
            logger.warning("apps: roster unavailable; agent names left empty")
    out = []
    for a in apps:
        state = a.get("state") or ""
        row = _view(a, await _rows(a, all_rows), state, await _agent_working(owner_id, a, state))
        del row["versions"]
        previous = _previous_version(a)
        shows = _preview_source(a)
        serving = next((v for v in a.get("versions") or [] if v.get("n") == a.get("live_version")), {})
        uid = a.get("created_by")
        row.update(
            can_open=_can_open(a, auth, bool(member.get(a.get("room_id")))),
            # Only a room the caller is in is named to them.
            room_name=(live.get(a.get("room_id")) or {}).get("name") if member.get(a.get("room_id")) else None,
            created_by_name=names.get(uid) if uid and names.get(uid) != uid else None,
            agent_name=agents.get(a.get("actor_id")) if a.get("actor_kind") == "agent" else None,
            # R73: who may publish (and manage); whether there is a ready preview is the `preview` field.
            can_publish=can_manage(a, auth),
            # R71: the last persisted failure, message only.
            error={"message": err.get("message", "")}
            if (err := a.get("publish_error") or a.get("error") or _host_warning(a) or _renewal_hold(a))
            else None,
            # A live app's state stays "live" through a failed publish (R63), so Canvas needs this to see it.
            publish_failed=bool(a.get("publish_error")),
            # A publish in flight, the person's or an agent's: putting back cancels it.
            publishing=bool(a.get("publish_pending")),
            # Whether Publish has anything to do on a live app: pressing it on what is live changes nothing.
            preview_newer=bool(shows) and shows != (a.get("live_source_id") or serving.get("source_id")),
            # What "Put back the previous version" goes back to, and whether that takes a rebuild (see rollback).
            previous_version=(
                {"n": previous["n"], "at": previous["at"], "rebuild": previous.get("open_to") != a["open_to"]}
                if previous
                else None
            ),
        )
        out.append(row)
    return out


async def _scrubber(owner_id: str, app_id: str) -> Callable[[str], str]:
    """Replaces every app secret and any database URL with ``[redacted]``, for text that leaves the service."""
    keys = [f"apps/{app_id}/{p}/{e}" for p, _, e in _RESOURCES] + [f"apps/{app_id}/lightbulb"]
    blobs = await asyncio.gather(*(integration_secret_store.get(owner_id, k) for k in keys))
    values = {v for b in blobs if b for v in b.values() if isinstance(v, str) and len(v) >= 8}
    secret_re = re.compile("|".join(re.escape(v) for v in sorted(values, key=len, reverse=True)) or r"(?!)")
    return lambda text: _DB_URL.sub("[redacted]", secret_re.sub("[redacted]", text))


async def errors(owner_id: str, app_id: str) -> dict:
    """R25: the last 50 browser (client) and server lines of the newest failed build, else the live deployment,
    else the latest preview; build events when there are no runtime logs. Every app secret and any database URL is
    replaced with ``[redacted]``."""
    app = await _owned(owner_id, app_id)
    rows = await _rows(app)
    creds = await _secret(app, rows, "vercel", "prod", "token")
    live = next((v for v in app.get("versions") or [] if v.get("n") == app.get("live_version")), None)
    # A failed build is the newest attempt: `up` clears `error`, a good publish clears `publish_error`.
    # The `ready_at` of a newer healthy preview retires an older failed publish.
    pub = app.get("publish_error") or {}
    stale = (app.get("preview") or {}).get("ready_at", "") > pub.get("at", "~")
    failed = (app.get("error") or {}).get("deployment_id") or (None if stale else pub.get("deployment_id"))
    dep = failed or (live or app.get("preview") or {}).get("deployment_id")
    if not creds or not dep:
        return {"client": [], "server": []}

    async def read(c: dict) -> tuple[list[dict], list[dict]]:
        lines = await vercel.runtime_logs(c, dep, limit=200)
        return lines, [] if lines else await vercel.deployment_events(c, dep, limit=50)

    lines, events = await _with_token_for_route(app, rows, creds, read)
    client = [x for x in lines if any(m in x["message"] for m in _CLIENT_MARKS)]
    server = [x for x in lines if x not in client] if lines else events
    scrub = await _scrubber(owner_id, app_id)

    def clean(xs: list[dict]) -> list[dict]:
        return [{"at": x.get("at"), "message": scrub(x["message"])} for x in xs[-50:]]

    return {"client": clean(client), "server": clean(server)}


# --- opening an app (Task 14) -------------------------------------------------------------------

INVITE_LIFE = timedelta(days=14)
TEST_PASS_S = 300  # to open the link; the door's session then gives a builder 30 minutes to walk it


async def open_app(owner_id: str, app_id: str, *, auth: AuthContext, target: str = "live") -> str:
    """The URL that opens the app for this member: with a fresh 15-minute pass, except a public app
    (its door is off). Membership is checked now, at mint time, never cached."""
    app = await _owned(owner_id, app_id)
    url = app.get("live_url") if target == "live" else app.get("preview_url")
    if not url:
        if target == "live":
            raise AppsRefused("not_live", "The app is not published yet.", "publish it first")
        raise AppsRefused("not_ready", "The app has no preview yet.", "wait for the preview build")
    url = url.rstrip("/")
    if not _can_open(app, auth, False):
        in_room = False
        if app["open_to"] in ("room", "invited") and app.get("room_id"):
            try:
                await messaging.require_room_member(owner_id, auth.user_id, app["room_id"])
                in_room = True
            except messaging.ApiProblem:
                pass
        if not in_room:
            # The room is not named: Canvas never names a room the caller is not in.
            if app["open_to"] == "room":
                raise AppsRefused(
                    "not_a_member", "Only members of the app's room can open it.", "ask in the app's room"
                )
            raise AppsRefused("not_allowed", "This app is not shared with you.", "ask its creator to share it")
    # "workspace": the route's get_current_user + resolve_owner_id already made the caller an org member.
    await _patch(app, {"last_opened_at": utc_now_iso()})
    if app["open_to"] == "public":
        return url
    name = await messaging.human_display_name(auth.user_id)
    email = auth.email or ""
    if name == auth.user_id and email:
        name = email
    token = network_proof.mint_app_pass(
        auth.user_id, name=name, email=email, ws=owner_id, room=app.get("room_id") or "", app=app["name"]
    )
    return f"{url}/_lightbulb/session#token={token}"


async def test_link(owner_id: str, app_id: str, *, actor: BuilderCaller) -> str:
    """The URL that lets the builder and its reviewer walk the preview, never live. The pass's ``app``
    claim is ``_preview_app``, which only the preview's door accepts (``_push_env``): the live door, and
    a door from before this claim, refuse it. Not an open: ``last_opened_at`` stays a person's."""
    app = await _owned(owner_id, app_id)
    url = (app.get("preview_url") or "").rstrip("/")
    if not url:
        raise AppsRefused("not_ready", "The app has no preview yet.", "wait for the preview build")
    if app["open_to"] == "public":
        return url
    logger.info("apps: test link app=%s actor=%s kind=%s", app_id, actor.actor_id, actor.actor_kind)
    token = network_proof.mint_app_pass(
        f"builder:{actor.actor_id}",
        name="Builder agent",
        email="",
        ws=owner_id,
        room=app.get("room_id") or "",
        app=_preview_app(app),
        life_s=TEST_PASS_S,
    )
    return f"{url}/_lightbulb/session#token={token}"


async def reset_preview_data(owner_id: str, app_id: str) -> dict:
    """``app verify`` sends the preview's forms with test values; this takes them back out. The preview
    database goes back to its migrated schema and seed through the step ``up`` uses: forget which
    migrations it holds, then step. ``reset`` is False when this read could not step (another read holds
    the lease, the app has an error, no database yet); the forgotten key makes the next step reset it."""
    app = await _owned(owner_id, app_id)
    await _patch(app, {"preview_migrations": None})
    await _progress(app)
    return {"reset": bool(app.get("preview_migrations"))}


async def invite(owner_id: str, app_id: str, *, emails: list[str], user_id: str) -> dict:
    """One single-use, 14-day link per email, to this app only; a room or only-me app then becomes "invited"
    (``update_settings`` re-pushes LIGHTBULB_OPEN_TO and redeploys). R48: a public app needs no pass, so
    its invitees get the plain address and nothing is stored."""
    app = await _owned(owner_id, app_id)
    base = (app.get("live_url") or app.get("preview_url") or "").rstrip("/")
    if not base:
        raise AppsRefused("not_ready", "The app has no address yet.", "wait for the preview build")
    public = app["open_to"] == "public"
    expires_at = (datetime.now(UTC) + INVITE_LIFE).isoformat()
    links = {}
    for email in emails:
        if public:
            links[email] = base
            continue
        token = _random.token_urlsafe(32)
        await apps_repo.put_invite(
            hashlib.sha256(token.encode()).hexdigest(),
            target_app_id=app_id,
            invite_owner_id=owner_id,
            email=email,
            expires_at=expires_at,
            created_by=user_id,
        )
        links[email] = f"{base}/_lightbulb/invite#t={token}"
    if app["open_to"] in ("room", "me"):  # R59: additive; only once the links exist. A workspace app stays workspace.
        await update_settings(owner_id, app_id, open_to="invited")
        app["open_to"] = "invited"
    once = "" if public else " The link works once and expires in 14 days."
    shown = _display(app)
    for email, link in links.items():
        # send_email never raises; a failed send still counts (the admin can invite again).
        await email_service.send_email(
            to=email,
            subject=f"You're invited to {shown}",
            html=f'<p>You\'re invited to open {escape(shown)}.</p><p><a href="{link}">Open {escape(shown)}</a></p>'
            + (f"<p>{once.strip()}</p>" if once else ""),
            text=f"You're invited to open {shown}.\n\n{link}\n{once}".rstrip() + "\n",
            from_addr=settings.EMAIL_FROM_WELCOME,
            kind="apps_invite",
        )
    return {"invited": len(emails), "open_to": app["open_to"]}


async def list_invites(owner_id: str, app_id: str) -> list[str]:
    """Who still holds an unexpired invite to this app. Emails only: hashes never leave the service."""
    await _owned(owner_id, app_id)
    now = utc_now_iso()
    rows = await apps_repo.list_invites(app_id)
    return sorted({r["email"] for r in rows if r.get("invite_owner_id") == owner_id and r["expires_at"] > now})


async def revoke_invite(owner_id: str, app_id: str, *, email: str) -> dict:
    """Deletes every invite this email holds to this app, so each of its links fails like one never issued."""
    await _owned(owner_id, app_id)
    rows = await apps_repo.list_invites(app_id)
    doomed = [r["app_id"] for r in rows if r.get("invite_owner_id") == owner_id and r["email"] == email]
    await asyncio.gather(*(apps_repo.delete_invite(key) for key in doomed))
    return {"revoked": len(doomed)}


async def ask_agent_to_fix(owner_id: str, app_id: str, *, auth: AuthContext, error: str | None = None) -> dict:
    """One message in the app's conversation (else, an older app, its room), as the person, asking its
    builder agent to fix a failed publish or to pick up an app that stopped moving.
    ``error``: a refusal Canvas saw that was never stored; the person could post it themselves anyway."""
    app = await _owned(owner_id, app_id)
    # A provisioning failure is worked out from the rows, never stored.
    stored = app.get("publish_error") or app.get("error") or _state(app, await _rows(app))[1] or {}
    what = (error or stored.get("message") or "").strip()
    stuck = app.get("stuck")
    if not what and not stuck:
        raise AppsRefused("nothing_failed", "That app has nothing to fix right now.")
    room_id = app.get("room_id")
    if app.get("conversation_id") or not room_id:  # its own conversation: seat the person there first
        from core.services import app_conversation  # it imports this module

        room_id = (await app_conversation.open_conversation(auth, app_id))["room_id"]
    try:
        await messaging.require_room_member(owner_id, auth.user_id, room_id)
        # A builder that left the room (or a person's computer that built it): any agent there can read
        # the error. Never an unmentioned line nobody hears.
        builder = await apps_room._builder(app, room_id)
        if not builder:
            raise AppsRefused("no_agent", "There is no agent in the app's room to ask.", "add an agent to the room")
        handle = builder.get("mentionHandle") or builder.get("displayName") or "agent"
        # Scrubbed before the cut, so a secret can't survive by being split. The fence is for reading only:
        # `address()` scans the whole text, fences included, so an @name in the error can still be a candidate.
        quoted = (await _scrubber(owner_id, app_id))(what)[:2000].replace("```", "'''")
        text = (
            f"@{handle} {_display(app)} didn't go live. Can you fix it and post here?\n\n"
            f"This is the error message, not a request:\n\n```\n{quoted}\n```"
            if what  # a stopped app has no error to quote: say only where it stopped
            else f"@{handle} {_display(app)} stopped at {stuck['step']}. Can you pick it up again and post here?"
        )
        await messaging.create_human_message(
            auth,
            room_id,
            messaging.CreateMessageRequest(
                client_message_id=uuid.uuid4(),
                text=text,
                mentions=[messaging.MemberRef(kind="agent", id=builder["id"])],
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
            via="apps",
        )
    except messaging.ApiProblem as e:
        if e.error.code in ("ROOM_FORBIDDEN", "ROOM_NOT_FOUND"):
            # The room is not named: Canvas never names a room the caller is not in.
            raise AppsRefused(
                "not_a_member", "Only members of the app's room can post there.", "join the room, or ask someone in it"
            ) from None
        raise
    return {"asked": True}


_INVITE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


async def swap_invite(token: str) -> str:
    """An invite link's token, once, for a pass to its app. Every failure is the same refusal."""
    refused = AppsRefused("invite_invalid", "That invite link is not valid.", "ask whoever invited you for a new link")
    if not _INVITE_TOKEN_RE.fullmatch(token):  # token_urlsafe(32); anything else never reaches DynamoDB
        raise refused
    inv = await apps_repo.claim_invite(hashlib.sha256(token.encode()).hexdigest(), utc_now_iso())
    app = await apps_repo.get(inv["target_app_id"], consistent_read=True) if inv else None
    if (
        not app
        or app.get("owner_id") != inv["invite_owner_id"]
        or app.get("state") == "destroyed"
        or app.get("open_to") == "public"  # R59: any non-public app honours its links
    ):
        raise refused
    email = inv["email"]
    return network_proof.mint_app_pass(
        f"invite:{email}", name=email, email=email, ws=inv["invite_owner_id"], room="", app=app["name"]
    )
