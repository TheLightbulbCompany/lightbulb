import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import registry  # noqa: E402
from core.services.credentials.materializers import qbo_dotenv  # noqa: E402

CRED = {"refresh_token": "AB11700000000aBcDeF", "realm_id": "9341457861985034"}
APP = {"client_id": "ABCuTiwBs0Dnwwqt", "client_secret": "THtXBeGwaxKYJUMhXsxl"}

_SET = "core.services.credentials.materializers.qbo_dotenv.config_patcher.set_openclaw_config_path"
_DEL = "core.services.credentials.materializers.qbo_dotenv.config_patcher.delete_openclaw_config_path"
_APP_GET = "core.services.credentials.materializers.qbo_dotenv.platform_oauth_app_store.get"
_EFS = "core.services.credentials.materializers.qbo_dotenv.owner_efs_root"


def _write(tmp_path, cred=CRED, app=APP):
    with (
        patch(_SET, new_callable=AsyncMock) as mock_set,
        patch(_APP_GET, new_callable=AsyncMock, return_value=app),
        patch(_EFS, return_value=tmp_path),
    ):
        identity = qbo_dotenv.write("org_1", "quickbooks", cred)
    return identity, mock_set, (tmp_path / "quickbooks" / ".env")


def test_write_seeds_store_and_publishes_path_only_block(tmp_path):
    identity, mock_set, store = _write(tmp_path)

    assert identity == "9341457861985034"
    body = store.read_text()
    # The platform secret comes from Secrets Manager, NOT the credential payload
    # (oauth_dance blanks it for platform_app providers).
    assert "QUICKBOOKS_CLIENT_SECRET=THtXBeGwaxKYJUMhXsxl" in body
    assert "QUICKBOOKS_REFRESH_TOKEN=AB11700000000aBcDeF" in body
    assert "QUICKBOOKS_REALM_ID=9341457861985034" in body
    # Their client mints its own access token from the refresh token; persisting a
    # 60-minute access token would only leave a dead secret on disk.
    assert "ACCESS_TOKEN" not in body
    assert store.stat().st_mode & 0o777 == 0o600

    # The published block must carry the store PATH and no credential — that is
    # what lets a reprovision rebuild it from the registry alone.
    (_owner, path, block), _ = mock_set.call_args
    assert path == ["mcp", "servers", "quickbooks"]
    assert block["env"] == {"QUICKBOOKS_TOKEN_STORE_PATH": "/home/node/.openclaw/quickbooks/.env"}
    assert "THtXBeGwaxKYJUMhXsxl" not in str(block)


def test_rehydrated_block_matches_what_the_materializer_publishes(tmp_path):
    """The reprovision path builds the block independently; a drift between the two
    silently points the server at a store that isn't there."""
    _identity, mock_set, _store = _write(tmp_path)
    (_owner, _path, written), _ = mock_set.call_args
    stdio = registry.get_provider("quickbooks").mcp_stdio
    assert stdio.server_block(qbo_dotenv.container_store_path()) == written


def test_write_refuses_without_realm_id(tmp_path):
    with pytest.raises(ValueError, match="realmId"):
        _write(tmp_path, cred={"refresh_token": "AB11700000000aBcDeF"})


def test_write_refuses_unseeded_platform_app(tmp_path):
    with pytest.raises(ValueError, match="unseeded"):
        _write(tmp_path, app=None)


def test_write_refuses_values_dotenv_would_mangle(tmp_path):
    """A quote or newline in a value would silently mis-parse into a token that only
    looks right, so the guard rejects it rather than writing it."""
    with pytest.raises(ValueError, match="mangle"):
        _write(tmp_path, cred={"refresh_token": 'tok"\nQUICKBOOKS_REALM_ID=evil', "realm_id": "93414"})


def test_remove_drops_store_and_server_block(tmp_path):
    _identity, _set, store = _write(tmp_path)
    assert store.exists()
    with patch(_DEL, new_callable=AsyncMock) as mock_del, patch(_EFS, return_value=tmp_path):
        qbo_dotenv.remove("org_1", "quickbooks", CRED)
        qbo_dotenv.remove("org_1", "quickbooks", CRED)  # idempotent

    assert not store.exists()
    (_owner, path), _ = mock_del.call_args
    assert path == ["mcp", "servers", "quickbooks"]
