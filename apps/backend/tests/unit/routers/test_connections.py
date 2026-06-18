"""Tests for the customer-facing connections router (Bucket A paste-key flow).

Mirrors the auth-override + async_client idiom from test_billing.py / conftest:
``get_current_user`` is overridden by the ``async_client`` fixture (personal
mode, owner_id == "user_test_123"). Async repo / apply calls are patched with
``AsyncMock`` so the awaits resolve.

Phase 2 of the credential redesign removed the DynamoDB credential vault: the
router now persists retained credentials to AWS Secrets Manager
(``integration_secret_store``) and writes only a secret-free status row to the
``integrations`` table (``integrations_repo``). These tests patch those two
collaborators (``routers.connections.integration_secret_store`` /
``routers.connections.integrations_repo``) — there is no longer a
``connection_repo`` or any ``scrub_secret`` / ``decrypt_credential`` path.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from routers import connections


class TestValidateToken:
    """``_validate_token`` builds the Authorization header from the per-provider
    validate ``auth_scheme`` (Stripe = ``Bearer <key>``) and sends the optional
    JSON ``body`` when the spec defines one."""

    @pytest.mark.asyncio
    async def test_stripe_gets_with_bearer_authorization_header_and_no_body(self, monkeypatch):
        from routers import connections
        from core.services.credentials import registry

        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):
            captured["method"] = method
            captured["headers"] = kwargs.get("headers")
            captured["json"] = kwargs.get("json")
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        ok = await connections._validate_token(registry.get_provider("stripe"), "sk_test_KEY")
        assert ok is True
        # Stripe: GET, default Bearer scheme, no JSON body.
        assert captured["method"] == "GET"
        assert captured["headers"]["Authorization"] == "Bearer sk_test_KEY"
        assert captured["json"] is None

    @pytest.mark.asyncio
    async def test_non_2xx_status_fails_validation(self, monkeypatch):
        from routers import connections
        from core.services.credentials import registry

        async def fake_request(self, method, url, **kwargs):
            return httpx.Response(401)

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        assert await connections._validate_token(registry.get_provider("stripe"), "bad") is False

    @pytest.mark.asyncio
    async def test_static_validate_headers_are_sent(self, monkeypatch):
        # Some providers require a static request header on validate (e.g. Notion's
        # `Notion-Version`). The spec's `headers` must be merged in alongside the
        # auth header so the request the provider expects actually goes out.
        from routers import connections
        from core.services.credentials.registry import Provider, ValidateSpec, Wiring

        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):
            captured["headers"] = kwargs.get("headers")
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        provider = Provider(
            key="notion",
            display_name="Notion",
            acquisition="paste_token",
            actor="mcp",
            refresh_owner="none",
            wiring=Wiring(mcp_server={"key": "notion", "command": "npx", "env": {"TOKEN": "input.token"}}),
            validate=ValidateSpec(
                method="GET",
                url="https://api.notion.com/v1/users/me",
                headers={"Notion-Version": "2022-06-28"},
            ),
        )
        ok = await connections._validate_token(provider, "secret_token")
        assert ok is True
        # Both the auth header and the static spec header reach the provider.
        assert captured["headers"]["Authorization"] == "Bearer secret_token"
        assert captured["headers"]["Notion-Version"] == "2022-06-28"

    def _slack_provider(self):
        # Slack's auth.test returns HTTP 200 even for a bad token, signalling
        # validity only via the JSON `ok` field.
        from core.services.credentials.registry import Provider, ValidateSpec, Wiring

        return Provider(
            key="slack",
            display_name="Slack",
            acquisition="paste_token",
            actor="mcp",
            refresh_owner="none",
            wiring=Wiring(mcp_server={"key": "slack", "command": "npx", "env": {"TOKEN": "input.token"}}),
            validate=ValidateSpec(
                method="POST",
                url="https://slack.com/api/auth.test",
                success_json_field="ok",
            ),
        )

    def _xoxp_provider(self):
        # A provider whose validate spec pins a token_prefix — checked pre-flight
        # (before any HTTP call). Self-contained, independent of the registry.
        from core.services.credentials.registry import Provider, ValidateSpec, Wiring

        return Provider(
            key="prefixed",
            display_name="Prefixed",
            acquisition="paste_token",
            actor="mcp",
            refresh_owner="none",
            wiring=Wiring(mcp_server={"key": "prefixed", "command": "npx", "env": {"TOKEN": "input.token"}}),
            validate=ValidateSpec(
                method="POST",
                url="https://example.com/auth.test",
                success_json_field="ok",
                token_prefix="xoxp-",
            ),
        )

    @pytest.mark.asyncio
    async def test_success_json_field_false_fails_despite_200(self, monkeypatch):
        from routers import connections

        async def fake_request(self, method, url, **kwargs):
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        assert await connections._validate_token(self._slack_provider(), "xoxb-bad") is False

    @pytest.mark.asyncio
    async def test_success_json_field_true_passes(self, monkeypatch):
        from routers import connections

        async def fake_request(self, method, url, **kwargs):
            return httpx.Response(200, json={"ok": True, "user": "bot"})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        assert await connections._validate_token(self._slack_provider(), "xoxb-good") is True

    @pytest.mark.asyncio
    async def test_success_json_field_missing_nested_path_fails(self, monkeypatch):
        # A dotted path that doesn't resolve (Linear GraphQL returned `errors`
        # instead of `data.viewer.id`) is treated as invalid, not an exception.
        from routers import connections
        from core.services.credentials.registry import Provider, ValidateSpec, Wiring

        async def fake_request(self, method, url, **kwargs):
            return httpx.Response(200, json={"errors": [{"message": "auth"}]})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        provider = Provider(
            key="linear",
            display_name="Linear",
            acquisition="paste_token",
            actor="mcp",
            refresh_owner="none",
            wiring=Wiring(mcp_server={"key": "linear", "command": "npx", "env": {"TOKEN": "input.token"}}),
            validate=ValidateSpec(
                method="POST",
                url="https://api.linear.app/graphql",
                success_json_field="data.viewer.id",
            ),
        )
        assert await connections._validate_token(provider, "lin_bad") is False

    @pytest.mark.asyncio
    async def test_success_json_field_non_json_body_fails(self, monkeypatch):
        # A 200 with a non-JSON body must be treated as invalid, not raise.
        from routers import connections

        async def fake_request(self, method, url, **kwargs):
            return httpx.Response(200, text="not json")

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        assert await connections._validate_token(self._slack_provider(), "xoxb") is False

    @pytest.mark.asyncio
    async def test_token_prefix_rejects_wrong_type_without_http(self, monkeypatch):
        # A provider with a token_prefix rejects a wrong-prefix token pre-flight
        # (no HTTP call) — the validate endpoint would otherwise ok it.
        from routers import connections

        async def fake_request(self, method, url, **kwargs):
            raise AssertionError("HTTP must not run for a wrong-prefix token")

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        assert await connections._validate_token(self._xoxp_provider(), "xoxb-bot") is False

    @pytest.mark.asyncio
    async def test_token_prefix_allows_matching_type(self, monkeypatch):
        from routers import connections

        async def fake_request(self, method, url, **kwargs):
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        assert await connections._validate_token(self._xoxp_provider(), "xoxp-good") is True


class TestListConnections:
    """GET /api/v1/connections — catalog + per-provider status."""

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_list_returns_catalog_with_status(self, mock_repo, async_client):
        # stripe is connected; google has no row -> disconnected.
        mock_repo.list_for_owner = AsyncMock(return_value=[{"provider": "stripe", "status": "connected"}])

        response = await async_client.get("/api/v1/connections")
        assert response.status_code == 200

        by_key = {c["provider"]: c for c in response.json()["connections"]}
        # Every registry provider is represented.
        assert {"stripe", "google"} <= set(by_key)
        assert by_key["stripe"]["status"] == "connected"
        assert by_key["google"]["status"] == "disconnected"
        # Catalog metadata surfaced from the registry.
        assert by_key["stripe"]["display_name"] == "Stripe"
        assert by_key["stripe"]["acquisition"] == "paste_token"
        assert by_key["google"]["acquisition"] == "direct_oauth"

        # Status now comes from the secret-free integrations index.
        mock_repo.list_for_owner.assert_awaited_once_with("user_test_123")

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_google_visible_to_every_owner(self, mock_repo, async_client, monkeypatch):
        mock_repo.list_for_owner = AsyncMock(return_value=[])  # no connected rows
        response = await async_client.get("/api/v1/connections")
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        assert "google" in by_key
        assert by_key["google"]["byo_required"] is True
        assert "requires_bedrock" not in by_key["google"]
        assert "byo_capable" not in by_key["google"]


class TestByoRequiredGate:
    """A `byo_required` provider (google) has no managed app, so its connect
    path requires the owner to have stored + acknowledged their own OAuth app
    first. The catalog lists it for everyone; authorize 400s until a BYO app
    is configured."""

    @pytest.mark.asyncio
    async def test_authorize_400_when_byo_app_not_configured(self, async_client, monkeypatch):
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=False))
        response = await async_client.get("/api/v1/connections/google/authorize")
        assert response.status_code == 400

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_succeeds_when_byo_app_configured(self, mock_dance, async_client, monkeypatch):
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=True))
        mock_dance.start = AsyncMock(return_value="https://accounts.google.com/o/oauth2/v2/auth?x=1")
        response = await async_client.get("/api/v1/connections/google/authorize")
        assert response.status_code == 200
        assert response.json()["authorize_url"].startswith("https://accounts.google.com")


class TestConnectPaste:
    """POST /api/v1/connections/{provider} — paste-key connect."""

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_happy_path(self, mock_validate, mock_repo, mock_store, mock_apply, async_client):
        mock_validate.return_value = True
        # Record relative call order — the rework's whole point is that the
        # status row is written only AFTER wiring succeeds.
        call_order: list[str] = []
        mock_store.put = AsyncMock(side_effect=lambda *a, **k: call_order.append("sm_put"))
        mock_store.delete = AsyncMock()
        mock_repo.put_status = AsyncMock(side_effect=lambda *a, **k: call_order.append("put_status"))
        mock_apply.apply_connection = AsyncMock(side_effect=lambda *a, **k: call_order.append("apply"))

        response = await async_client.post("/api/v1/connections/stripe", json={"token": "sk_test_abc123"})
        assert response.status_code == 200
        assert response.json() == {"status": "connected", "provider": "stripe"}

        # Credential persisted to Secrets Manager with the pasted token.
        mock_store.put.assert_awaited_once()
        put_args = mock_store.put.await_args
        assert put_args.args[0] == "user_test_123"
        assert put_args.args[1] == "stripe"
        assert put_args.args[2] == {"token": "sk_test_abc123"}

        # Wiring engine invoked with (owner, provider, credential).
        mock_apply.apply_connection.assert_awaited_once_with("user_test_123", "stripe", {"token": "sk_test_abc123"})

        # Status row written ONLY after a successful apply.
        mock_repo.put_status.assert_awaited_once_with("user_test_123", "stripe", "connected")

        # Load-bearing ordering: store secret -> wire container -> mark connected.
        # A put_status written before apply would falsely report "connected"
        # mid-wiring; this catches that regression.
        assert call_order == ["sm_put", "apply", "put_status"]

        # Happy path never deletes the just-stored secret.
        mock_store.delete.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_validation_failure_returns_422(
        self, mock_validate, mock_repo, mock_store, mock_apply, async_client
    ):
        mock_validate.return_value = False
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client.post("/api/v1/connections/stripe", json={"token": "bad"})
        assert response.status_code == 422
        # Nothing persisted / wired / indexed when validation fails.
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.MATERIALIZER_REMOVERS")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_rolls_back_secret_when_wiring_fails(
        self, mock_validate, mock_repo, mock_store, mock_apply, mock_removers, async_client
    ):
        mock_validate.return_value = True
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(side_effect=RuntimeError("efs write failed"))

        response = await async_client.post("/api/v1/connections/stripe", json={"token": "sk_test_abc123"})
        # Wiring failure surfaces as a 502 — the connection never reports a false "connected".
        assert response.status_code == 502

        # The secret was stored, then rolled back out of Secrets Manager.
        mock_store.put.assert_awaited_once()
        mock_store.delete.assert_awaited_once_with("user_test_123", "stripe")
        # Status is written only after a clean apply -> never on the failure path.
        mock_repo.put_status.assert_not_called()
        # stripe is mcp_server (no materializer) -> the EFS-artifact rollback is a no-op.
        mock_removers.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_non_paste_provider_returns_400(self, async_client):
        # google is direct_oauth, not paste_token, and must be rejected at this
        # paste surface with a 400.
        response = await async_client.post("/api/v1/connections/google", json={"token": "anything"})
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_connect_unknown_provider_returns_404(self, async_client):
        response = await async_client.post("/api/v1/connections/does-not-exist", json={"token": "x"})
        assert response.status_code == 404


class TestConnectionWriteOrgAdminGate:
    """Org callers write the SHARED org workspace (resolve_owner_id -> org_id),
    so the connect/authorize/disconnect writes must be gated to org admins —
    matching settings_keys / channels / config. Non-admin members get 403 with
    no side effects; admins pass through; the GET list is NOT gated."""

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_non_admin_member_forbidden(
        self, mock_validate, mock_repo, mock_store, mock_apply, async_client_org_member
    ):
        mock_validate.return_value = True
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client_org_member.post("/api/v1/connections/stripe", json={"token": "sk_test_abc123"})
        assert response.status_code == 403
        # Gate fires before any persistence / wiring.
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_non_admin_member_forbidden(self, mock_dance, async_client_org_member):
        mock_dance.start = AsyncMock()

        response = await async_client_org_member.get("/api/v1/connections/google/authorize")
        assert response.status_code == 403
        # OAuth dance never started for a non-admin member.
        mock_dance.start.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_non_admin_member_forbidden(
        self, mock_repo, mock_store, mock_patcher, async_client_org_member
    ):
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_patcher.set_openclaw_config_path = AsyncMock()

        response = await async_client_org_member.delete("/api/v1/connections/stripe")
        assert response.status_code == 403
        # Nothing deleted / unwired for a non-admin member.
        mock_repo.delete.assert_not_called()
        mock_store.delete.assert_not_called()
        mock_patcher.set_openclaw_config_path.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_org_admin_allowed(
        self, mock_validate, mock_repo, mock_store, mock_apply, async_client_org_admin
    ):
        mock_validate.return_value = True
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client_org_admin.post("/api/v1/connections/stripe", json={"token": "sk_test_abc123"})
        # Admin clears the gate; write targets the shared org workspace.
        assert response.status_code == 200
        assert response.json() == {"status": "connected", "provider": "stripe"}
        put_args = mock_store.put.await_args
        assert put_args.args[0] == "org_test_456"
        mock_apply.apply_connection.assert_awaited_once_with("org_test_456", "stripe", {"token": "sk_test_abc123"})
        mock_repo.put_status.assert_awaited_once_with("org_test_456", "stripe", "connected")

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_list_connections_not_gated_for_org_member(self, mock_repo, async_client_org_member):
        # GET returns only provider names + status (no secrets) -> members may read it.
        mock_repo.list_for_owner = AsyncMock(return_value=[{"provider": "stripe", "status": "connected"}])

        response = await async_client_org_member.get("/api/v1/connections")
        assert response.status_code == 200
        mock_repo.list_for_owner.assert_awaited_once_with("org_test_456")


class TestDisconnect:
    """DELETE /api/v1/connections/{provider} — disconnect + unwire."""

    @pytest.mark.asyncio
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_deletes_and_blanks_openclaw_path(
        self, mock_repo, mock_store, mock_patcher, async_client, monkeypatch
    ):
        from routers import connections
        from core.services.credentials.registry import OpenclawPath, Provider, Wiring

        # No live provider uses the openclaw_paths rail anymore, so exercise the
        # disconnect-time blanking of that rail via a synthetic provider.
        synthetic = Provider(
            key="synth",
            display_name="Synthetic",
            acquisition="paste_token",
            actor="mcp",
            refresh_owner="none",
            wiring=Wiring(
                openclaw_paths=[OpenclawPath(path="mcp.servers.synth.env.SYNTH_API_KEY", source="input.token")]
            ),
        )
        monkeypatch.setattr(connections.registry, "get_provider", lambda key: synthetic)

        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_patcher.set_openclaw_config_path = AsyncMock()

        response = await async_client.delete("/api/v1/connections/synth")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "synth"}

        # Both the retained secret and the status row are removed.
        mock_store.delete.assert_awaited_once_with("user_test_123", "synth")
        mock_repo.delete.assert_awaited_once_with("user_test_123", "synth")
        # openclaw_paths wiring: mcp.servers.synth.env.SYNTH_API_KEY -> blanked.
        mock_patcher.set_openclaw_config_path.assert_awaited_once_with(
            "user_test_123",
            ["mcp", "servers", "synth", "env", "SYNTH_API_KEY"],
            "",
        )

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.MATERIALIZER_REMOVERS")
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_materializer_provider_removes_materialized_creds(
        self, mock_repo, mock_store, mock_patcher, mock_removers, mock_accounts, async_client
    ):
        # google is a materializer-backed (gog_cli) actor-refresh provider. It
        # never stored a Secrets Manager credential, so get() returns None and
        # the remover is called with an empty credential (gog's remover keys off
        # owner/provider and ignores the credential anyway).
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_store.get = AsyncMock(return_value=None)
        remover = mock_removers.get.return_value
        mock_patcher.set_openclaw_config_path = AsyncMock()
        # Empty inventory keeps the focus on the materializer-cleanup assertions.
        mock_accounts.list_accounts = AsyncMock(return_value=[])
        mock_accounts.list_assignments = AsyncMock(return_value={})
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "google"}

        # Materialized credential files cleaned up; no SM secret -> empty cred.
        mock_removers.get.assert_called_once_with("gog_cli")
        remover.assert_called_once_with("user_test_123", "google", {})
        # Secret + status row both dropped (actor provider stored no secret -> no-op delete).
        mock_store.delete.assert_awaited_once_with("user_test_123", "google")
        mock_repo.delete.assert_awaited_once_with("user_test_123", "google")
        # No openclaw_paths on a materializer provider -> nothing blanked.
        mock_patcher.set_openclaw_config_path.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.MATERIALIZER_REMOVERS")
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_materializer_passes_retained_secret_to_remover(
        self, mock_repo, mock_store, mock_patcher, mock_removers, mock_accounts, async_client
    ):
        # Regression guard: some removers locate the on-disk artifact by a
        # credential field (e.g. toml_cli uses credential["cli"]). disconnect must
        # fetch the retained Secrets Manager credential and hand it to the remover
        # — passing an empty {} would leave the credential file on EFS. Uses the
        # google/gog_cli wiring as a stand-in materializer provider; the assertion
        # is that whatever get() returns is what the remover receives.
        retained = {"cli": "outlook", "access_token": "tok", "refresh_token": "r"}
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_store.get = AsyncMock(return_value=retained)
        remover = mock_removers.get.return_value
        mock_patcher.set_openclaw_config_path = AsyncMock()
        mock_accounts.list_accounts = AsyncMock(return_value=[])
        mock_accounts.list_assignments = AsyncMock(return_value={})
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google")
        assert response.status_code == 200

        mock_store.get.assert_awaited_once_with("user_test_123", "google")
        # The retained credential — not {} — is handed to the remover.
        remover.assert_called_once_with("user_test_123", "google", retained)

    @pytest.mark.asyncio
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_removes_mcp_server_block(self, mock_repo, mock_store, mock_patcher, async_client):
        # stripe is an mcp_server provider: disconnect removes the whole
        # mcp.servers.stripe block so no live-token server block lingers on EFS.
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_patcher.delete_openclaw_config_path = AsyncMock()

        response = await async_client.delete("/api/v1/connections/stripe")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "stripe"}

        # Secret + status row dropped, and the integration-owned server removed.
        mock_store.delete.assert_awaited_once_with("user_test_123", "stripe")
        mock_repo.delete.assert_awaited_once_with("user_test_123", "stripe")
        mock_patcher.delete_openclaw_config_path.assert_awaited_once_with("user_test_123", ["mcp", "servers", "stripe"])

    @pytest.mark.asyncio
    async def test_disconnect_unknown_provider_returns_404(self, async_client):
        response = await async_client.delete("/api/v1/connections/does-not-exist")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @patch("routers.connections.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.MATERIALIZER_REMOVERS")
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_all_clears_google_inventory(
        self, mock_repo, mock_store, mock_patcher, mock_removers, mock_accounts, mock_seed, async_client
    ):
        # A provider-level disconnect (no ?account=) must drop the whole keyring
        # AND clear every account + assignment from the owner's inventory.
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_store.get = AsyncMock(return_value=None)
        mock_patcher.set_openclaw_config_path = AsyncMock()
        remover = mock_removers.get.return_value
        mock_accounts.list_accounts = AsyncMock(return_value=[{"email": "a@x.com"}, {"email": "b@x.com"}])
        mock_accounts.list_assignments = AsyncMock(return_value={"agent_1": "a@x.com"})
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "google"}

        # Whole-keyring remove: remover called with empty credential (no email).
        remover.assert_called_once_with("user_test_123", "google", {})
        # Provider-level status row + secret dropped.
        mock_repo.delete.assert_awaited_once_with("user_test_123", "google")
        # Every inventory account removed.
        removed = {c.args[1] for c in mock_accounts.remove_account.await_args_list}
        assert removed == {"a@x.com", "b@x.com"}
        # Every assignment removed.
        mock_accounts.remove_assignment.assert_awaited_once_with("user_test_123", "agent_1")
        # Each un-assigned agent re-seeded so its IDENTITY.md gog-account block
        # is removed (the account it named is gone).
        mock_seed.assert_awaited_once_with("user_test_123", "agent_1")

    @pytest.mark.asyncio
    @patch("routers.connections.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.MATERIALIZER_REMOVERS")
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_single_google_account_removes_account_and_assignments(
        self, mock_repo, mock_store, mock_patcher, mock_removers, mock_accounts, mock_seed, async_client
    ):
        # ?account=<email> removes ONLY that account from the keyring + inventory,
        # and drops only the assignments pointing at it — other accounts and the
        # provider-level status row are untouched.
        mock_store.get = AsyncMock(return_value=None)
        remover = mock_removers.get.return_value
        mock_accounts.list_accounts = AsyncMock(return_value=[{"email": "a@x.com"}, {"email": "b@x.com"}])
        mock_accounts.list_assignments = AsyncMock(return_value={"agent_1": "a@x.com", "agent_2": "b@x.com"})
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock()
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_patcher.set_openclaw_config_path = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google?account=a@x.com")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "google", "account": "a@x.com"}

        # Per-account remove: remover called with the single email.
        remover.assert_called_once_with("user_test_123", "google", {"email": "a@x.com"})
        # Only that account removed from inventory.
        mock_accounts.remove_account.assert_awaited_once_with("user_test_123", "a@x.com")
        # Only the assignment pointing at a@x.com dropped; agent_2 (b@x.com) kept.
        mock_accounts.remove_assignment.assert_awaited_once_with("user_test_123", "agent_1")
        # Only the agent stripped of a@x.com is re-seeded (its block is removed);
        # agent_2, still on b@x.com, is left alone.
        mock_seed.assert_awaited_once_with("user_test_123", "agent_1")
        # Provider-level status row + secret NOT dropped on a single-account disconnect.
        mock_repo.delete.assert_not_called()
        mock_store.delete.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.MATERIALIZER_REMOVERS")
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_single_google_account_is_idempotent_when_absent(
        self, mock_repo, mock_store, mock_patcher, mock_removers, mock_accounts, async_client
    ):
        # A repeat disconnect of an already-gone account must succeed, not 500 —
        # the account isn't in the inventory, so the keyring remove is skipped.
        mock_store.get = AsyncMock(return_value=None)
        remover = mock_removers.get.return_value
        mock_accounts.list_accounts = AsyncMock(return_value=[{"email": "b@x.com"}])
        mock_accounts.list_assignments = AsyncMock(return_value={})
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google?account=a@x.com")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "google", "account": "a@x.com"}

        # Account absent -> no keyring remove attempted.
        remover.assert_not_called()
        # remove_account is still issued (a delete of an absent row is itself a no-op).
        mock_accounts.remove_account.assert_awaited_once_with("user_test_123", "a@x.com")

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.MATERIALIZER_REMOVERS")
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_single_google_account_tolerates_remover_not_found(
        self, mock_repo, mock_store, mock_patcher, mock_removers, mock_accounts, async_client
    ):
        # If the keyring remove races (gog exits 1 for an account it can't find),
        # the CalledProcessError is treated as already-gone success, not a 500.
        import subprocess

        mock_store.get = AsyncMock(return_value=None)
        remover = mock_removers.get.return_value
        remover.side_effect = subprocess.CalledProcessError(1, ["gog", "auth", "remove"])
        mock_accounts.list_accounts = AsyncMock(return_value=[{"email": "a@x.com"}])
        mock_accounts.list_assignments = AsyncMock(return_value={})
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google?account=a@x.com")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "google", "account": "a@x.com"}
        # Despite the remover error, the inventory row is still cleaned up.
        mock_accounts.remove_account.assert_awaited_once_with("user_test_123", "a@x.com")

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    async def test_disconnect_account_param_ignored_for_non_google_provider(self, mock_accounts, async_client):
        # The ?account= param is Google-specific; it must be rejected (not silently
        # honored) on a non-google provider so the inventory is never touched.
        mock_accounts.remove_account = AsyncMock()
        response = await async_client.delete("/api/v1/connections/stripe?account=a@x.com")
        assert response.status_code == 400
        mock_accounts.remove_account.assert_not_called()


class TestAuthorize:
    """GET /api/v1/connections/{provider}/authorize — start the OAuth dance."""

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_returns_consent_url(self, mock_dance, async_client, monkeypatch):
        # google is byo_required; an active BYO app lets authorize reach the
        # OAuth dance instead of the byo-required 400.
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=True))
        consent_url = "https://accounts.google.com/o/oauth2/v2/auth?x=1"
        mock_dance.start = AsyncMock(return_value=consent_url)

        response = await async_client.get("/api/v1/connections/google/authorize")
        assert response.status_code == 200
        assert response.json()["authorize_url"].startswith(consent_url)

        # Dance started with the server-derived redirect_uri.
        mock_dance.start.assert_awaited_once()
        call = mock_dance.start.await_args
        assert call.args[0] == "user_test_123"
        assert call.args[1] == "google"
        assert call.kwargs["redirect_uri"].endswith("/api/v1/oauth/google/callback")

    @pytest.mark.asyncio
    async def test_authorize_non_oauth_provider_returns_400(self, async_client):
        # stripe is paste_token, not an OAuth integration.
        response = await async_client.get("/api/v1/connections/stripe/authorize")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_authorize_unknown_provider_returns_404(self, async_client):
        response = await async_client.get("/api/v1/connections/does-not-exist/authorize")
        assert response.status_code == 404


class TestOAuthCallback:
    """GET /api/v1/oauth/{provider}/callback — PUBLIC; identity from state record."""

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_callback_happy_path_persists_and_redirects(
        self, mock_dance, mock_repo, mock_store, mock_apply, mock_accounts, async_client
    ):
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred))
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(return_value="user@example.com")
        mock_accounts.add_account = AsyncMock()

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        assert "/settings/connections?connected=google" in response.headers["location"]

        # Identity + credential recovered from the state record.
        mock_dance.exchange.assert_awaited_once()
        ex_call = mock_dance.exchange.await_args
        assert ex_call.args[0] == "st1"
        assert ex_call.args[1] == "abc"
        assert ex_call.kwargs["redirect_uri"].endswith("/api/v1/oauth/google/callback")

        # google is refresh_owner=actor: the container self-refreshes + owns the
        # durable credential, so the backend retains NOTHING in Secrets Manager.
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_awaited_once_with("org_1", "google", cred)
        # Status row written after a clean apply.
        mock_repo.put_status.assert_awaited_once_with("org_1", "google", "connected")

    @pytest.mark.asyncio
    @patch("routers.connections.MATERIALIZER_REMOVERS")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_callback_rolls_back_and_removes_efs_when_wiring_fails(
        self, mock_dance, mock_repo, mock_store, mock_apply, mock_removers, async_client
    ):
        # google is materializer-backed (gog_cli). If apply fails after the
        # materializer already wrote a partial keyring/client file, the rollback
        # must remove the on-disk artifact — so a failed connect leaves no durable
        # credential residue on EFS. google is actor-refresh -> no secret retained,
        # so there is no Secrets Manager delete to make, and no status row written.
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred))
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(side_effect=RuntimeError("efs write failed"))
        remover = mock_removers.get.return_value

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        # Redirect surfaces the apply failure rather than a false "connected".
        assert "error=apply_failed" in response.headers["location"]

        # EFS artifact removed (gog_cli remover) with the exchanged credential.
        mock_removers.get.assert_called_once_with("gog_cli")
        remover.assert_called_once_with("org_1", "google", cred)
        # Actor-refresh provider never retained a secret -> nothing to put or delete.
        mock_store.put.assert_not_called()
        mock_store.delete.assert_not_called()
        # No status row on the failure path.
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_google_callback_records_connected_account(
        self, mock_dance, mock_repo, mock_store, mock_apply, mock_accounts, async_client
    ):
        # A successful Google connect must record the connected account's email in
        # the per-owner inventory, on top of the provider-level "connected" status.
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred))
        mock_repo.put_status = AsyncMock()
        # apply_connection surfaces the connected account email (gog_cli rail).
        mock_apply.apply_connection = AsyncMock(return_value="alice@example.com")
        mock_accounts.add_account = AsyncMock()

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        assert "/settings/connections?connected=google" in response.headers["location"]

        # Provider-level status row still written.
        mock_repo.put_status.assert_awaited_once_with("org_1", "google", "connected")
        # The connected account is added to the owner's inventory.
        mock_accounts.add_account.assert_awaited_once_with("org_1", "alice@example.com")

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_two_google_callbacks_record_two_accounts(
        self, mock_dance, mock_repo, mock_store, mock_apply, mock_accounts, async_client
    ):
        # Connecting two different Google accounts yields two inventory records —
        # the keyring holds both, so the inventory must too.
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred))
        mock_repo.put_status = AsyncMock()
        mock_accounts.add_account = AsyncMock()

        mock_apply.apply_connection = AsyncMock(return_value="alice@example.com")
        await async_client.get("/api/v1/oauth/google/callback?code=a1&state=s1", follow_redirects=False)
        mock_apply.apply_connection = AsyncMock(return_value="bob@example.com")
        await async_client.get("/api/v1/oauth/google/callback?code=a2&state=s2", follow_redirects=False)

        emails = [c.args[1] for c in mock_accounts.add_account.await_args_list]
        assert emails == ["alice@example.com", "bob@example.com"]

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_non_google_callback_does_not_touch_account_inventory(
        self, mock_dance, mock_repo, mock_store, mock_apply, mock_accounts, async_client, monkeypatch
    ):
        # The Google inventory is provider-specific — a different OAuth provider's
        # callback must not record any Google account. Uses a synthetic OAuth,
        # backend-custodian provider so the generic path runs unchanged.
        from routers import connections
        from core.services.credentials.registry import OAuthSpec, Provider, Wiring

        synthetic = Provider(
            key="acme",
            display_name="Acme",
            acquisition="direct_oauth",
            actor="mcp",
            refresh_owner="none",
            oauth=OAuthSpec(
                authorization_url="https://acme.test/authorize",
                token_url="https://acme.test/token",
                scopes=["read"],
            ),
            wiring=Wiring(mcp_server={"key": "acme", "command": "npx", "env": {"TOKEN": "credentials.access_token"}}),
        )
        monkeypatch.setattr(connections.registry, "get_provider", lambda key: synthetic)

        cred = {"access_token": "a", "refresh_token": "r"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "acme", cred))
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(return_value=None)
        mock_accounts.add_account = AsyncMock()

        response = await async_client.get(
            "/api/v1/oauth/acme/callback?code=abc&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        # Generic flow unchanged: status written, Google inventory untouched.
        mock_repo.put_status.assert_awaited_once_with("org_1", "acme", "connected")
        mock_accounts.add_account.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_callback_bad_state_redirects_with_error_and_no_writes(
        self, mock_dance, mock_repo, mock_store, mock_apply, async_client
    ):
        mock_dance.exchange = AsyncMock(side_effect=ValueError("invalid or expired OAuth state"))
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=bad",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        assert "error=oauth_state" in response.headers["location"]

        # Nothing persisted / wired / indexed when state validation fails.
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()


@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_true_when_allowed_and_acknowledged(mock_get):
    from routers import connections

    mock_get.return_value = {"client_id": "x", "client_secret": "y", "acknowledged_at": "2026-06-18T00:00:00Z"}
    assert await connections._byo_active("user_1", "google") is True


@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_false_without_app(mock_get):
    from routers import connections

    mock_get.return_value = None
    assert await connections._byo_active("user_1", "google") is False


@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_false_when_app_not_acknowledged(mock_get):
    from routers import connections

    # A stored app with no `acknowledged_at` key is NOT active: storing client
    # credentials without the Limited-Use acknowledgement must not lift the gate.
    mock_get.return_value = {"client_id": "x", "client_secret": "y"}
    assert await connections._byo_active("user_1", "google") is False


@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_false_for_non_byo_provider(mock_get):
    from routers import connections

    mock_get.return_value = {"client_id": "x", "client_secret": "y", "acknowledged_at": "t"}
    # stripe is not byo_required → predicate short-circuits, never True
    assert await connections._byo_active("user_1", "stripe") is False


class TestByoApp:
    """PUT/DELETE /api/v1/connections/{provider}/byo-app — store/clear the
    owner's own OAuth app credentials, gated to providers that are
    ``byo_required``. The async_client owner is the personal owner
    ``user_test_123``."""

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.put", new_callable=AsyncMock)
    async def test_put_byo_app_requires_ack_and_stores(self, mock_put, async_client):
        # google byo_required=True: an acknowledged PUT stores the app.
        ok = await async_client.put(
            "/api/v1/connections/google/byo-app",
            json={"client_id": "cid", "client_secret": "csec", "acknowledge": True},
        )
        assert ok.status_code == 200
        mock_put.assert_awaited_once()
        # Stored under (owner, provider) with the submitted client credentials and
        # a timezone-aware ISO8601 acknowledgement timestamp (UTC -> "+00:00").
        put_call = mock_put.await_args
        assert put_call.args[0] == "user_test_123"
        assert put_call.args[1] == "google"
        assert put_call.kwargs["client_id"] == "cid"
        assert put_call.kwargs["client_secret"] == "csec"
        assert put_call.kwargs["acknowledged_at"].endswith("+00:00")

        # Acknowledgement without consent is rejected and stores nothing.
        mock_put.reset_mock()
        bad = await async_client.put(
            "/api/v1/connections/google/byo-app",
            json={"client_id": "cid", "client_secret": "csec", "acknowledge": False},
        )
        assert bad.status_code == 400
        mock_put.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.put", new_callable=AsyncMock)
    async def test_put_byo_app_rejected_for_non_byo_provider(self, mock_put, async_client):
        # stripe is not byo_required -> a BYO PUT is rejected (400) and nothing stored.
        resp = await async_client.put(
            "/api/v1/connections/stripe/byo-app",
            json={"client_id": "c", "client_secret": "s", "acknowledge": True},
        )
        assert resp.status_code in (400, 404)  # stripe is not byo_required
        mock_put.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.put", new_callable=AsyncMock)
    async def test_put_byo_app_unknown_provider_returns_404(self, mock_put, async_client):
        resp = await async_client.put(
            "/api/v1/connections/does-not-exist/byo-app",
            json={"client_id": "c", "client_secret": "s", "acknowledge": True},
        )
        assert resp.status_code == 404
        mock_put.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.delete", new_callable=AsyncMock)
    async def test_delete_byo_app_clears_stored_app(self, mock_delete, async_client):
        # DELETE clears the stored BYO app for (owner, provider). It does NOT
        # disconnect a live connection — only the app credentials are removed.
        resp = await async_client.delete("/api/v1/connections/google/byo-app")
        assert resp.status_code == 200
        mock_delete.assert_awaited_once_with("user_test_123", "google")

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.delete", new_callable=AsyncMock)
    async def test_delete_byo_app_rejected_for_non_byo_provider(self, mock_delete, async_client):
        # stripe is not byo_required -> DELETE is rejected (400) before any
        # store delete, mirroring PUT's guard instead of silently no-opping.
        resp = await async_client.delete("/api/v1/connections/stripe/byo-app")
        assert resp.status_code == 400
        mock_delete.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.delete", new_callable=AsyncMock)
    async def test_delete_byo_app_unknown_provider_returns_404(self, mock_delete, async_client):
        # An unknown provider 404s before any store delete (mirrors PUT).
        resp = await async_client.delete("/api/v1/connections/does-not-exist/byo-app")
        assert resp.status_code == 404
        mock_delete.assert_not_awaited()


class TestListByoExposure:
    """GET /api/v1/connections exposes byo_required / byo_configured / byo_setup_url
    per provider so the UI can render a BYO-app affordance."""

    @pytest.mark.asyncio
    @patch("routers.connections._byo_active", new_callable=AsyncMock)
    @patch("routers.connections.integrations_repo")
    async def test_list_exposes_byo_fields(self, mock_repo, mock_byo, async_client):
        # google is visible to every owner; assert the three BYO fields are
        # present and reflect the static flag + the live _byo_active result.
        mock_repo.list_for_owner = AsyncMock(return_value=[])
        mock_byo.return_value = True

        response = await async_client.get("/api/v1/connections")
        assert response.status_code == 200
        by_key = {c["provider"]: c for c in response.json()["connections"]}

        # google is byo_required and configured (mocked _byo_active True).
        assert by_key["google"]["byo_required"] is True
        assert by_key["google"]["byo_configured"] is True
        assert by_key["google"]["byo_setup_url"] == "https://console.cloud.google.com/apis/credentials"

        # stripe is not byo_required; no setup URL.
        assert by_key["stripe"]["byo_required"] is False
        assert by_key["stripe"]["byo_setup_url"] is None

    @pytest.mark.asyncio
    @patch("routers.connections._byo_active", new_callable=AsyncMock)
    @patch("routers.connections.integrations_repo")
    async def test_list_byo_configured_false_when_inactive(self, mock_repo, mock_byo, async_client):
        # byo_configured reflects _byo_active: False when no acknowledged app.
        mock_repo.list_for_owner = AsyncMock(return_value=[])
        mock_byo.return_value = False

        response = await async_client.get("/api/v1/connections")
        assert response.status_code == 200
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        assert by_key["google"]["byo_required"] is True
        assert by_key["google"]["byo_configured"] is False
