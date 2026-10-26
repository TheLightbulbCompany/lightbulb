"""AgentCore Browser: per-workspace profile, ownership rule, live-view presign.

Every AWS call goes through botocore's own ``Stubber`` (the pattern
``test_hermes_provisioning.py`` already uses) rather than a MagicMock: the stub runs
botocore's parameter validator, which is what proves ``clientToken`` satisfies the
model's 33-char minimum.
"""

import datetime
import hashlib

import boto3
import pytest
from botocore.stub import Stubber

from core.services import browser_sessions

OWNER = "org_x"
PROFILE_ID = "org_x-ab12cd34ef"
NOW = datetime.datetime(2026, 9, 19, tzinfo=datetime.timezone.utc)


def _summary(name: str, profile_id: str) -> dict:
    return {
        "profileId": profile_id,
        "profileArn": f"arn:aws:bedrock-agentcore:us-east-1:1:browser-profile/{profile_id}",
        "name": name,
        "status": "ACTIVE",
        "createdAt": NOW,
        "lastUpdatedAt": NOW,
    }


def _got_session(
    session_id: str,
    *,
    profile_id: str | None,
    status: str = "READY",
    view_port: dict | None = None,
) -> dict:
    out = {
        "browserIdentifier": "aws.browser.v1",
        "sessionId": session_id,
        "createdAt": NOW,
        "status": status,
    }
    if profile_id is not None:
        out["profileConfiguration"] = {"profileIdentifier": profile_id}
    if view_port is not None:
        out["viewPort"] = view_port
    return out


@pytest.fixture
def stub_control(monkeypatch):
    client = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.activate()
    monkeypatch.setattr(browser_sessions, "_control_client", lambda: client)
    yield stubber
    stubber.assert_no_pending_responses()
    stubber.deactivate()


@pytest.fixture
def stub_data(monkeypatch):
    client = boto3.client("bedrock-agentcore", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.activate()
    monkeypatch.setattr(browser_sessions, "_data_client", lambda: client)
    yield stubber
    stubber.assert_no_pending_responses()
    stubber.deactivate()


@pytest.fixture
def row(monkeypatch):
    """Install the owner's containers row. Defaults to one carrying PROFILE_ID."""

    def _install(value: dict | None = None):
        item = {"owner_id": OWNER, "browser_profile_id": PROFILE_ID} if value is None else value

        async def _get(owner_id, *, consistent=False):
            return item

        monkeypatch.setattr(browser_sessions.container_repo, "get_by_owner_id", _get)

    _install()
    return _install


@pytest.fixture
def writes(monkeypatch):
    """The two side effects ``ensure_browser_profile`` performs once it has an id."""
    calls: dict[str, list] = {"policy": [], "row": []}

    async def _policy(owner_id, profile_id):
        calls["policy"].append((owner_id, profile_id))

    async def _update(owner_id, fields):
        calls["row"].append((owner_id, fields))
        return {}

    monkeypatch.setattr(browser_sessions.bedrock_aip, "ensure_owner_browser_policy", _policy)
    monkeypatch.setattr(browser_sessions.container_repo, "update_fields", _update)
    return calls


@pytest.fixture
def aws_creds(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTTESTTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)


# --- ensure_browser_profile -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_browser_profile_matches_the_name_exactly(stub_control, writes):
    """``name`` is a FILTER on ListBrowserProfiles, not an exact match, so a longer
    workspace id sharing our prefix comes back in the same page. Adopting it would point
    this workspace at another one's profile."""
    stub_control.add_response(
        "list_browser_profiles",
        {"profileSummaries": [_summary("org_xy", "org_xy-zz11223344"), _summary(OWNER, PROFILE_ID)]},
        {"name": OWNER, "maxResults": 10},
    )
    # No create_browser_profile response is queued -- the Stubber errors if one is called.
    assert await browser_sessions.ensure_browser_profile(OWNER) == PROFILE_ID
    assert writes["policy"] == [(OWNER, PROFILE_ID)]
    assert writes["row"] == [(OWNER, {"browser_profile_id": PROFILE_ID})]


@pytest.mark.asyncio
async def test_create_uses_a_64_char_hex_client_token(stub_control, writes):
    """``ClientToken`` has a 33-char minimum and forbids underscores, so the raw Clerk org
    id fails the model on both counts; a sha256 hex digest is the stable idempotency key."""
    token = hashlib.sha256(OWNER.encode()).hexdigest()
    assert len(token) == 64
    stub_control.add_response("list_browser_profiles", {"profileSummaries": []}, {"name": OWNER, "maxResults": 10})
    stub_control.add_response(
        "create_browser_profile",
        {
            "profileId": PROFILE_ID,
            "profileArn": f"arn:aws:bedrock-agentcore:us-east-1:1:browser-profile/{PROFILE_ID}",
            "createdAt": NOW,
            "status": "ACTIVE",
        },
        {"name": OWNER, "clientToken": token},
    )
    assert await browser_sessions.ensure_browser_profile(OWNER) == PROFILE_ID


@pytest.mark.asyncio
async def test_a_concurrent_create_is_resolved_by_conflict_then_relist(stub_control, writes):
    """A provision and a roll can race; the loser gets ConflictException and adopts the
    winner's profile instead of failing the provision."""
    stub_control.add_response("list_browser_profiles", {"profileSummaries": []}, {"name": OWNER, "maxResults": 10})
    stub_control.add_client_error("create_browser_profile", service_error_code="ConflictException")
    stub_control.add_response(
        "list_browser_profiles", {"profileSummaries": [_summary(OWNER, PROFILE_ID)]}, {"name": OWNER, "maxResults": 10}
    )
    assert await browser_sessions.ensure_browser_profile(OWNER) == PROFILE_ID


@pytest.mark.asyncio
async def test_ensure_browser_profile_returns_empty_on_aws_error(stub_control, writes):
    """Never raises into provisioning: no profile just means no browser this round."""
    stub_control.add_client_error("list_browser_profiles", service_error_code="ThrottlingException")
    assert await browser_sessions.ensure_browser_profile(OWNER) == ""
    assert writes["policy"] == [] and writes["row"] == []


# --- owns_session ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owns_session_accepts_only_this_workspaces_profile(stub_data, row):
    stub_data.add_response(
        "get_browser_session",
        _got_session("S1", profile_id=PROFILE_ID),
        {"browserIdentifier": "aws.browser.v1", "sessionId": "S1"},
    )
    assert await browser_sessions.owns_session(OWNER, "S1") is not None


@pytest.mark.asyncio
async def test_owns_session_rejects_another_tenants_profile(stub_data, row):
    stub_data.add_response(
        "get_browser_session",
        _got_session("S1", profile_id="org_other-99zz88yy77"),
        {"browserIdentifier": "aws.browser.v1", "sessionId": "S1"},
    )
    assert await browser_sessions.owns_session(OWNER, "S1") is None


@pytest.mark.asyncio
async def test_owns_session_rejects_a_profileless_session(stub_data, row):
    """A compromised runtime may start a session with no profileConfiguration; it matches
    no workspace and must never be surfaced on someone's panel."""
    stub_data.add_response(
        "get_browser_session",
        _got_session("S1", profile_id=None),
        {"browserIdentifier": "aws.browser.v1", "sessionId": "S1"},
    )
    assert await browser_sessions.owns_session(OWNER, "S1") is None


@pytest.mark.asyncio
async def test_owns_session_rejects_a_terminated_session(stub_data, row):
    stub_data.add_response(
        "get_browser_session",
        _got_session("S1", profile_id=PROFILE_ID, status="TERMINATED"),
        {"browserIdentifier": "aws.browser.v1", "sessionId": "S1"},
    )
    assert await browser_sessions.owns_session(OWNER, "S1") is None


@pytest.mark.asyncio
async def test_owns_session_is_none_without_a_profile_id_and_makes_no_aws_call(stub_data, row):
    row({"owner_id": OWNER})
    assert await browser_sessions.owns_session(OWNER, "S1") is None


# --- live_view ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_view_presigns_for_five_minutes(aws_creds):
    out = await browser_sessions.session_view("S1", _got_session("S1", profile_id=PROFILE_ID))
    assert out["active"] is True
    assert "X-Amz-Expires=300" in out["url"]
    assert "/browser-streams/aws.browser.v1/sessions/S1/live-view" in out["url"]
    # The advertised expiry must never outlive the signature it describes.
    expires_at = datetime.datetime.fromisoformat(out["expiresAt"])
    assert expires_at.tzinfo is not None
    assert expires_at <= datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=browser_sessions.LIVE_VIEW_TTL_S
    )
    # No viewPort on the session -> the constants are the FALLBACK, not the answer.
    assert (out["remoteWidth"], out["remoteHeight"]) == (1920, 1080)


@pytest.mark.asyncio
async def test_session_view_reports_the_sessions_own_viewport(aws_creds):
    """The panel scales the DCV canvas by these; a hardcoded 1920x1080 against a session
    running at another size crops or letterboxes the view. GetBrowserSession carries the
    real viewPort and we have already read it."""
    got = _got_session("S1", profile_id=PROFILE_ID, view_port={"width": 1280, "height": 800})
    out = await browser_sessions.session_view("S1", got)
    assert (out["remoteWidth"], out["remoteHeight"]) == (1280, 800)


@pytest.mark.asyncio
async def test_delete_browser_profiles_removes_only_this_workspaces_profile(stub_control):
    """The profile holds the workspace's cookies and local storage, so a wipe that leaves it
    behind leaves a deleted workspace's session state in our account. Same exact-name rule as
    the create path: ``name`` is a filter, and deleting ``org_xy``'s profile is a live
    tenant's outage."""
    stub_control.add_response(
        "list_browser_profiles",
        {"profileSummaries": [_summary("org_xy", "org_xy-zz11223344"), _summary(OWNER, PROFILE_ID)]},
        {"name": OWNER, "maxResults": 10},
    )
    stub_control.add_response(
        "delete_browser_profile",
        {
            "profileId": PROFILE_ID,
            "profileArn": f"arn:aws:bedrock-agentcore:us-east-1:1:browser-profile/{PROFILE_ID}",
            "status": "DELETING",
            "lastUpdatedAt": NOW,
        },
        {"profileId": PROFILE_ID},
    )
    assert await browser_sessions.delete_browser_profiles(OWNER) == 1


@pytest.mark.asyncio
async def test_delete_browser_profiles_survives_a_delete_failure(stub_control):
    """A wipe must never fail on the browser leg, whatever the reason -- an AccessDenied, a
    throttle, or a ConflictException on an already-DELETING profile. It logs and carries on;
    every other teardown step still reports its own result."""
    stub_control.add_response(
        "list_browser_profiles",
        {"profileSummaries": [_summary(OWNER, PROFILE_ID)]},
        {"name": OWNER, "maxResults": 10},
    )
    stub_control.add_client_error("delete_browser_profile", service_error_code="AccessDeniedException")
    assert await browser_sessions.delete_browser_profiles(OWNER) == 0


@pytest.mark.asyncio
async def test_a_session_started_on_the_built_in_browser_is_still_found_after_the_move(stub_data, row, monkeypatch):
    """Codex on #4391: a session open on aws.browser.v1 when the VPC browser ships must still be
    read and stopped where it runs, not read as gone and orphaned until its TTL."""
    monkeypatch.setattr(browser_sessions, "BROWSER_ID", "lightbulb_dev_workspace_browser-Ab12Cd34Ef")
    monkeypatch.setattr(browser_sessions, "_SESSION_BROWSER", {})
    stub_data.add_client_error(
        "get_browser_session",
        service_error_code="ResourceNotFoundException",
        expected_params={"browserIdentifier": "lightbulb_dev_workspace_browser-Ab12Cd34Ef", "sessionId": "S1"},
    )
    stub_data.add_response(
        "get_browser_session",
        _got_session("S1", profile_id=PROFILE_ID),
        {"browserIdentifier": "aws.browser.v1", "sessionId": "S1"},
    )
    assert await browser_sessions.owns_session(OWNER, "S1") is not None
    assert browser_sessions.browser_of("S1") == "aws.browser.v1"  # no second lookup: cached
    assert "/browser-streams/aws.browser.v1/sessions/S1/" in browser_sessions._presign_stream("S1", "automation")
