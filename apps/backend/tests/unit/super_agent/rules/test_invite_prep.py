"""Invite prep built-in, observe (wave2-S Task 7, spec 12 §3.3/§5). Google and Clerk faked."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from core.repositories import automations_repo
from core.services import rules, rules_builtins, super_agent_flags
from scripts import rules_backfill_invite_prep as backfill

WS = "org_1"
ME = "sam@acme.com"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def google(monkeypatch):
    accounts = [{"email": ME}, {"email": "shared-inbox@acme.com"}]
    monkeypatch.setattr(rules_builtins.google_accounts_repo, "list_accounts", AsyncMock(return_value=accounts))
    monkeypatch.setattr(backfill.google_accounts_repo, "list_accounts", AsyncMock(return_value=accounts))
    members = [{"user_id": "user_sam", "email": "Sam@Acme.com"}, {"user_id": "user_priya", "email": "p@acme.com"}]
    monkeypatch.setattr(rules_builtins.clerk_admin, "list_organization_members", AsyncMock(return_value=members))


async def test_ensure_invite_prep_idempotent(table, google):
    first = await rules_builtins.ensure_invite_prep(WS)
    second = await rules_builtins.ensure_invite_prep(WS)
    assert first == second == [rules.invite_prep_id(ME)]
    row = await automations_repo.get_schedule(WS, first[0])
    assert (row["status"], row["mode"], row["builtin"], row["ownerUserId"]) == (
        "active", "observe", "invite_prep", "user_sam"
    )  # fmt: skip
    assert (row["budgetPerRunUsd"], row["timeLimitS"], row["dailyCapUsd"]) == (0.25, 180, 10.0)
    assert len(await automations_repo.list_schedules(WS)) == 1  # the unmatched shared inbox gets none


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


async def test_backfill_refuses_prod(table, google, monkeypatch):
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=True))
    monkeypatch.setattr(backfill.settings, "ENVIRONMENT", "prod")
    assert await backfill.run(WS, apply=True) == 2
    assert await automations_repo.list_schedules(WS) == []


async def test_backfill_refuses_switch_off(table, google, monkeypatch):
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=False))
    monkeypatch.setattr(backfill.settings, "ENVIRONMENT", "dev")
    assert await backfill.run(WS, apply=True) == 2
    assert await automations_repo.list_schedules(WS) == []


async def test_backfill_default_is_dry_run(table, google, monkeypatch, capsys):
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=True))
    monkeypatch.setattr(backfill.settings, "ENVIRONMENT", "dev")
    assert await backfill.run(WS) == 0
    assert await automations_repo.list_schedules(WS) == []
    assert rules.invite_prep_id(ME) in capsys.readouterr().out
    assert await backfill.run(WS, apply=True) == 0
    assert len(await automations_repo.list_schedules(WS)) == 1
