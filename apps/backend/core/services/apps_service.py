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
import json
import logging
import re
import secrets as _random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


class AppStatus(TypedDict):
    app_id: str
    name: str
    display_name: str
    room_id: str
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


async def _check_room(owner_id: str, room_id: str) -> None:
    """R46: an app belongs to a room of the caller's own workspace (membership is not required)."""
    meta = await messaging_repo.get_room(room_id) if room_id else None
    if not meta or meta.get("deletedAt") or meta.get("ownerId") != owner_id:
        raise AppsRefused("room_not_found", "That room is not in this workspace.", "pick a room of this workspace")


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
    for provider, service, env in _RESOURCES:
        row = rows.get((provider, service, env))
        if row is None or row.get("state") in _DEAD:
            name = app["name"] if env == "prod" else f"{app['name']}-preview"
            await provisioning_workflow.start(
                owner_id=app["owner_id"],
                provider_name=provider,
                service_id=service,
                configuration={"name": name},
                issue_id=issue_id,
                # The owner reads this on the card: say what it is for, in words.
                reason=f"{app.get('display_name') or app['name']}: {'hosting' if provider == 'vercel' else 'a database'} ({'live' if env == 'prod' else 'preview'})",
                app_id=app["app_id"],
                environment=env,
            )


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


async def _redeploy_live(app: dict, creds: dict) -> None:
    """A production build of what is live (or about to be), through ``publish_pending``: it lands
    as a new version of the same commit."""
    pending = app.get("publish_pending") or {}
    live = next((v for v in app.get("versions") or [] if v.get("n") == app.get("live_version")), {})
    source_id = pending.get("source_id") or app.get("live_source_id") or live.get("source_id")
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
                "reason": "sharing changed",
                "migrations": src.get("migrations") or [],
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
        await _clear_pending(app, "preview_pending", {"error": _build_failed(st)})


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
        version = {
            "commit": pending["commit"],
            "deployment_id": pending["deployment_id"],
            "source_id": pending["source_id"],
            "at": utc_now_iso(),
            "by": pending["by"],
            "live": True,
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
        err = {**_build_failed(st), "next": "fix the error and publish again"}
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


def _build_failed(st: dict) -> dict:
    return {
        "code": "build_failed",
        "message": st.get("errorMessage") or "The build failed.",
        "next": "fix the error and run up again",
    }


async def _add_domain(app: dict, creds: dict, *, preview: bool) -> dict:
    host = apps_dns.preview_hostname(app["name"]) if preview else apps_dns.hostname(app["name"])
    r = await vercel.add_domain(creds, host)
    await apps_dns.point(app["name"], cname_target=r["cname"], verification=r["verification"], preview=preview)
    return {"name": host, "verified": bool(r["verified"])}


async def _verify(app: dict, creds: dict, field: str) -> None:
    domain = app.get(field)
    if domain and not domain.get("verified") and await vercel.verify_domain(creds, domain["name"]):
        await _patch(app, {field: {**domain, "verified": True}})


async def _preview_alias(app: dict, creds: dict) -> None:
    """R27: the preview answers at {name}-preview.{APPS_DOMAIN} once Vercel has verified it."""
    ready = app.get("preview")
    if not ready:
        return
    if not app.get("preview_domain"):
        await _patch(app, {"preview_domain": await _add_domain(app, creds, preview=True)})
    else:
        await _verify(app, creds, "preview_domain")
    if app["preview_domain"]["verified"] and app.get("preview_alias_for") != ready["deployment_id"]:
        host = app["preview_domain"]["name"]
        try:
            await vercel.assign_alias(creds, ready["deployment_id"], host)
        except vercel.VercelError as e:  # keep the *.vercel.app url; the next read tries again
            logger.warning("apps: %s preview alias not assigned: %s", app["app_id"], e.code)
            return
        await _patch(app, {"preview_alias_for": ready["deployment_id"], "preview_url": f"https://{host}"})
    if app.get("preview_alias_for") and not app.get("protection_scoped"):
        # R51: live and preview both answer on custom domains now; stale deployment urls close.
        await vercel.protect_except_custom_domains(creds)
        await _patch(app, {"protection_scoped": True})


async def _progress(app: dict) -> tuple[dict, AppError | None]:
    """One step on every pending thing. Returns the workflow rows and a transient error, if any."""
    rows = await _rows(app)
    for key, row in rows.items():
        if row.get("state") not in wf_repo.TERMINAL_STATES:
            rows[key] = await provisioning_workflow.advance_on_read(row["workflow_id"]) or row
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


_REFUSED = frozenset({"forbidden", "unauthorized", "not_authorized"})  # Vercel's JSON codes for a token it won't take
_REFUSAL_CODE = "vercel_credential_refused"  # only ever written after a probe confirmed the refusal
_RENEWAL_HOLDS = frozenset({"pending", "errored"})  # no automatic rotation until a person removes the field
_RENEW_EVERY = timedelta(hours=1)


def _refused(e: vercel.VercelError | None) -> bool:
    return e is not None and e.status in (401, 403) and e.code in _REFUSED


def _renewal_hold(app: dict) -> AppError | None:
    if (app.get("credential_renewal") or {}).get("state") not in _RENEWAL_HOLDS:
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
    """One read the token must be able to do. The failure, if any: only ``_refused`` of it means the token."""
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
    logger.warning("apps: %s stored a new vercel credential from Stripe", app["app_id"])
    return fresh


async def _renew_vercel(app: dict, rows: dict, creds: dict) -> dict | None:
    """After a confirmed refusal: the credentials to retry with, or None to report it.

    Reveal first; rotate only when Stripe still holds the refused token. The claim on
    ``credential_renewal`` (compare-and-set on ``at``) is written as ``pending`` before the POST, so a
    crash, a timeout or a cancel leaves the outcome-unknown state. Stripe's doc: pending -> don't resend;
    errored -> stop; complete -> read the new configuration, which ``needs_reveal`` keeps owed. pending and
    errored hold until a person removes ``credential_renewal`` from the record; a reveal still picks up a
    token that lands later, but never clears the hold. Rotations that work are spaced by ``_RENEW_EVERY``.
    """
    await _reload(app)
    renewal = app.get("credential_renewal") or {}
    state = renewal.get("state")
    try:
        key = f"apps/{app['app_id']}/vercel/prod"
        stored = await integration_secret_store.get(app["owner_id"], key) or {}
        if stored.get("token") and stored["token"] != creds["token"]:
            return stored  # another reader replaced it after this one read its copy
        account = await provisioning_workflow.account_id_for(app["owner_id"])
        resource = rows[("vercel", "project", "prod")]["resource_id"]
        fresh = await _store_revealed(app, account, resource, stored)
    except Exception as e:  # noqa: BLE001 - nothing was rotated; the refusal is reported and the next attempt reveals
        logger.warning("apps: %s vercel credential not read: %s", app["app_id"], type(e).__name__)
        return None
    if fresh:
        if state == "needs_reveal":
            await _patch(app, {"credential_renewal": {"state": "complete", "at": utc_now_iso()}})
        return fresh
    recent = state == "complete" and datetime.now(UTC) - datetime.fromisoformat(renewal["at"]) < _RENEW_EVERY
    if state in _RENEWAL_HOLDS or state == "needs_reveal" or recent:
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
    logger.warning("apps: %s vercel credential rotation: %s", app["app_id"], status)
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
    if not fresh:
        return None
    # A token refused straight after issue would otherwise rotate again every hour with nobody looking.
    done = "errored" if _refused(await _probe(app, fresh)) else "complete"
    await _patch(app, {"credential_renewal": {"state": done, "at": utc_now_iso()}})
    return None if done == "errored" else fresh


async def _on_vercel_error(
    app: dict, rows: dict, creds: dict, e: vercel.VercelError, next_default: str, *, renew: bool
) -> tuple[dict | None, AppError]:
    """Every Vercel failure in a step or a deploy: the credentials to retry with, if any, and the error in the
    person's words. A 401/403 says something about one call; only the probe says it about the token."""
    if e.status in (401, 403) and _refused(await _probe(app, creds)):
        return (await _renew_vercel(app, rows, creds) if renew else None), _refusal(app)
    return None, {"code": f"vercel_{e.code}", "message": str(e), "next": next_default}


async def _steps(
    app: dict, rows: dict, creds: dict, dev_db: dict | None, live_db: dict | None, *, renewed: bool = False
) -> AppError | None:
    try:
        # A step may make no Vercel call at all, so a stored refusal is checked by asking.
        refusal_stored = (app.get("publish_error") or {}).get("code") == _REFUSAL_CODE
        if refusal_stored or (app.get("credential_renewal") or {}).get("state") == "needs_reveal":
            if failure := await _probe(app, creds):
                raise failure
            if refusal_stored:
                await _patch(app, {"publish_error": None}, expect={"publish_error.code": _REFUSAL_CODE})
        if not app.get("protection_off"):
            await vercel.disable_protection(creds)
            await _patch(app, {"protection_off": True})
        await _push_env(app, creds, dev_db, live_db)
        await _check_preview(app, creds)
        await _check_publish(app, creds)
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
        await _preview_alias(app, creds)
    except vercel.VercelError as e:
        fresh, err = await _on_vercel_error(app, rows, creds, e, "run up again in a minute", renew=not renewed)
        if fresh:
            return await _steps(app, rows, fresh, dev_db, live_db, renewed=True)
        logger.warning("apps: %s vercel step failed: %s", app["app_id"], e.code)
        return err
    except _WorkspaceUnknown:
        logger.warning("apps: %s workspace name unavailable", app["app_id"])
        return {
            "code": "workspace_unavailable",
            "message": "Couldn't read the workspace.",
            "next": "run up again in a minute",
        }
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
    # A failed build leaves the live version serving.
    return "ready", app.get("error") or app.get("publish_error") or _renewal_hold(app)


async def _record_state(app: dict, state: str) -> str:
    record = "live" if state == "ready" and app.get("live_version") else state
    if app.get("state") != record:
        # Only over the state this copy saw: a reader from before a release must not undo its "live".
        await _patch(app, {"state": record}, expect={"state": app.get("state")})
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
            if r.get("state") != "active"
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
    owner_id: str, *, name: str, room_id: str, commit: str, archive: bytes, actor: BuilderCaller
) -> dict:
    """R32: the source upload creates the app record; ``up`` fills in the rest."""
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
            open_to="room",
            budget_usd=0,
            display_name=_display({"name": name}),
        )
    if app.get("state") == "destroyed":
        raise AppsRefused("app_destroyed", f"{name} was removed.", "pick another name")
    source_id = await apps_source.put(owner_id, app["app_id"], commit=commit, data=archive)
    files = await asyncio.to_thread(lambda: dict(apps_source.iter_files(archive)))
    names = [n for n, _ in _migrations(files)[0]]
    entry = {"commit": commit, "at": utc_now_iso(), "migrations": names, "migrations_key": _migrations_key(files)}
    sources = {**(app.get("sources") or {}), source_id: entry}
    keep = {app.get("live_source_id"), app.get("preview_want"), (app.get("publish_pending") or {}).get("source_id")}
    recent = sorted(sources, key=lambda k: sources[k]["at"])[-10:]
    sources = {k: v for k, v in sources.items() if k in keep or k in recent}
    await apps_repo.patch(app["app_id"], {"sources": sources})
    return {"app_id": app["app_id"], "source_id": source_id}


async def up(
    owner_id: str,
    *,
    name: str,
    room_id: str,
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
        fields.update(services=services, open_to=open_to, budget_usd=budget_usd, origin=origin, room_id=room_id)
        if requested_by and not app.get("created_by"):  # R73: an agent's app gets its human creator
            try:
                await messaging.require_room_member(owner_id, requested_by, room_id)
                fields["created_by"] = requested_by
            except messaging.ApiProblem:
                logger.info("apps: %s requested_by is not a member of its room; ignored", app["app_id"])
    if app.get("error"):  # "run up again" after a failure retries the build
        fields.update(error=None, preview_built=None)
    await _patch(app, fields)
    await _ensure_workflows(app, issue_id)
    rows, transient = await _progress(app)
    return await _result(app, rows, transient, cost_cents)


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
    for attempt in (1, 2):
        try:
            if not app.get("domain"):
                await _patch(app, {"domain": await _add_domain(app, creds, preview=False)})
            dep_id = await _deploy(app, creds, source_id, production=True)
            break
        except vercel.VercelError as e:
            fresh, err = await _on_vercel_error(app, rows, creds, e, "publish again", renew=attempt == 1)
            if fresh:
                creds = fresh
                continue
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
    preview = app.get("preview") or {}
    # A preview from before its source was recorded: the last build is that preview when none is pending.
    source_id = preview.get("source_id") or (
        None if app.get("preview_pending") else (app.get("preview_built") or {}).get("source_id")
    )
    if not preview or not source_id:
        raise AppsRefused("not_ready", "The app has no ready preview to publish.", "wait for the preview")
    actor = BuilderCaller(owner_id=owner_id, actor_id=user_id, actor_kind="person")
    try:
        return await publish(owner_id, app_id, source_id=source_id, actor=actor)
    except AppsRefused as e:
        if e.code == "destructive_migration":
            e.next = "ask the builder to get the owner's approval"
        raise


async def rollback(owner_id: str, app_id: str, *, version: int | None) -> UpResult:
    """Code only: the live database is never touched."""
    app = await _owned(owner_id, app_id)
    n = version if version is not None else (app.get("live_version") or 0) - 1
    target = next((v for v in app.get("versions") or [] if v["n"] == n), None)
    if target is None:
        raise AppsRefused("version_not_found", f"There is no version {n}.", "pick a version from status")
    rows = await _rows(app)
    creds = await _secret(app, rows, "vercel", "prod", "token")
    if not creds:
        raise AppsRefused("not_ready", "The app's Vercel project is not set up.")
    await vercel.rollback(creds, target["deployment_id"])
    cancelled = app.get("publish_pending")
    # The rollback wins over a publish in flight; `rolled_back` makes the next release promote itself.
    await _patch(app, {"live_version": n, "live_source_id": None, "rolled_back": True, "publish_pending": None})
    res = await _result(app, rows, None)
    res["error"] = {
        "code": "code_only",
        "message": f"Version {n} is live again." + (" The publish in progress was cancelled." if cancelled else ""),
        "next": "Data is unchanged. To restore data, use Neon's own restore on the company's account "
        "(Canvas links to it).",
    }
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
    if confirm_name != app["name"]:
        raise AppsRefused("confirm_name_mismatch", f'Type "{app["name"]}" to confirm.')
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


def _view(app: dict, rows: dict, state: str) -> AppStatus:
    preview = app.get("preview")
    return {
        "app_id": app["app_id"],
        "name": app["name"],
        "display_name": _display(app),
        "room_id": app["room_id"],
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
    }


async def status(owner_id: str, app_id: str) -> AppStatus:
    app = await _owned(owner_id, app_id)
    rows, _ = await _progress(app)
    state, err = _state(app, rows)
    view = _view(app, rows, await _record_state(app, state))
    if err:
        view["error"] = err
    return view


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
    fields = {
        k: v
        for k, v in (("open_to", open_to), ("budget_usd", budget_usd), ("display_name", display_name))
        if v is not None
    }
    if fields:
        await _patch(app, fields)
    return await status(owner_id, app_id)


async def list_apps(owner_id: str, auth: AuthContext) -> list[dict]:
    """Canvas's rows: the record as stored (no provider calls), names resolved here, destroyed apps hidden."""
    apps = [plain(a) for a in await apps_repo.list_for_owner(owner_id) if a.get("state") != "destroyed"]
    if not apps:
        return []
    all_rows = await wf_repo.list_for_owner(owner_id)
    room_ids = sorted({a["room_id"] for a in apps})
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
        row = _view(a, await _rows(a, all_rows), a.get("state") or "")
        del row["versions"]
        uid = a.get("created_by")
        row.update(
            # R35: an invited app still opens for its room; invitees use their links.
            can_open=a["open_to"] in ("public", "workspace") or bool(member.get(a["room_id"])),
            # Only a room the caller is in is named to them.
            room_name=(live.get(a["room_id"]) or {}).get("name") if member.get(a["room_id"]) else None,
            created_by_name=names.get(uid) if uid and names.get(uid) != uid else None,
            agent_name=agents.get(a.get("actor_id")) if a.get("actor_kind") == "agent" else None,
            # R73: who may publish (and manage); whether there is a ready preview is the `preview` field.
            can_publish=can_manage(a, auth),
            # R71: the last persisted failure, message only.
            error={"message": err.get("message", "")}
            if (err := a.get("error") or a.get("publish_error") or _renewal_hold(a))
            else None,
        )
        out.append(row)
    return out


async def errors(owner_id: str, app_id: str) -> dict:
    """R25: the last 50 browser (client) and server lines of the live deployment, else the latest
    preview; build events when there are no runtime logs. Every app secret and any database URL is
    replaced with ``[redacted]``."""
    app = await _owned(owner_id, app_id)
    rows = await _rows(app)
    creds = await _secret(app, rows, "vercel", "prod", "token")
    live = next((v for v in app.get("versions") or [] if v.get("n") == app.get("live_version")), None)
    dep = (live or app.get("preview") or {}).get("deployment_id")
    if not creds or not dep:
        return {"client": [], "server": []}
    lines = await vercel.runtime_logs(creds, dep, limit=200)
    client = [x for x in lines if any(m in x["message"] for m in _CLIENT_MARKS)]
    server = [x for x in lines if x not in client] if lines else await vercel.deployment_events(creds, dep, limit=50)
    keys = [f"apps/{app_id}/{p}/{e}" for p, _, e in _RESOURCES] + [f"apps/{app_id}/lightbulb"]
    blobs = await asyncio.gather(*(integration_secret_store.get(owner_id, k) for k in keys))
    values = {v for b in blobs if b for v in b.values() if isinstance(v, str) and len(v) >= 8}
    secret_re = re.compile("|".join(re.escape(v) for v in sorted(values, key=len, reverse=True)) or r"(?!)")

    def clean(xs: list[dict]) -> list[dict]:
        return [
            {"at": x.get("at"), "message": _DB_URL.sub("[redacted]", secret_re.sub("[redacted]", x["message"]))}
            for x in xs[-50:]
        ]

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
    if app["open_to"] in ("room", "invited"):  # R35: invited apps still open for their room
        try:
            await messaging.require_room_member(owner_id, auth.user_id, app["room_id"])
        except messaging.ApiProblem:
            # The room is not named: Canvas never names a room the caller is not in.
            raise AppsRefused(
                "not_a_member", "Only members of the app's room can open it.", "ask in the app's room"
            ) from None
    # "workspace": the route's get_current_user + resolve_owner_id already made the caller an org member.
    await _patch(app, {"last_opened_at": utc_now_iso()})
    if app["open_to"] == "public":
        return url
    name = await messaging.human_display_name(auth.user_id)
    email = auth.email or ""
    if name == auth.user_id and email:
        name = email
    token = network_proof.mint_app_pass(
        auth.user_id, name=name, email=email, ws=owner_id, room=app["room_id"], app=app["name"]
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
        room=app["room_id"],
        app=_preview_app(app),
        life_s=TEST_PASS_S,
    )
    return f"{url}/_lightbulb/session#token={token}"


async def invite(owner_id: str, app_id: str, *, emails: list[str], user_id: str) -> dict:
    """One single-use, 14-day link per email, to this app only; a room app then becomes "invited"
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
    if app["open_to"] == "room":  # R59: additive; only once the links exist. A workspace app stays workspace.
        await update_settings(owner_id, app_id, open_to="invited")
        app["open_to"] = "invited"
    once = "" if public else " The link works once and expires in 14 days."
    for email, link in links.items():
        # send_email never raises; a failed send still counts (the admin can invite again).
        await email_service.send_email(
            to=email,
            subject=f"You're invited to {app['name']}",
            html=f'<p>You\'re invited to open {app["name"]}.</p><p><a href="{link}">Open {app["name"]}</a></p>'
            + (f"<p>{once.strip()}</p>" if once else ""),
            text=f"You're invited to open {app['name']}.\n\n{link}\n{once}".rstrip() + "\n",
            from_addr=settings.EMAIL_FROM_WELCOME,
            kind="apps_invite",
        )
    return {"invited": len(emails), "open_to": app["open_to"]}


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
