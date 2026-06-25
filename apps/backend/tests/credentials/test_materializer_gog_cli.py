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

    # write returns the derived account email so the connect path can record it
    # in the per-owner Google inventory without a second profile fetch.
    assert gog_cli.write("org_1", "google", cred) == "user@example.com"

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

    # Env carries XDG_CONFIG_HOME (gog reads config) AND XDG_DATA_HOME (gog stores
    # the file keyring there — NOT under XDG_CONFIG_HOME), both on the owner's EFS,
    # + keyring backend/password. Without XDG_DATA_HOME the keyring lands on the
    # backend's ephemeral disk and never reaches the per-user container.
    expected_cfg = str(Path(tmp_path) / "org_1" / ".config")
    expected_data = str(Path(tmp_path) / "org_1" / ".local" / "share")
    for run in (set_run, import_run):
        assert run["env"]["XDG_CONFIG_HOME"] == expected_cfg
        assert run["env"]["XDG_DATA_HOME"] == expected_data
        assert run["env"]["GOG_KEYRING_BACKEND"] == "file"
        assert run["env"]["GOG_KEYRING_PASSWORD"] == "kp"

    # Both dirs are created on EFS: .config (client config) and .local/share (keyring).
    assert (Path(tmp_path) / "org_1" / ".config").is_dir()
    assert (Path(tmp_path) / "org_1" / ".local" / "share").is_dir()


def test_write_chowns_gogcli_tree_when_root(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    monkeypatch.setattr(gog_cli.httpx, "get", lambda url, headers=None: _FakeResp("u@example.com"))

    # Simulate the real gog: it writes its OAuth client config under
    # XDG_CONFIG_HOME/gogcli and its file KEYRING under XDG_DATA_HOME/gogcli.
    config_gogcli = Path(tmp_path) / "org_1" / ".config" / "gogcli"
    data_gogcli = Path(tmp_path) / "org_1" / ".local" / "share" / "gogcli"

    def _fake_run(argv, input=None, env=None, check=None, capture_output=None, text=None):
        config_gogcli.mkdir(parents=True, exist_ok=True)
        (config_gogcli / "client.json").write_text("client")
        data_gogcli.mkdir(parents=True, exist_ok=True)
        (data_gogcli / "keyring.jwe").write_text("blob")

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
    # The KEYRING dir (data) must be chowned — else it lands on EFS root-owned and
    # the container's uid-1000 gog cannot read it. The config dir is chowned too.
    assert str(data_gogcli) in chowned_paths
    assert str(data_gogcli / "keyring.jwe") in chowned_paths
    assert str(config_gogcli) in chowned_paths
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


def test_remove_with_email_removes_one_account_and_keeps_keyring(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    # A keyring with multiple accounts must survive disconnecting one account.
    gogcli_dir = Path(tmp_path) / "org_1" / ".config" / "gogcli"
    gogcli_dir.mkdir(parents=True)
    (gogcli_dir / "keyring.jwe").write_text("blob")

    runs = []

    def _fake_run(argv, input=None, env=None, check=None, capture_output=None, text=None):
        runs.append({"argv": argv, "env": env, "check": check})

        class _CP:
            returncode = 0

        return _CP()

    rmtree_calls = []
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(gog_cli.shutil, "rmtree", lambda *a, **k: rmtree_calls.append((a, k)))

    gog_cli.remove("org_1", "google", {"email": "a@x.com"})

    # Exactly one per-account remove; the email is an argv element, never a shell string.
    assert len(runs) == 1
    run = runs[0]
    assert run["argv"] == ["gog", "auth", "remove", "a@x.com", "--no-input", "--force"]
    assert run["check"] is True

    # Same env as write: XDG_CONFIG_HOME + XDG_DATA_HOME under owner_efs_root +
    # keyring backend/password. XDG_DATA_HOME is required or gog operates on the
    # wrong (empty) keyring and the per-account remove silently no-ops.
    expected_cfg = str(Path(tmp_path) / "org_1" / ".config")
    expected_data = str(Path(tmp_path) / "org_1" / ".local" / "share")
    assert run["env"]["XDG_CONFIG_HOME"] == expected_cfg
    assert run["env"]["XDG_DATA_HOME"] == expected_data
    assert run["env"]["GOG_KEYRING_BACKEND"] == "file"
    assert run["env"]["GOG_KEYRING_PASSWORD"] == "kp"

    # The whole keyring is NOT nuked when removing a single account.
    assert rmtree_calls == []


def test_remove_without_email_deletes_keyring_and_config(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    # The keyring lives under .local/share/gogcli; the OAuth client config under
    # .config/gogcli. A full disconnect must drop BOTH.
    data_gogcli = Path(tmp_path) / "org_1" / ".local" / "share" / "gogcli"
    config_gogcli = Path(tmp_path) / "org_1" / ".config" / "gogcli"
    data_gogcli.mkdir(parents=True)
    (data_gogcli / "keyring.jwe").write_text("blob")
    config_gogcli.mkdir(parents=True)
    (config_gogcli / "client.json").write_text("client")

    # No per-account subprocess may run on the disconnect-ALL path.
    runs = []
    monkeypatch.setattr(
        gog_cli.subprocess,
        "run",
        lambda *a, **k: runs.append((a, k)),
    )

    gog_cli.remove("org_1", "google", {})
    assert not data_gogcli.exists()
    assert not config_gogcli.exists()
    assert runs == []


def test_remove_is_idempotent_when_missing(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    # No keyring dir exists — remove must not raise.
    gog_cli.remove("org_1", "google", {})
