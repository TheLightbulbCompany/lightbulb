from pathlib import Path

import pytest
import yaml

from core.services.credentials.materializers import gh_cli


@pytest.fixture
def cred():
    # paste_token providers resolve to {"token": <PAT>}.
    return {"token": "ghp_testpat"}


class _FakeResp:
    def __init__(self, login):
        self._login = login

    def raise_for_status(self):
        pass

    def json(self):
        return {"login": self._login}


def _patch_efs(monkeypatch, tmp_path):
    # Materializers only run inside a projection (credentials/hermes_cli.py), so the root is
    # the ContextVar's, not a settings path. tmp_path/"org_1" keeps every path assertion below.
    monkeypatch.setattr(gh_cli, "materializer_root", lambda: tmp_path / "org_1")


def _hosts_path(tmp_path):
    return Path(tmp_path) / "org_1" / ".config" / "gh" / "hosts.yml"


def test_write_produces_hosts_yml_with_token(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)

    user_calls = []

    def _fake_get(url, headers=None):
        user_calls.append((url, headers))
        return _FakeResp("octocat")

    monkeypatch.setattr(gh_cli.httpx, "get", _fake_get)

    gh_cli.write("org_1", "github", cred)

    # The username is fetched from /user with the PAT as a Bearer token.
    assert user_calls == [("https://api.github.com/user", {"Authorization": "Bearer ghp_testpat"})]

    path = _hosts_path(tmp_path)
    assert path.exists()
    # 0600 — same secret posture as the rest of the materializer rail.
    assert oct(path.stat().st_mode)[-3:] == "600"

    data = yaml.safe_load(path.read_text())
    # gh reads the top-level host map directly (no `hosts:` wrapper on disk).
    host = data["github.com"]
    assert host["oauth_token"] == "ghp_testpat"
    assert host["user"] == "octocat"
    assert host["git_protocol"] == "https"
    # The per-user sub-map carries the same token so gh's multi-user lookup resolves.
    assert host["users"]["octocat"]["oauth_token"] == "ghp_testpat"


def test_write_propagates_user_lookup_failure(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)

    class _BadResp:
        def raise_for_status(self):
            raise RuntimeError("boom")

        def json(self):
            return {}

    monkeypatch.setattr(gh_cli.httpx, "get", lambda url, headers=None: _BadResp())

    with pytest.raises(RuntimeError):
        gh_cli.write("org_1", "github", cred)
    # A failed lookup leaves no partial hosts.yml behind.
    assert not _hosts_path(tmp_path).exists()


def test_remove_deletes_hosts_yml(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)
    monkeypatch.setattr(gh_cli.httpx, "get", lambda url, headers=None: _FakeResp("octocat"))

    gh_cli.write("org_1", "github", cred)
    path = _hosts_path(tmp_path)
    assert path.exists()

    gh_cli.remove("org_1", "github", cred)
    assert not path.exists()


def test_remove_is_idempotent_when_missing(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    # No hosts.yml written first — remove must not raise.
    gh_cli.remove("org_1", "github", {})
