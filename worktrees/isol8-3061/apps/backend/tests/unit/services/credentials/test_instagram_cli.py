import json
import os
from unittest.mock import patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import registry  # noqa: E402
from core.services.credentials.materializers import instagram_cli  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


CRED = {
    "access_token": "LONG60D",
    "provider_user_id": "178414123",
    "token_expiry": "2026-09-17T00:00:00+00:00",
}


@patch("core.services.credentials.materializers.instagram_cli.httpx.get")
def test_write_seeds_skill_and_credentials(mock_get, tmp_path):
    mock_get.return_value = _FakeResponse({"user_id": "178414123", "username": "isol8ai"})
    with patch(
        "core.services.credentials.materializers.instagram_cli.materializer_root",
        return_value=tmp_path,
    ):
        identity = instagram_cli.write("org_1", "instagram", CRED)

    assert identity == "isol8ai"
    # The token authenticates via the Authorization header, never the query
    # string: the request URL is logged (httpx's own line, any proxy's access
    # log), so an ?access_token= there is a live credential written to CloudWatch.
    _, kwargs = mock_get.call_args
    assert kwargs["headers"] == {"Authorization": "Bearer LONG60D"}
    assert kwargs["params"] == {"fields": "user_id,username"}
    skill_dir = tmp_path / "skills" / "instagram"
    assert (skill_dir / "SKILL.md").exists()
    # The seeded SKILL.md is the agent-facing contract: it must point at the
    # credentials file and carry the confirm-before-publish doctrine.
    body = (skill_dir / "SKILL.md").read_text()
    assert "credentials.json" in body
    assert "media_publish" in body
    creds = json.loads((skill_dir / "credentials.json").read_text())
    assert creds == {
        "access_token": "LONG60D",
        "user_id": "178414123",
        "username": "isol8ai",
        "token_expiry": "2026-09-17T00:00:00+00:00",
    }
    # Hermes finds a skill by SCANNING the profile, so landing these three files IS the
    # install -- there is no config entry to enable and nothing else to write.
    assert sorted(p.name for p in skill_dir.iterdir()) == ["SKILL.md", "credentials.json", "token.sh"]


def test_remove_deletes_the_skill_dir(tmp_path):
    skill_dir = tmp_path / "skills" / "instagram"
    skill_dir.mkdir(parents=True)
    (skill_dir / "credentials.json").write_text("{}")
    with patch(
        "core.services.credentials.materializers.instagram_cli.materializer_root",
        return_value=tmp_path,
    ):
        instagram_cli.remove("org_1", "instagram", {})

    assert not skill_dir.exists()


def test_registry_platform_app_mutual_exclusion():
    # platform_app + byo_required (or registration_url) is a contradiction the
    # registry must reject at load time.
    base = {
        "display_name": "X",
        "acquisition": "direct_oauth",
        "actor": "cli",
        "refresh_owner": "none",
        "wiring": {"materializer": "instagram_cli"},
    }
    oauth = {
        "authorization_url": "https://x/auth",
        "token_url": "https://x/token",
        "scopes": ["a"],
        "platform_app": "meta-x",
    }
    with pytest.raises(ValueError, match="mutually exclusive"):
        registry.Provider(key="x", **base, byo_required=True, oauth=oauth)
    with pytest.raises(ValueError, match="mutually exclusive"):
        registry.Provider(key="x", **base, oauth={**oauth, "registration_url": "https://x/reg"})
    # Clean platform_app validates.
    p = registry.Provider(key="x", **base, oauth=oauth)
    assert p.oauth.platform_app == "meta-x"


def test_registry_loads_instagram_provider():
    p = registry.get_provider("instagram")
    assert p.acquisition == "direct_oauth"
    # actor, not backend-custody: token.sh renews in-container on use, and
    # this is what makes routers.connections._retains_secret skip Secrets Manager.
    assert p.refresh_owner == "actor"
    assert p.oauth.platform_app == "meta-instagram"
    assert p.oauth.long_lived_exchange == "ig_exchange_token"
    assert p.oauth.scope_separator == ","
    assert p.wiring.materializer == "instagram_cli"
    assert "instagram_business_content_publish" in p.oauth.scopes
