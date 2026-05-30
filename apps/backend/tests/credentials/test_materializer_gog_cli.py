import json
from pathlib import Path

import pytest

from core.services.credentials.materializers import gog_cli


@pytest.fixture
def cred():
    return {
        "client_id": "cid",
        "client_secret": "cs",
        "refresh_token": "rt",
        "access_token": "at",
    }


class _FakeResp:
    def __init__(self, email):
        self._email = email

    def raise_for_status(self):
        pass

    def json(self):
        return {"emailAddress": self._email}


def _patch_efs(monkeypatch, tmp_path):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))


def _patch_keyring_password(monkeypatch):
    monkeypatch.setattr("core.config.settings.GOG_KEYRING_PASSWORD", "kp")


def test_write_seeds_client_and_token_via_gog(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    profile_calls = []

    def _fake_get(url, headers=None):
        profile_calls.append((url, headers))
        return _FakeResp("user@example.com")

    runs = []

    def _fake_run(argv, input=None, env=None, check=None, capture_output=None, text=None):
        runs.append({"argv": argv, "input": input, "env": env, "check": check})

        class _CP:
            returncode = 0

        return _CP()

    monkeypatch.setattr(gog_cli.httpx, "get", _fake_get)
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    # Force the non-root path so chown is skipped under test.
    monkeypatch.setattr(gog_cli.os, "geteuid", lambda: 1000)

    gog_cli.write("org_1", "google", cred)

    # getProfile called with the Bearer access token.
    assert profile_calls == [
        (
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            {"Authorization": "Bearer at"},
        )
    ]

    assert len(runs) == 2
    set_run, import_run = runs

    # Exact argv — fixed, no caller data interpolated.
    assert set_run["argv"] == ["gog", "auth", "credentials", "set", "-", "--no-input"]
    assert import_run["argv"] == ["gog", "auth", "tokens", "import", "-", "--no-input"]
    assert set_run["check"] is True and import_run["check"] is True

    # Client stdin: web wrapper carrying client_id/secret (a flat object is rejected by gog).
    client = json.loads(set_run["input"])
    assert "web" in client
    assert client["web"]["client_id"] == "cid"
    assert client["web"]["client_secret"] == "cs"
    assert client["web"]["auth_uri"] == "https://accounts.google.com/o/oauth2/auth"
    assert client["web"]["token_uri"] == "https://oauth2.googleapis.com/token"

    # Token stdin: field is `email`, NOT `account`.
    token = json.loads(import_run["input"])
    assert token == {"email": "user@example.com", "refresh_token": "rt"}
    assert "account" not in token

    # Env carries XDG_CONFIG_HOME at owner_efs_root/.config + keyring backend/password.
    expected_cfg = str(Path(tmp_path) / "org_1" / ".config")
    for run in (set_run, import_run):
        assert run["env"]["XDG_CONFIG_HOME"] == expected_cfg
        assert run["env"]["GOG_KEYRING_BACKEND"] == "file"
        assert run["env"]["GOG_KEYRING_PASSWORD"] == "kp"

    # The .config dir is created so gog can write its keyring under it.
    assert (Path(tmp_path) / "org_1" / ".config").is_dir()


def test_write_chowns_gogcli_tree_when_root(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    monkeypatch.setattr(gog_cli.httpx, "get", lambda url, headers=None: _FakeResp("u@example.com"))

    # Simulate gog writing a keyring tree, since the real binary is mocked out.
    gogcli_dir = Path(tmp_path) / "org_1" / ".config" / "gogcli"

    def _fake_run(argv, input=None, env=None, check=None, capture_output=None, text=None):
        gogcli_dir.mkdir(parents=True, exist_ok=True)
        (gogcli_dir / "keyring.jwe").write_text("blob")

        class _CP:
            returncode = 0

        return _CP()

    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    # Pretend we're root so the chown branch runs.
    monkeypatch.setattr(gog_cli.os, "geteuid", lambda: 0)

    chowned = []
    monkeypatch.setattr(gog_cli.os, "chown", lambda p, uid, gid: chowned.append((str(p), uid, gid)))

    gog_cli.write("org_1", "google", cred)

    chowned_paths = {p for p, _, _ in chowned}
    assert str(gogcli_dir) in chowned_paths
    assert str(gogcli_dir / "keyring.jwe") in chowned_paths
    # All chowns target node uid/gid 1000.
    assert all(uid == 1000 and gid == 1000 for _, uid, gid in chowned)


def test_write_skips_chown_when_non_root(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    monkeypatch.setattr(gog_cli.httpx, "get", lambda url, headers=None: _FakeResp("u@example.com"))

    def _fake_run(argv, input=None, env=None, check=None, capture_output=None, text=None):
        class _CP:
            returncode = 0

        return _CP()

    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(gog_cli.os, "geteuid", lambda: 1000)

    chowned = []
    monkeypatch.setattr(gog_cli.os, "chown", lambda p, uid, gid: chowned.append(p))

    gog_cli.write("org_1", "google", cred)
    assert chowned == []


def test_write_propagates_gog_failure(monkeypatch, tmp_path, cred):
    import subprocess as _sp

    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    monkeypatch.setattr(gog_cli.httpx, "get", lambda url, headers=None: _FakeResp("u@example.com"))

    def _fake_run(argv, **kwargs):
        raise _sp.CalledProcessError(1, argv, stderr="boom")

    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(gog_cli.os, "geteuid", lambda: 1000)

    with pytest.raises(_sp.CalledProcessError):
        gog_cli.write("org_1", "google", cred)


def test_remove_deletes_gogcli_dir(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    gogcli_dir = Path(tmp_path) / "org_1" / ".config" / "gogcli"
    gogcli_dir.mkdir(parents=True)
    (gogcli_dir / "keyring.jwe").write_text("blob")

    gog_cli.remove("org_1", "google", {})
    assert not gogcli_dir.exists()


def test_remove_is_idempotent_when_missing(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    # No keyring dir exists — remove must not raise.
    gog_cli.remove("org_1", "google", {})
