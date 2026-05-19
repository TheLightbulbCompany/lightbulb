"""Tests for config patcher — EFS openclaw.json patching."""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.config_patcher import (  # noqa: E402
    ConfigPatchError,
    append_to_openclaw_config_list,
    apply_deploy_aux_mutations,
    delete_openclaw_config_path,
    patch_openclaw_config,
    remove_from_openclaw_config_list,
    replace_provider_block,
    set_openclaw_config_path,
)


@pytest.fixture
def efs_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_1")
        os.makedirs(user_dir)
        config = {
            "gateway": {"mode": "local", "bind": "lan"},
            "agents": {
                "defaults": {
                    "model": {"primary": "amazon-bedrock/minimax.minimax-m2.5"},
                    "models": {
                        "amazon-bedrock/minimax.minimax-m2.5": {"alias": "MiniMax M2.5"},
                    },
                }
            },
            "tools": {"profile": "full"},
        }
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)

        with patch("core.services.config_patcher._efs_mount_path", tmpdir):
            yield tmpdir


@pytest.mark.asyncio
async def test_patch_updates_model(efs_dir):
    await patch_openclaw_config(
        "user_1", {"agents": {"defaults": {"model": {"primary": "amazon-bedrock/qwen.qwen3-vl-235b-a22b"}}}}
    )
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert result["agents"]["defaults"]["model"]["primary"] == "amazon-bedrock/qwen.qwen3-vl-235b-a22b"


@pytest.mark.asyncio
async def test_patch_preserves_gateway(efs_dir):
    await patch_openclaw_config("user_1", {"agents": {"defaults": {"model": {"primary": "new-model"}}}})
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert result["gateway"]["mode"] == "local"
    assert result["gateway"]["bind"] == "lan"


@pytest.mark.asyncio
async def test_patch_preserves_tools(efs_dir):
    await patch_openclaw_config("user_1", {"agents": {"defaults": {"model": {"primary": "new-model"}}}})
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert result["tools"]["profile"] == "full"


@pytest.mark.asyncio
async def test_patch_creates_backup(efs_dir):
    await patch_openclaw_config("user_1", {"agents": {"defaults": {"model": {"primary": "new-model"}}}})
    backup = os.path.join(efs_dir, "user_1", "openclaw.json.bak")
    assert os.path.exists(backup)
    with open(backup) as f:
        original = json.load(f)
    assert original["agents"]["defaults"]["model"]["primary"] == "amazon-bedrock/minimax.minimax-m2.5"


@pytest.mark.asyncio
async def test_patch_deep_merges_models(efs_dir):
    await patch_openclaw_config(
        "user_1",
        {"agents": {"defaults": {"models": {"amazon-bedrock/qwen.qwen3-vl-235b-a22b": {"alias": "Qwen3 235B"}}}}},
    )
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    models = result["agents"]["defaults"]["models"]
    assert "amazon-bedrock/qwen.qwen3-vl-235b-a22b" in models
    assert "amazon-bedrock/minimax.minimax-m2.5" in models


@pytest.mark.asyncio
async def test_patch_nonexistent_owner_raises(efs_dir):
    with pytest.raises(ConfigPatchError, match="not found"):
        await patch_openclaw_config("nonexistent_user", {"agents": {}})


@pytest.mark.asyncio
async def test_patch_autowires_route_binding_for_new_channel_account(efs_dir):
    """A patch that introduces channels.<provider>.accounts.<agent_id> must
    also land a matching top-level route binding in the same atomic write.
    Without the binding OpenClaw's dispatcher falls through to the default
    agent — see desktop/openclaw src/routing/bindings.ts.
    """
    await patch_openclaw_config(
        "user_1",
        {"channels": {"telegram": {"accounts": {"ray": {"botToken": "T"}}}}},
    )
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert result["channels"]["telegram"]["accounts"]["ray"]["botToken"] == "T"
    assert result["bindings"] == [
        {"type": "route", "agentId": "ray", "match": {"channel": "telegram", "accountId": "ray"}}
    ]


@pytest.mark.asyncio
async def test_patch_autowires_binding_per_account_across_providers(efs_dir):
    await patch_openclaw_config(
        "user_1",
        {
            "channels": {
                "telegram": {"accounts": {"ray": {"botToken": "T1"}}},
                "discord": {"accounts": {"sales": {"token": "D1"}}},
            }
        },
    )
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    by_agent = {b["agentId"]: b for b in result["bindings"]}
    assert by_agent["ray"]["match"] == {"channel": "telegram", "accountId": "ray"}
    assert by_agent["sales"]["match"] == {"channel": "discord", "accountId": "sales"}
    assert len(result["bindings"]) == 2


@pytest.mark.asyncio
async def test_patch_dedupes_binding_on_repeat(efs_dir):
    for _ in range(2):
        await patch_openclaw_config(
            "user_1",
            {"channels": {"telegram": {"accounts": {"ray": {"botToken": "T"}}}}},
        )
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert len(result["bindings"]) == 1


@pytest.mark.asyncio
async def test_patch_preserves_existing_unrelated_bindings(efs_dir):
    await patch_openclaw_config(
        "user_1",
        {"channels": {"telegram": {"accounts": {"main": {"botToken": "M"}}}}},
    )
    await patch_openclaw_config(
        "user_1",
        {"channels": {"telegram": {"accounts": {"ray": {"botToken": "R"}}}}},
    )
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert sorted(b["agentId"] for b in result["bindings"]) == ["main", "ray"]


@pytest.mark.asyncio
async def test_patch_no_binding_when_patch_has_no_channels(efs_dir):
    await patch_openclaw_config("user_1", {"tools": {"profile": "full"}})
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert "bindings" not in result


@pytest.mark.asyncio
async def test_patch_no_binding_when_channels_touched_without_accounts(efs_dir):
    await patch_openclaw_config("user_1", {"channels": {"telegram": {"enabled": True}}})
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert result.get("bindings", []) == []


@pytest.fixture
def tmp_efs_with_config(monkeypatch):
    """Write a minimal openclaw.json to a tmp 'EFS' dir and point the patcher at it."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", d)
        owner_id = "user_test"
        owner_dir = os.path.join(d, owner_id)
        os.makedirs(owner_dir)
        config_path = os.path.join(owner_dir, "openclaw.json")
        with open(config_path, "w") as f:
            json.dump({"channels": {"telegram": {"accounts": {"main": {"allowFrom": ["111"]}}}}}, f)
        yield d, owner_id, config_path


@pytest.mark.asyncio
async def test_append_to_list_appends_to_existing(tmp_efs_with_config):
    _, owner_id, config_path = tmp_efs_with_config
    await append_to_openclaw_config_list(
        owner_id,
        ["channels", "telegram", "accounts", "main", "allowFrom"],
        "222",
    )
    with open(config_path) as f:
        result = json.load(f)
    assert result["channels"]["telegram"]["accounts"]["main"]["allowFrom"] == ["111", "222"]


@pytest.mark.asyncio
async def test_append_to_list_creates_path_when_missing(tmp_efs_with_config):
    _, owner_id, config_path = tmp_efs_with_config
    await append_to_openclaw_config_list(
        owner_id,
        ["channels", "discord", "accounts", "sales", "allowFrom"],
        "999",
    )
    with open(config_path) as f:
        result = json.load(f)
    assert result["channels"]["discord"]["accounts"]["sales"]["allowFrom"] == ["999"]


@pytest.mark.asyncio
async def test_append_to_list_dedups(tmp_efs_with_config):
    _, owner_id, config_path = tmp_efs_with_config
    await append_to_openclaw_config_list(
        owner_id,
        ["channels", "telegram", "accounts", "main", "allowFrom"],
        "111",  # already present
    )
    with open(config_path) as f:
        result = json.load(f)
    assert result["channels"]["telegram"]["accounts"]["main"]["allowFrom"] == ["111"]


@pytest.mark.asyncio
async def test_append_to_list_missing_config_file_raises(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", d)
        with pytest.raises(ConfigPatchError):
            await append_to_openclaw_config_list(
                "user_doesnt_exist",
                ["channels", "telegram", "accounts", "main", "allowFrom"],
                "123",
            )


@pytest.fixture
def tmp_efs_with_bindings(monkeypatch):
    """Minimal openclaw.json with a bindings array for predicate-removal testing."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", d)
        owner_id = "user_test"
        owner_dir = os.path.join(d, owner_id)
        os.makedirs(owner_dir)
        config_path = os.path.join(owner_dir, "openclaw.json")
        with open(config_path, "w") as f:
            json.dump(
                {
                    "channels": {"telegram": {"accounts": {"main": {"allowFrom": ["111", "222", "333"]}}}},
                    "bindings": [
                        {"match": {"channel": "telegram", "accountId": "main"}, "agentId": "main"},
                        {"match": {"channel": "telegram", "accountId": "sales"}, "agentId": "sales"},
                        {"match": {"channel": "discord", "accountId": "main"}, "agentId": "main"},
                    ],
                },
                f,
            )
        yield d, owner_id, config_path


@pytest.mark.asyncio
async def test_remove_from_list_value_match(tmp_efs_with_bindings):
    _, owner_id, config_path = tmp_efs_with_bindings
    await remove_from_openclaw_config_list(
        owner_id,
        ["channels", "telegram", "accounts", "main", "allowFrom"],
        predicate=lambda v: v == "222",
    )
    with open(config_path) as f:
        result = json.load(f)
    assert result["channels"]["telegram"]["accounts"]["main"]["allowFrom"] == ["111", "333"]


@pytest.mark.asyncio
async def test_remove_from_list_predicate_match_dict(tmp_efs_with_bindings):
    _, owner_id, config_path = tmp_efs_with_bindings
    await remove_from_openclaw_config_list(
        owner_id,
        ["bindings"],
        predicate=lambda b: (
            b.get("match", {}).get("channel") == "telegram" and b.get("match", {}).get("accountId") == "sales"
        ),
    )
    with open(config_path) as f:
        result = json.load(f)
    assert len(result["bindings"]) == 2
    assert all(b["match"]["accountId"] != "sales" for b in result["bindings"])


@pytest.mark.asyncio
async def test_remove_from_list_no_match_is_noop(tmp_efs_with_bindings):
    _, owner_id, config_path = tmp_efs_with_bindings
    await remove_from_openclaw_config_list(
        owner_id,
        ["channels", "telegram", "accounts", "main", "allowFrom"],
        predicate=lambda v: v == "nonexistent",
    )
    with open(config_path) as f:
        result = json.load(f)
    assert result["channels"]["telegram"]["accounts"]["main"]["allowFrom"] == ["111", "222", "333"]


@pytest.mark.asyncio
async def test_remove_from_list_missing_path_is_noop(tmp_efs_with_bindings):
    _, owner_id, config_path = tmp_efs_with_bindings
    await remove_from_openclaw_config_list(
        owner_id,
        ["channels", "slack", "accounts", "main", "allowFrom"],  # doesn't exist
        predicate=lambda v: True,
    )
    with open(config_path) as f:
        result = json.load(f)
    # Original config completely untouched (no path collision wrote anything)
    assert result == {
        "channels": {"telegram": {"accounts": {"main": {"allowFrom": ["111", "222", "333"]}}}},
        "bindings": [
            {"match": {"channel": "telegram", "accountId": "main"}, "agentId": "main"},
            {"match": {"channel": "telegram", "accountId": "sales"}, "agentId": "sales"},
            {"match": {"channel": "discord", "accountId": "main"}, "agentId": "main"},
        ],
    }


@pytest.fixture
def tmp_efs_with_multi_accounts(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", d)
        owner_id = "user_test"
        owner_dir = os.path.join(d, owner_id)
        os.makedirs(owner_dir)
        config_path = os.path.join(owner_dir, "openclaw.json")
        with open(config_path, "w") as f:
            json.dump(
                {
                    "channels": {
                        "telegram": {
                            "accounts": {
                                "main": {"botToken": "aaa", "allowFrom": ["111"]},
                                "sales": {"botToken": "bbb", "allowFrom": ["222"]},
                            },
                        },
                    },
                },
                f,
            )
        yield d, owner_id, config_path


@pytest.mark.asyncio
async def test_delete_path_removes_nested_key(tmp_efs_with_multi_accounts):
    _, owner_id, config_path = tmp_efs_with_multi_accounts
    await delete_openclaw_config_path(
        owner_id,
        ["channels", "telegram", "accounts", "sales"],
    )
    with open(config_path) as f:
        result = json.load(f)
    # sales removed, main preserved
    assert "sales" not in result["channels"]["telegram"]["accounts"]
    assert "main" in result["channels"]["telegram"]["accounts"]
    assert result["channels"]["telegram"]["accounts"]["main"]["botToken"] == "aaa"


@pytest.mark.asyncio
async def test_delete_path_leaves_empty_parent_as_empty_dict(tmp_efs_with_multi_accounts):
    _, owner_id, config_path = tmp_efs_with_multi_accounts
    await delete_openclaw_config_path(owner_id, ["channels", "telegram", "accounts", "main"])
    await delete_openclaw_config_path(owner_id, ["channels", "telegram", "accounts", "sales"])
    with open(config_path) as f:
        result = json.load(f)
    # The parent accounts dict is left as {} rather than being pruned
    assert result["channels"]["telegram"]["accounts"] == {}


@pytest.mark.asyncio
async def test_delete_path_missing_key_is_noop(tmp_efs_with_multi_accounts):
    _, owner_id, config_path = tmp_efs_with_multi_accounts
    await delete_openclaw_config_path(
        owner_id,
        ["channels", "telegram", "accounts", "does_not_exist"],
    )
    with open(config_path) as f:
        result = json.load(f)
    # Nothing removed
    assert "main" in result["channels"]["telegram"]["accounts"]
    assert "sales" in result["channels"]["telegram"]["accounts"]


@pytest.mark.asyncio
async def test_delete_path_missing_intermediate_is_noop(tmp_efs_with_multi_accounts):
    _, owner_id, config_path = tmp_efs_with_multi_accounts
    await delete_openclaw_config_path(
        owner_id,
        ["channels", "slack", "accounts", "main"],  # slack branch doesn't exist
    )
    with open(config_path) as f:
        result = json.load(f)
    assert "main" in result["channels"]["telegram"]["accounts"]


# ------------------------------------------------------------------
# apply_deploy_aux_mutations (catalog deploy path — plugins/mcp/skills only;
# agent_entry now goes through pair_with_openclaw RPC, not the file merge)
# ------------------------------------------------------------------


@pytest.fixture
def catalog_efs_dir():
    """EFS fixture with the real OpenClaw ``agents.list`` schema
    (see openclaw/src/config/zod-schema.agents.ts)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_1")
        os.makedirs(user_dir)
        config = {
            "agents": {"list": [{"id": "existing_agent", "name": "Existing"}]},
            "plugins": {"memory": {"enabled": True}},
            "tools": {"allowed": ["web-search"]},
        }
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)

        with patch("core.services.config_patcher._efs_mount_path", tmpdir):
            yield tmpdir


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_no_agent_entry_leaves_list_untouched(tmp_path, monkeypatch):
    """When no agent_entry is supplied, agents.list stays unchanged.
    Plugins/mcp/skills still deep-merge. This is the legacy listing
    path (no slice.agent) plus any call that wants only the aux merges.
    """
    from core.services import config_patcher

    config_dir = tmp_path / "user_abc"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    config_path.write_text('{"agents": {"list": [{"id": "agent_pre_existing"}]}, "plugins": {"entries": {}}}')

    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    await config_patcher.apply_deploy_aux_mutations(
        "user_abc",
        plugins_patch={"entries": {"resy": {"enabled": True}}},
        mcp_patch={"servers": {"resy": {"command": "node"}}},
        skills_patch={"entries": {"haiku": {"enabled": True}}, "allowBundled": ["foo"]},
    )

    written = json.loads(config_path.read_text())
    assert written["plugins"]["entries"]["resy"] == {"enabled": True}
    assert written["mcp"]["servers"]["resy"] == {"command": "node"}
    assert written["skills"]["entries"]["haiku"] == {"enabled": True}
    assert written["skills"]["allowBundled"] == ["foo"]
    # agents.list untouched — no agent_entry was passed.
    assert written["agents"]["list"] == [{"id": "agent_pre_existing"}]


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_appends_agent_entry_to_list(tmp_path, monkeypatch):
    """When agent_entry is provided, it gets appended to agents.list so
    chokidar's eventual reload picks up per-agent fields the OpenClaw
    agents.update RPC schema can't carry (skills binding,
    reasoningDefault, agentRuntime, etc.).
    """
    from core.services import config_patcher

    config_dir = tmp_path / "user_abc"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    config_path.write_text('{"agents": {"list": [{"id": "agent_pre_existing"}]}}')

    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    new_entry = {
        "id": "agent_new",
        "name": "Ray",
        "skills": ["mcp-resy"],
        "reasoningDefault": "on",
    }
    await config_patcher.apply_deploy_aux_mutations(
        "user_abc",
        plugins_patch={},
        agent_entry=new_entry,
    )

    written = json.loads(config_path.read_text())
    assert {a["id"] for a in written["agents"]["list"]} == {"agent_pre_existing", "agent_new"}
    appended = next(a for a in written["agents"]["list"] if a["id"] == "agent_new")
    assert appended["skills"] == ["mcp-resy"]
    assert appended["reasoningDefault"] == "on"
    assert appended["name"] == "Ray"


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_replaces_existing_agent_entry(tmp_path, monkeypatch):
    """Idempotent retry: if an agent with the same id already exists in
    agents.list (a previous deploy attempt got partway through), the
    new entry replaces it in place instead of duplicating.
    """
    from core.services import config_patcher

    config_dir = tmp_path / "user_abc"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    config_path.write_text(
        '{"agents": {"list": [{"id": "agent_dup", "name": "stale", "skills": []},{"id": "agent_other"}]}}'
    )

    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    await config_patcher.apply_deploy_aux_mutations(
        "user_abc",
        plugins_patch={},
        agent_entry={"id": "agent_dup", "name": "Ray", "skills": ["mcp-resy"]},
    )

    written = json.loads(config_path.read_text())
    ids = [a["id"] for a in written["agents"]["list"]]
    assert ids == ["agent_dup", "agent_other"]  # no duplication, order preserved
    dup = next(a for a in written["agents"]["list"] if a["id"] == "agent_dup")
    assert dup["name"] == "Ray"  # replaced, not merged
    assert dup["skills"] == ["mcp-resy"]


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_normalizes_legacy_flat_list(tmp_path, monkeypatch):
    """Legacy configs with ``agents: [...]`` (flat list) are tolerated
    on read and normalized to ``agents: {list: [...]}`` on write.
    """
    from core.services import config_patcher

    config_dir = tmp_path / "user_abc"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    config_path.write_text('{"agents": [{"id": "agent_legacy"}]}')

    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    await config_patcher.apply_deploy_aux_mutations(
        "user_abc",
        plugins_patch={},
        agent_entry={"id": "agent_new", "name": "Ray"},
    )

    written = json.loads(config_path.read_text())
    # agents is now an object with list — flat shape is gone.
    assert isinstance(written["agents"], dict)
    ids = [a["id"] for a in written["agents"]["list"]]
    assert ids == ["agent_legacy", "agent_new"]


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_merges_mcp_servers(catalog_efs_dir):
    """mcp_patch={'servers': {...}} deep-merges into config['mcp']['servers'].
    Existing MCPs untouched; same-name MCPs deep-merged so the buyer's
    config wins for any pre-existing keys (their env values survive)."""
    config_path = os.path.join(catalog_efs_dir, "user_1", "openclaw.json")
    with open(config_path) as f:
        cfg = json.load(f)
    cfg["mcp"] = {"servers": {"existing-mcp": {"command": "echo"}}}
    with open(config_path, "w") as f:
        json.dump(cfg, f)

    await apply_deploy_aux_mutations(
        "user_1",
        plugins_patch={},
        mcp_patch={
            "servers": {
                "jira-mcp": {
                    "command": "node",
                    "args": ["server.js"],
                    "env": {"JIRA_TOKEN": ""},
                }
            }
        },
    )
    with open(config_path) as f:
        result = json.load(f)

    servers = result["mcp"]["servers"]
    assert "existing-mcp" in servers
    assert servers["jira-mcp"]["command"] == "node"
    assert servers["jira-mcp"]["env"] == {"JIRA_TOKEN": ""}


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_merges_skills_entries_and_allowbundled(
    catalog_efs_dir,
):
    """skills_patch with both `entries` and `allowBundled` merges into
    config['skills']: entries deep-merged, allowBundled unioned (no dupes)."""
    config_path = os.path.join(catalog_efs_dir, "user_1", "openclaw.json")
    with open(config_path) as f:
        cfg = json.load(f)
    cfg["skills"] = {
        "entries": {"existing-skill": {"enabled": True}},
        "allowBundled": ["search"],
    }
    with open(config_path, "w") as f:
        json.dump(cfg, f)

    await apply_deploy_aux_mutations(
        "user_1",
        plugins_patch={},
        skills_patch={
            "entries": {"jira": {"enabled": True, "apiKey": "", "env": {"JIRA_USER": ""}}},
            "allowBundled": ["search", "fs-read"],
        },
    )
    with open(config_path) as f:
        result = json.load(f)

    skills = result["skills"]
    # Entries deep-merged, both old and new present.
    assert "existing-skill" in skills["entries"]
    assert skills["entries"]["jira"]["apiKey"] == ""
    # allowBundled unioned without duplicates.
    assert sorted(skills["allowBundled"]) == sorted(["search", "fs-read"])


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_none_patches_are_noops(catalog_efs_dir):
    """Default (no mcp_patch / no skills_patch + empty plugins_patch) leaves
    mcp/skills keys unsynthesized."""
    config_path = os.path.join(catalog_efs_dir, "user_1", "openclaw.json")
    await apply_deploy_aux_mutations(
        "user_1",
        plugins_patch={},
    )
    with open(config_path) as f:
        result = json.load(f)

    assert "mcp" not in result
    assert "skills" not in result


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_merges_plugins(catalog_efs_dir):
    """plugins_patch deep-merges into config['plugins'] (existing keys
    preserved, new keys added)."""
    config_path = os.path.join(catalog_efs_dir, "user_1", "openclaw.json")
    await apply_deploy_aux_mutations(
        "user_1",
        plugins_patch={"entries": {"browser": {"enabled": True}}},
    )
    with open(config_path) as f:
        result = json.load(f)

    # Plugins deep-merged under entries.{name} (real OpenClaw schema).
    assert result["plugins"]["memory"]["enabled"] is True
    assert result["plugins"]["entries"]["browser"]["enabled"] is True
    # agents.list NOT touched (aux mutation never writes there now).
    assert [a["id"] for a in result["agents"]["list"]] == ["existing_agent"]


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_self_heals_invalid_tools_allowed(catalog_efs_dir):
    """Pre-fix deploys wrote ``tools.allowed = []`` which OpenClaw's strict
    ToolsSchema rejects. On the next deploy, that key must be stripped so
    the file becomes valid again — without requiring a manual cleanup."""
    config_path = os.path.join(catalog_efs_dir, "user_1", "openclaw.json")
    with open(config_path, "w") as f:
        json.dump(
            {
                "agents": {"list": [{"id": "existing_agent", "name": "E"}]},
                "tools": {
                    "profile": "full",
                    "deny": ["canvas"],
                    "allowed": [],  # invalid — pre-fix corruption
                    "web": {"fetch": {"enabled": True}},
                },
            },
            f,
        )

    await apply_deploy_aux_mutations(
        "user_1",
        plugins_patch={},
    )
    with open(config_path) as f:
        result = json.load(f)

    tools = result.get("tools") or {}
    assert "allowed" not in tools
    # Other valid tool fields untouched.
    assert tools.get("profile") == "full"
    assert tools.get("deny") == ["canvas"]
    assert tools.get("web") == {"fetch": {"enabled": True}}


# ---- append_cron_jobs ----
#
# Cron jobs live in {owner_id}/cron/jobs.json (separate file from
# openclaw.json — see config.cron.store in OpenClaw's schema). The deploy
# path uses append_cron_jobs() to atomically add the publisher's filtered
# + remapped jobs to the deployer's file.


@pytest.fixture
def cron_efs_dir():
    """EFS fixture for cron-jobs tests. Same temp tree shape as catalog_efs_dir
    but without the openclaw.json prefill — append_cron_jobs operates on a
    different file and shouldn't depend on the agent config existing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "user_1"))
        with patch("core.services.config_patcher._efs_mount_path", tmpdir):
            yield tmpdir


@pytest.mark.asyncio
async def test_append_cron_jobs_creates_jobs_file_if_missing(cron_efs_dir):
    from core.services.config_patcher import append_cron_jobs

    jobs_path = os.path.join(cron_efs_dir, "user_1", "cron", "jobs.json")
    assert not os.path.exists(jobs_path)

    await append_cron_jobs(
        "user_1",
        [
            {
                "id": "j1",
                "agentId": "agent_abc",
                "name": "Morning brief",
                "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
            }
        ],
    )

    assert os.path.exists(jobs_path)
    with open(jobs_path) as f:
        data = json.load(f)
    assert data["version"] == 1
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["name"] == "Morning brief"


@pytest.mark.asyncio
async def test_append_cron_jobs_extends_existing_file(cron_efs_dir):
    from core.services.config_patcher import append_cron_jobs

    jobs_path = os.path.join(cron_efs_dir, "user_1", "cron", "jobs.json")
    os.makedirs(os.path.dirname(jobs_path), exist_ok=True)
    with open(jobs_path, "w") as f:
        json.dump(
            {
                "version": 1,
                "jobs": [{"id": "existing", "agentId": "main", "name": "Existing"}],
            },
            f,
        )

    await append_cron_jobs(
        "user_1",
        [{"id": "new1", "agentId": "agent_abc", "name": "New A"}],
    )
    await append_cron_jobs(
        "user_1",
        [{"id": "new2", "agentId": "agent_xyz", "name": "New B"}],
    )

    with open(jobs_path) as f:
        data = json.load(f)
    assert [j["id"] for j in data["jobs"]] == ["existing", "new1", "new2"]


@pytest.mark.asyncio
async def test_append_cron_jobs_empty_list_is_noop(cron_efs_dir):
    """Callers that have nothing to add shouldn't be required to skip the
    call themselves — the helper short-circuits and doesn't create the file."""
    from core.services.config_patcher import append_cron_jobs

    jobs_path = os.path.join(cron_efs_dir, "user_1", "cron", "jobs.json")
    await append_cron_jobs("user_1", [])
    assert not os.path.exists(jobs_path)


@pytest.mark.asyncio
async def test_append_cron_jobs_recovers_from_corrupt_file(cron_efs_dir):
    """If jobs.json is unreadable JSON (e.g. partial write from a crash),
    treat it as empty rather than refusing to ever write again."""
    from core.services.config_patcher import append_cron_jobs

    jobs_path = os.path.join(cron_efs_dir, "user_1", "cron", "jobs.json")
    os.makedirs(os.path.dirname(jobs_path), exist_ok=True)
    with open(jobs_path, "w") as f:
        f.write("{ this is not json")

    await append_cron_jobs(
        "user_1",
        [{"id": "j1", "agentId": "agent_abc", "name": "Recovery"}],
    )

    with open(jobs_path) as f:
        data = json.load(f)
    assert data["version"] == 1
    assert [j["name"] for j in data["jobs"]] == ["Recovery"]


# ------------------------------------------------------------------
# set_openclaw_config_path (replace-leaf semantics)
# ------------------------------------------------------------------


@pytest.fixture
def set_efs_root(monkeypatch):
    """Point config_patcher at a temp EFS root for set_openclaw_config_path tests."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", d)
        yield d


def _seed_set_config(efs_root: str, owner_id: str, config: dict) -> str:
    user_dir = os.path.join(efs_root, owner_id)
    os.makedirs(user_dir, exist_ok=True)
    config_path = os.path.join(user_dir, "openclaw.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return config_path


class TestSetOpenclawConfigPath:
    @pytest.mark.asyncio
    async def test_replaces_existing_leaf(self, set_efs_root):
        config_path = _seed_set_config(
            set_efs_root,
            "user_set_1",
            {"mcp": {"servers": {"old": {"command": "x"}}}},
        )
        await set_openclaw_config_path(
            "user_set_1",
            ["mcp", "servers"],
            {"new": {"command": "y"}},
        )
        with open(config_path) as f:
            result = json.load(f)
        assert result["mcp"]["servers"] == {"new": {"command": "y"}}

    @pytest.mark.asyncio
    async def test_creates_missing_intermediates(self, set_efs_root):
        config_path = _seed_set_config(set_efs_root, "user_set_2", {})
        await set_openclaw_config_path(
            "user_set_2",
            ["mcp", "servers"],
            {"a": {"command": "b"}},
        )
        with open(config_path) as f:
            result = json.load(f)
        assert result["mcp"]["servers"] == {"a": {"command": "b"}}

    @pytest.mark.asyncio
    async def test_overwrites_non_dict_intermediate(self, set_efs_root):
        """If an intermediate path segment is not a dict, replace it."""
        config_path = _seed_set_config(set_efs_root, "user_set_3", {"mcp": "stringy"})
        await set_openclaw_config_path(
            "user_set_3",
            ["mcp", "servers"],
            {"a": {"command": "b"}},
        )
        with open(config_path) as f:
            result = json.load(f)
        assert result["mcp"] == {"servers": {"a": {"command": "b"}}}

    @pytest.mark.asyncio
    async def test_empty_path_raises(self, set_efs_root):
        _seed_set_config(set_efs_root, "user_set_4", {})
        with pytest.raises(ConfigPatchError):
            await set_openclaw_config_path("user_set_4", [], {"x": 1})

    @pytest.mark.asyncio
    async def test_missing_config_raises(self, set_efs_root):
        with pytest.raises(ConfigPatchError):
            await set_openclaw_config_path("nonexistent_owner", ["mcp", "servers"], {})

    @pytest.mark.asyncio
    async def test_noop_when_value_unchanged(self, set_efs_root):
        """Setting an identical value should not bump the file mtime."""
        config_path = _seed_set_config(
            set_efs_root,
            "user_set_5",
            {"mcp": {"servers": {"a": {"command": "b"}}}},
        )
        original_mtime = os.stat(config_path).st_mtime_ns
        await set_openclaw_config_path(
            "user_set_5",
            ["mcp", "servers"],
            {"a": {"command": "b"}},
        )
        assert os.stat(config_path).st_mtime_ns == original_mtime


# ---------------------------------------------------------------------------
# replace_provider_block — used by billing_service.change_provider's hot path
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_efs_with_bedrock_provider(monkeypatch):
    """Owner whose openclaw.json has the Bedrock provider block written
    end-to-end (matches what write_openclaw_config produces post-onboarding
    for a bedrock_claude owner). The replace test then swaps it out for
    the chatgpt_oauth shape (empty providers + plugin entries)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_p")
        os.makedirs(user_dir)
        config = {
            "gateway": {"mode": "local"},
            "models": {
                "providers": {
                    "amazon-bedrock": {
                        "baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com",
                        "api": "bedrock-converse-stream",
                        "auth": "aws-sdk",
                        "models": [{"id": "us.anthropic.claude-sonnet-4-6"}],
                    },
                },
            },
            "agents": {
                "defaults": {
                    "model": {
                        "primary": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
                        "fallbacks": ["amazon-bedrock/us.anthropic.claude-sonnet-4-6"],
                    },
                    "workspace": "/home/node/.openclaw/workspaces",
                },
                "list": [
                    {"id": "main", "default": True, "workspace": "/home/node/.openclaw/workspaces/main"},
                ],
            },
            "plugins": {
                "entries": {
                    "amazon-bedrock": {"config": {"discovery": {"enabled": False}}},
                },
            },
            "tools": {"profile": "full"},
        }
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", tmpdir)
        yield tmpdir


@pytest.mark.asyncio
async def test_replace_provider_block_swaps_bedrock_for_chatgpt(tmp_efs_with_bedrock_provider):
    """Bedrock → ChatGPT OAuth: amazon-bedrock provider entry MUST be
    gone after the replace (a deep-merge into {} would leave it in place).
    Same for plugins.entries."""
    await replace_provider_block(
        "user_p",
        providers={},
        default_model={"primary": "openai-codex/gpt-5.5", "fallbacks": ["openai-codex/gpt-5.5"]},
        plugin_entries={},
    )
    with open(os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")) as f:
        result = json.load(f)
    assert result["models"]["providers"] == {}
    assert result["plugins"]["entries"] == {}
    assert result["agents"]["defaults"]["model"]["primary"] == "openai-codex/gpt-5.5"


@pytest.mark.asyncio
async def test_replace_provider_block_preserves_unrelated_subtrees(tmp_efs_with_bedrock_provider):
    """The swap touches exactly three subtrees; agents.list, agents.defaults
    .workspace, gateway, tools, etc must survive untouched. Catalog deploys
    write to agents.list — a replace that nuked siblings would lose deployed
    agents on every provider switch."""
    await replace_provider_block(
        "user_p",
        providers={},
        default_model={"primary": "openai-codex/gpt-5.5", "fallbacks": ["openai-codex/gpt-5.5"]},
        plugin_entries={},
    )
    with open(os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")) as f:
        result = json.load(f)
    # agents.list (catalog-deployed agents) preserved.
    assert result["agents"]["list"] == [
        {"id": "main", "default": True, "workspace": "/home/node/.openclaw/workspaces/main"},
    ]
    # agents.defaults.workspace preserved (only .model swapped).
    assert result["agents"]["defaults"]["workspace"] == "/home/node/.openclaw/workspaces"
    # Top-level siblings preserved.
    assert result["gateway"] == {"mode": "local"}
    assert result["tools"] == {"profile": "full"}


@pytest.mark.asyncio
async def test_replace_provider_block_swaps_chatgpt_for_bedrock():
    """ChatGPT OAuth → Bedrock: starting from an empty providers dict +
    empty plugin entries, the bedrock block must land cleanly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_c")
        os.makedirs(user_dir)
        config = {
            "models": {"providers": {}},
            "agents": {
                "defaults": {"model": {"primary": "openai-codex/gpt-5.5", "fallbacks": ["openai-codex/gpt-5.5"]}}
            },
            "plugins": {"entries": {}},
            "tools": {"profile": "full"},
        }
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)
        with patch("core.services.config_patcher._efs_mount_path", tmpdir):
            await replace_provider_block(
                "user_c",
                providers={
                    "amazon-bedrock": {
                        "baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com",
                        "api": "bedrock-converse-stream",
                        "auth": "aws-sdk",
                        "models": [{"id": "us.anthropic.claude-sonnet-4-6"}],
                    },
                },
                default_model={
                    "primary": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
                    "fallbacks": ["amazon-bedrock/us.anthropic.claude-sonnet-4-6"],
                },
                plugin_entries={"amazon-bedrock": {"config": {"discovery": {"enabled": False}}}},
            )
            with open(os.path.join(tmpdir, "user_c", "openclaw.json")) as f:
                result = json.load(f)
    assert result["models"]["providers"]["amazon-bedrock"]["api"] == "bedrock-converse-stream"
    assert result["plugins"]["entries"]["amazon-bedrock"]["config"]["discovery"]["enabled"] is False
    assert result["agents"]["defaults"]["model"]["primary"].startswith("amazon-bedrock/us.")


@pytest.mark.asyncio
async def test_replace_provider_block_missing_config_raises():
    """No openclaw.json on EFS — callers (change_provider) fall back to the
    cold path. ConfigPatchError is the signal."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # No user dir / no openclaw.json.
        with patch("core.services.config_patcher._efs_mount_path", tmpdir):
            with pytest.raises(ConfigPatchError):
                await replace_provider_block(
                    "user_missing",
                    providers={},
                    default_model={"primary": "x", "fallbacks": []},
                    plugin_entries={},
                )


@pytest.mark.asyncio
async def test_replace_provider_block_creates_missing_intermediates(monkeypatch):
    """A legacy openclaw.json missing ``models`` / ``plugins`` entirely should
    still accept the swap — replace_provider_block populates the parent
    dicts as needed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_legacy")
        os.makedirs(user_dir)
        config = {"gateway": {"mode": "local"}, "agents": {"defaults": {}}}
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", tmpdir)
        await replace_provider_block(
            "user_legacy",
            providers={"amazon-bedrock": {"api": "bedrock-converse-stream"}},
            default_model={"primary": "amazon-bedrock/x", "fallbacks": []},
            plugin_entries={"amazon-bedrock": {"config": {"discovery": {"enabled": False}}}},
        )
        with open(os.path.join(tmpdir, "user_legacy", "openclaw.json")) as f:
            result = json.load(f)
    assert "amazon-bedrock" in result["models"]["providers"]
    assert "amazon-bedrock" in result["plugins"]["entries"]
    assert result["agents"]["defaults"]["model"]["primary"] == "amazon-bedrock/x"
