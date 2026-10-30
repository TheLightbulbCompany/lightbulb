# `lightbulb app`, the starter, and the builder harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One command, `lightbulb app`, used the same way by a Lightbulb workspace agent and by a developer's own Claude Code or Codex, that creates an app from an embedded starter, checks it, ships previews and versions through Lightbulb's backend, and enforces the builder harness in code: the done gate, state files, fix caps and the destructive-migration gate.

**Architecture:** A new `src/apps/` module in the public Rust CLI (`TheLightbulbCompany/cli`) behind a clap `App` subcommand. It talks only to plan 1's builder routes (`/api/v1/apps/build/*`), authenticating with the workspace hook token when present, else the `lightbulb login` proof; there is no other mode. The starter is Dyad's Next.js template (Apache-2.0) extended with Drizzle, the door, an error reporter, sample-data mode on PGlite, a `check` script and `AGENTS.md`; it is embedded in the binary by a `build.rs` file table. Harness state lives in the app's repo under `.lightbulb/`.

**Tech Stack:** Rust 2021 (clap 4, serde_json, the curl-config transport in `src/login.rs`, `crate::mcp::rest` for signed calls), `git`, `tar`, `pnpm`; starter: Next.js 16 App Router, React 19, Tailwind 4, shadcn, Drizzle ORM + `@neondatabase/serverless` + `@electric-sql/pglite`, `jose`, Vitest, ESLint. CLI repo at `main` (v0.7.x). CI: `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test`.

**Spec:** `docs/superpowers/specs/2026-10-28-apps-design.md` (third draft, approved 29 Oct; §2, §4, §5, §7, Appendix B bind this plan). Backend contract: plan 1 (`2026-10-29-apps-1-backend.md`, "API contract"). Research: `2026-10-29-builder-harness-engineering.md`, `2026-10-29-dyad-source-deep.md` §11, `2026-10-29-dyad-vs-lovable-gaps.md`, `docs/research/2026-10-28-small-software-cli-survey.md`; CLI interfaces digest in the session scratchpad (`cli-digest.md`).

## Global Constraints

- One CLI, one path (founder ruling): every command calls plan 1's `/api/v1/apps/build/*`. Auth: if `OPENCLAW_HOOK_TOKEN`, `ISOL8_API_URL` and `ISOL8_OWNER_ID` are all set → `Authorization: Bearer <hook token>` + `X-Lightbulb-Owner`; otherwise the `lightbulb login` proof via `crate::mcp::rest` (error `not_signed_in` → `next: "run lightbulb login"`). No Stripe or Vercel CLI calls from the CLI, ever.
- Commands: `new`, `up`, `publish`, `rollback`, `status`, `destroy`, `check`, `evidence`, `report-broken-tool` (spec Appendix B plus `evidence` for the done gate). Every command supports `--json` (default when stdout is not a TTY) and fails with `{"code","message","next"}`, exit 1. Output uses real terms (environments, migrations, deployments); translation to plain words is the workspace skill's job (spec §2).
- `app.yaml` keys: `name`, `services` (Stripe catalog names), `open_to` (`room|workspace|invited|public`), `budget_usd`, `kind` (one of the spec §4 kinds), `origin` (`{kind, ref}`). Unknown keys are an error.
- `name` is a DNS label `^[a-z][a-z0-9-]{1,38}[a-z0-9]$`.
- The CLI never prints, logs or stores a credential value; tokens never appear in argv (curl-config-on-stdin, `src/login.rs:92-134`).
- Module name `apps/`, never `app.rs`; no daemon op named `app.*` (hosted assistants own that namespace). Docs say "apps you build" vs "hosted assistants".
- Lifted Dyad files carry `// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, <path>. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.`; the starter carries Dyad's `LICENSE` and a `NOTICE`. The MIT `pg-schema-classifier` rules carry its MIT notice. Nothing from `src/pro`.
- Fix caps: the same error signature twice → `next: "diagnose"`; three times → `next: "undo"` (spec §5).
- The done gate: a preview is reported `done: true` only when, for the current HEAD commit, the deployment is READY, `/api/_lightbulb/health` returned 200, and browser evidence was recorded after that deployment (spec §5).
- Starter dependency versions are pinned exact and current at implementation time (Vercel refuses known-vulnerable Next.js, spike 29 Oct); `.npmrc` sets `minimum-release-age=1440` (Dyad's one-day package age rule).
- The door verifies an EdDSA JWS with claims exactly `{iat, exp, sub, name, email, ws, room, app}` against `LIGHTBULB_ISSUER_KEY` (JWK `{"kty":"OKP","crv":"Ed25519","x","kid"}`), requires `app == LIGHTBULB_APP`, then sets cookie `lb_session` (HS256 with `LIGHTBULB_COOKIE_SECRET`, `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=86400`). `LIGHTBULB_OPEN_TO=public` turns the door off. Every response sends `Content-Security-Policy: frame-ancestors 'self' https://thelightbulb.company https://*.thelightbulb.company`.
- The browser never receives a database credential; data access is server-only (`import "server-only"`).

## File structure

CLI repo (`TheLightbulbCompany/cli`):
- Create `src/apps/mod.rs` (dispatch, config, auth selection, output), `src/apps/api.rs` (builder-route calls), `src/apps/check.rs` (tsc parser, caps), `src/apps/migrations.rs` (destructive classifier), `src/apps/evidence.rs`, `src/apps/starter.rs` (generated table), `build.rs` (walks `starter/` into the table), `starter/**` (the starter), `tests/apps.rs`, `docs/apps.md`.
- Modify `src/main.rs` (variant, HELP section "Apps you build:"), `src/lib.rs`, `src/login.rs` (multipart form option on the curl transport), `docs/cli.md`, `README.md`, `tests/cli.rs`.

**Not in this plan:** pointing at part of a preview to say "change this" (spec §5). Dyad's tagger (Apache) is a webpack plugin and the starter builds with Next.js 16's Turbopack, so it needs porting; it follows once the baseline eval shows where builders lose time.

## Review Focus

1. `up` in a folder that isn't an app: refuse with `not_an_app`, write nothing (Task 3 test `up_outside_an_app_refuses`).
2. `.env*`, `node_modules`, `.next` in the tree: never archived or uploaded (Task 6 test `archive_excludes_env_and_build_dirs`).
3. Evidence recorded for an older commit, then more edits: `up` must report `done: false` again (Task 7 test `evidence_for_an_old_commit_does_not_satisfy_the_gate`).
4. A migration that renames a column: blocked with the SQL shown and `next: "ask the owner"`, unless an approved owner card id is passed (Task 5 test `rename_column_is_destructive`).
5. A pass for another app pasted into this app's session URL: refused, no cookie (Task 9 test `door rejects a pass for another app`).

---

### Task 1: Subcommand, `app.yaml`, auth selection, output

**Files:**
- Create: `src/apps/mod.rs`, `src/apps/api.rs`; Modify: `src/main.rs:9-45` (HELP), `:120-324` (variant), `:411-422` (dispatch pattern), `src/lib.rs`, `src/login.rs` (add `pub(crate) fn request_form(base, path, fields: &[(&str,&str)], file: Option<(&str,&Path)>, headers) -> Result<(u16, Value)>` on the same curl-config transport)
- Test: `tests/apps.rs` (harness `tests/common/mod.rs`; backend stub from `tests/login.rs:18-60`), `tests/cli.rs:119-136`

**Interfaces:**
- Produces: `pub enum AppCommand { New { name: String, room: Option<String>, kind: Kind }, Up, Publish { reason: Option<String> }, Rollback { version: Option<u32> }, Status { errors: bool }, Destroy, Check { publish: bool }, Evidence { kind: EvidenceKind, file: Option<PathBuf>, note: String } }`; `pub fn run(cmd: AppCommand, json: bool) -> lightbulb::Result<i32>`; `pub struct AppConfig { name, services: Vec<String>, open_to: OpenTo, budget_usd: u32, kind: Kind, origin: Origin }` with `load(dir)`/`write(dir)` (hand-rolled parse of the six keys); `pub enum Auth { HookToken { api, owner, token }, Proof }` with `Auth::detect()`; `pub fn call(auth: &Auth, method: &str, path: &str, body: Option<&Value>) -> Result<Value, ApiError>` in `api.rs` (path relative to `/api/v1/apps/build`); `fn fail(json, code, message, next) -> i32`.
- HELP adds:
```
Apps you build:
  app       Build, check, preview, publish and undo apps: app new|up|publish|rollback|status|destroy|check|evidence
```

- [ ] **Step 1: Tests:** `help_lists_app_under_apps_you_build`, `app_yaml_round_trips_the_six_keys_and_rejects_unknown`, `auth_is_hook_token_only_when_all_three_vars_set`, `proof_calls_carry_signed_headers_and_no_token_in_argv` (stub records headers), `bad_name_is_refused`, `not_signed_in_says_run_lightbulb_login`.
- [ ] **Steps 2–5:** `cargo test --test apps` FAIL → implement → PASS (+ `cargo clippy --all-targets -- -D warnings`) → commit `"app: subcommand, app.yaml, one auth path"`.

### Task 2: The starter

**Files:**
- Create `starter/` from `dyad-sh/nextjs-template@121d5674` (Apache; keep its `LICENSE`, add `NOTICE`, header on modified files; remove the component tagger and any "made with Dyad" branding), then add:
  - `app.yaml` (template: `services: [vercel/project, neon/postgres]`, `open_to: room`, `budget_usd: 10`)
  - `drizzle.config.ts`, `src/lib/db/schema.ts`, `src/lib/db/index.ts` (`import "server-only"`; Neon serverless driver when `DATABASE_URL` is set; PGlite in memory when `LIGHTBULB_SAMPLE_DATA=1`, applying `drizzle/*.sql` at first use and then `drizzle/seed.sql`), `drizzle/0000_init.sql`, `drizzle/seed.sql` (the app's sample data, plain SQL: the same file seeds sample mode and the preview database, plan 1 Task 9)
  - `src/lib/door.ts`, `src/middleware.ts`, `src/app/_lightbulb/session/page.tsx`, `src/app/api/_lightbulb/session/route.ts`, `src/app/_lightbulb/closed/page.tsx` (bodies in Task 9)
  - `src/app/api/_lightbulb/health/route.ts` (200 `{ok, commit: process.env.VERCEL_GIT_COMMIT_SHA ?? process.env.LIGHTBULB_COMMIT, db: "live"|"sample"|"none"}`), `src/app/_lightbulb/error-reporter.tsx` + `src/app/api/_lightbulb/errors/route.ts` (client `window.onerror`/`unhandledrejection` → POST to the app's own route, caps 20 args, 8 KB per arg, 64 KB per call, from Dyad `worker/dyad_logs.js:7-16`; the route logs to stdout for Vercel logs)
  - `design/tokens.css` (shadcn CSS variables) and `design/DESIGN.md` (filled from the workspace brand by the builder), `scripts/design-lint.mjs` (flags raw hex/rgb colours and arbitrary Tailwind values outside `design/`; exit 1 with `file:line` list)
  - `package.json` scripts: `dev`, `build` (`next build`), `check` (`tsc --noEmit && eslint . && node scripts/design-lint.mjs`), `check:publish` (`pnpm check && pnpm audit --prod --audit-level high`), `test` (`vitest run`), `db:generate` (`drizzle-kit generate`); all versions pinned exact
  - `.npmrc` (`minimum-release-age=1440`), `.gitignore` (`node_modules`, `.next`, `.env*`, `.vercel`, `.lightbulb/progress.json`)
  - `AGENTS.md` and `CLAUDE.md` (= `@AGENTS.md`): the workflow of spec §5 in our words, structured after Dyad's `AI_RULES.md` (stack, where things go, "the home page must import new components", per-library rules, a "Common mistakes" list), the safety rules (no secret in code, never remove the door, schema only grows, data access server-only), and "run `lightbulb app check`, then `up`; post nothing until `up` says `done: true`"
  - `playbooks/{site,content-site,form,internal-tool,dashboard,portal,payments,automation}.md`: one per kind from spec §4 (builds first, asks, never asks, done when)
  - `.lightbulb/plan.md`, `.lightbulb/lessons.md` (empty headings), `.lightbulb/known-fixes.md` (shipped by us; known failure → fix)
- Create: `build.rs` (embeds every file under `starter/` as `pub const FILES: &[(&str, &[u8])]` via `include_bytes!`), `src/apps/starter.rs`
- Test: inside a scratch copy: `pnpm install && pnpm check && pnpm test && LIGHTBULB_SAMPLE_DATA=1 pnpm build`

- [ ] **Step 1:** Write `starter/src/lib/db/index.test.ts`: `sample mode uses pglite and seeds`, `live mode refuses to load in client code` (import from a `"use client"` module fails type-check via `server-only`).
- [ ] **Step 2:** In a scratch copy: `pnpm test` → FAIL.
- [ ] **Step 3:** Build the starter as listed.
- [ ] **Step 4:** `pnpm install && pnpm check && pnpm test && LIGHTBULB_SAMPLE_DATA=1 pnpm build` → all pass; `cargo build` embeds it (`cargo test --test apps starter_table_contains_agents_md`).
- [ ] **Step 5:** Commit `"starter: Next.js + Drizzle + door, from Dyad's template (Apache-2.0)"`.

### Task 3: `new` and `status`

**Files:** Modify `src/apps/mod.rs`; Test `tests/apps.rs`

**Interfaces:**
- `fn new(name: &str, room: Option<&str>, kind: Kind, json: bool) -> Result<i32>`: refuses a non-empty directory; writes `starter::FILES` with `{{name}}` replaced; writes `app.yaml` (`kind`, `open_to: room` if `room` else `workspace`, `origin: {kind: "ask"}`), copies `playbooks/<kind>.md` to `.lightbulb/plan.md` as the plan's first section; `git init -q`, `git add -A`, `git commit -qm "lightbulb app new {name}"`; prints `{"name","dir","files"}`.
- `fn preflight(json) -> Result<(PathBuf, AppConfig, Auth), i32>` (git root, `app.yaml`, auth; `not_an_app` / `bad_config`).
- `fn status(errors: bool, json)`: `GET /{app_id}/status` (app id from `.lightbulb/app.json`, written by the first `up`); with `--errors`, also `GET /{app_id}/errors` (the backend reads the app's error route logs and Vercel runtime logs; add this route to plan 1 Task 13 if missing).

- [ ] **Step 1: Tests:** `new_writes_the_starter_and_one_commit`, `new_refuses_a_non_empty_dir`, `new_sets_kind_and_plan_from_the_playbook`, `up_outside_an_app_refuses`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"app new, status, pre-flight"`.

### Task 4: `check` and the fix caps

**Files:** Create `src/apps/check.rs`; Test `tests/apps.rs` (fixtures under `tests/fixtures/apps/`)

**Interfaces:**
- `fn check(publish: bool, json) -> Result<i32>`: runs `pnpm run check` (or `check:publish`) with the repo as cwd; parses TypeScript diagnostics into `[{file, line, column, code, message}]` (port of Dyad `src/ipc/utils/tsc.ts:299-494`, header kept), ESLint's and the design lint's `file:line` lines; prints `{ok, problems, next}`.
- Caps: signature = `sha256(code + file + first 80 chars of message)`; `.lightbulb/progress.json` `{"attempts": {"<sig>": n}, "last_check": {...}}`; a failing check increments each problem's count; `next` = `"fix"` (n=1), `"diagnose: list what you tried, find the root cause, consider a stronger model for a fresh plan"` (n=2), `"undo: run lightbulb app rollback or git revert, then tell the room what failed"` (n≥3); a passing check clears the counts for signatures no longer present.

- [ ] **Step 1: Tests:** `parses_tsc_errors_into_file_line_code`, `same_error_twice_says_diagnose`, `same_error_three_times_says_undo`, `passing_check_clears_counts`, `design_lint_raw_hex_is_a_problem`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"app check: parsed problems and fix caps"`.

### Task 5: The destructive-migration gate

**Files:** Create `src/apps/migrations.rs`; Test `tests/apps.rs`

**Interfaces:**
- `fn classify(sql: &str) -> Vec<Finding>` with `Finding { statement: String, kind: "drop_table"|"drop_column"|"rename_table"|"rename_column"|"alter_type"|"set_not_null"|"truncate"|"delete" }`: a port of the MIT `packages/pg-schema-classifier` rules and Dyad `migration_utils.ts:43-120` patterns (MIT and Apache notices kept).
- `fn gate(repo: &Path) -> Result<GateResult>`: classifies every `drizzle/*.sql` not yet recorded in `.lightbulb/migrations.json` (which `publish` updates on success); destructive findings → `{code: "destructive_migration", message: <the SQL>, next: "rewrite add-only (add a column, copy data), or ask the owner and pass --approved <card id>"}`.
- `up` and `publish` call `gate` first and accept `--approved <id>`; the backend verifies the id is an approved Paperclip card for this owner (add to plan 1 Task 12: `approved_destructive` param checked with `shared_admin_client().get_approval`).

- [ ] **Step 1: Tests:** `rename_column_is_destructive`, `add_column_is_not`, `drop_table_lists_the_statement`, `recorded_migrations_are_not_rechecked`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"app: destructive-migration gate"`.

### Task 6: `up`, `publish`, `rollback`, `destroy`

**Files:** Modify `src/apps/mod.rs`, `src/apps/api.rs`; Test `tests/apps.rs` (backend stub)

**Interfaces:**
- `fn archive(root) -> Result<(PathBuf, String)>`: `git add -A && git commit -qm "lightbulb: <summary of changed files>"` if the tree is dirty (one commit per change, Dyad's behaviour), then `git ls-files -z | tar -czf <tmp> --null -T -` from HEAD; refuses > 100 MB; returns (path, HEAD sha).
- `up`: gate (Task 5) → `check` must pass (Task 4) → archive → `POST /source` (multipart via `request_form`) → `POST /up` with `app.yaml` fields → write `.lightbulb/app.json` `{app_id}`; then the done gate (Task 7) decides `done`; prints `UpResult` + `done` + `next`. Exit 0 for `awaiting_approval|provisioning|building|ready`, 1 for `failed`.
- `publish`: gate → `check --publish` → requires review evidence for HEAD (Task 7) → archive → `POST /{id}/publish` → on success records migrations in `.lightbulb/migrations.json`.
- `rollback [n]`: `POST /{id}/rollback`; then `git revert --no-edit` back to that version's commit as a new commit on top (never a reset); prints the backend's `next` ("Data is unchanged…").
- `destroy`: `POST /{id}/destroy`; prints `next`.

- [ ] **Step 1: Tests:** `archive_excludes_env_and_build_dirs`, `up_commits_a_dirty_tree_first`, `up_prints_cards_and_exits_zero_when_waiting`, `up_refuses_when_check_fails`, `publish_requires_review_evidence`, `rollback_is_a_new_commit_not_a_reset`, `no_secret_in_any_output` (stub returns a fake token field; output must not contain it).
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"app up/publish/rollback/destroy"`.

### Task 7: The done gate and `evidence`

**Files:** Create `src/apps/evidence.rs`; Test `tests/apps.rs`

**Interfaces:**
- `lightbulb app evidence --kind browser|review --file <recording or screenshot> --note "<what was checked>"`: records `{kind, commit: HEAD, deployment_id, file, sha256, at}` in `.lightbulb/progress.json` (the file stays local; in a room the skill attaches it to its post). No upload.
- `up` waits client-side: after `POST /up` returns `building`, it re-reads `GET /{id}/status` with backoff 2, 4, 8, 15, 15… s up to 10 minutes (the backend never waits inside a request), printing progress to stderr.
- `fn done(app_id) -> DoneState`: `GET /{id}/status`; done only when the preview deployment is for HEAD and READY, `GET <preview_url>/api/_lightbulb/health` returned 200 with `commit == HEAD`, and a `browser` evidence exists for HEAD recorded after that deployment's ready time. Otherwise `next` names the missing piece ("open <preview_url> in the browser, check the main flow, then lightbulb app evidence --kind browser --file …").
- `publish` requires a `review` evidence for HEAD (the separate reviewer's pass, plan 3).

- [ ] **Step 1: Tests:** `done_needs_ready_health_and_browser_evidence_for_head`, `evidence_for_an_old_commit_does_not_satisfy_the_gate`, `health_commit_mismatch_is_not_done`, `next_names_the_missing_piece`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"app: done gate and evidence"`.

### Task 8: Stuck reports and lessons

**Files:** Modify `src/apps/mod.rs`; Test `tests/apps.rs`

**Interfaces:**
- `lightbulb app report-broken-tool <tool> "<what happened>"`: appends a dated line to `.lightbulb/lessons.md` under "Broken tools". At most one per `up` cycle (tracked in `progress.json`). (A central report route waits until there are users to report.)
- `check` and `up` print, when a problem's signature matches a heading in `.lightbulb/known-fixes.md` or `lessons.md`, that entry under `hint`.

- [ ] **Step 1: Tests:** `known_fix_is_printed_as_hint`, `broken_tool_report_is_rate_limited_to_one_per_cycle`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"app: stuck reports and lesson hints"`.

### Task 9: The door (starter TypeScript)

**Files:** in `starter/`: `src/lib/door.ts`, `src/middleware.ts`, `src/app/_lightbulb/session/page.tsx`, `src/app/api/_lightbulb/session/route.ts`, `src/app/_lightbulb/invite/page.tsx` (exchanges `#t=` at plan 1's `GET /api/v1/apps/invite/{token}` for a pass, then the session route), `src/app/_lightbulb/closed/page.tsx`, `next.config.ts` (the `frame-ancestors` header), `src/lib/door.test.ts`

**Interfaces:**
- `door.ts`: `export type Viewer = { sub; name; email; ws; room; app }`; `verifyPass(token, now?) → Viewer | null` (jose `importJWK` + `jwtVerify`, `algorithms: ["EdDSA"]`, `app` must equal `LIGHTBULB_APP`); `sessionCookie(v) → string` (HS256, 24 h); `viewer() → Viewer | null`; `doorOpen() → boolean` (`LIGHTBULB_OPEN_TO === "public"`).
- `middleware.ts`: matcher everything except `/_lightbulb/*`, `/api/_lightbulb/*`, `/_next/*`; open when `doorOpen()`; else a valid cookie or rewrite to `/_lightbulb/closed` ("Open this from Canvas in Lightbulb.").

- [ ] **Step 1: Tests** (`door.test.ts`, jose-generated Ed25519 key): `accepts a fresh pass for this app`, `rejects an expired pass`, `rejects a pass signed by another key`, `door rejects a pass for another app`, `session cookie round-trips and expires in 24h`, `public mode needs no cookie`, `invite pass names the email`.
- [ ] **Steps 2–5:** FAIL → implement → PASS (`pnpm test`, `pnpm check`) → commit `"starter: the door"`.

### Task 9b: The `content` switch (sites people edit)

**Files:** in `starter/`: `addons/content/**` (Payload 3, MIT: `payload.config.ts` with collections `pages`, `posts`, `media`, `forms`, blocks `hero`, `features`, `cta`, `richText`; plugins SEO, redirects, form builder, search; `@payloadcms/db-postgres` on the app's `DATABASE_URL`; media via `@payloadcms/storage-vercel-blob`; admin at `/admin` signed in through the door, roles editor/publisher mapped from Lightbulb room membership, no second password; `src/app/_lightbulb/edit-overlay.tsx`: for a signed-in editor, an "Edit" button per section linking to its field in `/admin`; SEO defaults: sitemap, robots, canonical, Open Graph); `src/apps/mod.rs` (`new --kind content-site|site` and adding `content` to `services` copy `addons/content` in and run its migration generation)

**Interfaces:** `lightbulb app new <name> --kind content-site` produces an app whose `pnpm check` and `LIGHTBULB_SAMPLE_DATA=1 pnpm build` pass with Payload included; the room asks "change the hero headline" become Payload draft edits through its local API in a script the builder runs (`scripts/content.mjs set <collection> <id> <field> <value> --draft`), published on "ship it".

- [ ] **Step 1: Tests** (in a scratch content-site app): `admin requires the door`, `an editor can save a draft and a publisher can publish it`, `content script sets a draft field`, `sitemap lists published pages only`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"starter: content switch (Payload 3, MIT)"`.

### Task 10: Eval adapter for `lightbulb app`

**Files:** In the monorepo: `evals/apps/src/adapters/lightbulb-app.ts`; Test `evals/apps/src/adapters/lightbulb-app.test.ts`

**Interfaces:** `lightbulbAppAdapter: BuilderAdapter` (plan 0 Task 1 types): runs Claude Code headless in a fresh directory with `lightbulb app new <name> --kind <task.kind>` done first, the starter's `AGENTS.md` as its rules, and the instruction "finish when `lightbulb app up` reports done"; the served URL is the app's preview URL (no local `serve`). Uses a dedicated eval workspace's signed-in proof (`LIGHTBULB_CONNECT_HOME` pointing at an eval login).

- [ ] **Step 1: Test** with a stubbed `lightbulb` binary: `adapter_runs_new_then_returns_the_preview_url`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"evals/apps: lightbulb-app adapter"`; run the suite once and add the result beside the Stage 0 baseline.

### Task 11: Docs and the PR

**Files:** Create `docs/apps.md`; Modify `docs/cli.md` (`### app`, multi-subcommand style of `### memory`), `README.md` (Docs bullets: "Apps you build" → `docs/apps.md`; rename the existing bullet "Hosted assistants (Claude, ChatGPT…)")

- [ ] **Step 1:** Write the docs: what an app is, the eight commands, `app.yaml`, auth (workspace hook token or `lightbulb login`), the done gate, the starter's rules, licences.
- [ ] **Step 2:** `cargo test` (help assertions) green.
- [ ] **Step 3:** Commit `"docs: apps you build"`; open the PR with the pr skill. **Do not merge without the founder.**
