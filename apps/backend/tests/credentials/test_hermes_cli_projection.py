"""credentials.hermes_cli — the CLI-materializer projection into every Hermes profile.

Supersedes ruling M6-R24's blanket skip: since #2749 a Hermes turn's shell runs INSIDE the
AgentCore microVM, so gh/aws/gog/xurl/instagram need their credential files in the profile.
"""

import json

import pytest

from core.services import hermes_profiles
from core.services.credentials import hermes_cli
from core.services.credentials.materializers import base


@pytest.fixture
def s3(monkeypatch):
    """An in-memory stand-in for the owner-state bucket, at hermes_profiles' own S3 seam."""
    store: dict[str, bytes] = {}
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "owner-state")
    monkeypatch.setattr(hermes_profiles, "_s3_put_bytes", lambda b, k, body: store.__setitem__(k, body))
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda b, k, body: store.__setitem__(k, body.encode()))
    monkeypatch.setattr(hermes_profiles, "_s3_delete", lambda b, k: store.pop(k, None))
    monkeypatch.setattr(hermes_profiles, "_s3_get_bytes", lambda b, k: store.get(k))
    monkeypatch.setattr(
        hermes_profiles, "_s3_list_keys", lambda b, prefix: sorted(k for k in store if k.startswith(prefix))
    )
    return store


@pytest.fixture
def roster(monkeypatch):
    async def _roster(owner_id):
        return ["ceo", "eng"]

    monkeypatch.setattr(hermes_cli, "_roster", _roster)


@pytest.mark.asyncio
async def test_github_connect_lands_hosts_yml_in_every_profile(s3, roster, monkeypatch):
    from core.services.credentials.materializers import gh_cli

    monkeypatch.setattr(gh_cli, "_fetch_login", lambda token: "octo")

    await hermes_cli.project("org_1", "github", "gh_cli", {"token": "gho_secret"})

    for agent in ("ceo", "eng"):
        body = s3[f"org_1/hermes/{agent}/.config/gh/hosts.yml"].decode()
        assert "gho_secret" in body and "octo" in body
        # .revision LAST, so the bridge never re-pulls before the bytes landed.
        assert f"org_1/hermes/{agent}/.revision" in s3
    # Nothing was written to the owner's EFS root.
    assert not any(k.startswith("/mnt") for k in s3)


@pytest.mark.asyncio
async def test_github_disconnect_removes_it_from_every_profile(s3, roster, monkeypatch):
    from core.services.credentials.materializers import gh_cli

    monkeypatch.setattr(gh_cli, "_fetch_login", lambda token: "octo")
    await hermes_cli.project("org_1", "github", "gh_cli", {"token": "gho_secret"})

    await hermes_cli.clear("org_1", "github", "gh_cli", {})

    assert not [k for k in s3 if "hosts.yml" in k]


@pytest.mark.asyncio
async def test_aws_config_resolves_the_helper_through_the_profile_home(s3, roster, monkeypatch):
    """On ECS the credential_process names an absolute in-container path. A Hermes profile dir
    differs per agent, so the only stable name is $HOME -- and botocore never expands, hence the
    explicit `sh -c`."""
    monkeypatch.setattr(hermes_profiles.settings, "PUBLIC_API_BASE", "https://api-dev.flickmeon.ai")

    await hermes_cli.project("org_1", "aws", "aws_cli", {"region": "us-east-1", "cli_token": "tok_x"})

    config = s3["org_1/hermes/ceo/.aws/config"].decode()
    assert "[profile lightbulb-connector]" in config
    assert "credential_process = sh -c 'exec /usr/bin/env node \"$HOME/.aws/lightbulb-creds.mjs\"'" in config
    assert "/home/node/.openclaw" not in config
    assert json.loads(s3["org_1/hermes/ceo/.aws/token"].decode())["token"] == "tok_x"
    assert b"credential_process" not in s3["org_1/hermes/ceo/.aws/lightbulb-creds.mjs"][:1]


@pytest.mark.asyncio
async def test_xurl_reproject_preserves_the_owners_other_apps(s3, roster, monkeypatch):
    """The scratch root is seeded from the live profile first, so the materializer's own
    read-modify-write keeps the entries it does not own -- the same guarantee the EFS rail has
    from operating on the real file."""
    import yaml

    from core.services.credentials.materializers import xurl_cli

    monkeypatch.setattr(xurl_cli, "_fetch_username", lambda token: "acct")
    existing = yaml.safe_dump({"apps": {"mine": {"client_id": "keep-me"}}, "default_app": "mine"})
    s3["org_1/hermes/ceo/.xurl"] = existing.encode()

    await hermes_cli.project(
        "org_1", "x", "xurl_cli", {"client_id": "cid", "access_token": "at", "refresh_token": "rt"}
    )

    store = yaml.safe_load(s3["org_1/hermes/ceo/.xurl"].decode())
    assert set(store["apps"]) == {"mine", "lightbulb-x"}
    assert store["apps"]["mine"]["client_id"] == "keep-me"


@pytest.mark.asyncio
async def test_gog_projection_carries_the_keyring_password_for_the_shell(s3, roster, monkeypatch):
    """The profile .env feeds Hermes's in-process secret scope, which never reaches a
    subprocess -- so the shared keyring password the container's gog must decrypt with rides
    cli-env.json, which the bridge merges into the terminal env overlay."""
    from core.services.credentials.materializers import gog_cli

    monkeypatch.setattr(hermes_cli.settings, "GOG_KEYRING_PASSWORD", "kp")
    monkeypatch.setattr(gog_cli, "_fetch_account_email", lambda token: "a@b.co")
    seen = []

    def _fake_run(argv, **kw):
        seen.append(argv[:3])
        # gog writes the keyring itself; stand in for the binary.
        root = base.owner_efs_root("org_1")
        (root / ".local" / "share" / "gogcli").mkdir(parents=True, exist_ok=True)
        (root / ".local" / "share" / "gogcli" / "keyring").write_bytes(b"\x00jwe-blob")
        return None

    monkeypatch.setattr(gog_cli.subprocess, "run", _fake_run)

    email = await hermes_cli.project(
        "org_1",
        "google",
        "gog_cli",
        {"access_token": "at", "client_id": "c", "client_secret": "s", "refresh_token": "r"},
    )

    assert email == "a@b.co"
    assert s3["org_1/hermes/eng/.local/share/gogcli/keyring"] == b"\x00jwe-blob"
    assert json.loads(s3["org_1/hermes/ceo/cli-env.json"].decode())["GOG_KEYRING_PASSWORD"] == "kp"


@pytest.mark.asyncio
async def test_instagram_projection_never_patches_openclaw_json(s3, roster, monkeypatch):
    """An AgentCore owner has no openclaw.json; patching EFS from the materializer would write
    into a dead config (and can raise, failing the connect)."""
    from core.services.credentials.materializers import instagram_cli

    monkeypatch.setattr(instagram_cli, "_fetch_identity", lambda token: {"user_id": "1", "username": "ig"})

    def _must_not_patch(*a, **k):
        raise AssertionError("openclaw.json must not be touched for an AgentCore owner")

    monkeypatch.setattr(instagram_cli.config_patcher, "set_openclaw_config_path", _must_not_patch)

    await hermes_cli.project("org_1", "instagram", "instagram_cli", {"access_token": "IGtok"})

    creds = json.loads(s3["org_1/hermes/ceo/skills/instagram/credentials.json"].decode())
    assert creds["access_token"] == "IGtok"
    assert "org_1/hermes/ceo/skills/instagram/SKILL.md" in s3


def test_every_projected_materializer_has_a_remover():
    from core.services.credentials.materializers import MATERIALIZER_REMOVERS, MATERIALIZERS

    for name in hermes_cli.OWNED_PATHS:
        assert name in MATERIALIZERS and name in MATERIALIZER_REMOVERS


@pytest.mark.asyncio
async def test_clear_is_a_no_op_for_an_unprojected_materializer(s3, roster):
    await hermes_cli.clear("org_1", "quickbooks", "qbo_dotenv", {})  # must not raise
    assert not s3


def test_projection_root_is_scoped_and_restored(tmp_path):
    assert not base.is_projecting()
    with base.materialize_into(tmp_path):
        assert base.is_projecting()
        assert base.owner_efs_root("org_1") == tmp_path
    assert not base.is_projecting()
    assert base.owner_efs_root("org_1") != tmp_path
