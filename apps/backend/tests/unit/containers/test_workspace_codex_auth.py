"""Tests for the EFS pre-staging of OpenClaw's container-global ``oauth.json``.

OpenClaw 6.6 reads per-agent auth from a WAL SQLite store and IGNORES the
legacy per-agent ``auth-profiles.json``. The supported external seed is the
container-global ``<stateDir>/credentials/oauth.json`` (EFS
``<owner>/credentials/oauth.json``), which the in-container process imports
into each agent's SQLite store on first load (building profile
``openai:default``) under a cross-agent ``(provider, profileId)`` refresh lock
that serializes token rotation. So we write ONE flat file, not a per-agent
fan-out (the fan-out broke under refresh-token rotation: refresh_token_reused).
See project_openclaw_oauth_auth_model.
"""

import json

import pytest

from core.containers.workspace import (
    delete_auth_profile_store,
    pre_stage_auth_profile_store,
)

# The oauth.json top-level key is the PROVIDER name; 6.6 builds profile
# `<provider>:default` from it. Provider MUST be "openai" (the retired
# "openai-codex" is silently dropped — there is no runtime alias).
_PROVIDER_KEY = "openai"


@pytest.fixture
def fake_efs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("EFS_MOUNT_PATH", str(tmp_path))
    return tmp_path


def _oauth_path(efs_root, user_id):
    return efs_root / user_id / "credentials" / "oauth.json"


@pytest.mark.asyncio
async def test_writes_flat_oauth_json(fake_efs_root):
    tokens = {
        "access_token": "eyJ.access",
        "refresh_token": "rt_opaque",
        "expires_at": 1730000000000,
        "email": "user@example.com",
    }
    await pre_stage_auth_profile_store(user_id="u_1", oauth_tokens=tokens)

    written = json.loads(_oauth_path(fake_efs_root, "u_1").read_text())
    # Flat Record<provider, OAuthCredentials> — NOT the {version, profiles}
    # AuthProfileStore wrapper, and NOT keyed by profile id.
    assert set(written.keys()) == {_PROVIDER_KEY}
    assert "version" not in written and "profiles" not in written
    creds = written[_PROVIDER_KEY]
    assert creds["access"] == "eyJ.access"
    assert creds["refresh"] == "rt_opaque"
    assert creds["expires"] == 1730000000000
    assert creds["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_single_file_no_per_agent_fanout(fake_efs_root):
    """Exactly ONE file at credentials/oauth.json — never per-agent."""
    # Pre-create agent dirs to prove we do NOT write into any of them.
    for aid in ("main", "friday-uuid", "atlas-uuid"):
        (fake_efs_root / "org_1" / "agents" / aid / "agent").mkdir(parents=True)

    await pre_stage_auth_profile_store(
        user_id="org_1",
        oauth_tokens={"access_token": "a", "refresh_token": "b"},
    )

    assert _oauth_path(fake_efs_root, "org_1").exists()
    leaked = list((fake_efs_root / "org_1" / "agents").rglob("auth-profiles.json"))
    assert leaked == [], f"unexpected per-agent fan-out: {leaked}"


@pytest.mark.asyncio
async def test_field_mapping_from_ddb_keys(fake_efs_root):
    """DDB access_token/refresh_token/expires_at/account_id ->
    OAuthCredentials access/refresh/expires/accountId."""
    await pre_stage_auth_profile_store(
        user_id="u_map",
        oauth_tokens={
            "access_token": "A",
            "refresh_token": "R",
            "expires_at": 42,
            "account_id": "acct",
        },
    )
    creds = json.loads(_oauth_path(fake_efs_root, "u_map").read_text())[_PROVIDER_KEY]
    assert creds == {"access": "A", "refresh": "R", "expires": 42, "accountId": "acct"}


@pytest.mark.asyncio
async def test_omits_optional_fields(fake_efs_root):
    await pre_stage_auth_profile_store(
        user_id="u_min",
        oauth_tokens={"access_token": "a", "refresh_token": "b"},
    )
    creds = json.loads(_oauth_path(fake_efs_root, "u_min").read_text())[_PROVIDER_KEY]
    assert creds == {"access": "a", "refresh": "b"}
    assert "expires" not in creds and "email" not in creds and "accountId" not in creds


@pytest.mark.asyncio
async def test_overwrites_on_reauth(fake_efs_root):
    """Re-OAuth: rewrite the file with new tokens, no merge."""
    await pre_stage_auth_profile_store(user_id="u_1", oauth_tokens={"access_token": "old", "refresh_token": "old_rt"})
    await pre_stage_auth_profile_store(user_id="u_1", oauth_tokens={"access_token": "new", "refresh_token": "new_rt"})
    creds = json.loads(_oauth_path(fake_efs_root, "u_1").read_text())[_PROVIDER_KEY]
    assert creds["access"] == "new"
    assert creds["refresh"] == "new_rt"


@pytest.mark.asyncio
async def test_creates_credentials_dir(fake_efs_root):
    """The credentials/ subdir doesn't exist yet — helper must mkdir -p."""
    await pre_stage_auth_profile_store(user_id="brand_new", oauth_tokens={"access_token": "x", "refresh_token": "y"})
    assert _oauth_path(fake_efs_root, "brand_new").exists()


@pytest.mark.asyncio
async def test_chown_failure_is_non_fatal(fake_efs_root, monkeypatch):
    def raising_chown(*args, **kwargs):
        raise PermissionError("can't chown in test env")

    monkeypatch.setattr("os.chown", raising_chown)
    await pre_stage_auth_profile_store(user_id="u_perm", oauth_tokens={"access_token": "x", "refresh_token": "y"})
    creds = json.loads(_oauth_path(fake_efs_root, "u_perm").read_text())[_PROVIDER_KEY]
    assert creds["access"] == "x"


@pytest.mark.asyncio
async def test_rejects_path_traversal_user_id(fake_efs_root):
    with pytest.raises(ValueError, match="invalid characters"):
        await pre_stage_auth_profile_store(
            user_id="../etc/passwd",
            oauth_tokens={"access_token": "x", "refresh_token": "y"},
        )
    assert not (fake_efs_root / "etc" / "passwd").exists()


@pytest.mark.asyncio
async def test_rejects_missing_required_tokens(fake_efs_root):
    with pytest.raises(ValueError, match="missing required keys"):
        await pre_stage_auth_profile_store(
            user_id="u_x",
            oauth_tokens={"access_token": "x"},  # no refresh_token
        )


@pytest.mark.asyncio
async def test_delete_removes_oauth_json(fake_efs_root):
    await pre_stage_auth_profile_store(user_id="u_del", oauth_tokens={"access_token": "x", "refresh_token": "y"})
    path = _oauth_path(fake_efs_root, "u_del")
    assert path.exists()

    await delete_auth_profile_store(user_id="u_del")
    assert not path.exists()


@pytest.mark.asyncio
async def test_delete_sweeps_legacy_per_agent_files(fake_efs_root):
    """Disconnect must also sweep any legacy fanned-out auth-profiles.json
    (pre-6.6 model), or a revoked agent keeps a usable refresh token on EFS."""
    user = "u_legacy"
    for aid in ("main", "friday-uuid"):
        d = fake_efs_root / user / "agents" / aid / "agent"
        d.mkdir(parents=True)
        (d / "auth-profiles.json").write_text("{}")

    await pre_stage_auth_profile_store(user_id=user, oauth_tokens={"access_token": "x", "refresh_token": "y"})
    await delete_auth_profile_store(user_id=user)

    assert not _oauth_path(fake_efs_root, user).exists()
    leaked = list((fake_efs_root / user / "agents").rglob("auth-profiles.json"))
    assert leaked == [], f"legacy auth-profiles.json not swept: {leaked}"


@pytest.mark.asyncio
async def test_delete_no_op_when_absent(fake_efs_root):
    """Disconnect on a never-staged user must not raise."""
    await delete_auth_profile_store(user_id="u_never")  # should not raise
