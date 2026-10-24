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
        return {"email": self._email}


def _patch_efs(monkeypatch, tmp_path):
    # Materializers only run inside a projection (credentials/hermes_cli.py), so the root is
    # the ContextVar's, not a settings path. tmp_path/"org_1" keeps every path assertion below.
    monkeypatch.setattr(gog_cli, "materializer_root", lambda: tmp_path / "org_1")


def _patch_keyring_password(monkeypatch):
    monkeypatch.setattr("core.config.settings.GOG_KEYRING_PASSWORD", "kp")


def _fake_run_factory(runs):
    """A subprocess.run fake that records every gog invocation and, for the token
    `export` step, writes the token file gog would produce so the revoke path can
    read it back. The exported refresh token is deterministic per email."""

    def _fake_run(argv, **kwargs):
        runs.append(
            {"argv": argv, "env": kwargs.get("env"), "check": kwargs.get("check"), "input": kwargs.get("input")}
        )
        if "export" in argv:
            email = argv[argv.index("export") + 1]
            out_path = argv[argv.index("--out") + 1]
            Path(out_path).write_text(json.dumps({"email": email, "refresh_token": f"rt-{email}"}))

        class _CP:
            returncode = 0

        return _CP()

    return _fake_run


def _fake_post_factory(posts):
    def _fake_post(url, data=None, timeout=None):
        posts.append({"url": url, "data": data})

        class _R:
            status_code = 200

        return _R()

    return _fake_post


def test_write_seeds_client_and_token_via_gog(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    profile_calls = []

    def _fake_get(url, headers=None):
        profile_calls.append((url, headers))
        return _FakeResp("user@example.com")

    runs = []

    def _fake_run(argv, **kwargs):
        runs.append(
            {"argv": argv, "input": kwargs.get("input"), "env": kwargs.get("env"), "check": kwargs.get("check")}
        )

        class _CP:
            returncode = 0

        return _CP()

    monkeypatch.setattr(gog_cli.httpx, "get", _fake_get)
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    # Force the non-root path so chown is skipped under test.
    monkeypatch.setattr(gog_cli.os, "geteuid", lambda: 1000)

    # write returns the derived account email so the connect path can record it
    # in the per-owner Google inventory without a second userinfo fetch.
    assert gog_cli.write("org_1", "google", cred) == "user@example.com"

    # userinfo called with the Bearer access token.
    assert profile_calls == [
        (
            "https://openidconnect.googleapis.com/v1/userinfo",
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

    def _fake_run(argv, **kwargs):
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

    def _fake_run(argv, **kwargs):
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


def test_write_rollback_never_deletes_token_on_import_failure(monkeypatch, tmp_path, cred):
    # `credentials set` lands, then the token import fails. The rollback must NOT
    # delete any keyring entry (no `gog auth remove`, no rmtree): a connect rollback
    # never destroys a credential — a re-auth would lose the user's existing token,
    # and a brand-new account's partial state is harmless (absent from the inventory,
    # overwritten on retry, dropped on full disconnect).
    import subprocess as _sp

    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)
    monkeypatch.setattr(gog_cli.httpx, "get", lambda url, headers=None: _FakeResp("new@example.com"))

    runs = []

    def _fake_run(argv, **kwargs):
        runs.append(argv)
        if argv[:4] == ["gog", "auth", "tokens", "import"]:
            raise _sp.CalledProcessError(1, argv, stderr="boom")

        class _CP:
            returncode = 0

        return _CP()

    rmtree_calls = []
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(gog_cli.shutil, "rmtree", lambda *a, **k: rmtree_calls.append(a))
    monkeypatch.setattr(gog_cli.os, "geteuid", lambda: 1000)

    with pytest.raises(_sp.CalledProcessError):
        gog_cli.write("org_1", "google", cred)

    assert not any(a[:3] == ["gog", "auth", "remove"] for a in runs)
    assert rmtree_calls == []


def test_write_rollback_never_deletes_token_on_chown_failure(monkeypatch, tmp_path, cred):
    # The token import succeeds, then the post-import chown fails (an EFS ownership
    # hiccup while running as root). The error propagates, but the rollback still must
    # not delete the keyring entry — the imported token (new or re-auth) is left
    # intact rather than destroyed on a reported-failed connect.
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)
    monkeypatch.setattr(gog_cli.httpx, "get", lambda url, headers=None: _FakeResp("new@example.com"))

    runs = []

    def _fake_run(argv, **kwargs):
        runs.append(argv)

        class _CP:
            returncode = 0

        return _CP()

    def _boom_chown(path):
        raise OSError("chown boom")

    rmtree_calls = []
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(gog_cli, "_chown_tree_to_node", _boom_chown)
    monkeypatch.setattr(gog_cli.shutil, "rmtree", lambda *a, **k: rmtree_calls.append(a))

    with pytest.raises(OSError):
        gog_cli.write("org_1", "google", cred)

    assert not any(a[:3] == ["gog", "auth", "remove"] for a in runs)
    assert rmtree_calls == []


def test_write_rollback_restores_shared_config_ownership_when_root(monkeypatch, tmp_path, cred):
    # When running as root, `gog auth credentials set` rewrites the SHARED
    # .config/gogcli as root before the connect fails. The rollback must re-chown it
    # to the container uid, or the owner's OTHER accounts (which share that client
    # config) become unreadable by the container's uid-1000 gog.
    import subprocess as _sp

    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)
    monkeypatch.setattr(gog_cli.httpx, "get", lambda url, headers=None: _FakeResp("new@example.com"))

    config_gogcli = Path(tmp_path) / "org_1" / ".config" / "gogcli"

    def _fake_run(argv, **kwargs):
        if argv[:4] == ["gog", "auth", "credentials", "set"]:
            config_gogcli.mkdir(parents=True, exist_ok=True)
            (config_gogcli / "client.json").write_text("client")
        if argv[:4] == ["gog", "auth", "tokens", "import"]:
            raise _sp.CalledProcessError(1, argv, stderr="boom")

        class _CP:
            returncode = 0

        return _CP()

    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(gog_cli.os, "geteuid", lambda: 0)
    chowned = []
    monkeypatch.setattr(gog_cli.os, "chown", lambda p, uid, gid: chowned.append(str(p)))

    with pytest.raises(_sp.CalledProcessError):
        gog_cli.write("org_1", "google", cred)

    # The shared client config left root-owned by the failed connect is re-chowned.
    assert str(config_gogcli) in chowned


def test_remove_with_email_revokes_then_removes_one_account(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    # A keyring with multiple accounts must survive disconnecting one account.
    # The keyring lives under .local/share/gogcli (XDG_DATA_HOME), not .config.
    gogcli_dir = Path(tmp_path) / "org_1" / ".local" / "share" / "gogcli"
    gogcli_dir.mkdir(parents=True)
    (gogcli_dir / "keyring.jwe").write_text("blob")

    runs = []
    posts = []
    rmtree_calls = []
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run_factory(runs))
    monkeypatch.setattr(gog_cli.httpx, "post", _fake_post_factory(posts))
    monkeypatch.setattr(gog_cli.shutil, "rmtree", lambda *a, **k: rmtree_calls.append((a, k)))

    gog_cli.remove("org_1", "google", {"email": "a@x.com"})

    # The grant is revoked at Google FIRST (token exported from the keyring, then
    # POSTed to the revoke endpoint) — gog's local remove does not revoke server-side.
    export_runs = [r for r in runs if "export" in r["argv"]]
    assert len(export_runs) == 1
    assert export_runs[0]["argv"][:5] == ["gog", "auth", "tokens", "export", "a@x.com"]
    assert posts == [{"url": "https://oauth2.googleapis.com/revoke", "data": {"token": "rt-a@x.com"}}]

    # Then exactly one per-account remove; the email is an argv element, never a shell string.
    remove_runs = [r for r in runs if r["argv"][:3] == ["gog", "auth", "remove"]]
    assert len(remove_runs) == 1
    run = remove_runs[0]
    assert run["argv"] == ["gog", "auth", "remove", "a@x.com", "--no-input", "--force"]
    assert run["check"] is True

    # Same env as write: XDG_CONFIG_HOME + XDG_DATA_HOME under the projection root +
    # keyring backend/password. XDG_DATA_HOME is required or gog operates on the
    # wrong (empty) keyring and the per-account remove silently no-ops.
    expected_cfg = str(Path(tmp_path) / "org_1" / ".config")
    expected_data = str(Path(tmp_path) / "org_1" / ".local" / "share")
    assert run["env"]["XDG_CONFIG_HOME"] == expected_cfg
    assert run["env"]["XDG_DATA_HOME"] == expected_data
    assert run["env"]["GOG_KEYRING_BACKEND"] == "file"
    assert run["env"]["GOG_KEYRING_PASSWORD"] == "kp"

    # The whole keyring is NOT nuked when removing a single account — no rmtree,
    # and the keyring dir survives (gog removes just the one account's entry).
    assert rmtree_calls == []
    assert gogcli_dir.exists()


def test_remove_revoke_failure_does_not_block_local_removal(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    runs = []

    def _fake_run(argv, **kwargs):
        runs.append(argv)
        # The token export fails (e.g. keyring drift) — revoke is best-effort and
        # must not stop the authoritative local removal.
        if "export" in argv:
            raise RuntimeError("export boom")

        class _CP:
            returncode = 0

        return _CP()

    posts = []
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(gog_cli.httpx, "post", _fake_post_factory(posts))

    gog_cli.remove("org_1", "google", {"email": "a@x.com"})

    # Revoke was skipped (export failed) but the local remove still ran.
    assert posts == []
    assert ["gog", "auth", "remove", "a@x.com", "--no-input", "--force"] in runs


def test_remove_revoke_non_2xx_still_removes_locally(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)

    runs = []
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run_factory(runs))

    def _fake_post_500(url, data=None, timeout=None):
        class _R:
            status_code = 500

        return _R()

    monkeypatch.setattr(gog_cli.httpx, "post", _fake_post_500)

    # A non-200/400 revoke response (grant may still be live) is logged but must
    # NOT block the authoritative local removal.
    gog_cli.remove("org_1", "google", {"email": "a@x.com"})
    assert ["gog", "auth", "remove", "a@x.com", "--no-input", "--force"] in [r["argv"] for r in runs]


def test_remove_no_email_no_flag_is_noop(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)

    # An existing keyring holding the owner's accounts.
    data_gogcli = Path(tmp_path) / "org_1" / ".local" / "share" / "gogcli"
    data_gogcli.mkdir(parents=True)
    (data_gogcli / "keyring.jwe").write_text("blob")

    runs = []
    rmtree_calls = []
    monkeypatch.setattr(gog_cli.subprocess, "run", lambda *a, **k: runs.append(a))
    monkeypatch.setattr(gog_cli.shutil, "rmtree", lambda *a, **k: rmtree_calls.append(a))

    # The failed-connect rollback path: the credential is the raw OAuth payload (no
    # email, no disconnect_all). It must NOT wipe the owner's keyring — write()
    # already scoped-cleaned its own in-flight account.
    gog_cli.remove("org_1", "google", {"access_token": "at", "refresh_token": "rt", "client_id": "cid"})

    assert runs == []
    assert rmtree_calls == []
    assert data_gogcli.exists()


def test_disconnect_all_revokes_each_account_then_deletes_keyring_and_config(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)
    # The keyring lives under .local/share/gogcli; the OAuth client config under
    # .config/gogcli. A full disconnect must drop BOTH.
    data_gogcli = Path(tmp_path) / "org_1" / ".local" / "share" / "gogcli"
    config_gogcli = Path(tmp_path) / "org_1" / ".config" / "gogcli"
    data_gogcli.mkdir(parents=True)
    (data_gogcli / "keyring.jwe").write_text("blob")
    config_gogcli.mkdir(parents=True)
    (config_gogcli / "client.json").write_text("client")

    runs = []
    posts = []
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run_factory(runs))
    monkeypatch.setattr(gog_cli.httpx, "post", _fake_post_factory(posts))

    gog_cli.remove("org_1", "google", {"disconnect_all": True, "accounts": ["a@x.com", "b@x.com"]})

    # Every account's grant is revoked at Google (token exported, then POSTed)
    # before the keyring is dropped — two exports, two revokes, no per-account remove.
    exported = sorted(r["argv"][r["argv"].index("export") + 1] for r in runs if "export" in r["argv"])
    assert exported == ["a@x.com", "b@x.com"]
    assert {p["data"]["token"] for p in posts} == {"rt-a@x.com", "rt-b@x.com"}
    assert not any(r["argv"][:3] == ["gog", "auth", "remove"] for r in runs)

    # Both on-disk trees are gone after the wipe.
    assert not data_gogcli.exists()
    assert not config_gogcli.exists()


def test_disconnect_all_is_idempotent_when_missing(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    _patch_keyring_password(monkeypatch)
    # No keyring dir and no accounts — a full disconnect must not raise.
    gog_cli.remove("org_1", "google", {"disconnect_all": True, "accounts": []})


def test_write_reads_platform_secret_when_credential_has_none(monkeypatch, tmp_path):
    """google is a platform app: oauth_dance blanks the secret, so the materializer
    must fetch the app's client from the platform store for gog's own refresh."""
    _patch_efs(monkeypatch, tmp_path)
    runs = []

    class _Done:
        returncode = 0
        stdout = ""

    def _fake_run(argv, **kw):
        runs.append(kw.get("input"))
        return _Done()

    async def _app(name):
        assert name == "google"
        return {"client_id": "pcid", "client_secret": "psec"}

    monkeypatch.setattr(gog_cli.httpx, "get", lambda *a, **k: _FakeResp("user@example.com"))
    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(gog_cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(gog_cli.platform_oauth_app_store, "get", _app)

    cred = {"client_id": "pcid", "client_secret": "", "refresh_token": "rt", "access_token": "at"}
    assert gog_cli.write("org_1", "google", cred) == "user@example.com"
    web = json.loads(runs[0])["web"]
    assert (web["client_id"], web["client_secret"]) == ("pcid", "psec")
