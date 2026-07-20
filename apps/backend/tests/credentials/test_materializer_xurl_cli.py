from pathlib import Path

import pytest
import yaml

from core.services.credentials.materializers import xurl_cli


def _patch_efs(monkeypatch, tmp_path):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))


def _xurl_path(tmp_path):
    return Path(tmp_path) / "org_1" / ".xurl"


class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"id": "123", "username": "isol8"}}


@pytest.fixture
def cred():
    return {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_in": 3600,
        "client_id": "client-id",
        "client_secret": "client-secret",
        "redirect_uri": "https://app.isol8.co/api/v1/oauth/x/callback",
    }


def test_write_seeds_xurl_yaml_and_fetches_username(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    calls = []

    def _fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return _FakeResp()

    monkeypatch.setattr(xurl_cli.httpx, "get", _fake_get)

    assert xurl_cli.write("org_1", "x", cred) == "isol8"

    assert calls == [("https://api.x.com/2/users/me", {"Authorization": "Bearer access-token"}, 10)]
    path = _xurl_path(tmp_path)
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"
    data = yaml.safe_load(path.read_text())
    assert data["default_app"] == "isol8-x"
    app = data["apps"]["isol8-x"]
    assert app["client_id"] == "client-id"
    assert app["client_secret"] == "client-secret"
    assert app["redirect_uri"] == "https://app.isol8.co/api/v1/oauth/x/callback"
    assert app["default_user"] == "isol8"
    token = app["oauth2_tokens"]["isol8"]
    assert token["type"] == "oauth2"
    assert token["oauth2"]["access_token"] == "access-token"
    assert token["oauth2"]["refresh_token"] == "refresh-token"
    assert isinstance(token["oauth2"]["expiration_time"], int)


def test_write_preserves_other_apps_and_accepts_supplied_username(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    path = _xurl_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apps": {"personal": {"client_id": "old", "client_secret": "old"}},
                "default_app": "personal",
            },
            sort_keys=False,
        )
    )
    cred = {**cred, "username": "@owner"}
    monkeypatch.setattr(xurl_cli.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unused")))

    assert xurl_cli.write("org_1", "x", cred) == "owner"

    data = yaml.safe_load(path.read_text())
    assert set(data["apps"]) == {"personal", "isol8-x"}
    assert data["default_app"] == "isol8-x"
    assert data["apps"]["isol8-x"]["default_user"] == "owner"


def test_remove_deletes_only_isol8_app(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    path = _xurl_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apps": {
                    "isol8-x": {"client_id": "cid", "client_secret": "sec"},
                    "personal": {"client_id": "old", "client_secret": "old"},
                },
                "default_app": "isol8-x",
            },
            sort_keys=False,
        )
    )

    xurl_cli.remove("org_1", "x", {})

    data = yaml.safe_load(path.read_text())
    assert set(data["apps"]) == {"personal"}
    assert data["default_app"] == "personal"


def test_remove_unlinks_file_when_no_apps_remain(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    path = _xurl_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"apps": {"isol8-x": {"client_id": "cid"}}, "default_app": "isol8-x"}))

    xurl_cli.remove("org_1", "x", {})

    assert not path.exists()


def test_write_requires_refresh_token(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    monkeypatch.setattr(xurl_cli.httpx, "get", lambda *a, **k: _FakeResp())

    with pytest.raises(ValueError, match="refresh_token"):
        xurl_cli.write("org_1", "x", {**cred, "refresh_token": ""})
