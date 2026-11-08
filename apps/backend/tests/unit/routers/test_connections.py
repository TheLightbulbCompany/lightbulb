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

from core.services.credentials import registry
from routers import connections


@pytest.fixture(autouse=True)
def _oauth_state(monkeypatch):
    consume = AsyncMock(return_value={"owner_id": "org_1", "provider": "google", "verifier": "pkce"})
    monkeypatch.setattr(connections.oauth_state_repo, "consume", consume)
    return consume


@pytest.fixture(autouse=True)
def _owner_lifecycle_lease(monkeypatch):
    """Disconnect and the OAuth callback take owner_lifecycle_guard around their write sections,
    and its lease is a conditional DynamoDB write -- admit it here so every test in this file that
    reaches either write section doesn't need a table. The guard itself is covered by
    ``tests/unit/services/test_owner_lifecycle.py`` against moto."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="lease-token"))
    monkeypatch.setattr(container_repo, "release_owner_lifecycle_claim", AsyncMock())
    monkeypatch.setattr(container_repo, "get_oauth_revoked_at", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def hermes_clear(monkeypatch):
    """``hermes_cli.clear`` reads the owner's roster and talks to S3. Every disconnect
    door calls it, and no test in this file is about that projection, so stub it once and
    hand the mock to the tests that assert on WHAT was cleared."""
    from routers import connections

    mock = AsyncMock()
    monkeypatch.setattr(connections.hermes_cli, "clear", mock)
    return mock


@pytest.fixture(autouse=True)
def _no_real_byo_secret_reads(monkeypatch):
    """Catalog tests should not fetch a real OAuth app from Secrets Manager."""
    monkeypatch.setattr(connections.oauth_app_store, "get", AsyncMock(return_value=None))


class TestValidateToken:
    """``_validate_token`` builds the Authorization header from the per-provider
    validate ``auth_scheme`` (vercel_cli = ``Bearer <key>``) and sends the
    optional JSON ``body`` when the spec defines one."""

    @pytest.mark.asyncio
    async def test_vercel_gets_with_bearer_authorization_header_and_no_body(self, monkeypatch):
        from routers import connections
        from core.services.credentials import registry

        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):
            captured["method"] = method
            captured["headers"] = kwargs.get("headers")
            captured["json"] = kwargs.get("json")
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        ok = await connections._validate_token(registry.get_provider("vercel_cli"), "vc_test_KEY")
        assert ok is True
        # vercel_cli: GET, default Bearer scheme, no JSON body.
        assert captured["method"] == "GET"
        assert captured["headers"]["Authorization"] == "Bearer vc_test_KEY"
        assert captured["json"] is None

    @pytest.mark.asyncio
    async def test_non_2xx_status_fails_validation(self, monkeypatch):
        from routers import connections
        from core.services.credentials import registry

        async def fake_request(self, method, url, **kwargs):
            return httpx.Response(401)

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        assert await connections._validate_token(registry.get_provider("vercel_cli"), "bad") is False

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

    def _host_pattern_provider(self):
        # A two-value paste provider ("<host>|<token>") whose validate URL is a
        # per-account template. Synthetic on purpose: no registry provider packs
        # two values any more (Shopify left the rail 2026-09-16), but the
        # host_pattern fence is still live code and stays covered.
        from core.services.credentials.registry import Provider, ValidateSpec, Wiring

        return Provider(
            key="hosted",
            display_name="Hosted",
            acquisition="paste_token",
            actor="mcp",
            refresh_owner="none",
            wiring=Wiring(mcp_server={"key": "hosted", "command": "npx", "env": {"TOKEN": "input.token"}}),
            validate=ValidateSpec(
                method="POST",
                url="https://{host}/admin/api/2026-07/graphql.json",
                host_pattern=r"[a-z0-9][a-z0-9-]{0,59}\.myshopify\.com",
                auth_header="X-Shopify-Access-Token",
                auth_scheme="",
                token_prefix="shpat_",
                body={"query": "{ shop { name } }"},
                success_json_field="data.shop.name",
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

    @pytest.mark.asyncio
    async def test_host_pattern_paste_builds_the_per_account_url(self, monkeypatch):
        # Shopify's admin API lives on the owner's own domain, so the one paste
        # field carries "<host>|<token>". The host half must build the URL and
        # the token half must be what lands in the auth header — sending the
        # packed blob as the credential would 401 against the real store.
        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return httpx.Response(200, json={"data": {"shop": {"name": "My Store"}}})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        ok = await connections._validate_token(self._host_pattern_provider(), "mystore.myshopify.com|shpat_abc")
        assert ok is True
        assert captured["url"] == "https://mystore.myshopify.com/admin/api/2026-07/graphql.json"
        assert captured["headers"]["X-Shopify-Access-Token"] == "shpat_abc"

    @pytest.mark.asyncio
    async def test_host_pattern_refuses_a_foreign_host_without_http(self, monkeypatch):
        # host_pattern is an SSRF fence, not a hint: this is the only validate URL
        # a caller can influence, so a host that doesn't match — or a paste with
        # no separator at all — must be refused BEFORE any request goes out.
        async def fake_request(self, method, url, **kwargs):
            raise AssertionError(f"validation must not fetch {url}")

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        p = self._host_pattern_provider()
        assert await connections._validate_token(p, "169.254.169.254|shpat_abc") is False
        assert await connections._validate_token(p, "evil.com|shpat_abc") is False
        assert await connections._validate_token(p, "shpat_abc") is False

    @pytest.mark.asyncio
    async def test_host_pattern_prefix_check_applies_to_the_token_half(self, monkeypatch):
        # token_prefix guards the credential, not the packed string. Shopify shows
        # the custom app's API key next to its access token and owners paste the
        # wrong one — that must fail pre-flight, as it does for every other
        # prefixed provider.
        async def fake_request(self, method, url, **kwargs):
            raise AssertionError("HTTP must not run for a wrong-prefix token")

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        ok = await connections._validate_token(self._host_pattern_provider(), "mystore.myshopify.com|not-a-token")
        assert ok is False

    @pytest.mark.asyncio
    async def test_shopify_mints_a_client_credentials_token_then_queries_the_store(self, monkeypatch):
        # Dev Dashboard apps hand out a Client ID + secret, never an Admin API token: validation
        # has to run the client-credentials grant on the owner's store and then prove the minted
        # token against GraphQL, exactly as the agent's skill will.
        calls: list = []

        async def fake_request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/admin/oauth/access_token"):
                return httpx.Response(200, json={"access_token": "a" * 32, "scope": "read_orders", "expires_in": 86399})
            return httpx.Response(200, json={"data": {"shop": {"name": "My Store"}}})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        ok = await connections._validate_token(registry.get_provider("shopify"), "mystore.myshopify.com|cid:shpss_sec")
        assert ok is True
        (m1, u1, k1), (m2, u2, k2) = calls
        assert (m1, u1) == ("POST", "https://mystore.myshopify.com/admin/oauth/access_token")
        assert k1["data"] == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "shpss_sec"}
        assert (m2, u2) == ("POST", "https://mystore.myshopify.com/admin/api/2026-07/graphql.json")
        assert k2["headers"]["X-Shopify-Access-Token"] == "a" * 32

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant", [httpx.Response(401, text="bad"), httpx.Response(200, text="<html>")])
    async def test_shopify_failed_grant_is_invalid_and_skips_graphql(self, monkeypatch, grant):
        calls: list = []

        async def fake_request(self, method, url, **kwargs):
            calls.append(url)
            return grant

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        ok = await connections._validate_token(registry.get_provider("shopify"), "mystore.myshopify.com|cid:sec")
        assert ok is False
        assert calls == ["https://mystore.myshopify.com/admin/oauth/access_token"]

    @pytest.mark.asyncio
    async def test_shopify_foreign_host_is_refused_before_the_grant(self, monkeypatch):
        async def fake_request(self, method, url, **kwargs):
            raise AssertionError(f"validation must not fetch {url}")

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        p = registry.get_provider("shopify")
        assert await connections._validate_token(p, "169.254.169.254|cid:sec") is False
        assert await connections._validate_token(p, "evil.com|cid:sec") is False


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
        assert by_key["stripe"]["acquisition"] == "direct_oauth"
        assert by_key["google"]["acquisition"] == "direct_oauth"
        # The rail rides every row so the UI can badge MCP vs CLI vs skill —
        # a connected CLI integration must not read as a broken MCP one.
        assert by_key["stripe"]["actor"] == "mcp"
        assert by_key["google"]["actor"] == "cli"
        # A multi-field paste provider ships its labelled inputs; the rest ship none.
        assert [f["key"] for f in by_key["shopify"]["paste_fields"]] == ["shop", "client_id", "client_secret"]
        assert by_key["shopify"]["paste_fields"][2]["secret"] is True
        assert by_key["stripe"]["paste_fields"] is None

        # Status now comes from the secret-free integrations index.
        mock_repo.list_for_owner.assert_awaited_once_with("org_test_456")

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_list_returns_reconnect_reason_only_while_reconnect_required(self, mock_repo, async_client):
        mock_repo.list_for_owner = AsyncMock(
            return_value=[
                {"provider": "stripe", "status": "reconnect_required", "reconnect_reason": "invalid_grant"},
                # A stale reason on a connected row is never surfaced.
                {"provider": "linear", "status": "connected", "reconnect_reason": "invalid_grant"},
            ]
        )

        by_key = {c["provider"]: c for c in (await async_client.get("/api/v1/connections")).json()["connections"]}

        assert by_key["stripe"]["reconnect_reason"] == "invalid_grant"
        assert by_key["linear"]["reconnect_reason"] is None
        assert by_key["google"]["reconnect_reason"] is None

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_google_visible_to_every_owner(self, mock_repo, async_client, monkeypatch):
        mock_repo.list_for_owner = AsyncMock(return_value=[])  # no connected rows
        response = await async_client.get("/api/v1/connections")
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        assert "google" in by_key
        assert by_key["google"]["byo_required"] is False
        assert "requires_bedrock" not in by_key["google"]

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_can_trigger_reflects_registry_inbound_events(self, mock_repo, async_client):
        # stripe declares `inbound` (T1) -> can_trigger is its event labels.
        # google is a trigger source too (auto_google_watch); notion declares
        # no `inbound` -> can_trigger is empty.
        mock_repo.list_for_owner = AsyncMock(return_value=[])
        response = await async_client.get("/api/v1/connections")
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        # Full catalog now — assert the curated "popular" prefix (the registry
        # orders those first) rather than pinning all ~265 labels here.
        assert by_key["stripe"]["can_trigger"][:3] == [
            "Invoice payment failed",
            "Charge refunded",
            "Subscription canceled",
        ]
        assert len(by_key["stripe"]["can_trigger"]) >= 200
        assert by_key["google"]["can_trigger"] == [
            "Calendar changed",
            "Drive file changed",
        ]
        assert by_key["notion"]["can_trigger"] == []

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_inbound_carries_event_ids_and_registration_mode(self, mock_repo, async_client):
        # Task 6: the Add-flow needs event IDS (not just labels) plus the
        # registration mode (guided_paste) to submit
        # POST /automations/from-integration. `can_trigger` (labels only)
        # stays untouched for the Integrations card.
        mock_repo.list_for_owner = AsyncMock(return_value=[])
        response = await async_client.get("/api/v1/connections")
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        # Events carry their per-event `registration` override (None =
        # inherit the block level) so the frontend can branch per-event.
        # Full catalog now — pin the shape via the curated "popular" prefix
        # (the registry orders those first) rather than all ~265 events.
        assert by_key["stripe"]["inbound"]["webhook_registration"] == "guided_paste"
        assert by_key["stripe"]["inbound"]["events"][:3] == [
            {
                "id": "invoice.payment_failed",
                "label": "Invoice payment failed",
                "registration": None,
                "starter_prompt": None,
            },
            {"id": "charge.refunded", "label": "Charge refunded", "registration": None, "starter_prompt": None},
            {
                "id": "customer.subscription.deleted",
                "label": "Subscription canceled",
                "registration": None,
                "starter_prompt": None,
            },
        ]
        # The template the seed renders server-side, so the create dialog can
        # pre-fill an EDITABLE draft of the same title/instructions.
        assert by_key["stripe"]["inbound"]["template"]["title"] == "Stripe: {event_label}"
        assert "{event_label}" in by_key["stripe"]["inbound"]["template"]["starter_prompt"]
        # google is auto-registering (auto_google_watch); notion still declares no `inbound`.
        google = by_key["google"]["inbound"]
        assert google["webhook_registration"] == "auto_google_watch"
        assert [{k: e[k] for k in ("id", "label", "registration")} for e in google["events"]] == [
            {"id": "calendar.changed", "label": "Calendar changed", "registration": None},
            {"id": "drive.changed", "label": "Drive file changed", "registration": None},
        ]
        # Per-event starter_prompt overrides ride along — the draft has to
        # apply the same precedence the seed does (event wins over template).
        by_event = {e["id"]: e["starter_prompt"] for e in google["events"]}
        assert by_event["calendar.changed"] is None
        assert by_key["notion"]["inbound"] is None


class TestEnvGatedProvider:
    """A provider with an `environments` list (Instagram: [local, dev]) is
    listed everywhere but only connectable within those envs — outside them
    it's listed as requestable=true/status=unavailable so the UI can offer
    Request access. The test env resolves to dev (empty ENVIRONMENT), so it's
    connectable by default; force prod to prove the gate."""

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_instagram_listed_in_dev(self, mock_repo, async_client):
        mock_repo.list_for_owner = AsyncMock(return_value=[])
        response = await async_client.get("/api/v1/connections")
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        assert "instagram" in by_key

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_gated_provider_is_hidden_in_prod_by_default(self, mock_repo, async_client, monkeypatch):
        # A gate still means HIDDEN. Being requestable is an explicit opt-in
        # (offer_request_access), so a half-wired dev-only provider is never
        # advertised to a prod owner just because it exists in the registry.
        mock_repo.list_for_owner = AsyncMock(return_value=[])
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        response = await async_client.get("/api/v1/connections")
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        assert registry.get_provider("instagram").offer_request_access is False
        assert "instagram" not in by_key
        # A non-gated provider is unaffected.
        assert "google" in by_key

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_opted_in_gated_provider_is_listed_as_requestable(self, mock_repo, async_client, monkeypatch):
        # With offer_request_access=true the gated provider IS listed, with
        # requestable=true and status=unavailable, so the UI offers Request
        # access instead of hiding the integration entirely.
        mock_repo.list_for_owner = AsyncMock(return_value=[])
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        p = registry.get_provider("instagram").model_copy(update={"offer_request_access": True})
        monkeypatch.setattr(registry, "all_providers", lambda: {"instagram": p})
        response = await async_client.get("/api/v1/connections")
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        assert by_key["instagram"]["requestable"] is True
        assert by_key["instagram"]["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_instagram_authorize_404s_in_prod(self, async_client, monkeypatch):
        # Gated provider is listed as requestable, but the connect path still
        # 404s outside its envs, so it can't be reached by a hand-crafted
        # request either.
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        response = await async_client.get("/api/v1/connections/instagram/authorize")
        assert response.status_code == 404

    def test_gated_provider_is_listed_as_requestable(self, monkeypatch):
        """A provider gated off this env is LISTED (not skipped) with
        requestable=true, so the UI can offer Request access instead of hiding
        the integration entirely."""
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        p = registry.get_provider("instagram")
        assert connections._available_for(p, "org_other") is False
        # projection helper is what the route uses per provider
        row = connections._connection_row("instagram", p, connected={}, byo=False, owner_id="org_other")
        assert row["requestable"] is True
        assert row["status"] == "unavailable"

    def test_unsupported_hermes_provider_is_unavailable_for_new_connections(self):
        p = registry.Provider(
            key="synthetic_unsupported",
            display_name="Synthetic",
            acquisition="paste_token",
            actor="cli",
            refresh_owner="none",
            wiring=registry.Wiring(materializer="toml_cli"),
        )
        row = connections._connection_row(p.key, p, connected={}, byo=False, owner_id="org_1")
        assert row["requestable"] is False
        assert row["status"] == "unavailable"

    def test_existing_unsupported_connection_remains_visible_for_disconnect(self):
        p = registry.Provider(
            key="synthetic_unsupported",
            display_name="Synthetic",
            acquisition="paste_token",
            actor="cli",
            refresh_owner="none",
            wiring=registry.Wiring(materializer="toml_cli"),
        )
        row = connections._connection_row(p.key, p, connected={p.key: "connected"}, byo=False, owner_id="org_1")
        assert row["requestable"] is False
        assert row["status"] == "connected"
        assert row["runtime_supported"] is False

    @pytest.mark.asyncio
    async def test_unsupported_oauth_does_not_start_provider_consent(self, monkeypatch):
        # Fail-closed 503 gate: consent never starts when the provider has no usable
        # Hermes credential path. No real provider currently trips this in prod (the
        # broker promotion moved every remote MCP onto the workspace broker), so the
        # unsupported leg is forced directly rather than pinned to registry data.
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        monkeypatch.setattr(connections.apply_connection, "runtime_supported", lambda p: False)
        started = AsyncMock()
        monkeypatch.setattr(connections.oauth_dance, "start", started)
        with pytest.raises(connections.HTTPException) as exc:
            await connections._start_oauth("org_1", "stripe")
        assert exc.value.status_code == 503
        started.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_brokered_oauth_starts_provider_consent_in_prod(self, monkeypatch):
        # The broker promotion made stripe runtime-supported in prod, so consent starts.
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        started = AsyncMock(return_value="https://provider.example/authorize")
        monkeypatch.setattr(connections.oauth_dance, "start", started)
        result = await connections._start_oauth("org_1", "stripe")
        assert result == {"authorize_url": "https://provider.example/authorize"}
        started.assert_awaited_once()

    def test_allowlisted_owner_is_not_requestable(self, monkeypatch):
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        p = registry.get_provider("instagram")
        # Read the real allowlist rather than a literal, so repointing it in the
        # YAML doesn't need a test edit — but fail loudly if it is ever emptied,
        # instead of quietly falling back to an org that proves nothing.
        assert p.owner_allowlist, "instagram must keep a prod-dogfood owner_allowlist"
        owner = p.owner_allowlist[0]
        row = connections._connection_row("instagram", p, connected={}, byo=False, owner_id=owner)
        assert row["requestable"] is False
        assert row["status"] == "disconnected"

    def test_available_for_owner_allowlist(self, monkeypatch):
        # A dev-gated provider with an owner_allowlist: open to everyone in dev,
        # and in prod ONLY to the allowlisted owner (the prod-dogfood escape hatch).
        p = registry.Provider(
            key="x",
            display_name="X",
            acquisition="direct_oauth",
            actor="cli",
            refresh_owner="none",
            wiring={"materializer": "instagram_cli"},
            oauth={
                "authorization_url": "https://x/a",
                "token_url": "https://x/t",
                "scopes": ["a"],
                "platform_app": "meta-x",
            },
            environments=["local", "dev"],
            owner_allowlist=["org_founder"],
        )
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "dev")
        assert connections._available_for(p, "org_anyone") is True  # dev: open to all
        assert connections._available_for(p, None) is True
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        assert connections._available_for(p, "org_founder") is True  # prod: allowlisted only
        assert connections._available_for(p, "org_other") is False
        assert connections._available_for(p, None) is False

    def test_available_for_ungated_provider(self, monkeypatch):
        # No environments list → available everywhere, allowlist irrelevant.
        p = registry.get_provider("stripe")
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        assert connections._available_for(p, "org_anyone") is True


class TestByoRequiredGate:
    """A `byo_required` provider (x) has no managed app, so its connect
    path requires the owner to have stored their own OAuth app first. The
    catalog lists it for everyone; authorize 400s until a BYO app is
    configured."""

    @pytest.mark.asyncio
    async def test_authorize_400_when_byo_app_not_configured(self, async_client, monkeypatch):
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=False))
        response = await async_client.get("/api/v1/connections/x/authorize")
        assert response.status_code == 400

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_succeeds_when_byo_app_configured(self, mock_dance, async_client, monkeypatch):
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=True))
        mock_dance.start = AsyncMock(return_value="https://accounts.google.com/o/oauth2/v2/auth?x=1")
        response = await async_client.get("/api/v1/connections/x/authorize")
        assert response.status_code == 200
        assert response.json()["authorize_url"].startswith("https://accounts.google.com")

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_returns_502_not_500_when_oauth_start_fails(self, mock_dance, async_client, monkeypatch):
        # A provider's OAuth/DCR endpoint can return 4xx/5xx (e.g. Vercel's DCR
        # rejected our client registration with 400). That must surface as a
        # clean, retryable error — NOT a 500 that reads as a crash in a demo.
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=True))
        req = httpx.Request("POST", "https://example.com/oauth/register")
        resp = httpx.Response(400, request=req)
        mock_dance.start = AsyncMock(side_effect=httpx.HTTPStatusError("400 Bad Request", request=req, response=resp))
        response = await async_client.get("/api/v1/connections/x/authorize")
        assert response.status_code == 502

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_502_logs_the_providers_refusal(self, mock_dance, async_client, monkeypatch, caplog):
        # Intercom's DCR 502'd in prod with only an access-log line; the reason must be logged.
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=True))
        req = httpx.Request("POST", "https://mcp.intercom.com/register")
        resp = httpx.Response(400, request=req, text='{"error":"invalid_redirect_uri"}')
        mock_dance.start = AsyncMock(side_effect=httpx.HTTPStatusError("400", request=req, response=resp))
        with caplog.at_level("WARNING", logger="routers.connections"):
            response = await async_client.get("/api/v1/connections/x/authorize")
        assert response.status_code == 502
        assert "mcp.intercom.com/register" in caplog.text
        assert "status=400" in caplog.text
        assert "invalid_redirect_uri" in caplog.text

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_returns_503_not_500_when_platform_app_unseeded(self, mock_dance, async_client):
        # A platform-app provider (instagram) whose shared client hasn't been
        # seeded into Secrets Manager yet: _oauth_client raises ValueError. That
        # must degrade to a clean 503, not an uncaught 500 — the seed step is an
        # operator gap, and a user clicking Connect before it lands should see a
        # retryable message.
        mock_dance.start = AsyncMock(
            side_effect=ValueError("instagram: platform OAuth app 'meta-instagram' is not provisioned")
        )
        response = await async_client.get("/api/v1/connections/instagram/authorize")
        assert response.status_code == 503


class TestConnectPaste:
    """POST /api/v1/connections/{provider} — paste-key connect."""

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_labelled_fields_are_joined_server_side(
        self, mock_validate, mock_repo, mock_store, mock_apply, async_client
    ):
        # The owner fills three labelled inputs; nobody types "<shop>|<id>:<secret>".
        mock_validate.return_value = True
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        fields = {"shop": " mystore.myshopify.com ", "client_id": "cid", "client_secret": "shpss_sec"}
        response = await async_client.post("/api/v1/connections/shopify", json={"fields": fields})

        assert response.status_code == 200
        joined = "mystore.myshopify.com|cid:shpss_sec"
        assert mock_validate.await_args.args[1] == joined
        assert mock_store.put.await_args.args[2] == {"token": joined}

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_a_pasted_admin_url_normalises_to_the_store_host(
        self, mock_validate, mock_repo, mock_store, mock_apply, async_client
    ):
        # Owners copy the store address from the browser bar; only the host field is normalised
        # (a Client ID is case-sensitive and must reach the grant untouched).
        mock_validate.return_value = True
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        fields = {"shop": "https://MyStore.myshopify.com/admin/", "client_id": "CiD", "client_secret": "s"}
        response = await async_client.post("/api/v1/connections/shopify", json={"fields": fields})
        assert response.status_code == 200
        assert mock_validate.await_args.args[1] == "mystore.myshopify.com|CiD:s"

    @pytest.mark.asyncio
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_a_missing_field_is_named(self, mock_validate, async_client):
        response = await async_client.post(
            "/api/v1/connections/shopify", json={"fields": {"shop": "mystore.myshopify.com", "client_id": "cid"}}
        )
        assert response.status_code == 422
        assert "Client secret" in response.json()["detail"]
        mock_validate.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.integration_secret_store")
    async def test_a_rejected_grant_is_the_generic_422_and_never_echoes_the_secret(
        self, mock_store, async_client, monkeypatch
    ):
        real_request = httpx.AsyncClient.request

        async def fake_request(self, method, url, **kwargs):
            if "myshopify.com" not in str(url):  # the test client itself rides httpx
                return await real_request(self, method, url, **kwargs)
            return httpx.Response(401, json={"error": "invalid_client"})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        mock_store.put = AsyncMock()

        fields = {"shop": "mystore.myshopify.com", "client_id": "cid", "client_secret": "shpss_TOPSECRET"}
        response = await async_client.post("/api/v1/connections/shopify", json={"fields": fields})

        assert response.status_code == 422
        assert response.json()["detail"] == "token failed validation against the provider"
        assert "TOPSECRET" not in response.text
        mock_store.put.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_that_began_before_revoke_all_stores_nothing(
        self, mock_validate, mock_repo, mock_store, mock_apply, async_client, monkeypatch
    ):
        import time as _time

        from core.repositories import container_repo

        monkeypatch.setattr(
            container_repo, "get_oauth_revoked_at", AsyncMock(return_value=int(_time.time() * 1000) + 60_000)
        )
        mock_validate.return_value = True
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client.post("/api/v1/connections/vercel_cli", json={"token": "vc_test_abc123"})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "revoked"
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

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

        response = await async_client.post("/api/v1/connections/vercel_cli", json={"token": "vc_test_abc123"})
        assert response.status_code == 200
        assert response.json() == {"status": "connected", "provider": "vercel_cli"}

        # Credential persisted to Secrets Manager with the pasted token.
        mock_store.put.assert_awaited_once()
        put_args = mock_store.put.await_args
        assert put_args.args[0] == "org_test_456"
        assert put_args.args[1] == "vercel_cli"
        assert put_args.args[2] == {"token": "vc_test_abc123"}

        # Wiring engine invoked with (owner, provider, credential).
        mock_apply.apply_connection.assert_awaited_once_with("org_test_456", "vercel_cli", {"token": "vc_test_abc123"})

        # Status row written ONLY after a successful apply.
        assert mock_repo.put_status.await_count == 1
        assert mock_repo.put_status.await_args.args == ("org_test_456", "vercel_cli", "connected")

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
    async def test_resume_after_connect_no_signin(
        self, mock_validate, mock_repo, mock_store, mock_apply, async_client, monkeypatch
    ):
        """A connect applies, stores the status, then rewrites owner facts."""
        mock_validate.return_value = True
        order: list[str] = []
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock(side_effect=lambda *a, **k: order.append("apply"))
        mock_repo.put_status = AsyncMock(side_effect=lambda *a, **k: order.append("put_status"))
        monkeypatch.setattr(connections, "write_owner_facts", AsyncMock(side_effect=lambda *a: order.append("facts")))

        response = await async_client.post("/api/v1/connections/vercel_cli", json={"token": "vc_test_abc123"})
        assert response.status_code == 200
        assert order == ["apply", "put_status", "facts"]

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_a_successful_connect_invalidates_the_activity_view(
        self, mock_validate, mock_repo, mock_store, mock_apply, async_client, monkeypatch
    ):
        mock_validate.return_value = True
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()
        publish = AsyncMock()
        monkeypatch.setattr("core.services.live_events.publish_owner_users", publish)

        response = await async_client.post("/api/v1/connections/vercel_cli", json={"token": "vc_test_abc123"})
        assert response.status_code == 200
        publish.assert_awaited_once()
        assert publish.await_args.args[0] == "org_test_456"
        assert publish.await_args.args[1]["payload"]["resource"] == "activity"

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

        response = await async_client.post("/api/v1/connections/vercel_cli", json={"token": "bad"})
        assert response.status_code == 422
        # Nothing persisted / wired / indexed when validation fails.
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_rolls_back_secret_when_wiring_fails(
        self, mock_validate, mock_repo, mock_store, mock_apply, async_client, hermes_clear
    ):
        mock_validate.return_value = True
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(side_effect=RuntimeError("efs write failed"))

        response = await async_client.post("/api/v1/connections/parallel", json={"token": "pk_test_abc123"})
        # Wiring failure surfaces as a 502 — the connection never reports a false "connected".
        assert response.status_code == 502

        # The secret was stored, then rolled back out of Secrets Manager.
        mock_store.put.assert_awaited_once()
        mock_store.delete.assert_awaited_once_with("org_test_456", "parallel")
        # Status is written only after a clean apply -> never on the failure path.
        mock_repo.put_status.assert_not_called()
        # parallel rides openclaw_paths (no materializer) -> the projection rollback is a no-op.
        hermes_clear.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_returns_422_for_an_openclaw_paths_provider_with_no_hermes_equivalent(
        self, mock_validate, mock_repo, mock_store, async_client, monkeypatch
    ):
        """M6-R31: an openclaw_paths provider absent from HERMES_OPENCLAW_PATHS_PROVIDERS
        answers a specific 422 RUNTIME_UNSUPPORTED on an AgentCore owner, not the generic
        apply_failed/502 -- and the row is never marked connected, same rollback as the
        generic failure path. apply_connection itself runs for real here (not mocked out)
        so the actual RuntimeUnsupportedError class is what the router catches."""
        from routers import connections
        from core.services.credentials.registry import OpenclawPath, Provider, Wiring

        synthetic = Provider(
            key="synth",
            display_name="Synthetic",
            acquisition="paste_token",
            actor="mcp",
            refresh_owner="none",
            wiring=Wiring(openclaw_paths=[OpenclawPath(path="a.b.c", source="input.token")]),
        )
        monkeypatch.setattr(connections.registry, "get_provider", lambda key: synthetic)
        mock_validate.return_value = True
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_repo.put_status = AsyncMock()

        response = await async_client.post("/api/v1/connections/synth", json={"token": "lin_secret"})

        assert response.status_code == 422
        assert "RUNTIME_UNSUPPORTED" in response.json()["detail"]
        mock_store.put.assert_awaited_once()
        mock_store.delete.assert_awaited_once_with("org_test_456", "synth")
        mock_repo.put_status.assert_not_called()

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
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_non_admin_member_forbidden(self, mock_repo, mock_store, async_client_org_member):
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        response = await async_client_org_member.delete("/api/v1/connections/stripe")
        assert response.status_code == 403
        # Nothing deleted / unwired for a non-admin member.
        mock_repo.delete.assert_not_called()
        mock_store.delete.assert_not_called()

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

        response = await async_client_org_admin.post("/api/v1/connections/vercel_cli", json={"token": "vc_test_abc123"})
        # Admin clears the gate; write targets the shared org workspace.
        assert response.status_code == 200
        assert response.json() == {"status": "connected", "provider": "vercel_cli"}
        put_args = mock_store.put.await_args
        assert put_args.args[0] == "org_test_456"
        mock_apply.apply_connection.assert_awaited_once_with("org_test_456", "vercel_cli", {"token": "vc_test_abc123"})
        assert mock_repo.put_status.await_count == 1
        assert mock_repo.put_status.await_args.args == ("org_test_456", "vercel_cli", "connected")

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
    @patch("routers.connections.oauth_app_store")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_leaves_dcr_client_intact(
        self, mock_repo, mock_store, mock_app_store, async_client, monkeypatch, tmp_path
    ):
        # attio is a DCR provider. Disconnect must NOT delete the cached DCR client:
        # a delete+recreate of the same Secrets Manager name races AWS's async name
        # release on a quick reconnect. start() re-registers (overwrites) on a scope
        # change instead, so the cached client is deliberately left in place.
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_store.get = AsyncMock(return_value=None)  # actor-refresh: no retained secret
        mock_app_store.delete = AsyncMock()

        response = await async_client.delete("/api/v1/connections/attio")
        assert response.status_code == 200
        mock_app_store.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnect_unknown_provider_returns_404(self, async_client):
        response = await async_client.delete("/api/v1/connections/does-not-exist")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_a_retired_provider_still_drops_our_copies(
        self, mock_repo, mock_store, async_client, monkeypatch
    ):
        """A provider whose registry row is gone may still have a Secrets Manager blob and an
        integrations row. Revocation has to outlive the provider definition -- a credential the UI
        cannot delete is the worse state."""
        monkeypatch.setattr(connections, "RETIRED_PROVIDERS", frozenset({"gone_provider"}))
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()

        response = await async_client.delete("/api/v1/connections/gone_provider")

        assert response.status_code == 200
        assert response.json()["status"] == "disconnected"
        mock_store.delete.assert_awaited_once_with("org_test_456", "gone_provider")
        mock_repo.delete.assert_awaited_once_with("org_test_456", "gone_provider")

    @pytest.mark.asyncio
    async def test_shopify_is_live_again_not_retired(self):
        """Shopify is back as a client-credentials provider, so its disconnect runs the full
        teardown (including the skill-dir removal), not the tolerant row-only branch."""
        assert "shopify" not in connections.RETIRED_PROVIDERS
        assert "shopify" in registry.all_providers()

    @pytest.mark.asyncio
    @patch("routers.connections.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_all_clears_google_inventory(
        self, mock_repo, mock_store, mock_accounts, mock_seed, async_client, hermes_clear
    ):
        # A provider-level disconnect (no ?account=) must drop the whole keyring
        # AND clear every account + assignment from the owner's inventory.
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_store.get = AsyncMock(return_value=None)
        mock_accounts.list_accounts = AsyncMock(return_value=[{"email": "a@x.com"}, {"email": "b@x.com"}])
        mock_accounts.list_assignments = AsyncMock(return_value={"agent_1": "a@x.com"})
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "google"}

        # Whole-keyring remove: provider-level disconnect_all + the account
        # inventory, so gog revokes each grant at Google before the wipe.
        hermes_clear.assert_awaited_once_with(
            "org_test_456", "google", "gog_cli", {"disconnect_all": True, "accounts": ["a@x.com", "b@x.com"]}
        )
        # Provider-level status row + secret dropped.
        mock_repo.delete.assert_awaited_once_with("org_test_456", "google")
        # Every inventory account removed.
        removed = {c.args[1] for c in mock_accounts.remove_account.await_args_list}
        assert removed == {"a@x.com", "b@x.com"}
        # Every assignment removed.
        mock_accounts.remove_assignment.assert_awaited_once_with("org_test_456", "agent_1")
        # Each un-assigned agent re-seeded so its IDENTITY.md gog-account block
        # is removed (the account it named is gone).
        mock_seed.assert_awaited_once_with("org_test_456", "agent_1")

    @pytest.mark.asyncio
    @patch("routers.connections.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_single_google_account_removes_account_and_assignments(
        self, mock_repo, mock_store, mock_accounts, mock_seed, async_client, hermes_clear
    ):
        # ?account=<email> removes ONLY that account from the keyring + inventory,
        # and drops only the assignments pointing at it — other accounts and the
        # provider-level status row are untouched.
        mock_store.get = AsyncMock(return_value=None)
        mock_accounts.list_accounts = AsyncMock(return_value=[{"email": "a@x.com"}, {"email": "b@x.com"}])
        assignments = {"agent_1": "a@x.com", "agent_2": "b@x.com"}
        mock_accounts.list_assignments = AsyncMock(side_effect=lambda owner_id: dict(assignments))
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock(
            side_effect=lambda owner_id, agent_id: assignments.pop(agent_id, None)
        )
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google?account=a@x.com")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "google", "account": "a@x.com"}

        # Per-account remove: gog run against a scratch copy, with the single email.
        hermes_clear.assert_awaited_once_with("org_test_456", "google", "gog_cli", {"email": "a@x.com"})
        # Only that account removed from inventory.
        mock_accounts.remove_account.assert_awaited_once_with("org_test_456", "a@x.com")
        # Only the assignment pointing at a@x.com dropped; agent_2 (b@x.com) kept.
        mock_accounts.remove_assignment.assert_awaited_once_with("org_test_456", "agent_1")
        # agent_1 is re-seeded so its block goes with its account; agent_2 is re-seeded
        # because the projection's cli-env.json is provider-keyed, so this disconnect wiped
        # its GOG_ACCOUNT too (see the disconnect-A-keeps-B test below).
        assert [c.args for c in mock_seed.await_args_list] == [
            ("org_test_456", "agent_1"),
            ("org_test_456", "agent_2"),
        ]
        # Provider-level status row + secret NOT dropped on a single-account disconnect.
        mock_repo.delete.assert_not_called()
        mock_store.delete.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnecting_one_google_account_reprojects_the_survivors(
        self, mock_repo, mock_store, mock_accounts, mock_seed, async_client
    ):
        """On AgentCore the binding lives as GOG_ACCOUNT in each agent's profile
        ``cli-env.json``, which the PROVIDER-keyed projection deletes on any google
        disconnect. Nothing removes the row of an agent bound to an account that SURVIVES,
        so without an explicit re-seed that agent loses its binding permanently."""
        mock_store.get = AsyncMock(return_value=None)
        mock_accounts.list_accounts = AsyncMock(return_value=[{"email": "a@x.com"}, {"email": "b@x.com"}])
        # Nothing here points at the disconnected account: agent_2 is never touched by the
        # removal loop, and is exactly the agent the old code left stripped.
        mock_accounts.list_assignments = AsyncMock(return_value={"agent_2": "b@x.com"})
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google?account=a@x.com")
        assert response.status_code == 200

        mock_accounts.remove_assignment.assert_not_called()
        mock_seed.assert_awaited_once_with("org_test_456", "agent_2")

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_single_google_account_is_idempotent_when_absent(
        self, mock_repo, mock_store, mock_accounts, async_client, hermes_clear
    ):
        # A repeat disconnect of an already-gone account must succeed, not 500 —
        # the account isn't in the inventory, so the keyring remove is skipped.
        mock_store.get = AsyncMock(return_value=None)
        mock_accounts.list_accounts = AsyncMock(return_value=[{"email": "b@x.com"}])
        mock_accounts.list_assignments = AsyncMock(return_value={})
        mock_accounts.remove_account = AsyncMock()
        mock_accounts.remove_assignment = AsyncMock()

        response = await async_client.delete("/api/v1/connections/google?account=a@x.com")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "google", "account": "a@x.com"}

        # Account absent -> no keyring remove attempted.
        hermes_clear.assert_not_awaited()
        # remove_account is still issued (a delete of an absent row is itself a no-op).
        mock_accounts.remove_account.assert_awaited_once_with("org_test_456", "a@x.com")

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    async def test_disconnect_account_param_ignored_for_non_google_provider(self, mock_accounts, async_client):
        # The ?account= param is Google-specific; it must be rejected (not silently
        # honored) on a non-google provider so the inventory is never touched.
        mock_accounts.remove_account = AsyncMock()
        response = await async_client.delete("/api/v1/connections/stripe?account=a@x.com")
        assert response.status_code == 400
        mock_accounts.remove_account.assert_not_called()

    # -----------------------------------------------------------------------
    # M6-R24: AgentCore disconnect ("same for revoke/disconnect" as connect)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_agentcore_cli_skill_materializer_is_skipped(
        self, mock_repo, mock_store, mock_accounts, async_client, monkeypatch
    ):
        # google (gog_cli) has no mcp_remote/mcp_stdio -- on an AgentCore owner
        # the materializer remover never runs (no EFS keyring to touch), but the
        # row is still recorded as disconnected.
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_store.get = AsyncMock(return_value=None)
        mock_accounts.list_accounts = AsyncMock(return_value=[])
        mock_accounts.list_assignments = AsyncMock(return_value={})

        response = await async_client.delete("/api/v1/connections/google")

        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "google"}
        mock_store.delete.assert_awaited_once_with("org_test_456", "google")
        mock_repo.delete.assert_awaited_once_with("org_test_456", "google")

    @pytest.mark.asyncio
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_agentcore_mcp_shaped_provider_refreshes_hermes_profiles(
        self, mock_repo, mock_store, async_client, monkeypatch
    ):
        # An mcp_server-wiring provider is MCP-shaped: on an AgentCore owner the
        # teardown re-projects the (now-disconnected) set via reconcile_integrations
        # instead of patching openclaw.json.
        from core.services.credentials.registry import Provider

        p = Provider(
            key="fakemcp",
            display_name="Fake MCP",
            acquisition="paste_token",
            actor="mcp",
            refresh_owner="none",
            wiring={"mcp_server": {"key": "fakemcp", "command": "npx", "args": [], "env": {"K": "input.token"}}},
        )
        monkeypatch.setattr(connections.registry, "get_provider", lambda key: p)
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        refreshed = []

        async def fake_reconcile(owner_id, *, agentcore=None, pending=None):
            refreshed.append(owner_id)

        monkeypatch.setattr(connections, "reconcile_integrations", fake_reconcile)

        response = await async_client.delete("/api/v1/connections/fakemcp")

        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "fakemcp"}
        assert refreshed == ["org_test_456"]

    @pytest.mark.asyncio
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_agentcore_openclaw_paths_provider_clears_hermes_projection(
        self, mock_repo, mock_store, async_client, monkeypatch
    ):
        """M6-R31: reconcile_integrations has no fan-out for an openclaw_paths provider at
        all -- disconnecting one on an AgentCore owner must clear its own Hermes projection
        (apply_connection.clear_hermes_openclaw_paths_projection) instead of a no-op reconcile
        call or the ECS-only openclaw.json blank."""
        from routers import connections

        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        cleared = []

        async def fake_clear(owner_id, provider_key):
            cleared.append((owner_id, provider_key))

        monkeypatch.setattr(connections.apply_connection, "clear_hermes_openclaw_paths_projection", fake_clear)
        mock_reconcile = AsyncMock()
        monkeypatch.setattr(connections, "reconcile_integrations", mock_reconcile)

        response = await async_client.delete("/api/v1/connections/parallel")

        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "parallel"}
        assert cleared == [("org_test_456", "parallel")]
        mock_reconcile.assert_not_awaited()  # openclaw_paths never reaches the MCP reconcile branch


class TestAuthorize:
    """GET /api/v1/connections/{provider}/authorize — start the OAuth dance."""

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_returns_consent_url(self, mock_dance, async_client, monkeypatch):
        # x is byo_required; an active BYO app lets authorize reach the
        # OAuth dance instead of the byo-required 400.
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=True))
        consent_url = "https://accounts.google.com/o/oauth2/v2/auth?x=1"
        mock_dance.start = AsyncMock(return_value=consent_url)

        response = await async_client.get("/api/v1/connections/x/authorize")
        assert response.status_code == 200
        assert response.json()["authorize_url"].startswith(consent_url)

        # Dance started with the server-derived redirect_uri.
        mock_dance.start.assert_awaited_once()
        call = mock_dance.start.await_args
        assert call.args[0] == "org_test_456"
        assert call.args[1] == "x"
        assert call.kwargs["redirect_uri"].endswith("/api/v1/oauth/x/callback")

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_return_to_app_marks_state_origin(self, mock_dance, async_client, monkeypatch):
        # Desktop and iOS use different returns; neither is chosen by callback params.
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=True))
        mock_dance.start = AsyncMock(return_value="https://example.test/auth")

        await async_client.get("/api/v1/connections/x/authorize?return_to=app")
        assert mock_dance.start.await_args.kwargs["origin"] == "app"
        await async_client.get("/api/v1/connections/x/authorize?return_to=ios")
        assert mock_dance.start.await_args.kwargs["origin"] == "ios"
        await async_client.get("/api/v1/connections/x/authorize")
        assert mock_dance.start.await_args.kwargs["origin"] is None

    @pytest.mark.asyncio
    async def test_authorize_non_oauth_provider_returns_400(self, async_client):
        # vercel_cli is paste_token, not an OAuth integration.
        response = await async_client.get("/api/v1/connections/vercel_cli/authorize")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_authorize_unknown_provider_returns_404(self, async_client):
        response = await async_client.get("/api/v1/connections/does-not-exist/authorize")
        assert response.status_code == 404


class TestAuthorizeFromContainer:
    """GET /api/v1/connections/u/{owner_id}/{provider}/authorize — the agent
    mints the consent URL itself (hook-token auth), opens it in its own Chrome,
    and the owner signs in via the Browser panel."""

    URL = "/api/v1/connections/u/user_test_123/google/authorize"
    HEADERS = {"Authorization": "Bearer tok-123"}

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    @patch("routers.connections.integrations_repo")
    @patch("core.services.container_auth.container_repo.get_by_owner_id", new_callable=AsyncMock)
    async def test_mints_with_valid_hook_token(
        self, mock_get_container, mock_repo, mock_dance, async_client, monkeypatch
    ):
        mock_get_container.return_value = {"hook_token": "tok-123"}
        mock_repo.get = AsyncMock(return_value=None)
        monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=True))
        mock_dance.start = AsyncMock(return_value="https://accounts.google.com/o/oauth2/v2/auth?x=1")

        response = await async_client.get(self.URL, headers=self.HEADERS)
        assert response.status_code == 200
        assert response.json()["authorize_url"].startswith("https://accounts.google.com/")
        # Owner comes from the PATH, and the token was checked against THAT owner's row.
        mock_get_container.assert_awaited_once_with("user_test_123")
        call = mock_dance.start.await_args
        assert call.args[0] == "user_test_123" and call.args[1] == "google"
        assert call.kwargs["redirect_uri"].endswith("/api/v1/oauth/google/callback")
        # The state row records who minted it, so the callback can refuse a rebind.
        assert call.kwargs["origin"] == "container"

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    @patch("core.services.container_auth.container_repo.get_by_owner_id", new_callable=AsyncMock)
    async def test_missing_or_wrong_token_401(self, mock_get_container, mock_dance, async_client):
        mock_get_container.return_value = {"hook_token": "tok-123"}
        mock_dance.start = AsyncMock()
        assert (await async_client.get(self.URL)).status_code == 401
        assert (await async_client.get(self.URL, headers={"Authorization": "Bearer nope"})).status_code == 401
        mock_dance.start.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    @patch("routers.connections.integrations_repo")
    @patch("core.services.container_auth.container_repo.get_by_owner_id", new_callable=AsyncMock)
    async def test_already_connected_409_never_rebinds(self, mock_get_container, mock_repo, mock_dance, async_client):
        mock_get_container.return_value = {"hook_token": "tok-123"}
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_dance.start = AsyncMock()
        response = await async_client.get(self.URL, headers=self.HEADERS)
        assert response.status_code == 409
        mock_dance.start.assert_not_awaited()


class TestOAuthCallbackContainerOrigin:
    """A container-started flow may create a connection, never rebind a live one —
    judged at APPLY time under the connection lock, not only at mint time."""

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_container_flow_dropped_when_provider_went_live(
        self, mock_dance, mock_repo, mock_store, mock_apply, async_client
    ):
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "github", cred, "container"))
        mock_repo.get = AsyncMock(return_value={"status": "connected"})  # owner connected via the tab meanwhile
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client.get("/api/v1/oauth/github/callback?code=abc&state=st1", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert "/oauth/done?error=already_connected" in response.headers["location"]
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_awaited()
        mock_repo.put_status.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_owner_started_flow_still_applies_when_already_connected(
        self, mock_dance, mock_repo, mock_store, mock_apply, async_client
    ):
        # Owner-started (no origin) reconnects keep today's behaviour — Google's
        # add-another-account depends on connecting while already connected.
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "github", cred, None))
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(return_value=None)

        response = await async_client.get("/api/v1/oauth/github/callback?code=abc&state=st1", follow_redirects=False)
        assert "/oauth/done?connected=github" in response.headers["location"]
        mock_apply.apply_connection.assert_awaited_once()
        mock_repo.put_status.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    @pytest.mark.parametrize(
        "origin, expected",
        [
            ("app", "/checkout_redirect/connected?connected=posthog"),
            ("ios", "flickmeon://checkout_redirect/connected?connected=posthog"),
        ],
    )
    async def test_app_started_flow_lands_on_app_return_page(
        self, mock_dance, mock_repo, mock_store, mock_apply, async_client, origin, expected
    ):
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "posthog", cred, origin))
        mock_repo.get = AsyncMock(return_value=None)
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(return_value=None)

        response = await async_client.get("/api/v1/oauth/posthog/callback?code=abc&state=st1", follow_redirects=False)
        assert response.headers["location"].endswith(expected)

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    @pytest.mark.parametrize(
        "origin, expected",
        [
            ("app", "/checkout_redirect/connect_failed?error=apply_failed"),
            ("ios", "flickmeon://checkout_redirect/connect_failed?error=apply_failed"),
        ],
    )
    async def test_app_started_flow_failure_lands_on_app_return_page(
        self, mock_dance, mock_repo, mock_store, mock_apply, async_client, origin, expected
    ):
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "posthog", cred, origin))
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_apply.apply_connection = AsyncMock(side_effect=RuntimeError("boom"))
        mock_apply.remove_connection = AsyncMock()

        response = await async_client.get("/api/v1/oauth/posthog/callback?code=abc&state=st1", follow_redirects=False)
        assert response.headers["location"].endswith(expected)


class TestRetainsSecret:
    """``_retains_secret`` — backend custodianship of the exchanged credential.

    Base rule: every provider except ``refresh_owner: actor`` retains. The
    ``retain_secret: true`` yaml flag opts an actor-refresh provider back IN —
    posthog carries it so the backend can run HogQL reads (P0c) while the
    container's MCP copy stays untouched."""

    def test_posthog_retains_despite_actor_refresh(self):
        p = registry.get_provider("posthog")
        assert p.refresh_owner == "actor"  # guard: the flag is doing the work
        assert connections._retains_secret(p) is True

    def test_actor_refresh_without_flag_stays_unretained(self):
        p = registry.get_provider("google")
        assert p.refresh_owner == "actor"
        assert p.retain_secret is False
        assert connections._retains_secret(p) is False


class TestOAuthCallback:
    """GET /api/v1/oauth/{provider}/callback — PUBLIC; identity from state record."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", [None, "container", "app", "ios"])
    @pytest.mark.parametrize("code", [None, "abc"])
    async def test_failure_keeps_consumed_state_origin(self, async_client, monkeypatch, _oauth_state, origin, code):
        _oauth_state.return_value = {"owner_id": "org_1", "provider": "posthog", "verifier": "pkce", "origin": origin}
        exchange = AsyncMock(side_effect=httpx.ConnectError("provider unavailable"))
        monkeypatch.setattr(connections.oauth_dance, "exchange", exchange)
        query = f"code={code}&state=st1" if code else "error=access_denied&state=st1"

        response = await async_client.get(f"/api/v1/oauth/posthog/callback?{query}", follow_redirects=False)

        error = "oauth_exchange" if code else "oauth_provider"
        path = (
            "flickmeon://checkout_redirect/connect_failed"
            if origin == "ios"
            else "/checkout_redirect/connect_failed"
            if origin == "app"
            else "/oauth/done"
        )
        assert response.headers["location"].endswith(f"{path}?error={error}")
        _oauth_state.assert_awaited_once_with("st1")
        if code:
            assert exchange.await_args.args[0] == _oauth_state.return_value
        else:
            exchange.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spoofed_return", ["app", "ios"])
    async def test_expired_state_never_exchanges_or_trusts_return_to(
        self, async_client, monkeypatch, _oauth_state, spoofed_return
    ):
        _oauth_state.return_value = None
        exchange = AsyncMock()
        monkeypatch.setattr(connections.oauth_dance, "exchange", exchange)
        response = await async_client.get(
            f"/api/v1/oauth/posthog/callback?code=abc&state=expired&return_to={spoofed_return}",
            follow_redirects=False,
        )
        assert response.headers["location"].endswith("/oauth/done?error=oauth_state")
        exchange.assert_not_awaited()

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
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred, None))
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(return_value="user@example.com")
        mock_accounts.add_account = AsyncMock()
        mock_accounts.list_assignments = AsyncMock(return_value={})

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        assert "/oauth/done?connected=google" in response.headers["location"]

        # Identity + credential recovered from the state record.
        mock_dance.exchange.assert_awaited_once()
        ex_call = mock_dance.exchange.await_args
        assert ex_call.args[0] == {"owner_id": "org_1", "provider": "google", "verifier": "pkce"}
        assert ex_call.args[1] == "abc"
        assert ex_call.kwargs["redirect_uri"].endswith("/api/v1/oauth/google/callback")

        # google is refresh_owner=actor: the container self-refreshes + owns the
        # durable credential, so the backend retains NOTHING in Secrets Manager.
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_awaited_once_with("org_1", "google", cred)
        # Status row written after a clean apply.
        assert mock_repo.put_status.await_count == 1
        assert mock_repo.put_status.await_args.args == ("org_1", "google", "connected")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "state_times,connects",
        [
            ({"created_at_ms": 200_400}, False),
            ({"created_at_ms": 200_500}, False),
            ({"created_at_ms": 200_501}, True),  # the same second as revoke-all, but after it
            ({"ttl": 200 + 600}, False),  # a state minted before this field: its second fails closed
            ({"ttl": 201 + 600}, True),
        ],
    )
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_a_connect_started_before_revoke_all_is_refused(
        self,
        mock_dance,
        mock_repo,
        mock_store,
        mock_apply,
        mock_accounts,
        state_times,
        connects,
        async_client,
        monkeypatch,
        _oauth_state,
    ):
        """Revoke-all at t=200.500 s. A callback whose state was minted at or before it (its code
        already exchanged, or a provider revoke-all never saw) must not land a credential after
        the teardown. The start time is the stored state row's, never the request's."""
        from core.repositories import container_repo

        monkeypatch.setattr(container_repo, "get_oauth_revoked_at", AsyncMock(return_value=200_500))
        _oauth_state.return_value = {
            "owner_id": "org_1",
            "provider": "google",
            "verifier": "pkce",
            "ttl": 1_000_000,
            **state_times,
        }
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred, None))
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(return_value="user@example.com")
        mock_accounts.add_account = AsyncMock()
        mock_accounts.list_assignments = AsyncMock(return_value={})

        response = await async_client.get("/api/v1/oauth/google/callback?code=abc&state=st1", follow_redirects=False)

        if connects:
            assert "/oauth/done?connected=google" in response.headers["location"]
            assert mock_repo.put_status.await_count == 1
        else:
            assert response.headers["location"].endswith("/oauth/done?error=revoked")
            mock_apply.apply_connection.assert_not_awaited()
            mock_store.put.assert_not_called()
            mock_repo.put_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_callback_redirects_on_provider_error_redirect(self, async_client):
        # RFC 6749 error redirect: the provider sends ?error=…&state=… and no
        # code (denied consent, invalid_scope, …). Must land on the connections
        # page with a banner, not 422 raw JSON in the user's browser.
        response = await async_client.get(
            "/api/v1/oauth/posthog/callback?error=invalid_scope&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        assert "error=oauth_provider" in response.headers["location"]

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_callback_redirects_when_token_exchange_fails(self, mock_dance, async_client):
        # With a BYO app the owner's own client secret is user input: a wrong
        # secret makes the provider's token endpoint return 4xx, which exchange()
        # turns into an httpx.HTTPError. The callback must land on the connections
        # page with a retryable error, NOT raise a 500 after consuming the state.
        import httpx

        req = httpx.Request("POST", "https://oauth2.googleapis.com/token")
        resp = httpx.Response(400, request=req, json={"error": "invalid_client"})
        mock_dance.exchange = AsyncMock(side_effect=httpx.HTTPStatusError("invalid_client", request=req, response=resp))

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        assert "error=oauth_exchange" in response.headers["location"]

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_callback_rolls_back_and_clears_the_projection_when_wiring_fails(
        self, mock_dance, mock_repo, mock_store, mock_apply, async_client, hermes_clear
    ):
        # google is materializer-backed (gog_cli). If apply fails after the
        # materializer already wrote a partial keyring/client file, the rollback
        # must remove the on-disk artifact — so a failed connect leaves no durable
        # credential residue on EFS. google is actor-refresh -> no secret retained,
        # so there is no Secrets Manager delete to make, and no status row written.
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred, None))
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_apply.apply_connection = AsyncMock(side_effect=RuntimeError("efs write failed"))

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        # Redirect surfaces the apply failure rather than a false "connected".
        assert "error=apply_failed" in response.headers["location"]

        # The profile projection is cleared (gog_cli) with the exchanged credential.
        hermes_clear.assert_awaited_once_with("org_1", "google", "gog_cli", cred)
        # Actor-refresh provider never retained a secret -> nothing to put or delete.
        mock_store.put.assert_not_called()
        mock_store.delete.assert_not_called()
        # No status row on the failure path.
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.connections.google_accounts_repo")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.oauth_dance")
    async def test_google_callback_records_connected_account(
        self, mock_dance, mock_repo, mock_store, mock_apply, mock_accounts, mock_seed, async_client
    ):
        # A successful Google connect must record the connected account's email in
        # the per-owner inventory, on top of the provider-level "connected" status.
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred, None))
        mock_repo.put_status = AsyncMock()
        # apply_connection surfaces the connected account email (gog_cli rail).
        mock_apply.apply_connection = AsyncMock(return_value="alice@example.com")
        mock_accounts.add_account = AsyncMock()
        # An agent already bound to another account: the projection just rewrote every
        # profile's cli-env.json from its own keys, so that binding needs re-projecting.
        mock_accounts.list_assignments = AsyncMock(return_value={"agent_1": "bob@example.com"})

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        assert "/oauth/done?connected=google" in response.headers["location"]

        # Provider-level status row still written.
        assert mock_repo.put_status.await_count == 1
        assert mock_repo.put_status.await_args.args == ("org_1", "google", "connected")
        # The connected account is added to the owner's inventory.
        mock_accounts.add_account.assert_awaited_once_with("org_1", "alice@example.com")
        # ...and the surviving binding is re-projected, since the connect's cli-env.json
        # rewrite dropped it.
        mock_seed.assert_awaited_once_with("org_1", "agent_1")

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
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred, None))
        mock_repo.put_status = AsyncMock()
        mock_accounts.add_account = AsyncMock()
        mock_accounts.list_assignments = AsyncMock(return_value={})

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
        mock_dance.exchange = AsyncMock(return_value=("org_1", "acme", cred, None))
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
        assert mock_repo.put_status.await_count == 1
        assert mock_repo.put_status.await_args.args == ("org_1", "acme", "connected")
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
async def test_byo_active_true_when_app_stored(mock_get):
    from routers import connections

    mock_get.return_value = {"client_id": "x", "client_secret": "y"}
    assert await connections._byo_active("user_1", "x") is True


@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_false_without_app(mock_get):
    from routers import connections

    mock_get.return_value = None
    assert await connections._byo_active("user_1", "x") is False


@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_false_when_app_incomplete(mock_get):
    from routers import connections

    # A half-populated row (missing the secret) is NOT active — the predicate
    # requires both halves of the client credential.
    mock_get.return_value = {"client_id": "x"}
    assert await connections._byo_active("user_1", "x") is False


@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_false_for_non_byo_provider(mock_get):
    from routers import connections

    mock_get.return_value = {"client_id": "x", "client_secret": "y"}
    # stripe is not byo_required → predicate short-circuits, never True
    assert await connections._byo_active("user_1", "stripe") is False


class TestByoApp:
    """PUT/DELETE /api/v1/connections/{provider}/byo-app — store/clear the
    owner's own OAuth app credentials, gated to providers that are
    ``byo_required``. The async_client owner is the personal owner
    ``user_test_123``."""

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.put", new_callable=AsyncMock)
    async def test_put_byo_app_stores_credentials(self, mock_put, async_client):
        # x byo_required=True: a PUT stores the owner's app credentials.
        ok = await async_client.put(
            "/api/v1/connections/x/byo-app",
            json={"client_id": "cid", "client_secret": "csec"},
        )
        assert ok.status_code == 200
        mock_put.assert_awaited_once()
        # Stored under (owner, provider) with the submitted client credentials.
        put_call = mock_put.await_args
        assert put_call.args[0] == "org_test_456"
        assert put_call.args[1] == "x"
        assert put_call.kwargs["client_id"] == "cid"
        assert put_call.kwargs["client_secret"] == "csec"
        assert "acknowledged_at" not in put_call.kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["client_id", "client_secret"])
    @patch("routers.connections.oauth_app_store.put", new_callable=AsyncMock)
    async def test_put_byo_app_rejects_the_masked_placeholder(self, mock_put, async_client, field):
        # The form shows a stored secret as bullets; pasting that back stored "••••••…" as the
        # founder's X client_secret and every token refresh failed.
        body = {"client_id": "cid", "client_secret": "csec", field: "••••••abcd"}
        resp = await async_client.put("/api/v1/connections/x/byo-app", json=body)
        assert resp.status_code == 400
        assert (
            resp.json()["detail"]
            == "Paste the real client secret — this looks like the masked value shown after saving."
        )
        mock_put.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.put", new_callable=AsyncMock)
    async def test_put_byo_app_rejected_for_non_byo_provider(self, mock_put, async_client):
        # stripe is not byo_required -> a BYO PUT is rejected (400) and nothing stored.
        resp = await async_client.put(
            "/api/v1/connections/stripe/byo-app",
            json={"client_id": "c", "client_secret": "s"},
        )
        assert resp.status_code in (400, 404)  # stripe is not byo_required
        mock_put.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.put", new_callable=AsyncMock)
    async def test_put_byo_app_unknown_provider_returns_404(self, mock_put, async_client):
        resp = await async_client.put(
            "/api/v1/connections/does-not-exist/byo-app",
            json={"client_id": "c", "client_secret": "s"},
        )
        assert resp.status_code == 404
        mock_put.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_app_store.delete", new_callable=AsyncMock)
    async def test_delete_byo_app_clears_stored_app(self, mock_delete, async_client):
        # DELETE clears the stored BYO app for (owner, provider). It does NOT
        # disconnect a live connection — only the app credentials are removed.
        resp = await async_client.delete("/api/v1/connections/x/byo-app")
        assert resp.status_code == 200
        mock_delete.assert_awaited_once_with("org_test_456", "x")

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
        # x is visible to every owner; assert the three BYO fields are
        # present and reflect the static flag + the live _byo_active result.
        mock_repo.list_for_owner = AsyncMock(return_value=[])
        mock_byo.return_value = True

        response = await async_client.get("/api/v1/connections")
        assert response.status_code == 200
        by_key = {c["provider"]: c for c in response.json()["connections"]}

        # x is byo_required and configured (mocked _byo_active True).
        assert by_key["x"]["byo_required"] is True
        assert by_key["x"]["byo_configured"] is True
        assert by_key["x"]["byo_setup_url"] == "https://developer.x.com/en/portal/dashboard"

        # stripe is not byo_required; no setup URL.
        assert by_key["stripe"]["byo_required"] is False
        assert by_key["stripe"]["byo_setup_url"] is None

    @pytest.mark.asyncio
    @patch("routers.connections._byo_active", new_callable=AsyncMock)
    @patch("routers.connections.integrations_repo")
    async def test_list_byo_configured_false_when_inactive(self, mock_repo, mock_byo, async_client):
        # byo_configured reflects _byo_active: False when no stored app.
        mock_repo.list_for_owner = AsyncMock(return_value=[])
        mock_byo.return_value = False

        response = await async_client.get("/api/v1/connections")
        assert response.status_code == 200
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        assert by_key["x"]["byo_required"] is True
        assert by_key["x"]["byo_configured"] is False


class TestAwsSetup:
    """GET /api/v1/connections/{provider}/aws-setup — stage the AWS read-only
    connect: mint/reuse the STS ExternalId + return the CFN quick-create inputs.
    Does NOT mark the integration connected (that is aws-complete's job)."""

    @pytest.mark.asyncio
    @patch("routers.connections.aws_cfn")
    @patch("routers.connections.integration_secret_store")
    async def test_aws_setup_mints_then_reuses_external_id(self, mock_store, mock_cfn, async_client):
        # First call mints + persists an ExternalId; the second reuses it so a
        # repeated setup (or half-finished connect) keeps the SAME id the owner
        # already baked into their CloudFormation trust policy.
        store: dict = {}

        async def fake_get(owner, provider):
            return store.get((owner, provider))

        async def fake_put(owner, provider, cred):
            store[(owner, provider)] = cred
            return "secret-arn"

        mock_store.get = AsyncMock(side_effect=fake_get)
        mock_store.put = AsyncMock(side_effect=fake_put)
        mock_cfn.gen_external_id.return_value = "ext_fixed_123"
        mock_cfn.platform_trust_principal_arn.return_value = "arn:aws:iam::877352799272:root"

        r1 = await async_client.get("/api/v1/connections/aws/aws-setup")
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["external_id"] == "ext_fixed_123"
        assert body1["principal_arn"] == "arn:aws:iam::877352799272:root"
        assert body1["our_account_id"] == "877352799272"
        assert body1["default_region"] == "us-east-1"
        # The CFN template is unhosted until Plan 3 -> both quick-create URLs are
        # None rather than crashing on a None template_url.
        assert body1["view_only"]["policy_arn"] == "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"
        assert body1["read_only"]["policy_arn"] == "arn:aws:iam::aws:policy/ReadOnlyAccess"
        assert body1["view_only"]["quick_create_url"] is None
        assert body1["read_only"]["quick_create_url"] is None
        assert body1["power_user"]["policy_arn"] == "arn:aws:iam::aws:policy/PowerUserAccess"
        assert body1["power_user"]["quick_create_url"] is None

        r2 = await async_client.get("/api/v1/connections/aws/aws-setup")
        assert r2.status_code == 200
        assert r2.json()["external_id"] == "ext_fixed_123"

        # ExternalId minted + persisted exactly once across both calls.
        mock_cfn.gen_external_id.assert_called_once()
        assert mock_store.put.await_count == 1

    @pytest.mark.asyncio
    @patch("routers.connections.integration_secret_store")
    async def test_aws_setup_builds_quick_create_url_when_template_hosted(self, mock_store, async_client, monkeypatch):
        # With CFN_TEMPLATE_URL set (the hosted connector template wired by CDK),
        # aws-setup returns real quick-create URLs carrying the URL-encoded
        # template URL plus the ExternalId / principal / policy param prefills —
        # the button auto-enables on the frontend. aws_cfn is left UNMOCKED so the
        # real quick_create_url is exercised (its helpers are pure, no AWS calls).
        from urllib.parse import quote

        store: dict = {}

        async def fake_get(owner, provider):
            return store.get((owner, provider))

        async def fake_put(owner, provider, cred):
            store[(owner, provider)] = cred
            return "secret-arn"

        mock_store.get = AsyncMock(side_effect=fake_get)
        mock_store.put = AsyncMock(side_effect=fake_put)

        template_url = "https://lightbulb-dev-cfn-templates.s3.us-east-1.amazonaws.com/aws-connector.yaml"
        monkeypatch.setattr(connections.settings, "CFN_TEMPLATE_URL", template_url)

        r = await async_client.get("/api/v1/connections/aws/aws-setup")
        assert r.status_code == 200
        body = r.json()

        external_id = body["external_id"]
        principal_arn = body["principal_arn"]
        encoded_template = quote(template_url, safe="")
        for block in ("view_only", "read_only"):
            url = body[block]["quick_create_url"]
            assert url is not None
            assert f"templateURL={encoded_template}" in url
            assert f"param_ExternalId={external_id}" in url
            assert quote(principal_arn, safe="") in url
            assert quote(body[block]["policy_arn"], safe="") in url

    @pytest.mark.asyncio
    async def test_aws_setup_non_aws_provider_returns_400(self, async_client):
        # stripe is paste_token, not aws_sts -> rejected at this AWS surface.
        response = await async_client.get("/api/v1/connections/stripe/aws-setup")
        assert response.status_code == 400

    @pytest.mark.asyncio
    @patch("routers.connections.aws_cfn")
    @patch("routers.connections.integration_secret_store")
    async def test_aws_setup_non_admin_member_forbidden(self, mock_store, mock_cfn, async_client_org_member):
        mock_store.get = AsyncMock()
        mock_store.put = AsyncMock()
        response = await async_client_org_member.get("/api/v1/connections/aws/aws-setup")
        assert response.status_code == 403
        # Gate fires before any ExternalId mint / persistence.
        mock_cfn.gen_external_id.assert_not_called()
        mock_store.put.assert_not_called()


class TestAwsComplete:
    """POST /api/v1/connections/{provider}/aws-complete — mint the container's
    credential-fetch bearer token, materialize + mark connected. The
    owner-supplied role ARN is the confused-deputy surface, so it is validated
    BEFORE the credential is ever stored or assumed."""

    # The single canonical role name the quick-create template pins.
    _GOOD_ARN = "arn:aws:iam::123456789012:role/lightbulb-connector"
    _LEGACY_ARN = "arn:aws:iam::123456789012:role/isol8-connector"

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_rejects_our_account_arn(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        # SECURITY: a role ARN inside OUR account is the confused-deputy attack —
        # rejected with 400 BEFORE the credential is ever stored.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": "arn:aws:iam::877352799272:role/evil"},
        )
        assert resp.status_code == 400
        # No secret write, no wiring, no status when the guard fires.
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_rejects_malformed_arn(self, mock_repo, mock_store, mock_apply, mock_aip, async_client):
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": "not-an-arn"},
        )
        assert resp.status_code == 400
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_rejects_wildcard_arn(self, mock_repo, mock_store, mock_apply, mock_aip, async_client):
        # SECURITY: a ``role/*`` wildcard ARN is syntactically an IAM ARN but
        # would let the vend endpoint's AssumeRole reach ANY role in the
        # (external) account. The role-path charset rejects ``*`` -> 400 BEFORE
        # the credential is ever stored.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": "arn:aws:iam::111122223333:role/*"},
        )
        assert resp.status_code == 400
        # No wiring, no status, no secret write when the guard fires.
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()
        mock_store.put.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_rejects_non_connector_role_name(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        # SECURITY: the backend's sts:AssumeRole IAM grant is scoped to
        # role/lightbulb-connector, so a hand-rolled role with any other name is
        # rejected with a clear 400 at connect time — never stored, never
        # left to fail as a vend-time AccessDenied.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": "arn:aws:iam::123456789012:role/MyCustomRole"},
        )
        assert resp.status_code == 400
        from core.billing.bedrock_aip import CONNECTOR_ROLE_NAME

        assert CONNECTOR_ROLE_NAME in resp.json()["detail"]
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_rejects_the_retired_connector_role_name(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        """The pre-rename role name is no longer accepted anywhere.

        The backend's sts:AssumeRole grant (service-stack StsAssumeConnectorRole)
        names only `lightbulb-connector`, so accepting the old name here would
        store a credential that AccessDenies at vend time instead of failing
        loudly at connect time. An owner still holding the retired role must
        relaunch the quick-create stack."""
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._LEGACY_ARN},
        )
        assert resp.status_code == 400
        mock_store.put.assert_not_called()

    def test_connector_role_name_is_the_only_accepted_name(self):
        """One name, no transitional alias — the shim it replaced is gone."""
        from core.billing import bedrock_aip

        assert bedrock_aip.CONNECTOR_ROLE_NAME == "lightbulb-connector"
        assert not hasattr(bedrock_aip, "ACCEPTED_CONNECTOR_ROLE_NAMES")
        assert not hasattr(bedrock_aip, "LEGACY_CONNECTOR_ROLE_NAMES")

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_rejects_region_with_newline(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        # SECURITY: ``region`` is owner-supplied and is materialized into
        # ~/.aws/config (an INI file). A newline could inject a second
        # profile/section, so any region outside the [a-z0-9-] charset is
        # rejected with 400 BEFORE the credential is stored or used.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._GOOD_ARN, "region": "us-east-1\n[evil]"},
        )
        assert resp.status_code == 400
        # No wiring, no status, no secret write when the guard fires.
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()
        mock_store.put.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_without_prior_setup_returns_409(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        # A valid external-account ARN, but no stored ExternalId (aws-setup never
        # ran) -> 409, and still no wiring.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value=None)
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._GOOD_ARN},
        )
        assert resp.status_code == 409
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_that_began_before_revoke_all_stores_nothing(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client, monkeypatch
    ):
        import time as _time

        from core.repositories import container_repo

        monkeypatch.setattr(
            container_repo, "get_oauth_revoked_at", AsyncMock(return_value=int(_time.time() * 1000) + 60_000)
        )
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete", json={"role_arn": self._GOOD_ARN, "region": "us-west-2"}
        )
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "revoked"
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_happy_path_mints_token_materializes_marks_connected(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._GOOD_ARN, "region": "us-west-2"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "connected", "provider": "aws"}

        stored = mock_store.put.call_args.args[2]
        assert stored["role_arn"] == self._GOOD_ARN
        assert stored["external_id"] == "ext_abc"
        assert stored["region"] == "us-west-2"
        # Only the HASH is retained server-side; plaintext never reaches SM.
        assert "cli_token_hash" in stored and len(stored["cli_token_hash"]) == 64
        assert "cli_token" not in stored

        applied = mock_apply.apply_connection.call_args.args[2]
        # The materializer gets the stored dict PLUS the plaintext token.
        assert applied["cli_token_hash"] == stored["cli_token_hash"]
        import hashlib as _h

        assert _h.sha256(applied["cli_token"].encode()).hexdigest() == stored["cli_token_hash"]

        assert mock_repo.put_status.await_count == 1
        assert mock_repo.put_status.await_args.args == ("org_test_456", "aws", "connected")

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_defaults_region_when_omitted(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        # No region in the body -> the credential picks up the provider default.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._GOOD_ARN},
        )
        assert resp.status_code == 200
        put_call = mock_store.put.await_args
        assert put_call.args[2]["region"] == "us-east-1"

    @pytest.mark.asyncio
    @patch("routers.connections._remove_materialized_artifacts", new_callable=AsyncMock)
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_rolls_back_when_apply_fails(
        self, mock_repo, mock_store, mock_apply, mock_aip, mock_remove, async_client
    ):
        # apply_connection raising must roll back the on-disk artifact and never
        # mark connected. Instead of deleting the whole secret, the rollback
        # RE-PUTS the pending {external_id} record so the owner's
        # already-created CFN trust + documented ExternalId reuse survive a
        # transient apply failure.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_apply.apply_connection = AsyncMock(side_effect=RuntimeError("efs write failed"))
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._GOOD_ARN},
        )
        assert resp.status_code == 502
        # The partially-materialized artifact is torn back down.
        mock_remove.assert_awaited_once()
        # The secret is reset to the pending {external_id}-only record, NOT fully
        # deleted -> ExternalId survives for a retry.
        mock_store.delete.assert_not_called()
        assert mock_store.put.await_args_list[-1].args == ("org_test_456", "aws", {"external_id": "ext_abc"})
        # Never reports a false "connected".
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections._remove_materialized_artifacts", new_callable=AsyncMock)
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_rollback_is_best_effort_when_a_teardown_step_raises(
        self, mock_repo, mock_store, mock_apply, mock_aip, mock_remove, async_client
    ):
        # Each rollback teardown step is wrapped independently, so one failing
        # step does not skip the others. Here the artifact-removal step ITSELF
        # raises, yet the secret is still reset to the pending {external_id}
        # record. The endpoint still surfaces the original 5xx and never marks
        # connected.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_apply.apply_connection = AsyncMock(side_effect=RuntimeError("efs write failed"))
        mock_repo.put_status = AsyncMock()
        # Simulate a teardown step ALSO failing during rollback.
        mock_remove.side_effect = RuntimeError("artifact remove blew up")

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._GOOD_ARN},
        )
        assert resp.status_code == 502
        # Despite the artifact-removal step raising, the remaining teardown ran:
        assert mock_store.put.await_args_list[-1].args == ("org_test_456", "aws", {"external_id": "ext_abc"})
        # Secret never fully deleted; status never flips to connected.
        mock_store.delete.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_non_aws_provider_returns_400(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_apply.apply_connection = AsyncMock()
        resp = await async_client.post(
            "/api/v1/connections/stripe/aws-complete",
            json={"role_arn": self._GOOD_ARN},
        )
        assert resp.status_code == 400
        mock_apply.apply_connection.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_non_admin_member_forbidden(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client_org_member
    ):
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client_org_member.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._GOOD_ARN},
        )
        assert resp.status_code == 403
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    # --- Trust-pin gate: a role IN OUR account is allowed ONLY if its trust
    # policy pins the platform trust principal (our account root) + THIS
    # owner's ExternalId, verified via iam:GetRole. This is the guarantee that
    # no tenant can ever connect one of Lightbulb's own AWS roles.
    _PLATFORM_PRINCIPAL = "arn:aws:iam::877352799272:root"
    _OUR_ACCOUNT_ARN = "arn:aws:iam::877352799272:role/lightbulb-connector"

    @staticmethod
    def _set_trust(mock_aip, statements):
        mock_aip._iam_client.return_value.get_role.return_value = {
            "Role": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": statements}}
        }

    @pytest.mark.asyncio
    @patch("routers.connections.aws_cfn.platform_trust_principal_arn", return_value=_PLATFORM_PRINCIPAL)
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_our_account_arn_pinned_passes_gate(
        self, mock_repo, mock_store, mock_apply, mock_aip, _mock_cfn, async_client
    ):
        # (a) our-account role whose trust pins the platform principal (account
        # root) + THIS owner's ExternalId -> passes the gate and connects.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()
        self._set_trust(
            mock_aip,
            [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": self._PLATFORM_PRINCIPAL},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"sts:ExternalId": "ext_abc"}},
                }
            ],
        )

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._OUR_ACCOUNT_ARN},
        )
        assert resp.status_code == 200
        mock_apply.apply_connection.assert_awaited_once()
        assert mock_repo.put_status.await_count == 1
        assert mock_repo.put_status.await_args.args == ("org_test_456", "aws", "connected")

    @pytest.mark.asyncio
    @patch("routers.connections.aws_cfn.platform_trust_principal_arn", return_value=_PLATFORM_PRINCIPAL)
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_our_account_arn_not_pinned_rejected(
        self, mock_repo, mock_store, mock_apply, mock_aip, _mock_cfn, async_client
    ):
        # (b) TENANT-CONTAINMENT: an our-account role whose trust does NOT name
        # the platform principal (trusts a service principal, like every CDK
        # infra role) -> 400, no connect. A tenant can't create a role in our
        # account, and our infra roles never carry root+ExternalId trust.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()
        self._set_trust(
            mock_aip,
            [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        )

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._OUR_ACCOUNT_ARN},
        )
        assert resp.status_code == 400
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.aws_cfn.platform_trust_principal_arn", return_value=_PLATFORM_PRINCIPAL)
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_our_account_arn_external_id_mismatch_rejected(
        self, mock_repo, mock_store, mock_apply, mock_aip, _mock_cfn, async_client
    ):
        # (c) our-account role that trusts the platform principal but whose trust
        # requires a DIFFERENT ExternalId than the owner's pending one -> 400.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()
        self._set_trust(
            mock_aip,
            [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": self._PLATFORM_PRINCIPAL},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"sts:ExternalId": "ext_someone_else"}},
                }
            ],
        )

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._OUR_ACCOUNT_ARN},
        )
        assert resp.status_code == 400
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_external_account_arn_skips_trust_pin(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        # (d) an EXTERNAL-account ARN is unchanged: no iam:GetRole, proceeds
        # straight to materialize. The trust-pin check only fires for
        # our-account ARNs.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._GOOD_ARN},
        )
        assert resp.status_code == 200
        mock_aip._iam_client.return_value.get_role.assert_not_called()
        mock_apply.apply_connection.assert_awaited_once()
        assert mock_apply.apply_connection.call_args.args[2]["role_arn"] == self._GOOD_ARN

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_our_account_arn_without_setup_returns_409(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        # An our-account ARN with NO prior aws-setup (no stored ExternalId) must
        # yield the accurate 409 "run aws-setup first" BEFORE the trust-pin check
        # — not the misleading "not Isol8's" 400.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value=None)
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._OUR_ACCOUNT_ARN},
        )
        assert resp.status_code == 409
        mock_aip._iam_client.return_value.get_role.assert_not_called()
        mock_apply.apply_connection.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.aws_cfn.platform_trust_principal_arn", return_value=_PLATFORM_PRINCIPAL)
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_our_account_arn_pinned_list_principal_passes_gate(
        self, mock_repo, mock_store, mock_apply, mock_aip, _mock_cfn, async_client
    ):
        # (e) our-account role whose trust ``Principal.AWS`` is a LIST that
        # includes the platform principal (plus other unrelated ones) + the
        # owner's ExternalId -> passes the gate and connects. Covers the
        # ``isinstance(principal, list)`` normalization branch.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()
        self._set_trust(
            mock_aip,
            [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["arn:aws:iam::999999999999:role/other", self._PLATFORM_PRINCIPAL]},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"sts:ExternalId": "ext_abc"}},
                }
            ],
        )

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._OUR_ACCOUNT_ARN},
        )
        assert resp.status_code == 200
        mock_apply.apply_connection.assert_awaited_once()
        assert mock_repo.put_status.await_count == 1
        assert mock_repo.put_status.await_args.args == ("org_test_456", "aws", "connected")

    @pytest.mark.asyncio
    @patch("routers.connections.aws_cfn.platform_trust_principal_arn", return_value=_PLATFORM_PRINCIPAL)
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_our_account_arn_getrole_error_rejected(
        self, mock_repo, mock_store, mock_apply, mock_aip, _mock_cfn, async_client
    ):
        # (f) FAIL-CLOSED: iam:GetRole raising (e.g. AccessDenied / throttle) must
        # yield a clean 400, never a 500 or an accidental connect.
        import botocore.exceptions

        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()
        mock_aip._iam_client.return_value.get_role.side_effect = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "GetRole"
        )

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._OUR_ACCOUNT_ARN},
        )
        assert resp.status_code == 400
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.aws_cfn.platform_trust_principal_arn", return_value=_PLATFORM_PRINCIPAL)
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_our_account_arn_malformed_trust_doc_rejected(
        self, mock_repo, mock_store, mock_apply, mock_aip, _mock_cfn, async_client
    ):
        # (g) FAIL-CLOSED on a malformed-but-parseable trust doc: a bare-string
        # ``Principal`` ("*") used to raise AttributeError inside the parse loop
        # and surface as a 500. It must now be a clean 400 (non-match -> not
        # pinned), proving the parse loop is inside the fail-closed try.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()
        self._set_trust(
            mock_aip,
            [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"sts:ExternalId": "ext_abc"}},
                }
            ],
        )

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._OUR_ACCOUNT_ARN},
        )
        assert resp.status_code == 400
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.aws_cfn.platform_trust_principal_arn", return_value=_PLATFORM_PRINCIPAL)
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_our_account_arn_missing_condition_rejected(
        self, mock_repo, mock_store, mock_apply, mock_aip, _mock_cfn, async_client
    ):
        # (h) our-account role that trusts the platform principal but has NO
        # Condition block (so no ExternalId pin) -> 400. Missing Condition must
        # not raise; it just fails the ExternalId match. This is exactly the
        # shape of an account-root-trusting infra/admin role, so it doubles as
        # the proof that root trust ALONE never passes the gate.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()
        self._set_trust(
            mock_aip,
            [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": self._PLATFORM_PRINCIPAL},
                    "Action": "sts:AssumeRole",
                }
            ],
        )

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._OUR_ACCOUNT_ARN},
        )
        assert resp.status_code == 400
        mock_store.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
        mock_repo.put_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.integration_secret_store")
    async def test_our_account_arn_with_per_owner_principal_trust_now_rejected(
        self, mock_store, mock_aip, async_client
    ):
        # Pre-migration trust docs (pinning the per-owner container role) must
        # no longer pass: the platform principal is account root.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_aip._iam_client.return_value.get_role.return_value = {
            "Role": {
                "AssumeRolePolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {
                                "AWS": "arn:aws:iam::877352799272:role/isol8-prod-owner-bedrock-user_test_123"
                            },
                            "Condition": {"StringEquals": {"sts:ExternalId": "ext_abc"}},
                        }
                    ]
                }
            }
        }
        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": "arn:aws:iam::877352799272:role/lightbulb-connector"},
        )
        assert resp.status_code == 400


class TestDisconnectAws:
    """DELETE /api/v1/connections/aws — revocation is server-side: deleting the
    SM secret drops the cli_token_hash, so the container's next credential
    fetch 401s. On top of that, the aws_cli materializer removes the on-EFS
    credential_process bundle. There is no per-owner IAM grant to tear down."""

    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_disconnect_aws_drops_secret_and_status(
        self, mock_repo, mock_store, mock_aip, async_client, hermes_clear
    ):
        mock_repo.delete = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_store.get = AsyncMock(
            return_value={"role_arn": "arn:aws:iam::123456789012:role/ro", "external_id": "e", "region": "us-east-1"}
        )
        resp = await async_client.delete("/api/v1/connections/aws")
        assert resp.status_code == 200
        assert resp.json() == {"status": "disconnected", "provider": "aws"}

        # aws_cli projection cleared (drops the ~/.aws bundle from every profile).
        hermes_clear.assert_awaited_once_with(
            "org_test_456",
            "aws",
            "aws_cli",
            {"role_arn": "arn:aws:iam::123456789012:role/ro", "external_id": "e", "region": "us-east-1"},
        )
        # Secret delete IS the revocation: the cli_token_hash goes with it, so
        # the vend endpoint 401s the container's next fetch. Status row too.
        mock_store.delete.assert_awaited_once_with("org_test_456", "aws")
        mock_repo.delete.assert_awaited_once_with("org_test_456", "aws")


class TestAwsCredentialsEndpoint:
    """POST /api/v1/connections/aws/credentials — the container helper's
    credential_process endpoint. NO Clerk auth: bearer token minted at
    aws-complete, validated against the SHA-256 hash in the SM credential."""

    _TOKEN = "tok_plain_abc"

    @pytest.fixture(autouse=True)
    def _fresh_state(self):
        from routers import connections as conn_mod

        conn_mod._aws_cli_records.clear()
        conn_mod._aws_cli_rate_buckets.clear()
        conn_mod._aws_cli_refresh_locks.clear()
        yield
        conn_mod._aws_cli_records.clear()
        conn_mod._aws_cli_rate_buckets.clear()

    # sha256("tok_plain_abc") — precomputed so tests don't re-derive via the
    # same helper they're testing.
    _TOKEN_HASH = "18197ef8f0b27b7cbf4f3acfa60448a622594f7a20279c4fa6a0cfded6a605cc"

    def _stored(self):
        return {
            "role_arn": "arn:aws:iam::123456789012:role/IsolReadOnly",
            "external_id": "ext_abc",
            "region": "us-east-1",
            "cli_token_hash": self._TOKEN_HASH,
        }

    def _sts_response(self):
        from datetime import datetime, timezone

        return {
            "Credentials": {
                "AccessKeyId": "ASIATEST",
                "SecretAccessKey": "secret",
                "SessionToken": "session",
                "Expiration": datetime(2099, 1, 1, tzinfo=timezone.utc),
            }
        }

    @pytest.mark.asyncio
    @patch("routers.connections._sts_client")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_happy_path_returns_credential_process_json(self, mock_store, mock_repo, mock_sts, async_client):
        mock_store.get = AsyncMock(return_value=self._stored())
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_sts.return_value.assume_role.return_value = self._sts_response()

        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["Version"] == 1
        assert body["AccessKeyId"] == "ASIATEST"
        assert body["SecretAccessKey"] == "secret"
        assert body["SessionToken"] == "session"
        assert body["Expiration"].startswith("2099-01-01T00:00:00")

        kwargs = mock_sts.return_value.assume_role.call_args.kwargs
        assert kwargs["RoleArn"] == "arn:aws:iam::123456789012:role/IsolReadOnly"
        assert kwargs["ExternalId"] == "ext_abc"
        assert kwargs["DurationSeconds"] == 3600
        assert kwargs["RoleSessionName"].startswith("lightbulb-")

    @pytest.mark.asyncio
    async def test_missing_bearer_is_401(self, async_client):
        resp = await async_client.post("/api/v1/connections/aws/credentials", json={"owner_id": "org_x"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "missing_or_malformed_authorization"

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_wrong_token_is_401(self, mock_store, mock_repo, async_client):
        mock_store.get = AsyncMock(return_value=self._stored())
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid_token"

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_no_connection_record_is_401(self, mock_store, mock_repo, async_client):
        mock_store.get = AsyncMock(return_value=None)
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid_token"

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_disconnected_status_is_401(self, mock_store, mock_repo, async_client):
        # Revocation: SM secret may briefly outlive the status row (or vice
        # versa); either missing/non-connected side must 401 — and with the
        # SAME detail as the bad-token path, so an unauthenticated caller
        # can't oracle whether an owner_id has a connection.
        mock_store.get = AsyncMock(return_value=self._stored())
        mock_repo.get = AsyncMock(return_value=None)
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid_token"

    @pytest.mark.asyncio
    async def test_owner_id_too_long_is_422(self, async_client):
        # Unauthenticated endpoint: an unbounded owner_id must be rejected at
        # validation, before any store lookup.
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "x" * 129},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["org bad", "org/../x", "org_x\n", "org.x", "org%00"])
    async def test_owner_id_bad_charset_is_422(self, async_client, bad):
        # Chars outside the Clerk id shape (whitespace, path separators, control
        # bytes) must never reach the DDB key / SM name / RoleSessionName sinks.
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": bad},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_secret_store_client_error_is_503(self, mock_store, mock_repo, async_client):
        from botocore.exceptions import ClientError

        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_store.get = AsyncMock(
            side_effect=ClientError({"Error": {"Code": "ValidationException", "Message": "x"}}, "GetSecretValue")
        )
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "credential_service_unavailable"

    @pytest.mark.asyncio
    @patch("routers.connections._sts_client")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_sts_failure_is_502(self, mock_store, mock_repo, mock_sts, async_client):
        mock_store.get = AsyncMock(return_value=self._stored())
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_sts.return_value.assume_role.side_effect = RuntimeError("boom")
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 502

    @pytest.mark.asyncio
    @patch("routers.connections._sts_client")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_rate_limited_after_burst(self, mock_store, mock_repo, mock_sts, async_client):
        from routers import connections as conn_mod

        mock_store.get = AsyncMock(return_value=self._stored())
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_sts.return_value.assume_role.return_value = self._sts_response()
        conn_mod._aws_cli_rate_buckets.clear()
        try:
            for _ in range(conn_mod._AWS_CLI_RATE_LIMIT_PER_MINUTE):
                ok = await async_client.post(
                    "/api/v1/connections/aws/credentials",
                    json={"owner_id": "org_rate"},
                    headers={"Authorization": f"Bearer {self._TOKEN}"},
                )
                assert ok.status_code == 200
            over = await async_client.post(
                "/api/v1/connections/aws/credentials",
                json={"owner_id": "org_rate"},
                headers={"Authorization": f"Bearer {self._TOKEN}"},
            )
            assert over.status_code == 429
            assert over.headers["Retry-After"] == "60"
        finally:
            conn_mod._aws_cli_rate_buckets.clear()

    @pytest.mark.asyncio
    @patch("routers.connections._sts_client")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_wrong_token_spray_never_locks_out_the_real_token(
        self, mock_store, mock_repo, mock_sts, async_client
    ):
        # Wrong tokens are refused against the cached credential: no Secrets Manager fetch per
        # attempt and no rate-bucket slot, so the real helper is never 429'd by someone's junk.
        from routers import connections as conn_mod

        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_store.get = AsyncMock(return_value=self._stored())
        mock_sts.return_value.assume_role.return_value = self._sts_response()
        for _ in range(conn_mod._AWS_CLI_RATE_LIMIT_PER_MINUTE * 3):
            r = await async_client.post(
                "/api/v1/connections/aws/credentials",
                json={"owner_id": "org_spray"},
                headers={"Authorization": "Bearer wrong"},
            )
            assert r.status_code == 401
        assert mock_store.get.await_count == 1
        ok = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_spray"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert ok.status_code == 200

    @pytest.mark.asyncio
    @patch("routers.connections._sts_client")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_a_matching_cache_entry_expires_so_a_rotated_token_is_refused(
        self, mock_store, mock_repo, mock_sts, async_client
    ):
        # Disconnect + reconnect on another task: the old token matched this task's cache.
        # After the TTL the entry is refetched and the old token no longer works.
        from routers import connections as conn_mod

        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_store.get = AsyncMock(return_value=self._stored())
        mock_sts.return_value.assume_role.return_value = self._sts_response()

        def call():
            return async_client.post(
                "/api/v1/connections/aws/credentials",
                json={"owner_id": "org_rot"},
                headers={"Authorization": f"Bearer {self._TOKEN}"},
            )

        assert (await call()).status_code == 200
        mock_store.get = AsyncMock(return_value={**self._stored(), "cli_token_hash": "0" * 64})
        stamp, rec = conn_mod._aws_cli_records["org_rot"]
        conn_mod._aws_cli_records["org_rot"] = (stamp - conn_mod._AWS_CLI_RECORD_TTL_S, rec)
        assert (await call()).status_code == 401
        mock_store.get.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_a_concurrent_bad_token_burst_on_a_cold_cache_fetches_once(self, mock_store, mock_repo, async_client):
        import asyncio

        async def slow_get(owner_id, provider):
            await asyncio.sleep(0.01)  # hold the fetch open so the burst really overlaps it
            return self._stored()

        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_store.get = AsyncMock(side_effect=slow_get)
        responses = await asyncio.gather(
            *(
                async_client.post(
                    "/api/v1/connections/aws/credentials",
                    json={"owner_id": "org_burst"},
                    headers={"Authorization": f"Bearer wrong{i}"},
                )
                for i in range(20)
            )
        )
        assert [r.status_code for r in responses] == [401] * 20
        assert mock_store.get.await_count == 1


class TestProjectsRailNamespaceIsInvisibleHere:
    """Regression: the Stripe Projects provisioning rail keys its
    ``integrations`` rows ``projects/{provider}``, NOT the bare provider this
    rail owns. ``posthog`` sits on both rails, so a shared key made the OAuth
    readers treat a provisioned provider as OAuth-connected — which 409'd the
    OAuth start and let Disconnect force-delete the Projects credentials."""

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    async def test_a_projects_row_does_not_read_as_connected(self, mock_repo, async_client):
        mock_repo.list_for_owner = AsyncMock(
            return_value=[
                {
                    "provider": "projects/posthog",
                    "status": "connected",
                    "actor": "projects",
                    "provider_resources": [{"resource_id": "res_1", "env_keys": ["POSTHOG_API_KEY"]}],
                }
            ]
        )

        response = await async_client.get("/api/v1/connections")
        assert response.status_code == 200
        by_key = {c["provider"]: c for c in response.json()["connections"]}
        assert by_key["posthog"]["status"] == "disconnected"
        # And the namespaced key is not itself surfaced as a provider.
        assert "projects/posthog" not in by_key

    @pytest.mark.asyncio
    async def test_a_projects_row_publishes_no_mcp_server(self, monkeypatch):
        """``posthog`` rides ``mcp_remote``, which emits a server block from the
        registry + a connected status ALONE (no secret read) — so a bare-keyed
        Projects row would have injected a live MCP server the owner never
        connected."""
        from core.services.credentials import mcp_server

        monkeypatch.setattr(mcp_server.integrations_repo, "get_manual_mcp_servers", AsyncMock(return_value={}))
        monkeypatch.setattr(
            mcp_server.integrations_repo,
            "list_for_owner",
            AsyncMock(return_value=[{"provider": "projects/posthog", "status": "connected", "actor": "projects"}]),
        )
        monkeypatch.setattr(mcp_server.integration_secret_store, "get", AsyncMock(return_value={"token": "leaked"}))
        assert await mcp_server.build_connected_integration_servers("org_a") == {}
