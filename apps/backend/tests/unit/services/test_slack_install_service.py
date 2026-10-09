import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import slack_install_service as svc  # noqa: E402


def _install_kwargs(**over):
    base = dict(
        owner_id="org_1",
        agent_id="agent_f",
        team_id="T1",
        team_name="Acme",
        bot_token="xoxb-1",
        bot_user_id="U_BOT",
        app_id="A1",
        scopes=["chat:write"],
        installer_slack_user_id="U_ADMIN",
        installer_member_id="user_1",
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_apply_install_writes_the_secret_then_the_row_then_the_status():
    """Order is the whole contract: the token lands in Secrets Manager BEFORE the install row
    claims the workspace, and the owner's card only says Connected once the row is ours -- a
    conflict after the status flip would leave a Connected card for somebody else's workspace.

    The task-definition roll and the ``channels.slack.accounts.<team>`` + route-binding writes
    into ``openclaw.json`` that used to sit between the row and the greeting are gone with the
    runtime that read them (Slack transport is M5/LATER).
    """
    calls: list[str] = []

    async def _put_secret(owner, provider, cred):
        calls.append(f"secret:{owner}:{provider}:{cred['bot_token']}")
        return "arn:secret"

    async def _put_row(**kw):
        calls.append("row")
        return {**kw, "status": "active"}

    async def _status(*a, **k):
        calls.append("status")

    async def _greet(token, user):
        calls.append(f"greet:{token}:{user}")

    with (
        patch("core.services.slack_install_service.integration_secret_store.put", _put_secret),
        patch("core.services.slack_install_service.slack_installs_repo.put", _put_row),
        patch("core.services.slack_install_service._mark_connected", _status),
        patch("core.services.slack_install_service._greet_installer", _greet),
    ):
        row = await svc.apply_install(**_install_kwargs())

    assert row["status"] == "active"
    assert calls == ["secret:org_1:slack/T1:xoxb-1", "row", "status", "greet:xoxb-1:U_ADMIN"]


@pytest.mark.asyncio
async def test_apply_install_conflict_raises_and_deletes_secret():
    deleted = []

    async def _delete(owner, provider):
        deleted.append(provider)

    with (
        patch("core.services.slack_install_service.integration_secret_store.put", AsyncMock(return_value="arn")),
        patch("core.services.slack_install_service.integration_secret_store.delete", _delete),
        patch("core.services.slack_install_service.slack_installs_repo.put", AsyncMock(return_value=None)),
    ):
        with pytest.raises(svc.SlackInstallConflict):
            await svc.apply_install(**_install_kwargs())
    assert deleted == ["slack/T1"]


@pytest.mark.asyncio
async def test_apply_install_unwinds_when_the_greeting_fails():
    """The greeting is the last step and the only one that can fail after the row is active. A
    partial failure must not leave an active install row plus a "Connected" card behind -- unwind
    to the same state as if the install never happened."""
    with (
        patch("core.services.slack_install_service.integration_secret_store.put", AsyncMock(return_value="arn:secret")),
        patch(
            "core.services.slack_install_service.slack_installs_repo.put", AsyncMock(return_value={"status": "active"})
        ),
        patch("core.services.slack_install_service._mark_connected", AsyncMock()),
        patch(
            "core.services.slack_install_service._greet_installer", AsyncMock(side_effect=RuntimeError("slack down"))
        ),
        patch("core.services.slack_install_service.revoke_install", AsyncMock(return_value=True)) as revoke,
    ):
        with pytest.raises(RuntimeError, match="slack down"):
            await svc.apply_install(**_install_kwargs())
    revoke.assert_awaited_once_with("T1")


@pytest.fixture(autouse=True)
def _team_ids(monkeypatch):
    """revoke_install drops the team from the owner's team_ids row; admit it without a table."""
    monkeypatch.setattr(svc.integrations_repo, "remove_connected_team", AsyncMock())


@pytest.mark.asyncio
async def test_revoke_install_tears_down_everything():
    """The secret delete is UNCONDITIONAL now. It used to be ordered behind a task-definition
    roll that dropped the env reference first, and skipped when that roll failed -- a dead secret
    ARN in a live task definition breaks the container's boot, while an orphaned secret is
    harmless. Nothing but us references the secret, so there is nothing to order it behind."""
    row = {"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}

    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.slack_installs_repo.set_status", AsyncMock()) as set_status,
        patch("core.services.slack_install_service.integration_secret_store.delete", AsyncMock()) as del_secret,
        patch(
            "core.services.slack_install_service.channel_link_repo.sweep_by_owner_provider_agent",
            AsyncMock(return_value=2),
        ) as sweep,
        patch("core.services.slack_install_service.slack_installs_repo.list_for_owner", AsyncMock(return_value=[])),
        patch("core.services.slack_install_service._mark_disconnected", AsyncMock()) as mark_disconnected,
        patch("core.services.slack_install_service.integrations_repo.remove_connected_team", AsyncMock()) as forget,
    ):
        assert await svc.revoke_install("T1") is True
    mark_disconnected.assert_awaited_once_with("org_1")
    # The owner's team_ids set stops naming a workspace it no longer holds.
    forget.assert_awaited_once_with("org_1", "slack", "T1")
    set_status.assert_awaited_once_with("T1", "revoked")
    del_secret.assert_awaited_once_with("org_1", "slack/T1")
    sweep.assert_awaited_once_with(owner_id="org_1", provider="slack", agent_id="agent_f")


@pytest.mark.asyncio
async def test_revoke_keeps_slack_connected_while_another_workspace_remains():
    """The owner has ONE Slack status row for every workspace; revoking one of
    two must not flip it, or catalog requirement checks report Slack missing
    while the other workspace is still transporting."""
    row = {"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}
    remaining = [{"team_id": "T2", "owner_id": "org_1", "agent_id": "agent_g", "status": "active"}]

    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.slack_installs_repo.set_status", AsyncMock()),
        patch("core.services.slack_install_service.integration_secret_store.delete", AsyncMock()),
        patch(
            "core.services.slack_install_service.channel_link_repo.sweep_by_owner_provider_agent",
            AsyncMock(return_value=0),
        ),
        patch(
            "core.services.slack_install_service.slack_installs_repo.list_for_owner", AsyncMock(return_value=remaining)
        ),
        patch("core.services.slack_install_service._mark_disconnected", AsyncMock()) as mark_disconnected,
    ):
        assert await svc.revoke_install("T1") is True
    mark_disconnected.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_ignores_a_stale_gsi_echo_of_the_revoked_row():
    """The by-owner GSI is eventually consistent; the row just flipped to
    revoked can still read as active. It must not count as "remaining"."""
    row = {"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}
    stale = [{"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}]

    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.slack_installs_repo.set_status", AsyncMock()),
        patch("core.services.slack_install_service.integration_secret_store.delete", AsyncMock()),
        patch(
            "core.services.slack_install_service.channel_link_repo.sweep_by_owner_provider_agent",
            AsyncMock(return_value=0),
        ),
        patch("core.services.slack_install_service.slack_installs_repo.list_for_owner", AsyncMock(return_value=stale)),
        patch("core.services.slack_install_service._mark_disconnected", AsyncMock()) as mark_disconnected,
    ):
        assert await svc.revoke_install("T1") is True
    mark_disconnected.assert_awaited_once_with("org_1")


@pytest.mark.asyncio
async def test_revoke_unknown_team_is_false():
    with patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=None)):
        assert await svc.revoke_install("T_NOPE") is False


def test_manifest_scopes_match_service_scopes():
    import pathlib

    import yaml

    manifest = yaml.safe_load(pathlib.Path("scripts/slack-app-manifest.yaml").read_text())
    assert sorted(manifest["oauth_config"]["scopes"]["bot"]) == sorted(svc.BOT_SCOPES)
    assert len(svc.BOT_SCOPES) == 23
    assert manifest["settings"]["socket_mode_enabled"] is False
    assert manifest["settings"]["interactivity"]["is_enabled"] is True
    assert manifest["features"]["shortcuts"]
    # The plugin handles both Agent View events; the manifest must declare
    # them or Slack never delivers them (no new scope needed).
    bot_events = manifest["settings"]["event_subscriptions"]["bot_events"]
    assert "assistant_thread_started" in bot_events
    assert "assistant_thread_context_changed" in bot_events


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Client:
    """Minimal httpx.AsyncClient stand-in recording the one POST we make."""

    def __init__(self, sent, body=None, raises=None):
        self._sent, self._body, self._raises = sent, body or {"ok": True}, raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, headers, json):
        if self._raises:
            raise self._raises
        self._sent.append((url, headers, json))
        return _Resp(self._body)


@pytest.mark.asyncio
async def test_greet_installer_dms_the_installer_with_the_bot_token():
    sent = []
    with patch("core.services.slack_install_service.httpx.AsyncClient", lambda **kw: _Client(sent)):
        await svc._greet_installer("xoxb-abc", "U_ADMIN")
    assert len(sent) == 1
    url, headers, body = sent[0]
    assert url == svc.SLACK_POST_MESSAGE_URL
    assert headers["Authorization"] == "Bearer xoxb-abc"
    # A user id as `channel` is what opens the IM — that IS the fix.
    assert body["channel"] == "U_ADMIN"
    assert body["text"] == svc.INSTALL_GREETING


@pytest.mark.asyncio
async def test_greet_installer_is_a_noop_without_an_installer_id():
    sent = []
    with patch("core.services.slack_install_service.httpx.AsyncClient", lambda **kw: _Client(sent)):
        await svc._greet_installer("xoxb-abc", "")
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw",
    [{"body": {"ok": False, "error": "channel_not_found"}}, {"raises": RuntimeError("slack down")}],
)
async def test_greet_installer_never_raises(kw):
    """The workspace is already connected by the time we greet — a failed
    greeting must not turn a good install into a rollback."""
    with patch("core.services.slack_install_service.httpx.AsyncClient", lambda **k: _Client([], **kw)):
        await svc._greet_installer("xoxb-abc", "U_ADMIN")
