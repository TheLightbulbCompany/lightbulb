"""Tests for the internal Bedrock meter endpoint.

This router does NOT use Depends(get_current_user); auth is a bearer
bedrock_meter service token (service_token.verify_meter). Mirrors the
hooks_proxy 401 template.

NOTE (plan/reality reconciliation): the Plan-3 doc patched the Plan-2 resolvers
(`resolve_owner_from_aip_arn`, `resolve_owner_from_identity_arn`) and the
pricing helper (`price_from_aip_arn`) with plain `return_value`s, assuming they
were synchronous. The MERGED Plan-2 / Plan-3 reality is that all three are
`async def` (they do DynamoDB GetItems), so the endpoint awaits them and the
tests patch them with `AsyncMock`. Arithmetic + behavior are otherwise exactly
as the plan specifies (raw 10000 * 14 // 10 = 14000; 100000 - 14000 = 86000).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.services import service_token
from main import app


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-signing-key-32-bytes-long-xx")


@pytest.fixture
def client():
    return TestClient(app)


def _body(**overrides) -> dict:
    base = dict(
        request_id="req-1",
        model_id="arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus-org_abc",
        identity_arn="arn:aws:sts::877352799272:assumed-role/isol8-owner-org_abc/x",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
        run_status="end",
        region="us-east-1",
        occurred_at="2026-06-07T12:00:00Z",
    )
    base.update(overrides)
    return base


def test_missing_authorization_returns_401(client):
    r = client.post("/api/v1/internal/usage/from-bedrock-log", json=_body())
    assert r.status_code == 401


def test_wrong_kind_token_returns_401(client):
    # A paperclip-kind token must be rejected on this surface.
    bad = service_token.mint("user_x")
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": f"Bearer {bad}"},
        json=_body(),
    )
    assert r.status_code == 401


def test_garbage_token_returns_401(client):
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": "Bearer not-a-jwt"},
        json=_body(),
    )
    assert r.status_code == 401


def test_blank_request_id_returns_422(client):
    token = service_token.mint_meter()
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(request_id=""),
    )
    assert r.status_code == 422


def test_negative_tokens_returns_422(client):
    token = service_token.mint_meter()
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(input_tokens=-5),
    )
    assert r.status_code == 422


import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402


@pytest.fixture
def ledger_tables(monkeypatch):
    """Mirror Plan 1's ledger_tables fixture (credits + credit-transactions)."""
    with mock_aws():
        c = boto3.client("dynamodb", region_name="us-east-1")
        c.create_table(
            TableName="test-credits",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        c.create_table(
            TableName="test-credit-txns",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "tx_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "tx_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        c.put_item(
            TableName="test-credits",
            Item={"user_id": {"S": "org_abc"}, "balance_microcents": {"N": "100000"}},
        )
        monkeypatch.setenv("CREDITS_TABLE", "test-credits")
        monkeypatch.setenv("CREDIT_TRANSACTIONS_TABLE", "test-credit-txns")
        monkeypatch.setenv("LEDGER_BACKEND", "ddb")
        yield c


@pytest.fixture
def meter_live(monkeypatch):
    """Force the cutover seam ON for the live-deplete tests.

    `settings` is a module-level pydantic singleton, so a setenv after import is
    too late — patch the attribute directly. `METER_IS_LIVE` is OWNED by Plan 4
    (defined in core/config.py there); this plan only consumes it.
    """
    monkeypatch.setattr("routers.internal_usage.settings.METER_IS_LIVE", True, raising=False)
    yield


class TestDeplete:
    def _post(self, client, **overrides):
        token = service_token.mint_meter()
        return client.post(
            "/api/v1/internal/usage/from-bedrock-log",
            headers={"Authorization": f"Bearer {token}"},
            json=_body(**overrides),
        )

    def test_prices_attributes_and_deplets(self, client, ledger_tables, meter_live):
        with (
            patch(
                "routers.internal_usage.resolve_owner_from_aip_arn",
                AsyncMock(return_value="org_abc"),
            ),
            patch(
                "routers.internal_usage.price_from_aip_arn",
                AsyncMock(return_value=10_000),
            ),
            patch("core.services.bedrock_enforcement.enforce_after_deplete", AsyncMock()),
        ):
            r = self._post(client, request_id="req-A")
        assert r.status_code == 200
        data = r.json()
        assert data["applied"] is True
        # raw 10000 * 14 // 10 = 14000 marked up; 100000 - 14000 = 86000
        assert data["balance_microcents"] == 86_000

    def test_replay_is_noop(self, client, ledger_tables, meter_live):
        with (
            patch(
                "routers.internal_usage.resolve_owner_from_aip_arn",
                AsyncMock(return_value="org_abc"),
            ),
            patch(
                "routers.internal_usage.price_from_aip_arn",
                AsyncMock(return_value=10_000),
            ),
            patch("core.services.bedrock_enforcement.enforce_after_deplete", AsyncMock()),
        ):
            first = self._post(client, request_id="req-dup")
            second = self._post(client, request_id="req-dup")
        assert first.json()["applied"] is True
        assert second.json()["applied"] is False
        # Deducted exactly once.
        assert second.json()["balance_microcents"] == 86_000

    def test_error_status_still_deplets(self, client, ledger_tables, meter_live):
        with (
            patch(
                "routers.internal_usage.resolve_owner_from_aip_arn",
                AsyncMock(return_value="org_abc"),
            ),
            patch(
                "routers.internal_usage.price_from_aip_arn",
                AsyncMock(return_value=10_000),
            ),
            patch("core.services.bedrock_enforcement.enforce_after_deplete", AsyncMock()),
        ):
            r = self._post(client, request_id="req-err", run_status="error")
        assert r.status_code == 200
        assert r.json()["applied"] is True
        assert r.json()["balance_microcents"] == 86_000

    def test_identity_arn_fallback_attributes_when_aip_misses(self, client, ledger_tables, meter_live):
        # AIP-ARN lookup misses (None) but the assumed-role ARN resolves the
        # owner — spec §3.2's fallback path must deplete, not skip.
        with (
            patch(
                "routers.internal_usage.resolve_owner_from_aip_arn",
                AsyncMock(return_value=None),
            ),
            patch(
                "routers.internal_usage.resolve_owner_from_identity_arn",
                AsyncMock(return_value="org_abc"),
            ),
            patch(
                "routers.internal_usage.price_from_aip_arn",
                AsyncMock(return_value=10_000),
            ),
            patch("core.services.bedrock_enforcement.enforce_after_deplete", AsyncMock()),
        ):
            r = self._post(client, request_id="req-fallback")
        assert r.status_code == 200
        assert r.json()["applied"] is True
        assert r.json()["balance_microcents"] == 86_000

    def test_meter_live_calls_deplete_and_enforce(self, client, ledger_tables, meter_live):
        # GAP 4 + GAP 5: when METER_IS_LIVE is True the endpoint MUST deplete
        # the ledger AND fire the kill-switch (enforce_after_deplete) with the
        # resolved owner_id + the deplete result. Both deplete() and
        # enforce_after_deplete are awaited, so mock them with AsyncMock.
        fake_result = MagicMock(applied=True, balance_microcents=86_000)
        with (
            patch(
                "routers.internal_usage.resolve_owner_from_aip_arn",
                AsyncMock(return_value="org_abc"),
            ),
            patch(
                "routers.internal_usage.price_from_aip_arn",
                AsyncMock(return_value=10_000),
            ),
            patch("routers.internal_usage.get_ledger") as mock_get_ledger,
            patch(
                "core.services.bedrock_enforcement.enforce_after_deplete",
                new_callable=AsyncMock,
            ) as mock_enforce,
        ):
            mock_get_ledger.return_value.deplete = AsyncMock(return_value=fake_result)
            r = self._post(client, request_id="req-live")
        assert r.status_code == 200
        assert r.json() == {"applied": True, "balance_microcents": 86_000}
        # deplete was called exactly once...
        mock_get_ledger.return_value.deplete.assert_awaited_once()
        # ...and the kill-switch fired with (owner_id, result).
        mock_enforce.assert_awaited_once_with("org_abc", fake_result)

    def test_meter_shadow_logs_and_does_not_deplete(self, client, ledger_tables, monkeypatch):
        # GAP 5: when METER_IS_LIVE is False the endpoint is shadow-only — it
        # builds + prices the event but NEVER calls deplete() and returns
        # applied=False / shadow=True. This is what lets the meter run alongside
        # the legacy biller without double-deducting.
        monkeypatch.setattr("routers.internal_usage.settings.METER_IS_LIVE", False, raising=False)
        with (
            patch(
                "routers.internal_usage.resolve_owner_from_aip_arn",
                AsyncMock(return_value="org_abc"),
            ),
            patch(
                "routers.internal_usage.price_from_aip_arn",
                AsyncMock(return_value=10_000),
            ),
            patch("routers.internal_usage.get_ledger") as mock_get_ledger,
            patch("core.services.bedrock_enforcement.enforce_after_deplete", AsyncMock()) as mock_enforce,
        ):
            r = self._post(client, request_id="req-shadow")
        assert r.status_code == 200
        assert r.json() == {"applied": False, "shadow": True}
        # The ledger was NEVER touched (deplete not called)...
        mock_get_ledger.return_value.deplete.assert_not_called()
        # ...and the kill-switch never fired in shadow mode.
        mock_enforce.assert_not_called()
        # ...and nothing was written to the txns table.
        resp = ledger_tables.scan(TableName="test-credit-txns")
        assert resp["Count"] == 0


class TestEdgeCases:
    def _post(self, client, **overrides):
        token = service_token.mint_meter()
        return client.post(
            "/api/v1/internal/usage/from-bedrock-log",
            headers={"Authorization": f"Bearer {token}"},
            json=_body(**overrides),
        )

    def test_unattributed_owner_skips_without_deplete(self, client, ledger_tables):
        # BOTH attribution paths miss → truly unattributed → skip, no deplete.
        with (
            patch(
                "routers.internal_usage.resolve_owner_from_aip_arn",
                AsyncMock(return_value=None),
            ),
            patch(
                "routers.internal_usage.resolve_owner_from_identity_arn",
                AsyncMock(return_value=None),
            ),
        ):
            r = self._post(client, request_id="req-orphan")
        assert r.status_code == 200
        assert r.json() == {"applied": False, "balance_microcents": 0}
        # Nothing was written to the txns table.
        resp = ledger_tables.scan(TableName="test-credit-txns")
        assert resp["Count"] == 0

    def test_unknown_model_skips_without_deplete(self, client, ledger_tables):
        from core.billing.bedrock_pricing import UnknownModelError

        with (
            patch(
                "routers.internal_usage.resolve_owner_from_aip_arn",
                AsyncMock(return_value="org_abc"),
            ),
            patch(
                "routers.internal_usage.price_from_aip_arn",
                AsyncMock(side_effect=UnknownModelError("aip-???")),
            ),
        ):
            r = self._post(client, request_id="req-unknown")
        assert r.status_code == 200
        assert r.json()["applied"] is False
        resp = ledger_tables.scan(TableName="test-credit-txns")
        assert resp["Count"] == 0

    def test_route_is_mounted_but_hidden_from_openapi(self, client):
        # Mounted: a POST with no auth hits the auth dep (401), not a 404.
        assert client.post("/api/v1/internal/usage/from-bedrock-log", json=_body()).status_code == 401
        # Hidden: not in the public schema (schemathesis posture).
        # NOTE (plan/reality reconciliation): the Plan-3 doc fetched "/openapi.json",
        # but this app serves the schema at f"{settings.API_V1_STR}/openapi.json"
        # (main.py:423 sets openapi_url="/api/v1/openapi.json"), so "/openapi.json"
        # 404s. Hitting the real schema URL is what actually exercises the
        # include_in_schema=False posture.
        schema = client.get("/api/v1/openapi.json").json()
        assert "/api/v1/internal/usage/from-bedrock-log" not in schema["paths"]
