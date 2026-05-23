import pytest

from core.services.catalog_slice import (
    SkillBucket,
    classify_skill,
    extract_agent_slice,
    filter_cron_jobs_for_agent,
    strip_user_specific_fields,
)


FULL_OPENCLAW_JSON = {
    "defaultAgentId": "agent_abc",
    # Matches upstream OpenClaw schema (openclaw/src/config/zod-schema.agents.ts):
    # agents: { defaults?, list?: AgentEntry[] }
    "agents": {
        "defaults": {"workspace": "/workspace/root"},
        "list": [
            {
                "id": "agent_abc",
                "workspace": ".openclaw/workspaces/agent_abc",
                "name": "Pitch",
                "model": "qwen/qwen3-vl-235b",
                "thinkingDefault": True,
                "skills": ["web-search", "email-send"],
                "channels": {"telegram": {"bot_token": "SECRET"}},
                "cron": [{"schedule": "0 8 * * *", "workflow": "morning-briefing"}],
            },
            {"id": "agent_zzz", "name": "Other"},
        ],
    },
    "plugins": {"memory": {"enabled": True}},
    "tools": {"allowed": ["web-search", "email-send"]},
}


def test_extract_agent_slice_returns_only_named_agent():
    slice_ = extract_agent_slice(FULL_OPENCLAW_JSON, "agent_abc")
    assert slice_["agent"]["name"] == "Pitch"


def test_extract_agent_slice_includes_required_plugins_and_tools():
    slice_ = extract_agent_slice(FULL_OPENCLAW_JSON, "agent_abc")
    assert slice_["plugins"] == {"memory": {"enabled": True}}
    assert slice_["tools"] == {"allowed": ["web-search", "email-send"]}


def test_extract_agent_slice_missing_agent_raises():
    with pytest.raises(KeyError):
        extract_agent_slice(FULL_OPENCLAW_JSON, "agent_does_not_exist")


def test_extract_agent_slice_tolerates_non_dict_entries_in_agents_list():
    """Live prod regression: publish crashed with AttributeError when
    ``agents.list`` had a bare string alongside the dict entries."""
    cfg = {
        "agents": {
            "list": [
                "some-stray-string",
                {"id": "agent_abc", "name": "Pitch", "skills": ["web-search"]},
                None,
            ],
        },
        "plugins": {},
        "tools": {},
    }
    slice_ = extract_agent_slice(cfg, "agent_abc")
    assert slice_["agent"]["name"] == "Pitch"


def test_extract_agent_slice_raises_when_agents_missing_or_malformed():
    """Missing agents key, non-dict/non-list agents, or missing .list → empty."""
    for cfg in [
        {"plugins": {}, "tools": {}},
        {"agents": None, "plugins": {}, "tools": {}},
        {"agents": [], "plugins": {}, "tools": {}},  # empty flat list
        {"agents": {"defaults": {"workspace": "/x"}}, "plugins": {}, "tools": {}},
    ]:
        with pytest.raises(KeyError):
            extract_agent_slice(cfg, "agent_abc")


def test_extract_agent_slice_accepts_legacy_flat_list():
    """Regression: admins whose configs are still in the legacy flat
    shape (``agents: [...]``) must still be able to publish. The write path
    in config_patcher migrates the shape on deploy; the read path here
    tolerates it so we don't regress working admins while they wait for a
    migration write."""
    cfg = {
        "agents": [{"id": "agent_abc", "name": "Pitch", "skills": ["web-search"]}],
        "plugins": {"memory": {"enabled": True}},
        "tools": {"allowed": ["web-search"]},
    }
    slice_ = extract_agent_slice(cfg, "agent_abc")
    assert slice_["agent"]["name"] == "Pitch"
    assert slice_["plugins"] == {"memory": {"enabled": True}}


def test_strip_user_specific_fields_removes_model():
    agent = dict(FULL_OPENCLAW_JSON["agents"]["list"][0])
    cleaned = strip_user_specific_fields(agent)
    assert "model" not in cleaned


def test_strip_user_specific_fields_removes_channels():
    agent = dict(FULL_OPENCLAW_JSON["agents"]["list"][0])
    cleaned = strip_user_specific_fields(agent)
    assert "channels" not in cleaned


def test_strip_user_specific_fields_removes_workspace_path():
    agent = dict(FULL_OPENCLAW_JSON["agents"]["list"][0])
    cleaned = strip_user_specific_fields(agent)
    assert "workspace" not in cleaned


def test_strip_user_specific_fields_removes_id():
    agent = dict(FULL_OPENCLAW_JSON["agents"]["list"][0])
    cleaned = strip_user_specific_fields(agent)
    assert "id" not in cleaned


def test_strip_user_specific_fields_keeps_behavioral_flags():
    agent = dict(FULL_OPENCLAW_JSON["agents"]["list"][0])
    cleaned = strip_user_specific_fields(agent)
    assert cleaned["thinkingDefault"] is True
    assert cleaned["skills"] == ["web-search", "email-send"]
    assert cleaned["cron"] == [{"schedule": "0 8 * * *", "workflow": "morning-briefing"}]


def test_strip_user_specific_fields_removes_agentDir():
    """agentDir is keyed to the publisher's agent id. If carried into the slice,
    a self-deploy collides with the publisher's existing agent and trips
    OpenClaw's ``DuplicateAgentDirError`` (config rejected, deploy invisible)."""
    agent = {
        "id": "pulse",
        "name": "Pulse",
        "agentDir": "/home/node/.openclaw/agents/pulse/agent",
        "skills": ["web-search"],
    }
    cleaned = strip_user_specific_fields(agent)
    assert "agentDir" not in cleaned
    assert cleaned["skills"] == ["web-search"]


# ---- filter_cron_jobs_for_agent ----


def test_filter_cron_jobs_keeps_only_matching_agent():
    jobs = [
        {"id": "j1", "agentId": "agent_abc", "name": "Daily"},
        {"id": "j2", "agentId": "agent_xyz", "name": "Other"},
        {"id": "j3", "agentId": "agent_abc", "name": "Weekly"},
    ]
    out = filter_cron_jobs_for_agent(jobs, "agent_abc")
    assert [j["name"] for j in out] == ["Daily", "Weekly"]


def test_filter_cron_jobs_strips_runtime_and_user_specific_fields():
    """``id``, ``sessionKey``, ``state``, ``createdAtMs``, ``updatedAtMs``
    are regenerated at deploy and must not leak through the slice."""
    jobs = [
        {
            "id": "j1",
            "agentId": "agent_abc",
            "sessionKey": "agent:agent_abc:user_publisher",
            "name": "Daily",
            "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
            "payload": {"kind": "agentTurn", "message": "Run morning brief"},
            "delivery": {"mode": "announce", "channel": "telegram"},
            "createdAtMs": 1700000000000,
            "updatedAtMs": 1700000060000,
            "state": {"nextRunAtMs": 1700000120000, "lastRunAtMs": 1700000000000},
            "enabled": True,
        }
    ]
    [out] = filter_cron_jobs_for_agent(jobs, "agent_abc")
    assert "id" not in out
    assert "sessionKey" not in out
    assert "state" not in out
    assert "createdAtMs" not in out
    assert "updatedAtMs" not in out
    # Behavioral fields preserved.
    assert out["agentId"] == "agent_abc"
    assert out["name"] == "Daily"
    assert out["schedule"] == {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"}
    assert out["payload"]["message"] == "Run morning brief"
    assert out["delivery"] == {"mode": "announce", "channel": "telegram"}
    assert out["enabled"] is True


def test_filter_cron_jobs_skips_non_dict_entries():
    """Defensive: same posture as the agents-list reader. Stray strings or
    None entries from hand-edits don't crash; they're skipped."""
    jobs = [
        "stray-string",
        None,
        {"id": "j1", "agentId": "agent_abc", "name": "Keep"},
    ]
    out = filter_cron_jobs_for_agent(jobs, "agent_abc")
    assert [j["name"] for j in out] == ["Keep"]


def test_filter_cron_jobs_empty_input_returns_empty():
    assert filter_cron_jobs_for_agent([], "agent_abc") == []


# ---- classify_skill ----


def test_classify_skill_bucket_entries_when_in_skills_entries():
    cfg = {
        "skills": {
            "entries": {"jira": {"enabled": True, "apiKey": "secret"}},
            "allowBundled": ["search"],
        }
    }
    assert classify_skill("jira", cfg, workspace_skill_names=set()) is SkillBucket.ENTRIES


def test_classify_skill_bucket_bundled_when_in_allowBundled_only():
    cfg = {"skills": {"entries": {}, "allowBundled": ["search", "fs-read"]}}
    assert classify_skill("search", cfg, workspace_skill_names=set()) is SkillBucket.BUNDLED


def test_classify_skill_entries_wins_over_bundled():
    """A skill present in both should resolve to ENTRIES (config-registered
    is more specific than bundled-allowlist)."""
    cfg = {
        "skills": {
            "entries": {"search": {"apiKey": "key"}},
            "allowBundled": ["search"],
        }
    }
    assert classify_skill("search", cfg, workspace_skill_names=set()) is SkillBucket.ENTRIES


def test_classify_skill_workspace_when_in_workspace_dir():
    cfg = {"skills": {"entries": {}, "allowBundled": []}}
    assert classify_skill("custom-skill", cfg, workspace_skill_names={"custom-skill"}) is SkillBucket.WORKSPACE


def test_classify_skill_extra_dirs_when_in_load_extraDirs():
    cfg = {
        "skills": {
            "entries": {},
            "allowBundled": [],
            "load": {"extraDirs": ["/opt/openclaw/extra-skills"]},
        }
    }
    assert (
        classify_skill(
            "custom",
            cfg,
            workspace_skill_names=set(),
            extra_dir_skill_names={"custom"},
        )
        is SkillBucket.EXTRA_DIRS
    )


def test_classify_skill_unknown_when_nowhere():
    cfg = {"skills": {"entries": {}, "allowBundled": []}}
    assert classify_skill("ghost", cfg, workspace_skill_names=set()) is SkillBucket.UNKNOWN


def test_classify_skill_handles_missing_skills_section():
    cfg = {"agents": {"list": []}}
    assert classify_skill("anything", cfg, workspace_skill_names=set()) is SkillBucket.UNKNOWN


# ---- selection-aware extract_agent_slice (MCPs + skills) ----


CFG_WITH_MCPS_AND_SKILLS = {
    "agents": {
        "list": [
            {
                "id": "agent_abc",
                "name": "Pitch",
                "skills": ["jira", "search", "custom-skill"],
            },
        ]
    },
    "plugins": {},
    "tools": {},
    "mcp": {
        "servers": {
            "jira-mcp": {
                "command": "node",
                "args": ["jira-server.js"],
                "env": {"JIRA_TOKEN": "real-token-shhh", "JIRA_URL": "https://x"},
                "headers": {"Authorization": "Bearer xyz"},
            },
            "slack-mcp": {
                "command": "node",
                "args": ["slack.js"],
                "env": {"SLACK_TOKEN": "xoxb-shhh"},
            },
            "unused-mcp": {"command": "echo"},
        }
    },
    "skills": {
        "allowBundled": ["search", "fs-read"],
        "entries": {
            "jira": {
                "enabled": True,
                "apiKey": "atlassian-secret",
                "env": {"JIRA_USER": "alice@example.com"},
                "config": {"base_url": "https://x"},
            },
        },
    },
}


def test_extract_agent_slice_no_selections_omits_mcp_and_skills_keys():
    """Default (no selections) is backward-compatible: slice carries
    {agent, plugins, tools} only — same shape pre-existing deploys expect."""
    slice_ = extract_agent_slice(CFG_WITH_MCPS_AND_SKILLS, "agent_abc")
    assert "mcp" not in slice_
    assert "skills" not in slice_


def test_extract_agent_slice_with_selected_mcps_scrubs_env_and_headers():
    slice_ = extract_agent_slice(
        CFG_WITH_MCPS_AND_SKILLS,
        "agent_abc",
        selected_mcps=["jira-mcp"],
    )
    jira = slice_["mcp"]["servers"]["jira-mcp"]
    # Command + args ship intact (not secret).
    assert jira["command"] == "node"
    assert jira["args"] == ["jira-server.js"]
    # Every env value → "" (key names preserved so buyer sees what to fill).
    assert jira["env"] == {"JIRA_TOKEN": "", "JIRA_URL": ""}
    # Headers same treatment.
    assert jira["headers"] == {"Authorization": ""}
    # Unselected MCPs do NOT ride along.
    assert "slack-mcp" not in slice_["mcp"]["servers"]
    assert "unused-mcp" not in slice_["mcp"]["servers"]


def test_extract_agent_slice_with_selected_skills_emits_buckets_and_scrubs():
    slice_ = extract_agent_slice(
        CFG_WITH_MCPS_AND_SKILLS,
        "agent_abc",
        selected_skills=["jira", "search", "custom-skill"],
        workspace_skill_names={"custom-skill"},
    )
    skills = slice_["skills"]
    # ENTRIES bucket: jira's entry copied with secrets blanked.
    assert "jira" in skills["entries"]
    assert skills["entries"]["jira"]["apiKey"] == ""
    assert skills["entries"]["jira"]["env"] == {"JIRA_USER": ""}
    # config (non-secret) preserved.
    assert skills["entries"]["jira"]["config"] == {"base_url": "https://x"}
    # BUNDLED bucket: search added to allowBundled.
    assert "search" in skills["allowBundled"]
    # WORKSPACE bucket: not config-keyed, doesn't show up in slice (ships in tarball).
    assert "custom-skill" not in skills.get("entries", {})
    assert "custom-skill" not in skills.get("allowBundled", [])


def test_extract_agent_slice_with_unselected_mcps_drops_them():
    slice_ = extract_agent_slice(
        CFG_WITH_MCPS_AND_SKILLS,
        "agent_abc",
        selected_mcps=[],
        selected_skills=[],
    )
    # Empty selections still emit the structural keys so deploy can no-op
    # cleanly without a separate "mcp absent vs empty" check.
    assert slice_["mcp"]["servers"] == {}
    assert slice_["skills"]["entries"] == {}
    assert slice_["skills"]["allowBundled"] == []


def test_extract_agent_slice_skips_unknown_mcp_names_gracefully():
    """If the wizard sent an MCP name that's no longer in the config, skip
    it (don't crash + don't fabricate an entry)."""
    slice_ = extract_agent_slice(
        CFG_WITH_MCPS_AND_SKILLS,
        "agent_abc",
        selected_mcps=["jira-mcp", "ghost-mcp"],
    )
    assert "ghost-mcp" not in slice_["mcp"]["servers"]
    assert "jira-mcp" in slice_["mcp"]["servers"]


def test_extract_agent_slice_with_selections_does_not_mutate_input():
    import copy as _copy

    cfg = _copy.deepcopy(CFG_WITH_MCPS_AND_SKILLS)
    extract_agent_slice(
        cfg,
        "agent_abc",
        selected_mcps=["jira-mcp"],
        selected_skills=["jira"],
    )
    # Original env values must still be present — we scrubbed copies only.
    assert cfg["mcp"]["servers"]["jira-mcp"]["env"]["JIRA_TOKEN"] == "real-token-shhh"
    assert cfg["skills"]["entries"]["jira"]["apiKey"] == "atlassian-secret"
