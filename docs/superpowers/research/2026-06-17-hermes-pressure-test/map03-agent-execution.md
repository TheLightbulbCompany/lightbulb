# Map 03 — Hermes Agent Execution Internals

Basis: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ `4c46c35ed` (branch `basis-v0.14.0-spec`).
All file:line citations are against that tree. Every claim below was read directly.

> Scope note: this map covers prompt assembly, the conversation turn loop, tool
> dispatch, and context-file / @-reference injection. It does NOT re-derive the
> network/provider adapter layer (see other maps).

---

## 1. How a single turn executes (the conversation loop)

Entry: `AIAgent.run_conversation` (`run_agent.py:3840`) is a thin forwarder to
`agent.conversation_loop.run_conversation` (`agent/conversation_loop.py`).

**`conversation_loop.py` is ONE function ~3700 lines long** (225 KB file). The
main loop body is a single `while` at `conversation_loop.py:532`:

```
while (api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0) or agent._budget_grace_call:
```

Per-turn setup (`conversation_loop.py:180-298`):
- Resets a large pile of per-turn instance flags (`agent._post_tool_empty_retried`,
  `_vision_supported`, scrubbers, guardrails) — turn state lives as **mutable
  attributes on the shared `AIAgent` object**, not a turn-local context object
  (`:180-191`, `:270-277`).
- Builds a fresh `IterationBudget(agent.max_iterations)` every turn (`:215`).
- Copies `conversation_history` into a local `messages` list (`:229`) and appends
  the user message (`:295-297`). `current_turn_user_idx` is captured (`:297`) and
  later used to target ephemeral injection at exactly that message.
- **Gateway creates a fresh `AIAgent` per inbound message** — the loop explicitly
  re-hydrates todo state, nudge counters, and memory-turn counters FROM the
  passed-in history because in-memory state is empty on a cache miss / 1h idle
  eviction / config-signature mismatch / process restart (`:231-256`, esp. the
  comment block `:237-245`). This is a load-bearing assumption: **Hermes is NOT a
  long-lived per-session object in gateway mode; it is reconstructed-from-history
  per message.** (Surprise vs. a naive "container holds the agent" mental model.)

Per-iteration (inside the `while`):
1. `iteration_budget.consume()` gate (`:553`); on exhaustion it strips tools and
   does a final call via `_handle_max_iterations` (`:3722-3738`).
2. Repair passes BEFORE every API call: `_sanitize_tool_call_arguments`
   (`:649`), `_repair_message_sequence` (`:668`). These mutate `messages`
   defensively (wedged `tool→user` / `user→user` tails, corrupted JSON args).
3. Build `api_messages` (a per-call copy, `:676-719`) — ephemeral injection
   (memory prefetch + plugin `pre_llm_call` hooks) is spliced into the
   **current-turn user message only** (`:685-696`), never persisted.
4. System message is prepended fresh each call (`:736-740`): `effective_system =
   active_system_prompt + ephemeral_system_prompt`. The cached system prompt is
   replayed **verbatim** to keep the upstream prefix cache warm (`:731-735`).
5. After the model returns, tool calls are dispatched via
   `agent._execute_tool_calls(...)` (`:3256`).
6. `iteration_budget.refund()` is called for `execute_code`-style turns so
   programmatic tool-calling doesn't eat the budget (`:2820`, `:3286`).

**Iteration budget defaults** (`agent/iteration_budget.py:21-24`): parent agent
caps at `max_iterations` default **90**; each delegated subagent gets its OWN
independent budget capped at `delegation.max_iterations` default **50**. Comment
explicitly states total iterations across parent+subagents CAN exceed the
parent cap. (Relevant to any cost/runaway-budget concern Isol8 would inherit:
there is no global fleet-wide iteration ceiling — it is per-agent-tree.)

---

## 2. How the system prompt is assembled (3 tiers, cached once)

Builder: `agent/system_prompt.py:build_system_prompt_parts` (`:60`) →
`build_system_prompt` (`:274`). Built ONCE per session, cached on
`agent._cached_system_prompt`, rebuilt ONLY after context compression
(`invalidate_system_prompt`, `:293`). The whole string is treated as one
cache block (`:282-287`).

Three tiers, joined by `\n\n` (`:267-271`):

- **stable** (`:84-212`): identity (SOUL.md via `load_soul_md()` or hardcoded
  `DEFAULT_AGENT_IDENTITY`, `:89-98`); `HERMES_AGENT_HELP_GUIDANCE`; tool-aware
  guidance gated on `agent.valid_tool_names` (memory/session_search/skills/kanban,
  `:104-118`); computer-use block (`:122-124`); nous-subscription block (`:126`);
  tool-use-enforcement guidance with model-family branches for gemini/gpt/codex
  (`:136-160`); skills system prompt (`:162-178`); an Alibaba model-name workaround
  (`:185-192`); environment hints (`:197`); platform hints incl. a plugin
  `platform_registry` lookup (`:201-212`).
- **context** (`:214-231`): caller `system_message` (`:219-220`) + context files
  (AGENTS.md/.cursorrules/CLAUDE.md/.hermes.md) via `build_context_files_prompt`
  using `TERMINAL_CWD` for discovery (`:222-231`).
- **volatile** (`:233-265`): built-in memory snapshot + USER.md profile
  (`:236-245`); external memory-provider block (`:248-254`); a timestamp /
  session-id / model / provider line (`:256-265`).

**Ephemeral system prompt is NOT part of this** — it is appended at API-call time
in the loop (`conversation_loop.py:737-738`) so it stays out of the cached/stored
prompt (confirmed `system_prompt.py:217-218` and the loop site). This is the
mechanism that lets per-turn content change without busting the cache.

**Personality / identity precedence (subtle):** identity is SOUL.md-first ONLY
when `agent.load_soul_identity or not agent.skip_context_files` (`:90`). Cron-mode
sessions deliberately keep HERMES_HOME persona while disabling cwd project
instructions (`:88` comment). For Isol8, replicating Friday/Atlas SOUL identity
means writing SOUL.md into `HERMES_HOME`, not into the workspace cwd — and the
cache-once-per-session rule means identity changes require a session rebuild.

---

## 3. Context-file injection, the 20k cap, and injection scanning

These are TWO DIFFERENT mechanisms with TWO DIFFERENT caps. The spec's "20k cap"
language conflates them; verify which one it means.

### 3a. Project context files (the "20k cap")
`agent/prompt_builder.py:build_context_files_prompt` (`:1417`). Priority order,
**first match wins — only ONE project context type loads** (`:1438-1444`):
1. `.hermes.md`/`HERMES.md` (walks to git root, `:1332-1352`)
2. `AGENTS.md`/`agents.md` (cwd only, top-level, `:1355-1368`)
3. `CLAUDE.md`/`claude.md` (cwd only, `:1371-1384`)
4. `.cursorrules` + `.cursor/rules/*.mdc` (cwd only, `:1387-1414`)

SOUL.md from `HERMES_HOME` is independent and always included when present
(`:1448-1452`).

Cap: `CONTEXT_FILE_MAX_CHARS = 20_000` (`prompt_builder.py:824`) — a **per-source
CHARACTER cap**, not token. Truncation keeps head 0.7 / tail 0.2
(`CONTEXT_TRUNCATE_HEAD_RATIO=0.7`, `CONTEXT_TRUNCATE_TAIL_RATIO=0.2`, `:825-826`)
with a `[...truncated ...]` marker (`:1300`). Applied per source via
`_truncate_content` in each loader (`:1325,1349,1365,1381,1414`). **So multiple
sources can't stack (only one project type loads) but SOUL.md + one project file
= up to ~40k chars of system-prompt context, each independently truncated.**

### 3b. Injection scanning (`_scan_context_content`)
`prompt_builder.py:55-73`. Runs on EVERY context file before injection
(SOUL.md `:1324`, .hermes.md `:1347`, AGENTS.md `:1363`, CLAUDE.md `:1379`,
.cursorrules `:1395`, .cursor/rules `:1407`).

Scans for:
- 10 regex threat patterns (`_CONTEXT_THREAT_PATTERNS`, `:36-47`): "ignore
  previous instructions", "do not tell the user", "system prompt override",
  hidden HTML comments/divs, exfil `curl ...$KEY/TOKEN`, `cat .env/.netrc`, etc.
- A small invisible-unicode set (`_CONTEXT_INVISIBLE_CHARS`, `:49-52`):
  zero-width + bidi-override chars only.

On ANY finding it **replaces the entire file content** with
`[BLOCKED: <file> contained potential prompt injection ...]` (`:71`) and logs a
warning. **Skepticism flag:** this is a shallow regex blocklist, not a robust
sanitizer — trivially bypassed (e.g. "disregard the prior text", base64,
homoglyphs outside the tiny invisible-char set). For Isol8, where workspace files
are seeded/owned by the platform this matters less, but if marketplace-deployed
agents ship AGENTS.md, this is the only barrier and it is weak.

### 3c. @-reference injection (separate, PERCENTAGE cap — NOT 20k)
`agent/context_references.py`. This is the `@file:`, `@folder:`, `@diff`,
`@staged`, `@git:`, `@url:` inline expansion. Cap is
`hard_limit = context_length * 0.50` (refuse) and `soft_limit = context_length *
0.25` (warn) — token-based, sized off the model's context window
(`context_references.py:167-186`). **This is the cap the spec should cite for
runtime user-message attachments; it is NOT 20k.** If the spec says "context
injection is capped at 20k", that is wrong for the @-reference path.

Path safety for @-references (`:342-360`): blocks `.ssh/.aws/.gnupg/.kube/...`,
`.env`, `.netrc`, `.pgpass`, etc., and an `allowed_root` confinement
(`_resolve_path`, `:329-339`) that raises if a target escapes the workspace.
Call site (`gateway/run.py:7154-7159`) binds `allowed_root=TERMINAL_CWD`, so @
refs cannot escape the workspace cwd. **But there is NO injection-content
scanning on @-expanded file/url content** — `_scan_context_content` is applied
ONLY to the project context-file path (3b), not to @-references. A user can `@url:`
or `@file:` arbitrary content into the prompt with no threat-pattern check.

---

## 4. How tools are dispatched

Dispatch decision: `AIAgent._execute_tool_calls` (`run_agent.py:3751`) calls
`_should_parallelize_tool_batch(tool_calls)` (`agent/tool_dispatch_helpers.py:103`)
→ concurrent or sequential.

**Parallel gating** (`tool_dispatch_helpers.py:103-146`):
- Single call → sequential (`:105-106`).
- Any `clarify` in batch → sequential (`_NEVER_PARALLEL_TOOLS`, `:41`,`:109`).
- A tool is parallel-eligible only if in a **tiny hardcoded allowlist**
  `_PARALLEL_SAFE_TOOLS` (`:44-56`: read_file, search_files, web_search,
  web_extract, session_search, skill_view, skills_list, vision_analyze, 3 HA
  tools) OR a path-scoped file tool with non-overlapping path
  (`_PATH_SCOPED_TOOLS` = read_file/write_file/patch, `:59`,`:132-139`) OR an MCP
  tool whose server opted into parallel (`_is_mcp_tool_parallel_safe`, `:90-100`).
- **`terminal` is NOT in any allowlist → terminal always forces the whole batch
  sequential.** Same for delegate_task, computer_use, memory, todo. So real-world
  multi-tool turns are mostly sequential.

**Concurrent path** (`agent/tool_executor.py:execute_tool_calls_concurrent`,
`:64`): pre-flight interrupt check (`:74-83`), parse+guardrail+plugin-block each
call (`:85-143`), checkpoint file-mutating / destructive-terminal commands
(`:104-123`), then a `ThreadPoolExecutor(max_workers=min(n, _MAX_TOOL_WORKERS=8))`
(`:55`,`:288`) running `agent._invoke_tool` per call (`:237`), results collected
in original order.

**Actual invocation** — `agent.agent_runtime_helpers.invoke_tool` (`:1467`):
- Plugin `pre_tool_call` block check (`:1476-1487`).
- Hardcoded `if/elif` ladder for **agent-loop-local tools**: todo, session_search,
  memory (+ external-memory bridge), memory-manager tools, clarify, delegate_task
  (`:1489-1544`). These bypass the registry because they touch agent state.
- Everything else → `run_agent.handle_function_call` (`:1546-1552`), which lives in
  **`model_tools.py:731`** (top-level module, NOT under `agent/`), and routes
  through `registry.dispatch(...)` (`model_tools.py:812-820`). `_AGENT_LOOP_TOOLS`
  are rejected here with "must be handled by the agent loop" (`:761-762`).

**Tool-name repair** (`agent_runtime_helpers.py:repair_tool_call`, `:1556`):
lowercases, snake-cases, strips `_tool`/`Tool` suffixes (twice), then difflib
fuzzy-matches (cutoff 0.7) — a tolerance layer for models that emit `TodoTool_tool`
etc. (`:1572`). Implies the model-side tool-calling is noisy enough to need this.

**Implication for Isol8:** the tool surface is large and split across two
locations (`agent/agent_runtime_helpers.invoke_tool` for loop-local tools +
`model_tools.registry.dispatch` for the rest). Any "swap Hermes tools for Isol8
tools" estimate must account for BOTH dispatch sites plus the parallel-safety
allowlist, the guardrail layer (`agent._tool_guardrails`), the plugin
`pre_tool_call` hook, and the checkpoint manager — none of which the spec's
tool-mapping likely enumerates.

---

## 5. Notable contradictions / surprises vs. a clean "drop-in runtime" assumption

- **No turn-context object.** All per-turn state is mutable attributes on a
  shared `AIAgent` (`conversation_loop.py:180-277`). A 3700-line single-function
  loop with ~dozens of reset-per-turn flags is fragile to fork/modify.
- **Gateway agent is per-message, rebuilt from history** (`:231-256`). The
  "container = one warm agent" model is wrong; readiness/hydration cost is paid
  per message on cache miss.
- **Two different caps the spec calls "20k".** Project context files: 20k *chars*
  per source, one-project-wins (`prompt_builder.py:824`). @-references:
  50%/25% of *token* context window (`context_references.py:167`). Don't conflate.
- **Injection scanning is regex-only and only on project context files.**
  @-reference and @url content is NOT scanned (`context_references.py` has no
  call to `_scan_context_content`). The scanner is trivially bypassable
  (`prompt_builder.py:36-52`).
- **`handle_function_call` is in `model_tools.py`, not `agent/`** (`:731`) — a
  tool-dispatch surface outside the `agent/` directory the task pointed at;
  easy to miss when scoping a tool rewrite.
- **No global iteration ceiling** — budgets are per-agent-tree and subagents can
  push total iterations past the parent cap (`iteration_budget.py:21-24`).
