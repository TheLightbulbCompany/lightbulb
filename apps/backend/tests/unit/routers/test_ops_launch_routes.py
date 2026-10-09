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


async def test_ops_launch_delete_entry_removes_row_and_key_in_order(admin_client, audited):
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
    assert r.status_code == 200 and r.json()["deleted"] == "user_1"
    assert calls == ["key:user_1", "row:user_1"]


async def test_ops_launch_delete_entry_drops_the_public_response_cache(admin_client, audited):
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
    # mint_nonce is server-only — to_admin strips it from every read, GET included.
    assert r.json() == {k: v for k, v in launch_settings.DEFAULTS.items() if k != "mint_nonce"}


async def test_ops_launch_settings_get_requires_admin(async_client):
    r = await async_client.get("/api/v1/ops/launch/settings")
    assert r.status_code in (401, 403)


_FULL_PUT_BODY = {
    "enabled": True,
    "sheet_closes_at": "2026-09-30T12:00:00-04:00",
    "competition_start_at": "2026-10-01T00:00:00-04:00",
    "competition_end_at": "2026-12-01T00:00:00-05:00",
    "comp_slots": 200,
    "pot_front_cents": 100_000,
    "pot_per_entrant_cents": 1_000,
    "pot_per_seat_cents": 500,
    "referral_jump": 10,
}


async def test_ops_launch_settings_put_422_on_invalid_payload(admin_client, audited):
    r = await admin_client.put(
        "/api/v1/ops/launch/settings",
        json={**_FULL_PUT_BODY, "sheet_closes_at": None, "competition_start_at": None},
    )
    assert r.status_code == 422
    # FastAPI's default HTTPException envelope — the codebase's one 422
    # convention (Task 4 reads `body.detail.errors`, not `body.errors`).
    assert "enabled" in r.json()["detail"]["errors"]


async def test_ops_launch_settings_put_success_writes_row_audits_and_clears_caches(admin_client, audited):
    saved_row = {
        "enabled": True,
        "sheet_closes_at": "2026-09-30T12:00:00-04:00",
        "competition_start_at": "2026-10-01T00:00:00-04:00",
        "competition_end_at": "2026-12-01T00:00:00-05:00",
        "comp_slots": 200,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
        "pot_per_seat_cents": 500,
        "stripe_coupon_id": "",
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
    assert body["enabled"] is True and "mint_nonce" not in body
    invalidate.assert_called_once()
    clear_response_cache.assert_called_once()
    clear_ops_cache.assert_called_once()
    audited.assert_awaited_once()  # the write left an audit trail


async def test_ops_launch_settings_put_rejects_a_partial_body(admin_client, audited):
    """The PUT is a full replace, so every field is required: a partial body
    is a 422, never a silent reset of the dates it left out."""
    r = await admin_client.put(
        "/api/v1/ops/launch/settings",
        json={k: v for k, v in _FULL_PUT_BODY.items() if k != "sheet_closes_at"},
    )
    assert r.status_code == 422


async def test_ops_launch_settings_put_422_without_referral_jump(admin_client, audited):
    r = await admin_client.put(
        "/api/v1/ops/launch/settings",
        json={k: v for k, v in _FULL_PUT_BODY.items() if k != "referral_jump"},
    )
    assert r.status_code == 422


async def test_ops_launch_settings_put_audits_exactly_the_eight_editable_fields(admin_client, audited):
    """The audit body is the parsed model — the server-managed coupon and
    close-state fields are not on it."""
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


async def test_ops_launch_stripe_coupon_invalid_settings_422(admin_client, audited):
    with patch(
        "core.services.launch_settings.create_stripe_coupon",
        AsyncMock(side_effect=launch_settings.SettingsInvalid({"stripe_coupon_id": "redeem-by past"})),
    ):
        r = await admin_client.post("/api/v1/ops/launch/settings/stripe-coupon", json={})
    assert r.status_code == 422
    assert "stripe_coupon_id" in r.json()["detail"]["errors"]


async def test_ops_launch_stripe_coupon_stripe_error_502_no_message_leak(admin_client, audited):
    with patch(
        "core.services.launch_settings.create_stripe_coupon",
        AsyncMock(side_effect=launch_settings.StripeMintError()),
    ):
        r = await admin_client.post("/api/v1/ops/launch/settings/stripe-coupon", json={})
    assert r.status_code == 502
    assert r.json() == {"detail": "stripe_error"}  # the real Stripe message never reaches the client


async def test_ops_launch_stripe_coupon_force_flag_reaches_the_service(admin_client, audited):
    mock = AsyncMock(return_value={"stripe_coupon_id": "coupon_1"})
    with patch("core.services.launch_settings.create_stripe_coupon", mock):
        r = await admin_client.post("/api/v1/ops/launch/settings/stripe-coupon", json={"force": True})
    assert r.status_code == 200
    assert r.json()["stripe_coupon_id"] == "coupon_1"  # (+ the audit decorator's status field)
    assert mock.call_args.kwargs["force"] is True


async def test_ops_launch_stripe_coupon_requires_admin(async_client):
    r = await async_client.post("/api/v1/ops/launch/settings/stripe-coupon", json={})
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /ops/launch/close
# ---------------------------------------------------------------------------


async def test_ops_launch_close_starts_the_background_task_and_202s(admin_client, audited):
    """The close is async — the endpoint only STARTS it (hundreds of Stripe
    calls would blow the API-GW timeout) and 202s; the summary lands on the
    settings row as close_result."""
    with (
        patch("core.services.launch_close.start_close", AsyncMock()) as start,
        patch("core.services.ops.launch.clear_cache") as clear_ops,
        patch("routers.launch.clear_response_cache") as clear_public,
    ):
        r = await admin_client.post("/api/v1/ops/launch/close", json={})
    assert r.status_code == 202
    assert r.json()["status"] == "started"
    assert start.call_args.kwargs["force"] is False
    clear_ops.assert_called_once()
    clear_public.assert_called_once()
    audited.assert_awaited_once()


async def test_ops_launch_close_already_closed_409(admin_client, audited):
    from core.services import launch_close

    with patch("core.services.launch_close.start_close", AsyncMock(side_effect=launch_close.AlreadyClosed())):
        r = await admin_client.post("/api/v1/ops/launch/close", json={})
    assert r.status_code == 409 and r.json()["detail"] == "already_closed"


async def test_ops_launch_close_refuses_while_a_close_is_running(admin_client, audited):
    from core.services import launch_close

    with patch("core.services.launch_close.start_close", AsyncMock(side_effect=launch_close.CloseRunning())):
        r = await admin_client.post("/api/v1/ops/launch/close", json={})
    assert r.status_code == 409 and r.json()["detail"] == "close_running"


async def test_ops_launch_close_force_reaches_the_service(admin_client, audited):
    with patch("core.services.launch_close.start_close", AsyncMock()) as start:
        r = await admin_client.post("/api/v1/ops/launch/close", json={"force": True})
    assert r.status_code == 202
    assert start.call_args.kwargs["force"] is True


async def test_ops_launch_close_requires_admin(async_client):
    r = await async_client.post("/api/v1/ops/launch/close", json={})
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Admin-editable email copy: GET / PUT / test send
# ---------------------------------------------------------------------------

_ROW = {
    **launch_settings.DEFAULTS,
    "enabled": True,
    "comp_slots": 200,
    "referral_jump": 10,
    "email_copy": {"competing": {"lead": "hand-written lead"}},
}


async def test_ops_launch_email_copy_returns_every_variant_with_a_preview(admin_client):
    with patch("core.services.launch_settings.effective", AsyncMock(return_value=_ROW)):
        r = await admin_client.get("/api/v1/ops/launch/email-copy")
    assert r.status_code == 200
    variants = r.json()["variants"]
    assert [v["variant"] for v in variants] == [
        "join",
        "winner_paid",
        "winner_unpaid",
        "competing",
        "not_selected",
    ]
    competing = next(v for v in variants if v["variant"] == "competing")
    assert competing["override"] == {"lead": "hand-written lead"}
    assert competing["defaults"]["lead"].startswith("The sign-up sheet is closed")
    assert competing["placeholders"] == ["comp_slots"]
    # The preview renders the EFFECTIVE copy (override applied), as HTML.
    assert "hand-written lead" in competing["preview_html"]
    # A standalone document carrying its OWN light ground — the body fragment
    # hardcodes a dark text colour, so the preview must not inherit the admin
    # console's (possibly dark) theme.
    assert competing["preview_html"].startswith("<!doctype html>")
    assert "background:#ffffff" in competing["preview_html"]


async def test_ops_launch_email_copy_put_saves_one_variant(admin_client):
    with patch(
        "core.services.launch_settings.update_email_copy",
        AsyncMock(return_value={"competing": {"lead": "new"}}),
    ) as upd:
        r = await admin_client.put("/api/v1/ops/launch/email-copy", json={"variant": "competing", "lead": "new"})
    # `audit_status` rides along from @audit_admin_action's panic path (no
    # audit table in unit tests) — assert the payload, not the envelope.
    assert r.status_code == 200
    body = r.json()
    assert body["variant"] == "competing" and body["override"] == {"lead": "new"}
    assert upd.await_args.args[1] == "competing"
    assert "variant" not in upd.await_args.args[2]


async def test_ops_launch_email_copy_put_422s_on_an_unsupported_placeholder(admin_client):
    r = await admin_client.put(
        "/api/v1/ops/launch/email-copy",
        json={"variant": "winner_paid", "detail": "hello {referral_link}"},
    )
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert "referral_link" in errors["detail"]


async def test_ops_launch_email_copy_requires_admin(async_client):
    r = await async_client.get("/api/v1/ops/launch/email-copy")
    assert r.status_code in (401, 403)
    r = await async_client.put("/api/v1/ops/launch/email-copy", json={"variant": "competing", "lead": "x"})
    assert r.status_code in (401, 403)
    r = await async_client.post("/api/v1/ops/launch/email-test", json={"variant": "competing"})
    assert r.status_code in (401, 403)


async def test_ops_launch_email_test_sends_only_to_the_signed_in_admin(admin_client):
    with (
        patch("core.services.launch_settings.effective", AsyncMock(return_value=_ROW)),
        patch("core.services.clerk_admin.get_primary_email", AsyncMock(return_value="admin@x.com ")),
        patch("core.services.launch_sheet_email.send_test", AsyncMock(return_value="msg_1")) as send,
        patch("core.repositories.launch_waitlist_repo.claim_winner_email", AsyncMock()) as claim,
        patch("core.repositories.launch_waitlist_repo.update_fields", AsyncMock()) as update,
    ):
        r = await admin_client.post("/api/v1/ops/launch/email-test", json={"variant": "competing"})
    assert r.status_code == 200
    body = r.json()
    assert body["sent_to"] == "admin@x.com" and body["variant"] == "competing"
    assert send.await_args.args[0] == "competing"
    assert send.await_args.kwargs["to_email"] == "admin@x.com"
    # A test send touches NO sheet state — no claim stamped, no row written.
    claim.assert_not_awaited()
    update.assert_not_awaited()


async def test_ops_launch_email_test_rejects_an_unknown_variant(admin_client):
    with patch("core.services.launch_sheet_email.send_test", AsyncMock()) as send:
        r = await admin_client.post("/api/v1/ops/launch/email-test", json={"variant": "nope"})
    assert r.status_code == 422
    send.assert_not_awaited()


async def test_ops_launch_email_test_502s_when_the_send_fails(admin_client):
    with (
        patch("core.services.launch_settings.effective", AsyncMock(return_value=_ROW)),
        patch("core.services.clerk_admin.get_primary_email", AsyncMock(return_value="admin@x.com")),
        patch("core.services.launch_sheet_email.send_test", AsyncMock(return_value=None)),
    ):
        r = await admin_client.post("/api/v1/ops/launch/email-test", json={"variant": "competing"})
    assert r.status_code == 502


# --- team entries: the founders' manual lane --------------------------------


async def test_ops_team_entry_toggle_round_trips(admin_client):
    row = {"owner_id": "org_1", "team_name": "Tuple", "seats": 6, "counted": True}
    with (
        patch("core.services.launch_service.set_team_entry", AsyncMock(return_value=row)) as set_entry,
        patch("core.services.ops.launch.clear_cache") as clear,
    ):
        r = await admin_client.post("/api/v1/ops/launch/team-entries/org_1", json={"seats": 6, "counted": True})
    # The audit decorator merges its own `audit_status` into a dict response.
    assert r.status_code == 200 and r.json() | row == r.json()
    assert set_entry.await_args.kwargs == {"seats": 6, "counted": True}
    assert set_entry.await_args.args == ("org_1",)
    # The pot preview reads through the 60s ops cache — a flipped toggle has to
    # land on the next page load, not a minute later.
    clear.assert_called_once()


async def test_ops_team_entry_bad_seats_is_the_422_field_envelope(admin_client):
    with patch(
        "core.services.launch_service.set_team_entry",
        AsyncMock(side_effect=launch_settings.SettingsInvalid({"seats": "must be between 1 and 500"})),
    ):
        r = await admin_client.post("/api/v1/ops/launch/team-entries/org_1", json={"seats": 0})
    assert r.status_code == 422
    assert r.json()["detail"] == {"errors": {"seats": "must be between 1 and 500"}}


async def test_ops_team_entry_requires_admin(async_client):
    r = await async_client.post("/api/v1/ops/launch/team-entries/org_1", json={"counted": True})
    assert r.status_code in (401, 403)


async def test_settings_put_round_trips_a_non_default_pot_per_seat(admin_client):
    """Every admin-editable field must survive the ROUTE, not just the form.

    `put_launch_settings` does `body.model_dump()`, so a field missing from
    `LaunchSettingsUpdate` is dropped by pydantic before `validate` sees it —
    `validate` then falls back to DEFAULTS and `put` (a full replace) rewrites
    the stored value on every save. That failure is invisible from the client
    side: the form renders the field and posts it, and the console still shows
    the default. Assert on what is STORED."""
    stored: dict = {}

    async def _put(item):
        stored.update(item)
        return dict(item)

    with (
        patch.object(launch_settings.launch_settings_repo, "get", AsyncMock(return_value=None)),
        patch.object(launch_settings.launch_settings_repo, "put", AsyncMock(side_effect=_put)),
        patch.object(launch_settings, "_clear_caches"),
    ):
        r = await admin_client.put(
            "/api/v1/ops/launch/settings",
            json={
                "enabled": False,
                "sheet_closes_at": None,
                "competition_start_at": None,
                "competition_end_at": None,
                "comp_slots": 200,
                "pot_front_cents": 100_000,
                "pot_per_entrant_cents": 1_000,
                "pot_per_seat_cents": 1_000,  # NOT the 500 default
                "referral_jump": 10,
            },
        )
    assert r.status_code == 200
    assert stored["pot_per_seat_cents"] == 1_000
    assert r.json()["pot_per_seat_cents"] == 1_000


@pytest.mark.asyncio
async def test_ops_launch_delete_entry_writes_an_audit_row(admin_client, audited):
    """Remove revokes a stored Stripe key and deletes the entry: it leaves the
    same audit trail as every other destructive admin action."""
    with (
        patch("core.services.launch_revenue.delete_key", AsyncMock()),
        patch("core.repositories.launch_entries_repo.delete", AsyncMock()),
    ):
        r = await admin_client.delete("/api/v1/ops/launch/entries/user_1")
    assert r.status_code == 200
    audited.assert_awaited_once()
    assert audited.await_args.kwargs["action"] == "launch.entry.remove"
    assert audited.await_args.kwargs["target_user_id"] == "user_1"
