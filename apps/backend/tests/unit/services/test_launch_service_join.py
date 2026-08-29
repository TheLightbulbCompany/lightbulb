from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import launch_phase, launch_revenue, launch_service

NOW = datetime.now(timezone.utc)
DROP = NOW - timedelta(days=1)
S = launch_phase.Schedule(DROP, DROP + timedelta(hours=72), DROP + timedelta(hours=72, days=30), 220, 72, 30)
ACTIVE = {"subscription_status": "active"}
ACTIVATED = {"bootstrap_state": "delivered", "bootstrap_delivered_at": "x"}


# --- join() -------------------------------------------------------------


@pytest.fixture
def join_deps():
    with (
        patch("core.services.launch_service.billing_repo.get_by_owner_id", AsyncMock(return_value=ACTIVE)),
        patch(
            "core.services.launch_service.readiness_state.resolve_onboarding_state", AsyncMock(return_value=ACTIVATED)
        ),
        patch("core.services.launch_service.launch_waitlist_repo.get", AsyncMock(return_value={"position": 12})),
        patch("core.services.launch_service.launch_revenue.store_key", AsyncMock()) as store,
        patch(
            "core.services.launch_service.launch_entries_repo.join",
            AsyncMock(
                side_effect=lambda o, **kw: {
                    "owner_id": o,
                    **kw,
                    "entered_at": "t",
                    "has_key": False,
                    "baseline_cents": 0,
                    "window_cents": 0,
                    "delta_cents": 0,
                }
            ),
        ) as join_mock,
    ):
        yield {"join": join_mock, "store": store}


async def test_join_happy_path_creates_entry_without_touching_stripe(join_deps):
    auth = AuthContext(user_id="user_1", email="me@x.com")
    body = await launch_service.join(auth, S, display_name="Ada", public=True)
    join_deps["join"].assert_awaited_once_with("user_1", display_name="Ada", public=True, waitlist_position=12)
    join_deps["store"].assert_not_awaited()
    assert body["display_name"] == "Ada" and body["has_key"] is False


async def test_join_refuses_when_gate_fails(join_deps):
    with patch("core.services.launch_service.billing_repo.get_by_owner_id", AsyncMock(return_value=None)):
        with pytest.raises(launch_service.LaunchRefused) as e:
            await launch_service.join(AuthContext(user_id="user_1"), S, display_name="Ada", public=True)
    assert e.value.reason == "no_subscription"
    join_deps["join"].assert_not_awaited()


async def test_join_passes_no_stripe_fields_to_the_repo(join_deps):
    auth = AuthContext(user_id="user_1", email="me@x.com")
    await launch_service.join(auth, S, display_name="Ada", public=False)
    kwargs = join_deps["join"].await_args.kwargs
    assert "stripe_key" not in kwargs and "currency" not in kwargs and "baseline_cents" not in kwargs


# --- connect_stripe() -----------------------------------------------------


@pytest.fixture
def connect_deps():
    existing = {"owner_id": "user_1", "display_name": "Ada", "public": True, "has_key": False}
    with (
        patch(
            "core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=dict(existing))
        ) as get_mock,
        patch("core.services.launch_service.launch_revenue.validate_key", AsyncMock()) as validate,
        patch(
            "core.services.launch_service.launch_revenue.sum_net_revenue", AsyncMock(return_value=(80_00, "usd"))
        ) as summer,
        patch("core.services.launch_service.launch_revenue.store_key", AsyncMock()) as store,
        patch("core.services.launch_service.launch_entries_repo.set_key_connected", AsyncMock()) as set_key,
    ):
        yield {"get": get_mock, "validate": validate, "sum": summer, "store": store, "set_key": set_key}


async def test_connect_stripe_happy_path_stores_key_and_baseline(connect_deps):
    connect_deps["get"].side_effect = [
        {"owner_id": "user_1", "display_name": "Ada", "public": True, "has_key": False},
        {
            "owner_id": "user_1",
            "display_name": "Ada",
            "public": True,
            "has_key": True,
            "currency": "usd",
            "baseline_cents": 8000,
        },
    ]
    auth = AuthContext(user_id="user_1", email="me@x.com")
    body = await launch_service.connect_stripe(auth, S, stripe_key="rk_test_x")
    connect_deps["validate"].assert_awaited_once_with("rk_test_x")
    connect_deps["store"].assert_awaited_once_with("user_1", "rk_test_x")
    start, end = connect_deps["sum"].await_args.args[1], connect_deps["sum"].await_args.args[2]
    assert end == S.competition_start_at and start == S.competition_start_at - timedelta(days=30)
    connect_deps["set_key"].assert_awaited_once()
    kwargs = connect_deps["set_key"].await_args.kwargs
    assert kwargs["currency"] == "usd" and kwargs["baseline_cents"] == 8000 and kwargs["baseline_at"]
    assert body["has_key"] is True and body["baseline_cents"] == 8000


async def test_connect_stripe_refuses_without_an_entry(connect_deps):
    connect_deps["get"].return_value = None
    with pytest.raises(launch_service.LaunchRefused) as e:
        await launch_service.connect_stripe(AuthContext(user_id="user_1"), S, stripe_key="rk_test_x")
    assert e.value.reason == "no_entry"
    connect_deps["validate"].assert_not_awaited()
    connect_deps["store"].assert_not_awaited()


async def test_connect_stripe_maps_key_rejection_and_never_stores(connect_deps):
    connect_deps["validate"].side_effect = launch_revenue.KeyRejected("stripe_auth")
    with pytest.raises(launch_service.LaunchRefused) as e:
        await launch_service.connect_stripe(AuthContext(user_id="user_1"), S, stripe_key="sk_live_x")
    assert e.value.reason == "stripe_auth"
    connect_deps["store"].assert_not_awaited()
    connect_deps["set_key"].assert_not_awaited()


async def test_connect_stripe_mixed_currency_refused(connect_deps):
    connect_deps["sum"].side_effect = launch_revenue.MixedCurrency("usd vs eur")
    with pytest.raises(launch_service.LaunchRefused) as e:
        await launch_service.connect_stripe(AuthContext(user_id="user_1"), S, stripe_key="rk_test_x")
    assert e.value.reason == "mixed_currency"
    connect_deps["store"].assert_not_awaited()


async def test_connect_stripe_stores_only_after_validate_and_baseline_succeed(connect_deps):
    connect_deps["sum"].side_effect = launch_revenue.KeyRejected("stripe_permission")
    with pytest.raises(launch_service.LaunchRefused):
        await launch_service.connect_stripe(AuthContext(user_id="user_1"), S, stripe_key="rk_test_x")
    connect_deps["store"].assert_not_awaited()
    connect_deps["set_key"].assert_not_awaited()


async def test_connect_stripe_on_an_already_keyed_entry_replaces_the_key(connect_deps):
    connect_deps["get"].side_effect = [
        {"owner_id": "user_1", "display_name": "Ada", "has_key": True, "currency": "usd", "baseline_cents": 100},
        {"owner_id": "user_1", "display_name": "Ada", "has_key": True, "currency": "usd", "baseline_cents": 8000},
    ]
    await launch_service.connect_stripe(AuthContext(user_id="user_1"), S, stripe_key="rk_test_new")
    connect_deps["validate"].assert_awaited_once_with("rk_test_new")
    connect_deps["set_key"].assert_awaited_once()


# --- set_public() -----------------------------------------------------------


async def test_set_public_flips_the_flag_on_an_existing_entry():
    with (
        patch(
            "core.services.launch_service.launch_entries_repo.get",
            AsyncMock(
                side_effect=[
                    {"owner_id": "user_1", "display_name": "Ada", "public": True},
                    {"owner_id": "user_1", "display_name": "Ada", "public": False},
                ]
            ),
        ) as get_mock,
        patch("core.services.launch_service.launch_entries_repo.set_public", AsyncMock()) as set_mock,
    ):
        body = await launch_service.set_public(AuthContext(user_id="user_1"), S, public=False)
    set_mock.assert_awaited_once_with("user_1", False)
    assert get_mock.await_count == 2
    assert body["public"] is False


async def test_set_public_refuses_without_an_entry():
    with (
        patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=None)),
        patch("core.services.launch_service.launch_entries_repo.set_public", AsyncMock()) as set_mock,
    ):
        with pytest.raises(launch_service.LaunchRefused) as e:
            await launch_service.set_public(AuthContext(user_id="user_1"), S, public=True)
    assert e.value.reason == "no_entry"
    set_mock.assert_not_awaited()


# --- rank / leaderboard ----------------------------------------------------


def test_rank_only_considers_entries_with_a_connected_key():
    rows = launch_service.rank(
        [
            {
                "owner_id": "a",
                "display_name": "A",
                "has_key": True,
                "delta_cents": 500,
                "baseline_cents": 1000,
                "currency": "usd",
                "entered_at": "2",
            },
            {
                "owner_id": "b",
                "display_name": "B",
                "has_key": True,
                "delta_cents": 500,
                "baseline_cents": 0,
                "currency": "usd",
                "entered_at": "1",
            },
            {
                "owner_id": "c",
                "display_name": "C",
                "has_key": False,  # joined, never connected — must not rank
                "delta_cents": 9999,
                "baseline_cents": 1,
                "currency": "usd",
                "entered_at": "0",
            },
            {
                "owner_id": "d",
                "display_name": "D",
                "has_key": True,
                "delta_cents": -20,
                "baseline_cents": 100,
                "currency": "eur",
                "entered_at": "3",
            },
        ]
    )
    assert [r["display_name"] for r in rows] == ["B", "A", "D"]
    assert rows[0] == {"rank": 1, "display_name": "B", "delta_cents": 500, "pct": None, "currency": "usd"}
    assert rows[1]["pct"] == 0.5 and rows[2]["pct"] == -0.2


def test_rank_excludes_private_entrants_even_with_a_connected_key():
    """public=False means "don't show my name" — has_key alone must not
    override that privacy choice; only has_key AND public rank."""
    rows = launch_service.rank(
        [
            {
                "owner_id": "a",
                "display_name": "Private",
                "has_key": True,
                "public": False,
                "delta_cents": 999,
                "baseline_cents": 100,
                "currency": "usd",
                "entered_at": "1",
            },
            {
                "owner_id": "b",
                "display_name": "Public",
                "has_key": True,
                "public": True,
                "delta_cents": 5,
                "baseline_cents": 10,
                "currency": "usd",
                "entered_at": "2",
            },
        ]
    )
    assert [r["display_name"] for r in rows] == ["Public"]


def test_rank_pct_uses_the_prorated_baseline_when_present():
    """delta_cents is measured against the prorated baseline; pct must match, or
    the board understates everyone's growth until the last day."""
    rows = launch_service.rank(
        [
            {
                "display_name": "A",
                "has_key": True,
                "delta_cents": 500,
                "baseline_cents": 10_000,
                "baseline_prorated_cents": 1_000,
                "currency": "usd",
                "entered_at": "1",
            },
            {
                "display_name": "B",
                "has_key": True,
                "delta_cents": 100,
                "baseline_cents": 1_000,
                "baseline_prorated_cents": 0,
                "currency": "usd",
                "entered_at": "2",
            },
        ]
    )
    assert rows[0]["pct"] == 0.5  # 500 / 1_000, not 500 / 10_000
    assert rows[1]["pct"] == 0.1  # prorated 0 ⇒ falls back to the full baseline


async def test_leaderboard_returns_competing_and_on_board_counts():
    entries = [
        {
            "owner_id": "a",
            "display_name": "A",
            "has_key": True,
            "public": True,
            "delta_cents": 10,
            "baseline_cents": 0,
            "currency": "usd",
            "entered_at": "1",
        },
        {
            "owner_id": "b",
            "display_name": "B",
            "has_key": False,
            "delta_cents": 0,
            "baseline_cents": 0,
            "currency": "usd",
            "entered_at": "2",
        },
        {
            "owner_id": "c",
            "display_name": "C",
            "has_key": True,
            "public": False,  # private but keyed — counts toward on_board, absent from rows
            "delta_cents": 999,
            "baseline_cents": 0,
            "currency": "usd",
            "entered_at": "3",
        },
    ]
    with patch("core.services.launch_service.launch_entries_repo.list_all", AsyncMock(return_value=entries)):
        result = await launch_service.leaderboard()
    # on_board counts every has_key entry, including private ones — it can be > len(rows).
    assert result["competing"] == 3 and result["on_board"] == 2
    assert len(result["rows"]) == 1 and result["rows"][0]["display_name"] == "A"
    assert "C" not in [r["display_name"] for r in result["rows"]]
