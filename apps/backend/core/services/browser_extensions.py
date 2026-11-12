"""Chrome extensions (and Chrome policies) the workspace's shared browser starts with.

AWS's managed browser loads extensions only from ZIPs in our S3 at ``StartBrowserSession``
(``extensions=[…]``), so "installing" one means: put its ZIP under ``browser-state/<owner>/``,
add it to the list on the ``__browser__#<owner>`` row, and restart the session. Any workspace
member can do that -- from Connections (a Web Store link or a ZIP), or from inside the browser
through our helper extension. An agent can only ask (``request_install``): a web page can
prompt-inject an agent, and this browser holds every workspace sign-in.

Every object a session loads (extension ZIPs, our helper, the policy file) lives under
``browser-state/<owner>/`` -- the backend-only prefix. The agent's runtime can read AND write
everything under ``<owner>/*``: an extension ZIP there could be swapped for code that reads
every page, and the helper's baked-in token installs extensions and fills saved logins.

The helper (``apps/backend/browser_helper/``) is written at each session start: its token is
bound to that session. The policy file is written only when it changes (``browser_helper`` on
the ``__browser__#<owner>`` row).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import secrets
import struct
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import ClientError
from cryptography.fernet import InvalidToken

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.encryption import get_fernet
from core.repositories import container_repo, messaging_repo

logger = logging.getLogger(__name__)

#: AWS limits (browser-extensions devguide): 10 MB per ZIP, 10 per session -- one of which is
#: our helper.
MAX_BYTES = 10 * 1024 * 1024
MAX_EXTENSIONS = 9

#: The managed browser's Chrome major (its user agent, dev 2026-09-27). The update service
#: picks the newest build compatible with this version.
CHROME_VERSION = "148.0.0.0"
CRX_URL = (
    "https://clients2.google.com/service/update2/crx?response=redirect&prodversion={version}"
    "&acceptformat=crx3&x=id%3D{id}%26uc"
)
_WEBSTORE_ID = re.compile(r"(?<![a-z])[a-p]{32}(?![a-z])")

HELPER_DIR = Path(__file__).resolve().parents[2] / "browser_helper"
#: A session's hard lifetime (browser_control.SESSION_SECONDS); the token also dies with its
#: session (``helper_owner`` checks the row's ``helper_nonce``).
HELPER_TOKEN_TTL_S = 8 * 3600
_HELPER_PREFIX = "browser-helper:"

#: Chrome enterprise policies, RECOMMENDED level (the only level a session on the built-in
#: browser accepts). Chrome's own password manager and autofill can't work here -- AWS keeps no
#: Chrome profile between sessions -- so they are off, and the helper offers the workspace's
#: website logins instead.
POLICIES = {
    "PasswordManagerEnabled": False,
    "PasswordLeakDetectionEnabled": False,
    "AutofillAddressEnabled": False,
    "AutofillCreditCardEnabled": False,
    "BrowserSignin": 0,
    "SyncDisabled": True,
    "PromotionsEnabled": False,
    "DefaultBrowserSettingEnabled": False,
    "PrivacySandboxPromptEnabled": False,
    # Memory Saver discards background tabs (an agent's tab reloads under it), and while it is
    # off Chrome promotes it ("Make Chromium faster", prod 2026-09-27). Settled by policy, so no
    # promo; Battery Saver likewise.
    "HighEfficiencyModeEnabled": False,
    "BatterySaverModeAvailability": 0,
}

REQUEST_TTL_S = 30 * 24 * 3600


class ExtensionError(Exception):
    """A safe, user-facing refusal."""


def object_key(owner: str, slug: str) -> str:
    return f"browser-state/{owner}/extensions/{slug}.zip"


def helper_key(owner: str) -> str:
    return f"browser-state/{owner}/browser-helper.zip"


def policy_key(owner: str) -> str:
    return f"browser-state/{owner}/chrome-policy.json"


def _s3():
    return boto3.client("s3", region_name=settings.AWS_REGION)


def _public(ext: dict) -> dict:
    return {k: ext.get(k) for k in ("slug", "name", "version", "source", "addedBy", "addedAt")}


async def _current(owner: str) -> list[dict]:
    """The stored list, with any entry still under the old ``<owner>/browser-extensions/``
    prefix (#3287, which the agent's runtime can write) moved to ``browser-state/<owner>/``.
    Returns only entries under the new prefix: one that could not move is left out, never
    loaded. ponytail: remove with the old IAM grant once no row names the old prefix."""
    old = await container_repo.get_browser_extensions(owner) or []
    stale = [e for e in old if not e["key"].startswith(f"browser-state/{owner}/")]
    if stale:
        bucket, s3 = settings.OWNER_STATE_BUCKET, _s3()
        try:
            for e in stale:
                await run_in_thread(
                    s3.copy_object,
                    Bucket=bucket,
                    Key=object_key(owner, e["slug"]),
                    CopySource={"Bucket": bucket, "Key": e["key"]},
                    ContentType="application/zip",
                    MetadataDirective="REPLACE",
                )
            moved = [{**e, "key": object_key(owner, e["slug"])} if e in stale else e for e in old]
            if await container_repo.compare_browser_extensions(owner, old, moved):
                for e in stale:
                    await run_in_thread(s3.delete_object, Bucket=bucket, Key=e["key"])
                return moved
        except ClientError as exc:
            logger.warning("browser extension migration failed owner=%s: %s", owner, exc.response["Error"]["Code"])
    return [e for e in old if e["key"].startswith(f"browser-state/{owner}/")]


# ---------------------------------------------------------------------------
# session start: policies, the helper, the workspace's extensions
# ---------------------------------------------------------------------------


def helper_token(owner: str, nonce: str) -> str:
    return get_fernet().encrypt(f"{_HELPER_PREFIX}{owner}:{nonce}".encode()).decode()


async def helper_owner(token: str) -> str | None:
    """The workspace a helper token was minted for, while the session it was minted for is the
    one running (``browser_state.helper_nonce``); None when forged, expired, or that session
    ended (idle stop, restart, AWS expiry)."""
    try:
        value = get_fernet().decrypt(token.encode(), ttl=HELPER_TOKEN_TTL_S).decode()
    except (InvalidToken, ValueError):
        return None
    owner, _, nonce = value.removeprefix(_HELPER_PREFIX).rpartition(":")
    if not value.startswith(_HELPER_PREFIX) or not owner or not nonce:
        return None
    state = await container_repo.get_browser_state(owner) or {}
    if not state.get("session_id") or not secrets.compare_digest(str(state.get("helper_nonce") or ""), nonce):
        return None
    return owner


def helper_zip(owner: str, nonce: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(HELPER_DIR.iterdir()):
            if path.is_file():
                zf.write(path, path.name)
        api = f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/browser/helper"
        zf.writestr("config.json", json.dumps({"api": api, "token": helper_token(owner, nonce)}))
    return buf.getvalue()


_POLICY_DIGEST = hashlib.sha256(json.dumps(POLICIES, sort_keys=True).encode()).hexdigest()


async def session_config(owner: str, nonce: str) -> dict:
    """Keyword arguments for StartBrowserSession: the Chrome policy file, our helper, and the
    workspace's extensions. The helper is rewritten at every start (its token names this
    session's ``nonce``); the policy file only when ``POLICIES`` changed."""
    bucket = settings.OWNER_STATE_BUCKET
    s3 = _s3()
    row = await container_repo.get_by_owner_id(f"{container_repo.BROWSER_KEY_PREFIX}{owner}", consistent=True) or {}
    if (row.get("browser_helper") or {}).get("digest") != _POLICY_DIGEST:
        await run_in_thread(
            s3.put_object,
            Bucket=bucket,
            Key=policy_key(owner),
            Body=json.dumps(POLICIES).encode(),
            ContentType="application/json",
        )
        await container_repo.set_browser_helper(owner, {"digest": _POLICY_DIGEST})
    await run_in_thread(
        s3.put_object,
        Bucket=bucket,
        Key=helper_key(owner),
        Body=helper_zip(owner, nonce),
        ContentType="application/zip",
    )
    keys = [helper_key(owner), *(e["key"] for e in await _current(owner))]
    return {
        "enterprisePolicies": [
            {"type": "RECOMMENDED", "location": {"s3": {"bucket": bucket, "prefix": policy_key(owner)}}}
        ],
        "extensions": [{"location": {"s3": {"bucket": bucket, "prefix": k}}} for k in keys],
    }


# ---------------------------------------------------------------------------
# the list
# ---------------------------------------------------------------------------


async def _put(owner: str, entry: dict) -> dict:
    """Add or replace one entry by slug. The object is already written."""
    old = await container_repo.get_browser_extensions(owner)
    rest = [e for e in old or [] if e["slug"] != entry["slug"]]
    if len(rest) >= MAX_EXTENSIONS:
        raise ExtensionError(f"The browser can start with at most {MAX_EXTENSIONS} extensions. Remove one first.")
    if not await container_repo.compare_browser_extensions(owner, old, [*rest, entry]):
        raise ExtensionError("Extensions changed. Refresh and try again.")
    return _public(entry)


def _entry(owner: str, slug: str, name: str, version: str, source: str, user: str) -> dict:
    return {
        "slug": slug,
        "name": name[:120],
        "version": version[:40],
        "source": source,
        "key": object_key(owner, slug),
        "addedBy": user,
        "addedAt": datetime.now(timezone.utc).isoformat(),
    }


async def _store(owner: str, slug: str, data: bytes, name: str, version: str, source: str, user: str) -> dict:
    # AgentCore refuses an extension object whose Content-Type is not application/zip.
    await run_in_thread(
        _s3().put_object,
        Bucket=settings.OWNER_STATE_BUCKET,
        Key=object_key(owner, slug),
        Body=data,
        ContentType="application/zip",
    )
    return await _put(owner, _entry(owner, slug, name, version, source, user))


def read_manifest(data: bytes) -> dict:
    """Validate a ZIP the way Chrome will read it: manifest.json at the root. Adds ``_name``,
    the display name (a localized ``__MSG_x__`` name resolved from ``_locales``)."""
    if len(data) > MAX_BYTES:
        raise ExtensionError("Extensions must be 10 MB or smaller.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("manifest.json")
            if info.file_size > 1024 * 1024:
                raise ExtensionError("manifest.json is too large.")
            manifest = json.loads(zf.read(info).decode("utf-8-sig"))
            if isinstance(manifest, dict):
                manifest["_name"] = _display_name(manifest, zf)
    except KeyError:
        raise ExtensionError("manifest.json must be at the top of the ZIP, not inside a folder.") from None
    except (zipfile.BadZipFile, ValueError):
        raise ExtensionError("That file is not a valid extension ZIP.") from None
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 3:
        raise ExtensionError("Only Manifest V3 extensions are supported.")
    if not manifest["_name"]:
        raise ExtensionError("manifest.json has no name.")
    return manifest


def _display_name(manifest: dict, zf: zipfile.ZipFile) -> str:
    messages: dict = {}
    locale = manifest.get("default_locale")
    if isinstance(locale, str) and re.fullmatch(r"[A-Za-z_]{2,10}", locale):
        try:
            raw = json.loads(zf.read(f"_locales/{locale}/messages.json").decode("utf-8-sig"))
            messages = {k.lower(): v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
        except (KeyError, ValueError):
            pass
    for name in (manifest.get("name"), manifest.get("short_name")):
        if not isinstance(name, str):
            continue
        found = re.fullmatch(r"__MSG_(\w+)__", name.strip())
        if found:
            name = (messages.get(found.group(1).lower()) or {}).get("message")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


# ---------------------------------------------------------------------------
# Chrome Web Store
# ---------------------------------------------------------------------------


def is_webstore_id(value: str) -> bool:
    """A Web Store id (32 letters a-p): the slug of every store install, and the extension's real
    id because its store key is kept."""
    return bool(re.fullmatch(r"[a-p]{32}", value or ""))


def webstore_id(id_or_url: str) -> str:
    found = _WEBSTORE_ID.findall((id_or_url or "").strip().lower())
    if len(set(found)) != 1:
        raise ExtensionError("Paste a Chrome Web Store link or a 32-letter extension id.")
    return found[0]


def _fields(buf: bytes) -> dict[int, list[bytes]]:
    """The length-delimited fields of one protobuf message (all a CRX3 header needs)."""
    out: dict[int, list[bytes]] = {}
    i = 0

    def varint() -> int:
        nonlocal i
        shift = value = 0
        while True:
            byte = buf[i]
            i += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    while i < len(buf):
        tag = varint()
        wire = tag & 7
        if wire == 0:
            varint()
        elif wire == 2:
            size = varint()
            out.setdefault(tag >> 3, []).append(buf[i : i + size])
            i += size
        else:
            raise ValueError("unsupported protobuf wire type")
    return out


def crx_id(public_key: bytes) -> str:
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(public_key).hexdigest()[:32])


def unpack_crx(data: bytes, ext_id: str) -> bytes:
    """A CRX3 as the ZIP AgentCore wants, with the store's public key written into
    manifest.json as ``key`` -- so the unpacked copy keeps its store id, which
    externally_connectable and the extension's own backend rely on."""
    try:
        if data[:4] != b"Cr24" or struct.unpack("<I", data[4:8])[0] != 3:
            raise ValueError("not a CRX3")
        size = struct.unpack("<I", data[8:12])[0]
        header = _fields(data[12 : 12 + size])
        proofs = [_fields(p).get(1, [b""])[0] for p in header.get(2, []) + header.get(3, [])]
        key = next(k for k in proofs if k and crx_id(k) == ext_id)
        body = data[12 + size :]
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(body)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            manifest = json.loads(src.read("manifest.json").decode("utf-8-sig"))
            manifest["key"] = base64.b64encode(key).decode()
            for info in src.infolist():
                if info.filename != "manifest.json":
                    dst.writestr(info, src.read(info))
            dst.writestr("manifest.json", json.dumps(manifest))
    except (ValueError, KeyError, IndexError, StopIteration, struct.error, zipfile.BadZipFile):
        raise ExtensionError("The Chrome Web Store sent a package that could not be read.") from None
    return out.getvalue()


async def fetch_webstore(ext_id: str) -> tuple[bytes, dict]:
    """The store's current package as (zip, manifest). The backend fetches; the browser never."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        try:
            response = await client.get(CRX_URL.format(version=CHROME_VERSION, id=ext_id))
        except httpx.HTTPError:
            raise ExtensionError("Could not reach the Chrome Web Store. Try again.") from None
    if response.status_code != 200 or not response.content:
        raise ExtensionError("That extension isn't available in the Chrome Web Store.")
    if len(response.content) > MAX_BYTES:
        raise ExtensionError("Extensions must be 10 MB or smaller.")
    data = unpack_crx(response.content, ext_id)
    return data, read_manifest(data)


async def add_from_webstore(owner: str, id_or_url: str, user: str) -> dict:
    ext_id = webstore_id(id_or_url)
    data, manifest = await fetch_webstore(ext_id)
    return await _store(owner, ext_id, data, manifest["_name"], str(manifest.get("version", "")), "webstore", user)


# ---------------------------------------------------------------------------
# an agent's ask: a Needs-you Activity item any member can accept
# ---------------------------------------------------------------------------


def request_sk(request_id: str) -> str:
    return f"EXTREQ#{request_id}"


def activity_item_id(request_id: str) -> str:
    return f"extension:{request_id}"


async def get_request(owner: str, request_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{32}", request_id or ""):
        return None
    return await messaging_repo._get(messaging_repo.owner_pk(owner), request_sk(request_id))


async def request_install(owner: str, id_or_url: str, *, agent_id: str | None) -> dict:
    """File the ask. The package is fetched now, so a bad id fails the agent, not the human."""
    from core.services import website_logins
    from core.services.live_events import publish_owner_users
    from core.services.messaging import make_live_event

    ext_id = webstore_id(id_or_url)
    if any(e["slug"] == ext_id for e in await container_repo.get_browser_extensions(owner) or []):
        return {"status": "installed"}
    _, manifest = await fetch_webstore(ext_id)
    request_id = uuid.uuid4().hex
    created = utc_now_iso()
    row = {
        "pk": messaging_repo.owner_pk(owner),
        "sk": request_sk(request_id),
        "requestId": request_id,
        "extensionId": ext_id,
        "name": manifest["_name"][:120],
        "agentId": agent_id or "",
        "agentName": await website_logins._agent_name(owner, agent_id) if agent_id else "The shared browser",
        "status": "pending",
        "createdAt": created,
        "expiresAt": int(time.time()) + REQUEST_TTL_S,
    }
    await run_in_thread(messaging_repo._table().put_item, Item=row)
    for user_id in await website_logins._humans_to_ask(owner, None):
        try:
            await messaging_repo.put_activity_row(
                {
                    "pk": messaging_repo.activity_pk(owner, user_id),
                    "sk": messaging_repo.activity_sk(created, activity_item_id(request_id)),
                    "kind": "extension",
                    "requestId": request_id,
                    "createdAt": created,
                    "expiresAt": row["expiresAt"],
                }
            )
        except Exception:  # noqa: BLE001 -- the rest of the workspace still gets the ask
            logger.warning("browser extension ask row failed owner=%s user=%s", owner, user_id)
    try:
        await publish_owner_users(owner, make_live_event("runtime.resource.invalidated", {"resource": "activity"}))
    except Exception:  # noqa: BLE001 -- the badge catches up on the next focus
        logger.warning("browser extension ask live event failed owner=%s", owner)
    logger.info("browser_extension.request owner=%s request=%s ext=%s agent=%s", owner, request_id, ext_id, agent_id)
    return {"status": "requested", "requestId": request_id, "name": row["name"]}


async def settle_request(owner: str, request_id: str, status: str, user: str) -> bool:
    """pending -> ``status`` exactly once."""
    try:
        await run_in_thread(
            messaging_repo._table().update_item,
            Key={"pk": messaging_repo.owner_pk(owner), "sk": request_sk(request_id)},
            UpdateExpression="SET #s = :s, resolvedAt = :at, resolvedBy = :u",
            ConditionExpression="#s = :p",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":p": "pending", ":at": utc_now_iso(), ":u": user},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True
