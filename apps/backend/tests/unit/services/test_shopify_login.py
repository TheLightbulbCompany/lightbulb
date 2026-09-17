from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.services import shopify_login
from core.services.messaging import ApiProblem
from core.services.runtime_transport import RuntimeResponse

OWNER = "org_test"
STORE = "mystore.myshopify.com"
AUTH_URL = f"https://{STORE}/admin/oauth/authorize?client_id=7e9c&state=x"
CONTAINER = {"owner_id": OWNER, "gateway_token": "gw-secret"}


class TestNormalizeStore:
    @pytest.mark.parametrize(
        "raw",
        [
            "mystore.myshopify.com",
            " MyStore.myshopify.com ",
            "https://mystore.myshopify.com/admin/orders?x=1",
            "https://admin.shopify.com/store/mystore/orders",
            "admin.shopify.com/store/mystore",
            "mystore",
        ],
    )
    def test_every_spelling_an_owner_pastes_reduces_to_the_host(self, raw):
        assert shopify_login.normalize_store(raw) == STORE

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "shop.example.com",
            "169.254.169.254",
            "mystore.myshopify.com.evil.com",
            "my store",
            "a" * 61 + ".myshopify.com",
        ],
    )
    def test_anything_else_is_refused_with_the_hint(self, raw):
        with pytest.raises(ValueError, match="myshopify.com"):
            shopify_login.normalize_store(raw)


def _response(body: dict, status: int = 200) -> RuntimeResponse:
    import json

    return RuntimeResponse(status_code=status, text=json.dumps(body))


class TestStartLogin:
    @pytest.mark.asyncio
    async def test_starts_the_cli_then_opens_the_url_in_the_container_chrome(self):
        request = AsyncMock(return_value=_response({"ok": True, "store": STORE, "authorizeUrl": AUTH_URL}))
        rpc = AsyncMock(return_value={})
        with (
            patch.object(
                shopify_login.container_rpc, "running_container", AsyncMock(return_value=(CONTAINER, "10.0.0.9"))
            ),
            patch.object(shopify_login.runtime_transport, "request", request),
            patch.object(shopify_login.container_rpc, "call", rpc),
        ):
            assert await shopify_login.start_login(OWNER, STORE) == AUTH_URL

        # The plugin route, gateway-authenticated, with the read scopes.
        args, kwargs = request.call_args
        assert args[0] is CONTAINER
        assert args[1] == "/plugins/lightbulb-channel/shopify/login"
        assert kwargs["method"] == "POST"
        assert kwargs["json_body"] == {"store": STORE, "scopes": shopify_login.SCOPES}
        assert kwargs["headers"] == {"Authorization": "Bearer gw-secret"}
        # Chrome is started before the tab is opened, and the tab is the CLI's URL —
        # never one the caller supplied.
        assert [c.args[1:3] for c in rpc.call_args_list] == [
            ("browser.request", {"method": "POST", "path": "/start"}),
            ("browser.request", {"method": "POST", "path": "/tabs/open", "body": {"url": AUTH_URL}}),
        ]

    @pytest.mark.asyncio
    async def test_a_cli_that_printed_no_url_is_an_error_and_opens_nothing(self):
        request = AsyncMock(
            return_value=_response(
                {"ok": False, "error": {"code": "LOGIN_START_FAILED", "message": "shopify: not found"}}, 502
            )
        )
        rpc = AsyncMock()
        with (
            patch.object(
                shopify_login.container_rpc, "running_container", AsyncMock(return_value=(CONTAINER, "10.0.0.9"))
            ),
            patch.object(shopify_login.runtime_transport, "request", request),
            patch.object(shopify_login.container_rpc, "call", rpc),
        ):
            with pytest.raises(ApiProblem) as excinfo:
                await shopify_login.start_login(OWNER, STORE)
        assert excinfo.value.status_code == 500
        assert "shopify: not found" in str(excinfo.value)
        rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreachable_container_is_the_retryable_runtime_problem(self):
        with (
            patch.object(
                shopify_login.container_rpc, "running_container", AsyncMock(return_value=(CONTAINER, "10.0.0.9"))
            ),
            patch.object(shopify_login.runtime_transport, "request", AsyncMock(side_effect=httpx.ConnectError("boom"))),
        ):
            with pytest.raises(ApiProblem) as excinfo:
                await shopify_login.start_login(OWNER, STORE)
        assert excinfo.value.status_code == 503


class TestLoginStatus:
    @pytest.mark.asyncio
    async def test_reads_the_store_from_the_query_and_normalizes_the_shape(self):
        request = AsyncMock(
            return_value=_response(
                {
                    "ok": True,
                    "store": STORE,
                    "connected": True,
                    "pending": False,
                    "session": {"store": STORE, "scopes": ["read_orders"]},
                }
            )
        )
        with (
            patch.object(
                shopify_login.container_rpc, "running_container", AsyncMock(return_value=(CONTAINER, "10.0.0.9"))
            ),
            patch.object(shopify_login.runtime_transport, "request", request),
        ):
            status = await shopify_login.login_status(OWNER, STORE)
        assert request.call_args.args[1] == f"/plugins/lightbulb-channel/shopify/status?store={STORE}"
        assert request.call_args.kwargs["method"] == "GET"
        assert status == {
            "connected": True,
            "pending": False,
            "error": None,
            "session": {"store": STORE, "scopes": ["read_orders"]},
        }

    @pytest.mark.asyncio
    async def test_a_failed_login_carries_the_cli_s_last_line(self):
        request = AsyncMock(
            return_value=_response(
                {"ok": True, "connected": False, "pending": False, "error": "Timed out waiting for OAuth callback."}
            )
        )
        with (
            patch.object(
                shopify_login.container_rpc, "running_container", AsyncMock(return_value=(CONTAINER, "10.0.0.9"))
            ),
            patch.object(shopify_login.runtime_transport, "request", request),
        ):
            status = await shopify_login.login_status(OWNER, STORE)
        assert status["connected"] is False
        assert status["error"] == "Timed out waiting for OAuth callback."


class TestLogout:
    @pytest.mark.asyncio
    async def test_posts_the_store_to_the_plugin(self):
        request = AsyncMock(return_value=_response({"ok": True, "store": STORE, "removed": 1}))
        with (
            patch.object(
                shopify_login.container_rpc, "running_container", AsyncMock(return_value=(CONTAINER, "10.0.0.9"))
            ),
            patch.object(shopify_login.runtime_transport, "request", request),
        ):
            assert await shopify_login.logout(OWNER, STORE) is True
        assert request.call_args.args[1] == "/plugins/lightbulb-channel/shopify/logout"
        assert request.call_args.kwargs["json_body"] == {"store": STORE}

    @pytest.mark.asyncio
    async def test_a_parked_container_does_not_fail_the_disconnect(self):
        # Disconnect must still drop the row; the CLI session on EFS is not a
        # credential the backend holds, and uninstalling the app kills it anyway.
        unreachable = ApiProblem("RUNTIME_UNAVAILABLE", "waking", retryable=True)
        with patch.object(shopify_login.container_rpc, "running_container", AsyncMock(side_effect=unreachable)):
            assert await shopify_login.logout(OWNER, STORE) is False
