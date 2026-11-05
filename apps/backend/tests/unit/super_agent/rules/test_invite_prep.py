"""Invite prep built-in, observe (wave2-S Task 7, spec 12 §3.3/§5). gog, Clerk, Secrets Manager and Temporal faked
(lane A's seed harness)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.repositories import automations_repo, google_accounts_repo
from core.services import clerk_admin, rules, rules_builtins, super_agent_flags
from scripts import rules_backfill_invite_prep as backfill
from tests.unit.super_agent.automations.test_seed_hooks import _channel, env  # noqa: F401

WS = "org_1"
ME = "sam@acme.com"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(env, monkeypatch):  # noqa: F811
    accounts = [{"email": ME}, {"email": "shared-inbox@acme.com"}]
    monkeypatch.setattr(google_accounts_repo, "list_accounts", AsyncMock(return_value=accounts))
    members = [{"user_id": "user_sam", "email": "Sam@Acme.com"}, {"user_id": "user_priya", "email": "p@acme.com"}]
    monkeypatch.setattr(clerk_admin, "list_organization_members", AsyncMock(return_value=members))
    gog = MagicMock(return_value=_channel())
    with patch("core.services.google_watch._run_gog", gog):
        yield MagicMock(env=env, gog=gog)


async def _rows():
    rows = await automations_repo.list_schedules(WS)
    return rows


async def test_ensure_invite_prep_idempotent(world):
    first = await rules_builtins.ensure_invite_prep(WS)
    second = await rules_builtins.ensure_invite_prep(WS)
    assert first == second == [rules.invite_prep_id(ME)]
    (row,) = await _rows()  # the unmatched shared inbox gets none
    assert (row["status"], row["mode"], row["builtin"], row["ownerUserId"]) == (
        "active", "observe", "invite_prep", "user_sam"
    )  # fmt: skip
    assert (row["budgetPerRunUsd"], row["timeLimitS"], row["dailyCapUsd"]) == (0.25, 180, 10.0)
    (pid,) = row["hookPublicIds"]  # one calendar watch, not one per call
    hook = await automations_repo.get_hook(WS, pid)
    assert hook["account"] == ME and hook["scheduleId"] == row["scheduleId"] and hook["armed"] is True
    assert [s[0] for s in world.env.temporal.started] == ["GoogleWatchKeeperWorkflow"]
    assert world.gog.call_args.args[0][:6] == ["gog", "api", "call", "calendar", "v3", "events.watch"]


async def test_ensure_invite_prep_scope_missing(world):
    world.gog.side_effect = ValueError("Google refused the watch request: insufficient scope")
    await rules_builtins.ensure_invite_prep(WS)
    (row,) = await _rows()
    assert (row["status"], row["pausedReason"], row.get("hookPublicIds") or []) == ("paused_system", "google_scope", [])
    world.gog.side_effect = None  # the owner reconnects with the scope: the next ensure turns it on
    await rules_builtins.ensure_invite_prep(WS)
    (row,) = await _rows()
    assert row["status"] == "active" and len(row["hookPublicIds"]) == 1


async def test_on_google_connect_is_behind_the_switch_and_best_effort(monkeypatch):
    ensure = AsyncMock(side_effect=RuntimeError("clerk down"))
    monkeypatch.setattr(rules_builtins, "ensure_invite_prep", ensure)
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=False))
    await rules_builtins.on_google_connect(WS)
    ensure.assert_not_awaited()
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=True))
    await rules_builtins.on_google_connect(WS)  # raises inside: logged, never fails the connect
    ensure.assert_awaited_once()


def _event(**over):
    base = {
        "id": "e1",
        "status": "confirmed",
        "summary": "Q4 review",
        "organizer": {"email": "boss@acme.com"},
        "attendees": [{"email": ME, "responseStatus": "needsAction"}, {"email": "boss@acme.com"}],
        "start": {"dateTime": "2026-11-06T15:00:00Z"},
    }
    return {**base, **over}


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (_event(), True),
        (_event(organizer={"email": ME}), False),  # own event
        (_event(organizer={"self": True}), False),
        (_event(attendees=[{"email": ME, "responseStatus": "accepted"}]), False),
        (_event(start={"dateTime": "2026-11-01T15:00:00Z"}), False),  # past
        (_event(status="cancelled"), False),
        (_event(attendees=[{"email": "x@acme.com", "responseStatus": "needsAction"}]), False),  # not invited
        (_event(attendees=[{"self": True, "responseStatus": "needsAction"}], start={"date": "2026-11-07"}), True),
    ],
)
def test_calendar_invite_match(event, expected):
    assert rules.open_invite(event, ME, NOW) is expected


async def test_backfill_refuses_prod(world, monkeypatch):
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=True))
    monkeypatch.setattr(backfill.settings, "ENVIRONMENT", "prod")
    assert await backfill.run(WS, apply=True) == 2
    assert await _rows() == []


async def test_backfill_refuses_switch_off(world, monkeypatch):
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=False))
    monkeypatch.setattr(backfill.settings, "ENVIRONMENT", "dev")
    assert await backfill.run(WS, apply=True) == 2
    assert await _rows() == []


async def test_backfill_default_is_dry_run(world, monkeypatch, capsys):
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=True))
    monkeypatch.setattr(backfill.settings, "ENVIRONMENT", "dev")
    assert await backfill.run(WS) == 0
    assert await _rows() == []
    assert rules.invite_prep_id(ME) in capsys.readouterr().out
    assert await backfill.run(WS, apply=True) == 0
    assert len(await _rows()) == 1
