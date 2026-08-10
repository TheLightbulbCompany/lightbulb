"""EFS staging of the OpenCode harness credential (``opencode-data/opencode/auth.json``).

OpenCode reads ``$XDG_DATA_HOME/opencode/auth.json`` directly and rewrites it
on every token rotation — there is no import gate like the provider seed's.
The shape must match OpenCode's auth-store schema exactly (entries failing
schema decode are silently dropped): ``expires`` is a REQUIRED ms-epoch int.
"""

import json

import pytest

from core.containers.workspace import delete_opencode_auth, pre_stage_opencode_auth


@pytest.fixture
def fake_efs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("EFS_MOUNT_PATH", str(tmp_path))
    return tmp_path


def _auth_path(efs_root, user_id):
    return efs_root / user_id / "opencode-data" / "opencode" / "auth.json"


@pytest.mark.asyncio
async def test_writes_opencode_auth_shape(fake_efs_root):
    await pre_stage_opencode_auth(
        user_id="u_1",
        oauth_tokens={
            "access_token": "eyJ.access",
            "refresh_token": "rt_opaque",
            "expires_at": 1730000000000,
            "account_id": "acc_1",
        },
    )
    written = json.loads(_auth_path(fake_efs_root, "u_1").read_text())
    assert set(written.keys()) == {"openai"}
    creds = written["openai"]
    assert creds["type"] == "oauth"
    assert creds["access"] == "eyJ.access"
    assert creds["refresh"] == "rt_opaque"
    assert creds["expires"] == 1730000000000
    assert creds["accountId"] == "acc_1"


@pytest.mark.asyncio
async def test_expires_defaults_to_zero_not_absent(fake_efs_root):
    """``expires`` is required by OpenCode's schema — an entry without it is
    dropped, not defaulted. 0 = expired = refresh before first use."""
    await pre_stage_opencode_auth(
        user_id="u_1",
        oauth_tokens={"access_token": "a", "refresh_token": "r"},
    )
    creds = json.loads(_auth_path(fake_efs_root, "u_1").read_text())["openai"]
    assert creds["expires"] == 0
    assert "accountId" not in creds


@pytest.mark.asyncio
async def test_overwrite_false_preserves_live_file(fake_efs_root):
    """Provision-path self-heal must never clobber the live rotating file."""
    await pre_stage_opencode_auth(
        user_id="u_1",
        oauth_tokens={"access_token": "live", "refresh_token": "live_rt"},
    )
    await pre_stage_opencode_auth(
        user_id="u_1",
        oauth_tokens={"access_token": "stale", "refresh_token": "stale_rt"},
        overwrite=False,
    )
    creds = json.loads(_auth_path(fake_efs_root, "u_1").read_text())["openai"]
    assert creds["access"] == "live"


@pytest.mark.asyncio
async def test_overwrite_false_writes_when_missing(fake_efs_root):
    await pre_stage_opencode_auth(
        user_id="u_1",
        oauth_tokens={"access_token": "seed", "refresh_token": "seed_rt"},
        overwrite=False,
    )
    assert json.loads(_auth_path(fake_efs_root, "u_1").read_text())["openai"]["access"] == "seed"


@pytest.mark.asyncio
async def test_delete_removes_file_and_tolerates_absence(fake_efs_root):
    await pre_stage_opencode_auth(
        user_id="u_1",
        oauth_tokens={"access_token": "a", "refresh_token": "r"},
    )
    await delete_opencode_auth(user_id="u_1")
    assert not _auth_path(fake_efs_root, "u_1").exists()
    await delete_opencode_auth(user_id="u_1")  # second delete is a no-op


@pytest.mark.asyncio
async def test_rejects_bad_user_id(fake_efs_root):
    with pytest.raises(ValueError):
        await pre_stage_opencode_auth(
            user_id="../escape",
            oauth_tokens={"access_token": "a", "refresh_token": "r"},
        )
