from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext, require_platform_admin
from core.services import launch_settings


@pytest.fixture
def admin_client(app, async_client):
    app.dependency_overrides[require_platform_admin] = lambda: AuthContext(user_id="user_admin")
    yield async_client
    app.dependency_overrides.pop(require_platform_admin, None)


@patch("core.services.ops.launch.compute", AsyncMock(return_value=({"phase": "live"}, "ok", [])))
async def test_ops_launch_envelope(admin_client):
    r = await admin_client.get("/api/v1/ops/launch")
    assert r.status_code == 200 and r.json()["data"]["phase"] == "live" and r.json()["source_state"] == "ok"


@patch("core.services.launch_revenue.refresh_all", AsyncMock(return_value=7))
async def test_ops_launch_refresh_forces(admin_client):
    r = await admin_client.post("/api/v1/ops/launch/refresh")
    assert r.status_code == 200 and r.json() == {"refreshed": 7}


@patch("core.services.launch_revenue.refresh_all", AsyncMock(return_value=7))
async def test_ops_launch_refresh_drops_the_public_response_cache(admin_client):
    # The admin cache is not the only one — /launch and /launch/leaderboard hold
    # their own 20-30s response cache, and a judging-day refresh must land there.
    with patch("routers.launch.clear_response_cache") as clear:
        r = await admin_client.post("/api/v1/ops/launch/refresh")
    assert r.status_code == 200
    clear.assert_called_once()


async def test_ops_launch_requires_admin(async_client):
    r = await async_client.get("/api/v1/ops/launch")
    assert r.status_code in (401, 403)


async def test_ops_launch_delete_entry_removes_row_and_key_in_order(admin_client):
    # Key first: a dangling Secrets Manager entry with no owning row is worse
    # than a row that still points at an already-revoked key.
    calls: list[str] = []

    async def _delete_key(owner_id: str) -> None:
        calls.append(f"key:{owner_id}")

    async def _delete_row(owner_id: str) -> None:
        calls.append(f"row:{owner_id}")

    with (
        patch("core.services.launch_revenue.delete_key", AsyncMock(side_effect=_delete_key)),
        patch("core.repositories.launch_entries_repo.delete", AsyncMock(side_effect=_delete_row)),
    ):
        r = await admin_client.delete("/api/v1/ops/launch/entries/user_1")
    assert r.status_code == 200 and r.json() == {"deleted": "user_1"}
    assert calls == ["key:user_1", "row:user_1"]


async def test_ops_launch_delete_entry_drops_the_public_response_cache(admin_client):
    # A taken-down entrant has to leave the public board now, not up to 30s later.
    with (
        patch("core.services.launch_revenue.delete_key", AsyncMock()),
        patch("core.repositories.launch_entries_repo.delete", AsyncMock()),
        patch("routers.launch.clear_response_cache") as clear,
    ):
        r = await admin_client.delete("/api/v1/ops/launch/entries/user_1")
    assert r.status_code == 200
    clear.assert_called_once()


async def test_ops_launch_delete_entry_requires_admin(async_client):
    r = await async_client.delete("/api/v1/ops/launch/entries/user_1")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET/PUT /ops/launch/settings
# ---------------------------------------------------------------------------


@pytest.fixture
def audited():
    """Every write route below is @audit_admin_action-decorated. Patch the
    repo write so the decorator's synchronous audit write never reaches real
    DynamoDB."""
    with patch("core.repositories.admin_actions_repo.create", AsyncMock(return_value={})) as m:
        yield m


async def test_ops_launch_settings_get_returns_defaults_when_no_row(admin_client):
    with patch("core.repositories.launch_settings_repo.get", AsyncMock(return_value=None)):
        r = await admin_client.get("/api/v1/ops/launch/settings")
    assert r.status_code == 200
    # mint_nonce is server-only — to_public strips it from every read, GET included.
    assert r.json() == {k: v for k, v in launch_settings.DEFAULTS.items() if k != "mint_nonce"}


async def test_ops_launch_settings_get_requires_admin(async_client):
    r = await async_client.get("/api/v1/ops/launch/settings")
    assert r.status_code in (401, 403)


_FULL_PUT_BODY = {
    "enabled": True,
    "drop_at": "2026-09-18T00:00:00-04:00",
    "competition_start_at": "2026-09-21T00:00:00-04:00",
    "comp_slots": 220,
    "competition_days": 30,
    "pot_front_cents": 100_000,
    "pot_per_entrant_cents": 1_000,
}


async def test_ops_launch_settings_put_422_on_invalid_payload(admin_client, audited):
    r = await admin_client.put(
        "/api/v1/ops/launch/settings",
        json={**_FULL_PUT_BODY, "drop_at": None, "competition_start_at": None},
    )
    assert r.status_code == 422
    # FastAPI's default HTTPException envelope — the codebase's one 422
    # convention (Task 4 reads `body.detail.errors`, not `body.errors`).
    assert "enabled" in r.json()["detail"]["errors"]


async def test_ops_launch_settings_put_success_writes_row_audits_and_clears_caches(admin_client, audited):
    saved_row = {
        "enabled": True,
        "drop_at": "2026-09-18T00:00:00-04:00",
        "competition_start_at": "2026-09-21T00:00:00-04:00",
        "comp_slots": 220,
        "competition_days": 30,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
        "stripe_coupon_id": "",
        "stripe_promotion_code_id": "",
        "stripe_promotion_code": "",
        "updated_at": "2026-08-29T00:00:00+00:00",
        "updated_by": "user_admin",
    }
    with (
        patch("core.repositories.launch_settings_repo.get", AsyncMock(return_value=None)),
        patch("core.repositories.launch_settings_repo.put", AsyncMock(return_value=saved_row)),
        patch("core.services.launch_phase.invalidate") as invalidate,
        patch("routers.launch.clear_response_cache") as clear_response_cache,
        patch("core.services.ops.launch.clear_cache") as clear_ops_cache,
    ):
        r = await admin_client.put("/api/v1/ops/launch/settings", json=_FULL_PUT_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and "updated_by" not in body
    invalidate.assert_called_once()
    clear_response_cache.assert_called_once()
    clear_ops_cache.assert_called_once()
    audited.assert_awaited_once()  # the write left an audit trail


async def test_ops_launch_settings_put_rejects_a_partial_body(admin_client, audited):
    """The PUT is a full replace, so every field is required: a partial body
    is a 422, never a silent reset of the dates it left out."""
    r = await admin_client.put(
        "/api/v1/ops/launch/settings",
        json={k: v for k, v in _FULL_PUT_BODY.items() if k != "drop_at"},
    )
    assert r.status_code == 422


async def test_ops_launch_settings_put_audits_exactly_the_seven_editable_fields(admin_client, audited):
    """The audit body is the parsed model — the three server-managed stripe_*
    ids are not on it, so an admin save no longer logs empty coupon defaults
    that it never wrote."""
    with (
        patch("core.repositories.launch_settings_repo.get", AsyncMock(return_value=None)),
        patch("core.repositories.launch_settings_repo.put", AsyncMock(side_effect=lambda item: item)),
        patch("core.services.launch_settings._clear_caches"),
    ):
        r = await admin_client.put("/api/v1/ops/launch/settings", json=_FULL_PUT_BODY)
    assert r.status_code == 200
    body = audited.await_args.kwargs["payload"]["body"]
    assert set(body) == set(_FULL_PUT_BODY)


async def test_ops_launch_settings_put_requires_admin(async_client):
    r = await async_client.put("/api/v1/ops/launch/settings", json={})
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /ops/launch/settings/stripe-coupon
# ---------------------------------------------------------------------------


async def test_ops_launch_stripe_coupon_conflict_already_configured(admin_client, audited):
    with patch(
        "core.services.launch_settings.create_stripe_coupon",
        AsyncMock(side_effect=launch_settings.CouponAlreadyConfigured()),
    ):
        r = await admin_client.post("/api/v1/ops/launch/settings/stripe-coupon", json={})
    assert r.status_code == 409 and r.json()["detail"] == "already_configured"


async def test_ops_launch_stripe_coupon_missing_start_date_422(admin_client, audited):
    with patch(
        "core.services.launch_settings.create_stripe_coupon",
        AsyncMock(side_effect=launch_settings.SettingsInvalid({"competition_start_at": "must be set"})),
    ):
        r = await admin_client.post("/api/v1/ops/launch/settings/stripe-coupon", json={})
    assert r.status_code == 422
    assert "competition_start_at" in r.json()["detail"]["errors"]


async def test_ops_launch_stripe_coupon_stripe_error_502_no_message_leak(admin_client, audited):
    with patch(
        "core.services.launch_settings.create_stripe_coupon",
        AsyncMock(side_effect=launch_settings.StripeMintError()),
    ):
        r = await admin_client.post("/api/v1/ops/launch/settings/stripe-coupon", json={})
    assert r.status_code == 502
    assert r.json() == {"detail": "stripe_error"}  # the real Stripe message never reaches the client


async def test_ops_launch_stripe_coupon_force_flag_reaches_the_service(admin_client, audited):
    mock = AsyncMock(
        return_value={
            "stripe_coupon_id": "coupon_1",
            "stripe_promotion_code_id": "promo_1",
            "stripe_promotion_code": "LIGHTBULB-ABC123",
        }
    )
    with patch("core.services.launch_settings.create_stripe_coupon", mock):
        r = await admin_client.post("/api/v1/ops/launch/settings/stripe-coupon", json={"force": True})
    assert r.status_code == 200
    body = r.json()
    assert body["stripe_coupon_id"] == "coupon_1"
    assert body["stripe_promotion_code_id"] == "promo_1"
    assert body["stripe_promotion_code"] == "LIGHTBULB-ABC123"
    assert mock.call_args.kwargs["force"] is True


async def test_ops_launch_stripe_coupon_requires_admin(async_client):
    r = await async_client.post("/api/v1/ops/launch/settings/stripe-coupon", json={})
    assert r.status_code in (401, 403)
