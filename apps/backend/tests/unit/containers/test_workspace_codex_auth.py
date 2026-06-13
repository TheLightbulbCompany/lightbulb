"""Tests for the EFS pre-staging of OpenClaw's auth-profiles.json file.

OpenClaw's openai provider reads OAuth credentials from
``<agentDir>/auth-profiles.json`` (the new AuthProfileStore format).
``codex/auth.json`` (the legacy CLI format) is bridged FROM this file
automatically by OpenClaw — we never need to write it ourselves.
See docs/superpowers/specs/2026-04-29-chatgpt-oauth-auth-profiles-design.md.
"""

import json

import pytest

from core.containers.workspace import (
    delete_auth_profile_store,
    pre_stage_auth_profile_store,
)

# Verbatim from upstream src/agents/auth-profiles/constants.ts at the
# openclaw version we pin to.
_PROFILE_ID = "openai:default"
_AUTH_STORE_VERSION = 1


@pytest.fixture
def fake_efs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("EFS_MOUNT_PATH", str(tmp_path))
    return tmp_path


def _expected_path(efs_root, user_id):
    return efs_root / user_id / "agents" / "main" / "agent" / "auth-profiles.json"


@pytest.mark.asyncio
async def test_pre_stage_writes_auth_profile_store(fake_efs_root):
    tokens = {
        "access_token": "eyJ.access",
        "refresh_token": "rt_opaque",
        "expires_at": 1730000000000,
        "email": "user@example.com",
    }
    await pre_stage_auth_profile_store(user_id="u_1", oauth_tokens=tokens)

    written = json.loads(_expected_path(fake_efs_root, "u_1").read_text())
    assert written["version"] == _AUTH_STORE_VERSION
    profile = written["profiles"][_PROFILE_ID]
    assert profile["type"] == "oauth"
    assert profile["provider"] == "openai"
    assert profile["access"] == "eyJ.access"
    assert profile["refresh"] == "rt_opaque"
    assert profile["expires"] == 1730000000000
    assert profile["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_pre_stage_omits_optional_fields_when_absent(fake_efs_root):
    """expires_at and email are optional — must NOT write null/empty."""
    await pre_stage_auth_profile_store(
        user_id="u_min",
        oauth_tokens={"access_token": "a", "refresh_token": "b"},
    )
    profile = json.loads(_expected_path(fake_efs_root, "u_min").read_text())["profiles"][_PROFILE_ID]
    assert "expires" not in profile
    assert "email" not in profile


@pytest.mark.asyncio
async def test_pre_stage_overwrites_existing(fake_efs_root):
    """Re-OAuth: rewrite the file with new tokens, no merge needed."""
    await pre_stage_auth_profile_store(
        user_id="u_1",
        oauth_tokens={"access_token": "old", "refresh_token": "old_rt"},
    )
    await pre_stage_auth_profile_store(
        user_id="u_1",
        oauth_tokens={"access_token": "new", "refresh_token": "new_rt"},
    )
    profile = json.loads(_expected_path(fake_efs_root, "u_1").read_text())["profiles"][_PROFILE_ID]
    assert profile["access"] == "new"
    assert profile["refresh"] == "new_rt"


@pytest.mark.asyncio
async def test_pre_stage_creates_parent_dirs(fake_efs_root):
    """The agents/main/agent/ subdir tree doesn't exist yet — helper must mkdir -p."""
    await pre_stage_auth_profile_store(
        user_id="brand_new_user",
        oauth_tokens={"access_token": "x", "refresh_token": "y"},
    )
    assert _expected_path(fake_efs_root, "brand_new_user").exists()


@pytest.mark.asyncio
async def test_pre_stage_chown_failure_is_non_fatal(fake_efs_root, monkeypatch):
    """If the runtime can't chown to UID 1000 (e.g. local dev without root),
    the helper still writes the file and returns successfully."""

    def raising_chown(*args, **kwargs):
        raise PermissionError("can't chown in test env")

    monkeypatch.setattr("os.chown", raising_chown)

    await pre_stage_auth_profile_store(
        user_id="u_perm",
        oauth_tokens={"access_token": "x", "refresh_token": "y"},
    )

    profile = json.loads(_expected_path(fake_efs_root, "u_perm").read_text())["profiles"][_PROFILE_ID]
    assert profile["access"] == "x"


@pytest.mark.asyncio
async def test_pre_stage_rejects_path_traversal_user_id(fake_efs_root):
    """Defense-in-depth: a user_id with path-traversal chars is rejected."""
    with pytest.raises(ValueError, match="invalid characters"):
        await pre_stage_auth_profile_store(
            user_id="../etc/passwd",
            oauth_tokens={"access_token": "x", "refresh_token": "y"},
        )

    # Confirm no file was created at the malicious path.
    assert not (fake_efs_root / "etc" / "passwd").exists()


@pytest.mark.asyncio
async def test_pre_stage_rejects_missing_required_tokens(fake_efs_root):
    with pytest.raises(ValueError, match="missing required keys"):
        await pre_stage_auth_profile_store(
            user_id="u_x",
            oauth_tokens={"access_token": "x"},  # no refresh_token
        )


@pytest.mark.asyncio
async def test_delete_auth_profile_store_removes_file(fake_efs_root):
    await pre_stage_auth_profile_store(
        user_id="u_del",
        oauth_tokens={"access_token": "x", "refresh_token": "y"},
    )
    path = _expected_path(fake_efs_root, "u_del")
    assert path.exists()

    await delete_auth_profile_store(user_id="u_del")
    assert not path.exists()


@pytest.mark.asyncio
async def test_delete_auth_profile_store_no_op_when_absent(fake_efs_root):
    """Disconnect on a never-staged user must not raise."""
    await delete_auth_profile_store(user_id="u_never")  # should not raise


def _agent_path(efs_root, user_id, agent_id):
    return efs_root / user_id / "agents" / agent_id / "agent" / "auth-profiles.json"


@pytest.mark.asyncio
async def test_pre_stage_fans_out_to_all_agents(fake_efs_root):
    """The credential must land in EVERY agent's dir, not just `main` — OpenClaw
    resolves auth strictly per-agentDir with no shared fallback, so Friday/Atlas/
    custom agents boot unauthenticated unless each is staged. This is the bug
    that broke org ChatGPT-OAuth inference (only `main` was staged)."""
    agents = ["friday-uuid", "atlas-uuid", "custom-uuid"]
    await pre_stage_auth_profile_store(
        user_id="org_1",
        oauth_tokens={"access_token": "a", "refresh_token": "b"},
        agent_ids=agents,
    )
    for agent_id in ["main", *agents]:
        p = _agent_path(fake_efs_root, "org_1", agent_id)
        assert p.exists(), f"missing profile for agent {agent_id}"
        profile = json.loads(p.read_text())["profiles"][_PROFILE_ID]
        assert profile["provider"] == "openai"
        assert profile["access"] == "a"
        assert profile["refresh"] == "b"


@pytest.mark.asyncio
async def test_pre_stage_always_includes_main(fake_efs_root):
    """`main` is staged even when the caller's agent_ids omit it (default agent)."""
    await pre_stage_auth_profile_store(
        user_id="org_2",
        oauth_tokens={"access_token": "a", "refresh_token": "b"},
        agent_ids=["only-custom"],
    )
    assert _agent_path(fake_efs_root, "org_2", "main").exists()
    assert _agent_path(fake_efs_root, "org_2", "only-custom").exists()


@pytest.mark.asyncio
async def test_pre_stage_dedupes_agents(fake_efs_root):
    """Duplicate ids (incl. `main` in the list) don't error or double-write."""
    await pre_stage_auth_profile_store(
        user_id="org_3",
        oauth_tokens={"access_token": "a", "refresh_token": "b"},
        agent_ids=["main", "x", "x"],
    )
    assert _agent_path(fake_efs_root, "org_3", "main").exists()
    assert _agent_path(fake_efs_root, "org_3", "x").exists()


@pytest.mark.asyncio
async def test_pre_stage_rejects_unsafe_agent_id(fake_efs_root):
    """Path-traversal agent ids are refused (defense-in-depth; ids come from our
    own Paperclip population, but guard anyway)."""
    with pytest.raises(ValueError, match="unsafe agent_id"):
        await pre_stage_auth_profile_store(
            user_id="org_4",
            oauth_tokens={"access_token": "a", "refresh_token": "b"},
            agent_ids=["../../etc"],
        )
