"""Tests for the public platform-info catalog route.

The route serves the LIVE connectable integrations + channels catalog with NO
auth so the baked isol8 skill can read it from inside a container instead of
hardcoding a drift-prone list. These tests pin the two guarantees that make the
live read worth it:

- a newly-added provider/channel surfaces with ZERO skill/route change, and
- the response never leaks the Provider's internal wiring (projection, not
  ``model_dump()``).
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.constants import CHANNEL_PROVIDER_IDS  # noqa: E402
from core.services.credentials import registry  # noqa: E402
from routers import platform_info  # noqa: E402

PREFIX = "/api/v1/platform-info"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(platform_info.router, prefix=PREFIX)
    return TestClient(app)


def test_catalog_is_public_and_lists_supported_integrations(client):
    """No auth → 200, and the dev broker exposes hosted MCP + skill rails."""
    resp = client.get(PREFIX)
    assert resp.status_code == 200
    body = resp.json()

    got_integrations = {i["key"] for i in body["integrations"]}
    assert got_integrations <= set(registry.all_providers())
    assert "google" in got_integrations
    assert "github" in got_integrations
    assert "stripe" in got_integrations
    assert "vercel_cli" in got_integrations
    assert "notion" in got_integrations
    assert "quickbooks" in got_integrations

    got_channels = {c["key"] for c in body["channels"]}
    assert got_channels == set(CHANNEL_PROVIDER_IDS)

    # Each integration carries exactly the projected fields, nothing else.
    for entry in body["integrations"]:
        assert set(entry.keys()) == {"key", "display_name", "byo_required", "requestable", "actor"}
        # The rail is a literal enum — agents branch on it to know whether the
        # integration surfaces as tools (mcp) or via the shell (cli).
        assert entry["actor"] in {"cli", "mcp", "skill", "nango_proxy"}
    for entry in body["channels"]:
        assert set(entry.keys()) == {"key", "display_name"}


def test_new_provider_surfaces_with_no_skill_change(client, monkeypatch):
    """Appending a provider to the registry surfaces it in the catalog — the
    whole point of the live read (no image rebuild, no skill edit)."""
    fake = registry.Provider(
        key="acme",
        display_name="Acme",
        byo_required=True,
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=registry.Wiring(mcp_server=registry.McpServer(key="acme", command="acme")),
    )
    real = dict(registry.all_providers())
    real["acme"] = fake
    monkeypatch.setattr(platform_info.registry, "all_providers", lambda: real)

    body = client.get(PREFIX).json()
    entry = next(i for i in body["integrations"] if i["key"] == "acme")
    assert entry == {
        "key": "acme",
        "display_name": "Acme",
        "byo_required": True,
        "requestable": False,
        "actor": "mcp",
    }


def test_env_gated_provider_is_hidden_unless_it_opts_in(client, monkeypatch):
    """A gate still means HIDDEN on this unauthenticated route. Advertising a
    gated provider as requestable is an explicit per-provider opt-in
    (``offer_request_access``) — otherwise adding a half-wired dev-only provider
    would have agents pitching it to every prod owner, with no knob to stop it.
    """
    monkeypatch.setattr(registry.settings, "ENVIRONMENT", "dev")
    dev = {i["key"]: i for i in client.get(PREFIX).json()["integrations"]}
    assert dev["instagram"]["requestable"] is False

    monkeypatch.setattr(registry.settings, "ENVIRONMENT", "prod")
    prod = {i["key"]: i for i in client.get(PREFIX).json()["integrations"]}
    assert registry.get_provider("instagram").offer_request_access is False
    assert "instagram" not in prod

    # Opted in → advertised, flagged requestable, so the agent tells the owner to
    # request it rather than claiming it is one click away.
    opted_in = registry.get_provider("instagram").model_copy(update={"offer_request_access": True})
    monkeypatch.setattr(platform_info.registry, "all_providers", lambda: {"instagram": opted_in})
    body = {i["key"]: i for i in client.get(PREFIX).json()["integrations"]}
    assert body["instagram"]["requestable"] is True


def test_new_channel_surfaces_with_no_name_map(client, monkeypatch):
    """A channel key derives its display name via ``key.replace("_", " ").title()``
    — there is no hardcoded name map to edit, so a new key never 500s on a
    KeyError."""
    monkeypatch.setattr(platform_info, "CHANNEL_PROVIDER_IDS", ("telegram", "whatsapp"))
    body = client.get(PREFIX).json()
    entry = next(c for c in body["channels"] if c["key"] == "whatsapp")
    assert entry == {"key": "whatsapp", "display_name": "Whatsapp"}


def test_no_internal_wiring_disclosure(client):
    """Projection, not model_dump: none of the Provider's internal wiring keys
    (MCP command/env, materializer, OAuth token_url, skill package, STS policy
    ARN) may appear anywhere in the public response body."""
    raw = client.get(PREFIX).text
    for forbidden in ("command", "env", "materializer", "token_url", "package", "policy_arn"):
        assert forbidden not in raw, f"leaked internal wiring token: {forbidden!r}"
