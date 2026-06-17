# Hermes runtime verification — #04: personality prompt, cwd/identity (audit Q5 & Q8)

**Date:** 2026-06-16
**Scope:** Seeding Friday + Atlas on Hermes v0.14.0 — system-prompt delivery mechanism and per-run workspace/cwd control.
**Source tree:** `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/hermes-agent` (Hermes v0.14.0, gitignored clone).
**Method:** every claim grounded in a file:line I read this session. Where the prior audit (`c9562d39:docs/audit-2026-05-17-hermes-vs-openclaw.md`) or the task brief is contradicted by source, it is flagged **CONTRADICTION**.

---

## 1. Is there any length cap / truncation on `ephemeral_system_prompt` or a personality `system_prompt`?

**No length cap and no truncation on `ephemeral_system_prompt`.** It is concatenated to the cached system prompt verbatim at API-call time, in two parallel code paths:

- `agent/conversation_loop.py:736-740`:
  ```py
  effective_system = active_system_prompt or ""
  if agent.ephemeral_system_prompt:
      effective_system = (effective_system + "\n\n" + agent.ephemeral_system_prompt).strip()
  if effective_system:
      api_messages = [{"role": "system", "content": effective_system}] + api_messages
  ```
- `agent/chat_completion_helpers.py:923-927` — identical concat against `agent._cached_system_prompt`.

The only transformations applied to `ephemeral_system_prompt` anywhere:
- **ASCII sanitization** (not truncation): `agent/conversation_loop.py:1721-1724` runs `_strip_non_ascii` over it and overwrites the attribute. A ~600-line English prompt is unaffected; only non-ASCII bytes are stripped.
- **A 60-char log preview** for console output only: `agent/agent_init.py:836-837` (`prompt_preview = agent.ephemeral_system_prompt[:60] + "..."`). Cosmetic; does not mutate the prompt sent to the model.

**Personality `system_prompt` has no separate cap either — because a personality IS an `ephemeral_system_prompt`.** In `gateway/run.py` the `/personality` command resolves the config entry and stores it as the daemon's ephemeral prompt:
- `gateway/run.py:9650-9658` `_resolve_prompt(value)` flattens the dict form `{system_prompt, tone, style}` into one string (string form passed through as-is).
- `gateway/run.py:9670-9683` writes it to `config["agent"]["system_prompt"]` (persisted to config.yaml) **and** sets `self._ephemeral_system_prompt = new_prompt`.
- `gateway/run.py:15222-15223, 15399` the daemon's `_ephemeral_system_prompt` is concatenated into `combined_ephemeral` and passed as the `ephemeral_system_prompt=` kwarg.

So both knobs land in the same un-capped concat path.

**CONTRAST — context files (SOUL.md/AGENTS.md/etc.) ARE capped at 20,000 chars each** with head/tail truncation: `agent/prompt_builder.py:824-826` (`CONTEXT_FILE_MAX_CHARS = 20_000`, head 0.7 / tail 0.2) and `_truncate_content` at `prompt_builder.py:1292-1301`. This cap applies to the *file-injection* path, NOT to `ephemeral_system_prompt`. A ~600-line (~20-40KB) prompt delivered as a context file would be silently truncated; delivered as `ephemeral_system_prompt` it is sent in full.

---

## 2. Config `personalities[]` entry vs per-call `ephemeral_system_prompt` — which for Friday/Atlas?

The two are **not two independent mechanisms** — a personality is a named, config-persisted *source* for the daemon's single `_ephemeral_system_prompt`. Behavior delta:

| | Config `personalities[]` (+ `/personality <name>`) | Per-call `ephemeral_system_prompt` (run-create `instructions`) |
|---|---|---|
| Where defined | `hermes_cli/config.py:1428-1431` (`"personalities": {}`, string or `{system_prompt,tone,style}` dict) | Supplied per request — `gateway/platforms/api_server.py:2893,2943` maps `body["instructions"] → ephemeral_system_prompt` |
| Scope | **Daemon-global, persistent.** Selecting one rewrites `config.yaml agent.system_prompt` and the single in-memory `self._ephemeral_system_prompt` (`run.py:9677-9683`). Affects ALL subsequent runs on that daemon. | **Per-run.** Passed straight into `_create_agent(ephemeral_system_prompt=...)` (`api_server.py:2978-2984`), never persisted, never shared. |
| Per-agent isolation | ❌ none — one daemon has one active personality. Cannot have Friday and Atlas active simultaneously via personalities. | ✅ each `POST /v1/runs` carries its own `instructions`; two concurrent runs get two different prompts. |
| Trajectory persistence | personality text lands in config; not the point — still injected as ephemeral, NOT saved to trajectories | explicitly NOT saved to trajectories (`agent_init.py:157` docstring) |

**RECOMMENDATION: use per-call `ephemeral_system_prompt` (run-create `instructions`) for Friday and Atlas. Do NOT use `personalities[]`.** A single Hermes daemon per container must host *both* Friday and Atlas (and any marketplace agents); the personalities mechanism is a global toggle that can only express one persona at a time and mutates config.yaml as a side effect. The orchestrator already holds the rendered ~600-line prompt per identity (today it seeds Friday/Atlas via `friday_identity.py`); pass that string as `instructions` on each run, keyed by which agent the inbound request targets. This matches the audit §3.3/§3.5 conclusion that the orchestrator holds the per-identity→kwargs mapping.

---

## 3. AGENTS.md / SOUL.md auto-injection vs `skip_context_files` + `load_soul_identity`

The flags are consumed in `agent/system_prompt.py:build_system_prompt_parts`. Exact behavior matrix (SOUL.md lives at `HERMES_HOME/SOUL.md`; project files like AGENTS.md come from cwd):

**Identity slot (`system_prompt.py:89-98`):**
```py
if agent.load_soul_identity or not agent.skip_context_files:
    _soul_content = _r.load_soul_md()
    if _soul_content:
        stable_parts.append(_soul_content); _soul_loaded = True
if not _soul_loaded:
    stable_parts.append(DEFAULT_AGENT_IDENTITY)   # hardcoded fallback
```

**Project-context slot (`system_prompt.py:222-231`):**
```py
if not agent.skip_context_files:
    _context_cwd = os.getenv("TERMINAL_CWD") or None
    context_files_prompt = _r.build_context_files_prompt(cwd=_context_cwd, skip_soul=_soul_loaded)
```

| `skip_context_files` | `load_soul_identity` | SOUL.md (HERMES_HOME) | Project files (AGENTS.md/CLAUDE.md/.cursorrules/.hermes.md from cwd) |
|---|---|---|---|
| False (default) | (ignored) | **injected** as identity slot; `skip_soul=True` prevents double-inject | **injected** (first-match-wins: .hermes.md → AGENTS.md → CLAUDE.md → .cursorrules, `prompt_builder.py:1438-1456`) |
| True | False | **NOT injected** → falls back to `DEFAULT_AGENT_IDENTITY` (`system_prompt.py:96-98`) | **NOT injected** |
| True | True | **injected** as identity slot | **NOT injected** (project context stays disabled — exactly the cron/batch use case described at `agent_init.py:185-187`) |

Two extra facts that matter for seeding:

1. **Project context is auto-discovered from `TERMINAL_CWD`, not from os.getcwd()** (`system_prompt.py:227`). The gateway sets TERMINAL_CWD away from the repo dir specifically so it doesn't pick up Hermes' own AGENTS.md (`system_prompt.py:223-226` comment). So whatever workspace dir we point a container at, any AGENTS.md/CLAUDE.md there auto-loads unless `skip_context_files=True`.
2. **SOUL.md and AGENTS.md pass through a prompt-injection scanner before injection** (`prompt_builder.py:1324,1363` → `_scan_context_content`, `prompt_builder.py:55-73`). If the seeded content trips an invisible-unicode or threat-pattern check it is **replaced wholesale** with `[BLOCKED: ... potential prompt injection ...]` — the identity silently disappears. Our Friday/Atlas seed text must be scanner-clean. **CONTRADICTION-ish caveat to the audit:** the audit §3.5 says "inject identity via system prompt" is an option but does not flag that the SOUL.md *file* path is both 20k-capped AND injection-scanned; the `ephemeral_system_prompt` path is neither.

**Implication for seeding:** the cleanest, cap-free, scan-free path is to deliver the full Friday/Atlas persona via `ephemeral_system_prompt` and run with `skip_context_files=True, load_soul_identity=False` so the file-based SOUL/AGENTS machinery (capped + scanned + cwd-coupled) stays out of the way entirely. `DEFAULT_AGENT_IDENTITY` is harmless when the ephemeral prompt fully defines the persona.

---

## 4. Can the orchestrator set the working directory per run via a run-create `workspace` field?

**No.** This is the largest correction to both the task brief and the prior audit.

**CONTRADICTION (task brief):** the brief lists `workspace` as a kwarg of `agent/agent_init.py:init_agent`. It is not there. `grep -n "workspace" agent/agent_init.py` returns exactly one hit — `agent_init.py:1007 _init_kwargs["agent_workspace"] = "hermes"` — which is a hardcoded **memory-provider scoping label** (passed to `agent._memory_manager.initialize_all`, `agent_init.py:1010`), not a filesystem cwd. There is no `workspace` parameter in the `init_agent` signature (`agent_init.py:87-140`).

**CONTRADICTION (prior audit §9 open question framing):** there is no `workspace` field on the run-create request body. `POST /v1/runs` (`gateway/platforms/api_server.py:2862-2943`) parses only: `input`, `instructions`, `previous_response_id`, `conversation_history`, `session_id`, `model`. No `workspace`/`cwd`/`workdir`. And `_create_agent` (`api_server.py:851-912`) has no `workspace` kwarg and never passes one to `AIAgent`.

**How cwd is actually resolved — daemon-global env var, read live per tool call:**
- Boot: `gateway/run.py:619-622` sets `os.environ["TERMINAL_CWD"]` once, from config.yaml `terminal.cwd` (bridged at `run.py:445`), defaulting to `$HOME` when unset/placeholder.
- Tool execution reads it **live on every call**: `agent/tool_executor.py:118` and `:586` — `cwd = function_args.get("workdir") or os.getenv("TERMINAL_CWD", os.getcwd())`.
- Context-file discovery, subdirectory hints, terminal allowed-root all read the same env var: `system_prompt.py:227`, `agent_init.py:1362`, `tui_gateway/server.py:3174-3175`.
- The terminal env object does take a `cwd` at construction and honors a per-call `cwd` override (`tools/environments/base.py:311-312, 796` — `effective_cwd = cwd or self.cwd`), but the only per-call override exposed upward is the **model-chosen `workdir` tool argument** (`tool_executor.py:118`), not an orchestrator-supplied run field.

**So the only two ways to control cwd are both NOT per-run-from-orchestrator:**
1. Set `TERMINAL_CWD` (config.yaml `terminal.cwd` or env) — daemon-wide, one value per container.
2. Let the model pass `workdir` per shell tool call — agent-driven, not orchestrator-driven, and only scopes the terminal tool.

**Implication:** per-user workspace isolation on Hermes is achieved at the **container boundary** (one container per user, `TERMINAL_CWD` = that user's EFS workspace) — exactly the OpenClaw posture today. Per-*agent* (Friday vs Atlas, or marketplace agents) cwd isolation *within one container* is NOT available via a run-create field; if Friday and Atlas need different subdirectories, that must be encoded in their `ephemeral_system_prompt` ("work under /workspace/atlas") and enforced via the model's `workdir` arg — soft, not hard. This is weaker than OpenClaw's per-agent `workspace` in `AgentEntrySchema` (audit §3.2).

---

## 5-bullet summary + recommendation

- **No cap/truncation on `ephemeral_system_prompt`** — sent verbatim (concat at `conversation_loop.py:736-740` / `chat_completion_helpers.py:923-927`), only ASCII-sanitized (`conversation_loop.py:1721-1724`) and 60-char log-previewed (`agent_init.py:837`). The 20k-char head/tail cap (`prompt_builder.py:824-826,1292`) applies ONLY to file-based context (SOUL/AGENTS), not to ephemeral. A ~600-line prompt is safe as ephemeral, unsafe as a SOUL.md file.
- **`personalities[]` is a daemon-global, config-mutating toggle** (`run.py:9670-9683`) that can only express one persona per container at a time; it ultimately just sets the same `_ephemeral_system_prompt`. It cannot host Friday AND Atlas simultaneously.
- **SOUL.md/AGENTS.md auto-injection is gated by `skip_context_files`/`load_soul_identity`** (`system_prompt.py:89-98, 222-231`): `skip=True,load=False` → no SOUL, no project files, falls back to `DEFAULT_AGENT_IDENTITY`; `skip=True,load=True` → SOUL only. Project files load from `TERMINAL_CWD`, and both SOUL & AGENTS pass an injection scanner that can BLOCK them wholesale (`prompt_builder.py:55-73`).
- **There is NO per-run `workspace`/`cwd` field** — contradicts both the task brief (no `workspace` kwarg in `init_agent`; `agent_workspace="hermes"` at `agent_init.py:1007` is a memory label) and the audit. cwd is the daemon-global `TERMINAL_CWD` (`run.py:619-622`), read live per tool call (`tool_executor.py:118,586`), with the model's `workdir` arg as the only per-call override.
- **RECOMMENDATION — seed Friday/Atlas via per-call `ephemeral_system_prompt` (run-create `instructions`), with `skip_context_files=True` and `load_soul_identity=False`.** This delivers the full ~600-line persona uncapped and unscanned, keeps Friday and Atlas independently addressable on one daemon (each run carries its own prompt), and avoids the SOUL.md file path's 20k cap + injection-scan + TERMINAL_CWD coupling. Per-user workspace isolation comes from the container boundary (`TERMINAL_CWD` → that user's EFS dir), matching today's model; accept that intra-container per-agent cwd is soft (prompt + model `workdir`), not a hard config field like OpenClaw's `AgentEntrySchema.workspace`.
