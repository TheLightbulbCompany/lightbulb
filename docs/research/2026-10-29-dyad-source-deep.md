# Dyad, read from the source (29 Oct 2026)

Source: `github.com/dyad-sh/dyad` at `main`, commit `0ee4a524a8128a3aaefd97c35b9078d6daa0ca62` (committed 2026-10-29T19:17Z), downloaded as a tarball through the GitHub API. Also `dyad-sh/nextjs-template` at `121d56749e874184aa308e05d3f3ee4f2070b401`. All paths below are relative to the Dyad repo root at that commit unless stated. Read for Lightbulb Apps (`docs/superpowers/specs/2026-10-28-apps-design.md`): a headless builder (Hermes on AgentCore, or a person's Claude Code or Codex) that runs `lightbulb app ...` from the open-source Rust CLI, with the backend provisioning the customer's own Neon and Vercel through Stripe Projects.

This does not repeat `docs/research/2026-10-28-small-software-dyad-read.md` (what Dyad is, pricing, the top-level directory list). It goes into the code.

**Licence rule used throughout.** Code outside `src/pro` is Apache-2.0 and may be reused with attribution. Anything under `src/pro/**` is FSL-1.1 with a competing-use ban. FSL findings are described by behaviour only, never quoted or paraphrased, and are marked **FSL, do not reuse**.

## 0. What changed since the 28 Oct read, in six lines

1. **The agent loop is FSL now, for every mode.** Since PR #4381 (`1a94b002`, 2026-08-27, "Make Build mode use the agentic loop"), Build, Ask, Plan and Agent all run through `handleLocalAgentStream` in `src/pro/main/ipc/handlers/local_agent/local_agent_handler.ts` (`src/ipc/handlers/chat_stream_handlers.ts:20`, `:3001-3133`). The `<dyad-write>` tag protocol everyone associates with Dyad is legacy: still parsed for old chats and for manual "approve" of proposals, no longer what the model is asked to emit.
2. **`distributed_machines` is not remote execution.** It is an actor runtime for multiple Electron windows (`docs/adr/main-owned-state-machines.md:1-30`). The open question from the 28 Oct read is closed.
3. **Dyad already runs Claude Code as a backend.** `src/ipc/services/claude_code/runtime.ts:348-385` spawns `claude -p` with every native tool off and Dyad's tools served over a loopback MCP server. Apache.
4. **Neon "revert code and data together" exists and is real**: a per-commit database timestamp plus Neon point-in-time restore of the development branch (`src/ipc/utils/neon_dev_branch.ts`, `src/ipc/handlers/version_handlers.ts:827-900`). It needs a Neon API key, which Stripe Projects does not reveal.
5. **The "auto-fix problems" loop was removed.** The setting is deleted on load (`src/lib/schemas.ts:747`). Self-repair now lives in the agent's Verify step (type check, tests, build), and the tools that do it are FSL.
6. **There is a working eval harness**: `benchmarks/app-builder/` (Playwright journeys + security probes + an LLM judge, 7 models x 3 apps x 3 milestones). Apache. This is the most reusable thing in the repo for us.

## 1. Licence map

| Path | Licence | Evidence |
|---|---|---|
| Everything except the rows below | Apache-2.0 | `LICENSE:1-4` ("All content that resides under the "src/pro/" directory ... is licensed under ... src/pro/LICENSE. Content outside ... is available under the Apache 2 license") |
| `src/pro/**` (198 files, 66,694 lines incl. tests) | FSL-1.1, ALv2 future licence | `src/pro/LICENSE` |
| `packages/ts-pg-schema-diff` (6,189 lines), `packages/pg-schema-classifier` | **MIT** | their `package.json:24` / `:23` |
| `packages/@dyad-sh/react-vite-component-tagger`, `nextjs-webpack-component-tagger` | Apache-2.0 (own LICENSE) | |
| `benchmarks/app-builder/template/nextjs` | Apache-2.0 (own LICENSE) | |
| `dyad-sh/nextjs-template` (separate repo) | Apache-2.0 | GitHub API `license.spdx_id` |
| `NOTICE` | Apache NOTICE: "Dyad / Copyright (c) Dyad contributors", plus bundled mustardscript and Playwright (both Apache) | `NOTICE:1-12` |

No per-file copyright headers exist in Dyad's Apache files. Apache-2.0 section 4 therefore asks us to: ship a copy of the Apache licence with anything we lift, carry the `NOTICE` text ("Dyad, Copyright (c) Dyad contributors"), and mark each lifted file as modified. Recommended header for every lifted file:

```
// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524,
// <original path>. Copyright (c) Dyad contributors. Apache-2.0.
// Modified by The Lightbulb Company.
```

**Apache files that import FSL code (watch these when lifting):**
- `src/ipc/processors/response_processor.ts:53` imports `applySearchReplace` from `src/pro/main/ipc/processors/search_replace_processor`. Lifting the processor means dropping or rewriting search-replace.
- `src/prompts/system_prompt.ts:4` imports `TURBO_EDITS_V2_SYSTEM_PROMPT` from `src/pro/main/prompts/`. Lift `system_prompt.ts` without that import.
- `src/ipc/handlers/chat_stream_handlers.ts:20,43,130` imports the FSL agent handler and chat indexer. Not liftable as a unit anyway.

`CLA.md` binds contributors to Dyad, not users; it does not affect reuse.

## 2. The agent loop and prompts

### Files
- Prompt text (Apache): `src/prompts/system_prompt.ts` (941 lines), `src/prompts/local_agent_prompt.ts` (1,106), `plan_mode_prompt.ts` (134), `supabase_prompt.ts` (545), `neon_prompt.ts` (424) + `neon_prompt_rules.ts` (12), `security_review_prompt.ts`, `summarize_chat_system_prompt.ts`, `compaction_system_prompt.ts`, `test_assertions_prompt.ts`, `guides/*.md` (add-authentication, add-email-verification, add-password-reset, custom-supabase-test-fixtures).
- Mode dispatch (Apache, but welded to FSL): `src/ipc/handlers/chat_stream_handlers.ts` (3,813 lines).
- Legacy tag protocol (Apache): `src/ipc/utils/dyad_tag_parser.ts` (250), `src/ipc/processors/response_processor.ts` (892), `src/ipc/handlers/proposal_handlers.ts`.
- The agent loop and every tool (**FSL, do not reuse**): `src/pro/main/ipc/handlers/local_agent/` (handler 3,122 lines, `tool_definitions.ts` 1,063, `tools/` ~60 tools, `subagents/`, `processors/file_operations.ts`).
- Turbo edits and search-replace (**FSL, do not reuse**): `src/pro/main/prompts/turbo_edits_v2_prompt.ts`, `src/pro/shared/search_replace_{parser,markers}.ts`, `src/pro/main/ipc/processors/search_replace_processor.ts`.

### How it works
**Modes.** `ChatModeSchema = z.enum(["build", "ask", "local-agent", "plan"])` (`src/lib/schemas.ts:178`). The old "agent" value is migrated to "build" on read (`chat_stream_handlers.ts:2552`). Dispatch at `chat_stream_handlers.ts:2171-2174` and `:3001-3170`:
- **Ask**: the local-agent prompt in read-only form (`constructSystemPrompt({chatMode: "local-agent", readOnly: true})`, `:3002-3010`), then the FSL loop with only read tools.
- **Plan**: `PLAN_MODE_SYSTEM_PROMPT` (`plan_mode_prompt.ts:3`), FSL loop restricted to plan tools (`planModeOnly: true`). It runs `planning_questionnaire` and ends with `write_plan`; `src/plan_handoff/` then carries the approved plan into a build chat.
- **Build**: `constructBuildAgentPrompt` (`local_agent_prompt.ts:1060`), FSL loop with `toolProfile: "build"` (`chat_stream_handlers.ts:3107-3133`). The comment at `:3104-3106` describes it as a "fail-closed app-building tool profile: no sub-agents, Engine tools, logs, verification commands, sandbox scripts, or MCP servers".
- **Agent** ("local-agent"): `constructLocalAgentPrompt` (`local_agent_prompt.ts:946`), full tool set, Pro or "basic" (free, 20 messages/day).
- The legacy path (`streamText` then `processFullResponseActions`, `:3176-3445`) is now reachable only outside those four branches; in practice it serves fixtures and proposal approval (`proposal_handlers.ts:407`).

**System prompt assembly** (`local_agent_prompt.ts:625-785`). The prompt is concatenated XML-ish blocks:
`<role>` (`:37-40`), `<app_commands>` (`:42-50`), `<app_lifecycle>` (`:52-90`, when to call `restart_app` / `reinstall_and_restart_app`), `<general_guidelines>` (`:92-126`), `<tool_calling>` (`:128-139`), `<dyad_workflow>` + `<git_context>` (`:141-165`), tool selection rules (`:167-192` Pro, `:383-404` basic), `<development_workflow>` (`:268-331`: Understand, Clarify, Plan, Implement, Verify, Finalize), `[[SERVER_LAYER]]` (`:515-530`, Nitro), test guidance (from `system_prompt.ts:355-556`), `<app_blueprint>` (`:532-606`), and `<ai_rules_meta>` with the app's `AI_RULES.md` pasted in (`:432-465`).

Short quotes worth keeping (Apache):
- Simplicity rule, `local_agent_prompt.ts:99-107`: "Don't create helpers, utilities, or abstractions for one-time operations ... three similar lines of code is better than a premature abstraction."
- Completeness rule, `:116-126`: "NEVER make partial changes ... no placeholders, no partial implementations, no TODO comments."
- Verify step, `:325`: "After making code changes, use `run_type_checks` to verify ..." plus, when testing is on, "run the affected spec(s) with `run_tests` and fix any failures before finishing".
- `run_build` gating, `:316`: an "expensive final verification step", used for package/lockfile/build-config/routing changes or new apps, "Call it once; retry only after fixing a cause indicated by the failed build."
- Edit tool choice, `:174-192`: `search_replace` for small to half-file edits, `write_file` above that; "If `search_replace` fails twice in a row on the same edit ... use `write_file` instead."

**The app blueprint** (`local_agent_prompt.ts:532-606`). For a new app the agent must first ask 1-5 product questions (never framework choices), then write a blueprint (name, design direction, primary colour, image prompts) that the user approves in a card before any code. This is Dyad's answer to "first build looks generic".

**The legacy tag protocol** (still parsed, `dyad_tag_parser.ts:71-250`): `<dyad-write path description>`, `<dyad-rename from to>`, `<dyad-copy from to>`, `<dyad-delete path>`, `<dyad-add-dependency packages="a b">`, `<dyad-execute-sql description>`, `<dyad-search-replace path>`, `<dyad-command type="rebuild|restart|refresh">`, `<dyad-chat-summary>`. The model-facing spec is `BUILD_SYSTEM_PREFIX` (`system_prompt.ts:68-330`) and `BUILD_SYSTEM_POSTFIX` (`:332-344`, the "NEVER, EVER use markdown code blocks" block). `processFullResponseActions` (`response_processor.ts:148-892`) applies them in order: SQL first (Neon or Supabase, `:280-340`), deletes, renames, writes, search-replace, copies, dependency installs; redeploys touched Supabase functions; then `git add` + one commit per turn with the chat summary as message, amending in any files edited outside Dyad (`:794-830`); then appends `<dyad-output type="error|warning">` blocks to the stored message (`:868-890`).

**Agent tool set (FSL, do not reuse; names and behaviour only).** File: read_file, list_files, grep, code_search, write_file, search_replace, rename_file, copy_file, delete_file. Project: add_dependency, enable_nitro, add_integration, execute_sql (default consent "ask"), get_database_table_schema, get_supabase_project_info, get_neon_project_info. Verify: run_type_checks, run_build (max 3 per turn, 10-minute timeout), run_tests (4 fix attempts per spec, 10 runs per turn), run_pre_commit, read_logs, restart_app, reinstall_and_restart_app. Git: git_status, git_diff, git_log, git_show_commit, git_show_file, git_restore_file. Planning: planning_questionnaire, write_plan, exit_plan, write_app_blueprint, update_todos, set_chat_summary. Memory: search_chats, read_chat, explore_chat_history. Sub-agents: spawn_agent (explorer/implementer/reviewer personas), wait_agents, send_message, cancel_agent. Pro web: web_search, web_fetch, web_crawl, generate_image. Also run_shell (with an LLM shell-safety reviewer), execute_sandbox_script, MCP search/schema tools. Default step cap 100 tool calls per turn (`src/constants/settings_constants.ts:2`, read at `local_agent_handler.ts:712`); up to 3 retries of a terminated stream.

**Context selection.** In agent modes the model pulls context with tools; there is no up-front dump. The older up-front path is `extractCodebase` in `src/utils/codebase.ts:621` (Apache): an extension allowlist (`:18`), excluded dirs (`:70`), `.env*` always omitted (`:94`), and outside "smart context" also omits `src/components/ui`, tsconfigs and `components.json` (`:101-118`) to save tokens; files over 1 MB skipped (`:120`). "Smart context" itself is a server-side feature of Dyad's paid engine: the client only sets `dyad_options.enable_smart_files_context` / `smart_context_mode` on the request (`src/ipc/utils/llm_engine_provider.ts:241-252`). Not in the repo.

**Token budgeting.** `src/ipc/utils/token_utils.ts` (Apache): 4 chars per token estimate (`:8-14`); compaction triggers at `min(cap, contextWindow - 25k)` with caps of 190k for Google, 220k for OpenAI, 250k otherwise (`:93-121`); compaction prompt in `compaction_system_prompt.ts`, implementation in `src/ipc/handlers/compaction/`.

### Quality
Prompts are the best-engineered part of the Apache side: each rule carries its reason, tool use is gated on what is actually available (`appLifecycleBlock` returns nothing if the tools are off), and the blocks have snapshot tests (`local_agent_prompt.test.ts`, 862 lines). Weak spots: Dyad-UI language everywhere ("the iframe on the right", "action button above the chat input", "Help button in the bottom left"); the legacy Ask prompt's shouting ("IF YOU USE ANY OF THESE TAGS, YOU WILL BE FIRED", `system_prompt.ts:699`).

### Verdict for Lightbulb
- **Agent loop and tools: REPLACE** with the builder's own loop. Our builders are Claude Code, Codex and Hermes, which already have read/edit/search/shell/todo/sub-agents. Dyad's loop is FSL and would be a second, worse loop inside theirs. What we must supply is the domain part: rules, verification commands, and the provider steps, all through `lightbulb app`.
- **Tag protocol and `response_processor`: REPLACE** (nothing to replace it with; our builders edit files directly). Keep one idea: SQL is applied before files, and the turn ends in exactly one commit whose message is the change summary. That becomes `lightbulb app up` committing whatever the builder left uncommitted.
- **Prompt blocks: ADAPT** into the starter's `AGENTS.md` and the Hermes skill: the simplicity and completeness rules, the Understand/Clarify/Plan/Implement/Verify/Finalize workflow with `lightbulb app check` (see section 3) as the Verify command, the edit-tool fallback rule, the `run_build`-is-expensive rule, the Neon/RLS/secret rules (section 6). Strip all Dyad-UI references.
- **App blueprint: ADAPT** as a room behaviour: one message with 2-3 questions and a one-paragraph blueprint before the first `lightbulb app new`. Behaviour only; the prompt text is Apache and may be adapted, the `write_app_blueprint` tool is FSL and is not needed.
- **Turbo edits / search-replace: FSL, do not reuse.** Not needed: Claude Code and Codex have their own edit tools.
- **Smart context: nothing to reuse** (closed server feature). **Compaction thresholds: not needed** (the host agent manages its own context).

## 3. Error handling and self-fix

### Files
- Type check (Apache): `src/ipc/processors/tsc.ts` (598 lines), `typescript_utility_process_scheduler.ts`, `src/ipc/handlers/problems_handlers.ts`, `src/shared/problem_prompt.ts`.
- Preview errors (Apache): `worker/dyad-shim.js` (352), `worker/dyad_logs.js` (398), `worker/proxy_server.js:248-320` (injection), `src/preview_console/`, `src/components/preview_panel/PreviewIframe.tsx:1619`.
- Tests (Apache plumbing): `src/test_run_queue/` (state 52, transition 114, controller 205), `src/ipc/services/test_run_queue_service.ts`, `src/ipc/handlers/tests_handlers.ts` (3,640), `src/ipc/utils/playwright_*.ts`, `src/ipc/services/e2e_test_*.ts`, `isolated_test_db.ts`.
- Agent verify tools `run_type_checks`, `run_build`, `run_tests`, `read_logs` (**FSL, do not reuse**).
- Deploy-failure fix prompt (Apache): `src/cloudflare_deploy/fix_prompt.ts`.

### How it works
**Type check.** `runTypeScriptCheck` (`tsc.ts:495-596`) resolves the app's own TypeScript, then runs `tsc --pretty false --noEmit --incremental --tsBuildInfoFile <cache> --project <tsconfig>` with defensive "off" flags and version gates (`--explainFiles` only from 4.2, `--incremental` with `--noEmit` only from 4.0), a timeout and an output cap. `parseTypeScriptDiagnostics` (`:299`) turns the text into `{file, line, column, code, message, snippet}`. Runs are serialised per process (`typescriptUtilityProcessScheduler.runExclusive("tsc", ...)`). The Problems panel calls it on demand (`problems_handlers.ts:18-60`); `createProblemFixPrompt` (`problem_prompt.ts:7-30`) renders a numbered "Fix these N TypeScript compile-time errors" message the user can send.

**Auto-fix.** The automatic "fix problems after every turn" loop is gone: `enableAutoFixProblems` is deleted from settings on load (`src/lib/schemas.ts:747`). Repair is now the agent's own Verify step: type check after edits, targeted tests, build when warranted, each with per-turn caps (FSL: 4 fix attempts per spec, 10 test runs per turn, 3 builds per turn). The Apache prompt states the loop rules (`system_prompt.ts:458-486`): read `error-context.md` (the accessibility snapshot) and the failure screenshot first, decide test-wrong vs app-wrong, make one change, re-run; "If the tool says your last change did NOT alter the failure, do NOT retry a small variation"; flaky reruns do not count.

**Preview runtime errors.** The proxy injects `dyad-shim.js` and `dyad_logs.js` into every HTML response (`proxy_server.js:248-320`). The shim posts `window-error`, `unhandled-rejection` and source-mapped errors to the parent window (`dyad-shim.js:177-260`); `dyad_logs.js` forwards every console call with strict size caps (20 args, 8 KB per arg, 64 KB per call, `:7-16`). The renderer shows a banner whose button sends `Fix error: <message>` as a new prompt (`PreviewIframe.tsx:1619`). Server stdout/stderr is captured separately (section 5). Note from Dyad's own benchmark: headless, only server logs exist, because the client-log writer is the iframe (`benchmarks/app-builder/DESIGN.md`, section 5 P4).

**Test queue.** One FIFO per app, one active run, every stop names its cause (`src/test_run_queue/state.ts:1-40`). Managed Neon apps get a throwaway database copy per test case; Supabase apps get a fresh test user per case (`system_prompt.ts:420-432`).

### Quality
`tsc.ts` is careful, defensive production code with a full test file. The shim and console forwarder are well bounded. The removal of auto-fix in favour of agent-driven verification is the right call and matches what we would do.

### Verdict for Lightbulb
- **tsc runner: ADAPT, but move it into the starter.** In a headless loop the cheapest correct design is `pnpm run check` in the starter (`tsc --noEmit && next build`, later `playwright test`) and a small diagnostic parser in the Rust CLI so `lightbulb app check` (or `up`'s pre-flight) returns `{file, line, code, message}` JSON. Port `parseTypeScriptDiagnostics`' format handling (`tsc.ts:299-494`) to Rust; keep its flag list and version gates as comments. The incremental cache and per-process scheduler are not needed on a single-builder sandbox.
- **Fix-loop rules: ADAPT** into the skill: one change per failure, read the evidence first, change approach when the failure does not move, hard caps per turn. These are Apache prompt rules; the FSL counters are not needed because the CLI can enforce caps itself (for example, `up` refuses a 4th consecutive failing deploy in one turn and returns `next: "ask the room"`).
- **Preview error capture: ADAPT** for preview deployments, not iframes. Lightbulb previews are Vercel deployments shown in Canvas. Replace "inject a shim through a local proxy" with a tiny client error reporter in the starter (`window.onerror` + `unhandledrejection` posting to the app's own `/api/_lightbulb/errors`, the same caps as `dyad_logs.js:7-16`), plus Vercel runtime logs read by the backend. `lightbulb app status --errors` returns both. That gives the builder what `read_logs` gives Dyad's agent, without a browser.
- **"Fix error" button: ADAPT** as a room action: when a preview reports a runtime error, the room line offers "fix it", which posts the error to the builder.
- **Test queue and per-case isolation: REPLACE** later. Day one has no tests in the loop; when we add them, run Playwright against the preview URL inside the Vercel/CI build, not on the builder's machine.

## 4. Scaffold and rules

### Files
- `scaffold/` (Apache): Vite 8 + React + React Router + Tailwind 3 + shadcn (49 files in `src/components/ui/`), `AI_RULES.md`, `components.json`, `vercel.json` (SPA rewrite), `vite.config.ts` (with `@dyad-sh/react-vite-component-tagger`), `src/components/made-with-dyad.tsx`, `src/utils/toast.ts`, `pnpm-lock.yaml`.
- Templates list: `src/shared/templates.ts:23-66`: React (the scaffold), `dyad-sh/nextjs-template`, `dyad-sh/react-vite-nitro`, a Neon "Portal mini store". Creation: `src/ipc/handlers/createFromTemplate.ts`, `template_handlers.ts`.
- Rules plumbing: `readAiRules` (`system_prompt.ts:930-941`, falls back to `DEFAULT_AI_RULES` at `:589-606`), `src/ipc/utils/ai_rules_patcher.ts` (idempotent marker-delimited sections), `src/ipc/utils/nitro_setup.ts` (243 lines, adds a server layer to a Vite app and patches the rules).

### How it works
The app's `AI_RULES.md` is the only stack description the model gets; it is pasted into the system prompt every turn (`local_agent_prompt.ts:432-447`: "treat that as the source of truth without re-reading the file"). The scaffold's rules (`scaffold/AI_RULES.md`, quoted in part): "Use React Router. KEEP the routes in src/App.tsx ... The main page (default page) is src/pages/Index.tsx ... UPDATE the main page to include the new components. OTHERWISE, the user can NOT see any components! ... You ALREADY have ALL the shadcn/ui components and their dependencies installed." The Next.js template's `AI_RULES.md` is longer and per-library (shadcn first, Tailwind only, lucide, react-hook-form + zod, sonner, recharts).

When a capability is added later, the matching rules are appended between markers, for example `<!-- nitro:start --> ... <!-- nitro:end -->` (`ai_rules_patcher.ts:4-60`), which teaches routes, the exact import paths, and a "Common mistakes" list. That last list is the pattern worth copying: concrete wrong imports with the right one next to them.

Defects seen: `scaffold/components.json` points `tailwind.css` at `src/index.css` while the file is `src/globals.css` (`scaffold/src/main.tsx:3`), so `shadcn add` would write to a missing file. The scaffold ships a "Made with Dyad" component.

### Verdict for Lightbulb
- **The scaffold itself: REPLACE.** Our starter is Next.js + server routes + Drizzle + Neon on Vercel (spec section 7). Dyad's Vite scaffold has no server layer; its Next.js template (Apache) is the closer base: **ADAPT `dyad-sh/nextjs-template`** at `121d5674` as the starter's skeleton (App Router, shadcn `components.json`, Tailwind config, `src/components/ui/`), then add `lib/door.ts`, Drizzle, `app.yaml`, the error reporter, `check` script. Keep its `LICENSE` and add the Dyad attribution to `NOTICE`. Remove the tagger and "made with Dyad".
- **`AI_RULES.md` idea: KEEP, renamed `AGENTS.md`** (which Claude Code, Codex and Hermes all read natively; Dyad has to paste its file into the prompt because its models do not). Write ours fresh, using Dyad's structure: stack, where things go, "the home page must import new components", per-library rules, a "Common mistakes" list.
- **Marker-delimited rule sections: KEEP** (`ai_rules_patcher.ts`). When `services:` in `app.yaml` gains a provider, `lightbulb app up` appends that provider's rules block to `AGENTS.md` between markers. ~40 lines of Rust.
- **Component tagger: skip.** It exists for click-to-select in the preview; Canvas has no editor.

## 5. Preview and runtime

### Files
- `src/ipc/services/app_runtime_service.ts` (2,515 lines): `executeApp` (`:375-446`) with three modes, host (`executeAppLocalNode`, `:883`), Docker (`executeAppInDocker`, `:1344`) and cloud (`executeAppInCloud`, `:1595`).
- Ports: `shared/ports.ts` (app ports `32100 + appId % 10000`, proxy ports from `42100`).
- Proxy: `src/ipc/utils/start_proxy_server.ts` (98) spawns `worker/proxy_server.js` (926) in a worker thread.
- Logs: `listenToProcess` (`app_runtime_service.ts:1113-1240`), `src/ipc/utils/process_output_log_limiter.ts`, `bounded_output_buffer.ts`.
- Cloud sandbox client: `src/ipc/utils/cloud_sandbox_provider.ts` (1,041). Machines: `src/app_run/`, `src/preview_iframe/`.

### How it works
**Host mode.** Command = custom install+start if both set, else `pnpm install` (with a supply-chain policy) `&& pnpm run dev --port <port>`, or `npm install --legacy-peer-deps && npm run dev -- --port <port>` (`:153-246`). Spawned through a shell in the app folder. stdout is stripped of ANSI, stored as `type: "server"` logs, and scanned: a `(y/N)` prompt becomes an `input-requested` event for the UI; the first `http://localhost:<port>` seen means "server up", which starts the proxy (`:1163-1225`). Drizzle-push prompts on Neon apps are answered with Enter automatically (`:1176-1181`).

**Proxy.** A worker-thread HTTP proxy in front of the dev server. It injects the shim, console forwarder, component selector, recorder, auth bootstrap and screenshot helpers into HTML (`proxy_server.js:248-320`), namespaces HttpOnly cookies per app so previews on one `localhost` do not overwrite each other's sessions (`:33-44`), and falls back across a port band when its port is taken (`start_proxy_server.ts:62-71`).

**Docker mode.** Writes a `Dockerfile.dyad` (`FROM node:22-alpine`) and runs the same pnpm command in a container.

**Cloud mode** (Dyad Pro only; "Dyad Pro API key is required for cloud sandboxes", `cloud_sandbox_provider.ts:236`). The desktop uploads the app's files (excluding `node_modules`, `.git`, `.next`, keeping `.env`) to Dyad's engine (`/sandboxes`, `/sandboxes/{id}/files`, `/logs`, `:722-768`) and gets back a preview URL plus auth token; after each turn it syncs changed and deleted paths (`response_processor.ts:843-852`). The sandbox service itself is closed. The proposed design for a real web/mobile Dyad is `docs/adrs/0002-cloud-runtime-topology.md` (status "Proposed", 2026-02-15): control plane + runtime workers + git workers, "Desktop relay model ... Rejected for v1".

### What changes for headless/cloud
Everything UI-shaped goes: the iframe, the proxy's injected helpers, the `(y/N)` relay, cookie namespacing (each preview has its own Vercel domain). What remains useful is the idea that the runtime reports "server up" and streams bounded logs. For Lightbulb a preview is a Vercel preview deployment of the pushed tree, created by the backend; there is no long-running dev server in the builder's sandbox in the workspace path. On a laptop, `pnpm dev` is the person's own business; `lightbulb app up` still deploys a preview so the link works in the room.

### Verdict for Lightbulb
- **Local dev-server runner, proxy, ports, Docker mode: REPLACE** with Vercel preview deployments created by the backend (spec section 6). Keeping a dev server alive inside a 30-minute AgentCore turn buys nothing the room can see.
- **Cloud sandbox client: REPLACE**; its shape (upload tree, get URL + token, sync dirty paths, status with `appStatus: starting|running|standby|failed`, `cloud_sandbox_provider.ts:57-78`) is a decent checklist for the JSON `lightbulb app up` returns.
- **Bounded log handling: ADAPT** (`process_output_log_limiter.ts`, `bounded_output_buffer.ts`): cap build-log tails the backend returns to the builder.

## 6. Backend integrations: Supabase and Neon

### Files
- Supabase (Apache): `src/supabase_admin/supabase_management_client.ts` (createSupabaseProject `:1155`, executeSupabaseSql `:989` via `POST /v1/projects/{ref}/database/query`, function deploy `:1309-1475`, logs `:903`, auth redirect config `:809-860`), `supabase_utils.ts`, `supabase_deploy_queue.ts`; prompt rules `src/prompts/supabase_prompt.ts:5-22`.
- Neon (Apache): `src/neon_admin/neon_management_client.ts` (516), `neon_context.ts` (450: `executeNeonSql` `:147`, `executeNeonStatementsInTransaction` `:185`, `getNeonTableSchema` `:331`, `getNeonClientCode` `:394`), `src/ipc/utils/neon_dev_branch.ts` (1,667), `neon_timestamp_utils.ts` (153), `neon_branch_data.ts`, `neon_test_branch.ts`, `migration_utils.ts` (350), `src/ipc/handlers/migration_handlers.ts`, `vercel_neon_sync.ts`; prompt `src/prompts/neon_prompt.ts`, `neon_prompt_rules.ts`.
- Schema diff (MIT): `packages/ts-pg-schema-diff`, `packages/pg-schema-classifier`.
- Credentials (Apache): `src/ipc/utils/secret_storage.ts`, `src/main/settings.ts:411-413, 678-688`.

### How it works
**Credentials.** Provider tokens (GitHub, Vercel, Cloudflare, Supabase and Neon OAuth access/refresh) live in the user's settings JSON as `SecretSchema` values encrypted with Electron `safeStorage` (OS keychain), falling back to `plain:`-prefixed plaintext when encryption is unavailable (`secret_storage.ts:1-30`). Supabase and Neon OAuth go through Dyad's own relay at `oauth.dyad.sh` (`neon_management_client.ts:54`). Everything runs on the user's machine, so the model's tools call provider APIs directly with the user's token.

**Supabase.** Create project, then the agent writes schema with `execute_sql`. RLS policy is a rule, not code: "RLS is mandatory: Enable RLS for every table exposed through Supabase and require appropriate policies for each operation the app uses" (`supabase_prompt.ts:13-14`); "Grants and RLS are separate security layers" (`:7-8`); "Supabase Edge Functions deploy with `verify_jwt: false` ... explicitly verify and decode the caller's JWT" (`:19-20`). Edge functions under `supabase/functions/<name>/` are redeployed automatically whenever a file in them changes, and all of them when `_shared` changes (`response_processor.ts:520-735`). Migrations: SQL runs live first; only if it succeeded and mutates schema (checked by `pg-schema-classifier`) is it also written to `supabase/migrations/NNNN_<description>.sql` (`response_processor.ts:330-345`, `file_utils.ts:69-100`), behind the `enableSupabaseWriteSqlMigration` setting (`schemas.ts:601`). The model is told never to write those files itself (`supabase_prompt.ts:5-6`).

**Neon.** Different and more interesting. The prompt says Dyad keeps no schema-of-record (`neon_prompt.ts:159`): the agent runs SQL on a **development branch**; publishing to production computes a schema diff between the dev and production branches with `ts-pg-schema-diff` and applies it (`migration_utils.ts:122-276`, `migration_handlers.ts`), with `detectDestructiveStatements` (`migration_utils.ts:65-120`: DROP, column type change, plus anything the differ marks destructive) shown to the user before apply, and a stale-plan check on the production branch's `updated_at` (`:38-42`). The dev branch is a **root** branch, not a child of `main`, because restoring a child branch leaks backup branches until the project hits its branch limit (`neon_dev_branch.ts:3-26`, Dyad issue #4943). Test runs use throwaway `dyad-test-*` branches. Neon Auth (managed Better Auth) is the only allowed auth (`neon_prompt_rules.ts:1-2`); the other rules forbid `DATABASE_URL` or `@neondatabase/serverless` in browser code and false RLS claims (`:7-12`).

**What runs where.** In Dyad everything runs on the host: SQL, schema diff, function deploys, env writes to `.env.local` (`app_env_var_utils.ts`), and Vercel env pushes (`vercel_neon_sync.ts:184`, keys `DATABASE_URL`, `NEON_AUTH_BASE_URL`, `NEON_AUTH_COOKIE_SECRET`). The app itself only holds its runtime env.

### Quality
High. The Neon code carries verified-against-live-API reasoning in comments and handles retention-window failures; the Supabase function redeploy is scoped and queued. The coupling is to Dyad's sqlite `apps` table (project ids, branch ids) and settings, not to Electron.

### Verdict for Lightbulb
- **Supabase: skip.** Our starter is Neon only. Keep the RLS/grants/JWT rule texts as reference if a Supabase starter ever appears.
- **Neon management client and dev-branch restore: REPLACE for day one.** Stripe Projects reveals no Neon API key (spec section 7, 29 Oct spike), so branching and point-in-time restore are unavailable to us; we use dump/restore. If Stripe later exposes the key, **ADAPT** `neon_dev_branch.ts` + `neon_timestamp_utils.ts` into the backend: store a database timestamp per published version and restore in place. Read the root-branch comment (`neon_dev_branch.ts:3-26`) before designing any branch scheme; it is the expensive lesson.
- **"No migration files, diff the live branches": REPLACE** with our spec's choice (Drizzle migration files in the repo, `drizzle-kit migrate` in the Vercel build). Ours is better for a headless, credential-free builder: the migration is code the reviewer and rollback can see, and the builder never needs a database URL.
- **`pg-schema-classifier` (MIT): KEEP** as a guard. Run it over every new file in `drizzle/` during `lightbulb app up` / `publish`; a destructive statement (drop, column type change) turns `publish` into an owner card ("this change deletes the `truck` column; 12 rows have values"). Dyad shows the warning in a dialog; we put it on the approval card. It ships as a TS package; either call it from a small Node helper the CLI shells to, or port its patterns to Rust (it is heuristic text matching, a few hundred lines).
- **`detectDestructiveStatements` patterns: ADAPT** together with the above.
- **Prompt rules (`neon_prompt_rules.ts`, the DATABASE_URL/serverless/RLS lines): ADAPT** into the starter's `AGENTS.md`, dropping Neon Auth (our sign-in is `lib/door.ts`).
- **Credential storage: REPLACE.** Workspace: AWS Secrets Manager behind the backend; laptop: `.env` from `stripe projects env --pull` (spec section 6). Dyad's model, where the agent's tools hold provider tokens, is the one our spec rules out.

## 7. Versioning

### Files
`src/ipc/utils/git_utils.ts` (3,277 lines, `dugite`-bundled git: `gitCommit` `:937`, `gitStageToRevert` `:1012`, `gitPush` `:1940`), `src/ipc/handlers/version_handlers.ts` (2,138), `src/ipc/services/version_preview_service.ts`, `git_overlay_workspace.ts`, `src/version_preview/`, `src/github_ops/`, `src/ipc/handlers/github_handlers.ts` (1,723), `src/ipc/services/github_ops_service.ts`.

### How it works
- **One commit per assistant turn.** Agent turns commit at turn end (in the FSL file-operations processor); legacy turns commit in `response_processor.ts:794-830`. The commit hash is stored on the message row, and the next user message is annotated "Previous assistant message created commit: ..." (`local_agent_prompt.ts:141-153`) so the model can inspect history with git tools.
- **Restore is a forward commit.** `gitStageToRevert` refuses to run on a dirty tree, stages the target tree and commits "Reverted all changes back to version <sha>" (`version_handlers.ts:836-846`, `git_utils.ts:1012-1060`). History is never rewritten. A run interrupted mid-turn is first saved as an "[Interrupted]" commit (`version_handlers.ts:811-825`).
- **Database restore only on Neon.** Before reverting, Dyad stores the current DB timestamp on the current version; then restores the dev branch to the target version's timestamp, clears timestamps on newer versions, and reports "including database" only if the restore succeeded (`version_handlers.ts:827-890`). Supabase: code only (functions redeployed from the reverted tree).
- **Version preview** checks out an old commit in an overlay workspace to look at it without changing the branch.
- **GitHub sync.** OAuth device flow; create or pick a repo; push with pull-first, and force or force-with-lease when the user chooses (`github_handlers.ts:837-941`); tokens are scrubbed from remote URLs and error text (`git_remote_token_scrub.ts`, `github_ops_safe_error.ts`).

### Quality
Careful. The "store DB timestamp, then restore, then report honestly" sequence and the dirty-tree refusal are exactly the edge cases a rollback has to get right.

### Verdict for Lightbulb
- **Commit per turn + commit hash in the version record: KEEP the behaviour** (re-implement in Rust; `git` is on every builder). `up` commits any dirty tree with the builder's summary before archiving it, so every preview maps to one commit.
- **Restore as forward commit, never reset: KEEP the behaviour.** `lightbulb app rollback` should create "Rolled back to v3 (<sha>)" on top, so the builder's next edit starts from the rolled-back tree and history stays auditable.
- **Honest "including database" reporting: KEEP** (our spec already says "rolling back further restores code only and says so").
- **DB restore by Neon PITR: REPLACE** with dump/restore (see section 6).
- **GitHub sync: REPLACE / not day one.** Our source of truth is the archive in the workspace's S3 (spec section 8). If GitHub is added, lift `git_remote_token_scrub.ts` (ADAPT).

## 8. Deploy

### Files
- Vercel: `src/ipc/handlers/vercel_handlers.ts` (716), `src/ipc/utils/vercel_utils.ts`, `vercel_neon_sync.ts` (364) + helpers (143), `vercel_project_scope.ts`, `vercel_errors.ts`.
- Cloudflare: `src/cloudflare_deploy/` (`api.ts` 928, `build_config.ts`, `targets.ts`, `convert_prompt.ts`, `fix_prompt.ts`), `src/ipc/handlers/cloudflare_handlers.ts` (1,400).
- Coolify: `src/coolify_deploy/`, `src/coolify_setup/`, `src/ipc/handlers/coolify_handlers.ts`, `src/ipc/utils/ssh_client.ts`.
- Env: `src/ipc/utils/app_env_var_utils.ts` (320).

### How it works
- **Vercel requires GitHub.** `createProject` links `gitRepository: {type: "github", repo}` (`vercel_handlers.ts:298-348`); deployments are `createDeployment` with `gitSource` of the app's GitHub repo and branch (`:379-392`, `:579-590`). No GitHub, no Vercel. Neon env vars are pushed to the Vercel project across targets, with a preview of what will be pushed and trusted-domain reconciliation for Neon Auth (`vercel_neon_sync.ts:118-305`).
- **Cloudflare** also builds from the GitHub repo (Workers Builds), with a deploy rule carrying `NITRO_PRESET=cloudflare_module`; worker secrets via the API (`api.ts:345-361`). On failure the card offers "Fix with AI", which sends the log tail and a diagnosis hint (`fix_prompt.ts:7-53`).
- **Coolify** deploys over SSH to the user's own server.
- **Env handling** on the host: `.env.local` parse/serialise (`app_env_var_utils.ts:144-320`), cookie secret generation (`:206`).

### Verdict for Lightbulb
- **Vercel deploy: REPLACE.** Our backend deploys from files with the Stripe-revealed `VERCEL_TOKEN`/`PROJECT_ID`/`TEAM_ID` and no GitHub (spec section 6). Dyad's code adds nothing to that path.
- **Env sync to Vercel: ADAPT the shape** (preview what keys will be pushed, owned-key list, remove on disconnect: `vercel_neon_sync.ts:26-60`). The owned-key list idea prevents the backend from deleting env vars the customer set by hand.
- **Deploy "fix with AI" prompt: ADAPT** (`fix_prompt.ts`): when a Vercel build fails, `up` returns the log tail plus a one-line diagnosis in `next`, so the builder fixes and retries without asking the room.
- **Cloudflare, Coolify: skip.**

## 9. State machines, distributed machines, MCP, plan handoff, first prompt, tests

- **`src/state_machines/`** (3,992 non-test lines, Apache): a small pure-transition framework (typed events, `{state, commands}` results, keyed hosts, timer leases, a trace). Rationale in `docs/why-state-machines.md`: repeated UI race bugs (a message silently dropped after the input cleared). **Skip**: it solves multi-window renderer races we do not have.
- **`src/distributed_machines/`** (11,486 non-test lines): the IPC transport for those machines, so actors live in Electron main and every window is a remote client (`docs/adr/main-owned-state-machines.md:1-30`, accepted 2026-07-24). Not a cloud runtime. **Skip.**
- **MCP.** Dyad is an MCP *client* for user-configured servers (`src/ipc/utils/mcp_manager.ts`, stdio and HTTP, OAuth, encrypted secrets, consent per tool). It is also an MCP *server* in one place: the Claude Code bridge (`src/ipc/services/claude_code/tool_bridge.ts`, 220 lines) serves Dyad's tool set over a loopback streamable-HTTP MCP endpoint to a `claude -p` child that has `--tools ""`, `--strict-mcp-config`, `--permission-mode manual`, `--allowedTools <dyad tools>`, hooks and plugins disabled, a minimal environment, and `MCP_TOOL_TIMEOUT` at its maximum so human-input tools can wait (`runtime.ts:57-80`, `:348-400`). **ADAPT the hardening flags only**: when Lightbulb itself launches Claude Code (not the person's own interactive session), these flags are the tested recipe for "Claude Code as a model with our tools only". For the laptop path the opposite applies: the person's Claude Code keeps its own tools and calls `lightbulb app`.
- **`plan_handoff/`, `first_prompt/`**: renderer/main sagas (plan approved, open a build chat; home prompt, create app, apply theme, start chat), with resume-after-failure that avoids creating a second app (`first_prompt/state.ts:1-15`). **Skip the code; KEEP the idempotency rule**, which our spec already has (`up` is safe to re-run).
- **`e2e-tests/`** (147 files, 144 Playwright specs) drive the Electron UI against a fake LLM server (`testing/fake-llm-server`) that replays canned responses from `e2e-tests/fixtures/*.md`. They test Dyad's UI; **not reusable** as an eval.
- **`src/testing/chat_flow_harness.ts`** runs the real chat pipeline headless with only `electron` mocked, ~3 s per test (`src/testing/CHAT_FLOW_HARNESS.md`). Good pattern; our equivalent is "run `lightbulb app` against a fake backend".
- **`benchmarks/app-builder/`** (Apache) is the eval harness worth taking. Design (`DESIGN.md` sections 1-5): 3 apps x 3 milestone prompts; PRD-style prompts with pinned routes and `data-testid` contracts so one fixed Playwright journey suite runs unmodified against any model's output; score = 60% journey pass rate + 25% adversarial security probes + 15% LLM judge (capped inputs); cost from wire-captured tokens through a recording proxy; per-cell budget cap ($40) and global kill switch; git tag + database snapshot per milestone so scoring is re-runnable; a Neon simulator (local Postgres + control-plane shim + self-hosted Better Auth) to run offline. Specs for seven apps (`specs/`: curbside, deskhero, ledgerly, portalis, relay-crm, slotline, todo), journeys in `cuj-tests/`, judge rubric in `judge/rubric.md`. Their result (`REPORT.md` section 1): the strongest models score 85-88% at $20-34 per three-milestone app; near-zero cells "are single defects, not incapacity" (e.g. `uuid` columns for opaque session ids). **ADAPT** as Lightbulb's builder eval: same scoring split, our starter instead of theirs, `lightbulb app up` instead of the chat harness, preview URL as the target.

## 10. Code quality and architecture

- **Size.** `src/` is 617,288 lines of TS/TSX at this commit, 1,496 non-test source files and 916 test files. Biggest areas (non-test): `src/ipc` 110k, `src/components` 85k (React UI), `src/pro` 32k, `src/distributed_machines` 11k, `src/hooks` 12k. Prompts are 3.7k. The legacy chat handler alone is 3.8k lines and the agent handler 3.1k.
- **Languages/stack.** TypeScript throughout; Electron Forge + Vite; React + TanStack Router + Jotai + React Query in the renderer; Drizzle over local sqlite (`src/db/schema.ts`, 912 lines); Vercel AI SDK for models; `dugite` for git; Playwright; Vitest.
- **Electron coupling of the parts we care about.** Mostly shallow: `electron-log` (a logger), `readSettings()` and the sqlite `db`. Of the liftable modules, `cloudflare_deploy/` and `worker/proxy_server.js` touch none of these; `tsc.ts`, `codebase.ts`, `dyad_tag_parser.ts`, `migration_utils.ts`, `neon_dev_branch.ts`, `git_utils.ts` each touch one (usually only the logger or a single settings read). The hard couplings are the IPC handler layer (`createTypedHandler`), `safeStorage`, and the app/chat/message rows that thread project ids and branch ids through everything. Pure logic (parsers, diff, classifiers, prompts, tsc runner) lifts into Node in a day; the handler layer does not lift at all.
- **Tests.** Dense: near one test file per source file in the areas read; snapshot tests for prompts; integration tests against fake GitHub/Neon/MCP/LLM servers. The code shows signs of heavy agent authorship (very long explanatory comments, rule files under `rules/`, `AGENTS.md`/`CLAUDE.md` at root) with good discipline about invariants.
- **Architecture verdict.** A desktop product whose valuable logic is spread through one Electron main process. There is no headless service boundary; Dyad itself had to build a test-only harness to run headless, and its proposed cloud design (ADR-0002) is a new system, not a mode of this one. Porting Dyad wholesale to a server would mean rewriting the handler and persistence layers and replacing the FSL loop, i.e. writing a new product. Lifting specific Apache modules is cheap.

## 11. Reuse plan for Lightbulb

Start from Dyad where it is ahead, and keep our own shape where our constraints differ (headless builder, no credentials in the builder, customer-owned accounts, rooms as sign-in). Every lifted file carries the header from section 1, the repo carries Apache-2.0 text and a `NOTICE` line "Includes code derived from Dyad (https://github.com/dyad-sh/dyad), Copyright (c) Dyad contributors, Apache-2.0", and nothing comes from `src/pro`.

### What to lift, and where it lands

| # | From Dyad (commit `0ee4a524`) | Licence | Lands in | Verdict | Change |
|---|---|---|---|---|---|
| 1 | `dyad-sh/nextjs-template` @ `121d5674` (App Router, shadcn `components.json`, `src/components/ui/*`, Tailwind, tsconfig) | Apache | **Starter repo** | ADAPT | Add Drizzle + Neon, `lib/door.ts`, `app.yaml`, `check` script, error reporter; drop tagger and branding; keep its `LICENSE`. |
| 2 | Structure of `scaffold/AI_RULES.md`, the Next.js template's `AI_RULES.md`, `ai_rules_patcher.ts` "Common mistakes" pattern | Apache | **Starter `AGENTS.md`** | ADAPT | Write fresh in our voice; Next.js server-route rules; secrets/door rules from our spec; per-provider rule blocks between markers. |
| 3 | Prompt rules in `src/prompts/local_agent_prompt.ts:99-126, 174-192, 268-331, 532-606` and `system_prompt.ts:458-486` (simplicity, completeness, workflow, edit fallback, build gating, fix-loop rules, blueprint-first) | Apache | **Hermes skill** (and the same text in `AGENTS.md` for laptop builders) | ADAPT | Replace tool names with `lightbulb app` commands; remove Dyad-UI references; blueprint becomes a room message. |
| 4 | `neon_prompt_rules.ts:7-12` (no DATABASE_URL in browser, no serverless driver in browser, no false RLS claims) | Apache | **Starter `AGENTS.md`** | ADAPT | Drop Neon Auth rule. |
| 5 | `tsc.ts:299-494` diagnostic parsing, flag list `:510-545` | Apache | **Rust CLI** (`lightbulb app check`, and `up` pre-flight) | ADAPT (port) | Rust parser over `pnpm run check` output; JSON `{file,line,column,code,message}`. |
| 6 | `packages/pg-schema-classifier` + `migration_utils.ts:43-120` destructive patterns | MIT / Apache | **Rust CLI** (port) or a Node helper the CLI shells to | KEEP / ADAPT | Run over new `drizzle/*.sql` on `up`/`publish`; destructive ⇒ owner card. |
| 7 | Commit-per-turn, revert-as-forward-commit, dirty-tree refusal, honest "including database" (`response_processor.ts:794-830`, `git_utils.ts:1012-1060`, `version_handlers.ts:811-890`) | Apache | **Rust CLI** (`up`, `rollback`) + **backend** (version record) | KEEP (behaviour, re-implemented) | Plain `git` calls; DB side is dump/restore. |
| 8 | `worker/dyad_logs.js:7-16` caps and `dyad-shim.js:177-260` error capture | Apache | **Starter** (`app/_lightbulb/error-reporter.tsx` + `/api/_lightbulb/errors`) | ADAPT | Post to the app's own route, not `window.parent`; backend reads them with Vercel logs for `status --errors`. |
| 9 | `cloudflare_deploy/fix_prompt.ts` pattern | Apache | **Backend** (build-failure response) + **skill** | ADAPT | Vercel build log tail + one-line diagnosis in `next`. |
| 10 | `vercel_neon_sync.ts:26-60` owned-env-key list and push preview | Apache | **Backend** | ADAPT (shape only) | Python; backend owns only the keys it set. |
| 11 | `claude_code/runtime.ts:348-385` hardening flags, `tool_bridge.ts` | Apache | **Backend / Hermes runtime**, only if Lightbulb ever launches Claude Code itself | ADAPT later | Not needed for the laptop path. |
| 12 | `benchmarks/app-builder/` (DESIGN, scoring split, `specs/`, `cuj-tests/` approach, `judge/rubric.md`, recording-proxy cost capture, budget guards) | Apache | **New `evals/apps/` in the monorepo** | ADAPT | Our starter; drive with `lightbulb app up`; journeys against preview URLs; sim of Stripe Projects instead of neon-sim. |
| 13 | `neon_dev_branch.ts`, `neon_timestamp_utils.ts` (root dev branch, PITR restore per commit) | Apache | **Backend**, only if Stripe ever reveals a Neon API key | Parked | Read `neon_dev_branch.ts:3-26` first. |

### What not to take, and why
- **The agent loop, all tools, sub-agents, turbo edits, search-replace, shell safety reviewer, MCP consent** (`src/pro/**`): **FSL, do not reuse.** Also not needed: our builders bring their own loop.
- **The `<dyad-*>` tag protocol and `response_processor`**: built for models that cannot call tools; obsolete even inside Dyad.
- **The runtime (`app_runtime_service`, proxy, ports, Docker, cloud sandbox), preview iframe, state machines, distributed machines, renderer**: desktop/UI machinery with no counterpart in a room.
- **Supabase integration, Cloudflare, Coolify, GitHub-based Vercel deploy, `safeStorage` credentials, `oauth.dyad.sh` relay**: wrong providers, or ruled out by "the builder never holds a credential".

### What to rewrite (no Dyad code fits)
- `lightbulb app new|up|publish|rollback|status|destroy` in the Rust CLI, with `{code, message, next}` errors (spec section 5).
- The backend path behind them: Stripe Projects provisioning, file-based Vercel deploys, Secrets Manager, dump/restore, the app record, Canvas pass minting.
- `lib/door.ts`.

### Where to do better than Dyad
1. **Migrations as code, not live-branch diffs.** Dyad's Neon flow has no schema-of-record (`neon_prompt.ts:159`) and runs SQL from the host. Ours keeps Drizzle migrations in the repo and runs them in the Vercel build, so rollback, review and a credential-free builder all work. Add the classifier gate (row 6) so destructive migrations need the owner.
2. **Verification as one command the CLI enforces.** Dyad spreads verification over FSL tools with caps inside them. Make `lightbulb app check` (type check + build, later tests) the single Verify step in the skill, have `up` run it first, and put the retry cap in the CLI, so every builder (Hermes, Claude Code, Codex) gets the same loop without any of them needing Dyad's tool set.
3. **Runtime errors without an iframe.** Dyad's best runtime-error path needs its proxy and iframe and disappears headless (their own benchmark notes this). The starter's reporter + Vercel logs make the same signal available to an agent in a room and to the person in Canvas.
4. **Rules the builder reads natively.** `AGENTS.md` is read by all our builders; Dyad has to paste `AI_RULES.md` into every prompt. Per-provider sections appended by `up` keep it short.
5. **Approvals on cards, not dialogs.** Dyad's consent prompts (SQL, destructive migrations, shell) are desktop dialogs to the one user. Ours go to the owner's Inbox and wake the builder with a room line, which works when the requester and the payer differ.
6. **An eval before launch.** Dyad measured itself; we should too, with their method (row 12) on our starter, so "the builder can make a crew schedule" is a number, not a demo.
7. **Fewer modes.** Dyad has Build/Ask/Plan/Agent plus blueprint mode. In a room the conversation already does Ask and Plan; we need only the blueprint-first habit and the build loop.

### Open items
- The Next.js template's `pnpm-lock.yaml` and dependency versions are from 27 Oct 2026; re-lock when forking.
- `pg-schema-classifier` targets SQL text; check it against Drizzle-generated SQL (statement breakpoints) before relying on it.
- Whether Stripe Projects can ever reveal a Neon API key decides row 13; ask the rail owner.
