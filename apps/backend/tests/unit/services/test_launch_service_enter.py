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


@pytest.fixture
def deps():
    with (
        patch("core.services.launch_service.billing_repo.get_by_owner_id", AsyncMock(return_value=ACTIVE)),
        patch(
            "core.services.launch_service.readiness_state.resolve_onboarding_state", AsyncMock(return_value=ACTIVATED)
        ),
        patch("core.services.launch_service.launch_waitlist_repo.get", AsyncMock(return_value={"position": 12})),
        patch("core.services.launch_service.launch_revenue.validate_key", AsyncMock()) as validate,
        patch(
            "core.services.launch_service.launch_revenue.sum_net_revenue", AsyncMock(return_value=(80_00, "usd"))
        ) as summer,
        patch("core.services.launch_service.launch_revenue.store_key", AsyncMock()) as store,
        patch(
            "core.services.launch_service.launch_entries_repo.upsert",
            AsyncMock(
                side_effect=lambda o, **kw: {
                    "owner_id": o,
                    **kw,
                    "entered_at": "t",
                    "window_cents": 0,
                    "delta_cents": 0,
                }
            ),
        ) as upsert,
    ):
        yield {"validate": validate, "sum": summer, "store": store, "upsert": upsert}


async def test_enter_happy_path_stores_key_and_baseline(deps):
    auth = AuthContext(user_id="user_1", email="me@x.com")
    body = await launch_service.enter(auth, S, display_name="Ada", stripe_key="rk_test_x", public=True)
    deps["validate"].assert_awaited_once_with("rk_test_x")
    deps["store"].assert_awaited_once_with("user_1", "rk_test_x")
    start, end = deps["sum"].await_args.args[1], deps["sum"].await_args.args[2]
    assert end == S.competition_start_at and start == S.competition_start_at - timedelta(days=30)
    deps["upsert"].assert_awaited_once_with(
        "user_1", display_name="Ada", public=True, waitlist_position=12, currency="usd", baseline_cents=8000
    )
    assert body["display_name"] == "Ada" and body["baseline_cents"] == 8000


async def test_enter_refuses_when_gate_fails(deps):
    with patch("core.services.launch_service.billing_repo.get_by_owner_id", AsyncMock(return_value=None)):
        with pytest.raises(launch_service.EnterRefused) as e:
            await launch_service.enter(
                AuthContext(user_id="user_1"), S, display_name="Ada", stripe_key="rk_test_x", public=True
            )
    assert e.value.reason == "no_subscription"
    deps["store"].assert_not_awaited()


async def test_enter_maps_key_rejection_and_never_stores(deps):
    deps["validate"].side_effect = launch_revenue.KeyRejected("stripe_auth")
    with pytest.raises(launch_service.EnterRefused) as e:
        await launch_service.enter(
            AuthContext(user_id="user_1"), S, display_name="Ada", stripe_key="sk_live_x", public=True
        )
    assert e.value.reason == "stripe_auth"
    deps["store"].assert_not_awaited()


async def test_enter_mixed_currency_refused(deps):
    deps["sum"].side_effect = launch_revenue.MixedCurrency("usd vs eur")
    with pytest.raises(launch_service.EnterRefused) as e:
        await launch_service.enter(
            AuthContext(user_id="user_1"), S, display_name="Ada", stripe_key="rk_test_x", public=True
        )
    assert e.value.reason == "mixed_currency"


def test_rank_orders_and_computes_pct():
    rows = launch_service.rank(
        [
            {
                "owner_id": "a",
                "display_name": "A",
                "public": True,
                "delta_cents": 500,
                "baseline_cents": 1000,
                "currency": "usd",
                "entered_at": "2",
            },
            {
                "owner_id": "b",
                "display_name": "B",
                "public": True,
                "delta_cents": 500,
                "baseline_cents": 0,
                "currency": "usd",
                "entered_at": "1",
            },
            {
                "owner_id": "c",
                "display_name": "C",
                "public": False,
                "delta_cents": 9999,
                "baseline_cents": 1,
                "currency": "usd",
                "entered_at": "0",
            },
            {
                "owner_id": "d",
                "display_name": "D",
                "public": True,
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
