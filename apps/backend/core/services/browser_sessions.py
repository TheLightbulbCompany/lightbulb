"""AgentCore Browser: one profile per workspace, and the owner's watch-only live view.

Three functions, no state. The browser itself is AWS's built-in ``aws.browser.v1`` — we
never create one — so the only per-workspace object is a **Browser Profile**, whose
AWS-assigned id is injected into that workspace's runtime environment
(``AGENTCORE_PROFILE_ID``) and recorded on the containers row as ``browser_profile_id``.

**The backend discovers the live session; nothing self-reports it** (ruling R12). There is
no callback and no hook-token route: a runtime inside a microVM could only ever *claim* a
session id, and a claim is not an ownership proof. ``ListBrowserSessions`` summaries carry
no ``profileConfiguration`` (confirmed against the installed botocore model — only
``GetBrowserSession`` has it), so resolution is List-then-Get, and the Get is not optional.
The profile id is minted by us, carries an AWS-random 10-char suffix, and reaches exactly
one workspace's runtime env, which is what makes it a real owner binding.

Nothing about a session is cached or stored: no row field, no in-process map. The cost is
one List plus N Gets per panel refresh (the panel polls at 240 s against a 300 s presigned
URL — AWS's hard cap on that TTL).
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
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo

logger = logging.getLogger(__name__)

#: AWS's built-in managed browser. We never create a custom one, so this is a constant.
BROWSER_ID = "aws.browser.v1"

#: AWS hard-caps the live-view presigned URL at 300 s. Not a tunable.
LIVE_VIEW_TTL_S = 300

#: The DCV stream's fixed remote resolution, echoed to the panel so it can scale.
REMOTE_WIDTH = 1920
REMOTE_HEIGHT = 1080

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


def _profile_id_by_name(client, name: str) -> str:
    """``name`` is documented as a FILTER, not an exact match, so the exact comparison is
    ours: ``org_x`` and ``org_xy`` can come back in the same page and adopting the wrong
    one would point a workspace at another tenant's profile. One page only — the filter
    already narrows it to near-exact matches."""
    for summary in client.list_browser_profiles(name=name).get("profileSummaries", []):
        if summary.get("name") == name:
            return summary["profileId"]
    return ""


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


def _get_session(session_id: str) -> dict:
    """Sync — call via ``run_in_thread``."""
    return _data_client().get_browser_session(browserIdentifier=BROWSER_ID, sessionId=session_id)


async def owns_session(owner_id: str, session_id: str) -> dict | None:
    """THE ownership rule, and the only one: a session belongs to this workspace when
    ``GetBrowserSession`` reports it ``READY`` and carries this workspace's own
    ``profileConfiguration.profileIdentifier``. A profileless session — which a compromised
    runtime can start, since its policy allows ``StartBrowserSession`` on the built-in
    browser — matches nobody and is invisible to every panel."""
    row = await container_repo.get_by_owner_id(owner_id) or {}
    profile_id = row.get("browser_profile_id") or ""
    if not profile_id:
        return None
    try:
        got = await run_in_thread(_get_session, session_id)
    except ClientError:
        logger.info("browser session %s unreadable for owner=%s", session_id, owner_id, exc_info=True)
        return None
    if got.get("status") != "READY":
        return None
    if (got.get("profileConfiguration") or {}).get("profileIdentifier") != profile_id:
        return None
    return got


def _ready_session_ids() -> list[str]:
    """Sync — call via ``run_in_thread``. One page: a workspace's live session is seconds
    old, and the fleet's concurrent READY sessions are counted in single digits."""
    items = _data_client().list_browser_sessions(browserIdentifier=BROWSER_ID, status="READY").get("items", [])
    return [item["sessionId"] for item in items]


def _presign_live_view(session_id: str) -> str:
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
        f"/browser-streams/{BROWSER_ID}/sessions/{session_id}/live-view",
    )
    SigV4QueryAuth(credentials.get_frozen_credentials(), "bedrock-agentcore", region, expires=LIVE_VIEW_TTL_S).add_auth(
        request
    )
    return request.url


async def live_view(owner_id: str) -> dict:
    """The workspace's running browser session as a watch-only DCV URL, or inactive.

    Never starts a session: an open panel must not cost $0.1273/h, and the agent could not
    join a session the panel started anyway. The URL is returned to the owner's browser and
    goes nowhere else — not a log, not analytics, not the row.
    """
    row = await container_repo.get_by_owner_id(owner_id) or {}
    if not row.get("browser_profile_id"):
        return dict(_INACTIVE)
    for session_id in await run_in_thread(_ready_session_ids):
        if await owns_session(owner_id, session_id):
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=LIVE_VIEW_TTL_S)
            return {
                "active": True,
                "url": await run_in_thread(_presign_live_view, session_id),
                "expiresAt": expires_at.isoformat(),
                "remoteWidth": REMOTE_WIDTH,
                "remoteHeight": REMOTE_HEIGHT,
            }
    return dict(_INACTIVE)
