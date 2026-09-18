"""The CLI-connector credentials the backend projects into a profile
(backend ``core/services/credentials/hermes_cli.py``): how the shell finds them, and the
backend-owned "pulled by name, never pushed" rule they live under."""
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from bridge import sync, turn


# ---- how the shell finds them ----------------------------------------------------------------
def test_overlay_points_every_cli_at_the_profile(home):
    profile = home / "profiles" / "ceo"
    profile.mkdir(parents=True)
    env = turn._terminal_env(profile)

    assert env["TERMINAL_CWD"] == str(profile)
    # xurl resolves $HOME/.xurl and has no config-location variable of its own.
    assert env["HOME"] == str(profile)
    # gog splits OAuth client config from its file keyring and needs both.
    assert env["XDG_CONFIG_HOME"] == str(profile / ".config")
    assert env["XDG_DATA_HOME"] == str(profile / ".local" / "share")
    assert env["GH_CONFIG_DIR"] == str(profile / ".config" / "gh")
    assert env["AWS_CONFIG_FILE"] == str(profile / ".aws" / "config")
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == str(profile / ".aws" / "credentials")


def test_aws_profile_is_pinned_only_once_aws_is_connected(home):
    """The projected profile is NAMED, so `aws` needs AWS_PROFILE -- but pinning a profile that
    does not exist would break every other credential source for an owner who never connected
    AWS. Same "only when the config is there" rule the baked aws-exec-profile plugin uses."""
    profile = home / "profiles" / "ceo"
    profile.mkdir(parents=True)
    assert "AWS_PROFILE" not in turn._terminal_env(profile)

    (profile / ".aws").mkdir()
    (profile / ".aws" / "config").write_text("[profile lightbulb-connector]\n")
    assert turn._terminal_env(profile)["AWS_PROFILE"] == "lightbulb-connector"


def test_cli_env_json_reaches_the_shell_and_a_broken_one_does_not_fail_the_turn(home):
    """gog's shared keyring password cannot ride the profile .env -- that feeds Hermes's
    in-process secret scope, which never touches a subprocess's environment."""
    profile = home / "profiles" / "ceo"
    profile.mkdir(parents=True)
    (profile / "cli-env.json").write_text(json.dumps({"GOG_KEYRING_PASSWORD": "kp"}))
    assert turn._terminal_env(profile)["GOG_KEYRING_PASSWORD"] == "kp"

    (profile / "cli-env.json").write_text("{not json")
    assert "GOG_KEYRING_PASSWORD" not in turn._terminal_env(profile)


def test_terminal_scope_installs_the_overlay(home, monkeypatch):
    seen = {}
    native = ModuleType("tools.terminal_scope")
    native.install_profile_terminal_scope = lambda profile, *, env_overlay: seen.update(env_overlay) or "tok"
    native.reset_terminal_scope = lambda token: seen.update(reset=token)
    monkeypatch.setitem(sys.modules, "tools.terminal_scope", native)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda agent_id: None)
    monkeypatch.setattr(turn, "_construct_agent", lambda *a: SimpleNamespace(
        run_conversation=lambda **kw: {"api_calls": 1, "final_response": "ok"}))

    from tests import factories as f

    turn._run_hermes("ceo", f.ingress(), None)
    assert seen["GH_CONFIG_DIR"] == str(home / "profiles" / "ceo" / ".config" / "gh")
    assert seen["reset"] == "tok"


# ---- backend-owned: pulled by name, never pushed ---------------------------------------------
@pytest.mark.parametrize(
    "rel",
    [
        ".aws/config",
        ".aws/lightbulb-creds.mjs",
        ".aws/cache.json",
        ".config/gh/hosts.yml",
        ".config/gogcli/client.json",
        ".local/share/gogcli/keyring",
        "skills/instagram/credentials.json",
        ".xurl",
        "cli-env.json",
    ],
)
def test_a_projected_credential_is_never_pushed_to_s3(rel):
    """The backend is the only writer of these paths. An upload from a turn could only revert
    a connect/disconnect the owner just made -- and would let one turn write a credential every
    later turn inherits."""
    assert sync.is_excluded(rel)


def test_ordinary_profile_files_are_still_pushed():
    for rel in ("memories/USER.md", "notes.md", ".config/other/thing.yml", "sessions/main.db"):
        assert not sync.is_excluded(rel)


def test_pull_by_name_overwrites_and_prunes(home, monkeypatch):
    """EXCLUDE_GLOBS takes these paths out of the recency walk too, so without an explicit pull
    a mid-session connect would never reach a warm microVM. Unlike mcp-tokens this OVERWRITES
    (the backend is the sole writer) and DELETES what the backend dropped -- a disconnected
    connector must not keep working for the life of the warm profile."""
    profile = home / "profiles" / "ceo"
    (profile / ".config" / "gh").mkdir(parents=True)
    (profile / ".config" / "gh" / "hosts.yml").write_text("stale")
    (profile / ".xurl").write_text("disconnected-but-still-here")

    objects = {f"{sync.S3_PREFIX}/ceo/.config/gh/hosts.yml": "fresh"}

    class _S3:
        def get_paginator(self, _):
            return SimpleNamespace(
                paginate=lambda Bucket, Prefix: [
                    {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}
                ]
            )

        def download_file(self, bucket, key, dest):
            Path(dest).write_text(objects[key])

    monkeypatch.setattr(sync, "_s3", _S3)
    monkeypatch.setattr(sync, "S3_BUCKET", "owner-state")

    sync._pull_cli_credentials("ceo")

    assert (profile / ".config" / "gh" / "hosts.yml").read_text() == "fresh"
    assert not (profile / ".xurl").exists()


def test_pull_by_name_failure_does_not_raise(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)

    def _boom():
        raise RuntimeError("s3 down")

    monkeypatch.setattr(sync, "_s3", _boom)
    monkeypatch.setattr(sync, "S3_BUCKET", "owner-state")
    sync._pull_cli_credentials("ceo")  # must not raise -- a blip must not tear down a warm profile
