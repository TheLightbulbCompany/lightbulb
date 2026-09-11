"""POST /launch/team-interest — a team raises its hand, the founders enter it
by hand. Storage + the founder email + the manual-lane invariants."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import launch_service, launch_settings

CLOSES = datetime.now(timezone.utc) + timedelta(days=10)


@pytest.fixture(autouse=True)
def _fresh_state():
    from routers import launch

    launch._RATE.clear()
    launch._RESP_CACHE.clear()
    yield
    launch._RATE.clear()
    launch._RESP_CACHE.clear()


@pytest.fixture
def configured(launch_schedule):
    with launch_schedule(CLOSES):
        yield


BODY = {"team_name": "Tuple", "seats": 4, "note": "six of us, four seats"}


# --- the route --------------------------------------------------------------


async def test_team_interest_requires_auth(configured, unauthenticated_async_client):
    r = await unauthenticated_async_client.post("/api/v1/launch/team-interest", json=BODY)
    assert r.status_code in (401, 403)


@patch("routers.launch.launch_service.submit_team_interest", AsyncMock(return_value={"team_name": "Tuple", "seats": 4}))
async def test_team_interest_stores_and_returns_the_row(configured, async_client):
    r = await async_client.post("/api/v1/launch/team-interest", json=BODY)
    assert r.status_code == 200 and r.json()["team_name"] == "Tuple"


async def test_team_interest_404s_when_the_feature_is_off(launch_schedule, async_client):
    with launch_schedule():
        r = await async_client.post("/api/v1/launch/team-interest", json=BODY)
    assert r.status_code == 404


@pytest.mark.parametrize(
    "over",
    [
        {"seats": 0},
        {"seats": -3},
        {"seats": 501},  # past TEAM_SEATS_MAX
        {"seats": "four"},
        {"team_name": "T"},
        {"team_name": "   "},  # stripped BEFORE min_length, or it lands as ""
    ],
)
async def test_team_interest_validates_the_body(configured, async_client, over):
    r = await async_client.post("/api/v1/launch/team-interest", json={**BODY, **over})
    assert r.status_code == 422


@patch("routers.launch.launch_service.submit_team_interest", AsyncMock(return_value={}))
async def test_an_org_context_may_submit_even_though_it_may_not_join(configured, async_client_org_admin):
    """The whole point of the lane: /join still refuses an org context (409
    org_not_supported, untouched), and this is where those teams go instead."""
    r = await async_client_org_admin.post("/api/v1/launch/team-interest", json=BODY)
    assert r.status_code == 200
    join = await async_client_org_admin.post("/api/v1/launch/join", json={"display_name": "Tuple"})
    assert join.status_code == 409 and join.json()["detail"] == {"reason": "org_not_supported"}


@patch("routers.launch.launch_service.submit_team_interest", AsyncMock(return_value={}))
async def test_an_org_member_is_refused_like_every_other_one_row_per_owner_write(configured, async_client_org_member):
    """The row keys to resolve_owner_id (the ORG id), so a member submitting
    would overwrite the org's row — the same reason /connect-stripe and
    PATCH /entry gate on org admin."""
    r = await async_client_org_member.post("/api/v1/launch/team-interest", json=BODY)
    assert r.status_code == 403 and r.json()["detail"] == "org_admin_required"


@patch("routers.launch.launch_service.submit_team_interest", AsyncMock(return_value={}))
async def test_team_interest_is_rate_limited_per_ip(configured, async_client):
    for _ in range(10):
        assert (await async_client.post("/api/v1/launch/team-interest", json=BODY)).status_code == 200
    r = await async_client.post("/api/v1/launch/team-interest", json=BODY)
    assert r.status_code == 429


# --- the service ------------------------------------------------------------

AUTH = AuthContext(user_id="human_t", org_id="user_t", org_role="org:admin", email="founder@tuple.com")


class _Store:
    """The team-interest rows, in memory, with the repo's upsert semantics:
    `submitted_at` and the founders' `counted` verdict survive a second
    submission."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def put_interest(self, owner_id, **fields):
        row = self.rows.setdefault(owner_id, {"submitted_at": "first", "counted": False})
        if row.get("counted"):
            # Mirrors the repo's conditional retry: a counted row keeps the
            # founders' seat count, everything else still lands.
            fields = {k: v for k, v in fields.items() if k != "seats"}
        row.update(fields, owner_id=owner_id)
        return dict(row)

    async def update_fields(self, owner_id, fields):
        if owner_id not in self.rows:
            return None
        self.rows[owner_id].update(fields)
        return dict(self.rows[owner_id])

    async def list_all(self):
        return [dict(r) for r in self.rows.values()]


@pytest.fixture
def store(launch_schedule):
    s = _Store()
    with (
        launch_schedule(CLOSES),
        patch.object(launch_service.launch_teams_repo, "put_interest", s.put_interest),
        patch.object(launch_service.launch_teams_repo, "update_fields", s.update_fields),
        patch.object(launch_service.launch_teams_repo, "list_all", s.list_all),
        patch.object(launch_service, "resolve_email", AsyncMock(return_value="founder@tuple.com")),
    ):
        yield s


@patch("core.services.launch_sheet_email.email_service.send_email", AsyncMock(return_value="msg_1"))
async def test_submission_stores_the_row_and_emails_the_founders(store):
    from core.services.launch_sheet_email import email_service

    out = await launch_service.submit_team_interest(AUTH, team_name="Tuple", seats=4, note="hi")
    assert out == {"team_name": "Tuple", "seats": 4, "note": "hi", "submitted_at": "first", "counted": False}
    assert store.rows["user_t"]["submitter_email"] == "founder@tuple.com"

    kwargs = email_service.send_email.await_args.kwargs
    assert kwargs["to"] == launch_service.settings.EMAIL_SUPPORT_ADDRESS
    assert kwargs["reply_to"] == "founder@tuple.com"
    assert kwargs["kind"] == "launch_team_interest"
    assert "Tuple" in kwargs["subject"] and "4 seats" in kwargs["subject"]
    # The pot contribution the founders would otherwise compute by hand, and
    # the standing rule about what a winning team's free month covers.
    for body in (kwargs["html"], kwargs["text"]):
        assert "$30.00" in body  # $10 per entrant + 4 × $5 per seat
        assert "Workspace plan" in body


@patch("core.services.launch_sheet_email.email_service.send_email", AsyncMock(return_value="msg_1"))
async def test_a_counted_team_cannot_raise_its_own_pot_contribution(store):
    """A submitter must not be able to move the number the founders approved."""
    await launch_service.submit_team_interest(AUTH, team_name="Tuple", seats=5, note="")
    await launch_service.set_team_entry("user_t", seats=None, counted=True)
    assert await launch_service.counted_team_seats() == [5]

    await launch_service.submit_team_interest(AUTH, team_name="Tuple", seats=500, note="")
    assert await launch_service.counted_team_seats() == [5]


@patch("core.services.launch_sheet_email.email_service.send_email", AsyncMock(return_value="msg_1"))
async def test_submission_is_idempotent_per_submitter(store):
    await launch_service.submit_team_interest(AUTH, team_name="Tuple", seats=4, note="")
    await launch_service.submit_team_interest(AUTH, team_name="Tuple Inc", seats=9, note="grew")
    assert len(store.rows) == 1
    row = store.rows["user_t"]
    assert row["team_name"] == "Tuple Inc" and row["seats"] == 9
    assert row["submitted_at"] == "first"  # the queue position survives an edit


@patch(
    "core.services.launch_sheet_email.email_service.send_email",
    AsyncMock(side_effect=RuntimeError("SES down")),
)
async def test_a_failed_founder_email_never_fails_the_submission(store):
    out = await launch_service.submit_team_interest(AUTH, team_name="Tuple", seats=2, note="")
    assert out["team_name"] == "Tuple" and store.rows["user_t"]["seats"] == 2


@patch("core.services.launch_sheet_email.email_service.send_email", AsyncMock(return_value="msg_1"))
async def test_a_submission_is_never_counted_until_an_admin_says_so(store):
    """An unreviewed submission must contribute NOTHING to the pot."""
    await launch_service.submit_team_interest(AUTH, team_name="Tuple", seats=4, note="")
    assert await launch_service.counted_team_seats() == []

    await launch_service.set_team_entry("user_t", seats=None, counted=True)
    assert await launch_service.counted_team_seats() == [4]

    await launch_service.set_team_entry("user_t", seats=None, counted=False)
    assert await launch_service.counted_team_seats() == []


@patch("core.services.launch_sheet_email.email_service.send_email", AsyncMock(return_value="msg_1"))
async def test_the_admin_can_correct_the_seat_count(store):
    await launch_service.submit_team_interest(AUTH, team_name="Tuple", seats=4, note="")
    row = await launch_service.set_team_entry("user_t", seats=6, counted=True)
    assert row["seats"] == 6 and row["counted"] is True
    assert await launch_service.counted_team_seats() == [6]


@pytest.mark.parametrize("seats", [0, -1, 501])
async def test_the_admin_seat_edit_is_bounded(store, seats):
    with pytest.raises(launch_settings.SettingsInvalid) as e:
        await launch_service.set_team_entry("user_t", seats=seats, counted=None)
    assert "seats" in e.value.errors


async def test_editing_an_unknown_owner_is_a_field_error_not_a_500(store):
    with pytest.raises(launch_settings.SettingsInvalid) as e:
        await launch_service.set_team_entry("user_nope", seats=None, counted=True)
    assert "owner_id" in e.value.errors


async def test_the_contribution_is_ten_dollars_plus_five_per_seat(store):
    assert launch_service.team_contribution_cents(0) == 1_000
    assert launch_service.team_contribution_cents(1) == 1_500
    assert launch_service.team_contribution_cents(5) == 3_500
