# Agent Beacon (Asymptote Labs): reference

Researched 2026-10-21 against `github.com/Asymptote-Labs/agent-beacon` at `main` (latest tag
`v1.3.45`, 2026-10-20), its in-repo docs (the source of docs.beacon.sh / docs.asymptotelabs.ai),
and the live pages beacon.sh, beacon.sh/pricing and asymptotelabs.ai. Paths are repo-relative
unless they are URLs. Read-only: nothing was installed, signed up for or run.

Markers used below:
- **[code]** checked in source.
- **[docs]** taken from the repo's `docs/` (published docs) without code confirmation.
- **[site]** taken from a live web page.
- **[unverified]** could not be confirmed.

---

## 0. One-paragraph summary

Beacon is an MIT-licensed, Go-first, local-first telemetry agent for AI coding agents
("harnesses"). It installs hooks, plugins or OTLP settings into about 30 runtimes, normalizes
everything into one JSONL event schema (`~/.beacon/endpoint/logs/runtime.jsonl`), and builds
three things on that log: (1) offline detection with an open YAML+CEL rule format (77 rules in
the corpus), (2) a review-gated "memory" loop that turns traces into approved lessons served
over MCP or as Agent Skills, and (3) forwarding to SIEMs, buckets and the hosted **Beacon Cloud**
(beacon.sh). The positioning has moved. The repository and docs read as security and endpoint
telemetry ("Sigma for agent telemetry", MDM, SIEM packs). The current README and beacon.sh read as
"Self-improving memory for coding agents". Blocking is not in the open build: it ships an inert,
fail-open policy seam, and real enforcement is a separately distributed, signed "enterprise
edition". Paid tiers are Beacon Cloud Pro ($20/mo, $15/mo annually) and Enterprise (custom).

---

## 1. Architecture

### 1.1 Components and languages

| Component | Path | Language | Role |
|---|---|---|---|
| `beacon` CLI and endpoint runtime | `cli/beacon` | Go (module go 1.27; cobra, bubbletea/lipgloss TUI, `modernc.org/sqlite`, yaml.v3, go-toml) | Install/repair/uninstall, harness discovery, session-store sync ("poll"), dashboard, scan, rules, memory, MCP server, share, handoff, git links, pricing/token reports, Cloud connect |
| `beacon-hooks` | `cli/beacon-hooks` | Go | Short-lived process the harness runs per hook event. Maps payloads into events, appends to JSONL, runs the policy seam. Embedded in the `beacon` binary and installed under the endpoint base dir |
| `beacon-otelcol` | `collector-builder` | Go (OpenTelemetry Collector distribution) | Loopback OTLP receivers plus the custom `beaconjson` exporter (`collector-builder/exporter/beaconjsonexporter`) that writes Beacon JSONL |
| `pkg/asymptoteobserve` | `pkg/asymptoteobserve` | Go library | Event schema, harness-name normalization, provenance markers, redaction (`privacy.go`), token usage, threat-rules engine (`threatrules`), rule store, policy contract, share/trace projections, lens contract. Imported by CLI, hooks and exporter |
| Runtime plugins | `plugins/{opencode,cline,pi,omp,openclaw,prime,omo}-beacon`, `plugins/shared` | TypeScript (bun) | One-file plugins/extensions for runtimes without hook files. Mirrored into `cli/beacon/internal/endpoint/hooks/assets/` |
| Browser extension | `browser-extension` | TypeScript, MV3 | Captures claude.ai and chatgpt.com chat streams. Posts OTLP to `127.0.0.1:4318` |
| Observe SDK | `packages/asymptote-sdk-js` (`@asymptote/sdk`) | TypeScript | OpenLLMetry-based spans for apps. Default hosted endpoint `https://api.asymptotelabs.ai/v1/observe` (`src/index.ts:51-52`) **[code]** |
| Agent Skills | `agent-skills/skills/*` | Markdown (Agent Skills spec) | `beacon-memory-recall`, `-distill`, `-promote`, `beacon-lens-create`, `beacon-trace-share` |
| Rule corpus and spec | `rules/`, `spec/threat-rules` | YAML, JSON Schema | 77 rules in 10 categories. Spec `threat-rules/v1.1` |
| Lens spec | `spec/lenses` | HTML/JS contract | Single-file HTML trace views run in a sandboxed iframe |
| Sandbox harness | `beacon-sandbox` | Go | Runs a real Claude Code session in a disposable sandbox (Modal referenced) and checks what was captured |
| Packaging | `packaging/{macos,linux,windows,vector,release}` | sh/PowerShell/WiX | Signed and notarized `.pkg` (arm64 only), `.deb`/`.rpm`, unsigned `.msi`, Jamf/Fleet assets, pinned Vector 0.56.0 |
| Terraform | `terraform/{aws-s3,gcs,aws-s3-asymptote-reader,gcs-asymptote-reader}` | HCL | Customer buckets plus write-only writer identities. The `*-asymptote-reader` modules let Asymptote's ClickHouse (ClickPipes, GCP-hosted) read a customer bucket through SQS/PubSub |

Size signals **[code]**: about 143k lines of non-test Go, about 135k lines of Go tests (562
`_test.go` files), about 16.6k lines of TS/MJS outside `node_modules`.

### 1.2 Processes on an endpoint

| Process | Lifetime | Service unit |
|---|---|---|
| `beacon-otelcol` collector | Resident | launchd `com.beacon.endpoint.collector[.user].plist`; systemd `beacon-collector.service`; Windows `BeaconCollector`; pidfile "supervised" fallback in containers or Windows user mode |
| `beacon-hooks` | One process per hook event (synchronous with the agent's tool call) | n/a |
| Vector forwarder (Beacon Cloud or customer S3/GCS/Falcon) | Resident, only after `connect` or a forwarder helper | `com.beacon.endpoint.asymptote-forwarder`, `beacon-asymptote-forwarder.service` |
| Inventory job | One-shot, every 6 hours and at load | `com.beacon.endpoint.inventory`, `beacon-inventory.timer` |
| Updater | One-shot schedule, opt-in, macOS system package only | `com.beacon.endpoint.updater` |
| Dashboard | On demand, `beacon endpoint dashboard`, loopback | n/a |
| MCP server | On demand, `beacon mcp serve` (stdio or `127.0.0.1:8766`) | n/a |

Ports **[docs]** (`docs/cli/endpoint-paths.mdx`): OTLP gRPC `127.0.0.1:4317`, OTLP HTTP
`127.0.0.1:4318`, collector health `127.0.0.1:13133`.

### 1.3 Data flow

```
 Harness (Claude Code, Codex, Cursor, Hermes, ...)
   |                     |                         |                        |
   | native hook cmd     | OTLP logs/traces/metrics| plugin/extension (TS)  | session store on disk
   v                     v                         v                        v
 beacon-hooks  <---------+------------------  beacon-hooks          beacon endpoint <rt> sync
 (per-event proc;        |                    (spawned by plugin)   (poll; cursor in state/;
  policy seam;           v                                           30-day backfill on install)
  redact+truncate)   beacon-otelcol
   |                 (127.0.0.1:4317/4318 -> batch -> beaconjson exporter)
   |                     |                                                  |
   +---------------------+---------------> runtime.jsonl <------------------+
                                           (append; rotate 10 MiB x 5;
                                            event.id = deterministic UUID)
                                                   |
     +------------------+---------------+----------+-----------+------------------+-------------+
     v                  v               v                      v                  v             v
 local dashboard    beacon scan     history.db (opt-in   memory.db (learning   Vector tail     file shippers
 (loopback, RO;     (YAML+CEL ->    SQLite index;        loop: evaluations,    -> Beacon Cloud (Filebeat, Datadog
  lenses)           findings)       90d / 1 GiB)         candidates, memories) ingest or S3/  Agent, Wazuh, AMA..)
                                                                |              GCS/Falcon
                                                    beacon mcp serve (stdio)
                                                    memory skills -> .agents/skills/
 Direct collector exporters (optional): Splunk HEC, Falcon LogScale HEC.
 Cloud sandboxes: beacon-hooks -> /tmp/beacon/runtime.jsonl -> per-run gzip snapshot -> customer GCS/S3.
 CI: beacon ci exec|start|finish -> temp collector -> $RUNNER_TEMP/beacon/runtime.jsonl -> artifact.
```

Event identity and ordering **[docs + code]**: `event.id` is a deterministic UUID derived from
the event (`EventIDForLine`), so hook and OTLP captures of the same action collide on purpose
and Cloud deduplicates on it. `timestamp` has nanosecond precision and fixed width. The optional
`sequence` counter is per writer. Readers must sort by `(timestamp, sequence)`; append order is
not emission order. `prompt.id` ties every event of one user turn together (normalized from
Claude `promptId`, Gemini `prompt_id`, Codex `turn_id`, and others).

---

## 2. Supported harnesses

Source: README coverage table, `docs/runtimes/*.mdx`, `docs/cli/endpoint-paths.mdx`,
`docs/runtimes/integration-model.mdx`, the project `CLAUDE.md` "Telemetry Scope", and
`cli/beacon-hooks/cmd/policy.go` for blocking. The "Can block" column means the hook path has a
native deny shape for the policy seam (section 4.3), not that the open build blocks anything.

| Runtime (harness name) | Mechanism | Files written or read | Notable captured / not captured | Can block (seam) |
|---|---|---|---|---|
| Claude Code (`claude_code`) | OTLP + hooks + poll | `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR`), project `./.claude/settings.json`; poll `~/.claude/projects` | OTLP lacks session start/end, file reads/diffs and subagents, so hooks fill those. Stop hook records `last_assistant_message` as `agent.message`. SessionStart appends `BEACON_SESSION_ID`/`BEACON_HARNESS` to `$CLAUDE_ENV_FILE` | Yes (PreToolUse and PermissionRequest objects) |
| Claude Cowork | Admin-configured OTLP from Anthropic's service | Org settings, no local file | `cowork.surface=remote` maps to `origin=cloud` | n/a |
| Codex CLI (`codex_cli`) / Codex Desktop (`codex_desktop`) | OTLP + SessionStart/Stop hooks + poll | `~/.codex/config.toml` (or `$CODEX_HOME`), `hooks.json`; poll `~/.codex/sessions` | Keeps only the `session_task.turn` span for tokens and drops hundreds of transport spans. No file events. Never reads `~/.codex/auth.json` | No deny shape |
| Cursor | Hooks + poll | `~/.cursor/hooks.json`, `./.cursor/hooks.json`; poll Composer DB plus `~/.cursor/projects` transcripts | `afterAgentThought` reasoning captured. No tokens | Yes (`{"permission":"deny"}`) |
| Cline (`cline`) | Plugin + poll | `~/.cline/plugins/beacon.ts` (`$CLINE_DIR`); poll `data/tasks`, `tasks`, `data/sessions`, `kanban/workspaces` | One plugin covers VS Code, JetBrains and CLI hosts. No approvals | No |
| OpenCode | Plugin + poll | `~/.config/opencode/plugins/beacon.ts`, `./.opencode/plugins/beacon.ts`; poll `opencode.db` / legacy storage | Live approvals via plugin. Open issue #951: sync fails on OpenCode v2 schema | No |
| Gemini CLI | OTLP | `~/.gemini/settings.json` (`target=local`, gRPC, `outfile` removed) | No session or command events per README table | No |
| GitHub Copilot CLI | OTLP (MDM-set env) + poll | Validates `COPILOT_OTEL_ENABLED`; poll `~/.copilot/session-state` | Cost from `totalNanoAiu` (10^9 nano-AIU = $0.01) after 2026-06-01. Beacon does not write Copilot config | No |
| VS Code (Copilot Chat) | OTLP + preview hooks | VS Code user settings | Partial command/file/MCP coverage | (hooks preview) |
| Factory Droid | OTLP (launch env) + hooks + poll | `~/.factory/settings.json`; poll `~/.factory/sessions` | Beacon does not edit shell profiles for the OTLP env | Not in deny switch |
| Devin CLI | Hooks | `~/.config/devin/config.json`, `./.devin/hooks.v1.json` | | Yes (`{"decision":"reject"}`) |
| Devin Desktop (Windsurf/Cascade) | Hooks | `~/.codeium/windsurf/hooks.json`, `./.windsurf/hooks.json` | No session events | Yes (`reject`) |
| Devin Cloud Agents | API poll of Devin organization API, then GCS | Central command | Prompts and tokens only | n/a |
| Antigravity CLI | Hooks | `~/.gemini/config/hooks.json`, `./.agents/hooks.json` | | Yes (`{"decision":"deny"}`) |
| Grok Build | Hooks + poll | `~/.grok/hooks/beacon-endpoint.json`; poll `~/.grok/sessions/...` | | Yes (`deny`) |
| **Hermes Agent** (`hermes`) | Shell hooks + poll | Merges into `$HERMES_HOME/config.yaml` or the active profile's (default `~/.hermes/config.yaml`), user scope only; poll `state.db` (SQLite) | Prompts via `pre_llm_call` (`extra.user_message`), tools, approvals (allow/deny/timeout), session and subagent stop. Needs `HERMES_ACCEPT_HOOKS=1` / `--accept-hooks` / `hooks_auto_accept: true` for unattended runs. Open issue #960: no profile attribution on events and the cursor is not keyed per profile | No deny shape |
| Kimi Code (`kimi_code`) | Hooks | Appends `[[hooks]]` to `$KIMI_CODE_HOME/config.toml` (default `~/.kimi-code`), which also holds provider API keys; parse-back verification, no re-serialization | 13 of 20 events. No tokens. Writes nothing to stdout (stdout enters model context) | Yes, exit code 2, not in the permission phase |
| Kiro | Hooks | `~/.kiro/hooks/beacon-endpoint.json` (or `$KIRO_HOME`), `<repo>/.kiro/hooks/...` | Hook stdout enters context, so stdout stays silent. No approvals | Yes, exit code 2 plus stderr |
| Muse Code (Meta) | Hooks | `~/.config/muse/beacon-endpoint-hooks.json` plus one `managed_hooks_path` key in `settings.json` | Leaves `PreLLMCall`/`PostLLMCall` unsubscribed | Yes |
| OpenHands | Hooks | Merges into `<repo>/.openhands/hooks.json` (project scope is the practical default) | No approvals, tokens, tool ids or assistant text | Yes (`deny` plus reason) |
| Qwen Code | Hooks | `~/.qwen/settings.json`, `./.qwen/settings.json` | Gemini fork without OTLP | Yes (Claude shape) |
| DeepSeek Harness (`dsh`) | Hook bridge + poll | `$DSH_HOME/beacon-endpoint-hooks.json` plus a row in `$DSH_HOME/cordis.patch.yml`; skill at `$DSH_HOME/skills/beacon-endpoint/SKILL.md`; poll `$DSH_HOME/sessions` | | Yes |
| Pi (`pi_cli`) | Extension + poll | `~/.pi/agent/extensions/beacon.ts` (`$PI_CODING_AGENT_DIR`), `./.pi/extensions/beacon.ts` | No approvals | No |
| Oh My Pi (`omp`) | Extension | `~/.omp/agent/extensions/beacon.ts`, `./.omp/extensions/beacon.ts` | Real approval events | No |
| Prime Agent (`prime_agent`) | Extension + poll | `~/.prime/agent/extensions/beacon.ts`; poll `~/.prime/agent/sessions` | ipython kernel cells become `command.executed` | No |
| Senpi / OMO (`omo_senpi`) | Extension | `~/.omo/agent/extensions/beacon.ts` | | No |
| OpenClaw Gateway (`openclaw_gateway`) | Plugin + OTLP (`diagnostics-otel`) + poll | `~/.openclaw/extensions/beacon-endpoint/` (`$OPENCLAW_STATE_DIR`); poll `~/.openclaw/agents/<profile>/sessions/` | Plugin `before_tool_call` is fail-closed, so it dispatches synchronously. Conversation hooks need `allowConversationAccess`. Beacon reads OpenClaw config but does not rewrite it | No |
| fx (Vercel Labs) | Poll only | `~/.fx/sessions/<s>/events.jsonl`, `session.json`; cursor `~/.beacon/endpoint/state/fx.json` | No hooks, OTLP or plugin surface exist. No session-end event | No |
| goose | Adapter only (manual hooks file plus manual OTLP) | Proposed `~/.agents/plugins/beacon-endpoint/hooks/hooks.json`; goose `config.yaml` OTLP | No installer yet. No tool output on hooks | Yes (`{"decision":"block"}`) when hand-wired |
| Browser: claude.ai (`claude_web`), chatgpt.com (`chatgpt_web`) | MV3 extension to local OTLP | Extension options. Loaded unpacked (not in Chrome Web Store); Firefox unlisted AMO `.xpi` | Prompt, response, tools. Retention defaults to **full** content. Stream formats are private, so adapters are experimental | n/a |
| Claude Code / Cursor Cloud Agents | Sandbox hooks to GCS/S3 | `beacon cloud claude-web print-setup/print-hooks`, `cursor print-*`, `gcs-setup`, `s3-setup` | Uploads fail open | n/a |
| CI jobs | `beacon ci exec` / `start` / `finish`, GitHub Action `asymptote-labs/agent-beacon@vX` (`action.yml`) | `$RUNNER_TEMP/beacon/runtime.jsonl` | | n/a |
| SDKs | `@asymptote/sdk` (OpenLLMetry for Anthropic/OpenAI, `Observe.wrapClaudeAgentQuery()`, Vercel AI SDK `experimental_telemetry`) | App code | | n/a |

General limitations **[docs]**:
- Poll paths "cannot hold, approve, deny or delay tool calls".
- Approvals are never synthesized from persisted records. An approval inferred from a pre-tool
  notification is marked `event.fidelity=inferred`.
- `beacon-hooks` sits synchronously on every tool call, so latency matters (changelog v1.3.42:
  a large post-tool hook went from about 250 ms to lower).
- MDM env defaults: `BEACON_ENDPOINT_HARNESSES=claude,codex`, plus optional
  `BEACON_HOOK_HARNESSES`.

---

## 3. Event model and schema

### 3.1 Envelope

Required fields (`docs/telemetry-schema/event-schema.mdx`) **[docs]**:

| Field | Value |
|---|---|
| `timestamp` | RFC3339 UTC, nanoseconds |
| `vendor` | `beacon` |
| `product` | `endpoint-agent` |
| `schema_version` | `1.0` |
| `event.kind` | `agent_runtime` |
| `event.action` | Normalized action |
| `event.category` | Category |
| `severity` | `info` / `low` / `medium` / `high` / `critical` |
| `endpoint` | `hostname`, `os`, `agent_version` |
| `harness` | `name`, `version`, `executable_path`, `config_path` |

Optional fields and provenance:
- `event.id` (deterministic UUID) and `sequence`.
- `harness.collection_method` is one of `hook`, `plugin`, `otlp`, `poll`.
- `event.fidelity` is `observed` or `inferred`.

Entities: `user`, `origin` (`local`/`cloud`/`ci`), `run` (CI provider, run id, workflow, PR,
actor), `session` (`id`, `working_directory`), `tool`, `command` (`command`, `exit_code`,
`duration_ms`), `file` (`path`, `operation`, `language`, `diff_hash`, `diff_bytes`), `mcp`
(`server`, `tool`, `method.name`, `protocol.version`, `resource.uri`, `session.id`), `approval`
(`required`, `decision`, `reason`), `policy` (`id`, `name`, `decision`, `enforcement`, `reason`),
`prompt.text`, `system_context` (skill listings), `content` (`retention`, `included`,
`redacted`, `truncated`), `handoff`, `share`, `vcs`, `user_agent`, `destination`, `health`,
`jsonrpc`/`rpc`/`network`/`server`/`error`, and top-level `model`, `repository`, `branch`,
`message`, `raw`, `field_truncated`.

### 3.2 OTel mapping

OTel GenAI semantic-convention attributes are preserved under `gen_ai.*`:
- `request`, `response`, `usage`, `input.messages`, `output.messages`, `system_instructions`,
  `tool.call.{id,arguments,result}`, `retrieval`, `evaluation`, `agent`, `provider`, `workflow`.
- They are also projected into flat fields (`model`, `tool.name`, `prompt.text`).

`gen_ai.usage` is the single token representation:
- Counters: `input_tokens`, `output_tokens`, `cache_creation.input_tokens` (plus
  `ephemeral_1h_input_tokens`), `cache_read.input_tokens`, `reasoning.output_tokens`, `cost_usd`.
- `cost_usd` holds runtime-reported cost only. List-price estimates are computed at report time
  from an embedded catalog generated from LiteLLM's price table
  (`cli/beacon/internal/pricing`), with optional overrides in `<BaseDir>/pricing/overrides.json`.
- Generic process/runtime OTLP metrics are filtered out unless `--include-runtime-metrics` is
  set.

### 3.3 Actions seen in code

From `pkg/asymptoteobserve/*.go` **[code]**:

| Group | Actions |
|---|---|
| Session | `session.started`, `session.ended`, `session.idle`, `session.activity`, `session.status`, `session.error`, `session.context`, `session.handoff`, `session.shared`, `session.share_requested`, `session.commit_linked` |
| Prompt and agent | `prompt.submitted`, `agent.message`, `agent.response`, `agent.reasoning`, `agent.plan`, `agent.error` |
| Tool | `tool.invoked`, `tool.completed`, `tool.failed` |
| Command and file | `command.executed`, `file.read`, `file.modified`, `file.created`, `file.deleted`, `file.edited` |
| MCP | `mcp.tool_invoked`, `mcp.tool_listed` |
| Approval | `approval.requested`, `approval.allowed`, `approval.denied` |
| Self and inventory | `inventory.snapshot`, `inventory.heartbeat`, `endpoint.heartbeat`, `git.hook_budget_exceeded` |

Several source-native names (`tool.execute.before`, `tool.execution_start`, ...) are normalized
into these.

### 3.4 runtime.jsonl

| Property | Value |
|---|---|
| Format | One JSON object per line |
| Path, user mode | `~/.beacon/endpoint/logs/runtime.jsonl` |
| Path, system mode (macOS/Linux) | `/var/log/beacon-agent/runtime.jsonl` |
| Path, system mode (Windows) | `%ProgramData%\Beacon\Endpoint\logs\runtime.jsonl` |
| Rotation | 10 MiB, 5 numbered archives |
| Max event size | 64 KiB (`DefaultMaxEventBytes`, `pipeline.go:9`) **[code]**. An oversized event drops its raw payload first, then the event |
| Inventory | Separate `inventory_state.jsonl` |
| Stability | Field names are a release contract ("Wazuh-compatible JSONL") |

### 3.5 Redaction and secrets

Source: `pkg/asymptoteobserve/privacy.go`, `docs/security/retention-redaction.mdx`.
- Pattern-based redaction runs before truncation and replaces values with `[REDACTED]`. Targets:
  - labeled secrets (`token=`, `AWS_SECRET_ACCESS_KEY=`, JSON `api_key`);
  - fields named `*password|secret|token|api_key|access_key|private_key|authorization|credential(s)|cookie`;
  - `Authorization` headers and `--password`/`--token` flags;
  - URL credentials and PEM/OpenSSH private keys;
  - prefixed tokens with the prefix kept (`sk-`, `AKIA`/`ASIA`, `ghp_`/`github_pat_`, `glpat-`,
    `xox*-`, `sk_live_`, `AIza`, npm, HF, JWTs, Beacon's own `bcn_*` keys);
  - a 40-character AWS secret next to its key id.
- Docs state that bare random passwords are not detected.
- Limits: per-string 4096 bytes (`DefaultStringLimit`), raw strings 2048 bytes, events 64 KiB.
- The retention field holds `full`, `redacted` or `metadata`. The `--content-retention` install
  flag is now a deprecated no-op: "Beacon always captures full content subject to redaction and
  size limits" (`cli/beacon/cmd/endpoint.go:445`) **[code]**.
- Known gap: open issue #958, "Apply secret redaction before direct Splunk/Falcon HEC
  forwarding".

### 3.6 Sampling and retention

- No sampling found. Everything is captured; noise is filtered by action type (metrics, Codex
  transport spans) **[code/docs]**.
- Local retention is bounded by rotation only (about 60 MiB of JSONL).
- The optional `history.db` SQLite index (`beacon endpoint traces reindex`) defaults to 90 days
  and 1024 MiB (`--retention-days`, `--max-size-mb`) **[docs]**.
- `memory.db` is durable.
- Beacon Cloud retention: the install wizard says sessions are "kept while the account is
  active" (changelog v1.3.42). The pricing page lists no retention figures **[site]**.

---

## 4. Detection and policy

### 4.1 Rule language

`spec/threat-rules/SPEC.md` (v1.1) describes itself as "Sigma for agent telemetry".
- Format: YAML. Required fields: `id, version, title, severity, status, posture, emit, tests`.
- Match logic is either a single `match:` CEL expression over the event bound as `e`, or
  `correlation:` with `scope: session` (the only scope), a `window` (Go duration),
  `order: sequence|any` (v1.1), and at least 2 `steps`.
- One finding per session per rule.
- `posture` is `detect` or `enforce-capable` and flows to `policy.enforcement`.
- `status` is `experimental`, `stable` or `deprecated`. A stable rule must carry at least one
  `match` and one `no_match` fixture, and every fixture must pass.
- `taxonomy` holds OWASP LLM, MITRE ATLAS and `beacon_category`.
- CEL uses RE2 (no ReDoS) with a cost cap of 5,000,000 (`threatrules.MaxEvalCost`).
- Derived fields: `e.gen_ai.tool.call.result_text` and `e.gen_ai.system_instructions_text`,
  each redacted and capped.
- Field typos fail at load time because paths are type-checked against the schema.
- Engine: `pkg/asymptoteobserve/threatrules`. Rules are external data in `~/.beacon/endpoint/rules`
  managed by `beacon rules list|add|remove|pull|lint|fields`.
- `beacon rules pull <url>` is https-only, size-capped and takes optional `--sha256`.
- `beacon scan` is offline and read-only, and sorts by `(timestamp, sequence)`.
- Six baseline rules are embedded in the binary
  (`pkg/asymptoteobserve/rulestore/baseline`): `credentials-in-curl-data`, `curl-pipe-to-shell`,
  `exfiltrate-system-prompt`, `ignore-previous-instructions`, `recursive-root-delete`,
  `secret-read-then-egress`.
- A store rule replaces a baseline rule only by id.
- The full corpus ships as release asset `threat-rules.tar.gz`.

### 4.2 Built-in corpus

77 rules in `rules/`. Every rule found has `posture: detect`.

| Category | Rules (id, severity) |
|---|---|
| agent-control (4) | agent-permission-bypass-spawn (high), handoff-to-second-agent-with-credentials (critical, experimental), permission-scope-expansion-via-env (high), sandbox-escape-attempt (critical) |
| approval-abuse (3) | approval-bypass-via-renamed-binary (critical), approval-denied-command-executed-anyway (critical), high-risk-approval-allowed-without-reason (high) |
| context-exfiltration (13) | archive-then-upload (medium), browser-session-read-then-egress (high), cloud-metadata-then-egress (critical), credentials-in-curl-data (high), dns-tunnel-exfiltration (high), env-dump-piped-to-network (critical), git-bundle-then-egress (high), secret-read-then-destructive (high), secret-read-then-egress (high), secret-read-then-external-mcp (high), secret-read-then-mcp-tool-handoff (medium), ssh-tunnel-egress (high), upload-to-paste-transfer-site (high) |
| credential-access (9) | browser-session-store-read, credential-file-read, env-secret-enumeration, gpg-keyring-access, kubernetes-secret-file-read, password-manager-db-read, process-table-secret-scrape, shell-history-secret-scan (all high), ssh-agent-key-enumeration (medium) |
| external-access (5) | cloud-metadata-endpoint-access (high), external-mcp-tool-call (low), imds-access-via-noncurl-client (high), imds-obfuscated-ip-fetch (high), link-local-internal-target-fetch (medium) |
| prompt-injection (11) | covert-action-injection, decode-and-execute-injection, exfiltrate-context-secrets, exfiltrate-context-via-markdown-image, indirect-injection-in-tool-result, invisible-character-prompt-injection, mcp-poisoned-tool-description (high); exfiltrate-system-prompt, ignore-previous-instructions, indirect-injection-new-instructions-marker, role-override-jailbreak (medium) |
| resource-consumption (3) | disk-fill-attempt (medium), excessive-single-call-cost (high), fork-bomb-pattern (high) |
| risky-command (13) | base64-decode-piped-to-shell, crontab-modification, curl-pipe-to-shell, kernel-module-load, path-injection-into-login-file, persistence-install-command, privileged-container-run, root-equivalent-group-add, setuid-or-root-shell-escalation, sudoers-nopasswd-tampering (high); recursive-root-delete (critical); shell-escape-via-interactive-tool, world-writable-chmod (medium) |
| sensitive-edit (11) | agent-control-surface-modified, agent-instruction-surface-modified, autostart-unit-file-installed, direnv-envrc-modified, etc-hosts-modified, shell-login-dotfile-modified, ssh-authorized-keys-modified (high); auth-security-code-modified, cicd-pipeline-file-modified, container-or-iac-file-modified, dependency-manifest-modified (medium) |
| source-control (5) | git-config-exec-injection, git-force-push-to-protected, git-hook-installed, git-remote-tampering (high); git-history-rewrite (medium) |

### 4.3 Blocking and approval

- **Open build: no built-in enforcement.**
  - An off-by-default seam (`cli/beacon-hooks/internal/policy`, contract in
    `pkg/asymptoteobserve/policycontract`) applies when `BEACON_POLICY_PROVIDER` names an
    executable.
  - The `pre-tool` and `permission-request` hooks send it one JSON
    `{version:"1", phase, platform, event}` on stdin and read
    `{decision:"allow"|"deny", reason, rule_id, severity, mode}`.
  - Timeout is **2 s** (`policy.go:31`). The seam fails open on error, timeout, non-zero exit or
    malformed output. Only the exact string `"deny"` denies.
  - On deny it writes a telemetry event with `policy.enforcement=enforce` and returns the
    runtime's native deny shape. Shapes are listed in section 2 (Claude/Qwen
    `hookSpecificOutput.permissionDecision`, Cursor `permission: deny`, Devin `reject`,
    goose `block`, Kiro/Kimi exit 2).
  - Runtimes with no deny shape (Codex, Hermes, OpenCode, Cline, the Pi family, Gemini, Copilot,
    OpenClaw, Factory) allow anyway.
- **Enterprise enforcement** is a separate signed package:
  - `sudo beacon endpoint enforcement enable` works only on an Apple Silicon Mac in system mode,
    connected to Beacon Cloud, with no user-mode Beacon for the console user.
  - It downloads from `<dashboard>/api/cli/enforcement/download` and verifies checksum, Apple
    team `3A98D35XJR` and Gatekeeper. Version and edition must read `enterprise`, or it rolls back.
  - The repo contains "no enforcement logic" and CI refuses `enterprise`-tagged files
    (`cli/beacon/cmd/endpoint_enforcement.go`; `CLAUDE.md`) **[code/docs]**.
  - Pricing: "Block or require approval for risky actions" is Enterprise only **[site]**.
- Native harness approvals are observed (`approval.*`) where runtimes expose them.

### 4.4 False-positive handling

- Local: no suppression or exception field exists in the rule spec. The levers are editing,
  replacing or removing rules (`--rules <dir>`, store override by id), `event.fidelity`
  conditions inside rules, and fixtures plus `beacon rules lint`.
- Beacon Cloud "Findings" (hourly, LLM-style analysis, separate from `beacon scan`):
  - Kinds: Security, Skill, Behavior.
  - Each finding can be marked open/resolved/dismissed and rated helpful or not helpful with a
    note. "The next analysis reads your feedback, so it stops repeating what you rejected."
  - Recurrences update the existing finding.
  - Analysis runs "in an isolated sandbox per account" on a "compact, redacted record". Every
    finding must cite events **[docs: `docs/cloud/findings.mdx`]**.

---

## 5. Memory layer

| Aspect | Local (open source) | Beacon Cloud (Pro) |
|---|---|---|
| Store | `memory.db` (SQLite) next to the endpoint base dir. Tables `evaluations`, `candidates`, `memories` (`cli/beacon/internal/learning/store.go`) **[code]** | Server-side. Same `beacon.learning.v1` shapes and IDs. Separate from local, with no sync in either direction |
| Unit | Memory record: `kind` (`workflow`, `correction`, `debugging_pattern`, `gotcha`, `convention`), title, body, applicability, tags, evidence event IDs, provenance | Same |
| Session to memory | (a) `beacon memory evaluations run` sends a bounded, redacted trace projection to an external "System One" evaluator: TypeSafe **Jev** (`https://api.typesafe.ai/v1/systemone`, model `jev-latest`) or SubQ (`subq-snap-preview`). Three yes/no questions return probabilities. A candidate needs `task_success ≥ 0.50` and a mean of 3 ≥ 0.60. The evaluator writes no lesson text. (b) `beacon memory candidates create` with lesson text written by a human or agent. Then `candidates approve|reject|supersede` | Agent drafts a lesson from a forwarded session, the user confirms, then `beacon_memory_candidate_create`, then `beacon_memory_candidate_review`. "There is no evaluator in Beacon Cloud" **[docs]**. beacon.sh markets a "dreaming process" that reviews successful sessions and drafts skills **[site]**; the Findings "Skill" kind is the documented counterpart |
| Serving: MCP | `beacon mcp serve` (stdio or loopback HTTP `127.0.0.1:8766`, `/mcp`, `/health`; hand-rolled JSON-RPC, not an SDK). Tools: `search_activity`, `summarize_activity`, `get_activity_event`, `list_activity_filters`, `search_memory`, `get_memory`, `get_memory_context` (at most 5 items). All read-only **[code: `internal/mcpserver/server.go`]** | `https://mcp.beacon.sh`, Streamable HTTP + OAuth (RFC 9728 metadata check), or `bcn_mcp_` tokens (read-only, 90-day default, ≤25 active). Tools: `beacon_memory_candidate_create`, `_candidates_list`, `_candidate_get`, `_candidate_review`, `beacon_memory_search` (all words must match), `beacon_memory_get`, `beacon_memory_skill`, plus session, findings, lens and share tools (section 8) |
| Serving: Agent Skills | `beacon memory skills preview|install <candidate>` writes `.agents/skills/<slug>/SKILL.md` with provenance in frontmatter `metadata` (`beacon_memory_id`, `beacon_candidate_id`, `beacon_memory_kind`, `beacon_tags`) | `beacon_memory_skill` renders the same file. The agent writes it to the repo |
| Workflow skills | `beacon-memory-recall`, `beacon-memory-distill`, `beacon-memory-promote` (plus `beacon-lens-create`, `beacon-trace-share`). Installed by plugin marketplace (`.claude-plugin/marketplace.json`, `.cursor-plugin/...`), `npx skills add asymptote-labs/agent-beacon`, Pi packages, or Kiro Powers. The wizard copies them into `~/.agents/skills` and `~/.claude/skills` | Same skills switch to Cloud MCP when `beacon-managed` is connected (v1.3.43) |
| Scope | **Project** (resolved repo; a worktree maps to its main checkout). "Cross-project or user-global memory is not automatic" | Per account ("You see only your own history"). Team or org-shared memory is **not documented** **[unverified]**. Enterprise lists "Findings across your whole org", not memory |
| Gating | Every write is a CLI action. Hooks never call evaluators. The dashboard's Memory page is read-only | Write scope must be granted in the OAuth approval. Pro-only; a free account gets an error that links to pricing |

---

## 6. Install and rollout

| Path | Commands / artifacts |
|---|---|
| macOS | `brew trust asymptote-labs/tap && brew tap asymptote-labs/tap && brew install beacon && beacon endpoint install`. Signed and notarized `BeaconEndpointAgent-<v>-arm64.pkg` (arm64 only) installs to `/opt/beacon` in system mode |
| Linux | `curl -fsSL .../install.sh \| bash` (checks `checksums.txt`, apt/dnf). `.deb`/`.rpm` amd64 and arm64. Statically linked binaries. Tarball user mode. Packages and checksums are not GPG-signed (issue #907) |
| Windows | `msiexec /i BeaconEndpointAgent-<v>-x64.msi [/qn]`. **Unsigned** (issue #906). Service `BeaconCollector`. Cloud forwarding "Not yet supported" on Windows |
| Agent-driven | "Read https://beacon.sh/start.md then set me up on Beacon" |
| Interactive wizard | `beacon endpoint install` on a TTY: sign in (browser PKCE, token at `~/.beacon/auth/session.json` 0600), **Beacon Cloud preselected**, Local is opt-out, privacy mode Standard or Metadata-only, then an agent-tools screen (Cloud MCP plus Skills, each preselected). The confirm screen is the consent point. Skipped for system, root, CI, piped stdin, dry runs and `BEACON_ONBOARDING=0` |
| Per-harness | `beacon endpoint hooks install|status|uninstall --harness <x> [--level project]`; `beacon endpoint discover|status|doctor [--fix]|repair|uninstall [--keep-logs] [--keep-config]` |
| Backfill | After every install and connect: 30 days of session stores, 24 MiB budget (≤12 MiB per runtime); `--no-backfill` or `BEACON_BACKFILL=0` to skip |
| MDM | Jamf Pro (extension attributes, scripts, Claude/Falcon/S3/GCS/Sentinel guides), Fleet (policies, queries, SQL), Rippling. Intune is named on pricing **[site]** but has no doc found **[unverified]**. Env-driven config (`BEACON_ENDPOINT_HARNESSES`, `BEACON_SPLUNK_HEC_*`, `BEACON_FALCON_HEC_*`). Hooks install in the console user's context. **No headless Cloud enrollment for fleets yet**: `sudo beacon endpoint connect --system` opens browser approval ("planned") |
| CI | `beacon ci exec -- claude -p "..."`; `beacon ci start/finish`; GitHub Action `uses: asymptote-labs/agent-beacon@v0.0.95` with `mode: start|finish` (examples in `examples/github-actions`) |
| Cloud sandboxes | `beacon cloud claude-web|cursor print-setup|print-hooks`, `beacon cloud gcs-setup|s3-setup` (creates bucket plus a scoped SA key or IAM user and access key; docs warn this is for proof-of-concept use). `BEACON_ORIGIN=cloud` shuttle uploads to GCS, S3 or Beacon ingest |
| Updates | CLI notice: cached daily GitHub check (`BEACON_UPDATE_CHECK=0` disables). Homebrew upgrade then `beacon endpoint repair`. macOS system package self-update `beacon endpoint update enable --check-only|...` with modes off (default), check-only and auto. Reads `update-manifest.json`, verifies checksum, Developer ID team `3A98D35XJR` (pinned in binary since v1.3.43) and Gatekeeper, health-checks, rolls back |
| Release supply chain | Tag `v*` runs GoReleaser, macOS sign and notarize, Windows MSI install test, then `checksums.txt` and Sigstore build-provenance attestations (`gh attestation verify`). Actions pinned by SHA |
| OS support | macOS (launchd; system pkg Apple Silicon only), Linux (systemd or supervised), Windows x64 (SCM; user mode supervised) |

---

## 7. Exports and integrations

| Destination | Mechanism | Auth / where secrets live | Delivery |
|---|---|---|---|
| Local JSONL | Default | n/a | Append, rotation |
| Beacon Cloud | Vector `http` sink tails `runtime.jsonl` + `inventory_state.jsonl` → `POST /v1/ingest/runtime` and `/v1/ingest/inventory`, gzip NDJSON | Per-device `bcn_device_*` key only in `<BaseDir>/asymptote/vector-secrets.json` (0600), read by Vector's secret backend | Batches of 5 MB / 5,000 events / 10 s. ≤1 runtime batch/s/device. Limits 8 MiB compressed, 10k lines, 1 MiB/line. Disk buffer 512 MiB runtime / 256 MiB inventory. Retries on 429/5xx, drops other 4xx. At-least-once with server dedup on `event.id`. Revocation takes ≤1 min (cached checks). Forwards only from the connect point forward, plus the 30-day backfill |
| Splunk | Collector `splunk_hec` exporter (`--splunk-hec-endpoint/-token/--splunk-index`) | HEC token in collector config | Collector exporter. Redaction gap (#958) |
| CrowdStrike Falcon LogScale | Collector HEC exporter, or Vector tail for hook-only | Ingest token in collector config | Same |
| Microsoft Sentinel | Azure Monitor Agent + DCR content pack over JSONL | Customer AMA | File tail |
| Rapid7 InsightIDR | Custom Logs webhook content pack (`vector.toml`) | Webhook URL kept outside Beacon config | Vector |
| Sumo Logic | HTTP Logs & Metrics Source pack (`vector.toml`) | Customer | Vector |
| Wazuh | `localfile` config plus rules pack (`beacon endpoint wazuh print-config|install-pack|validate`) | n/a | Wazuh agent |
| Datadog | Datadog Agent custom log collection (`service:beacon-endpoint-agent`) | Customer agent | File tail |
| Elastic | Filebeat or Elastic Agent pack; local Docker test stack (`elastic up`) | Customer | File tail |
| AWS CloudWatch Logs | Vector pack | Customer AWS creds | Vector |
| S3 / GCS | Vector forwarder helpers (`install-forwarder.sh`), Terraform base modules (put-only `beacon-endpoint-writer` user or SA with `objectCreator` prefix-scoped), CI upload, cloud-agent snapshots | Customer creds; no key created unless asked | Vector checkpoints |
| Slack (Cloud) | Dashboard connector for alert notifications | OAuth app | n/a |
| Okta (Cloud) | Connector populating users, groups and devices (org URL + API token) | API token in Cloud | Sync |
| Other (named on asymptotelabs.ai) | GitHub, Jira, ServiceNow and about 15 more logos **[site, unverified as integrations]** | | |

Format everywhere: the Beacon event JSON shape, unwrapped (Vector re-parses each line).
Metadata-only mode is a local Vector VRL transform (`pack/metadata-transforms.toml`). A test
forces every text field to be classified as removed or kept.

---

## 8. Open source vs cloud and SaaS

### 8.1 Plans

Sources: `docs/deployment/plans.mdx` **[docs]** and beacon.sh/pricing **[site]**.

| | Open Source | Beacon Cloud | Beacon Cloud Pro | Enterprise |
|---|---|---|---|---|
| Price | $0 | $0 | $20/mo, or $15/mo billed annually | Custom ("Book a demo") |
| Hosting | Self | Managed (beacon.sh) | Managed | Managed, or "your own bucket and region" |
| Capture, local dashboard, `beacon scan`, export to Elastic/S3/OTel | ✓ | ✓ | ✓ | ✓ |
| Unified timeline across agents and machines | — | ✓ | ✓ | ✓ |
| Findings (risks, skills, habits) | Local rules only | 50 lifetime | Unlimited | Unlimited, org-wide |
| One-click skills and fix prompts | — | ✓ | ✓ | ✓ |
| MCP | Local server | Sessions search, lenses, sharing | Plus findings, feedback, memory | Same as Pro |
| Memory over MCP; skills from sessions; evidence trail | — | — | ✓ | ✓ |
| Share sessions by link | — | ✓ | ✓ | ✓ |
| Alerts | — | Daily digest | Real-time email | Real-time and custom |
| Block or require approval | — | — | — | ✓ |
| Fleet rollout (Jamf, Intune, Fleet) | — | — | — | ✓ |
| SSO, RBAC, audit logs | — | — | — | ✓ |
| Support | Community | Community | Priority | Dedicated SLA |

Notes:
- Seats and per-user pricing are not stated **[site]**.
- On a free account at the 50-finding limit, analysis stops but uploads continue.
- Pro is "a property of your beacon.sh account". Nothing changes on the device.

### 8.2 Hosted product surface

| Surface | Detail |
|---|---|
| Dashboard pages | Sessions, Lenses (with a marketplace), Analytics (token/cost), Findings, Inventory (configs, MCP servers, skills, hooks per device), Devices, MCP Access, Billing, public Profile page |
| Site extras | beacon.sh also links a "Trace Marketplace" and a leaderboard **[site]** |
| Session sharing | `beacon share` → `api.beacon.sh` `PUT /v1/traces/{external_id}`, `POST .../events/batch` (≤1000/batch). Visibility direct (link-only, default), private or public. Auto-share by project is server-built from a `session.share_requested` event |

Cloud MCP tool list **[docs]**:
- All plans: `beacon_search_sessions`, `beacon_grep`, `beacon_read_session`, `beacon_get_event`,
  `beacon_lookup`, `beacon_usage`, `beacon_lenses_list`, `beacon_lenses_*`,
  `beacon_shared_traces_list`, `beacon_share_session`, `beacon_shared_trace_update`.
- Pro: `beacon_findings_list`, `beacon_get_finding`, `beacon_finding_feedback`, plus the 7 memory
  tools from section 5.

### 8.3 SSO, RBAC and multi-tenancy

**[docs + inferred from code]**
- Enterprise lists SSO, RBAC and audit logs. No implementation is in this repo.
- Tenant identity is "added server-side" from the device key.
- Ingest checks three things: the key is valid, the approving user is still an org member, and
  the org is enabled for managed ingest.
- Org membership exists: removing a user revokes every device they approved.
- Self-hosted enterprise: Asymptote reads the customer's bucket with ClickHouse ClickPipes on
  GCP through SQS or Pub/Sub (`terraform/*-asymptote-reader`).
- An internal `beacon-ingest` service is referenced (`/v1/ingest/*`,
  `scripts/mint_test_device.py`) but is not in this repo.
- The asymptotelabs.ai product name for the managed platform is "Asymptote Platform": "Managed
  ingest, retention, search, and detections", "policy gates, and approvals", and role views for
  Security, Engineering, Platform and Finance **[site]**.

### 8.4 Company

**[site]**
- Asymptote Labs, Inc. Seed round: "$5.5M" banner on asymptotelabs.ai.
- Named backers or advisors include the Deputy CISO of Microsoft Gaming, Modal's co-founder and
  CTO, Brex's CISO and Dropbox's former CTO.
- An in-repo blog post claims SOC 2 Type II (`docs/blog/asymptote-achieves-soc-2-type-ii-compliance.mdx`).
- Discord community.

---

## 9. Security and privacy model

| Topic | Fact |
|---|---|
| Default egress | Hooks never touch the network. The collector binds to loopback. `scan`, the dashboard and the local MCP server are offline |
| Network calls that exist | Each is explicit or opt-in: login (PKCE), connect/enroll, Vector forwarder, `share` (plus an opt-in git pre-push share), `mcp connect` metadata check, `mcp token create`, `rules pull`, memory evaluator calls (TypeSafe/SubQ), daily update check (GitHub), self-update, enterprise download |
| What leaves in Cloud Standard mode | Every runtime line after connect "exactly as Beacon wrote it locally" (redacted, 4 KB strings), the 30-day backfill and inventory |
| What leaves in Cloud Metadata-only mode | Text content stripped on device by Vector. Keeps ids, hashes, counts, actions, paths and token usage |
| Data use | The install wizard (v1.3.42) states that unless the user opts out, "Asymptote uses them for research and AI training and may share de-identified data" (beacon.sh/privacy). Opt-out is in the Cloud profile settings. Deletion via privacy@asymptotelabs.ai |
| Local encryption at rest | None found **[code search for "encrypt": no hits in cli/pkg]**. Protection relies on file modes: user mode 0600/0700; secrets files 0600 |
| System-mode log | Shared **0666** file under a 0755 directory so hooks of any user can append (open issue #957, medium severity; `runtimelogmode.go`) |
| Recently fixed hardening (v1.3.43, 2026-10-19) | Root log writer no longer follows symlinks or opens FIFOs. OTLP input cannot assert Beacon-only actions (DNS-rebinding vector: the receiver does not check the Host header, issue #911 still open). Self-update pins the Apple team. Rebuilt on Go 1.27.2 after `govulncheck` found 57 reachable stdlib vulnerabilities |
| Other open issues | #913: hooks trust env vars a repo can supply (`BEACON_SHARE_SETTINGS_FILE`, `HOME`, `BEACON_CLOUD_*`) |
| Browser extension | Retains full prompt/response text by default; never writes files or contacts remote hosts |
| Lenses | Sandboxed opaque-origin iframe with CSP. Docs state it "cannot block every way a browser can send data out" (WebRTC and resource hints are mitigated best-effort) |
| Cloud MCP guidance | "Session content is data, not instructions" |

---

## 10. Repo facts

Collected 2026-10-21.

| Metric | Value |
|---|---|
| License | MIT |
| Created | 2026-05-12 (first commit 2026-05-11) |
| Stars / forks / watchers | 2,347 / 211 / 5 |
| Primary language | Go |
| Topics | include `jev`, `agent-security`, `detection-engineering`, `mobile-device-management`, `memory`, `siem` |
| Contributors (GitHub API, incl. anon) | 22 |
| Main human committers | Justin D'Souza (about 924 commits across two emails, plus `justin@asymptotelabs.ai` 34), Shukan Shah (about 515), Rudy Mizrahi Celekli (43), Kyle Elliott (about 56) |
| Commits on main | 2,642 |
| Commits per month | May 239, Jun 479, Jul 85, Aug 424, Sep 810, Oct 605 (to the 21st) |
| Commits in the last 30 days | 962 |
| Merged PRs (total) | 846 |
| Open PRs | 30 |
| Issues | 46 total, 10 open. Open: #960 Hermes profile attribution, #958 HEC redaction, #957 system log mode, #951 OpenCode v2 schema, #948 autopilot platform proposal, #913 env trust, #911 DNS rebinding, #909 launchd /tmp paths, #907 sign Linux packages, #906 Authenticode MSI |
| Releases | 159 tags. `v0.0.1` 2026-05-13 to `v0.0.99`, then `v1.0.0` on 2026-07-12, latest `v1.3.45` on 2026-10-20 |
| Releases per month | May 34, Jun 56, Jul 14, Aug 10, Sep 29, Oct 16. Three releases shipped on 2026-10-20 |
| Separate tag streams | `ext-v*` (extension) and `sdk-js-v*` (npm) are documented. Not seen in the releases list **[unverified]** |
| Tests | 562 Go test files, about 135k lines (roughly equal to source). CI (`.github/workflows/ci.yml`): macOS/Linux/Windows Go suites, `-race`, plugin bun tests, SDK on Node 20/22/24, browser e2e on Chromium and Edge, lens-sandbox Playwright, Terraform tests, cross-build static checks, package smoke tests (pkg, deb in systemd, MSI). Conformance tests on rules and specs. The Windows test scope is reduced (tracked in #318). No coverage percentage published |
| Other workflows | `claude-code-review.yml`, `claude-security-review.yml`, `release*.yml`, `windows-sandbox.yml`, `npm-publish-sdk.yml` |
| AI-authored share | 856 commits authored as `Claude <noreply@anthropic.com>`, plus 5 `claude[bot]` and 189 `Cursor Agent`: about 40% by author. Commits whose author or message names an AI agent (Claude, Cursor, Codex, Copilot, Devin): **1,963 / 2,642 ≈ 74%**. Top co-author trailers: Claude Opus 5.5 (366, plus 83 at 1M), Claude Opus 5 (229, plus 106 at 1M), Cursor (196), Claude Fable 5.x (63) |
| Repo agent guidance | `CLAUDE.md` (596 lines), `AGENTS.md`, `.claude/skills/self-verify-beacon-in-sandbox` |

Notable recent changes (Oct 2026, `docs/changelog/cli.mdx`):
- The wizard now ends at the hosted dashboard.
- The forwarder flushes every 10 s, down from 60 s.
- The share API moved to `api.beacon.sh`.
- Sharing honors Metadata-only mode.
- Live `agent.message` capture for Claude Code, Codex, Cursor, Factory and Devin Desktop.
- `beacon endpoint enforcement enable` (enterprise installer) shipped.
- Install, hooks and inventory follow `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `HERMES_HOME` (plus the
  active profile), `CLINE_DIR`, `GEMINI_CLI_HOME`, `COPILOT_HOME` and `PI_CODING_AGENT_DIR`.
- Security hardening as in section 9.

Roadmap themes (inferred from open issues and docs saying "planned" or "not yet"):
- Package signing (Linux, Windows Authenticode).
- Headless MDM enrollment to Cloud.
- Windows Cloud forwarding.
- Chrome Web Store publication.
- A goose installer.
- Per-profile Hermes attribution.
- System-log isolation.
- New harness proposals (Autopilot).

---

## 11. Overlap with cross-agent messaging (agents and sessions talking to each other)

Beacon has no live agent-to-agent message bus. It has no rooms, mailboxes or delivery protocol
between running agents. What it does have is session-to-session transfer and cross-agent
observation:

| Feature | Mechanism | Paths |
|---|---|---|
| Session handoff | `beacon handoff list|export|resume [--agent codex] [--new]`. Reopens natively (`claude --resume`, `codex resume`, `opencode --session`) or writes a Markdown **brief** (sanitized; fences longer than any backtick run so quoted content cannot forge structure) to `~/.beacon/endpoint/handoffs` (0700/0600). Starts a new session whose first prompt names the brief by absolute path and ends with the marker `[beacon-handoff from=<harness> session=<id>]`. Hooks record `session.handoff` with `handoff.source_harness`/`source_session_id`, labeled "an observed claim, not a verified identity". Approvals stay on: Beacon strips `COPILOT_ALLOW_ALL`, `HERMES_YOLO_MODE`, `GOOSE_MODE` and forces Cline `--auto-approve false`. Reads 14 runtimes' stores; local only | `cli/beacon/internal/handoff/runtimes.go`, `pkg/asymptoteobserve/handoff.go`, `docs/concepts/session-handoff.mdx` |
| Cross-harness memory | One agent's reviewed trace becomes memory another harness retrieves over MCP or Skills. Asynchronous, review-gated, not messaging | Section 5 |
| Cloud MCP read of other sessions | Any connected agent can search, grep and read every forwarded session of the same account across machines and harnesses (`beacon_read_session`, `beacon_grep`) | `docs/deployment/managed-mcp.mdx` |
| Shared traces | Publish a session as a link (direct/private/public). `--follow` keeps it updated live (polling, upload on change). Git notes `refs/notes/beacon` carry `beacon:<harness>/<session-id> <share-url>` per commit, shared by opt-in pre-push | `cli/beacon/cmd/share*.go`, `cli/beacon/internal/gitlink` |
| Active-session record | `~/.beacon/endpoint/active-sessions.json` (≤256 entries, 14-day prune) maps working dir to the current harness session. `BEACON_SESSION_ID`/`BEACON_HARNESS` env lets a child process know which session it is inside | `pkg/asymptoteobserve/activesession` |
| Subagents | Captured as events where runtimes expose them (Claude Code hooks, Hermes subagent stop, Qwen, Kimi) | Section 2 |
| Detection of agent-spawns-agent | `agent-permission-bypass-spawn` (e.g. `claude --dangerously-skip-permissions` in a command), `permission-scope-expansion-via-env`, `handoff-to-second-agent-with-credentials` (spawning claude, codex, cursor-agent, aider, goose, opencode, gemini or an MCP server with secret paths or tokens on the command line), `secret-read-then-mcp-tool-handoff` | `rules/agent-control/`, `rules/context-exfiltration/` |
| OpenClaw Gateway | Beacon observes OpenClaw, a gateway connecting chat apps (Slack, Discord, Teams, ...) to agents. Beacon does not provide the messaging; it reads plugin, OTLP and session files | `docs/runtimes/openclaw-gateway.mdx` |
| Hermes Agent | Supported as a harness (hooks into `$HERMES_HOME/config.yaml`, poll `state.db`). Directly relevant to any product that runs Hermes | Section 2 |

Naming coincidence: Beacon's memory evaluator is TypeSafe's "Jev" (a repo topic). It is an
external probability-scoring API and unrelated to any internal component of the same name.

---

## Notable design choices (neutral)

- The log is the contract. Every feature (scan, memory, MCP, share, forwarding) reads one
  append-only JSONL with deterministic event IDs, so capture paths can overlap and deduplicate.
- Provenance is explicit on every event: `collection_method` (hook/plugin/otlp/poll) and
  `fidelity` (observed/inferred).
- Each harness gets its strongest native surface (hook, plugin, OTLP or session-store poll), and
  poll doubles as a 30-day onboarding backfill.
- Rules are open, self-testing data (YAML+CEL with fixtures). The engine is in the binary; the
  corpus is external.
- Enforcement lives outside the open build: an inert, fail-open 2 s exec seam plus a signed,
  separately distributed enterprise package.
- Memory is review-gated and project-scoped. The local and Cloud stores are deliberately not
  synced.
- The hosted channel is a single Vector pipeline with per-device keys. Privacy-mode stripping
  happens on the device.
