from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from routers import lifecycle_operations


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(lifecycle_operations.router, prefix="/api/v1/lifecycle")
    return TestClient(app)


def _row(**overrides):
    row = {
        "customer_id": "user_1",
        "workspace_id": "user_1",
        "email": "customer@example.com",
        "first_name": "Ada",
        "signup_at": "2026-01-01T00:00:00Z",
        "last_sign_in_at": "2026-01-02T00:00:00Z",
        "last_activity_at": "2026-01-03T00:00:00Z",
        "activation": {"workspace_created_at": "2026-01-01T01:00:00Z", "workspace_ready_at": None},
        "plan": "free",
        "trial_ends_at": None,
        "subscription_status": "trial_local",
        "account_status": "active",
        "marketing_consent_at": "2026-01-01T00:00:00Z",
        "marketing_unsubscribed_at": None,
    }
    row.update(overrides)
    return row


def test_auth_is_required_and_compared_as_bearer_secret(monkeypatch):
    monkeypatch.setattr(settings, "PAPERCLIP_EVENTS_WEBHOOK_SECRET", "service-secret")
    with patch.object(lifecycle_operations, "build_candidates", AsyncMock(return_value=([], None))):
        assert _app().get("/api/v1/lifecycle/candidates").status_code == 401
        assert _app().get("/api/v1/lifecycle/candidates", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert (
            _app().get("/api/v1/lifecycle/candidates", headers={"Authorization": "Bearer service-secret"}).status_code
            == 200
        )


def test_empty_cohort_has_stable_shape(monkeypatch):
    monkeypatch.setattr(settings, "PAPERCLIP_EVENTS_WEBHOOK_SECRET", "service-secret")
    with patch.object(lifecycle_operations, "build_candidates", AsyncMock(return_value=([], None))):
        response = _app().get(
            "/api/v1/lifecycle/candidates",
            headers={"Authorization": "Bearer service-secret"},
        )
    assert response.json() == {"candidates": [], "cursor": None}


def test_internal_unsubscribed_and_suppressed_are_excluded_from_marketing():
    rows = [
        _row(email="owner@thelightbulb.company"),
        _row(customer_id="user_2", email="no@example.com", marketing_unsubscribed_at="2026-01-03T00:00:00Z"),
        _row(customer_id="user_3", email="bounce@example.com"),
        _row(customer_id="user_4", email="yes@example.com"),
    ]
    with (
        patch("core.services.lifecycle_operations._raw_candidates", AsyncMock(return_value=(rows, None))),
        patch(
            "core.services.lifecycle_operations._is_ses_suppressed",
            AsyncMock(side_effect=lambda email: email == "bounce@example.com"),
        ),
        patch("core.services.lifecycle_operations.lifecycle_ledger.get_recipient_state", AsyncMock(return_value=None)),
        patch(
            "core.services.lifecycle_operations.user_repo.get",
            AsyncMock(side_effect=lambda uid: next(row for row in rows if row["customer_id"] == uid)),
        ),
    ):
        from core.services.lifecycle_operations import build_candidates
        import asyncio

        candidates, cursor = asyncio.run(build_candidates(audience="marketing", limit=100, cursor=None))
    assert cursor is None
    assert [row["email"] for row in candidates] == ["yes@example.com"]
    assert candidates[0]["eligibility"]["marketing"] is True
    assert candidates[0]["eligibility"]["transactional"] is True


def test_claim_is_idempotent_and_72_hour_cap_is_separate_from_monthly(monkeypatch):
    monkeypatch.setattr(settings, "PAPERCLIP_EVENTS_WEBHOOK_SECRET", "service-secret")
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    with patch.object(
        lifecycle_operations.lifecycle_ledger,
        "claim",
        AsyncMock(side_effect=[{"claimed": True, "ledger_id": "a"}, {"claimed": False, "reason": "duplicate"}]),
    ) as claim:
        client = _app()
        payload = {
            "recipient": "customer@example.com",
            "message": "trial-day-2",
            "eligibility_window": "2026-01-10",
            "kind": "lifecycle",
            "reason": "trial nurture",
            "now": now.isoformat(),
        }
        first = client.post(
            "/api/v1/lifecycle/ledger/claim", json=payload, headers={"Authorization": "Bearer service-secret"}
        )
        second = client.post(
            "/api/v1/lifecycle/ledger/claim", json=payload, headers={"Authorization": "Bearer service-secret"}
        )
    assert first.json()["claimed"] is True
    assert second.json() == {"claimed": False, "reason": "duplicate"}
    assert claim.await_args_list[0].kwargs["frequency_cap_hours"] == 72


def test_monthly_broadcast_does_not_use_lifecycle_frequency_cap(monkeypatch):
    monkeypatch.setattr(settings, "PAPERCLIP_EVENTS_WEBHOOK_SECRET", "service-secret")
    with patch.object(
        lifecycle_operations.lifecycle_ledger, "claim", AsyncMock(return_value={"claimed": True, "ledger_id": "b"})
    ) as claim:
        response = _app().post(
            "/api/v1/lifecycle/ledger/claim",
            json={
                "recipient": "customer@example.com",
                "message": "monthly-october",
                "eligibility_window": "2026-10",
                "kind": "monthly_broadcast",
                "reason": "monthly product update",
            },
            headers={"Authorization": "Bearer service-secret"},
        )
    assert response.status_code == 200
    assert claim.await_args.kwargs["frequency_cap_hours"] is None


class _LedgerClient:
    def __init__(self, error=False):
        self.error = error
        self.calls = []

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise ClientError(
                {"Error": {"Code": "TransactionCanceledException", "Message": "conditional"}},
                "TransactWriteItems",
            )


class _LedgerTable:
    table_name = "test-containers"

    def __init__(self, client, existing=None):
        self.meta = type("Meta", (), {"client": client})()
        self.existing = existing

    def get_item(self, **_kwargs):
        return {"Item": self.existing} if self.existing else {}


def test_ledger_atomically_caps_lifecycle_but_not_monthly_broadcast():
    import asyncio

    from core.repositories import lifecycle_ledger

    client = _LedgerClient()
    table = _LedgerTable(client)
    with patch.object(lifecycle_ledger, "get_table", return_value=table):
        lifecycle = asyncio.run(
            lifecycle_ledger.claim(
                recipient="a@example.com",
                message="nudge",
                eligibility_window="w1",
                kind="lifecycle",
                reason="test",
                frequency_cap_hours=72,
                now=datetime(2026, 1, 10, tzinfo=timezone.utc),
            )
        )
        monthly = asyncio.run(
            lifecycle_ledger.claim(
                recipient="a@example.com",
                message="newsletter",
                eligibility_window="2026-10",
                kind="monthly_broadcast",
                reason="test",
                frequency_cap_hours=None,
                now=datetime(2026, 1, 10, tzinfo=timezone.utc),
            )
        )

    assert lifecycle["claimed"] is True
    assert monthly["claimed"] is True
    lifecycle_update = client.calls[0]["TransactItems"][1]["Update"]
    monthly_update = client.calls[1]["TransactItems"][1]["Update"]
    assert "ConditionExpression" in lifecycle_update
    assert "last_lifecycle_sent_at" in lifecycle_update["UpdateExpression"]
    assert "ConditionExpression" not in monthly_update
    assert "last_monthly_broadcast_at" in monthly_update["UpdateExpression"]


def test_ledger_distinguishes_duplicate_from_frequency_cap():
    import asyncio

    from core.repositories import lifecycle_ledger

    kwargs = {
        "recipient": "a@example.com",
        "message": "nudge",
        "eligibility_window": "w1",
        "kind": "lifecycle",
        "reason": "test",
        "frequency_cap_hours": 72,
        "now": datetime(2026, 1, 10, tzinfo=timezone.utc),
    }
    duplicate_table = _LedgerTable(_LedgerClient(error=True), existing={"owner_id": "already"})
    capped_table = _LedgerTable(_LedgerClient(error=True), existing=None)
    with patch.object(lifecycle_ledger, "get_table", return_value=duplicate_table):
        duplicate = asyncio.run(lifecycle_ledger.claim(**kwargs))
    with patch.object(lifecycle_ledger, "get_table", return_value=capped_table):
        capped = asyncio.run(lifecycle_ledger.claim(**kwargs))

    assert duplicate == {"claimed": False, "reason": "duplicate"}
    assert capped == {"claimed": False, "reason": "frequency_cap"}
