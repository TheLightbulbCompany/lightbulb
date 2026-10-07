from pathlib import Path

import yaml

from core.services.credentials.materializers import xurl_cli


def _patch_efs(monkeypatch, tmp_path):
    # Materializers only run inside a projection (credentials/hermes_cli.py), so the root is
    # the ContextVar's, not a settings path. tmp_path/"org_1" keeps every path assertion below.
    monkeypatch.setattr(xurl_cli, "materializer_root", lambda: tmp_path / "org_1")


def _xurl_path(tmp_path):
    return Path(tmp_path) / "org_1" / ".xurl"


def _credential(**overrides):
    cred = {
        "client_id": "x-client",
        "client_secret": "x-secret",
        "access_token": "x-access",
        "refresh_token": "x-refresh",
        "expires_in": 3600,
        "username": "isol8",
        "redirect_uri": "https://api.isol8.example/api/v1/oauth/x/callback",
    }
    cred.update(overrides)
    return cred


def test_write_produces_xurl_yaml(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    monkeypatch.setattr(xurl_cli.time, "time", lambda: 1_700_000_000)

    identity = xurl_cli.write("org_1", "x", _credential())

    assert identity == "isol8"
    path = _xurl_path(tmp_path)
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"
    data = yaml.safe_load(path.read_text())
    assert data["default_app"] == "lightbulb-x"
    app = data["apps"]["lightbulb-x"]
    assert app["client_id"] == "x-client"
    assert app["client_secret"] == "x-secret"
    assert app["redirect_uri"] == "https://api.isol8.example/api/v1/oauth/x/callback"
    assert app["default_user"] == "isol8"
    token = app["oauth2_tokens"]["isol8"]
    assert token["type"] == "oauth2"
    assert token["oauth2"] == {
        "access_token": "x-access",
        "refresh_token": "x-refresh",
        "expiration_time": 1_700_003_600,
    }


def test_store_mutations_hold_an_exclusive_lock(monkeypatch, tmp_path):
    """~/.xurl is multi-writer (the owner's in-container xurl rewrites it on token
    refresh), so every read-modify-write must be fcntl-guarded — an unlocked
    read-then-overwrite silently drops the racing writer's apps. Structural guard:
    a regression to plain read/write would take no lock at all."""
    _patch_efs(monkeypatch, tmp_path)
    modes = []
    real_lockf = xurl_cli.fcntl.lockf
    monkeypatch.setattr(xurl_cli.fcntl, "lockf", lambda fd, op, *a: (modes.append(op), real_lockf(fd, op, *a))[1])

    xurl_cli.write("org_1", "x", _credential())
    assert xurl_cli.fcntl.LOCK_EX in modes

    modes.clear()
    xurl_cli.remove("org_1", "x", {})
    assert xurl_cli.fcntl.LOCK_EX in modes


def test_write_derives_redirect_uri_when_credential_omits_it(monkeypatch, tmp_path):
    """oauth_dance carries redirect_uri on the credential, but a credential minted
    before that field existed must not write an empty callback into the owner's
    xurl app entry — xurl's interactive re-auth reads it back."""
    _patch_efs(monkeypatch, tmp_path)
    monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", "https://api-dev.isol8.co")
    cred = _credential()
    cred.pop("redirect_uri")

    xurl_cli.write("org_1", "x", cred)

    app = yaml.safe_load(_xurl_path(tmp_path).read_text())["apps"]["lightbulb-x"]
    assert app["redirect_uri"] == "https://api-dev.isol8.co/api/v1/oauth/x/callback"


def test_write_preserves_unknown_top_level_keys(monkeypatch, tmp_path):
    """~/.xurl is shared with the owner's own xurl usage. A key xurl grows that we
    don't model must survive our read-modify-write, not get normalized away."""
    _patch_efs(monkeypatch, tmp_path)
    path = _xurl_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {"apps": {}, "default_app": "", "some_future_xurl_key": {"keep": True}},
            sort_keys=False,
        )
    )

    xurl_cli.write("org_1", "x", _credential())

    data = yaml.safe_load(path.read_text())
    assert data["some_future_xurl_key"] == {"keep": True}
    assert data["apps"]["lightbulb-x"]["client_id"] == "x-client"


def test_write_preserves_unrelated_apps_and_replaces_owned_app(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    path = _xurl_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apps": {
                    "personal": {"client_id": "keep", "oauth2_tokens": {}},
                    "lightbulb-x": {"client_id": "old", "oauth2_tokens": {}},
                },
                "default_app": "personal",
            },
            sort_keys=False,
        )
    )

    xurl_cli.write("org_1", "x", _credential(username="newuser"))

    data = yaml.safe_load(path.read_text())
    assert data["apps"]["personal"]["client_id"] == "keep"
    assert data["apps"]["lightbulb-x"]["client_id"] == "x-client"
    assert list(data["apps"]["lightbulb-x"]["oauth2_tokens"]) == ["newuser"]
    assert data["default_app"] == "lightbulb-x"


def test_write_fetches_username_when_not_supplied(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"username": "from_api"}}

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return Resp()

    monkeypatch.setattr(xurl_cli.httpx, "get", fake_get)
    cred = _credential()
    cred.pop("username")

    assert xurl_cli.write("org_1", "x", cred) == "from_api"
    assert calls == [
        (
            "https://api.x.com/2/users/me",
            {"Authorization": "Bearer x-access"},
            10,
        )
    ]


def test_remove_deletes_only_isol8_app(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    path = _xurl_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apps": {
                    "personal": {"client_id": "keep", "oauth2_tokens": {}},
                    "lightbulb-x": {"client_id": "remove", "oauth2_tokens": {}},
                },
                "default_app": "lightbulb-x",
            },
            sort_keys=False,
        )
    )

    xurl_cli.remove("org_1", "x", {})

    data = yaml.safe_load(path.read_text())
    assert set(data["apps"]) == {"personal"}
    assert data["default_app"] == "personal"


def test_remove_deletes_file_when_no_apps_remain(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    xurl_cli.write("org_1", "x", _credential())
    path = _xurl_path(tmp_path)
    assert path.exists()

    xurl_cli.remove("org_1", "x", {})

    assert not path.exists()


def test_remove_is_idempotent_when_missing(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    xurl_cli.remove("org_1", "x", {})
