# Browser Session Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a container browser's logged-in sessions (cookies + origin storage) alive across container restarts/rolls, so an owner who signs into a site once stays signed in.

**Architecture:** Snapshot the browser's *state* (cookies + per-origin localStorage) to a single JSON on the per-user EFS spine on a timer and on clean shutdown; restore it into the browser on launch, before the agent/human uses it. This mirrors Browserbase "Contexts" (persist state, not the whole profile dir) and deliberately does NOT put the Chrome `user-data-dir` (LevelDB/SQLite) on EFS — that reintroduces the `database is locked` SQLite-on-NFS class the fleet already hit (O6). Restore happens via CDP injection at browser-launch time in the OpenClaw browser tool.

**Tech Stack:** OpenClaw fork `extensions/browser` (TypeScript, Playwright/CDP), Isol8 backend `core/containers` (Python), EFS per-user access point, existing `browser.request` gateway RPC.

**Spec:** this plan is self-contained; design derived from the 2026-08-20 session investigation (see memory `project_container_browser_profile_persistence_parked`) and Browserbase Contexts docs.

## Global Constraints

- **Never move the Chrome `user-data-dir` onto EFS/NFS.** It stays on `/tmp` (`container-stack.ts` symlinks `~/.openclaw/browser` → `/tmp/openclaw-browser`). SQLite/LevelDB write+lock traffic on NFSv4.1 wedges the Node event loop (`rpc_wait_bit_killable`) — this is the O6 hazard.
- **Persist state only, never the HTTP cache** (images/CSS/JS/fonts). Cookies + localStorage (+ optionally IndexedDB) only — keep the snapshot small.
- **The snapshot is plaintext on EFS behind KMS-at-rest + per-user access-point UID isolation** — same security posture as the existing `pre_stage_auth_profile_store` OAuth seed (`workspace.py:815`). Do not add in-file encryption (the container can't decrypt at boot).
- **EFS path is owner-scoped:** the access point chroots `/users/{owner_id}` → `/home/node/.openclaw`, so the snapshot lives at `/home/node/.openclaw/browser-state/state.json` in-container = `<EFS>/users/{owner_id}/browser-state/state.json` backend-side.
- **Restore must complete BEFORE the browser serves its first navigation** or the site sees a logged-out browser.
- All fork changes ship via the fork release → extended-image roll → `openclaw-version.json` pin (heavier than an isol8-repo PR). Isol8-repo-only changes (config, backend) ship via the normal backend/CDK pipeline.

---

### Task 1: Snapshot writer in the browser tool (fork)

**Files:**
- Create: `extensions/browser/src/browser/session-state-store.ts`
- Test: `extensions/browser/src/browser/session-state-store.test.ts`

**Interfaces:**
- Produces: `snapshotSessionState(cdp: CdpClient, outPath: string): Promise<{cookies: number; origins: number}>` — reads cookies via CDP `Storage.getCookies` (all browser contexts) and per-origin localStorage via `Runtime.evaluate` over the open pages' origins, writes `{version:1, savedAt, cookies:[...], origins:[{origin, localStorage:{...}}]}` atomically (write tmp + rename). Returns counts.
- Produces: `SESSION_STATE_VERSION = 1`.

- [ ] **Step 1: Write the failing test** — assert `snapshotSessionState` writes a v1 JSON with the cookies returned by a mocked `Storage.getCookies` and the localStorage from a mocked `Runtime.evaluate`, via a tmp-then-rename (assert no partial file on a mid-write throw).
- [ ] **Step 2: Run it, verify it fails** (`snapshotSessionState is not a function`).
- [ ] **Step 3: Implement** — CDP `Storage.getCookies` for cookies; for each distinct page origin, `Runtime.evaluate('Object.entries(localStorage)')`; assemble the v1 shape; atomic write (`fs.writeFile(tmp)` + `fs.rename`). Skip `chrome://`/`about:` origins.
- [ ] **Step 4: Run test, verify pass.**
- [ ] **Step 5: Commit** (`feat(browser): session-state snapshot writer`).

### Task 2: Restore reader in the browser tool (fork)

**Files:**
- Modify: `extensions/browser/src/browser/session-state-store.ts`
- Test: `extensions/browser/src/browser/session-state-store.test.ts`

**Interfaces:**
- Consumes: `SESSION_STATE_VERSION`, the v1 file shape from Task 1.
- Produces: `restoreSessionState(cdp: CdpClient, inPath: string): Promise<{cookies:number; origins:number} | null>` — returns null if the file is absent or a version mismatch; otherwise `Network.setCookies` (or `Storage.setCookies`) for the cookies and, per origin, sets localStorage via an `addScriptToEvaluateOnNewDocument`-style injection or a direct `Runtime.evaluate` after navigating a throwaway tab to the origin. MUST be idempotent and tolerate individual cookie failures (log + continue).

- [ ] **Step 1: Write the failing test** — v1 file in → asserts `Storage.setCookies` called with the parsed cookies and localStorage set per origin; absent file → returns null; wrong `version` → returns null (no CDP calls).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement**, per-item try/catch so one bad cookie doesn't abort restore.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** (`feat(browser): session-state restore reader`).

### Task 3: Wire restore into browser launch (fork)

**Files:**
- Modify: `extensions/browser/src/browser/server-context.availability.ts` (the `ensureBrowserAvailableOnce` / `launchManagedChrome` path — around the post-launch, pre-first-navigation point; verify exact function from `launchOpenClawChrome` at `chrome.ts:~978`)
- Test: `extensions/browser/src/browser/server-context.availability.test.ts` (extend existing if present)

**Interfaces:**
- Consumes: `restoreSessionState` (Task 2). Path from Task 5's config key (fallback to the fixed `~/.openclaw/browser-state/state.json`).

- [ ] **Step 1: Write the failing test** — asserts that a fresh managed-browser launch calls `restoreSessionState` exactly once, before the browser is reported "available", and that a null return (no snapshot) does not block launch.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — after Chrome is up + CDP reachable but before `ensureBrowserAvailableOnce` returns success, call `restoreSessionState`; swallow errors (log) so a bad snapshot never blocks the browser. Gate on the config flag from Task 5.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** (`feat(browser): restore session state on managed-chrome launch`).

### Task 4: Periodic + shutdown snapshot trigger (fork)

**Files:**
- Modify: `extensions/browser/src/browser/session-tab-cleanup.ts` (reuse the existing sweep scheduler pattern) OR a new small scheduler beside it; and the browser shutdown/close path (`chrome.ts:~1409` `Browser.close`).
- Test: alongside.

**Interfaces:**
- Consumes: `snapshotSessionState` (Task 1).

- [ ] **Step 1: Write the failing test** — asserts a snapshot fires on the interval while a browser is running, and once on clean shutdown; asserts it does NOT fire when no browser is running (no wake-spam).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — a timer (default 60s; config-driven) that snapshots while the managed browser is up, plus a best-effort snapshot in the close path. Fargate task kills are NOT graceful, so the timer is the primary durability mechanism; the shutdown hook is best-effort only.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** (`feat(browser): periodic + shutdown session-state snapshots`).

### Task 5: Config surface (fork schema + isol8 config generator)

**Files:**
- Modify (fork): `src/config/types.browser.ts`, `src/config/zod-schema.root-shape.ts` — add `browser.sessionState?: { enabled?: boolean; intervalSeconds?: number; path?: string }`.
- Modify (isol8): `apps/backend/core/containers/config.py` browser block (~line 1935) — emit `sessionState: {enabled: true, intervalSeconds: 60, path: "~/.openclaw/browser-state/state.json"}`.
- Test (isol8): `apps/backend/tests/unit/containers/test_config_tier1b.py` — assert the block is emitted.

**Interfaces:**
- Produces: the `browser.sessionState` config consumed by Tasks 3-4.

- [ ] **Step 1: Write the failing isol8 test** — `_cfg()["browser"]["sessionState"]["enabled"] is True` and `path` endswith `browser-state/state.json`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the fork schema fields + the isol8 config emission.
- [ ] **Step 4: Run isol8 container tests, verify pass.**
- [ ] **Step 5: Commit** (`feat(container): browser.sessionState config`).

### Task 6: EFS path provisioning + boot ordering (isol8 infra)

**Files:**
- Modify: `apps/infra/lib/stacks/container-stack.ts` boot command — ensure `~/.openclaw/browser-state/` exists on EFS (it's the durable spine, NOT the /tmp symlink), owned by the access-point uid, created before the gateway starts.
- Test: `apps/infra/test/container-stack.test.ts`.

**Interfaces:**
- Consumes: the path from Task 5.

- [ ] **Step 1: Write the failing test** — the boot command mkdirs `/home/node/.openclaw/browser-state` (durable EFS path, not `/tmp/...`) and chowns it, before the gateway exec.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the mkdir/chown in the boot chain (mirror the existing XDG-dir creation at `workspace.py:202` / the boot script).
- [ ] **Step 4: Run infra tests, verify pass.**
- [ ] **Step 5: Commit** (`feat(container): provision browser-state EFS dir`).

### Task 7: End-to-end verification (manual, documented)

**Files:** none (verification runbook appended to PR description).

- [ ] Roll a dev container onto the built image + config. Sign into a test site in the Browser panel. Force a task replacement (admin apply-image-update or `force_redeploy_user_service`). Confirm the session survives (still logged in) after the new task boots. Confirm `du -h` of `browser-state/state.json` is small (KB-MB, not the full profile). Confirm no `database is locked` warnings appear (proves we didn't touch the SQLite-on-EFS path).

## Self-Review

- **Coverage:** snapshot (T1,T4) + restore (T2,T3) + config (T5) + EFS path (T6) + verify (T7) — the full loop. ✓
- **Placeholders:** none — each task names exact files (verified this session) and concrete CDP methods.
- **Type consistency:** `snapshotSessionState`/`restoreSessionState`/`SESSION_STATE_VERSION` used consistently across T1-T4; `browser.sessionState` config shape consistent T5→T3/T4.
- **Open risk to flag to reviewers:** IndexedDB is out of scope v1 (cookies + localStorage cover most logins incl. the Google-session case); add IndexedDB only if a target site needs it. The OAuth `auth_profile_store` (Chrome-side SQLite, separate subsystem) already has its own persistence via the litestream boot chain — this plan does NOT touch it.
