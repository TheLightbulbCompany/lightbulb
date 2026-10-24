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
        body = s3[f"org_1/hermes/{agent}/home/.config/gh/hosts.yml"].decode()
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
    monkeypatch.setattr(hermes_profiles.settings, "PUBLIC_API_BASE", "https://api-dev.thelightbulb.company")

    await hermes_cli.project("org_1", "aws", "aws_cli", {"region": "us-east-1", "cli_token": "tok_x"})

    config = s3["org_1/hermes/ceo/home/.aws/config"].decode()
    # [default], not the NAMED profile ECS uses: nothing in the microVM shares a $HOME with this
    # file (the bridge process's own is "/"), so there is no in-process SDK for a default section
    # to capture -- and a default is what lets the shell work with no AWS_PROFILE to deliver.
    assert config.startswith("[default]\n")
    assert "lightbulb-connector" not in config
    assert "credential_process = sh -c 'exec /usr/bin/env node \"$HOME/.aws/lightbulb-creds.mjs\"'" in config
    assert "/home/node/.openclaw" not in config
    assert json.loads(s3["org_1/hermes/ceo/home/.aws/token"].decode())["token"] == "tok_x"
    assert b"credential_process" not in s3["org_1/hermes/ceo/home/.aws/lightbulb-creds.mjs"][:1]


@pytest.mark.asyncio
async def test_xurl_reproject_preserves_the_owners_other_apps(s3, roster, monkeypatch):
    """The scratch root is seeded from the live profile first, so the materializer's own
    read-modify-write keeps the entries it does not own -- the same guarantee the EFS rail has
    from operating on the real file."""
    import yaml

    from core.services.credentials.materializers import xurl_cli

    monkeypatch.setattr(xurl_cli, "_fetch_username", lambda token: "acct")
    existing = yaml.safe_dump({"apps": {"mine": {"client_id": "keep-me"}}, "default_app": "mine"})
    s3["org_1/hermes/ceo/home/.xurl"] = existing.encode()

    await hermes_cli.project(
        "org_1", "x", "xurl_cli", {"client_id": "cid", "access_token": "at", "refresh_token": "rt"}
    )

    store = yaml.safe_load(s3["org_1/hermes/ceo/home/.xurl"].decode())
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
        root = base.materializer_root()  # == <scratch>/home while projecting
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
    assert s3["org_1/hermes/eng/home/.local/share/gogcli/keyring"] == b"\x00jwe-blob"
    assert json.loads(s3["org_1/hermes/ceo/cli-env.json"].decode())["GOG_KEYRING_PASSWORD"] == "kp"


@pytest.mark.asyncio
async def test_instagram_projection_lands_only_the_skill_dir(s3, roster, monkeypatch):
    """Landing the skill dir IS the install -- Hermes finds a skill by scanning the profile.
    The materializer's old openclaw.json ``skills.entries`` switch went with the ECS runtime,
    so nothing but the projected tree may be written."""
    from core.services.credentials.materializers import instagram_cli

    monkeypatch.setattr(instagram_cli, "_fetch_identity", lambda token: {"user_id": "1", "username": "ig"})

    await hermes_cli.project("org_1", "instagram", "instagram_cli", {"access_token": "IGtok"})

    creds = json.loads(s3["org_1/hermes/ceo/skills/instagram/credentials.json"].decode())
    assert creds["access_token"] == "IGtok"
    assert "org_1/hermes/ceo/skills/instagram/SKILL.md" in s3
    # Only the skill dir (plus the per-profile .revision the write bumps) -- no config object.
    assert {k.split("/", 3)[3] for k in s3} == {
        "skills/instagram/SKILL.md",
        "skills/instagram/credentials.json",
        "skills/instagram/token.sh",
        ".revision",
    }


@pytest.mark.asyncio
async def test_vercel_skill_projection_lands_skill_and_token_env(s3, roster):
    """The Vercel skill is a Hermes profile skill; its token travels through the
    allowlisted CLI environment rather than an agent-editable config field."""
    await hermes_cli.project("org_1", "vercel_cli", "skill_cli", {"token": "vrc_secret"})

    body = s3["org_1/hermes/ceo/skills/vercel/SKILL.md"].decode()
    assert "vercel@54.18.2" in body
    assert json.loads(s3["org_1/hermes/ceo/cli-env.json"].decode()) == {"VERCEL_TOKEN": "vrc_secret"}


@pytest.mark.asyncio
async def test_quickbooks_projection_lands_profile_local_store(s3, roster, monkeypatch):
    from unittest.mock import AsyncMock

    from core.services.credentials.materializers import qbo_dotenv

    monkeypatch.setattr(
        qbo_dotenv.platform_oauth_app_store,
        "get",
        AsyncMock(return_value={"client_id": "cid", "client_secret": "cs"}),
    )
    await hermes_cli.project(
        "org_1",
        "quickbooks",
        "qbo_dotenv",
        {"refresh_token": "rt", "realm_id": "123"},
    )

    body = s3["org_1/hermes/ceo/home/quickbooks/.env"].decode()
    assert "QUICKBOOKS_REFRESH_TOKEN=rt" in body
    assert "QUICKBOOKS_REALM_ID=123" in body


def test_every_projected_materializer_has_a_remover():
    from core.services.credentials.materializers import MATERIALIZER_REMOVERS, MATERIALIZERS

    for name in hermes_cli.OWNED_PATHS:
        assert name in MATERIALIZERS and name in MATERIALIZER_REMOVERS


@pytest.mark.asyncio
async def test_clear_is_idempotent_for_quickbooks(s3, roster):
    await hermes_cli.clear("org_1", "quickbooks", "qbo_dotenv", {})
    assert "org_1/hermes/ceo/.revision" in s3


def test_projection_root_is_scoped_and_restored(tmp_path):
    """Outside a projection there is no root at all -- ``materializer_root`` raises rather than
    falling back to a mount path, so a materializer dispatched by mistake cannot write a
    credential nothing reads."""
    with pytest.raises(RuntimeError):
        base.materializer_root()
    with base.materialize_into(tmp_path):
        assert base.materializer_root() == tmp_path
    with pytest.raises(RuntimeError):
        base.materializer_root()


@pytest.mark.asyncio
async def test_a_full_google_disconnect_leaves_no_cli_env_json(s3, roster, monkeypatch):
    """Review B2. ``cli-env.json`` is written only on the write leg and is not under any
    OWNED_PATHS, so it was never in the delete set -- the FLEET-SHARED gog keyring password
    stayed in every profile of every owner who had ever connected Google, permanently."""
    from core.services.credentials.materializers import gog_cli

    monkeypatch.setattr(hermes_cli.settings, "GOG_KEYRING_PASSWORD", "kp")
    monkeypatch.setattr(gog_cli, "_fetch_account_email", lambda token: "a@b.co")
    monkeypatch.setattr(
        gog_cli,
        "subprocess",
        type(
            "S",
            (),
            {
                "run": staticmethod(
                    lambda argv, **kw: (base.materializer_root() / ".local" / "share" / "gogcli").mkdir(
                        parents=True, exist_ok=True
                    )
                )
            },
        ),
    )
    await hermes_cli.project(
        "org_1",
        "google",
        "gog_cli",
        {"access_token": "at", "client_id": "c", "client_secret": "s", "refresh_token": "r"},
    )
    assert "org_1/hermes/ceo/cli-env.json" in s3

    await hermes_cli.clear("org_1", "google", "gog_cli", {"disconnect_all": True, "accounts": []})

    assert not [k for k in s3 if "cli-env.json" in k]
    assert not [k for k in s3 if "gogcli" in k]


@pytest.mark.asyncio
async def test_an_empty_roster_is_a_no_op_not_an_indexerror(s3, monkeypatch):
    """Review N2. ``agent_ids[0]`` raised IndexError, which ``project`` does not catch, so an
    owner with no roster surfaced the connect as a generic ``apply_failed``."""

    async def _none(owner_id):
        return []

    monkeypatch.setattr(hermes_cli, "_roster", _none)
    assert await hermes_cli.project("org_1", "github", "gh_cli", {"token": "t"}) is None
    assert not s3


def test_profile_paths_rebase_dotfiles_onto_home_but_not_the_skill_dir():
    """$HOME is ``<profile>/home`` (TERMINAL_HOME_MODE=profile), so a dotfile tree belongs
    there. Instagram's output is a Hermes SKILLS dir -- profile-relative, not a home dotfile."""
    assert hermes_cli.profile_paths("aws_cli") == ("home/.aws/",)
    assert hermes_cli.profile_paths("gog_cli") == ("home/.config/gogcli/", "home/.local/share/gogcli/")
    assert hermes_cli.profile_paths("xurl_cli") == ("home/.xurl",)
    assert hermes_cli.profile_paths("instagram_cli") == ("skills/instagram/",)


@pytest.mark.asyncio
async def test_a_file_prefix_does_not_seed_from_a_longer_sibling(s3, roster, monkeypatch):
    """Review N4. ``home/.xurl`` names one FILE; listing by raw string prefix also matched
    ``home/.xurlfoo``, which would have been seeded into the scratch root as if it were ours."""
    import yaml

    from core.services.credentials.materializers import xurl_cli

    monkeypatch.setattr(xurl_cli, "_fetch_username", lambda token: "acct")
    s3["org_1/hermes/ceo/home/.xurlfoo"] = b"not ours"

    await hermes_cli.project(
        "org_1", "x", "xurl_cli", {"client_id": "cid", "access_token": "at", "refresh_token": "rt"}
    )

    assert s3["org_1/hermes/ceo/home/.xurlfoo"] == b"not ours"
    assert set(yaml.safe_load(s3["org_1/hermes/ceo/home/.xurl"].decode())["apps"]) == {"lightbulb-x"}


# ---- set_profile_cli_env: the per-AGENT leg of cli-env.json ------------------------------------
@pytest.mark.asyncio
async def test_set_profile_cli_env_merges_creates_and_bumps_the_revision(s3):
    """Read-modify-write on ONE agent: the projection's own keys survive, the sibling profile is
    untouched (this value is per agent), and .revision moves so the bridge re-pulls it."""
    s3["org_1/hermes/ceo/cli-env.json"] = json.dumps({"GOG_KEYRING_PASSWORD": "kp"}).encode()

    await hermes_cli.set_profile_cli_env("org_1", "ceo", {"GOG_ACCOUNT": "a@b.co"})

    assert json.loads(s3["org_1/hermes/ceo/cli-env.json"].decode()) == {
        "GOG_KEYRING_PASSWORD": "kp",
        "GOG_ACCOUNT": "a@b.co",
    }
    assert "org_1/hermes/ceo/.revision" in s3
    assert "org_1/hermes/eng/cli-env.json" not in s3


@pytest.mark.asyncio
async def test_set_profile_cli_env_creates_the_file_and_none_deletes_the_key(s3):
    await hermes_cli.set_profile_cli_env("org_1", "ceo", {"GOG_ACCOUNT": "a@b.co"})
    assert json.loads(s3["org_1/hermes/ceo/cli-env.json"].decode()) == {"GOG_ACCOUNT": "a@b.co"}

    await hermes_cli.set_profile_cli_env("org_1", "ceo", {"GOG_ACCOUNT": None})

    assert "org_1/hermes/ceo/cli-env.json" not in s3  # the last key went: no empty file left


@pytest.mark.asyncio
async def test_a_google_connect_and_disconnect_keep_another_providers_cli_env_keys(s3, roster, monkeypatch):
    """cli-env.json is shared by every CLI provider. Writing it whole from one provider's keys
    wiped the others' (reconnecting Google deleted VERCEL_TOKEN); it is merged key-by-key."""
    import json

    from core.services.credentials.materializers import gog_cli

    for agent in ("ceo", "eng"):
        s3[f"org_1/hermes/{agent}/cli-env.json"] = json.dumps({"VERCEL_TOKEN": "vt"}).encode()
    monkeypatch.setattr(hermes_cli.settings, "GOG_KEYRING_PASSWORD", "kp")
    monkeypatch.setattr(gog_cli, "_fetch_account_email", lambda token: "a@b.co")
    monkeypatch.setattr(
        gog_cli,
        "subprocess",
        type(
            "S",
            (),
            {
                "run": staticmethod(
                    lambda argv, **kw: (base.materializer_root() / ".local" / "share" / "gogcli").mkdir(
                        parents=True, exist_ok=True
                    )
                )
            },
        ),
    )

    await hermes_cli.project(
        "org_1",
        "google",
        "gog_cli",
        {"access_token": "at", "client_id": "c", "client_secret": "s", "refresh_token": "r"},
    )
    for agent in ("ceo", "eng"):
        env = json.loads(s3[f"org_1/hermes/{agent}/cli-env.json"])
        assert env["VERCEL_TOKEN"] == "vt" and env["GOG_KEYRING_PASSWORD"] == "kp"

    await hermes_cli.clear("org_1", "google", "gog_cli", {"disconnect_all": True, "accounts": []})
    for agent in ("ceo", "eng"):
        assert json.loads(s3[f"org_1/hermes/{agent}/cli-env.json"]) == {"VERCEL_TOKEN": "vt"}


@pytest.mark.asyncio
async def test_an_agent_created_after_a_connect_gets_the_credentials(s3, roster, monkeypatch):
    from core.services.credentials.materializers import gh_cli

    monkeypatch.setattr(gh_cli, "_fetch_login", lambda token: "octo")
    await hermes_cli.project("org_1", "github", "gh_cli", {"token": "gho_secret"})
    s3["org_1/hermes/ceo/cli-env.json"] = json.dumps({"VERCEL_TOKEN": "v", "GOG_ACCOUNT": "ceo@x.co"}).encode()

    await hermes_cli.seed_new_profile("org_1", "hired")

    assert b"gho_secret" in s3["org_1/hermes/hired/home/.config/gh/hosts.yml"]
    # owner-level shell keys are inherited, the per-agent Google binding is not
    assert json.loads(s3["org_1/hermes/hired/cli-env.json"]) == {"VERCEL_TOKEN": "v"}
    assert "org_1/hermes/hired/.revision" in s3


@pytest.mark.asyncio
async def test_second_account_connect_keeps_the_first_when_the_first_profile_is_empty(s3, roster, monkeypatch):
    """agent_ids[0] ('ceo') holds nothing; the seed must come from the profile that does ('eng'),
    or the earlier xurl app is wiped."""
    import yaml

    from core.services.credentials.materializers import xurl_cli

    monkeypatch.setattr(xurl_cli, "_fetch_username", lambda token: "acct")
    s3["org_1/hermes/eng/home/.xurl"] = yaml.safe_dump({"apps": {"mine": {"client_id": "keep-me"}}}).encode()

    await hermes_cli.project(
        "org_1", "x", "xurl_cli", {"client_id": "cid", "access_token": "at", "refresh_token": "rt"}
    )

    for agent in ("ceo", "eng"):
        store = yaml.safe_load(s3[f"org_1/hermes/{agent}/home/.xurl"].decode())
        assert set(store["apps"]) == {"mine", "lightbulb-x"}
