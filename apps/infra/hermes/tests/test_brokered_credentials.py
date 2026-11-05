"""Spec 13 §8.3: brokered credentials on the bridge (V11). Fake HTTP and S3; no token in a log."""

import hashlib
import json
import sys
import types

import pytest
import yaml

from bridge import credentials, server, sync, turn

MARKER = {"providers": {"openai-codex": {"custody": "backend", "tokens": {}}}, "active_provider": "openai-codex"}
MODEL = {"provider": "openai-codex", "access_token": "AT-1", "id_token": "ID-1", "account_id": "acct", "expires_at": 1_900_000_000}


class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


@pytest.fixture
def backend(monkeypatch):
    """A fake credentials route: ``answers`` is consumed per call (path suffix -> (status, payload))."""
    calls, answers = [], {}

    def urlopen(request, timeout):
        path = request.full_url.split("/u/org_test", 1)[1] or "/"
        calls.append((path, json.loads(request.data)))
        status, payload = answers[path]
        if isinstance(status, Exception):
            raise status
        if status != 200:
            import io
            import urllib.error

            raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(json.dumps(payload).encode()))
        return _Resp(payload)

    monkeypatch.setattr(credentials.urllib.request, "urlopen", urlopen)
    for mod in (credentials, turn):
        monkeypatch.setattr(mod, "API", "https://api.example")
        monkeypatch.setattr(mod, "OWNER", "org_test")
        monkeypatch.setattr(mod, "HOOK_TOKEN", "hook")
    return types.SimpleNamespace(calls=calls, answers=answers)


@pytest.fixture
def s3(monkeypatch, home):
    """restore_root_auth/save_root_auth against a one-object fake bucket holding ``stored``."""
    state = types.SimpleNamespace(stored=MARKER, puts=[])
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket")
    monkeypatch.setattr(sync, "_ROOT_AUTH_REVISION", None)
    monkeypatch.setattr(sync, "_ROOT_AUTH_ETAG", None)
    monkeypatch.setattr(sync, "_ROOT_AUTH_HASH", None)
    monkeypatch.setattr(sync, "_root_auth_revision", lambda: "r1")
    monkeypatch.setattr(sync, "_root_auth_etag", lambda: "e1")
    monkeypatch.setattr(sync, "_download_object", lambda key, dest: dest.write_text(json.dumps(state.stored)))

    def upload(src, key, *, if_match):
        state.puts.append(key)
        return "e2"

    monkeypatch.setattr(sync, "_upload_object", upload)
    monkeypatch.setattr(sync, "_s3", lambda: types.SimpleNamespace(put_object=lambda **kw: state.puts.append(kw["Key"])))
    return state


@pytest.fixture
def hermes_auth(monkeypatch):
    """A stand-in for the fork's F6 surface (hermes_cli.auth_codex)."""
    registered = []

    class AuthError(RuntimeError):
        def __init__(self, msg, code, relogin):
            super().__init__(msg)
            self.code, self.relogin_required = code, relogin

    mod = types.ModuleType("hermes_cli.auth_codex")
    mod.set_refresh_delegate = registered.append
    mod._codex_err = lambda msg, code, relogin=False: AuthError(msg, code, relogin)
    pkg = types.ModuleType("hermes_cli")
    pkg.auth_codex = mod
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth_codex", mod)
    return registered


def test_marker_restore_then_vend_writes_local_auth(backend, s3, home):
    sync.restore_root_auth()
    assert sync._CODEX_BROKERED is True
    backend.answers["/"] = (200, {"tokens": {}, "model": MODEL, "expires_at": {"model": MODEL["expires_at"]}})

    assert turn.runtime_mcp_env("friday") == {}

    codex = json.loads((home / "auth.json").read_text())["providers"]["openai-codex"]
    assert codex["tokens"] == {"access_token": "AT-1", "refresh_token": "lightbulb-brokered", "id_token": "ID-1", "account_id": "acct"}
    assert codex["custody"] == "backend"
    assert s3.stored == MARKER and s3.puts == []  # the S3 marker is untouched


def test_save_root_auth_noop_when_brokered(backend, s3, home):
    sync.restore_root_auth()
    backend.answers["/"] = (200, {"tokens": {}, "model": MODEL})
    credentials.vend("friday")
    sync.save_root_auth()
    assert s3.puts == []


def test_sigterm_never_uploads_auth_when_brokered(backend, s3, home, monkeypatch):
    sync.restore_root_auth()
    backend.answers["/"] = (200, {"tokens": {}, "model": MODEL})
    credentials.vend("friday")
    monkeypatch.setattr(server.os, "_exit", lambda _code: None)
    server._save_on_sigterm(15, None)
    assert s3.puts == []


def test_legacy_marker_absent_unchanged(backend, s3, home):
    """No marker: today's restore, rotate in place, conditional write-back; no delegate, no renewer."""
    s3.stored = {"providers": {"openai-codex": {"tokens": {"access_token": "A", "refresh_token": "R"}}}}
    sync.restore_root_auth()
    assert sync._CODEX_BROKERED is False
    backend.answers["/"] = (200, {"tokens": {"stripe": "s"}})
    assert turn.runtime_mcp_env("friday") == {"MCP_STRIPE_ACCESS_TOKEN": "s"}
    assert json.loads((home / "auth.json").read_text()) == s3.stored  # the vend wrote nothing
    assert credentials.TurnRenewer.for_turn("friday") is None

    (home / "auth.json").write_text(json.dumps({"rotated": True}))  # Hermes refreshed in place
    sync.save_root_auth()
    assert s3.puts == ["auth.json", f"{sync.S3_PREFIX}/auth.revision"]


def _token_free_files(home, agent):
    d = home / "profiles" / agent
    (d / "home" / "quickbooks").mkdir(parents=True)
    (d / "skills" / "instagram").mkdir(parents=True)
    xurl = {"apps": {"lightbulb-x": {"client_id": "c", "client_secret": "", "default_user": "me",
            "oauth2_tokens": {"me": {"type": "oauth2", "oauth2": {"access_token": "", "refresh_token": "", "expiration_time": 0}}}}},
            "default_app": "lightbulb-x"}
    (d / "home" / ".xurl").write_text(yaml.safe_dump(xurl))
    (d / "home" / "quickbooks" / ".env").write_text("QUICKBOOKS_CLIENT_ID=c\nQUICKBOOKS_REALM_ID=1\n")
    (d / "skills" / "instagram" / "credentials.json").write_text(json.dumps({"access_token": "", "token_expiry": "", "custody": "backend"}))
    for rel in sync.ROTATING_CLI_CREDENTIALS:
        sync._CLI_PULLED.setdefault(agent, {})[rel] = ("etag", hashlib.sha256((d / rel).read_bytes()).hexdigest())
    return d


def test_cli_tokens_written_locally_never_uploaded(backend, s3, home, monkeypatch):
    friday, eng = _token_free_files(home, "friday"), _token_free_files(home, "eng")
    backend.answers["/"] = (200, {"tokens": {"x": "XT", "quickbooks": "QT", "instagram": "IT"},
                                  "expires_at": {"x": 1_900_000_100, "quickbooks": 1_900_000_200, "instagram": 1_900_000_300}})
    written_back = []
    monkeypatch.setattr(sync, "_write_back_rotation", lambda *a: written_back.append(a))

    vended = credentials.vend("friday")

    for d in (friday, eng):  # every local profile: xurl rides whichever $HOME first ran a shell
        oauth2 = yaml.safe_load((d / "home" / ".xurl").read_text())["apps"]["lightbulb-x"]["oauth2_tokens"]["me"]["oauth2"]
        assert oauth2 == {"access_token": "XT", "refresh_token": "", "expiration_time": 1_900_000_100}
        env = (d / "home" / "quickbooks" / ".env").read_text()
        assert "QUICKBOOKS_ACCESS_TOKEN=QT\n" in env and "QUICKBOOKS_ACCESS_TOKEN_EXPIRES_AT=1900000200\n" in env
        assert "QUICKBOOKS_CLIENT_ID=c" in env
        ig = json.loads((d / "skills" / "instagram" / "credentials.json").read_text())
        assert ig == {"access_token": "IT", "token_expiry": "2030-03-17T17:51:40Z", "custody": "backend"}
    assert vended.brokered == {"x", "quickbooks", "instagram"}

    sync.save_rotated_cli_credentials()
    assert written_back == [] and s3.puts == []


def test_cli_tokens_need_a_materialized_file(backend, home):
    backend.answers["/"] = (200, {"tokens": {"x": "XT"}})
    credentials.vend("friday")  # nothing materialized: nothing created
    assert not (home / "profiles" / "friday" / "home" / ".xurl").exists()


def test_delegate_posts_presented_hash_and_maps_errors(backend, s3, home, hermes_auth):
    backend.answers["/"] = (200, {"tokens": {}, "model": MODEL})
    credentials.vend("friday")
    newer = {**MODEL, "access_token": "AT-2", "expires_at": 1_900_000_900}
    backend.answers["/model"] = (200, {"model": newer, "refreshed": True})

    out = credentials.codex_refresh_delegate("AT-1", "lightbulb-brokered")

    assert out["access_token"] == "AT-2" and out["refresh_token"] == "lightbulb-brokered" and out["last_refresh"]
    assert backend.calls[-1] == ("/model", {"agent_id": "friday", "presented_sha256": hashlib.sha256(b"AT-1").hexdigest(), "force": True})
    assert json.loads((home / "auth.json").read_text())["providers"]["openai-codex"]["tokens"]["access_token"] == "AT-2"
    assert credentials._LAST["friday"].expires_at["model"] == 1_900_000_900

    for status, relogin in ((410, True), (409, False), (502, False), (TimeoutError("t"), False)):
        backend.answers["/model"] = (status, {"code": "x"})
        with pytest.raises(RuntimeError) as info:
            credentials.codex_refresh_delegate("AT-2", "lightbulb-brokered")
        assert info.value.relogin_required is relogin, status
    backend.answers["/model"] = (200, {"model": {}})  # a broken reply is not a dead grant
    with pytest.raises(RuntimeError) as info:
        credentials.codex_refresh_delegate("AT-2", "lightbulb-brokered")
    assert info.value.relogin_required is False


def test_delegate_not_registered_without_model(backend, s3, home, hermes_auth):
    backend.answers["/"] = (200, {"tokens": {"stripe": "s"}})
    credentials.vend("friday")
    assert hermes_auth == []  # inert: no model, nothing registered

    backend.answers["/"] = (200, {"tokens": {}, "model": MODEL})
    credentials.vend("friday")
    credentials.vend("friday")
    assert hermes_auth == [credentials.codex_refresh_delegate]  # once

    backend.answers["/"] = (200, {"tokens": {}})  # no model, but the local grant is still brokered: kept
    credentials.vend("friday")
    assert hermes_auth == [credentials.codex_refresh_delegate]
    # custody rolled back: restore pulls a real grant, then Hermes refreshes itself again
    s3.stored = {"providers": {"openai-codex": {"tokens": {"access_token": "A", "refresh_token": "R"}}}}
    sync.restore_root_auth()
    credentials.vend("friday")
    assert hermes_auth == [credentials.codex_refresh_delegate, None]


def test_settings_reads_a_marker_with_a_vended_model_as_connected(backend, s3, home, monkeypatch):
    sync.restore_root_auth()
    monkeypatch.setattr(sync, "restore_root_auth", lambda: None)
    backend.answers["/"] = (200, {"tokens": {}, "model": MODEL})
    assert server._provider_credential("friday", "openai-codex") == (True, "chatgpt-oauth")
    (home / "auth.json").write_text(json.dumps(MARKER))
    backend.answers["/"] = (200, {"tokens": {}, "unavailable": ["openai-codex"]})
    assert server._provider_credential("friday", "openai-codex") == (False, "chatgpt-oauth")


def test_broker_blip_keeps_the_delegate(backend, s3, home, hermes_auth):
    """#4784 review blocker: a vend without `model` because the broker is busy / OpenAI is down
    (openai-codex in `unavailable`) must NOT drop F6 while the marker holds -- else Hermes posts
    the placeholder refresh token to OpenAI and a working grant is quarantined."""
    sync.restore_root_auth()
    backend.answers["/"] = (200, {"tokens": {}, "model": MODEL})
    credentials.vend("friday")
    backend.answers["/"] = (200, {"tokens": {}, "unavailable": ["openai-codex"]})
    credentials.vend("friday")
    server._provider_credential("friday", "openai-codex")  # the settings check vends too
    assert hermes_auth == [credentials.codex_refresh_delegate]  # still registered

    # A real rollback: S3 holds a real grant again; the next restore + vend unregisters.
    s3.stored = {"providers": {"openai-codex": {"tokens": {"access_token": "A", "refresh_token": "R"}}}}
    sync._ROOT_AUTH_ETAG = "stale"
    sync.restore_root_auth()
    backend.answers["/"] = (200, {"tokens": {}})
    credentials.vend("friday")
    assert hermes_auth == [credentials.codex_refresh_delegate, None]


def test_model_404_not_brokered_is_transient(backend, s3, home, hermes_auth):
    backend.answers["/"] = (200, {"tokens": {}, "model": MODEL})
    credentials.vend("friday")
    backend.answers["/model"] = (404, {"detail": {"code": "not_brokered"}})
    with pytest.raises(RuntimeError) as info:
        credentials.codex_refresh_delegate("AT-1", "lightbulb-brokered")
    assert info.value.relogin_required is False


def test_cli_file_marked_before_the_write(backend, home, monkeypatch):
    """A concurrent save_rotated_cli_credentials must see the mark before the patched bytes land."""
    _token_free_files(home, "friday")
    seen = []
    real = credentials._atomic_write
    monkeypatch.setattr(credentials, "_atomic_write", lambda path, data, **kw: (seen.append(set(sync._BROKERED_CLI.get("friday", ()))), real(path, data, **kw)))
    backend.answers["/"] = (200, {"tokens": {"x": "XT"}})
    credentials.vend("friday")
    assert seen and all(f"{sync.PROFILE_HOME}/.xurl" in marks for marks in seen)


def test_vend_never_raises(backend, home, monkeypatch, caplog):
    backend.answers["/"] = (200, {"tokens": {}, "model": MODEL, "expires_at": {"model": float("inf")}})
    monkeypatch.setattr(credentials, "write_codex_auth", lambda _m: (_ for _ in ()).throw(OSError("disk full AT-1")))
    out = credentials.vend("friday")
    assert out is not None and out.expires_at == {}
    assert "AT-1" not in caplog.text
