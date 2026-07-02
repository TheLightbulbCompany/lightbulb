# Map 06 — Hermes MCP + Config System (pressure-test)

Basis: `~/Desktop/hermes-agent` @ `basis-v0.14.0-spec` (commit 4c46c35e). All
file:line refs are against this tree.

---

## 1. Config is a SINGLE YAML dict — there is NO schema, no validator gate

- `~/.hermes/config.yaml` (`get_config_path`, `config.py:327-329`) is the entire
  config surface. The "schema" is the in-code `DEFAULT_CONFIG` Python dict
  (`config.py:470-1693`), versioned only by `"_config_version": 23`
  (`config.py:1692`). There is **no JSON Schema / pydantic / zod / typebox**
  anywhere — `validate_config_structure` (`config.py:3235`) is a soft linter
  that returns a list of `ConfigIssue` warnings; it does NOT reject load.
- `load_config` (`config.py:4275`) does `yaml.safe_load` → `_deep_merge` over
  DEFAULT_CONFIG → normalize → `_expand_env_vars`. **A YAML parse error
  silently falls back to DEFAULT_CONFIG and drops every user override**
  (`config.py:4314-4316`, `_warn_config_parse_failure` 37-71). Contrast with
  Isol8's OpenClaw experience where an invalid value crash-loops the container
  (memory `reference_openclaw_config_brick`): Hermes does the OPPOSITE — it
  swallows bad config and runs defaults, which for an Isol8 "config writer"
  means a malformed write produces a silently-wrong agent, not a loud failure.
  An Isol8 Hermes-config writer would have to mirror this ~1200-line dict by
  hand (or vendor it) and would get **zero validation feedback** from Hermes.

## 2. MCP is process-GLOBAL, NOT per-agent scoped — same posture as OpenClaw

- MCP servers live under one top-level `mcp_servers:` key in config.yaml
  (`mcp_config.py:8`, `mcp_tool.py:2237-2263 _load_mcp_config`). They are
  registered into a **module-level singleton** `_servers: Dict[str,
  MCPServerTask] = {}` (`mcp_tool.py:1709`) guarded by a module `_lock`.
  `register_mcp_servers` / `discover_mcp_tools` (`mcp_tool.py:3189`, `3284`)
  connect every enabled server once per process and register their tools into
  the global tool registry. There is **no per-agent / per-profile / per-session
  MCP scoping** in the config shape — the only per-server granularity is
  `tools.include`/`tools.exclude` (`mcp_config.py:401, 497-508`) and
  `enabled: false` (`mcp_tool.py:3215`). This directly mirrors Isol8's existing
  OpenClaw finding (memory `project_openclaw_mcp_global_only_nested_inert`):
  **switching runtimes does not buy per-agent MCP** — Hermes has the same
  global-only model. Any spec claim that Hermes gives finer MCP scoping is
  unsupported by the code.

## 3. How an MCP server is declared/launched (stdio vs HTTP, env injection)

- Two transports, declared per-server in config: **stdio** (`command` + `args`
  + `env`) or **HTTP** (`url` + `headers`) — `mcp_config.py:275-282`,
  list rendering at `mcp_config.py:478-494`. `--env KEY=VALUE` is **stdio-only**
  and rejected for `url` servers (`mcp_config.py:254-256`).
- Secrets are NOT stored inline: `hermes mcp add --url` writes the key to
  `~/.hermes/.env` as `MCP_<NAME>_API_KEY` and stores only a `${VAR}` template
  in `headers.Authorization` (`mcp_config.py:107-109, 322-334`). At launch,
  `_interpolate_env_vars` (`mcp_tool.py:2225-2234`) and config-load
  `_expand_env_vars` (`config.py:4045-4062`) resolve `${VAR}` from
  `os.environ`, which is hydrated from `~/.hermes/.env` via
  `load_hermes_dotenv` (`mcp_tool.py:2255-2258`, `env_loader.py:142`). **An
  Isol8 Hermes-config writer must therefore write TWO files** (config.yaml +
  .env) and rely on env interpolation — there is no in-config encrypted-secret
  slot. This is a different secret rail than OpenClaw's `mcp.servers[].args[]`
  (which had the cred-leak defect); Hermes's `${VAR}` indirection is cleaner
  but still depends on the daemon's process env being seeded correctly.
- OAuth (`auth: oauth`) is HTTP-only and handled by `mcp_oauth_manager`
  (`mcp_config.py:287-310`) — a browser-redirect PKCE flow that is **infeasible
  in a headless ECS container** without a node-host relay equivalent, a real
  porting cost the spec must account for.

## 4. Daemon-GLOBAL vs per-call knobs

- **Daemon-global** (read once per gateway process, changing requires restart
  or `/reload-mcp`): everything in `mcp_servers` (the global `_servers`
  registry, `mcp_tool.py:1709`); all `providers` / `fallback_providers` /
  `model`; `terminal.*` backend + Docker image (`config.py:558-637`);
  `lsp.*`, `curator.*`, `sessions.auto_prune`, `logging.*`, `kanban.*`,
  `cron.*`. `/reload-mcp` exists but **invalidates the provider prompt cache**
  (tool schemas are baked into the system prompt) so the next message re-sends
  full input tokens — gated behind `approvals.mcp_reload_confirm`
  (`config.py:1390-1397`). i.e. MCP changes are expensive and effectively
  restart-class, exactly like OpenClaw.
- **Per-call / per-turn**: `agent.gateway_timeout`, `agent.max_turns`,
  `agent.tool_use_enforcement`, `agent.image_input_mode`, `compression.*`,
  `approvals.mode`, `display.*`, `tool_output.*`, `auxiliary.<task>.*` (each
  side-task — vision/compression/approval/mcp/title/curator — has its own
  provider+model+timeout, `config.py:848-966`). These are re-read from
  `load_config()` (which is mtime-cached, `config.py:4296-4298`) on the path
  that uses them, so they take effect without restart.
- **Env-only knobs that have NO config.yaml equivalent**: a large set is
  managed directly in `~/.hermes/.env` (`_EXTRA_ENV_KEYS`, `config.py:98-146`)
  — all channel home-channels, `SUDO_PASSWORD`, `API_SERVER_*`,
  `GATEWAY_PROXY_*`, Langfuse keys. An Isol8 config writer that only emits
  config.yaml would silently miss every one of these.

## 5. The FULL config surface a Hermes config writer must produce

Top-level keys in `DEFAULT_CONFIG` (`config.py:470-1693`) — this is the entire
contract, ~50 sections: `model`, `providers`, `fallback_providers`,
`credential_pool_strategies`, `toolsets`, `agent` (20+ sub-keys), `terminal`
(30+ sub-keys incl. Docker), `web`, `browser` (+ `camofox`), `checkpoints`,
`file_read_max_chars`, `tool_output`, `tool_loop_guardrails`, `compression`,
`prompt_caching`, `openrouter`, `bedrock` (incl. discovery + guardrail),
`auxiliary` (13 named side-tasks), `display` (40+ keys), `dashboard`,
`privacy`, `tts`/`stt`/`voice`, `human_delay`, `context`, `memory`,
`delegation`, `goals`, `skills`, `curator`, `honcho`, `timezone`, 9 chat
**platform** blocks (`slack`/`discord`/`whatsapp`/`telegram`/`mattermost`/
`matrix`), `approvals`, `command_allowlist`, `quick_commands`, `hooks`,
`personalities`, `security` (tirith), `cron`, `kanban`, `code_execution`,
`logging`, `model_catalog`, `network`, `sessions`, `onboarding`, `updates`,
`lsp`, `x_search`, plus `mcp_servers` (added by `hermes mcp`, not in defaults).
**Plus** the parallel `.env` surface (`OPTIONAL_ENV_VARS`, `config.py:1717+`,
~150 keys across provider/messaging/tool/setting categories). The .env.example
(469 lines) categories are: **LLM providers** (~18: OpenRouter, Novita, Google/
Gemini, Ollama, z.ai/GLM, Kimi/Moonshot, Arcee, MiniMax, OpenCode Zen/Go, HF,
Qwen-OAuth, Xiaomi, NVIDIA, DeepSeek, DashScope, xAI, LM Studio), **tool API
keys** (Exa, Parallel, Firecrawl, Tavily, Browserbase, FAL, ElevenLabs, …),
**messaging/channel creds** (Discord/Slack/Telegram/Matrix/Mattermost/
BlueBubbles/QQ/IRC/WeChat-family + per-channel HOME_CHANNEL + allowlists),
**API/gateway server** (`API_SERVER_*`, `GATEWAY_PROXY_*`, `WEBHOOK_*`), and
**agent settings** (`SUDO_PASSWORD`, `HERMES_MAX_ITERATIONS`, prefill/ephemeral
prompt). This is materially LARGER than OpenClaw's `openclaw.json` surface —
the spec's effort estimate must reflect mirroring ~50 config sections + ~150
env keys + a two-file (yaml+env) writer, not a single JSON emitter.

## 6. HERMES_HOME directory layout (= the unit of isolation, ≈ OpenClaw EFS workspace)

- Resolution: `get_hermes_home()` (`hermes_constants.py:14-68`) reads
  `$HERMES_HOME`, else `~/.hermes`. **Profiles are just alternate HERMES_HOMEs**
  at `<root>/profiles/<name>` (`hermes_constants.py:80-104`) — there is no
  in-process multi-tenant model; multi-agent = multiple HERMES_HOME dirs, each
  with its own config.yaml + .env + state.db. Subprocess spawners MUST pass
  `HERMES_HOME` explicitly or data lands in the wrong profile (loud warning,
  issue #18594, `hermes_constants.py:54-61`).
- Seeded subdirs (`ensure_hermes_home`, `config.py:434-441`): `cron/`,
  `sessions/`, `logs/` (+`logs/curator/`), `memories/`, `pairing/`, `hooks/`,
  `image_cache/`, `audio_cache/`, `skills/`, plus `config.yaml`, `.env`,
  `SOUL.md` (seeded from `DEFAULT_SOUL_MD`, `config.py:408-414`), and at runtime
  `state.db` (sessions/messages/FTS5, `config.py:1575-1601`). Permissions are
  0700 dir / 0600 file but **auto-relaxed inside containers** (`_is_container`
  detects `/.dockerenv`, cgroup, or `HERMES_CONTAINER`/`HERMES_SKIP_CHMOD`,
  `config.py:365-405`) — relevant because Isol8 runs on ECS Fargate. Note
  `state.db` is SQLite with FTS5; an EFS-mounted HERMES_HOME would hit the SAME
  WAL/NFS hazard Isol8 already documented for OpenClaw
  (memory `project_efs_sqlite_wal_two_task_corruption`) — the spec must not
  assume Hermes sidesteps the EFS-SQLite problem; it inherits it.
