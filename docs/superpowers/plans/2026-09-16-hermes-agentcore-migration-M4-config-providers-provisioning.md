# Hermes on AgentCore — M4 Config writer, providers, provisioning, secrets, park/wake (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Master plan:** `docs/superpowers/plans/2026-09-16-hermes-agentcore-migration.md` — read its Goal, Architecture, Global Constraints and File Structure first; every task here inherits them. **Spec:** `docs/superpowers/specs/2026-09-16-hermes-agentcore-migration-design.md`.

**Milestone:** M4 Config writer, providers, provisioning, secrets, park/wake. Pass criteria: spec §6. Task numbering is local to this file.

---

## Tasks

Pass criterion (spec §6): a fresh workspace provisions a Hermes runtime with the profile writer;
Bedrock, ChatGPT OAuth and BYO each answer a turn; MCP integrations refresh tokens; a hot patch
applies on the next turn.

**Public interface (fixed by the coordinator; M6's migration job consumes it):**
`core/services/hermes_profiles.py` exposes
`write_agent_profile(owner_id: str, agent: dict, *, provider_choice: str, aip_arns: dict[str, str],
integrations: list[dict], env_vars: dict[str, str]) -> ProfileFiles` where
`ProfileFiles` is a dataclass `(config_yaml: str, dotenv: str, soul_md: str, managed_overlay: str)`,
and `upload_profile(owner_id: str, agent_id: str, files: ProfileFiles) -> None` writing to the
replicas bucket under `<owner>/hermes/<agent_id>/`.
`core/services/hermes_provisioning.py` exposes
`async provision_hermes_runtime(owner_id: str) -> str`.
Tasks 7–10 build the four strings; Task 12 composes and uploads them.

---

### Task 7: `build_profile_config` — the complete per-profile `config.yaml` body

**Files:**
- Create: `apps/backend/core/services/hermes_profiles.py`
- Create: `apps/backend/tests/unit/services/test_hermes_profiles.py`

**Interfaces:**
- Consumes: `config.premium_model_ref` / `cheap_model_ref`
  (`apps/backend/core/containers/config.py:950,993`), `config.TOOL_DENY_BASE` (`:363`),
  `aip_arns: dict[str,str]` from `bedrock_aip.ensure_owner_aips`
- Produces: `build_profile_config(*, agent_id, agent_role, provider_choice, byo_provider=None,
  aip_arns=None, mcp_servers=None, is_ceo=False, listening_paid=True) -> dict`
  (the dict `write_agent_profile` renders into `ProfileFiles.config_yaml`)

Spec §4.5 and report 01 §6 fix the emitted set. Three facts the test pins, each with a silent
failure mode: `model` must be `{"default": ...}` (report 01 §5.7 — only the dict form survives a
merge); `bedrock.discovery.enabled` defaults **true** and `ListFoundationModels` is denied on the
owner role (report 01 §5.6); every `auxiliary.<task>` must pin `provider: bedrock` with
`fallback_chain: []` or the auto-chain probes OpenRouter/Nous (report 01 §5.5).

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_profiles.py
from core.services import hermes_profiles

AIPS = {
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/son",
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/opu",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/hai",
}


def cfg(**kw):
    base = dict(agent_id="a1", agent_role="engineering", provider_choice="bedrock_claude", aip_arns=AIPS)
    return hermes_profiles.build_profile_config(**(base | kw))


def test_model_is_written_as_a_dict_with_default_never_a_bare_string():
    assert cfg()["model"] == {"default": AIPS["sonnet"], "provider": "bedrock"}


def test_the_ceo_profile_is_pinned_to_the_premium_tier():
    assert cfg(agent_role="ceo", is_ceo=True)["model"]["default"] == AIPS["opus"]


def test_a_trial_owner_with_one_aip_keeps_every_tier_on_that_one_profile():
    trial = cfg(provider_choice="bedrock_trial", aip_arns={"sonnet": AIPS["sonnet"]}, is_ceo=True)
    assert trial["model"]["default"] == AIPS["sonnet"]
    assert trial["auxiliary"]["compression"]["model"] == AIPS["sonnet"]


def test_bedrock_discovery_is_pinned_off():
    assert cfg()["bedrock"]["discovery"]["enabled"] is False
    assert cfg()["bedrock"]["region"] == "us-east-1"


def test_every_auxiliary_task_is_pinned_to_bedrock_with_an_empty_fallback_chain():
    aux = cfg()["auxiliary"]
    assert aux["free_only"] is True
    for task in hermes_profiles.AUXILIARY_TASKS:
        assert aux[task]["provider"] == "bedrock", task
        assert aux[task]["fallback_chain"] == [], task
        assert aux[task]["model"] == AIPS["haiku"], task


def test_approvals_are_off_with_an_unattended_approve_and_a_deny_backstop():
    approvals = cfg()["approvals"]
    assert approvals["mode"] == "off"
    assert approvals["unattended_mode"] == "approve"
    assert approvals["deny"] == list(hermes_profiles.APPROVALS_DENY)


def test_the_tool_deny_list_is_ported_to_disabled_toolsets():
    assert set(cfg()["agent"]["disabled_toolsets"]) == {"cronjob", "tts", "video_gen"}
    assert cfg()["dashboard"]["enabled"] is False
    assert cfg()["cron"]["allow_agent_scheduling"] is False


def test_limits_memory_skills_and_sessions_are_all_emitted():
    c = cfg()
    assert c["agent"]["run_budget_seconds"] == 1800
    assert c["max_concurrent_sessions"] == 3
    assert c["memory"]["memory_enabled"] is True and c["memory"]["provider"] == ""
    assert c["sessions"]["retention_days"] == 90 and c["sessions"]["vacuum_after_prune"] is True
    assert c["skills"]["write_approval"] is False


def test_no_secret_ever_lands_in_config_yaml():
    import json

    c = cfg(mcp_servers={"stripe": {"url": "https://mcp.stripe.com",
                                    "headers": {"Authorization": "${STRIPE_MCP_TOKEN}"}}})
    blob = json.dumps(c)
    assert "sk-" not in blob and "pcp_" not in blob and "Bearer " not in blob
    assert c["mcp_servers"]["stripe"]["headers"]["Authorization"] == "${STRIPE_MCP_TOKEN}"


def test_byo_key_selects_the_direct_provider_and_its_model_ids():
    c = cfg(provider_choice="byo_key", byo_provider="anthropic", aip_arns=None)
    assert c["model"] == {"default": "claude-sonnet-5", "provider": "anthropic"}
    assert "bedrock" not in c


def test_chatgpt_oauth_selects_the_codex_provider_with_no_tier_split():
    c = cfg(provider_choice="chatgpt_oauth", aip_arns=None, is_ceo=True)
    assert c["model"]["provider"] == "openai-codex"
    assert c["auxiliary"]["compression"]["provider"] == "auto"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.hermes_profiles'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/hermes_profiles.py
"""The per-profile Hermes config writer — the replacement for ``write_openclaw_config``.

A Hermes profile IS a complete ``$HERMES_HOME`` (report 01 §3): its ``config.yaml`` is merged
over ``DEFAULT_CONFIG`` ALONE, with no inheritance from the root home. So every generated file
must be complete, produced from one template plus per-agent overrides — the same shape
``build_openclaw_config_dict`` has today, minus about two thirds of the keys (``_emit_81_shape``
gates, ``meta.lastTouchedVersion``, the Chrome recipe, the node trust store, the lightbulb channel
and bindings, the hooks ingress) which have no Hermes equivalent and are DROPPED, not ported.

Secrets NEVER land here (spec §4.5): ``config.yaml`` carries ``${VAR}`` references only and the
per-profile ``.env`` is the store. The non-negotiables an agent could otherwise edit out of its own
config live in the managed overlay (Task 11), which wins at the leaf after env expansion.
"""

from __future__ import annotations

from core.containers.config import cheap_model_ref, premium_model_ref

_BEDROCK = "bedrock"

#: Every auxiliary task we pin. Hermes's auto-chain is main provider -> OpenRouter -> Nous Portal
#: -> ... (auxiliary_client.py:3-6); an unpinned task on a platform-billed path is egress to a
#: vendor we have no account with. ``fallback_chain: []`` plus ``free_only`` is belt and braces.
AUXILIARY_TASKS = (
    "vision", "compression", "skills_hub", "approval", "mcp", "title_generation",
    "memory_query_rewrite", "triage_specifier", "kanban_decomposer", "profile_describer",
    "goal_judge", "curator", "monitor", "background_review",
)

#: Hard backstop: a match blocks even under ``--yolo``/mode=off (config_defaults.py:1582-1583).
APPROVALS_DENY = ("rm -rf /", "mkfs*", "dd if=* of=/dev/*", ":(){:|:&};:")

#: The ``TOOL_DENY_BASE`` port. canvas/music_generate have no Hermes toolset; dashboard and cron
#: are switches, not toolsets (report 01 §2.7).
_DISABLED_TOOLSETS = ("cronjob", "tts", "video_gen")

_BYO_MODELS = {
    "anthropic": ("anthropic", "claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"),
    "openai": ("openai-api", "gpt-5.4", "gpt-5.6-sol", "gpt-5.4-mini"),
}


def _bedrock_ids(provider_choice, byo_provider, aip_arns, is_ceo):
    default = aip_arns["sonnet"]
    strip = lambda ref: ref.split("/", 1)[1] if ref else None  # noqa: E731 — "amazon-bedrock/<arn>"
    premium = strip(premium_model_ref(provider_choice, byo_provider, aip_arns=aip_arns))
    cheap = strip(cheap_model_ref(provider_choice, byo_provider, aip_arns=aip_arns))
    return (premium or default) if is_ceo else default, cheap or default


def _model_block(provider_choice, byo_provider, aip_arns, is_ceo) -> tuple[dict, str, str]:
    """(model block, aux model id, aux provider)."""
    if provider_choice in ("bedrock_claude", "bedrock_trial"):
        if not aip_arns:
            raise ValueError(f"{provider_choice} requires aip_arns")
        default, cheap = _bedrock_ids(provider_choice, byo_provider, aip_arns, is_ceo)
        return {"default": default, "provider": _BEDROCK}, cheap, _BEDROCK
    if provider_choice == "byo_key":
        provider, standard, premium, cheap = _BYO_MODELS[byo_provider or "anthropic"]
        return {"default": premium if is_ceo else standard, "provider": provider}, cheap, provider
    # chatgpt_oauth — flat rate, one tier; aux inherits the main model ("auto"), because the Codex
    # backend rejects an aux-side summariser override (config.py cheap_model_ref docstring).
    return {"default": "gpt-5.6-sol", "provider": "openai-codex"}, "", "auto"


def build_profile_config(
    *,
    agent_id: str,
    agent_role: str,
    provider_choice: str,
    byo_provider: str | None = None,
    aip_arns: dict[str, str] | None = None,
    mcp_servers: dict | None = None,
    is_ceo: bool = False,
    listening_paid: bool = True,
) -> dict:
    model, aux_model, aux_provider = _model_block(provider_choice, byo_provider, aip_arns, is_ceo)
    aux: dict = {"free_only": True, "transient_retries": 2}
    for task in AUXILIARY_TASKS:
        aux[task] = {"provider": aux_provider, "model": aux_model, "fallback_chain": []}
    config: dict = {
        "model": model,
        "auxiliary": aux,
        "toolsets": ["hermes-cli"],
        "max_concurrent_sessions": 3,
        "agent": {
            "disabled_toolsets": list(_DISABLED_TOOLSETS),
            "run_budget_seconds": 1800,
            "stall_guards": True,
        },
        "approvals": {
            "mode": "off",
            "cron_mode": "deny",
            "single_query_mode": "deny",
            "unattended_mode": "approve",
            "deny": list(APPROVALS_DENY),
        },
        "cron": {"allow_agent_scheduling": False},
        "dashboard": {"enabled": False},
        "memory": {
            "memory_enabled": True,
            "user_profile_enabled": True,
            "write_approval": False,
            "provider": "",  # built-in: never pays for embeddings on a platform-billed path
            "memory_char_limit": 2200,
            "user_char_limit": 1375,
        },
        "skills": {
            "write_approval": False,
            "guard_agent_created": not listening_paid,
            "external_dirs": ["/opt/hermes-skills"],
        },
        "sessions": {
            "auto_prune": True,
            "retention_days": 90,
            "auto_archive": True,
            "vacuum_after_prune": True,
        },
        "terminal": {"backend": "lightbulb_node"},
        "mcp_servers": dict(mcp_servers or {}),
        "plugins": {"entries": {"lightbulb_node": {"enabled": True}, "lightbulb": {"enabled": True}}},
    }
    if model["provider"] == _BEDROCK:
        config["bedrock"] = {"region": "us-east-1", "discovery": {"enabled": False}}
    return config
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/hermes_profiles.py apps/backend/tests/unit/services/test_hermes_profiles.py
git commit -m "feat(hermes): per-profile config.yaml writer (model/aux pins, approvals, toolsets, limits)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `build_profile_env` + `render_dotenv` — the per-profile `.env`

**Files:**
- Modify: `apps/backend/core/services/hermes_profiles.py` (append after `build_profile_config`)
- Modify: `apps/backend/tests/unit/services/test_hermes_profiles.py` (append)

**Interfaces:**
- Consumes: `provisioning_landing.build_provisioned_env(owner_id) -> dict[str,str]`
  (`apps/backend/core/services/provisioning_landing.py:162-201`) — reaches
  `write_agent_profile` as its `env_vars` argument
- Produces: `build_profile_env(*, gate_bearer_ref, gate_url, region, paperclip_key_ref,
  env_vars, byo_key_ref=None) -> dict[str,str]`, `render_dotenv(env) -> str`
  (`ProfileFiles.dotenv`)

Report 06 §1.3 is the sharp edge: `AWS_BEARER_TOKEN_BEDROCK` and
`AWS_ENDPOINT_URL_BEDROCK_RUNTIME` are read from the **raw process env** by
`bedrock_adapter.py:180-182` and by botocore, NOT through the per-profile secret scope — they are
process-wide, which is correct only because there is one bridge process per WORKSPACE. Plain
API-key providers and `mcp_servers` `${VAR}` references DO go through the profile scope, so they
belong in `.env`.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_profiles.py  (append)
def test_dotenv_carries_the_per_profile_refs_and_the_provisioned_env():
    env = hermes_profiles.build_profile_env(
        gate_bearer_ref="lightbulb/dev/gate-bearer/org_a",
        gate_url="https://gate.lambda-url.us-east-1.on.aws",
        region="us-east-1",
        paperclip_key_ref="lightbulb/dev/paperclip/org_a/a1",
        env_vars={"STRIPE_SECRET_KEY": "sk_live_x"},
    )
    assert env["AWS_ENDPOINT_URL_BEDROCK_RUNTIME"] == "https://gate.lambda-url.us-east-1.on.aws"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["LIGHTBULB_GATE_BEARER_SECRET"] == "lightbulb/dev/gate-bearer/org_a"
    assert env["LIGHTBULB_PAPERCLIP_KEY_SECRET"] == "lightbulb/dev/paperclip/org_a/a1"
    assert env["STRIPE_SECRET_KEY"] == "sk_live_x"


def test_dotenv_never_carries_a_plaintext_gate_bearer():
    env = hermes_profiles.build_profile_env(
        gate_bearer_ref="lightbulb/dev/gate-bearer/org_a",
        gate_url="https://gate",
        region="us-east-1",
        paperclip_key_ref="lightbulb/dev/paperclip/org_a/a1",
        env_vars={},
    )
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env  # resolved at boot from Secrets Manager (Task 20)


def test_render_dotenv_quotes_and_escapes():
    out = hermes_profiles.render_dotenv({"A": "one two", "B": 'say "hi"', "C": "plain"})
    assert out.splitlines() == ['A="one two"', 'B="say \\"hi\\""', "C=plain"]


def test_render_dotenv_refuses_an_illegal_key():
    import pytest

    with pytest.raises(ValueError):
        hermes_profiles.render_dotenv({"not a key": "x"})
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles.py -q -k dotenv`
Expected: FAIL with `AttributeError: module 'core.services.hermes_profiles' has no attribute 'build_profile_env'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/hermes_profiles.py  (append)
import re

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./:@+=-]*$")


def build_profile_env(
    *,
    gate_bearer_ref: str,
    gate_url: str,
    region: str,
    paperclip_key_ref: str,
    env_vars: dict[str, str],
    byo_key_ref: str | None = None,
) -> dict[str, str]:
    """The profile's ``.env``.

    Credentials the PLATFORM owns arrive as a Secrets Manager NAME, not a value: AgentCore
    ``environmentVariables`` are plaintext to anyone with ``GetAgentRuntime`` (spec §4.9) and this
    file rides S3. ``sync.load_secrets`` (Task 20) resolves every ``*_SECRET`` name at boot with the
    execution role. Owner-provisioned values (``env_vars``, from ``build_provisioned_env``) are
    carried as-is — the same rail ``openclaw.json``'s ``env.vars`` used.
    """
    env = {
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": gate_url,
        "AWS_REGION": region,
        "LIGHTBULB_GATE_BEARER_SECRET": gate_bearer_ref,
        "LIGHTBULB_PAPERCLIP_KEY_SECRET": paperclip_key_ref,
    }
    if byo_key_ref:
        env["LIGHTBULB_BYO_KEY_SECRET"] = byo_key_ref
    env.update(env_vars or {})
    return env


def render_dotenv(env: dict[str, str]) -> str:
    lines = []
    for key, value in env.items():
        if not _KEY_RE.match(key):
            raise ValueError(f"illegal dotenv key: {key!r}")
        text = str(value)
        lines.append(f"{key}={text}" if _PLAIN_RE.match(text) else f'{key}="' + text.replace('"', '\\"') + '"')
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles.py -q`
Expected: PASS (15 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/hermes_profiles.py apps/backend/tests/unit/services/test_hermes_profiles.py
git commit -m "feat(hermes): per-profile .env writer — secret NAMES only, resolved at boot

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Translate `integration_mcp_servers` into Hermes `mcp_servers`

**Files:**
- Modify: `apps/backend/core/services/hermes_profiles.py` (append)
- Create: `apps/backend/tests/unit/services/test_hermes_profiles_mcp.py`

**Interfaces:**
- Consumes: `credentials.mcp_server.build_connected_integration_servers(owner_id) -> dict[str, dict]`
  (`apps/backend/core/services/credentials/mcp_server.py:40-90`) — the OpenClaw spelling
  (`transport`, `connectionTimeoutMs`, `requestTimeoutMs`, `maxTokensCap`); reaches
  `write_agent_profile` as its `integrations` argument
- Produces: `translate_mcp_servers(servers: dict | None) -> dict` in Hermes's snake_case shape
  (`command/args/env/cwd` stdio; `url/headers/auth/oauth` remote; `timeout`, `connect_timeout`,
  `tools.include/exclude`, `sampling.max_tokens_cap`)

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_profiles_mcp.py
from core.services.hermes_profiles import translate_mcp_servers


def test_remote_oauth_server_keeps_auth_oauth_and_snake_cases_timeouts():
    out = translate_mcp_servers({
        "stripe": {
            "transport": "streamable-http",
            "url": "https://mcp.stripe.com",
            "auth": "oauth",
            "requestTimeoutMs": 60000,
            "connectionTimeoutMs": 15000,
        }
    })
    assert out["stripe"] == {
        "url": "https://mcp.stripe.com",
        "auth": "oauth",
        "timeout": 60,
        "connect_timeout": 15,
    }


def test_stdio_server_copies_command_args_env_and_converts_timeout():
    out = translate_mcp_servers({
        "qbo": {
            "command": "npx",
            "args": ["-y", "qbo-mcp"],
            "env": {"QBO_TOKEN_STORE": "/opt/data/profiles/a1/qbo.json"},
            "connectionTimeoutMs": 30000,
        }
    })
    assert out["qbo"]["command"] == "npx"
    assert out["qbo"]["args"] == ["-y", "qbo-mcp"]
    assert out["qbo"]["env"] == {"QBO_TOKEN_STORE": "/opt/data/profiles/a1/qbo.json"}
    assert out["qbo"]["connect_timeout"] == 30
    assert "connectionTimeoutMs" not in out["qbo"]


def test_tool_filters_and_sampling_are_snake_cased():
    out = translate_mcp_servers({
        "s": {"url": "https://x", "tools": {"include": ["a"], "exclude": ["b"]},
              "sampling": {"enabled": True, "maxTokensCap": 4096, "maxRpm": 10}}
    })
    assert out["s"]["tools"] == {"include": ["a"], "exclude": ["b"]}
    assert out["s"]["sampling"] == {"enabled": True, "max_tokens_cap": 4096, "max_rpm": 10}


def test_an_unknown_key_is_dropped_not_passed_through():
    out = translate_mcp_servers({"s": {"url": "https://x", "somethingOpenClawOnly": 1}})
    assert out["s"] == {"url": "https://x"}


def test_empty_input_is_an_empty_map():
    assert translate_mcp_servers({}) == {}
    assert translate_mcp_servers(None) == {}
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles_mcp.py -q`
Expected: FAIL with `ImportError: cannot import name 'translate_mcp_servers' from 'core.services.hermes_profiles'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/hermes_profiles.py  (append)
#: OpenClaw spelling -> Hermes spelling. camelCase -> snake_case is the trap (report 01 §2.8).
#: An unrecognised key is DROPPED rather than passed through: Hermes's mcp config is a closed
#: shape and a stray key is a spawn-time error, not a warning.
_MCP_DIRECT = ("command", "args", "env", "cwd", "url", "headers", "auth", "oauth", "enabled")
_MCP_MS_TO_S = {"requestTimeoutMs": "timeout", "connectionTimeoutMs": "connect_timeout"}
_MCP_SAMPLING = {
    "enabled": "enabled", "model": "model", "maxTokensCap": "max_tokens_cap",
    "timeout": "timeout", "maxRpm": "max_rpm",
}


def translate_mcp_servers(servers: dict | None) -> dict:
    out: dict = {}
    for name, block in (servers or {}).items():
        entry: dict = {key: block[key] for key in _MCP_DIRECT if key in block}
        for ms_key, s_key in _MCP_MS_TO_S.items():
            if isinstance(block.get(ms_key), int):
                entry[s_key] = block[ms_key] // 1000
        tools = block.get("tools")
        if isinstance(tools, dict):
            entry["tools"] = {k: tools[k] for k in ("include", "exclude") if k in tools}
        sampling = block.get("sampling")
        if isinstance(sampling, dict):
            entry["sampling"] = {v: sampling[k] for k, v in _MCP_SAMPLING.items() if k in sampling}
        out[name] = entry
    return out
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles_mcp.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/hermes_profiles.py apps/backend/tests/unit/services/test_hermes_profiles_mcp.py
git commit -m "feat(hermes): translate integration MCP servers into Hermes mcp_servers (snake_case)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: `SOUL.md` per profile from the Paperclip roster row

**Files:**
- Modify: `apps/backend/core/services/hermes_profiles.py` (append)
- Create: `apps/backend/tests/unit/services/test_hermes_profiles_soul.py`

**Interfaces:**
- Consumes: the Paperclip agent dict (`{"id","name","role","title","capabilities","icon"}` — the
  rows `_build_agents_list` reads, `apps/backend/core/containers/config.py:1074-1157`); this is
  `write_agent_profile`'s `agent` argument
- Produces: `build_soul_md(agent: dict, *, company_name: str | None = None) -> str`
  (`ProfileFiles.soul_md`)

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_profiles_soul.py
from core.services.hermes_profiles import build_soul_md


def test_soul_names_the_agent_and_its_role():
    soul = build_soul_md({"id": "a1", "name": "Ada", "role": "engineering", "title": "Head of Engineering"})
    assert soul.startswith("# Ada")
    assert "Head of Engineering" in soul
    assert "engineering" in soul


def test_the_ceo_soul_says_it_is_the_root_of_the_roster():
    soul = build_soul_md({"id": "a0", "name": "Friday", "role": "ceo"})
    assert "CEO" in soul
    assert "report to you" in soul


def test_capabilities_are_carried_verbatim_when_paperclip_has_them():
    soul = build_soul_md({"id": "a1", "name": "Ada", "role": "engineering", "capabilities": "ships backend code"})
    assert "ships backend code" in soul


def test_a_nameless_agent_falls_back_to_its_id_not_a_crash():
    assert build_soul_md({"id": "a9", "role": "general"}).startswith("# a9")
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles_soul.py -q`
Expected: FAIL with `ImportError: cannot import name 'build_soul_md' from 'core.services.hermes_profiles'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/hermes_profiles.py  (append)
_CEO_LINE = (
    "You are the CEO of this workspace. The other agents report to you; you set direction, "
    "approve spend and answer the owner first."
)


def build_soul_md(agent: dict, *, company_name: str | None = None) -> str:
    """The profile's ``SOUL.md`` — the agent's identity, from the Paperclip roster row.

    Hermes reads ``$HERMES_HOME/SOUL.md`` per profile (config_home.py:66). There is no
    ``agents.list[*].name`` to carry a display name any more, so the name lives here and in the
    Paperclip row, and nowhere in config.yaml.
    """
    name = agent.get("name") or agent["id"]
    role = agent.get("role") or "general"
    lines = [f"# {name}", ""]
    if agent.get("title"):
        lines += [f"**{agent['title']}**", ""]
    where = f" at {company_name}" if company_name else ""
    lines.append(f"You are {name}, the {role} agent{where} on Lightbulb.")
    if role == "ceo":
        lines.append(_CEO_LINE)
    if agent.get("capabilities"):
        lines += ["", "## What you do", "", agent["capabilities"]]
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles_soul.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/hermes_profiles.py apps/backend/tests/unit/services/test_hermes_profiles_soul.py
git commit -m "feat(hermes): SOUL.md per profile from the Paperclip roster row

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: The managed overlay — non-negotiables an agent cannot edit out

**Files:**
- Modify: `apps/backend/core/services/hermes_profiles.py` (append `MANAGED_OVERLAY_YAML`)
- Create: `apps/infra/hermes/managed/config.yaml` (baked to `/etc/hermes/config.yaml`)
- Modify: `apps/infra/hermes/Dockerfile.spike` (COPY + root-owned, read-only)
- Create: `apps/backend/tests/unit/services/test_hermes_managed_overlay.py`

**Interfaces:**
- Consumes: Hermes's managed scope — `get_managed_dir()` resolves `$HERMES_MANAGED_DIR` then
  `/etc/hermes`, and the overlay merges AFTER env expansion so a profile's own `${VAR}` cannot
  shadow a managed literal (`/opt/hermes/hermes_cli/managed_scope.py:38-53,101-120`)
- Produces: `MANAGED_OVERLAY_YAML: str` — the single source for BOTH the baked
  `/etc/hermes/config.yaml` and `ProfileFiles.managed_overlay`

This is the structural answer to report 01 §5.8: Hermes writes its own `config.yaml` from many
paths, so anything that must hold cannot live only in the file the agent can rewrite. One constant,
two consumers, and a test that fails the moment the baked file drifts from it.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_managed_overlay.py
from pathlib import Path

import yaml

from core.services.hermes_profiles import MANAGED_OVERLAY_YAML

REPO = Path(__file__).resolve().parents[4]
BAKED = REPO / "apps/infra/hermes/managed/config.yaml"


def parsed():
    return yaml.safe_load(MANAGED_OVERLAY_YAML)


def test_the_overlay_pins_bedrock_discovery_off():
    assert parsed()["bedrock"]["discovery"]["enabled"] is False


def test_the_overlay_pins_shared_telemetry_and_updates_off():
    data = parsed()
    assert data["telemetry"]["shared_metrics"]["enabled"] is False
    assert data["telemetry"]["shared_metrics"]["send"] is False
    assert data["updates"]["check_on_start"] is False


def test_the_overlay_carries_the_approvals_deny_backstop():
    assert "rm -rf /" in parsed()["approvals"]["deny"]


def test_the_overlay_holds_no_secret_and_no_env_reference():
    # Managed wins AFTER expansion, so a ${VAR} here would be pinned as a literal.
    assert "${" not in MANAGED_OVERLAY_YAML
    assert "sk-" not in MANAGED_OVERLAY_YAML and "pcp_" not in MANAGED_OVERLAY_YAML


def test_the_baked_image_file_is_byte_identical_to_the_constant():
    assert BAKED.read_text() == MANAGED_OVERLAY_YAML


def test_the_dockerfile_installs_it_read_only_at_etc_hermes():
    dockerfile = (REPO / "apps/infra/hermes/Dockerfile.spike").read_text()
    assert "COPY managed/config.yaml /etc/hermes/config.yaml" in dockerfile
    assert "chown root:root /etc/hermes/config.yaml" in dockerfile
    assert "chmod 0444 /etc/hermes/config.yaml" in dockerfile
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_managed_overlay.py -q`
Expected: FAIL with `ImportError: cannot import name 'MANAGED_OVERLAY_YAML' from 'core.services.hermes_profiles'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/hermes_profiles.py  (append)
#: The managed scope (/etc/hermes/config.yaml). Hermes merges it AFTER env expansion and it wins at
#: the leaf, so an agent that rewrites its own profile config.yaml cannot undo anything here
#: (managed_scope.py:101-120, docs/design/managed-scope.md §4.1). Only non-negotiables belong
#: here; anything an owner may legitimately change stays in the per-profile file.
#: apps/infra/hermes/managed/config.yaml is a byte-identical copy, asserted by the unit test.
MANAGED_OVERLAY_YAML = """\
bedrock:
  discovery:
    # Default is TRUE and calls ListFoundationModels hourly; the per-owner role denies it.
    enabled: false
telemetry:
  shared_metrics:
    enabled: false
    send: false
updates:
  check_on_start: false
approvals:
  # Blocks even under mode=off / --yolo. Belt for the profile's own copy.
  deny:
    - "rm -rf /"
    - "mkfs*"
    - "dd if=* of=/dev/*"
    - ":(){:|:&};:"
"""
```
```bash
# create the baked copy from the constant, so the two can never drift by hand
cd apps/backend && mkdir -p ../infra/hermes/managed && uv run python -c "from core.services.hermes_profiles import MANAGED_OVERLAY_YAML; open('../infra/hermes/managed/config.yaml','w').write(MANAGED_OVERLAY_YAML)"
```
```dockerfile
# apps/infra/hermes/Dockerfile.spike  (add before the USER 1000 line)
COPY managed/config.yaml /etc/hermes/config.yaml
RUN chown root:root /etc/hermes/config.yaml && chmod 0444 /etc/hermes/config.yaml
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_managed_overlay.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/hermes_profiles.py apps/backend/tests/unit/services/test_hermes_managed_overlay.py apps/infra/hermes/managed/config.yaml apps/infra/hermes/Dockerfile.spike
git commit -m "feat(hermes): managed overlay pins discovery, telemetry, updates and the deny backstop

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: `write_agent_profile` + `upload_profile` — compose and publish to S3

**Files:**
- Modify: `apps/backend/core/services/hermes_profiles.py` (append the public API)
- Create: `apps/backend/tests/unit/services/test_hermes_profiles_write.py`

**Interfaces:**
- Consumes: `build_profile_config`, `build_profile_env`, `render_dotenv`, `translate_mcp_servers`,
  `build_soul_md`, `MANAGED_OVERLAY_YAML`; `settings.OWNER_STATE_BUCKET` (Task 18's rename of
  `LITESTREAM_BUCKET`, value `lightbulb-{env}-openclaw-replicas`)
- Produces (verbatim, consumed by M6's migration job):
  - `@dataclass class ProfileFiles: config_yaml: str; dotenv: str; soul_md: str; managed_overlay: str`
  - `write_agent_profile(owner_id: str, agent: dict, *, provider_choice: str,
    aip_arns: dict[str, str], integrations: list[dict], env_vars: dict[str, str]) -> ProfileFiles`
  - `upload_profile(owner_id: str, agent_id: str, files: ProfileFiles) -> None`
    writing `<owner>/hermes/<agent_id>/{config.yaml,.env,SOUL.md,managed/config.yaml,.revision}`

The prefix is `<owner>/hermes/<agentId>/` — owner FIRST. The per-owner IAM grant is
`arn:aws:s3:::<bucket>/<owner>/*` (`bedrock_aip.build_owner_litestream_s3_policy:98-131`), so a
`hermes/<owner>/...` prefix (the spike bridge's current default) is outside every owner's grant and
AccessDenies on the first restore. No new bucket and no IAM change are needed for the corrected order.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_profiles_write.py
import yaml

from core.services import hermes_profiles
from core.services.hermes_profiles import ProfileFiles, upload_profile, write_agent_profile

AIPS = {"sonnet": "arn:son", "opus": "arn:opu", "haiku": "arn:hai"}


def test_write_agent_profile_returns_the_four_files():
    files = write_agent_profile(
        "org_a",
        {"id": "a1", "name": "Ada", "role": "ceo"},
        provider_choice="bedrock_claude",
        aip_arns=AIPS,
        integrations=[{"key": "stripe", "url": "https://mcp.stripe.com", "auth": "oauth"}],
        env_vars={"STRIPE_SECRET_KEY": "sk_live_x"},
    )
    assert isinstance(files, ProfileFiles)
    config = yaml.safe_load(files.config_yaml)
    assert config["model"]["default"] == "arn:opu"          # role ceo -> premium tier
    assert config["mcp_servers"]["stripe"]["auth"] == "oauth"
    assert "STRIPE_SECRET_KEY=sk_live_x" in files.dotenv
    assert files.soul_md.startswith("# Ada")
    assert files.managed_overlay == hermes_profiles.MANAGED_OVERLAY_YAML


def test_config_yaml_carries_no_secret_even_though_dotenv_does():
    files = write_agent_profile(
        "org_a", {"id": "a1", "role": "engineering"},
        provider_choice="bedrock_claude", aip_arns=AIPS, integrations=[],
        env_vars={"STRIPE_SECRET_KEY": "sk_live_x"},
    )
    assert "sk_live_x" not in files.config_yaml
    assert "sk_live_x" in files.dotenv


def test_upload_writes_owner_first_under_hermes(monkeypatch):
    puts = []
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.append((bucket, key)))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas")
    upload_profile("org_a", "a1", ProfileFiles("model: {}\n", "A=1", "# Ada\n", "updates: {}\n"))
    keys = [key for _, key in puts]
    assert keys[:4] == [
        "org_a/hermes/a1/config.yaml",
        "org_a/hermes/a1/.env",
        "org_a/hermes/a1/SOUL.md",
        "org_a/hermes/a1/managed/config.yaml",
    ]
    assert keys[4] == "org_a/hermes/a1/.revision"  # written LAST — the bridge polls it
    assert {bucket for bucket, _ in puts} == {"lightbulb-dev-openclaw-replicas"}


def test_upload_is_a_no_op_without_a_bucket(monkeypatch):
    puts = []
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda *a: puts.append(a))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")
    upload_profile("org_a", "a1", ProfileFiles("x", "y", "z", "w"))
    assert puts == []
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles_write.py -q`
Expected: FAIL with `ImportError: cannot import name 'ProfileFiles' from 'core.services.hermes_profiles'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/hermes_profiles.py  (append)
import time
from dataclasses import dataclass

import yaml

from core.config import settings


@dataclass
class ProfileFiles:
    config_yaml: str
    dotenv: str
    soul_md: str
    managed_overlay: str


def _gate_bearer_ref(owner_id: str) -> str:
    return f"lightbulb/{settings.ENVIRONMENT}/gate-bearer/{owner_id}"


def _paperclip_key_ref(owner_id: str, agent_id: str) -> str:
    return f"lightbulb/{settings.ENVIRONMENT}/paperclip/{owner_id}/{agent_id}"


def write_agent_profile(
    owner_id: str,
    agent: dict,
    *,
    provider_choice: str,
    aip_arns: dict[str, str],
    integrations: list[dict],
    env_vars: dict[str, str],
) -> ProfileFiles:
    """The four files that ARE one agent's Hermes profile.

    ``integrations`` is the owner's connected-integration list (each row carries the MCP block
    ``build_connected_integration_servers`` emits under ``key``); ``env_vars`` is
    ``provisioning_landing.build_provisioned_env(owner_id)``.
    """
    from core.billing.bedrock_gate_url import gate_url

    servers = {row["key"]: {k: v for k, v in row.items() if k != "key"} for row in integrations or []}
    config = build_profile_config(
        agent_id=agent["id"],
        agent_role=agent.get("role") or "general",
        provider_choice=provider_choice,
        byo_provider=agent.get("byo_provider"),
        aip_arns=aip_arns,
        mcp_servers=translate_mcp_servers(servers),
        is_ceo=(agent.get("role") == "ceo"),
    )
    env = build_profile_env(
        gate_bearer_ref=_gate_bearer_ref(owner_id),
        gate_url=gate_url() or "",
        region=settings.AWS_REGION,
        paperclip_key_ref=_paperclip_key_ref(owner_id, agent["id"]),
        env_vars=env_vars,
    )
    return ProfileFiles(
        config_yaml=yaml.safe_dump(config, sort_keys=False),
        dotenv=render_dotenv(env),
        soul_md=build_soul_md(agent),
        managed_overlay=MANAGED_OVERLAY_YAML,
    )


def _s3_put(bucket: str, key: str, body: str) -> None:
    import boto3

    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode())


def profile_prefix(owner_id: str, agent_id: str) -> str:
    """``<owner>/hermes/<agentId>/`` — owner FIRST.

    The per-owner IAM grant is ``arn:aws:s3:::<bucket>/<owner>/*``
    (bedrock_aip.build_owner_litestream_s3_policy). Any other order is outside every owner's
    grant and AccessDenies on the first restore.
    """
    return f"{owner_id}/hermes/{agent_id}/"


def upload_profile(owner_id: str, agent_id: str, files: ProfileFiles) -> None:
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    prefix = profile_prefix(owner_id, agent_id)
    _s3_put(bucket, f"{prefix}config.yaml", files.config_yaml)
    _s3_put(bucket, f"{prefix}.env", files.dotenv)
    _s3_put(bucket, f"{prefix}SOUL.md", files.soul_md)
    _s3_put(bucket, f"{prefix}managed/config.yaml", files.managed_overlay)
    # LAST, always: the bridge re-pulls a profile when this object's value changes (Task 13).
    # Writing it before the files it describes would advertise a config that is not there yet.
    _s3_put(bucket, f"{prefix}.revision", str(int(time.time() * 1000)))
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles_write.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/hermes_profiles.py apps/backend/tests/unit/services/test_hermes_profiles_write.py
git commit -m "feat(hermes): write_agent_profile + upload_profile to <owner>/hermes/<agentId>/

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: Hot patch — `config.get`/`config.patch` on an AgentCore row, and the bridge re-pull

**Files:**
- Modify: `apps/backend/core/services/runtime_rpc.py` (`execute_sync`, before the
  `container_rpc.call` at `:330`)
- Modify: `apps/backend/core/services/hermes_profiles.py` (append `read_profile_config`,
  `patch_profile_config`)
- Modify: `apps/infra/hermes/bridge/sync.py` (M1's module: `ensure_profile` re-pull on a changed
  `.revision`)
- Create: `apps/backend/tests/unit/services/test_runtime_rpc_hermes_config.py`
- Create: `apps/infra/hermes/tests/test_sync_revision.py`

**Interfaces:**
- Consumes: `runtime_transport.host_for(container) == "agentcore"`
  (`apps/backend/core/services/runtime_transport.py:82-85`), `upload_profile`
- Produces: `patch_profile_config(owner_id, agent_id, patch: dict) -> dict` (deep merge, re-upload,
  bumped `.revision`); `sync.ensure_profile(agent_id) -> None` re-pulling when `.revision` changed

Spec §4.5: "Config is read once per `AIAgent` construction and cached on file mtime, so rewriting
`profiles/<id>/config.yaml` takes effect on the agent's next turn. No watcher, no restart." The
backend is the only writer, so `config.patch` never reaches the runtime at all — it rewrites S3 and
the bridge pulls before the next turn. That is what deletes `config_patcher.py` and the EFS locks.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_runtime_rpc_hermes_config.py
import pytest

from core.services import runtime_rpc


@pytest.mark.asyncio
async def test_config_get_on_an_agentcore_row_is_served_from_s3_not_the_runtime(monkeypatch):
    called = {}
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", lambda owner_id: True)
    monkeypatch.setattr(
        runtime_rpc.hermes_profiles, "read_profile_config",
        lambda owner_id, agent_id: {"model": {"default": "arn:son"}},
    )
    monkeypatch.setattr(
        runtime_rpc.container_rpc, "call",
        lambda *a, **k: called.setdefault("forwarded", True),
    )
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1",
        method="config.get", params={"agentId": "a1"},
    )
    assert out == {"model": {"default": "arn:son"}}
    assert "forwarded" not in called


@pytest.mark.asyncio
async def test_config_patch_on_an_agentcore_row_rewrites_s3_and_bumps_the_revision(monkeypatch):
    patched = {}
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", lambda owner_id: True)
    monkeypatch.setattr(
        runtime_rpc.hermes_profiles, "patch_profile_config",
        lambda owner_id, agent_id, patch: patched.update(owner_id=owner_id, agent_id=agent_id, patch=patch) or {"ok": True},
    )
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1",
        method="config.patch", params={"agentId": "a1", "patch": {"memory": {"memory_enabled": False}}},
    )
    assert out == {"ok": True}
    assert patched["patch"] == {"memory": {"memory_enabled": False}}


@pytest.mark.asyncio
async def test_config_methods_still_forward_for_an_ecs_row(monkeypatch):
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", lambda owner_id: False)
    seen = {}

    async def fake_call(owner_id, method, params=None, **k):
        seen.update(method=method)
        return {"from": "container"}

    monkeypatch.setattr(runtime_rpc.container_rpc, "call", fake_call)
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="config.get", params={},
    )
    assert out == {"from": "container"} and seen["method"] == "config.get"
```
```python
# apps/backend/tests/unit/services/test_hermes_profiles_patch.py
from core.services import hermes_profiles


def test_patch_deep_merges_and_reuploads(monkeypatch):
    stored = {"model": {"default": "arn:son", "provider": "bedrock"}, "memory": {"memory_enabled": True}}
    uploaded = {}
    monkeypatch.setattr(hermes_profiles, "read_profile_config", lambda o, a: stored)
    monkeypatch.setattr(
        hermes_profiles, "_upload_config_yaml",
        lambda o, a, cfg: uploaded.update(cfg=cfg),
    )
    out = hermes_profiles.patch_profile_config("org_a", "a1", {"memory": {"memory_enabled": False}})
    assert uploaded["cfg"]["memory"] == {"memory_enabled": False}
    assert uploaded["cfg"]["model"]["default"] == "arn:son"  # untouched keys survive the merge
    assert out["memory"]["memory_enabled"] is False
```
```python
# apps/infra/hermes/tests/test_sync_revision.py
from bridge import sync


def test_a_changed_revision_triggers_a_re_pull(monkeypatch):
    pulls = []
    revisions = iter(["1", "1", "2"])
    monkeypatch.setattr(sync, "_read_revision", lambda agent_id: next(revisions))
    monkeypatch.setattr(sync, "_download_profile", lambda agent_id: pulls.append(agent_id))
    sync._REVISIONS.clear()
    sync.ensure_profile("a1")   # cold: pull
    sync.ensure_profile("a1")   # same revision: no pull
    sync.ensure_profile("a1")   # bumped: pull
    assert pulls == ["a1", "a1"]


def test_an_unreadable_revision_does_not_re_pull_a_warm_profile(monkeypatch):
    pulls = []
    monkeypatch.setattr(sync, "_read_revision", lambda agent_id: None)
    monkeypatch.setattr(sync, "_download_profile", lambda agent_id: pulls.append(agent_id))
    sync._REVISIONS.clear()
    sync._REVISIONS["a1"] = "7"
    sync.ensure_profile("a1")
    assert pulls == []  # a transient S3 blip must not re-download a working profile mid-turn
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_runtime_rpc_hermes_config.py tests/unit/services/test_hermes_profiles_patch.py ../infra/hermes/tests/test_sync_revision.py -q`
Expected: FAIL with `AttributeError: module 'core.services.runtime_rpc' has no attribute '_is_agentcore'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/runtime_rpc.py  (add above execute_sync)
from core.services import hermes_profiles, runtime_transport


async def _is_agentcore(owner_id: str) -> bool:
    from core.repositories import container_repo

    return runtime_transport.host_for(await container_repo.get_by_owner_id(owner_id)) == "agentcore"


# ...and inside execute_sync, before the container_rpc.call block:
    if method in ("config.get", "config.patch") and await _is_agentcore(owner_id):
        # The backend is the ONLY writer of a Hermes profile (spec §4.5): the runtime reads
        # config.yaml once per AIAgent construction, so a patch is an S3 rewrite plus a revision
        # bump, and it lands on the agent's next turn. No watcher, no lock, no round trip.
        agent_id = params.get("agentId") or _default_agent_id_for(owner_id)
        if method == "config.get":
            return hermes_profiles.read_profile_config(owner_id, agent_id)
        result = hermes_profiles.patch_profile_config(owner_id, agent_id, params.get("patch") or {})
        await _invalidate(owner_id, method)
        return result
```
```python
# apps/backend/core/services/hermes_profiles.py  (append)
def _s3_get(bucket: str, key: str) -> str | None:
    import boto3
    import botocore.exceptions

    try:
        return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    except botocore.exceptions.ClientError:
        return None


def read_profile_config(owner_id: str, agent_id: str) -> dict:
    body = _s3_get(settings.OWNER_STATE_BUCKET, f"{profile_prefix(owner_id, agent_id)}config.yaml")
    return yaml.safe_load(body) if body else {}


def _deep_merge(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _upload_config_yaml(owner_id: str, agent_id: str, config: dict) -> None:
    prefix = profile_prefix(owner_id, agent_id)
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}config.yaml", yaml.safe_dump(config, sort_keys=False))
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}.revision", str(int(time.time() * 1000)))


def patch_profile_config(owner_id: str, agent_id: str, patch: dict) -> dict:
    config = _deep_merge(read_profile_config(owner_id, agent_id), patch or {})
    _upload_config_yaml(owner_id, agent_id, config)
    return config
```
```python
# apps/infra/hermes/bridge/sync.py  (append)
_REVISIONS: dict[str, str] = {}


def _read_revision(agent_id: str) -> str | None:
    """The profile's ``.revision`` marker, or None when it cannot be read."""
    try:
        key = f"{S3_PREFIX}/{agent_id}/.revision"
        return _s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode().strip()
    except Exception:
        return None


def ensure_profile(agent_id: str) -> None:
    """Pull the profile if we have never seen it, or if the backend bumped its revision.

    One HeadObject-sized read per turn buys hot config patches with no watcher. An unreadable
    revision leaves a WARM profile alone: a transient S3 error must not re-download mid-turn.
    """
    revision = _read_revision(agent_id)
    known = _REVISIONS.get(agent_id)
    if known is not None and (revision is None or revision == known):
        return
    _download_profile(agent_id)
    if revision is not None:
        _REVISIONS[agent_id] = revision
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_runtime_rpc_hermes_config.py tests/unit/services/test_hermes_profiles_patch.py ../infra/hermes/tests/test_sync_revision.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/runtime_rpc.py apps/backend/core/services/hermes_profiles.py apps/infra/hermes/bridge/sync.py apps/backend/tests/unit/services/test_runtime_rpc_hermes_config.py apps/backend/tests/unit/services/test_hermes_profiles_patch.py apps/infra/hermes/tests/test_sync_revision.py
git commit -m "feat(hermes): config.get/patch served from the profile in S3, re-pulled on revision bump

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 14: Integration token refresh rewrites the profile (`.env` + `mcp-tokens/`)

**Files:**
- Modify: `apps/backend/core/services/credentials/mcp_server.py` (`reconcile_integrations`, `:93-137`)
- Modify: `apps/backend/core/services/hermes_profiles.py` (append `refresh_integrations`)
- Create: `apps/backend/tests/unit/services/test_hermes_profiles_integrations.py`

**Interfaces:**
- Consumes: `integration_secret_store.get(owner_id, provider)`, `build_connected_integration_servers`
- Produces: `refresh_integrations(owner_id: str, agent_ids: list[str]) -> None` — rewrites each
  profile's `config.yaml` `mcp_servers`, `.env` and, for OAuth-remote servers,
  `mcp-tokens/<server>.json`, then bumps `.revision`

Report 06 §4.4 splits the two classes. **Actor-refresh servers** (Stripe, PostHog — our backend keeps
no copy): write the token file once at restore and let Hermes's own `OAuthClientProvider` rotate it
in place. **Backend-retained credentials** (`gh_cli`/`aws_cli`/`xurl_cli`-style): re-assert on every
rewrite, exactly as `reconcile_integrations` does today for `openclaw.json`.

> **Not fully concrete.** `HermesTokenStorage`'s on-disk pydantic field names
> (`$HERMES_HOME/mcp-tokens/<server>.json` + `.client.json` + `.meta.json`,
> `/opt/hermes/tools/mcp_oauth.py:228-232,403-421`) were not read field-by-field in the research
> pass. Step 3 starts by reading them; the mapping below is the shape to fill, not a guess to ship.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_profiles_integrations.py
from core.services import hermes_profiles


def test_refresh_rewrites_every_profile_and_bumps_each_revision(monkeypatch):
    uploads = []
    monkeypatch.setattr(hermes_profiles, "read_profile_config", lambda o, a: {"mcp_servers": {}})
    monkeypatch.setattr(hermes_profiles, "_upload_config_yaml", lambda o, a, cfg: uploads.append((a, cfg)))
    monkeypatch.setattr(
        hermes_profiles, "_connected_servers",
        lambda owner_id: {"stripe": {"url": "https://mcp.stripe.com", "auth": "oauth", "requestTimeoutMs": 60000}},
    )
    monkeypatch.setattr(hermes_profiles, "_write_mcp_token_files", lambda o, a, servers: None)

    hermes_profiles.refresh_integrations("org_a", ["a1", "a2"])
    assert [agent for agent, _ in uploads] == ["a1", "a2"]
    for _, cfg in uploads:
        assert cfg["mcp_servers"]["stripe"] == {"url": "https://mcp.stripe.com", "auth": "oauth", "timeout": 60}


def test_refresh_replaces_the_whole_mcp_block_so_a_disconnect_removes_a_server(monkeypatch):
    uploads = []
    monkeypatch.setattr(
        hermes_profiles, "read_profile_config",
        lambda o, a: {"mcp_servers": {"posthog": {"url": "https://old"}}},
    )
    monkeypatch.setattr(hermes_profiles, "_upload_config_yaml", lambda o, a, cfg: uploads.append(cfg))
    monkeypatch.setattr(hermes_profiles, "_connected_servers", lambda owner_id: {})
    monkeypatch.setattr(hermes_profiles, "_write_mcp_token_files", lambda o, a, servers: None)

    hermes_profiles.refresh_integrations("org_a", ["a1"])
    assert uploads[0]["mcp_servers"] == {}


def test_oauth_remote_servers_get_a_token_file_and_key_based_ones_do_not(monkeypatch):
    written = {}
    monkeypatch.setattr(hermes_profiles, "_put_token_file", lambda o, a, name, blob: written.setdefault(name, blob))
    monkeypatch.setattr(
        hermes_profiles, "_integration_credential",
        lambda owner_id, name: {"access_token": "at", "refresh_token": "rt"},
    )
    hermes_profiles._write_mcp_token_files(
        "org_a", "a1",
        {"stripe": {"url": "https://mcp.stripe.com", "auth": "oauth"}, "qbo": {"command": "npx"}},
    )
    assert set(written) == {"stripe"}
    assert written["stripe"]["access_token"] == "at"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles_integrations.py -q`
Expected: FAIL with `AttributeError: module 'core.services.hermes_profiles' has no attribute 'refresh_integrations'`

- [ ] **Step 3: Write minimal implementation**
First read the storage shape, and put the field names in the docstring:
`docker run --rm --entrypoint sed lightbulb/hermes-spike:1 -n '220,240p;395,430p' /opt/hermes/tools/mcp_oauth.py`
```python
# apps/backend/core/services/hermes_profiles.py  (append)
def _connected_servers(owner_id: str) -> dict:
    from core.services.credentials.mcp_server import build_connected_integration_servers_sync

    return build_connected_integration_servers_sync(owner_id)


def _integration_credential(owner_id: str, provider: str) -> dict:
    from core.services.credentials import integration_secret_store

    return integration_secret_store.get_sync(owner_id, provider) or {}


def _put_token_file(owner_id: str, agent_id: str, name: str, blob: dict) -> None:
    import json as _json

    key = f"{profile_prefix(owner_id, agent_id)}mcp-tokens/{name}.json"
    _s3_put(settings.OWNER_STATE_BUCKET, key, _json.dumps(blob))


def _write_mcp_token_files(owner_id: str, agent_id: str, servers: dict) -> None:
    """Seed ``$HERMES_HOME/mcp-tokens/<server>.json`` for OAuth-remote servers only.

    Field names come from HermesTokenStorage (tools/mcp_oauth.py:228-232, 403-421) — read them
    before filling this in. Key-based and stdio servers carry their credential through the
    profile ``.env`` instead and get no token file.

    After the seed, Hermes's own OAuthClientProvider refreshes in place for the life of the
    process (report 06 §4.4) — the backend does not re-push per turn.
    """
    for name, block in (servers or {}).items():
        if block.get("auth") != "oauth":
            continue
        _put_token_file(owner_id, agent_id, name, _integration_credential(owner_id, name))


def refresh_integrations(owner_id: str, agent_ids: list[str]) -> None:
    """Re-assert every connected integration into each profile, then bump the revision.

    The ``mcp_servers`` block is REPLACED, not merged: a disconnected provider has to disappear,
    and a merge would leave its server wired forever (the same reason ``reconcile_integrations``
    rewrites rather than patches today).
    """
    servers = translate_mcp_servers(_connected_servers(owner_id))
    for agent_id in agent_ids:
        config = read_profile_config(owner_id, agent_id)
        config["mcp_servers"] = servers
        _write_mcp_token_files(owner_id, agent_id, _connected_servers(owner_id))
        _upload_config_yaml(owner_id, agent_id, config)
```
```python
# apps/backend/core/services/credentials/mcp_server.py  (in reconcile_integrations)
    from core.services import runtime_transport

    container = await container_repo.get_by_owner_id(owner_id)
    if runtime_transport.host_for(container) == "agentcore":
        from core.services.hermes_profiles import refresh_integrations
        from core.services.paperclip_openclaw_pairing import fetch_paperclip_agents_for_owner

        agents = await fetch_paperclip_agents_for_owner(owner_id)
        return refresh_integrations(owner_id, [a["id"] for a in agents])
    # ... existing openclaw.json path unchanged
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles_integrations.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/hermes_profiles.py apps/backend/core/services/credentials/mcp_server.py apps/backend/tests/unit/services/test_hermes_profiles_integrations.py
git commit -m "feat(hermes): integration refresh rewrites every profile's mcp_servers and token files

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 15: ChatGPT OAuth — stage into the ROOT `auth.json`, sync the rotation back

**Files:**
- Modify: `apps/backend/core/services/container_auth.py`
  (`push_oauth_credential_to_container`, `:87-138`)
- Modify: `apps/backend/core/services/hermes_profiles.py` (append `stage_codex_auth`)
- Modify: `apps/infra/hermes/bridge/sync.py` (append the root-`auth.json` leg of save/restore)
- Create: `apps/backend/tests/unit/services/test_hermes_codex_auth.py`

**Interfaces:**
- Consumes: `oauth_service` DDB row (Fernet-decrypted `access_token`, `refresh_token`, `id_token`)
- Produces: `stage_codex_auth(owner_id: str, tokens: dict) -> None` writing
  `<owner>/hermes/auth.json` (the ROOT home, beside `profiles/`) as
  `{"active_provider": "openai-codex", "providers": {"openai-codex": {"tokens": {...},
  "last_refresh": ..., "auth_mode": "chatgpt"}}}`

Report 06 §2.2: Hermes's `openai-codex` provider implements natively the shape we hand-rolled —
a profile with no `providers.openai-codex` block reads ROOT's grant through the fallback
(`auth.py:737-754`), and a refresh under that profile write-throughs to ROOT's chain
(`auth_codex.py:145-163`), which is exactly what stops OpenAI revoking the whole rotation family.
So the token belongs in the ROOT home, not in six profiles. **The docs contradict the source**
(the Profiles page says a named profile never inherits the root's logins), so step 4 includes a
live two-profile check before this is trusted in prod.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_codex_auth.py
import json

from core.services import hermes_profiles


def test_stage_writes_the_root_auth_json_not_a_per_profile_one(monkeypatch):
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.update({key: body}))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt", "id_token": "it"})
    assert set(puts) == {"org_a/hermes/auth.json"}
    blob = json.loads(puts["org_a/hermes/auth.json"])
    assert blob["active_provider"] == "openai-codex"
    state = blob["providers"]["openai-codex"]
    assert state["tokens"] == {"access_token": "at", "refresh_token": "rt", "id_token": "it"}
    assert state["auth_mode"] == "chatgpt"
    assert state["last_refresh"].endswith("Z")


def test_stage_drops_an_empty_token_rather_than_writing_a_null(monkeypatch):
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.update({key: body}))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt", "id_token": None})
    state = json.loads(puts["org_a/hermes/auth.json"])["providers"]["openai-codex"]
    assert "id_token" not in state["tokens"]


def test_stage_is_a_no_op_without_a_bucket(monkeypatch):
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda *a: puts.setdefault("x", 1))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"})
    assert puts == {}
```
```python
# apps/infra/hermes/tests/test_sync_root_auth.py
from bridge import sync


def test_the_root_auth_json_is_restored_once_and_saved_back_after_a_turn(monkeypatch):
    ops = []
    monkeypatch.setattr(sync, "_download_object", lambda key, dest: ops.append(("get", key)))
    monkeypatch.setattr(sync, "_upload_object", lambda src, key: ops.append(("put", key)))
    sync._ROOT_AUTH_RESTORED.clear()
    sync.restore_root_auth()
    sync.restore_root_auth()
    sync.save_root_auth()
    assert ops == [("get", "auth.json"), ("put", "auth.json")]
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_codex_auth.py ../infra/hermes/tests/test_sync_root_auth.py -q`
Expected: FAIL with `AttributeError: module 'core.services.hermes_profiles' has no attribute 'stage_codex_auth'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/hermes_profiles.py  (append)
def root_home_prefix(owner_id: str) -> str:
    """``<owner>/hermes/`` — the workspace's Hermes ROOT home; profiles live under it."""
    return f"{owner_id}/hermes/"


def stage_codex_auth(owner_id: str, tokens: dict) -> None:
    """Write the owner's ChatGPT grant into the ROOT ``auth.json``, once per connect.

    Not per profile, deliberately (report 06 §2.2): a profile with no ``providers.openai-codex``
    block reads root's grant (auth.py:737-754), and a refresh under that profile rotates ROOT's
    chain (auth_codex.py:145-163). Six copies would each consume the single-use refresh token and
    OpenAI revokes the whole family. This replaces the EFS ``credentials/oauth.json`` +
    clear-auth-sentinel + boot ``doctor --fix`` choreography entirely.
    """
    import json as _json
    from datetime import datetime, timezone

    if not settings.OWNER_STATE_BUCKET:
        return
    payload = {k: v for k, v in tokens.items() if k in ("access_token", "refresh_token", "id_token") and v}
    blob = {
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {
                "tokens": payload,
                "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "auth_mode": "chatgpt",
            }
        },
    }
    _s3_put(settings.OWNER_STATE_BUCKET, f"{root_home_prefix(owner_id)}auth.json", _json.dumps(blob))
```
```python
# apps/backend/core/services/container_auth.py  (in push_oauth_credential_to_container)
    from core.services import runtime_transport

    if runtime_transport.host_for(container) == "agentcore":
        # No EFS stage, no sentinel, no forced redeploy: Hermes re-reads auth.json at credential
        # resolution time, so the next turn picks the new grant up.
        from core.services.hermes_profiles import stage_codex_auth

        return stage_codex_auth(owner_id, tokens)
    # ... existing EFS path unchanged
```
```python
# apps/infra/hermes/bridge/sync.py  (append)
_ROOT_AUTH_RESTORED: set[str] = set()


def restore_root_auth() -> None:
    """Pull ``<owner>/hermes/auth.json`` into the root home, once per process."""
    if "done" in _ROOT_AUTH_RESTORED:
        return
    _download_object("auth.json", HOME / "auth.json")
    _ROOT_AUTH_RESTORED.add("done")


def save_root_auth() -> None:
    """Push the root ``auth.json`` back after a turn — Hermes rotates the refresh token in place
    and the next microVM must start from the rotated chain, not a replayed one."""
    _upload_object(HOME / "auth.json", "auth.json")
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_codex_auth.py ../infra/hermes/tests/test_sync_root_auth.py -q`
Expected: PASS (4 passed)

Then the live check the docs/source contradiction demands (report 06 §2.5):
```bash
docker run --rm -e HERMES_HOME=/opt/data/profiles/a1 -v "$PWD/tmp/hermeshome:/opt/data" \
  --entrypoint python3 lightbulb/hermes-spike:1 -c \
  "from hermes_cli.auth import _load_auth_store, _load_provider_state; \
   print(_load_provider_state(_load_auth_store(), 'openai-codex'))"
```
Expected: the root-seeded `openai-codex` state prints from a profile that has no `auth.json` of
its own. If it prints `None`, the docs are right and the token must be staged per profile with a
single designated refresher — record which in the ledger before M4 closes.

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/hermes_profiles.py apps/backend/core/services/container_auth.py apps/infra/hermes/bridge/sync.py apps/backend/tests/unit/services/test_hermes_codex_auth.py apps/infra/hermes/tests/test_sync_root_auth.py
git commit -m "feat(hermes): ChatGPT OAuth staged into the root auth.json, rotation synced back

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 16: Secrets at boot — the runtime env carries identifiers only

**Files:**
- Modify: `apps/infra/hermes/bridge/sync.py` (append `load_secrets`)
- Modify: `apps/infra/hermes/bridge/server.py` (call it once before serving)
- Create: `apps/infra/hermes/tests/test_sync_secrets.py`

**Interfaces:**
- Consumes: the profile `.env` names `LIGHTBULB_GATE_BEARER_SECRET`,
  `LIGHTBULB_PAPERCLIP_KEY_SECRET`, `LIGHTBULB_BYO_KEY_SECRET`, and the runtime env
  `LIGHTBULB_HOOK_TOKEN_SECRET` / `LIGHTBULB_GATEWAY_TOKEN_SECRET`; AWS Secrets Manager via the
  execution role
- Produces: `load_secrets() -> None` exporting `AWS_BEARER_TOKEN_BEDROCK`,
  `OPENCLAW_HOOK_TOKEN`, `GATEWAY_TOKEN`, `PAPERCLIP_API_KEY`, `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`

Spec §4.9: AgentCore `environmentVariables` are plaintext to anyone with `GetAgentRuntime` — wider
than an ECS task definition's `secrets:` block, and a rotation would only reach new sessions
anyway. Secret paths: `lightbulb/{env}/gate-bearer/{owner}`,
`lightbulb/{env}/hook-token/{owner}`, `lightbulb/{env}/gateway-token/{owner}`,
`lightbulb/{env}/paperclip/{owner}/{agent}`, `lightbulb/{env}/user-keys/{user}/{provider}`
(`key_service._secret_name`, `apps/backend/core/services/key_service.py:35-43`). IAM: the per-owner
role needs `secretsmanager:GetSecretValue` on `lightbulb/{env}/*/{owner}*` plus the existing
user-keys scope — added in Task 19.

- [ ] **Step 1: Write the failing test**
```python
# apps/infra/hermes/tests/test_sync_secrets.py
import os

from bridge import sync


def test_load_secrets_exports_the_gate_bearer_from_its_secret_name(monkeypatch):
    monkeypatch.setenv("LIGHTBULB_GATE_BEARER_SECRET", "lightbulb/dev/gate-bearer/org_a")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setattr(sync, "_secret_value", lambda name: {"lightbulb/dev/gate-bearer/org_a": "tok"}[name])
    sync.load_secrets()
    assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "tok"


def test_load_secrets_never_logs_or_returns_a_value(monkeypatch, caplog):
    monkeypatch.setenv("LIGHTBULB_HOOK_TOKEN_SECRET", "lightbulb/dev/hook-token/org_a")
    monkeypatch.setattr(sync, "_secret_value", lambda name: "supersecret")
    with caplog.at_level("DEBUG"):
        assert sync.load_secrets() is None
    assert "supersecret" not in caplog.text


def test_a_missing_secret_name_is_skipped_not_fatal(monkeypatch):
    for var in ("LIGHTBULB_GATE_BEARER_SECRET", "LIGHTBULB_BYO_KEY_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sync, "_secret_value", lambda name: (_ for _ in ()).throw(AssertionError("called")))
    sync.load_secrets()  # must not raise


def test_an_unreadable_secret_leaves_the_env_var_unset_rather_than_empty(monkeypatch):
    monkeypatch.setenv("LIGHTBULB_GATE_BEARER_SECRET", "lightbulb/dev/gate-bearer/org_a")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setattr(sync, "_secret_value", lambda name: None)
    sync.load_secrets()
    # An empty bearer would make every Bedrock call 401 with a confusing "invalid token";
    # unset makes the adapter say "no credentials", which is the true diagnosis.
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest ../infra/hermes/tests/test_sync_secrets.py -q`
Expected: FAIL with `AttributeError: module 'sync' has no attribute 'load_secrets'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/infra/hermes/bridge/sync.py  (append)
#: profile/runtime env var holding a SECRET NAME -> the process env var the value is exported as.
#: The runtime's own environmentVariables are plaintext on a control-plane resource (spec §4.9),
#: so nothing here is ever the value itself.
_SECRET_ENV = {
    "LIGHTBULB_GATE_BEARER_SECRET": "AWS_BEARER_TOKEN_BEDROCK",
    "LIGHTBULB_HOOK_TOKEN_SECRET": "OPENCLAW_HOOK_TOKEN",
    "LIGHTBULB_GATEWAY_TOKEN_SECRET": "GATEWAY_TOKEN",
    "LIGHTBULB_PAPERCLIP_KEY_SECRET": "PAPERCLIP_API_KEY",
    "LIGHTBULB_BYO_KEY_SECRET": "LIGHTBULB_BYO_KEY",
}


def _secret_value(name: str) -> str | None:
    import boto3

    try:
        return boto3.client("secretsmanager").get_secret_value(SecretId=name)["SecretString"]
    except Exception as exc:  # noqa: BLE001 — never print the id's neighbourhood, just the type
        log.warning("secret %s unreadable: %s", name, type(exc).__name__)
        return None


def load_secrets() -> None:
    """Resolve every ``*_SECRET`` name in the env with the execution role, once at boot.

    A missing NAME is skipped (that owner has no such credential); an unreadable VALUE leaves the
    target var UNSET rather than empty, so the failure reads as "no credentials" instead of an
    invalid-token 401 on every turn.
    """
    for name_var, value_var in _SECRET_ENV.items():
        name = os.environ.get(name_var)
        if not name:
            continue
        value = _secret_value(name)
        if value:
            os.environ[value_var] = value
    byo = os.environ.pop("LIGHTBULB_BYO_KEY", None)
    if byo:
        provider_var = os.environ.get("LIGHTBULB_BYO_PROVIDER_ENV", "")
        if provider_var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            os.environ[provider_var] = byo
```
```python
# apps/infra/hermes/bridge/server.py  (in main(), before serve_forever)
    sync.load_secrets()
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest ../infra/hermes/tests/test_sync_secrets.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/infra/hermes/bridge/sync.py apps/infra/hermes/bridge/server.py apps/infra/hermes/tests/test_sync_secrets.py
git commit -m "feat(hermes): resolve every credential from Secrets Manager at boot, not from runtime env

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 17: `hermes_provisioning.provision_hermes_runtime` — the fresh-provision path

**Files:**
- Create: `apps/backend/core/services/hermes_provisioning.py`
- Create: `apps/backend/tests/unit/services/test_hermes_provisioning.py`

**Interfaces:**
- Consumes: `agentcore_runtime.ensure_agentcore_trust` / `runtime_name_for` / `IDLE_TIMEOUT_S` /
  `MAX_LIFETIME_S` (`apps/backend/core/services/agentcore_runtime.py:43-105,200-204`),
  `bedrock_aip.ensure_owner_aips` / `ensure_owner_bedrock_role` / `_owner_role_name`,
  `bedrock_gate_tokens.mint_gate_token`, `hermes_profiles.write_agent_profile` / `upload_profile`,
  `provisioning_landing.build_provisioned_env`,
  `paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner`
- Produces (verbatim): `async provision_hermes_runtime(owner_id: str) -> str` returning the runtime
  ARN and writing `runtime_host="agentcore"`, `agentcore_runtime_arn`, `status="running"` on the
  containers row; plus the pure `build_hermes_runtime_request(...) -> dict`

Spec §4.9 and report 04 risk 5: `agentcore_runtime.ensure_runtime` is a MIGRATION tool — it raises
without an existing EFS access point and requires `status == "stopped"` on ECS
(`agentcore_runtime.py:235-239`). A fresh Hermes workspace has none of that, so this is a parallel
path, not a patch. `CreateAgentRuntime` is one synchronous call, so there is no watchdog and no
`wait_for_running` poll; the claim lease still fences it.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_provisioning.py
import pytest

from core.services import hermes_provisioning


def test_the_request_has_no_efs_leg_and_points_at_the_hermes_image():
    req = hermes_provisioning.build_hermes_runtime_request(
        owner_id="org_a",
        image_uri="877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-v1",
        role_arn="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a",
        subnets=["subnet-1"],
        security_groups=["sg-1"],
        environment={"ISOL8_OWNER_ID": "org_a"},
    )
    assert "filesystemConfigurations" not in req
    assert req["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"].endswith("hermes-agent-arm64:h-v1")
    assert req["networkConfiguration"]["networkMode"] == "VPC"
    assert req["protocolConfiguration"]["serverProtocol"] == "HTTP"
    assert req["lifecycleConfiguration"]["idleRuntimeSessionTimeout"] == 4 * 60 * 60
    assert req["tags"]["ManagedBy"] == "lightbulb-backend"


def test_the_runtime_env_carries_identifiers_only_and_the_owner_first_prefix():
    env = hermes_provisioning.build_hermes_runtime_env(
        owner_id="org_a", api_base="https://api-dev.flickmeon.ai", bucket="lightbulb-dev-openclaw-replicas",
    )
    assert env["S3_PREFIX"] == "org_a/hermes"     # owner FIRST — the IAM grant is <owner>/*
    assert env["S3_BUCKET"] == "lightbulb-dev-openclaw-replicas"
    assert env["HERMES_HOME"] == "/opt/data"
    assert env["LIGHTBULB_HOOK_TOKEN_SECRET"] == "lightbulb/dev/hook-token/org_a"
    assert env["LIGHTBULB_GATEWAY_TOKEN_SECRET"] == "lightbulb/dev/gateway-token/org_a"
    for key, value in env.items():
        assert not value.startswith(("sk-", "pcp_")), key
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
    assert "OPENCLAW_HOOK_TOKEN" not in env


@pytest.mark.asyncio
async def test_provision_writes_the_row_fields_every_existing_seam_reads(monkeypatch):
    updated = {}
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:aws:bedrock-agentcore:...:runtime/r1")
    monkeypatch.setattr(hermes_provisioning, "_prepare_owner_aws", _fake_prepare)
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
    monkeypatch.setattr(
        hermes_provisioning.container_repo, "update_fields",
        lambda owner_id, fields: updated.update(fields),
    )
    arn = await hermes_provisioning.provision_hermes_runtime("org_a")
    assert arn == "arn:aws:bedrock-agentcore:...:runtime/r1"
    assert updated["runtime_host"] == "agentcore"
    assert updated["agentcore_runtime_arn"] == arn
    assert updated["status"] == "running"


@pytest.mark.asyncio
async def test_provision_refuses_without_a_configured_image(monkeypatch):
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", "")
    with pytest.raises(RuntimeError, match="AGENTCORE_HERMES_IMAGE"):
        await hermes_provisioning.provision_hermes_runtime("org_a")


@pytest.mark.asyncio
async def test_provision_is_idempotent_on_a_row_that_already_has_a_runtime(monkeypatch):
    monkeypatch.setattr(
        hermes_provisioning.container_repo, "get_by_owner_id",
        _row({"agentcore_runtime_arn": "arn:existing"}),
    )
    created = {}
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: created.setdefault("x", 1))
    assert await hermes_provisioning.provision_hermes_runtime("org_a") == "arn:existing"
    assert created == {}
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_provisioning.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.hermes_provisioning'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/hermes_provisioning.py
"""Fresh-provision path for a Hermes-on-AgentCore workspace.

NOT a patch of ``agentcore_runtime.ensure_runtime``: that function is a MIGRATION tool and
hard-requires an existing EFS access point and a ``stopped`` ECS row (agentcore_runtime.py:235-239),
which a fresh workspace has never had. Patching it in place would inherit "must come from ECS" into
new-workspace provisioning (report 04, risk 5).

What is reused verbatim: the IAM/naming scaffolding (``runtime_name_for``,
``ensure_agentcore_trust``), the owner's AIPs and Bedrock role, ``mint_gate_token``, and the
lifecycle constants. What is dropped: ``filesystemConfigurations`` (no EFS — profiles are S3),
the OpenClaw env builder, the SFN watchdog and the ``wait_for_running`` poll loop
(``CreateAgentRuntime`` is synchronous, seconds).
"""

from __future__ import annotations

import logging

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services.agentcore_runtime import (
    IDLE_TIMEOUT_S,
    MAX_LIFETIME_S,
    ensure_agentcore_trust,
    runtime_name_for,
)

logger = logging.getLogger(__name__)


def build_hermes_runtime_env(*, owner_id: str, api_base: str, bucket: str) -> dict[str, str]:
    """The bridge's environment. Identifiers ONLY — every credential is a Secrets Manager NAME
    that ``sync.load_secrets`` resolves at boot with the execution role (Task 16)."""
    env = settings.ENVIRONMENT or "dev"
    return {
        "ISOL8_OWNER_ID": owner_id,
        "ISOL8_API_URL": api_base.rstrip("/"),
        "HERMES_HOME": "/opt/data",
        "S3_BUCKET": bucket,
        # Owner FIRST: the per-owner S3 grant is ``<bucket>/<owner>/*``
        # (bedrock_aip.build_owner_litestream_s3_policy). The spike's ``hermes/<owner>`` default is
        # outside every owner's grant and AccessDenies on the first restore.
        "S3_PREFIX": f"{owner_id}/hermes",
        "LIGHTBULB_HOOK_TOKEN_SECRET": f"lightbulb/{env}/hook-token/{owner_id}",
        "LIGHTBULB_GATEWAY_TOKEN_SECRET": f"lightbulb/{env}/gateway-token/{owner_id}",
        "LIGHTBULB_GATE_BEARER_SECRET": f"lightbulb/{env}/gate-bearer/{owner_id}",
    }


def build_hermes_runtime_request(
    *,
    owner_id: str,
    image_uri: str,
    role_arn: str,
    subnets: list[str],
    security_groups: list[str],
    environment: dict[str, str],
) -> dict:
    return {
        "agentRuntimeName": runtime_name_for(owner_id),
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {"securityGroups": security_groups, "subnets": subnets},
        },
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": IDLE_TIMEOUT_S,
            "maxLifetime": MAX_LIFETIME_S,
        },
        "environmentVariables": environment,
        "tags": {"owner_id": owner_id, "Environment": settings.ENVIRONMENT, "ManagedBy": "lightbulb-backend"},
    }


def _create_runtime(request: dict) -> str:
    import boto3

    return boto3.client("bedrock-agentcore-control").create_agent_runtime(**request)["agentRuntimeArn"]


async def _prepare_owner_aws(owner_id: str) -> str:
    """AIPs, the per-owner Bedrock role, AgentCore trust and a freshly minted gate bearer."""
    from core.billing.bedrock_aip import _owner_role_name, ensure_owner_aips, ensure_owner_bedrock_role
    from core.billing.bedrock_gate_tokens import mint_gate_token
    from core.secrets_manager import create_or_update_secret

    aip_arns = await ensure_owner_aips(owner_id)
    role_arn = await ensure_owner_bedrock_role(owner_id, aip_arns)
    await ensure_agentcore_trust(owner_id, _owner_role_name(owner_id), role_arn.split(":")[4])
    bearer = await mint_gate_token(owner_id, aip_arns)
    # The runtime env carries the NAME; the value only ever lives here.
    await create_or_update_secret(f"lightbulb/{settings.ENVIRONMENT}/gate-bearer/{owner_id}", bearer)
    return role_arn


async def _write_profiles(owner_id: str) -> None:
    from core.billing.bedrock_aip import ensure_owner_aips
    from core.services.container_provision import resolve_provider_choice
    from core.services.credentials.mcp_server import build_connected_integration_servers
    from core.services.hermes_profiles import upload_profile, write_agent_profile
    from core.services.paperclip_openclaw_pairing import fetch_paperclip_agents_for_owner
    from core.services.provisioning_landing import build_provisioned_env

    provider_choice, _ = await resolve_provider_choice(owner_id)
    aip_arns = await ensure_owner_aips(owner_id)
    servers = await build_connected_integration_servers(owner_id)
    integrations = [{"key": key, **block} for key, block in servers.items()]
    env_vars = await build_provisioned_env(owner_id)
    for agent in await fetch_paperclip_agents_for_owner(owner_id):
        files = write_agent_profile(
            owner_id, agent,
            provider_choice=provider_choice, aip_arns=aip_arns,
            integrations=integrations, env_vars=env_vars,
        )
        await run_in_thread(upload_profile, owner_id, agent["id"], files)


async def provision_hermes_runtime(owner_id: str) -> str:
    """Create this workspace's AgentCore runtime and point its container row at it.

    Idempotent: a row that already carries ``agentcore_runtime_arn`` is returned as-is.
    """
    if not settings.AGENTCORE_HERMES_IMAGE:
        raise RuntimeError("AGENTCORE_HERMES_IMAGE is not configured for this environment")
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    existing = row.get("agentcore_runtime_arn")
    if existing:
        return existing
    role_arn = await _prepare_owner_aws(owner_id)
    await _write_profiles(owner_id)
    request = build_hermes_runtime_request(
        owner_id=owner_id,
        image_uri=settings.AGENTCORE_HERMES_IMAGE,
        role_arn=role_arn,
        subnets=[s.strip() for s in settings.ECS_SUBNETS.split(",") if s.strip()],
        security_groups=[settings.ECS_SECURITY_GROUP_ID] if settings.ECS_SECURITY_GROUP_ID else [],
        environment=build_hermes_runtime_env(
            owner_id=owner_id, api_base=settings.PUBLIC_API_BASE, bucket=settings.OWNER_STATE_BUCKET
        ),
    )
    arn = await run_in_thread(_create_runtime, request)
    # ``running`` from here: AgentCore wakes on invoke, so every "stopped -> start" door must see a
    # live workspace, and the ECS manager's start/stop chokepoints already no-op for this row.
    await container_repo.update_fields(
        owner_id, {"runtime_host": "agentcore", "agentcore_runtime_arn": arn, "status": "running"}
    )
    logger.info("hermes runtime created owner=%s arn=%s", owner_id, arn)
    return arn
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_provisioning.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/hermes_provisioning.py apps/backend/tests/unit/services/test_hermes_provisioning.py
git commit -m "feat(hermes): fresh-provision path — profiles to S3, CreateAgentRuntime, row fields

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 18: Wire step 1 behind the `HERMES_RUNTIME` flag

**Files:**
- Modify: `apps/backend/scripts/provision_runner.py` (`run()`, the container leg at `:300-317`)
- Modify: `apps/backend/core/config.py` (add `HERMES_RUNTIME: bool = False`)
- Create: `apps/backend/tests/unit/scripts/test_provision_runner_hermes.py`

**Interfaces:**
- Consumes: `settings.HERMES_RUNTIME`, `hermes_provisioning.provision_hermes_runtime`
- Produces: step 1 dispatching to Hermes instead of `ecs_manager.provision_user_container`;
  step 0 (`_seed_owner_paperclip`, `:135-229`) and the claim/attempt fencing unchanged

Spec §4.9: step 0 is reused verbatim. The flag is an environment switch, not a per-owner one: a
workspace's runtime is decided at provision and recorded on the row, and `runtime_transport.host_for`
reads the row from then on.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/scripts/test_provision_runner_hermes.py
import pytest

from scripts import provision_runner


@pytest.mark.asyncio
async def test_with_the_flag_on_step_1_provisions_a_hermes_runtime(monkeypatch, runner_env):
    calls = []
    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", True)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning, "provision_hermes_runtime",
        lambda owner_id: calls.append(owner_id) or "arn:r1",
    )
    monkeypatch.setattr(provision_runner, "_get_manager", _explodes_if_called)
    assert await provision_runner.run("org_a", "claim-1") == provision_runner.EXIT_OK
    assert calls == ["org_a"]


@pytest.mark.asyncio
async def test_with_the_flag_on_there_is_no_wait_for_running_poll(monkeypatch, runner_env):
    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", True)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "provision_hermes_runtime", lambda o: "arn:r1")
    monkeypatch.setattr(provision_runner, "_get_manager", _explodes_if_called)
    await provision_runner.run("org_a", "claim-1")  # _get_manager would raise if the ECS leg ran


@pytest.mark.asyncio
async def test_step_0_still_runs_and_still_refuses_a_non_org_owner(monkeypatch, runner_env):
    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", True)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _refuses_non_org)
    assert await provision_runner.run("user_a", "claim-1") == 10


@pytest.mark.asyncio
async def test_with_the_flag_off_the_ecs_leg_is_unchanged(monkeypatch, runner_env):
    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", False)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning, "provision_hermes_runtime", _explodes_if_called,
    )
    await provision_runner.run("org_a", "claim-1")  # the ECS manager double is what runs
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_provision_runner_hermes.py -q`
Expected: FAIL with `AttributeError: module 'scripts.provision_runner' has no attribute 'hermes_provisioning'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/config.py  (beside AGENTCORE_GATEWAY_IMAGE at :375)
    #: Provision new workspaces onto Hermes/AgentCore instead of ECS. Per ENVIRONMENT, not per
    #: owner: the choice is recorded on the container row at provision and read from there after.
    HERMES_RUNTIME: bool = False
    AGENTCORE_HERMES_IMAGE: str = ""
```
```python
# apps/backend/scripts/provision_runner.py  (in run(), replacing the container leg)
from core.config import settings
from core.services import hermes_provisioning

        if await _container_already_healthy(manager, owner_id, pre_row):
            logger.info("container leg: owner %s already healthy — skipping provision", owner_id)
        elif settings.HERMES_RUNTIME:
            # CreateAgentRuntime is one synchronous call, so there is no service to poll and no
            # generation to re-read: provision_hermes_runtime writes the terminal row itself.
            await hermes_provisioning.provision_hermes_runtime(owner_id)
            return EXIT_OK
        else:
            provider_choice, byo_provider = await _resolve_provider(owner_id)
            # ... existing ECS leg unchanged
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_provision_runner_hermes.py tests/unit/scripts -q`
Expected: PASS (4 new + the existing provision-runner suite)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/scripts/provision_runner.py apps/backend/core/config.py apps/backend/tests/unit/scripts/test_provision_runner_hermes.py
git commit -m "feat(hermes): provision step 1 dispatches to AgentCore behind HERMES_RUNTIME

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 19: CDK — Hermes image var, the bucket-env rename, ECR + secrets grants, committed `deploy.yml`

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts` (`backendContainerEnvironment`, `:1391-1414`)
- Modify: `apps/infra/lib/stacks/container-stack.ts` (`AgentCoreEcrPull` at `:363-367`,
  `ownerBedrockBasePolicy`; and the boundary's Secrets Manager statement)
- Modify: `apps/backend/core/config.py` (`OWNER_STATE_BUCKET` with a `LITESTREAM_BUCKET` alias)
- Create: `apps/infra/test/hermes-runtime.test.ts`
- Modify: `.github/workflows/deploy.yml` (regenerated by `cdk synth`, committed)

**Interfaces:**
- Produces: `AGENTCORE_HERMES_IMAGE` (dev only, empty in prod — same deliberate refuse-until-promoted
  shape as `AGENTCORE_GATEWAY_IMAGE`), `HERMES_RUNTIME` (`"false"` in both envs at this point),
  `OWNER_STATE_BUCKET` replacing `LITESTREAM_BUCKET`, ECR pull on
  `repository/lightbulb/hermes-agent-arm64` in the per-owner base policy **and** the boundary
  (mirroring the dev CLI grants already made: base v6 / boundary v5), and
  `secretsmanager:GetSecretValue` on `lightbulb/{env}/*` scoped to the owner

**No new bucket and no new prefix grant** (coordinator cut 2): profiles ride
`lightbulb-{env}-openclaw-replicas` under `<owner>/hermes/`, already inside
`build_owner_litestream_s3_policy`'s `<owner>/*`.

- [ ] **Step 1: Write the failing test**
```ts
// apps/infra/test/hermes-runtime.test.ts
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";

import { synthStage } from "./helpers";

describe("hermes runtime wiring", () => {
  it("gives dev a Hermes image and leaves prod empty so a promotion is deliberate", () => {
    const dev = Template.fromStack(synthStage(new App(), "dev").serviceStack);
    const prod = Template.fromStack(synthStage(new App(), "prod").serviceStack);
    const envOf = (t: Template) => {
      const [def] = Object.values(t.findResources("AWS::ECS::TaskDefinition"));
      return Object.fromEntries(
        (def as any).Properties.ContainerDefinitions[0].Environment.map((e: any) => [e.Name, e.Value]),
      );
    };
    expect(envOf(dev).AGENTCORE_HERMES_IMAGE).toMatch(/lightbulb\/hermes-agent-arm64:/);
    expect(envOf(prod).AGENTCORE_HERMES_IMAGE).toBe("");
  });

  it("names the state bucket for what it holds, not for litestream", () => {
    const dev = Template.fromStack(synthStage(new App(), "dev").serviceStack);
    const [def] = Object.values(dev.findResources("AWS::ECS::TaskDefinition"));
    const names = (def as any).Properties.ContainerDefinitions[0].Environment.map((e: any) => e.Name);
    expect(names).toContain("OWNER_STATE_BUCKET");
    expect(names).not.toContain("LITESTREAM_BUCKET");
  });

  it("lets the per-owner role pull the Hermes image in both the base policy and the boundary", () => {
    const container = Template.fromStack(synthStage(new App(), "dev").containerStack);
    const docs = JSON.stringify(container.findResources("AWS::IAM::ManagedPolicy"));
    expect(docs).toContain("repository/lightbulb/hermes-agent-arm64");
    // base v6 + boundary v5 — the grant exists in BOTH or the role is capped out of its own image
    expect(docs.match(/repository\/lightbulb\/hermes-agent-arm64/g)!.length).toBeGreaterThanOrEqual(2);
  });

  it("lets the per-owner role read its own secrets at boot", () => {
    const container = Template.fromStack(synthStage(new App(), "dev").containerStack);
    const docs = JSON.stringify(container.findResources("AWS::IAM::ManagedPolicy"));
    expect(docs).toContain("secretsmanager:GetSecretValue");
    expect(docs).toContain("secret:lightbulb/dev/");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/infra && npx vitest run test/hermes-runtime.test.ts`
Expected: FAIL with `expected undefined to match /lightbulb\/hermes-agent-arm64:/`

- [ ] **Step 3: Write minimal implementation**
```ts
// apps/infra/lib/stacks/service-stack.ts  (in backendContainerEnvironment, replacing LITESTREAM_BUCKET)
      // The per-owner state bucket. Named for what it holds, not for the tool that first used it:
      // OpenClaw's litestream replicas AND (under <owner>/hermes/) the Hermes profile set both
      // live here, inside the same per-owner <owner>/* IAM grant. No second bucket.
      OWNER_STATE_BUCKET: `lightbulb-${env}-openclaw-replicas`,
      // The Hermes bridge image a workspace's AgentCore runtime runs. DEV ONLY: empty in prod makes
      // provision_hermes_runtime refuse, so nothing moves a prod workspace until it is promoted.
      AGENTCORE_HERMES_IMAGE:
        env === "dev"
          ? `${this.account}.dkr.ecr.${this.region}.amazonaws.com/lightbulb/hermes-agent-arm64:h-v1`
          : "",
      // Flipped to "true" per environment only when the milestone's dev proof has landed.
      HERMES_RUNTIME: "false",
```
```ts
// apps/infra/lib/stacks/container-stack.ts  (AgentCoreEcrPull, and the same resource in the boundary)
        new iam.PolicyStatement({
          sid: "AgentCoreEcrPull",
          actions: ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
          resources: [
            `arn:aws:ecr:${this.region}:${this.account}:repository/lightbulb/openclaw-gateway-arm64`,
            // The Hermes bridge image. Mirrors the grants already made by CLI in dev
            // (owner base policy v6, boundary v5) — a boundary that omits it caps the role out of
            // pulling its own image, and the runtime fails to start with no useful error.
            `arn:aws:ecr:${this.region}:${this.account}:repository/lightbulb/hermes-agent-arm64`,
          ],
        }),
        // The runtime env carries secret NAMES only (spec §4.9); the bridge resolves them at boot.
        new iam.PolicyStatement({
          sid: "OwnerSecretsRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${props.environment}/*`],
        }),
```
```python
# apps/backend/core/config.py  (replacing LITESTREAM_BUCKET at :372)
    from pydantic import AliasChoices, Field

    #: Per-owner state bucket (``lightbulb-{env}-openclaw-replicas``): OpenClaw litestream replicas
    #: under ``<owner>/`` and Hermes profiles under ``<owner>/hermes/``. The old name is still
    #: accepted so a CDK deploy and a backend roll can land in either order.
    OWNER_STATE_BUCKET: str = Field(
        "", validation_alias=AliasChoices("OWNER_STATE_BUCKET", "LITESTREAM_BUCKET")
    )
```
Then the mechanical rename of the ~19 readers and ~12 test references:
`cd apps/backend && grep -rl "LITESTREAM_BUCKET" core routers scripts tests | xargs sed -i '' 's/LITESTREAM_BUCKET/OWNER_STATE_BUCKET/g'`

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/infra && npx vitest run test/hermes-runtime.test.ts && npx cdk synth >/dev/null && cd ../backend && uv run pytest tests/unit -q -k "litestream or owner_state or replica"`
Expected: PASS (4 infra tests; backend suite green), and `git status` shows a regenerated
`.github/workflows/deploy.yml` to commit — `infra-ci.yml` fails the PR without it.

- [ ] **Step 5: Commit**
```bash
git add apps/infra/lib/stacks/service-stack.ts apps/infra/lib/stacks/container-stack.ts apps/infra/test/hermes-runtime.test.ts apps/backend/core/config.py apps/backend/core apps/backend/routers apps/backend/scripts apps/backend/tests .github/workflows/deploy.yml
git commit -m "feat(infra): AGENTCORE_HERMES_IMAGE, owner-state bucket rename, Hermes ECR + secrets grants

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 20: The production Hermes image and its build workflow

**Files:**
- Create: `apps/infra/hermes/Dockerfile` (production; `Dockerfile.spike` stays for local work)
- Create: `.github/workflows/build-hermes-image.yml`
- Create: `apps/infra/test/hermes-image-workflow.test.ts`

**Interfaces:**
- Consumes: `apps/infra/hermes/{server,turn,sync}.py`, `plugins/`, `managed/config.yaml`,
  `skills/` (Task 24), the pinned `nousresearch/hermes-agent` base
- Produces: ECR `lightbulb/hermes-agent-arm64:h-v<N>` built on changes under `apps/infra/hermes/**`

Mirrors `.github/workflows/build-openclaw-image.yml`: path-filtered, builds arm64, pushes to ECR,
and **never writes the version pin** — a human lands the `AGENTCORE_HERMES_IMAGE` tag flip, the same
promotion gate the OpenClaw image has.

- [ ] **Step 1: Write the failing test**
```ts
// apps/infra/test/hermes-image-workflow.test.ts
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { parse } from "yaml";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "../../..");
const wf = parse(readFileSync(join(ROOT, ".github/workflows/build-hermes-image.yml"), "utf8"));
const dockerfile = readFileSync(join(ROOT, "apps/infra/hermes/Dockerfile"), "utf8");

describe("build-hermes-image", () => {
  it("is path-filtered on the hermes directory only", () => {
    expect(wf.on.push.paths).toContain("apps/infra/hermes/**");
    expect(wf.on.push.paths).not.toContain("apps/backend/**");
  });

  it("builds arm64 — AgentCore runtimes are arm64 only", () => {
    expect(JSON.stringify(wf)).toContain("linux/arm64");
  });

  it("pushes to the Hermes ECR repo and never writes a version pin", () => {
    const text = JSON.stringify(wf);
    expect(text).toContain("lightbulb/hermes-agent-arm64");
    expect(text).not.toContain("AGENTCORE_HERMES_IMAGE");
  });

  it("the production image pins its base, bakes the plugins, skills and managed overlay", () => {
    expect(dockerfile).toMatch(/^FROM nousresearch\/hermes-agent:\d/m);   // pinned, never :latest
    expect(dockerfile).toContain("COPY plugins/ /opt/hermes/plugins/");
    expect(dockerfile).toContain("COPY skills/ /opt/hermes-skills/");
    expect(dockerfile).toContain("COPY managed/config.yaml /etc/hermes/config.yaml");
  });

  it("the production image carries no diagnostic hatch and no ws echo", () => {
    const server = readFileSync(join(ROOT, "apps/infra/hermes/bridge/server.py"), "utf8");
    expect(server).not.toContain("diagnostic");
    expect(server).not.toContain("_ws_echo");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/infra && npx vitest run test/hermes-image-workflow.test.ts`
Expected: FAIL with `ENOENT: no such file or directory, open '.../.github/workflows/build-hermes-image.yml'`

- [ ] **Step 3: Write minimal implementation**
```dockerfile
# apps/infra/hermes/Dockerfile
# The production Hermes-on-AgentCore image. Pinned base, no diagnostic hatch, no :latest.
FROM nousresearch/hermes-agent:0.21.3

# The bundled Anthropic SDK (0.87) predates AnthropicBedrock's bearer mode, which is how the
# bedrock-gate is reached. The assertion is the gate: a silent downgrade breaks every turn.
RUN uv pip install --python "$(command -v python3)" --upgrade "anthropic>=1.0" \
 && python3 -c "import inspect; from anthropic import AnthropicBedrock; assert 'api_key' in inspect.signature(AnthropicBedrock.__init__).parameters" \
 && python3 -c "from run_agent import AIAgent; print('hermes imports ok')"

# Linux skill binaries the bundled skills shell out to (the OpenClaw extended image's set).
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl jq ripgrep unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY plugins/ /opt/hermes/plugins/
COPY skills/ /opt/hermes-skills/
COPY server.py turn.py sync.py /opt/bridge/
COPY managed/config.yaml /etc/hermes/config.yaml
RUN chown root:root /etc/hermes/config.yaml && chmod 0444 /etc/hermes/config.yaml \
 && mkdir -p /opt/data && chown -R 1000:1000 /opt/data /opt/bridge /opt/hermes-skills

USER 1000
ENV HERMES_HOME=/opt/data HERMES_HEADLESS=1 PYTHONUNBUFFERED=1 AGENTCORE_PORT=8080
EXPOSE 8080
ENTRYPOINT []
CMD ["python3", "/opt/bridge/server.py"]
```
```yaml
# .github/workflows/build-hermes-image.yml
name: build-hermes-image

on:
  push:
    branches: [main]
    paths:
      - "apps/infra/hermes/**"
      - ".github/workflows/build-hermes-image.yml"
  workflow_dispatch:

concurrency:
  group: build-hermes-image
  cancel-in-progress: false

permissions:
  id-token: write
  contents: read

jobs:
  build:
    runs-on: ${{ vars.CI_RUNNER || 'ubuntu-latest' }}
    steps:
      - uses: actions/checkout@v5
      - uses: aws-actions/configure-aws-credentials@v5
        with:
          role-to-assume: ${{ secrets.IMAGE_BUILDER_ROLE_ARN }}
          aws-region: us-east-1
      - uses: aws-actions/amazon-ecr-login@v2
        id: ecr
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - name: Build and push (arm64 — AgentCore runtimes are arm64 only)
        uses: docker/build-push-action@v6
        with:
          context: apps/infra/hermes
          file: apps/infra/hermes/Dockerfile
          platforms: linux/arm64
          push: true
          # The tag is the pin's CANDIDATE. This workflow never edits
          # service-stack.ts: promotion is a human landing the image var, same as OpenClaw's.
          tags: ${{ steps.ecr.outputs.registry }}/lightbulb/hermes-agent-arm64:h-${{ github.run_number }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/infra && npx vitest run test/hermes-image-workflow.test.ts`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/infra/hermes/Dockerfile .github/workflows/build-hermes-image.yml apps/infra/test/hermes-image-workflow.test.ts
git commit -m "build(hermes): production image + arm64 ECR build workflow (manual promotion)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 21: The gate-token supersede drain reads `GetAgentRuntime`, not ECS task definitions

**Files:**
- Modify: `apps/backend/core/billing/bedrock_gate_tokens.py`
  (`_presentable_gate_token_hashes`, `:259-372`)
- Modify: `apps/backend/tests/unit/billing/test_bedrock_gate_tokens.py` (append)

**Interfaces:**
- Consumes: the container row's `runtime_host` / `agentcore_runtime_arn`,
  `bedrock-agentcore-control.get_agent_runtime(agentRuntimeId=...)`
- Produces: `_agentcore_presentable_hashes(owner_id, runtime_arn) -> dict[str, str]` — the hashes an
  AgentCore runtime can still present, mapped to the reason, in the same shape the ECS half returns

Report 04 risk 3: today the drain decides a token is safe to revoke by reading which token is baked
into the owner's **live ECS task definitions** (`describe_task_definition` / `list_tasks` /
`describe_services`, `:167-258`). For an AgentCore owner that enumeration returns nothing, which
reads downstream as "no task presents that token" — the deletion decision. Left unported it revokes
a bearer a live runtime is using. The AgentCore version is simpler: one runtime per owner, so one
`GetAgentRuntime` and no per-task enumeration.

The same `_INCOMPLETE_NOTE` discipline applies: an unreadable runtime is **unknown**, not empty, and
must raise so the caller keeps the rows.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/billing/test_bedrock_gate_tokens.py  (append)
import pytest

from core.billing import bedrock_gate_tokens as mod


@pytest.mark.asyncio
async def test_an_agentcore_owner_presents_the_token_named_in_its_runtime_env(monkeypatch):
    monkeypatch.setattr(mod, "_container_row", _row({"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:r1"}))
    monkeypatch.setattr(
        mod, "_describe_agent_runtime",
        lambda arn: {"environmentVariables": {"LIGHTBULB_GATE_BEARER_SECRET": "lightbulb/dev/gate-bearer/org_a"}},
    )
    monkeypatch.setattr(mod, "_secret_value", lambda name: "live-token")
    presentable = await mod._presentable_gate_token_hashes("org_a")
    assert mod.hash_token("live-token") in presentable
    assert presentable[mod.hash_token("live-token")].startswith("agentcore runtime")


@pytest.mark.asyncio
async def test_an_agentcore_owner_never_touches_the_ecs_apis(monkeypatch):
    monkeypatch.setattr(mod, "_container_row", _row({"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:r1"}))
    monkeypatch.setattr(mod, "_describe_agent_runtime", lambda arn: {"environmentVariables": {}})
    monkeypatch.setattr(mod, "_owner_service_view", _explodes_if_called)
    assert await mod._presentable_gate_token_hashes("org_a") == {}


@pytest.mark.asyncio
async def test_an_unreadable_runtime_raises_so_the_caller_keeps_the_rows(monkeypatch):
    monkeypatch.setattr(mod, "_container_row", _row({"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:r1"}))

    def boom(arn):
        raise RuntimeError("throttled")

    monkeypatch.setattr(mod, "_describe_agent_runtime", boom)
    with pytest.raises(RuntimeError):
        await mod._presentable_gate_token_hashes("org_a")


@pytest.mark.asyncio
async def test_an_ecs_owner_still_reads_task_definitions(monkeypatch):
    monkeypatch.setattr(mod, "_container_row", _row({"runtime_host": "ecs"}))
    monkeypatch.setattr(mod, "_describe_agent_runtime", _explodes_if_called)
    # existing ECS doubles from this module's fixtures apply
    await mod._presentable_gate_token_hashes("org_a")
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/billing/test_bedrock_gate_tokens.py -q -k agentcore`
Expected: FAIL with `AttributeError: module 'core.billing.bedrock_gate_tokens' has no attribute '_describe_agent_runtime'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/billing/bedrock_gate_tokens.py  (add above _presentable_gate_token_hashes)
def _describe_agent_runtime(runtime_arn: str) -> dict:
    import boto3

    return boto3.client("bedrock-agentcore-control").get_agent_runtime(
        agentRuntimeId=runtime_arn.rsplit("/", 1)[-1]
    )


def _secret_value(name: str) -> str | None:
    import boto3

    return boto3.client("secretsmanager").get_secret_value(SecretId=name)["SecretString"]


async def _container_row(owner_id: str) -> dict | None:
    from core.repositories import container_repo

    return await container_repo.get_by_owner_id(owner_id, consistent=True)


async def _agentcore_presentable_hashes(owner_id: str, runtime_arn: str) -> dict[str, str]:
    """Hashes an AgentCore runtime can still present.

    One runtime per owner, so this is one call and no enumeration — simpler than the ECS half
    (report 04, risk 3). The runtime env carries the secret NAME (spec §4.9), so the live value is
    resolved from Secrets Manager and hashed the same way a task-def value is.

    An exception propagates on purpose: an unreadable runtime is UNKNOWN, not empty, and reading it
    as empty is the deletion decision (_INCOMPLETE_NOTE).
    """
    runtime = await run_in_thread(_describe_agent_runtime, runtime_arn)
    name = (runtime.get("environmentVariables") or {}).get("LIGHTBULB_GATE_BEARER_SECRET")
    if not name:
        return {}
    value = await run_in_thread(_secret_value, name)
    if not value:
        return {}
    return {hash_token(value): f"agentcore runtime {runtime_arn.rsplit('/', 1)[-1]} env -> {name}"}


# ...and as the first lines of _presentable_gate_token_hashes:
    row = await _container_row(owner_id) or {}
    if row.get("runtime_host") == "agentcore" and row.get("agentcore_runtime_arn"):
        return await _agentcore_presentable_hashes(owner_id, row["agentcore_runtime_arn"])
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/billing/test_bedrock_gate_tokens.py -q`
Expected: PASS (4 new + the existing suite)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/billing/bedrock_gate_tokens.py apps/backend/tests/unit/billing/test_bedrock_gate_tokens.py
git commit -m "fix(billing): gate-token drain reads GetAgentRuntime for AgentCore owners

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 22: Park/wake as a backend judgement from `last_interactive_at`

**Files:**
- Modify: `apps/backend/core/services/wake_policy.py` (`is_parked_trial`, `:43-74`)
- Modify: `apps/backend/core/services/readiness_state.py` (`resolve_runtime_phase`, `:76-104`, and
  the `parked` lifecycle branch)
- Modify: `apps/backend/tests/unit/services/test_wake_policy.py` (append)
- Create: `apps/backend/tests/unit/services/test_readiness_state_agentcore_parked.py`

**Interfaces:**
- Consumes: the container row's `runtime_host`, `last_interactive_at`
  (stamped by `container_repo.touch_last_interactive`, already called on every send/steer),
  `agentcore_runtime.IDLE_TIMEOUT_S`
- Produces: `is_parked_trial` returning True for an AgentCore trial row whose
  `last_interactive_at` is older than the runtime's idle timeout; `resolve_runtime_phase`
  reporting `ready` for a live AgentCore row without a health RPC into a parked microVM

Report 04 risk 2 / spec §4.9: `stop_user_service` no-ops for AgentCore (`ecs_manager.py:1449-1451`)
and never stamps `stop_reason`, so the row stays `status="running"` forever and `is_parked_trial`
can never fire — the whole `TRIAL_WAKE_DAILY_BUDGET` UX is silently dead for Hermes trial owners.
AWS parks the microVM invisibly, so the only honest signal the backend has is when the workspace was
last used. The gates themselves (trial cap, expiry, wake budget) already run at dispatch and are
unchanged; this only decides what the card says.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_wake_policy.py  (append)
from datetime import datetime, timedelta, timezone

import pytest

from core.services import wake_policy


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.mark.asyncio
async def test_an_idle_agentcore_trial_row_reads_as_parked(monkeypatch, trial_account):
    row = {"runtime_host": "agentcore", "status": "running", "last_interactive_at": _iso(60 * 5)}
    assert await wake_policy.is_parked_trial("org_a", row, account=trial_account) is True


@pytest.mark.asyncio
async def test_a_recently_used_agentcore_trial_row_is_not_parked(monkeypatch, trial_account):
    row = {"runtime_host": "agentcore", "status": "running", "last_interactive_at": _iso(5)}
    assert await wake_policy.is_parked_trial("org_a", row, account=trial_account) is False


@pytest.mark.asyncio
async def test_an_agentcore_row_with_no_last_interactive_at_is_not_parked(monkeypatch, trial_account):
    row = {"runtime_host": "agentcore", "status": "running"}
    # Fails OPEN, like every other uncertainty in this predicate: a fresh row must not show
    # "Agents asleep" before the owner has ever sent anything.
    assert await wake_policy.is_parked_trial("org_a", row, account=trial_account) is False


@pytest.mark.asyncio
async def test_a_paid_agentcore_row_is_never_parked(monkeypatch, paid_account):
    row = {"runtime_host": "agentcore", "status": "running", "last_interactive_at": _iso(60 * 9)}
    assert await wake_policy.is_parked_trial("org_a", row, account=paid_account) is False


@pytest.mark.asyncio
async def test_the_ecs_predicate_is_untouched(monkeypatch, trial_account):
    row = {"runtime_host": "ecs", "status": "stopped", "stop_reason": "trial_idle"}
    assert await wake_policy.is_parked_trial("org_a", row, account=trial_account) is True
    row = {"runtime_host": "ecs", "status": "running", "last_interactive_at": _iso(60 * 9)}
    assert await wake_policy.is_parked_trial("org_a", row, account=trial_account) is False
```
```python
# apps/backend/tests/unit/services/test_readiness_state_agentcore_parked.py
from core.services.readiness_state import resolve_runtime_phase


def test_a_live_agentcore_row_is_ready_without_a_health_rpc():
    row = {"runtime_host": "agentcore", "status": "running"}
    # The microVM may be parked; AgentCore wakes it on invoke, so a failed probe is not "starting".
    assert resolve_runtime_phase(row, gateway_connected=False) == "ready"


def test_an_ecs_row_still_needs_the_gateway_probe():
    row = {"runtime_host": "ecs", "status": "running"}
    assert resolve_runtime_phase(row, gateway_connected=False) == "starting"
    assert resolve_runtime_phase(row, gateway_connected=True) == "ready"


def test_a_provisioning_agentcore_row_is_still_provisioning():
    assert resolve_runtime_phase({"runtime_host": "agentcore", "status": "provisioning"}, False) == "provisioning"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_wake_policy.py tests/unit/services/test_readiness_state_agentcore_parked.py -q -k "agentcore or ecs_predicate"`
Expected: FAIL with `assert False is True` (the AgentCore row has no `stop_reason`, so the current
predicate returns False)

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/wake_policy.py  (inside is_parked_trial, before the status check)
    if container and container.get("runtime_host") == "agentcore":
        # AgentCore parks an idle microVM invisibly and stop_user_service is a no-op for this row,
        # so there is no stopped/stop_reason to read (report 04 risk 2). The honest signal is when
        # the workspace was last used, against the runtime's own idle timeout.
        if not await _is_trial(owner_id, account):
            return False
        last = container.get("last_interactive_at")
        if not last:
            return False  # fail OPEN, like every other uncertainty here
        from datetime import datetime, timezone

        from core.services.agentcore_runtime import IDLE_TIMEOUT_S

        try:
            seen = datetime.fromisoformat(str(last))
        except ValueError:
            return False
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - seen).total_seconds() > IDLE_TIMEOUT_S
```
```python
# apps/backend/core/services/wake_policy.py  (extract the existing trial read, used by both legs)
async def _is_trial(owner_id: str, account) -> bool:
    from core.repositories import billing_repo

    try:
        acct = account if account is not None else await billing_repo.get_by_owner_id(owner_id)
    except Exception:
        logger.exception("wake_policy billing read failed owner=%s — treating as not parked", owner_id)
        return False
    return bool(acct) and acct.get("subscription_status") == "trial_local"
```
```python
# apps/backend/core/services/readiness_state.py  (in resolve_runtime_phase)
    status = (container or {}).get("status")
    if status != "running":
        return "provisioning"
    if (container or {}).get("runtime_host") == "agentcore":
        # A parked microVM answers no health RPC and needs none: AgentCore wakes it on invoke, so a
        # live row IS ready. Probing here would show "starting" forever for an idle workspace.
        return "ready"
    if gateway_connected:
        return "ready"
    return "starting"
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_wake_policy.py tests/unit/services/test_readiness_state_agentcore_parked.py tests/unit/services/test_readiness_state_architecture.py -q`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/wake_policy.py apps/backend/core/services/readiness_state.py apps/backend/tests/unit/services/test_wake_policy.py apps/backend/tests/unit/services/test_readiness_state_agentcore_parked.py
git commit -m "fix(trial): park/wake judged from last_interactive_at on AgentCore rows

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 23: `AgentCreateForm` stops hard-coding an EFS path

**Files:**
- Modify: `apps/frontend/src/components/control/panels/AgentCreateForm.tsx:44-58`
- Modify: `apps/frontend/src/components/control/panels/__tests__/AgentCreateForm.test.tsx`
- Modify: `apps/mobile/src/lib/agentCatalog.ts:168-173` (the same hard-code, plural/singular drift)
- Modify: `apps/backend/core/services/runtime_rpc.py` (`_sanitize_agent_create`)

**Interfaces:**
- Consumes: `agents.create` RPC params
- Produces: the client sends **no** `workspace`; the backend fills it per runtime — `.openclaw/
  workspace-{id}` for an ECS row, nothing at all for an AgentCore row (a Hermes profile dir IS the
  state root and is created by `upload_profile`)

Spec §9 / report 04 §18: the form bakes `workspace: ".openclaw/workspace-" + normalizedId` into the
payload, which is meaningless with no EFS. The lazy fix is to delete the client's knowledge of it
rather than teach two clients a second path convention — the backend already knows which runtime
the owner is on.

- [ ] **Step 1: Write the failing test**
```tsx
// apps/frontend/src/components/control/panels/__tests__/AgentCreateForm.test.tsx  (append)
it("does not send a workspace path — the backend owns the layout", async () => {
  const callRpc = vi.fn().mockResolvedValue({});
  vi.mocked(useRuntimeRpcMutation).mockReturnValue(callRpc);
  render(<AgentCreateForm existingNames={[]} onCreated={() => {}} onCancel={() => {}} />);
  await userEvent.type(screen.getByLabelText(/name/i), "Ada");
  await userEvent.click(screen.getByRole("button", { name: /create/i }));
  expect(callRpc).toHaveBeenCalledWith("agents.create", expect.not.objectContaining({ workspace: expect.anything() }));
  expect(JSON.stringify(callRpc.mock.calls)).not.toContain(".openclaw");
});
```
```python
# apps/backend/tests/unit/services/test_runtime_rpc_agent_create_workspace.py
from core.services.runtime_rpc import _sanitize_agent_create


def test_an_ecs_owner_gets_the_efs_workspace_filled_in():
    out = _sanitize_agent_create({"name": "Ada"}, runtime_host="ecs", agent_id="ada")
    assert out["workspace"] == ".openclaw/workspace-ada"


def test_an_agentcore_owner_gets_no_workspace_key_at_all():
    out = _sanitize_agent_create({"name": "Ada"}, runtime_host="agentcore", agent_id="ada")
    assert "workspace" not in out


def test_a_client_supplied_workspace_is_ignored_not_trusted():
    out = _sanitize_agent_create({"name": "Ada", "workspace": "/etc"}, runtime_host="agentcore", agent_id="ada")
    assert "workspace" not in out
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/frontend && pnpm exec vitest run src/components/control/panels/__tests__/AgentCreateForm.test.tsx`
Expected: FAIL with `expected "agents.create" to not contain objectContaining {"workspace": Anything}`

- [ ] **Step 3: Write minimal implementation**
```tsx
// apps/frontend/src/components/control/panels/AgentCreateForm.tsx  (replace lines 44-58)
      await callRpc(
        "agents.create",
        {
          name: name.trim(),
          // No `workspace`: the backend fills the layout for the owner's runtime (an EFS path on
          // ECS, nothing on Hermes, where a profile dir IS the state root). A client that names a
          // path bakes one runtime's filesystem into every client release.
          reasoningDefault: "stream",
          params: { paperclip: paperclipBlock },
        },
      );
```
```python
# apps/backend/core/services/runtime_rpc.py  (_sanitize_agent_create)
def _sanitize_agent_create(params: dict, *, runtime_host: str = "ecs", agent_id: str | None = None) -> dict:
    out = {k: v for k, v in params.items() if k != "workspace"}  # never trust a client-named path
    if runtime_host != "agentcore":
        # OpenClaw's agents.create requires a non-empty workspace — it does not inherit
        # agents.defaults.workspace at create time (only at runtime via resolveAgentWorkspaceDir).
        out["workspace"] = f".openclaw/workspace-{agent_id or normalize_agent_id(params.get('name', ''))}"
    return out
```
Mirror the deletion in `apps/mobile/src/lib/agentCatalog.ts:168-173`.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/frontend && pnpm exec vitest run src/components/control/panels/__tests__/AgentCreateForm.test.tsx && cd ../backend && uv run pytest tests/unit/services/test_runtime_rpc_agent_create_workspace.py -q`
Expected: PASS (both)

- [ ] **Step 5: Commit**
```bash
git add apps/frontend/src/components/control/panels/AgentCreateForm.tsx apps/frontend/src/components/control/panels/__tests__/AgentCreateForm.test.tsx apps/mobile/src/lib/agentCatalog.ts apps/backend/core/services/runtime_rpc.py apps/backend/tests/unit/services/test_runtime_rpc_agent_create_workspace.py
git commit -m "fix(agents): the backend owns the workspace layout, not the create form

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 24: Bundled skills into the Hermes image + the tool-name swap

**Files:**
- Create: `apps/infra/hermes/skills/` (copy of `apps/infra/openclaw/skills/{lightbulb,paperclip}`;
  `shopify` too **if** PR #2710's skill has landed — it is not in this branch today)
- Modify: the copied `SKILL.md` / `references/*.md` (tool names only)
- Create: `apps/backend/tests/unit/services/test_hermes_skill_tool_names.py`

**Interfaces:**
- Consumes: nothing at runtime — `config.yaml`'s `skills.external_dirs: ["/opt/hermes-skills"]`
  (Task 7) is what the agent reads
- Produces: skill text naming Hermes tools

Spec §3: "Keep; one pass swaps tool names; binaries bake into the Hermes image." The swap list,
from grepping the skill files against Hermes's `CONFIGURABLE_TOOLSETS`
(`/opt/hermes/hermes_cli/tools_config.py:52-83`):

| in the skills today | Hermes / our plugin | note |
|---|---|---|
| `exec`, "run a command" | `terminal` (+ `process` for background) | the `terminal` toolset's two tools |
| `read` / write / edit a file | `read`, `write`, `patch`, `search` | the `file` toolset |
| `web` search | `web_search`, `web_extract` | the `web` toolset |
| `memory_search` | `memory_search` | unchanged — the `memory` toolset keeps the name |
| `todo` (the tool) | `todo_list` | Paperclip's `--status todo` is a STATUS, never renamed |
| `mcp` | `call_mcp` / the server's own tool names | Hermes exposes MCP tools directly |
| `nodes` | `nodes` | our plugin tool (M3 Task 3) |
| the `message` tool | `send_room_message` | our plugin tool (spec §4.3) |
| `browser` / "container Chrome" / "Browser panel" | `browser` toolset (navigate/click/type/scroll) | copy also changes in M5 when the panel does |
| `sessions_spawn({runtime:"acp"})` | **removed** | no Hermes equivalent (report 01 §5.3); the ACP harness is dropped |
| `skill_workshop` | `skills` toolset (list/view/manage) | |
| `secrets` tool | `secrets` | Hermes secret sources (`register_source`) keep the name |
| `/app/skills/paperclip/scripts/…` | `/opt/hermes-skills/paperclip/scripts/…` | the baked path moves |
| `lightbulb_local_sessions` | unchanged | our plugin tool, same name |
| `$ISOL8_API_URL` | unchanged | the bridge sets the same variable |

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_hermes_skill_tool_names.py
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SKILLS = REPO / "apps/infra/hermes/skills"

#: Names that must not survive the swap, with what replaced them.
RETIRED = {
    "`exec`": "`terminal`",
    "`todo`": "`todo_list`",
    "sessions_spawn": "(dropped — no ACP harness on Hermes)",
    "/app/skills/": "/opt/hermes-skills/",
    "openclaw backup create": "(dropped)",
}


def _texts():
    return {path: path.read_text() for path in SKILLS.rglob("*.md")}


def test_the_skills_were_copied_into_the_hermes_image_tree():
    assert (SKILLS / "lightbulb/SKILL.md").exists()
    assert (SKILLS / "paperclip/SKILL.md").exists()


def test_no_retired_tool_name_survives():
    offenders = {
        f"{path.relative_to(SKILLS)}: {needle} -> {replacement}"
        for path, text in _texts().items()
        for needle, replacement in RETIRED.items()
        if needle in text
    }
    assert not offenders, offenders


def test_our_own_plugin_tool_names_are_still_referenced():
    joined = "\n".join(_texts().values())
    assert "lightbulb_local_sessions" in joined
    assert "send_room_message" in joined


def test_the_paperclip_helper_path_points_at_the_baked_skills_dir():
    text = (SKILLS / "paperclip/SKILL.md").read_text()
    assert "/opt/hermes-skills/paperclip/scripts/paperclip-api.sh" in text
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_skill_tool_names.py -q`
Expected: FAIL with `AssertionError: assert False` on `test_the_skills_were_copied_into_the_hermes_image_tree`

- [ ] **Step 3: Write minimal implementation**
```bash
cd /path/to/worktree
cp -R apps/infra/openclaw/skills/lightbulb apps/infra/openclaw/skills/paperclip apps/infra/hermes/skills/
# the mechanical half of the swap; the prose half is reviewed by hand in the same commit
cd apps/infra/hermes/skills
grep -rl '/app/skills/' . | xargs sed -i '' 's#/app/skills/#/opt/hermes-skills/#g'
grep -rl '`todo`' . | xargs sed -i '' 's/`todo`/`todo_list`/g'
grep -rl '`exec`' . | xargs sed -i '' 's/`exec`/`terminal`/g'
```
Then by hand, in the same commit: delete the `sessions_spawn({runtime:"acp"})` / Claude-Code
delegation passages (no Hermes equivalent), the "upload your `openclaw backup create` archive"
line, and replace the `message` tool with `send_room_message` wherever the skills tell an agent to
post to a room. Each `MAINTENANCE.md` gains one line naming the Hermes tool set as the anchor.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_skill_tool_names.py -q && cd ../infra && npx vitest run test/hermes-image-workflow.test.ts`
Expected: PASS (4 + 5 passed — the Dockerfile test already asserts `COPY skills/ /opt/hermes-skills/`)

- [ ] **Step 5: Commit**
```bash
git add apps/infra/hermes/skills apps/backend/tests/unit/services/test_hermes_skill_tool_names.py
git commit -m "feat(hermes): bake the lightbulb + paperclip skills, swapped to Hermes tool names

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---


## Open items (each resolved by the step named)

Four items, each with the step that resolves it rather than a guess shipped as fact:

1. **`HermesTokenStorage`'s on-disk field names** (Task 14) — `$HERMES_HOME/mcp-tokens/<server>.json`
   + `.client.json` + `.meta.json` (`/opt/hermes/tools/mcp_oauth.py:228-232,403-421`) were not read
   field by field. Task 14 step 3 opens with the `docker run … sed` that reads them; the mapping is
   a shape to fill, and the test asserts only that an OAuth server gets a file and a key-based one
   does not.
2. **Cross-profile ChatGPT OAuth** (Task 15) — the source implements a root-fallback carve-out for
   `openai-codex` (`auth_codex.py:145-163`, `auth.py:737-754`) and the published Profiles docs say
   flatly that a profile never inherits root's logins. Task 15 step 4 runs the two-profile check;
   if the docs win, the token stages per profile with one designated refresher, which is a change
   to that task only.
3. **Hermes's `user_override` / `custom_contract` pricing key** (report 06 §1.5) — an AIP ARN is
   opaque to Hermes's pricing catalogue, so its own cost reporting reads `unknown`. Not on any task
   here, deliberately: the bedrock-gate and the meter are the billing truth and neither reads
   Hermes's number. Worth a line in the ledger, not a task.
4. **The `shopify` skill** (Task 24) — `apps/infra/openclaw/skills/` holds only `lightbulb` and
   `paperclip` on this branch; the Shopify skill rides PR #2710. Copy it in the same pass once that
   lands, or M5 picks it up with the rest of the Shopify door.

Two corrections to the brief, both verified in source and folded into the tasks above:
`device.mac` / `device.json` / `device.cli` do not exist (M3 preamble), and the spike bridge's
`S3_PREFIX=hermes/{OWNER}` is the wrong way round for the per-owner IAM grant (Tasks 12 and 17).
