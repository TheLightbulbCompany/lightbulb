"""The CLI-connector credentials the backend projects into a profile
(backend ``core/services/credentials/hermes_cli.py``): how the shell finds them, and the
backend-owned "pulled by name, never pushed" rule they live under.

Fix round 1 (review B1/B3): the earlier version of this file asserted an ``env_overlay``
carrying ``HOME``/``XDG_*``/``GH_CONFIG_DIR``/``AWS_*`` through a STUBBED
``tools.terminal_scope``. The real module keeps only ``TERMINAL_``-prefixed keys
(``terminal_scope.py:136-137``), so those assertions were green against a contract the runtime
contradicts. Nothing here asserts a key the filter drops any more: the surviving mechanism is
``TERMINAL_HOME_MODE`` + the profile's own ``home/``, and the per-CLI path variables move to
``terminal.env_passthrough`` once #2754 lands that rail.
"""
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from bridge import sync, turn


# ---- how the shell finds them ----------------------------------------------------------------
def test_the_overlay_carries_only_keys_the_filter_keeps(home, monkeypatch):
    """``TERMINAL_HOME_MODE: profile`` is the whole discovery mechanism: it points $HOME at
    ``<profile>/home``, where the backend projects the dotfile trees, so each CLI's OWN default
    lookup finds them. Everything else the filter would drop is deliberately absent."""
    seen = {}
    native = ModuleType("tools.terminal_scope")
    native.install_profile_terminal_scope = lambda profile, *, env_overlay: seen.update(
        profile=profile, **env_overlay
    ) or "tok"
    native.reset_terminal_scope = lambda token: seen.update(reset=token)
    monkeypatch.setitem(sys.modules, "tools.terminal_scope", native)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda agent_id: None)
    monkeypatch.setattr(
        turn,
        "_construct_agent",
        lambda *a: SimpleNamespace(run_conversation=lambda **kw: {"api_calls": 1, "final_response": "ok"}),
    )

    from tests import factories as f

    turn._run_hermes("ceo", f.ingress(), None)

    profile = home / "profiles" / "ceo"
    assert seen["profile"] == profile
    assert seen["TERMINAL_CWD"] == str(profile)
    assert seen["TERMINAL_HOME_MODE"] == "profile"
    assert seen["reset"] == "tok"
    # tools/terminal_scope.py keeps `k.startswith("TERMINAL_")` and drops the rest -- an overlay
    # key that is not TERMINAL_-prefixed is silently dead, so none may be added here.
    assert all(k.startswith("TERMINAL_") for k in seen if k not in ("profile", "reset"))


def test_the_profile_home_exists_before_any_turn(home):
    """``hermes_constants.get_subprocess_home``'s ``auto`` mode falls back to ``/`` when
    ``{HERMES_HOME}/home`` is missing, which is where $HOME actually pointed before this change.
    ``restore_profile`` creates it on both of its paths."""
    sync.restore_profile("ceo")  # S3_BUCKET unset in tests -> the early-return path
    assert (home / "profiles" / "ceo" / sync.PROFILE_HOME).is_dir()


def test_cli_env_json_carries_the_per_agent_google_account(home):
    """``GOG_ACCOUNT`` is written per agent by the backend's google_identity seeder; it only
    reaches the shell if it is in ``CLI_ENV_FILE_NAMES``, the allow-list this file is read
    through (the agent can write ``cli-env.json`` too, so an unfiltered read is an injection)."""
    profile = home / "profiles" / "ceo"
    profile.mkdir(parents=True)
    (profile / "cli-env.json").write_text('{"GOG_ACCOUNT": "alice@gmail.com", "NOT_ALLOWED": "x"}')

    env = sync.cli_credential_env("ceo")

    assert env["GOG_ACCOUNT"] == "alice@gmail.com"
    assert "NOT_ALLOWED" not in env


def test_cli_env_json_carries_the_vercel_skill_token(home):
    profile = home / "profiles" / "ceo"
    profile.mkdir(parents=True)
    (profile / "cli-env.json").write_text('{"VERCEL_TOKEN": "vrc_secret", "NOPE": "x"}')

    env = sync.cli_credential_env("ceo")
    assert env["VERCEL_TOKEN"] == "vrc_secret"
    assert "NOPE" not in env


def test_aws_child_without_connector_has_no_platform_credential_fallback():
    env = turn._fence_aws_child_env(
        {
            "AWS_ACCESS_KEY_ID": "platform",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://platform",
            "AWS_CONFIG_FILE": "/platform/config",
        },
        None,
    )
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_CONTAINER_CREDENTIALS_FULL_URI" not in env
    assert env["AWS_CONFIG_FILE"] == "/dev/null"
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert env["AWS_PROFILE"] == "default"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"


def test_ensure_profile_home_is_idempotent(home):
    first = sync.ensure_profile_home("ceo")
    (first / ".gitconfig").write_text("keep me")
    assert sync.ensure_profile_home("ceo") == first
    assert (first / ".gitconfig").read_text() == "keep me"


# ---- backend-owned: pulled by name, never pushed ---------------------------------------------
@pytest.mark.parametrize(
    "rel",
    [
        "home/.aws/config",
        "home/.aws/lightbulb-creds.mjs",
        "home/.aws/cache.json",
        "home/.config/gh/hosts.yml",
        "home/.config/gogcli/client.json",
        "home/.local/share/gogcli/keyring",
        "home/.xurl",
        "skills/instagram/credentials.json",
        "cli-env.json",
    ],
)
def test_a_projected_credential_is_never_pushed_to_s3(rel):
    """The backend is the only writer of these paths. An upload from a turn could only revert
    a connect/disconnect the owner just made -- and would let one turn write a credential every
    later turn inherits."""
    assert sync.is_excluded(rel)


@pytest.mark.parametrize(
    "rel",
    [
        "home/.cache/pip/wheel",
        "home/.npm/_cacache/index",
        "home/.ssh/id_ed25519",
        "home/.gnupg/pubring.kbx",
        "home/.gitconfig",
    ],
)
def test_home_caches_and_agent_written_credentials_are_never_pushed(rel):
    """Putting $HOME inside the profile puts every tool's dotfiles in the dir save_profile
    walks. These are the ones that must not ride along."""
    assert sync.is_excluded(rel)


def test_ordinary_profile_and_home_files_are_still_pushed():
    for rel in ("memories/USER.md", "notes.md", "sessions/main.db", "home/work/README.md"):
        assert not sync.is_excluded(rel)


def test_pull_by_name_overwrites_and_prunes(home, monkeypatch):
    """EXCLUDE_GLOBS takes these paths out of the recency walk too, so without an explicit pull
    a mid-session connect would never reach a warm microVM. Unlike mcp-tokens this OVERWRITES
    (the backend is the sole writer) and DELETES what the backend dropped -- a disconnected
    connector must not keep working for the life of the warm profile."""
    profile = home / "profiles" / "ceo"
    (profile / "home" / ".config" / "gh").mkdir(parents=True)
    (profile / "home" / ".config" / "gh" / "hosts.yml").write_text("stale")
    (profile / "home" / ".xurl").write_text("disconnected-but-still-here")
    (profile / "cli-env.json").write_text('{"GOG_KEYRING_PASSWORD": "old"}')

    objects = {f"{sync.S3_PREFIX}/ceo/home/.config/gh/hosts.yml": "fresh"}

    class _S3:
        def get_paginator(self, _):
            return SimpleNamespace(
                paginate=lambda Bucket, Prefix: [{"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}]
            )

        def download_file(self, bucket, key, dest):
            Path(dest).write_text(objects[key])

    monkeypatch.setattr(sync, "_s3", _S3)
    monkeypatch.setattr(sync, "S3_BUCKET", "owner-state")

    sync._pull_cli_credentials("ceo")

    assert (profile / "home" / ".config" / "gh" / "hosts.yml").read_text() == "fresh"
    assert not (profile / "home" / ".xurl").exists()
    assert not (profile / "cli-env.json").exists()  # B2's teardown reaches the microVM too


def test_a_file_prefix_does_not_match_a_longer_sibling(home, monkeypatch):
    """``home/.xurl`` names one FILE; it must not pull or prune ``home/.xurlfoo``."""
    profile = home / "profiles" / "ceo"
    (profile / "home").mkdir(parents=True)
    (profile / "home" / ".xurlfoo").write_text("not ours")
    objects = {f"{sync.S3_PREFIX}/ceo/home/.xurlfoo": "also not ours"}

    class _S3:
        def get_paginator(self, _):
            return SimpleNamespace(
                paginate=lambda Bucket, Prefix: [{"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}]
            )

        def download_file(self, bucket, key, dest):
            Path(dest).write_text(objects[key])

    monkeypatch.setattr(sync, "_s3", _S3)
    monkeypatch.setattr(sync, "S3_BUCKET", "owner-state")

    sync._pull_cli_credentials("ceo")

    assert (profile / "home" / ".xurlfoo").read_text() == "not ours"


def test_pull_by_name_failure_does_not_raise(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)

    def _boom():
        raise RuntimeError("s3 down")

    monkeypatch.setattr(sync, "_s3", _boom)
    monkeypatch.setattr(sync, "S3_BUCKET", "owner-state")
    sync._pull_cli_credentials("ceo")  # must not raise -- a blip must not tear down a warm profile


class _RotatingS3:
    """Keys -> (bytes, etag); PutObject honours IfMatch the way S3 does (412 on a mismatch)."""

    def __init__(self, objects, fail_put=()):
        self.objects = objects
        self.fail_put = set(fail_put)
        self.puts = []
        self._n = 0

    def get_paginator(self, _):
        def paginate(Bucket, Prefix, Delimiter=None):
            keys = [k for k in self.objects if k.startswith(Prefix)]
            if Delimiter:
                subs = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter for k in keys})
                return [{"CommonPrefixes": [{"Prefix": s} for s in subs]}]
            return [{"Contents": [{"Key": k, "ETag": self.objects[k][1]} for k in keys]}]
        return SimpleNamespace(paginate=paginate)

    def download_file(self, bucket, key, dest):
        Path(dest).write_bytes(self.objects[key][0])

    def get_object(self, Bucket, Key):
        body, etag = self.objects[Key]
        return {"Body": SimpleNamespace(read=lambda: body), "ETag": etag}

    def put_object(self, *, Bucket, Key, Body, IfMatch):
        self.puts.append((Key, IfMatch))
        if Key in self.fail_put or self.objects[Key][1] != IfMatch:
            raise RuntimeError("PreconditionFailed")
        self._n += 1
        self.objects[Key] = (Body, f'"new-{self._n}"')
        return {"ETag": self.objects[Key][1]}


def test_a_rotated_xurl_is_written_back_to_every_roster_profile(home, monkeypatch):
    """X refresh tokens are single-use: xurl rewrites ~/.xurl on refresh, and a pull that
    restores the pre-refresh copy hands it a spent token (NoAuthMethod ~2 h after connect)."""
    key = lambda agent: f"{sync.S3_PREFIX}/{agent}/home/.xurl"
    roster = ("ceo", "engineering", "growth", "finance")
    s3 = _RotatingS3({key(a): (b"old-token", f'"etag-{a}"') for a in roster}, fail_put={key("growth")})
    monkeypatch.setattr(sync, "_s3", lambda: s3)
    monkeypatch.setattr(sync, "S3_BUCKET", "owner-state")

    sync._pull_cli_credentials("ceo")  # finance/growth/engineering are never restored locally
    xurl = home / "profiles" / "ceo" / "home" / ".xurl"
    xurl.write_bytes(b"rotated-token")  # the in-microVM refresh

    sync.save_rotated_cli_credentials()  # growth's 412 is skipped, not raised

    for agent in ("ceo", "engineering", "finance"):
        assert s3.objects[key(agent)][0] == b"rotated-token"
        assert (key(agent), f'"etag-{agent}"') in s3.puts  # conditional on the ETag it replaced
    assert s3.objects[key("growth")][0] == b"old-token"

    sync._pull_cli_credentials("ceo")  # a later .revision bump must not resurrect the spent token
    assert xurl.read_bytes() == b"rotated-token"

    s3.objects[key("ceo")] = (b"reconnected", '"backend"')  # an owner reconnect still wins
    sync._pull_cli_credentials("ceo")
    assert xurl.read_bytes() == b"reconnected"


def test_a_backend_write_mid_turn_is_never_overwritten(home, monkeypatch):
    key = f"{sync.S3_PREFIX}/ceo/home/.xurl"
    s3 = _RotatingS3({key: (b"old-token", '"etag-1"')})
    monkeypatch.setattr(sync, "_s3", lambda: s3)
    monkeypatch.setattr(sync, "S3_BUCKET", "owner-state")
    sync._pull_cli_credentials("ceo")
    (home / "profiles" / "ceo" / "home" / ".xurl").write_bytes(b"rotated-token")
    s3.objects[key] = (b"reconnected", '"etag-2"')

    sync.save_rotated_cli_credentials()

    assert s3.objects[key][0] == b"reconnected" and not s3.puts


def test_a_quickbooks_rotation_in_any_agents_copy_propagates(home, monkeypatch):
    """Intuit rotates on every refresh, and each agent's MCP server writes its OWN profile's
    home/quickbooks/.env -- so every profile is scanned, and the newest rotation wins."""
    import os

    rel = "home/quickbooks/.env"
    key = lambda agent: f"{sync.S3_PREFIX}/{agent}/{rel}"
    roster = ("ceo", "engineering", "finance")
    s3 = _RotatingS3({key(a): (b"REFRESH=old", f'"etag-{a}"') for a in roster})
    monkeypatch.setattr(sync, "_s3", lambda: s3)
    monkeypatch.setattr(sync, "S3_BUCKET", "owner-state")
    for agent in ("ceo", "engineering"):
        sync._pull_cli_credentials(agent)
    older = home / "profiles" / "ceo" / rel
    newer = home / "profiles" / "engineering" / rel
    older.write_bytes(b"REFRESH=ceo-rotated")
    newer.write_bytes(b"REFRESH=eng-rotated")
    os.utime(older, (1, 1))

    sync.save_rotated_cli_credentials()

    assert all(s3.objects[key(a)][0] == b"REFRESH=eng-rotated" for a in roster)
    for agent in ("ceo", "engineering"):
        sync._pull_cli_credentials(agent)  # a re-pull brings the winner, never the old token
        assert (home / "profiles" / agent / rel).read_bytes() == b"REFRESH=eng-rotated"
