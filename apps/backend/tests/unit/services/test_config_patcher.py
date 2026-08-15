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
    delete_provider_level_channel_secrets,
    patch_openclaw_config,
    remove_from_openclaw_config_list,
    replace_provider_block,
    set_openclaw_config_path,
    strip_removed_agent_keys,
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
async def test_remove_cron_jobs_removes_only_the_named_ids(cron_efs_dir):
    """The deploy-unwind path: a failed deploy's appended jobs come back out
    by id, leaving every other job untouched."""
    from core.services.config_patcher import remove_cron_jobs

    jobs_path = os.path.join(cron_efs_dir, "user_1", "cron", "jobs.json")
    os.makedirs(os.path.dirname(jobs_path), exist_ok=True)
    with open(jobs_path, "w") as f:
        json.dump(
            {
                "version": 1,
                "jobs": [
                    {"id": "keep", "agentId": "main", "name": "Keep me"},
                    {"id": "j1", "agentId": "agent_abc", "name": "From deploy"},
                    {"id": "j2", "agentId": "agent_abc", "name": "From deploy too"},
                ],
            },
            f,
        )

    await remove_cron_jobs("user_1", ["j1", "j2"])

    with open(jobs_path) as f:
        data = json.load(f)
    assert [j["id"] for j in data["jobs"]] == ["keep"]


@pytest.mark.asyncio
async def test_remove_cron_jobs_missing_file_and_ids_are_noops(cron_efs_dir):
    from core.services.config_patcher import remove_cron_jobs

    jobs_path = os.path.join(cron_efs_dir, "user_1", "cron", "jobs.json")
    # No file → no-op, and it must not create one.
    await remove_cron_jobs("user_1", ["j1"])
    assert not os.path.exists(jobs_path)
    # Empty id list → no-op.
    await remove_cron_jobs("user_1", [])
    assert not os.path.exists(jobs_path)


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
                    {"id": "main", "default": True, "workspace": "/home/node/.openclaw/workspace-main"},
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
    The provider-independent bundled extensions persist, so entries is the
    workflow + memory seam exactly (no lingering amazon-bedrock)."""
    from core.containers.config import memory_plugin_entries, workflow_plugin_entries

    await replace_provider_block(
        "user_p",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
    )
    with open(os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")) as f:
        result = json.load(f)
    assert result["models"]["providers"] == {}
    # amazon-bedrock dropped; bundled extensions survive the wholesale replace.
    assert result["plugins"]["entries"] == {
        **workflow_plugin_entries(),
        **memory_plugin_entries(),
    }
    assert "amazon-bedrock" not in result["plugins"]["entries"]
    assert result["agents"]["defaults"]["model"]["primary"] == "openai/gpt-5.5"


@pytest.mark.asyncio
async def test_replace_provider_block_preserves_workflow_plugins(tmp_efs_with_bedrock_provider):
    """A provider swap must not drop lobster / llm-task. They are bundled
    workflow extensions enabled at provision time and are independent of the
    chosen LLM provider; losing them on a Bedrock ↔ ChatGPT switch would
    silently break workflows until a full reprovision."""
    await replace_provider_block(
        "user_p",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
    )
    with open(os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")) as f:
        entries = json.load(f)["plugins"]["entries"]
    assert entries["lobster"] == {"enabled": True}
    assert entries["llm-task"] == {"enabled": True}


@pytest.mark.asyncio
async def test_replace_provider_block_preserves_unrelated_subtrees(tmp_efs_with_bedrock_provider):
    """The swap touches exactly three subtrees; agents.list, agents.defaults
    .workspace, gateway, tools, etc must survive untouched. Catalog deploys
    write to agents.list — a replace that nuked siblings would lose deployed
    agents on every provider switch."""
    await replace_provider_block(
        "user_p",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
    )
    with open(os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")) as f:
        result = json.load(f)
    # agents.list (catalog-deployed agents) preserved.
    assert result["agents"]["list"] == [
        {"id": "main", "default": True, "workspace": "/home/node/.openclaw/workspace-main"},
    ]
    # agents.defaults.workspace preserved (only .model swapped).
    assert result["agents"]["defaults"]["workspace"] == "/home/node/.openclaw/workspaces"
    # Top-level siblings preserved.
    assert result["gateway"] == {"mode": "local"}
    assert result["tools"] == {"profile": "full"}


@pytest.mark.asyncio
async def test_replace_provider_block_swaps_acp_and_seeds_acpx(tmp_efs_with_bedrock_provider):
    """The harness set follows the provider: passing acp_block/acpx_entry
    replaces the root acp block wholesale (a stale claude allowedAgents on a
    bedrock → chatgpt swap would advertise an unauthable harness) and the
    acpx entry survives the wholesale plugins.entries rebuild — without the
    param it is DROPPED, stranding the harness until a cold reprovision."""
    from core.containers.config import build_acp_config

    acp_block, acpx_entry = build_acp_config(["opencode"])
    await replace_provider_block(
        "user_p",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
        acp_block=acp_block,
        acpx_entry=acpx_entry,
    )
    with open(os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")) as f:
        result = json.load(f)
    assert result["acp"]["allowedAgents"] == ["opencode"]
    assert result["acp"]["defaultAgent"] == "opencode"
    acpx = result["plugins"]["entries"]["acpx"]
    assert acpx["enabled"] is True
    assert acpx["config"]["agents"]["opencode"]["command"] == "env"


@pytest.mark.asyncio
async def test_replace_provider_block_preserves_memory_core(tmp_efs_with_bedrock_provider):
    """A provider swap must not drop memory-core. Like lobster/llm-task it is
    a bundled extension enabled at provision time and is independent of the
    chosen LLM provider; losing it on a Bedrock <-> ChatGPT switch would
    silently disable dreaming until a full reprovision."""
    from core.containers.config import memory_plugin_entries

    await replace_provider_block(
        "user_p",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
    )
    with open(os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")) as f:
        entries = json.load(f)["plugins"]["entries"]
    assert entries["memory-core"] == memory_plugin_entries()["memory-core"]
    assert entries["memory-core"]["config"]["dreaming"]["enabled"] is True
    assert entries["memory-core"]["config"]["dreaming"]["phases"]["deep"] == {"enabled": False}


@pytest.mark.asyncio
async def test_replace_provider_block_leaves_memory_search_enabled(tmp_efs_with_bedrock_provider):
    """A provider swap must NOT force memorySearch off. Recall is intentionally
    on (memory-core registers memory_search/memory_get and the "Memory Recall"
    prompt section only renders while those tools exist), and the embeddings
    store stays on EFS — live logs show no sqlite contention. So a config with
    ``memorySearch.enabled: true`` keeps it after the swap."""
    from core.containers.config import memory_plugin_entries

    # On-disk shape: the FTS-only value a bedrock/chatgpt owner is provisioned
    # with (both hot-path providers resolve to provider:"none"). The swap must
    # leave it byte-for-byte intact — memorySearch is provider-derived at
    # provision time, never rewritten by the hot patch.
    config_path = os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")
    with open(config_path) as f:
        cfg = json.load(f)
    cfg["agents"]["defaults"]["memorySearch"] = {
        "enabled": True,
        "provider": "none",
    }
    with open(config_path, "w") as f:
        json.dump(cfg, f)

    await replace_provider_block(
        "user_p",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
    )
    with open(config_path) as f:
        result = json.load(f)

    # The swap leaves memorySearch at its provisioned value — recall stays on.
    assert result["agents"]["defaults"]["memorySearch"]["enabled"] is True
    assert "memory-core" in result["plugins"]["entries"]
    assert result["plugins"]["entries"]["memory-core"] == memory_plugin_entries()["memory-core"]
    # The provider-derived value survives untouched (no model key on FTS-only).
    assert result["agents"]["defaults"]["memorySearch"]["provider"] == "none"
    assert "model" not in result["agents"]["defaults"]["memorySearch"]


@pytest.mark.asyncio
async def test_replace_provider_block_swaps_chatgpt_for_bedrock():
    """ChatGPT OAuth → Bedrock: starting from an empty providers dict +
    empty plugin entries, the bedrock block must land cleanly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_c")
        os.makedirs(user_dir)
        config = {
            "models": {"providers": {}},
            "agents": {"defaults": {"model": {"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]}}},
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
                ceo_model="amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
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
                    ceo_model=None,
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
            ceo_model="amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
        )
        with open(os.path.join(tmpdir, "user_legacy", "openclaw.json")) as f:
            result = json.load(f)
    assert "amazon-bedrock" in result["models"]["providers"]
    assert "amazon-bedrock" in result["plugins"]["entries"]
    assert result["agents"]["defaults"]["model"]["primary"] == "amazon-bedrock/x"


@pytest.fixture
def tmp_efs_with_ceo_premium_model(monkeypatch):
    """Owner whose openclaw.json has a bedrock_claude provider block AND a CEO
    (``default: true``) agent pinned to the premium Opus tier via a per-agent
    ``model`` override — exactly what _build_agents_list(ceo_model=…) writes at
    provision time. The bedrock → chatgpt switch must strip this stale ref."""
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_ceo")
        os.makedirs(user_dir)
        config = {
            "models": {
                "providers": {
                    "amazon-bedrock": {"api": "bedrock-converse-stream"},
                },
            },
            "agents": {
                "defaults": {
                    "model": {
                        "primary": "amazon-bedrock/us.anthropic.claude-sonnet-4-6",
                        "fallbacks": ["amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"],
                    },
                    "workspace": "/home/node/.openclaw/workspaces",
                },
                "list": [
                    {
                        "id": "agent_friday",
                        "default": True,
                        "model": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
                        "workspace": "/home/node/.openclaw/workspace-agent_friday",
                    },
                    {
                        "id": "agent_atlas",
                        "workspace": "/home/node/.openclaw/workspace-agent_atlas",
                    },
                ],
            },
            "plugins": {"entries": {"amazon-bedrock": {"config": {"discovery": {"enabled": False}}}}},
        }
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", tmpdir)
        yield tmpdir


@pytest.mark.asyncio
async def test_replace_provider_block_strips_ceo_model_when_none(tmp_efs_with_ceo_premium_model):
    """Bedrock → ChatGPT OAuth (ceo_model=None): the CEO's stale per-agent
    amazon-bedrock Opus ref MUST be deleted so Friday inherits the new
    flat-rate default. Leaving it would leave Friday pointing at a provider
    that's no longer in models.providers — Friday's chats break."""
    await replace_provider_block(
        "user_ceo",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
    )
    with open(os.path.join(tmp_efs_with_ceo_premium_model, "user_ceo", "openclaw.json")) as f:
        result = json.load(f)
    agents = result["agents"]["list"]
    friday = next(a for a in agents if a.get("default") is True)
    assert "model" not in friday, "stale amazon-bedrock CEO ref must be removed"
    # Non-default agent untouched (it never had a model, still doesn't).
    atlas = next(a for a in agents if a["id"] == "agent_atlas")
    assert "model" not in atlas


@pytest.mark.asyncio
async def test_replace_provider_block_sets_ceo_model_when_provided(tmp_efs_with_bedrock_provider):
    """ChatGPT OAuth → Bedrock (ceo_model=<bedrock Opus>): the CEO must get the
    premium per-agent override so Friday runs on Opus, not the cheap Sonnet
    default. The fixture's default agent starts with no ``model``."""
    ceo_model = "amazon-bedrock/us.anthropic.claude-opus-4-6-v1"
    # Add a non-default sibling so we can assert it stays untouched.
    config_path = os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")
    with open(config_path) as f:
        cfg = json.load(f)
    cfg["agents"]["list"].append({"id": "agent_atlas", "workspace": "/home/node/.openclaw/workspace-agent_atlas"})
    with open(config_path, "w") as f:
        json.dump(cfg, f)

    await replace_provider_block(
        "user_p",
        providers={"amazon-bedrock": {"api": "bedrock-converse-stream"}},
        default_model={
            "primary": "amazon-bedrock/us.anthropic.claude-sonnet-4-6",
            "fallbacks": ["amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"],
        },
        plugin_entries={"amazon-bedrock": {"config": {"discovery": {"enabled": False}}}},
        ceo_model=ceo_model,
    )
    with open(config_path) as f:
        result = json.load(f)
    agents = result["agents"]["list"]
    friday = next(a for a in agents if a.get("default") is True)
    assert friday["model"] == ceo_model
    # Non-default agent never gets a per-agent model override.
    atlas = next(a for a in agents if a["id"] == "agent_atlas")
    assert "model" not in atlas


@pytest.mark.asyncio
async def test_replace_provider_block_ceo_model_noop_when_no_agents_list(monkeypatch):
    """A config with no ``agents.list`` (legacy / unseeded) accepts the swap
    without erroring — the CEO-model step is a guarded no-op."""
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_no_list")
        os.makedirs(user_dir)
        config = {"agents": {"defaults": {}}}
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", tmpdir)
        await replace_provider_block(
            "user_no_list",
            providers={},
            default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
            plugin_entries={},
            ceo_model=None,
        )
        with open(os.path.join(tmpdir, "user_no_list", "openclaw.json")) as f:
            result = json.load(f)
    assert "list" not in result["agents"]


# ---------------------------------------------------------------------------
# replace_provider_block — aux-model (compaction / memoryFlush / heartbeat)
# overrides must also track the new provider on a hot swap, same as the CEO
# model. Otherwise a Bedrock → ChatGPT swap leaves stale amazon-bedrock/...
# refs under agents.defaults pointing at a provider no longer in
# models.providers.
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_efs_with_aux_model_overrides(monkeypatch):
    """Owner whose openclaw.json carries the aux-model overrides provisioning
    writes for a Bedrock owner: ``agents.defaults.compaction.model``,
    ``compaction.memoryFlush.model``, and ``heartbeat.model`` — all pinned to
    the cheap Bedrock Haiku tier. The Bedrock → flat-rate swap (cheap_model=None)
    must DELETE these so the aux slots inherit the new default. Sibling keys
    (``heartbeat.every``, ``compaction.memoryFlush.enabled``) must survive."""
    haiku = "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_aux")
        os.makedirs(user_dir)
        config = {
            "models": {
                "providers": {"amazon-bedrock": {"api": "bedrock-converse-stream"}},
            },
            "agents": {
                "defaults": {
                    "model": {
                        "primary": "amazon-bedrock/us.anthropic.claude-sonnet-4-6",
                        "fallbacks": [haiku],
                    },
                    "workspace": "/home/node/.openclaw/workspaces",
                    "compaction": {
                        "model": haiku,
                        "memoryFlush": {"model": haiku, "enabled": True},
                    },
                    "heartbeat": {"every": "30m", "model": haiku},
                },
                "list": [
                    {"id": "main", "default": True, "workspace": "/home/node/.openclaw/workspace-main"},
                ],
            },
            "plugins": {"entries": {"amazon-bedrock": {"config": {"discovery": {"enabled": False}}}}},
        }
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)
        monkeypatch.setattr("core.services.config_patcher._efs_mount_path", tmpdir)
        yield tmpdir


@pytest.mark.asyncio
async def test_replace_provider_block_strips_aux_models_when_cheap_model_none(
    tmp_efs_with_aux_model_overrides,
):
    """Bedrock → ChatGPT OAuth (cheap_model=None): the three aux-model overrides
    holding stale amazon-bedrock Haiku refs MUST be removed so the aux slots
    inherit the new flat-rate default. Sibling keys (heartbeat.every,
    memoryFlush.enabled) must be preserved."""
    await replace_provider_block(
        "user_aux",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
        cheap_model=None,
    )
    with open(os.path.join(tmp_efs_with_aux_model_overrides, "user_aux", "openclaw.json")) as f:
        defaults = json.load(f)["agents"]["defaults"]

    # The three aux model overrides are gone.
    assert "model" not in defaults["compaction"]
    assert "model" not in defaults["compaction"]["memoryFlush"]
    assert "model" not in defaults["heartbeat"]
    # Sibling keys preserved — we must not clobber the rest of the slot.
    assert defaults["heartbeat"]["every"] == "30m"
    assert defaults["compaction"]["memoryFlush"]["enabled"] is True


@pytest.mark.asyncio
async def test_replace_provider_block_sets_aux_models_when_cheap_model_provided(
    tmp_efs_with_bedrock_provider,
):
    """ChatGPT OAuth → Bedrock (cheap_model=<bedrock Haiku>): the compaction
    slots must be (re)pinned to the cheap tier, and heartbeat.model rides its
    own heartbeat_model parameter (the heartbeat is an ordinary turn, split
    from the compaction lane — see replace_provider_block docstring). The
    fixture starts with no aux model overrides at all, so the slot dicts must
    be created."""
    cheap_model = "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
    await replace_provider_block(
        "user_p",
        providers={"amazon-bedrock": {"api": "bedrock-converse-stream"}},
        default_model={
            "primary": "amazon-bedrock/us.anthropic.claude-sonnet-4-6",
            "fallbacks": [cheap_model],
        },
        plugin_entries={"amazon-bedrock": {"config": {"discovery": {"enabled": False}}}},
        ceo_model="amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
        cheap_model=cheap_model,
        heartbeat_model=cheap_model,
    )
    with open(os.path.join(tmp_efs_with_bedrock_provider, "user_p", "openclaw.json")) as f:
        defaults = json.load(f)["agents"]["defaults"]

    assert defaults["compaction"]["model"] == cheap_model
    assert defaults["compaction"]["memoryFlush"]["model"] == cheap_model
    assert defaults["heartbeat"]["model"] == cheap_model


@pytest.mark.asyncio
async def test_apply_paperclip_meta_creates_params_paperclip_block(tmp_path, monkeypatch):
    """When the agent entry has no params yet, the helper creates the
    nested structure and writes the full meta block."""
    from core.services import config_patcher

    config_dir = tmp_path / "user_x"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    config_path.write_text('{"agents": {"list": [{"id": "agent_abc", "name": "Ray"}]}}')
    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    await config_patcher.apply_paperclip_meta(
        "user_x",
        agent_id="agent_abc",
        meta={"role": "engineer", "title": "Backend dev", "capabilities": "Writes Python."},
    )

    written = json.loads(config_path.read_text())
    agent = next(a for a in written["agents"]["list"] if a["id"] == "agent_abc")
    assert agent["params"]["paperclip"]["role"] == "engineer"
    assert agent["params"]["paperclip"]["title"] == "Backend dev"
    assert agent["params"]["paperclip"]["capabilities"] == "Writes Python."


@pytest.mark.asyncio
async def test_apply_paperclip_meta_merges_with_existing_block(tmp_path, monkeypatch):
    """Partial update: existing keys preserved, supplied keys overwritten."""
    from core.services import config_patcher

    config_dir = tmp_path / "user_x"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    config_path.write_text(
        '{"agents": {"list": [{"id": "agent_abc", "params": {"paperclip": '
        '{"role": "engineer", "title": "Original", "icon": "terminal"}}}]}}'
    )
    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    await config_patcher.apply_paperclip_meta(
        "user_x",
        agent_id="agent_abc",
        meta={"title": "Updated"},
    )

    written = json.loads(config_path.read_text())
    pc = written["agents"]["list"][0]["params"]["paperclip"]
    # title updated, role + icon preserved
    assert pc["title"] == "Updated"
    assert pc["role"] == "engineer"
    assert pc["icon"] == "terminal"


@pytest.mark.asyncio
async def test_apply_paperclip_meta_preserves_other_agent_entries(tmp_path, monkeypatch):
    """A meta update for agent_abc must not touch agent_xyz."""
    from core.services import config_patcher

    config_dir = tmp_path / "user_x"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    config_path.write_text(
        '{"agents": {"list": ['
        '{"id": "agent_abc", "name": "A"},'
        '{"id": "agent_xyz", "name": "X", "params": {"paperclip": {"role": "cmo"}}}'
        "]}}"
    )
    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    await config_patcher.apply_paperclip_meta(
        "user_x",
        agent_id="agent_abc",
        meta={"role": "engineer"},
    )

    written = json.loads(config_path.read_text())
    by_id = {a["id"]: a for a in written["agents"]["list"]}
    assert by_id["agent_abc"]["params"]["paperclip"]["role"] == "engineer"
    # xyz untouched
    assert by_id["agent_xyz"]["params"]["paperclip"]["role"] == "cmo"


@pytest.mark.asyncio
async def test_apply_paperclip_meta_noop_when_agent_id_missing(tmp_path, monkeypatch):
    """If the agent_id isn't in agents.list (e.g. user deleted it
    concurrently), the helper skips the write entirely — no orphan
    entry gets created, no exception raised."""
    from core.services import config_patcher

    config_dir = tmp_path / "user_x"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    original = '{"agents": {"list": [{"id": "agent_abc"}]}}'
    config_path.write_text(original)
    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    await config_patcher.apply_paperclip_meta(
        "user_x",
        agent_id="agent_phantom",
        meta={"role": "engineer"},
    )

    # File untouched (no agent_phantom entry created).
    assert config_path.read_text() == original


@pytest.mark.asyncio
async def test_apply_paperclip_meta_clears_field_when_value_is_empty_string(tmp_path, monkeypatch):
    """Codex P1 round 2: the UI sends every meta field as a trimmed
    string on save, including ``""`` to clear. The merge must drop
    title/capabilities/icon keys whose value is empty so the on-disk
    block actually loses the previously-set value."""
    from core.services import config_patcher

    config_dir = tmp_path / "user_x"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    config_path.write_text(
        '{"agents": {"list": [{"id": "agent_abc", "params": {"paperclip": '
        '{"role": "engineer", "title": "Original", "capabilities": "Old caps", '
        '"icon": "terminal"}}}]}}'
    )
    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    await config_patcher.apply_paperclip_meta(
        "user_x",
        agent_id="agent_abc",
        meta={"role": "engineer", "title": "", "capabilities": "   ", "icon": ""},
    )

    written = json.loads(config_path.read_text())
    pc = written["agents"]["list"][0]["params"]["paperclip"]
    # role preserved, the three clearable fields dropped entirely.
    assert pc["role"] == "engineer"
    assert "title" not in pc
    assert "capabilities" not in pc
    assert "icon" not in pc


@pytest.mark.asyncio
async def test_strip_removed_agent_keys_scrubs_defaults_and_list_entries():
    """Pre-roll migration: removed-upstream keys are stripped from
    agents.defaults AND every agents.list[] entry (the latter can carry them
    from an older marketplace deploy); valid keys are preserved, nested
    structures are untouched, and a second pass is a no-op."""
    removed = ("embeddedHarness", "embeddedPi", "agentRuntime", "systemPromptOverride")
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "owner_1")
        os.makedirs(user_dir)
        config = {
            "agents": {
                "defaults": {
                    "model": {"primary": "amazon-bedrock/x"},
                    "embeddedHarness": {},
                    "contextLimits": {},
                },
                "list": [
                    {
                        "id": "agent_a",
                        "skills": ["search"],
                        "embeddedHarness": {},
                        "agentRuntime": {"maxTurns": 50},
                        "systemPromptOverride": "be terse",
                        "models": {"x": {"agentRuntime": {"maxTurns": 9}}},
                    }
                ],
            }
        }
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)

        with patch("core.services.config_patcher._efs_mount_path", tmpdir):
            await strip_removed_agent_keys("owner_1", defaults_keys=("embeddedHarness",), entry_keys=removed)
            # second pass is a no-op (idempotent)
            await strip_removed_agent_keys("owner_1", defaults_keys=("embeddedHarness",), entry_keys=removed)

        with open(os.path.join(user_dir, "openclaw.json")) as f:
            result = json.load(f)

    defaults = result["agents"]["defaults"]
    assert "embeddedHarness" not in defaults
    assert defaults["model"] == {"primary": "amazon-bedrock/x"}
    assert defaults["contextLimits"] == {}

    entry = result["agents"]["list"][0]
    assert "embeddedHarness" not in entry
    assert "agentRuntime" not in entry
    assert "systemPromptOverride" not in entry
    assert entry["skills"] == ["search"]
    # nested models[*].agentRuntime is not a top-level entry key — left intact
    assert entry["models"]["x"]["agentRuntime"] == {"maxTurns": 9}


# --- delete_provider_level_channel_secrets -----------------------------------
# Provider-level token fields (channels.<p>.botToken / appToken / token /
# signingSecret) are legacy orphans: OpenClaw reads them as a synthetic
# "[default]" account that auths and crash-loops on invalid creds. The current
# wizard only ever writes tokens under accounts.<id>, so any provider-level
# token is stale and must be strippable without touching real accounts.


def _write_config(efs_dir, owner, config):
    with open(os.path.join(efs_dir, owner, "openclaw.json"), "w") as f:
        json.dump(config, f)


@pytest.mark.asyncio
async def test_strip_provider_secrets_removes_orphan_tokens_keeps_accounts(efs_dir):
    _write_config(
        efs_dir,
        "user_1",
        {
            "channels": {
                "slack": {
                    "enabled": True,
                    "dmPolicy": "pairing",
                    "mode": "socket",
                    "appToken": "xapp-orphan",
                    "botToken": "xoxb-orphan",
                    "accounts": {"agent_x": {"botToken": "xoxb-real", "appToken": "xapp-real"}},
                },
                "telegram": {"enabled": True, "dmPolicy": "pairing", "botToken": "tg-orphan"},
            }
        },
    )
    await delete_provider_level_channel_secrets("user_1", "slack")
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    slack = result["channels"]["slack"]
    # provider-level secrets stripped
    assert "botToken" not in slack
    assert "appToken" not in slack
    # non-secret config + the real account preserved (enabled stays true by design)
    assert slack["enabled"] is True
    assert slack["dmPolicy"] == "pairing"
    assert slack["accounts"]["agent_x"]["botToken"] == "xoxb-real"
    assert slack["accounts"]["agent_x"]["appToken"] == "xapp-real"
    # other providers are untouched (per-provider operation)
    assert result["channels"]["telegram"]["botToken"] == "tg-orphan"


@pytest.mark.asyncio
async def test_strip_provider_secrets_is_noop_when_absent(efs_dir):
    _write_config(
        efs_dir,
        "user_1",
        {"channels": {"telegram": {"enabled": True, "dmPolicy": "pairing", "accounts": {}}}},
    )
    # No provider-level secrets present -> must not error, must not create a .bak.
    await delete_provider_level_channel_secrets("user_1", "telegram")
    assert not os.path.exists(os.path.join(efs_dir, "user_1", "openclaw.json.bak"))
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert result["channels"]["telegram"] == {"enabled": True, "dmPolicy": "pairing", "accounts": {}}


@pytest.mark.asyncio
async def test_strip_provider_secrets_strips_discord_token_and_signing_secret(efs_dir):
    _write_config(
        efs_dir,
        "user_1",
        {
            "channels": {
                "discord": {"enabled": True, "token": "disc-orphan"},
                "slack": {"enabled": True, "signingSecret": "sig-orphan", "botToken": "xoxb-orphan"},
            }
        },
    )
    await delete_provider_level_channel_secrets("user_1", "discord")
    await delete_provider_level_channel_secrets("user_1", "slack")
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert "token" not in result["channels"]["discord"]
    assert result["channels"]["discord"]["enabled"] is True
    assert "signingSecret" not in result["channels"]["slack"]
    assert "botToken" not in result["channels"]["slack"]


# ---------------------------------------------------------------------------
# P3: prototype-pollution / unsafe config-key hardening in _deep_merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_rejects_proto_pollution_top_level_key(efs_dir):
    """A ``__proto__`` top-level key (stored via the pen-test fleet write) is
    rejected at the merge boundary."""
    with pytest.raises(ConfigPatchError):
        await patch_openclaw_config("user_1", {"__proto__": {"polluted": True}})


@pytest.mark.asyncio
async def test_patch_rejects_nested_unsafe_key(efs_dir):
    """Recursion covers nested keys too (e.g. ``constructor`` under a legit key)."""
    with pytest.raises(ConfigPatchError):
        await patch_openclaw_config("user_1", {"agents": {"constructor": {"x": 1}}})


@pytest.mark.asyncio
async def test_patch_rejects_control_char_key(efs_dir):
    with pytest.raises(ConfigPatchError):
        await patch_openclaw_config("user_1", {"bad\nkey": 1})


@pytest.mark.asyncio
async def test_patch_allows_legit_dotted_and_slashed_model_keys(efs_dir):
    """Real configs key by model id (dots, slashes, hyphens) and caller-chosen
    MCP server names — these must NOT be rejected (we block bad keys, not an
    allowlist)."""
    await patch_openclaw_config(
        "user_1",
        {
            "agents": {"defaults": {"models": {"amazon-bedrock/qwen.qwen3-vl-235b-a22b": {"alias": "Q"}}}},
            "mcp": {"servers": {"my-custom-server.v2": {"command": "npx", "args": []}}},
        },
    )
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        result = json.load(f)
    assert "amazon-bedrock/qwen.qwen3-vl-235b-a22b" in result["agents"]["defaults"]["models"]
    assert "my-custom-server.v2" in result["mcp"]["servers"]


def test_assert_safe_patch_keys_unit():
    from core.services.config_patcher import _assert_safe_patch_keys

    # Clean keys pass.
    _assert_safe_patch_keys({"gateway": 1, "amazon-bedrock/claude.x-1": 2, "server_name": 3})
    # Each dangerous variant raises.
    for bad in ({"__proto__": 1}, {"constructor": 1}, {"prototype": 1}, {"__reduce__": 1}):
        with pytest.raises(ConfigPatchError):
            _assert_safe_patch_keys(bad)
    with pytest.raises(ConfigPatchError):
        _assert_safe_patch_keys({"x" * 200: 1})
    with pytest.raises(ConfigPatchError):
        _assert_safe_patch_keys({"tab\tkey": 1})


@pytest.mark.asyncio
async def test_patch_rejects_unsafe_key_in_inserted_subtree(efs_dir):
    """Regression: a patch that INSERTS a subtree absent from the base config
    (so the merge never recurses into it) must still have its nested keys
    validated — otherwise the proto-pollution guard is bypassed."""
    # base fixture has no top-level "plugins" → this whole subtree is inserted.
    with pytest.raises(ConfigPatchError):
        await patch_openclaw_config("user_1", {"plugins": {"entries": {"x": {"__proto__": {}}}}})


@pytest.mark.asyncio
async def test_patch_rejects_unsafe_key_in_list_item(efs_dir):
    """Dicts carried inside list entries (plugin/agent lists) are validated too."""
    with pytest.raises(ConfigPatchError):
        await patch_openclaw_config("user_1", {"somelist": [{"ok": 1}, {"constructor": {}}]})


def test_assert_safe_patch_keys_recurses_into_inserted_and_list_values():
    from core.services.config_patcher import _assert_safe_patch_keys

    # Deeply nested unsafe key (no existing base to merge against).
    with pytest.raises(ConfigPatchError):
        _assert_safe_patch_keys({"a": {"b": {"c": {"__proto__": 1}}}})
    # Unsafe key inside a list of dicts.
    with pytest.raises(ConfigPatchError):
        _assert_safe_patch_keys({"list": [{"clean": 1}, {"prototype": 2}]})
    # Clean nested structure (incl. list of dicts) passes.
    _assert_safe_patch_keys({"a": {"b": [{"id": "x", "model": "y"}]}, "mcp": {"servers": {"s.v2": {}}}})


# ---------------------------------------------------------------------------
# replace_provider_block — provider hot-swap (Bedrock ↔ ChatGPT OAuth)
# ---------------------------------------------------------------------------

_HAIKU_ARN = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku"
_SONNET_ARN = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet"
_OPUS_ARN = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus"


@pytest.fixture
def bedrock_efs_dir():
    """An owner provisioned on paid Bedrock: premium CEO pin, cheap-tier aux
    routing, pinned subagents/utility models, llm-task config — the full
    shape build_openclaw_config_dict writes, so the swap tests exercise every
    provider-dependent key the hot path must re-pin or clear."""
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_1")
        os.makedirs(user_dir)
        config = {
            "gateway": {"mode": "local", "bind": "lan"},
            "models": {
                "providers": {"amazon-bedrock": {"models": []}},
                "pricing": {"enabled": False},
            },
            "agents": {
                "defaults": {
                    "model": {
                        "primary": f"amazon-bedrock/{_SONNET_ARN}",
                        "fallbacks": [f"amazon-bedrock/{_HAIKU_ARN}"],
                    },
                    "utilityModel": f"amazon-bedrock/{_HAIKU_ARN}",
                    "compaction": {
                        "mode": "safeguard",
                        "midTurnPrecheck": {"enabled": True},
                        "model": f"amazon-bedrock/{_HAIKU_ARN}",
                        "memoryFlush": {"model": f"amazon-bedrock/{_HAIKU_ARN}"},
                    },
                    "heartbeat": {
                        "every": "30m",
                        "isolatedSession": True,
                        "lightContext": True,
                        "model": f"amazon-bedrock/{_HAIKU_ARN}",
                    },
                    "subagents": {
                        "allowAgents": ["*"],
                        "model": f"amazon-bedrock/{_SONNET_ARN}",
                        "runTimeoutSeconds": 1800,
                    },
                },
                "list": [
                    {"id": "agent_friday", "default": True, "model": f"amazon-bedrock/{_OPUS_ARN}"},
                ],
            },
            "plugins": {
                "entries": {
                    "amazon-bedrock": {"config": {"discovery": {"enabled": False}}},
                    "lobster": {"enabled": True},
                    "llm-task": {
                        "enabled": True,
                        "config": {"defaultProvider": "amazon-bedrock", "defaultModel": _HAIKU_ARN},
                    },
                    "aws-exec-profile": {"enabled": True},
                    "memory-core": {"enabled": True},
                },
            },
            "tools": {
                "profile": "full",
                "web": {
                    "fetch": {"enabled": True, "provider": "firecrawl"},
                    "search": {"provider": "parallel-free"},
                },
                "toolSearch": {"mode": "code"},
            },
        }
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)

        with patch("core.services.config_patcher._efs_mount_path", tmpdir):
            yield tmpdir


def _read_config(tmpdir):
    with open(os.path.join(tmpdir, "user_1", "openclaw.json")) as f:
        return json.load(f)


@pytest.mark.asyncio
async def test_replace_provider_block_to_oauth_repins_all_provider_keys(bedrock_efs_dir):
    """Bedrock → ChatGPT OAuth: every provider-dependent ref must be re-pinned
    or cleared — a stale amazon-bedrock/… ref points at a provider no longer
    in models.providers and fails every call it governs."""
    await replace_provider_block(
        "user_1",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
        cheap_model=None,
        heartbeat_model="openai/gpt-5.4-mini",
        utility_model="openai/gpt-5.4-mini",
        subagent_model="openai/gpt-5.5",
        llm_task_model=None,
        web_search_provider="parallel-free",
        tool_search_mode=None,
    )
    config = _read_config(bedrock_efs_dir)
    defaults = config["agents"]["defaults"]

    # Aux compaction slots cleared (Codex owns compaction — a pinned override
    # makes compaction fail and the session overflow), midTurnPrecheck kept.
    assert "model" not in defaults["compaction"]
    assert "model" not in defaults["compaction"]["memoryFlush"]
    assert defaults["compaction"]["midTurnPrecheck"] == {"enabled": True}

    # Heartbeat is an ordinary turn the OAuth token CAN serve on mini — it is
    # re-pinned, NOT deleted with the compaction slots. Siblings survive.
    assert defaults["heartbeat"]["model"] == "openai/gpt-5.4-mini"
    assert defaults["heartbeat"]["every"] == "30m"
    assert defaults["heartbeat"]["isolatedSession"] is True

    assert defaults["utilityModel"] == "openai/gpt-5.4-mini"
    assert defaults["subagents"]["model"] == "openai/gpt-5.5"
    assert defaults["subagents"]["allowAgents"] == ["*"]
    assert defaults["subagents"]["runTimeoutSeconds"] == 1800

    # CEO premium override cleared on the flat-rate path.
    assert "model" not in config["agents"]["list"][0]

    # llm-task drops its bedrock config but stays enabled; the other bundled
    # entries persist; the old provider entry is gone.
    entries = config["plugins"]["entries"]
    assert entries["llm-task"] == {"enabled": True}
    assert entries["lobster"] == {"enabled": True}
    assert entries["aws-exec-profile"] == {"enabled": True}
    assert "amazon-bedrock" not in entries

    # The parallel-free search pin SURVIVES the swap to ChatGPT OAuth, and
    # fetch config survives alongside it. billing_service used to pass None
    # here so "Codex-native hosted search" could engage — but these owners run
    # the EMBEDDED runtime, so that deleted a working pin and left them with no
    # web search at all.
    web = config["tools"]["web"]
    assert web["search"] == {"provider": "parallel-free"}
    assert web["fetch"] == {"enabled": True, "provider": "firecrawl"}

    # Stale toolSearch from the Bedrock provision must not survive onto the
    # Codex path: provision-time never writes it for chatgpt_oauth, and a
    # future swap back (or a runtime that stops ignoring the key) would hide
    # the entire catalog behind the bridge.
    assert "toolSearch" not in config["tools"]


@pytest.mark.asyncio
async def test_replace_provider_block_none_clears_utility_and_subagent_model(bedrock_efs_dir):
    """None for the new params must DELETE the keys (stale-ref hazard), never
    leave the old provider's refs behind."""
    await replace_provider_block(
        "user_1",
        providers={},
        default_model={"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
        plugin_entries={},
        ceo_model=None,
        cheap_model=None,
        heartbeat_model=None,
        utility_model=None,
        subagent_model=None,
        llm_task_model=None,
        web_search_provider=None,
    )
    defaults = _read_config(bedrock_efs_dir)["agents"]["defaults"]
    assert "utilityModel" not in defaults
    assert "model" not in defaults["subagents"]
    assert defaults["subagents"]["allowAgents"] == ["*"]  # siblings preserved
    assert "model" not in defaults["heartbeat"]


@pytest.mark.asyncio
async def test_replace_provider_block_creates_web_search_path_when_missing(efs_dir):
    """A config missing `tools` entirely (ancient/hand-edited EFS state) must
    still come out of an embedded-runtime swap with the search pin created —
    parity with the heartbeat/subagents slot creation."""
    await replace_provider_block(
        "user_1",
        providers={"amazon-bedrock": {"api": "bedrock-converse-stream"}},
        default_model={"primary": "amazon-bedrock/x", "fallbacks": []},
        plugin_entries={},
        ceo_model=None,
        cheap_model=None,
        web_search_provider="parallel-free",
    )
    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        cfg = json.load(f)
    assert cfg["tools"]["web"]["search"] == {"provider": "parallel-free"}


@pytest.mark.asyncio
async def test_concurrent_writers_serialize_and_survive_inode_swap(efs_dir):
    """The thread gate plus sidecar protocol must survive data-inode swaps.

    With the old no-op lock (fcntl on the swapped data inode, no thread gate),
    concurrent read-modify-writes dropped each other's appends.
    """
    import asyncio

    n = 50
    await asyncio.gather(*(append_to_openclaw_config_list("user_1", ["plugins", "list"], {"id": i}) for i in range(n)))

    with open(os.path.join(efs_dir, "user_1", "openclaw.json")) as f:
        cfg = json.load(f)
    got = sorted(entry["id"] for entry in cfg["plugins"]["list"])
    assert got == list(range(n)), f"lost concurrent appends: {len(got)}/{n} landed"
    # Phase-one rollout preserves the legacy persistent sidecar until boot
    # recovery publishes the protocol marker. This keeps backend mutations
    # available while every backend replica learns the recovery guard.
    assert os.path.isfile(os.path.join(efs_dir, "user_1", "openclaw.json.lock"))
    assert os.path.exists(os.path.join(efs_dir, "user_1", "openclaw.json.lock.isol8-guard"))
