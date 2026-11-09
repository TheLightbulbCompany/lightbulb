"""Spec 13 T7: access-token-free CLI files under backend custody, and the X/QuickBooks/Instagram
cutover + rollback (§6.2)."""

import json
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest

from core.config import settings
import yaml
from moto import mock_aws

from core.repositories import container_repo
from core.services import hermes_provisioning, owner_lifecycle
from core.services.credentials import cli_cutover
from core.services.credentials.materializers import instagram_cli, qbo_dotenv, xurl_cli

OWNER = "org_c"
BUCKET = "test-owner-state"
APP = {"client_id": "QBOCLIENTID", "client_secret": "QBOPLATFORMSECRET"}
TOKEN_SCRIPT = Path(cli_cutover.__file__).parent / "skills" / "instagram" / "token.sh"


# --- materializers --------------------------------------------------------------------------------


def _files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*") if p.is_file()}


def test_backend_custody_files_have_no_refresh_token(tmp_path):
    secrets = ("RT-X", "AT-X", "XSECRET", "RT-QBO", "QBOPLATFORMSECRET", "IG-TOKEN")
    x_cred = {
        "client_id": "xcid",
        "client_secret": "XSECRET",
        "access_token": "AT-X",
        "refresh_token": "RT-X",
        "expires_in": 7200,
        "username": "lb",
        "custody": "backend",
    }
    with patch.object(xurl_cli, "materializer_root", return_value=tmp_path / "x"):
        xurl_cli.write(OWNER, "x", x_cred)
    with (
        patch.object(qbo_dotenv, "materializer_root", return_value=tmp_path / "q"),
        patch.object(qbo_dotenv.platform_oauth_app_store, "get", AsyncMock(return_value=APP)),
    ):
        qbo_dotenv.write(OWNER, "quickbooks", {"refresh_token": "RT-QBO", "realm_id": "123", "custody": "backend"})
    with (
        patch.object(instagram_cli, "materializer_root", return_value=tmp_path / "i"),
        patch.object(instagram_cli, "_fetch_identity", return_value={"user_id": "9", "username": "lb"}),
    ):
        instagram_cli.write(OWNER, "instagram", {"access_token": "IG-TOKEN", "custody": "backend"})

    written = _files(tmp_path)
    assert not [(f, s) for f, body in written.items() for s in secrets if s in body]
    x = yaml.safe_load(written["x/.xurl"])["apps"]["lightbulb-x"]
    assert x["client_id"] == "xcid" and x["oauth2_tokens"]["lb"]["oauth2"]["expiration_time"] == 0
    assert "QUICKBOOKS_REALM_ID=123" in written["q/quickbooks/.env"]
    ig = json.loads(written["i/skills/instagram/credentials.json"])
    assert ig["custody"] == "backend" and ig["access_token"] == "" and ig["username"] == "lb"

    # Legacy form (no custody) is unchanged: the refresh token is in the file.
    with patch.object(xurl_cli, "materializer_root", return_value=tmp_path / "legacy"):
        xurl_cli.write(OWNER, "x", {k: v for k, v in x_cred.items() if k != "custody"})
    assert "RT-X" in (tmp_path / "legacy" / ".xurl").read_text()


@pytest.mark.skipif(shutil.which("jq") is None, reason="token.sh needs jq")
def test_token_sh_prints_only_when_brokered(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "curl-called"
    (bindir / "curl").write_text(f'#!/bin/bash\ntouch "{marker}"\necho "{{}}"\n')
    (bindir / "curl").chmod(0o755)
    cred = tmp_path / "credentials.json"
    env = {"IG_CREDENTIALS": str(cred), "PATH": f"{bindir}:/usr/bin:/bin:/opt/homebrew/bin"}

    # Brokered, inside the 30-day window: the bridge-filled token is printed, Meta never called.
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    cred.write_text(json.dumps({"access_token": "VENDED", "token_expiry": soon, "custody": "backend"}))
    result = subprocess.run(["bash", str(TOKEN_SCRIPT)], capture_output=True, text=True, env=env)
    assert (result.returncode, result.stdout) == (0, "VENDED")
    assert not marker.exists()
    assert json.loads(cred.read_text())["access_token"] == "VENDED"

    # Brokered but the bridge has not filled it: fail loudly, still no renewal.
    cred.write_text(json.dumps({"access_token": "", "custody": "backend"}))
    result = subprocess.run(["bash", str(TOKEN_SCRIPT)], capture_output=True, text=True, env=env)
    assert result.returncode == 1 and result.stdout == "" and not marker.exists()


# --- cutover / rollback ---------------------------------------------------------------------------


@pytest.fixture
def world(monkeypatch):
    @asynccontextmanager
    async def _guard(owner_id):
        yield

    state = {"row": {"provider": "x", "status": "connected"}, "secret": None, "projected": []}
    monkeypatch.setattr(cli_cutover.settings, "OWNER_STATE_BUCKET", BUCKET)
    monkeypatch.setattr(owner_lifecycle, "owner_lifecycle_guard", _guard)
    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"agentcore_runtime_arn": "arn:rt"}))
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(hermes_provisioning, "stop_runtime_session", stop)
    idle = AsyncMock(return_value=True)
    monkeypatch.setattr(hermes_provisioning, "_wait_for_idle_session", idle)
    lease = MagicMock(return_value=True)
    finish = MagicMock()
    monkeypatch.setattr(container_repo, "try_take_wake_lease_sync", lease)
    monkeypatch.setattr(container_repo, "finish_wake_sync", finish)
    monkeypatch.setattr(cli_cutover.integrations_repo, "get", AsyncMock(side_effect=lambda *a, **k: state["row"]))

    async def _set_custody(owner, provider, custody, *, expected=None):
        assert state["row"].get("custody") == expected
        state["row"] = {**state["row"], "custody": custody}

    monkeypatch.setattr(cli_cutover.integrations_repo, "set_custody", _set_custody)

    async def _put(owner, provider, credential):
        state["secret"] = credential

    monkeypatch.setattr(cli_cutover.integration_secret_store, "put", _put)
    monkeypatch.setattr(
        cli_cutover.integration_secret_store, "get", AsyncMock(side_effect=lambda *a, **k: state["secret"])
    )

    async def _project(owner, provider, materializer, credential):
        state["projected"].append((provider, materializer, credential))

    monkeypatch.setattr(cli_cutover.hermes_cli, "project", _project)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(cli_cutover, "_s3", lambda: s3)
        yield {"state": state, "s3": s3, "stop": stop, "lease": lease, "finish": finish, "idle": idle}


def _xurl(rt: str, exp: int) -> bytes:
    app = {
        "client_id": "xcid",
        "client_secret": "XSECRET",
        "redirect_uri": "https://api/cb",
        "default_user": "lb",
        "oauth2_tokens": {
            "lb": {
                "type": "oauth2",
                "oauth2": {"access_token": f"AT-{rt}", "refresh_token": rt, "expiration_time": exp},
            }
        },
    }
    return yaml.safe_dump(
        {"apps": {"lightbulb-x": app, "mine": {"client_id": "other"}}, "default_app": "lightbulb-x"}
    ).encode()


@pytest.mark.asyncio
async def test_x_newest_profile_copy_wins(world):
    s3, state = world["s3"], world["state"]
    exp = int(time.time()) + 3600
    s3.put_object(Bucket=BUCKET, Key=f"{OWNER}/hermes/a1/home/.xurl", Body=_xurl("RT-OLD", exp))
    time.sleep(1.1)  # S3 LastModified has one-second resolution
    s3.put_object(Bucket=BUCKET, Key=f"{OWNER}/hermes/a2/home/.xurl", Body=_xurl("RT-NEW", exp))

    assert await cli_cutover.cli_cutover(OWNER, "x") == "done"

    secret = state["secret"]
    assert secret["refresh_token"] == "RT-NEW" and secret["access_token"] == "AT-RT-NEW"
    assert secret["client_id"] == "xcid" and secret["client_secret"] == "XSECRET"
    assert secret["custody"] == "backend" and secret["username"] == "lb"
    assert abs(datetime.fromisoformat(secret["token_expiry"]).timestamp() - exp) < 2
    assert state["row"]["custody"] == "backend"
    assert state["projected"] == [("x", "xurl_cli", secret)]
    world["stop"].assert_awaited_once_with(OWNER, settings.HERMES_SHARED_RUNTIME_ARN)
    world["lease"].assert_called_once()
    world["finish"].assert_called_once()
    assert await cli_cutover.cli_cutover(OWNER, "x") == "already"


@pytest.mark.asyncio
async def test_quickbooks_secret_from_platform_store(world):
    s3, state = world["s3"], world["state"]
    state["row"] = {"provider": "quickbooks", "status": "connected"}
    env = "QUICKBOOKS_CLIENT_ID=QBOCLIENTID\nQUICKBOOKS_CLIENT_SECRET=QBOPLATFORMSECRET\n"
    env += "QUICKBOOKS_REFRESH_TOKEN=RT-QBO\nQUICKBOOKS_REALM_ID=123\nQUICKBOOKS_ENVIRONMENT=sandbox\n"
    s3.put_object(Bucket=BUCKET, Key=f"{OWNER}/hermes/a1/home/quickbooks/.env", Body=env.encode())

    assert await cli_cutover.cli_cutover(OWNER, "quickbooks") == "done"
    # The platform app secret is never copied into the owner's credential: refresh reads its store.
    assert state["secret"] == {"refresh_token": "RT-QBO", "realm_id": "123", "access_token": "", "custody": "backend"}


@pytest.mark.asyncio
async def test_instagram_window_30_days(world):
    s3, state = world["s3"], world["state"]
    state["row"] = {"provider": "instagram", "status": "connected"}
    expiry = (datetime.now(timezone.utc) + timedelta(days=45)).isoformat()
    body = {"access_token": "IG-1", "user_id": "9", "username": "lb", "token_expiry": expiry}
    s3.put_object(Bucket=BUCKET, Key=f"{OWNER}/hermes/a1/skills/instagram/credentials.json", Body=json.dumps(body))

    assert await cli_cutover.cli_cutover(OWNER, "instagram") == "done"
    assert state["secret"] == {**body, "custody": "backend"}


@pytest.mark.asyncio
async def test_rollback_rematerializes_refresh_token(world):
    state = world["state"]
    state["row"] = {"provider": "x", "status": "connected", "custody": "backend"}
    state["secret"] = {"access_token": "AT2", "refresh_token": "RT2", "client_id": "xcid", "custody": "backend"}

    assert await cli_cutover.cli_rollback(OWNER, "x") == "done"

    [(provider, materializer, credential)] = state["projected"]
    assert (provider, materializer) == ("x", "xurl_cli")
    assert credential["refresh_token"] == "RT2" and "custody" not in credential  # legacy form
    assert state["row"]["custody"] == "actor"
    assert await cli_cutover.cli_rollback(OWNER, "x") == "already"


@pytest.mark.asyncio
async def test_cutover_preconditions(world):
    state = world["state"]
    assert await cli_cutover.cli_cutover(OWNER, "x") == "no_grant"  # no profile copy in S3
    world["stop"].return_value = False
    world["s3"].put_object(Bucket=BUCKET, Key=f"{OWNER}/hermes/a1/home/.xurl", Body=_xurl("RT", 0))
    assert await cli_cutover.cli_cutover(OWNER, "x") == "busy_session"
    state["row"] = {"provider": "x", "status": "reconnect_required"}
    assert await cli_cutover.cli_cutover(OWNER, "x") == "no_grant"
    assert state["secret"] is None and "custody" not in state["row"]


@pytest.mark.asyncio
async def test_cut_over_quickbooks_without_access_token_refreshes_first(monkeypatch):
    """A cut-over QuickBooks credential has only the refresh token (their store never held an
    access token): the first vend refreshes instead of marking it reconnect_required."""
    from core.services.credentials import access_token

    @asynccontextmanager
    async def _guard(owner_id):
        yield

    stored = {"refresh_token": "RT-QBO", "realm_id": "123", "access_token": "", "custody": "backend"}
    monkeypatch.setattr(access_token.owner_lifecycle, "owner_lifecycle_guard", _guard)
    monkeypatch.setattr(
        access_token.integrations_repo,
        "get",
        AsyncMock(return_value={"status": "connected", "custody": "backend"}),
    )
    monkeypatch.setattr(access_token.integration_secret_store, "get", AsyncMock(return_value=dict(stored)))
    put = AsyncMock()
    monkeypatch.setattr(access_token.integration_secret_store, "put", put)
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    refresh = AsyncMock(
        return_value={**stored, "access_token": "AT-QBO", "refresh_token": "RT2", "token_expiry": later}
    )
    monkeypatch.setattr(access_token.oauth_dance, "refresh", refresh)
    mark = AsyncMock()
    monkeypatch.setattr(access_token, "_mark_reconnect", mark)

    token, _ = await access_token.get_with_expiry(OWNER, "quickbooks")
    assert token == "AT-QBO"
    mark.assert_not_awaited()
    put.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollback_between_read_and_lease_never_refreshes(monkeypatch):
    """Review of #4683: a rollback (custody -> actor) landing between the unleased read and the
    lease hands the grant back to the VM; the backend must not then spend X's single-use token."""
    from core.services.credentials import access_token

    rows = iter([{"status": "connected", "custody": "backend"}, {"status": "connected", "custody": "actor"}])

    @asynccontextmanager
    async def _guard(owner_id):
        yield

    monkeypatch.setattr(access_token.owner_lifecycle, "owner_lifecycle_guard", _guard)
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(side_effect=lambda *a, **k: next(rows)))
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    cred = {"access_token": "AT", "refresh_token": "RT", "token_expiry": soon}
    monkeypatch.setattr(access_token.integration_secret_store, "get", AsyncMock(return_value=cred))
    refresh = AsyncMock()
    monkeypatch.setattr(access_token.oauth_dance, "refresh", refresh)

    with pytest.raises(ValueError):
        await access_token.get_with_expiry(OWNER, "x")
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_cutover_waits_for_idle_and_never_stops_a_running_turn(world):
    """Review of #4691: stopping mid-turn would lose the turn's profile writes. The fence waits
    for the bridge to report idle (and hold new turns) first; still busy -> busy_session, no stop."""
    world["s3"].put_object(Bucket=BUCKET, Key=f"{OWNER}/hermes/a1/home/.xurl", Body=_xurl("RT", 0))
    world["idle"].return_value = False
    assert await cli_cutover.cli_cutover(OWNER, "x") == "busy_session"
    world["stop"].assert_not_awaited()
    world["lease"].assert_not_called()
    assert world["state"]["secret"] is None

    world["idle"].return_value = True
    assert await cli_cutover.cli_cutover(OWNER, "x") == "done"
    world["idle"].assert_awaited_with(OWNER)
    world["stop"].assert_awaited_once()
