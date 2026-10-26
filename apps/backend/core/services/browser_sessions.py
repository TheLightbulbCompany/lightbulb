"""AgentCore Browser profiles, AWS ownership checks and direct stream signing.

The session lifecycle and saved state live in browser_control.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError
from botocore.session import get_session

from core.billing import bedrock_aip
from core.billing.bedrock_aip import BROWSER_ID, BUILTIN_BROWSER_ID
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo

logger = logging.getLogger(__name__)

#: ``BROWSER_ID`` (the env's VPC browser) has ONE home, ``bedrock_aip`` — the owner's IAM
#: grant, this module's session calls and the runtime's ``AGENTCORE_BROWSER_ID`` env var must
#: agree or the failure is silent and split: the runtime starts sessions on a browser the
#: policy does not name (403), or the backend lists a browser the runtime is not using (a
#: permanently dead panel). Re-exported here so ``hermes_provisioning`` reaches it through the
#: module it already imports.
__all__ = ["BROWSER_ID", "delete_browser_profiles", "ensure_browser_profile", "owns_session"]

#: AWS hard-caps the live-view presigned URL at 300 s. Not a tunable.
LIVE_VIEW_TTL_S = 300

#: Fallback viewport, used ONLY when GetBrowserSession reports none. The session's own
#: ``viewPort`` is what the panel must scale by.
REMOTE_WIDTH = 1920
REMOTE_HEIGHT = 1080

#: The profile list is filtered by an all-but-exact name, so one small page is enough.
_PROFILE_PAGE = 10

_INACTIVE = {
    "active": False,
    "url": None,
    "expiresAt": None,
    "remoteWidth": REMOTE_WIDTH,
    "remoteHeight": REMOTE_HEIGHT,
}


def _control_client():
    """Profiles live on the CONTROL plane. Built per call (same reason
    ``hermes_provisioning`` does): tests monkeypatch ``boto3.client`` globally."""
    return boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)


def _data_client():
    """Sessions live on the DATA plane — a different service name, not an alias."""
    return boto3.client("bedrock-agentcore", region_name=settings.AWS_REGION)


def _profile_ids_by_name(client, name: str) -> list[str]:
    """``name`` is documented as a FILTER, not an exact match, so the exact comparison is
    ours: ``org_x`` and ``org_xy`` can come back in the same page, and adopting (or
    deleting) the wrong one reaches into another tenant's workspace. One page only — the
    filter already narrows it to near-exact matches."""
    page = client.list_browser_profiles(name=name, maxResults=_PROFILE_PAGE)
    return [s["profileId"] for s in page.get("profileSummaries", []) if s.get("name") == name]


def _profile_id_by_name(client, name: str) -> str:
    ids = _profile_ids_by_name(client, name)
    return ids[0] if ids else ""


def _find_or_create_profile(owner_id: str) -> str:
    """Sync — call via ``run_in_thread``."""
    client = _control_client()
    found = _profile_id_by_name(client, owner_id)
    if found:
        return found
    try:
        # ClientToken's model minimum is 33 chars and it forbids underscores, so a Clerk
        # org id cannot be the idempotency key; its sha256 hex digest is stable and legal.
        # A Clerk org id DOES satisfy BrowserProfileName ([a-zA-Z][a-zA-Z0-9_]{0,47}).
        return client.create_browser_profile(
            name=owner_id,
            clientToken=hashlib.sha256(owner_id.encode()).hexdigest(),
        )["profileId"]
    except ClientError as exc:
        # Code-string check, not `client.exceptions.ConflictException`: this module is
        # exercised with stubbed and mocked clients, and `except <non-class>` is a
        # TypeError at except-evaluation time (the same trap bedrock_aip documents).
        if exc.response["Error"]["Code"] != "ConflictException":
            raise
        # A provision and a roll raced. The winner's profile is ours too.
        found = _profile_id_by_name(client, owner_id)
        if not found:
            raise
        return found


async def ensure_browser_profile(owner_id: str) -> str:
    """Create-or-adopt this workspace's Browser Profile, grant it, record it; return the
    AWS-assigned ``profileId``.

    **Never raises into provisioning.** A failure here returns ``""``, which travels up as
    "no profile" and therefore "no browser variables on the runtime" — the workspace comes
    up with the browser tools hidden rather than the provision failing.
    """
    try:
        profile_id = await run_in_thread(_find_or_create_profile, owner_id)
        await bedrock_aip.ensure_owner_browser_policy(owner_id, profile_id)
        await container_repo.update_fields(owner_id, {"browser_profile_id": profile_id})
        return profile_id
    except Exception:  # noqa: BLE001 — the browser is never worth failing a provision over
        logger.warning("browser profile setup failed for owner=%s", owner_id, exc_info=True)
        return ""


def _delete_profiles(owner_id: str) -> int:
    """Sync — call via ``run_in_thread``."""
    client = _control_client()
    deleted = 0
    for profile_id in _profile_ids_by_name(client, owner_id):
        client.delete_browser_profile(profileId=profile_id)
        deleted += 1
    return deleted


async def delete_browser_profiles(owner_id: str) -> int:
    """Account-wipe half: destroy this workspace's Browser Profile(s).

    A profile stores the workspace's **cookies and local storage** (up to 50 MB), so leaving
    it behind after a wipe leaves a deleted workspace's session state in our account — a
    data-deletion gap, not merely a quota one. Its name is deterministic (``== owner_id``),
    the same argument ``reap_owner_bedrock_resources`` uses to reap the IAM role
    unconditionally, so this needs nothing from the row image.

    **Never raises**, like every other leg of the reap's best-effort set: a wipe must not
    fail on the browser leg, whatever the reason. It returns 0 and logs a WARNING — and
    that warning means a real failure worth reading (a throttle, a ``ConflictException`` on
    an already-``DELETING`` profile, a genuine ``AccessDenied``), not an expected one: the
    backend task role's ``bedrock-agentcore:DeleteBrowserProfile`` grant is #2794, merged.
    """
    try:
        return await run_in_thread(_delete_profiles, owner_id)
    except Exception:  # noqa: BLE001 — a wipe must not fail on the browser leg
        logger.warning(
            "browser profile delete FAILED for owner=%s -- the workspace's cookies/local "
            "storage may survive the wipe; delete it by hand if this persists",
            owner_id,
            exc_info=True,
        )
        return 0


#: session id -> the browser it runs on. A session never changes browser, so a hit is final.
_SESSION_BROWSER: dict[str, str] = {}


def browser_of(session_id: str) -> str:
    """Sync — call via ``run_in_thread``. The browser this session runs on: ``BROWSER_ID``, or
    the built-in one for a session started before the move to the VPC browser, which is still
    read, used and stopped there until it ends (an 8 h TTL at most). Unknown: ``BROWSER_ID``."""
    if session_id not in _SESSION_BROWSER and BROWSER_ID != BUILTIN_BROWSER_ID:
        try:
            _get_session(session_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
    return _SESSION_BROWSER.get(session_id, BROWSER_ID)


def _get_session(session_id: str) -> dict:
    """Sync — call via ``run_in_thread``. Tries the session's known browser, else each one."""
    known = _SESSION_BROWSER.get(session_id)
    candidates = [known] if known else list(dict.fromkeys((BROWSER_ID, BUILTIN_BROWSER_ID)))
    for browser in candidates:
        try:
            got = _data_client().get_browser_session(browserIdentifier=browser, sessionId=session_id)
        except ClientError as exc:
            if browser != candidates[-1] and exc.response["Error"]["Code"] == "ResourceNotFoundException":
                continue
            raise
        _SESSION_BROWSER[session_id] = browser
        return got
    raise AssertionError("unreachable")


async def owns_session(
    owner_id: str, session_id: str, *, profile_id: str | None = None, strict: bool = False
) -> dict | None:
    """THE ownership rule, and the only one: a session belongs to this workspace when
    ``GetBrowserSession`` reports it ``READY`` and carries this workspace's own
    ``profileConfiguration.profileIdentifier``. A profileless session — which a compromised
    runtime can start, since its policy allows ``StartBrowserSession`` on the built-in
    browser — matches nobody and is invisible to every panel.

    ``profile_id`` lets a caller that has already read the row hand it down rather than pay
    a GetItem per candidate; omit it and the row is read here.
    """
    if profile_id is None:
        row = await container_repo.get_by_owner_id(owner_id) or {}
        profile_id = row.get("browser_profile_id") or ""
    if not profile_id:
        return None
    try:
        got = await run_in_thread(_get_session, session_id)
    except ClientError as exc:
        if strict and exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        logger.info("browser session %s unreadable for owner=%s", session_id, owner_id, exc_info=True)
        return None
    if got.get("status") != "READY":
        return None
    if (got.get("profileConfiguration") or {}).get("profileIdentifier") != profile_id:
        return None
    return got


def _presign_stream(session_id: str, stream: str) -> str:
    """Sync — call via ``run_in_thread``. Mirrors ``bedrock-agentcore`` 1.23.1's
    ``generate_live_view_url``; botocore already signs AppSync the same way in
    ``live_events.py``, so this needs no new package."""
    credentials = get_session().get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials unavailable for the browser live-view presign")
    region = settings.AWS_REGION
    request = AWSRequest(
        method="GET",
        url=f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/browser-streams/{browser_of(session_id)}/sessions/{session_id}/{stream}",
    )
    SigV4QueryAuth(credentials.get_frozen_credentials(), "bedrock-agentcore", region, expires=LIVE_VIEW_TTL_S).add_auth(
        request
    )
    return request.url.replace("https://", "wss://", 1) if stream == "automation" else request.url


def _presign_live_view(session_id: str) -> str:
    return _presign_stream(session_id, "live-view")


async def session_view(session_id: str, got: dict) -> dict:
    view_port = got.get("viewPort") or {}
    return {
        "active": True,
        "url": await run_in_thread(_presign_live_view, session_id),
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=LIVE_VIEW_TTL_S)).isoformat(),
        "remoteWidth": view_port.get("width") or REMOTE_WIDTH,
        "remoteHeight": view_port.get("height") or REMOTE_HEIGHT,
    }
