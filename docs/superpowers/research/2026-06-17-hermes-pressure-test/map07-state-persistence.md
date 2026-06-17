# Map 07 — Hermes State / Cron / Kanban / Memory Persistence

Basis: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ `4c46c35ed` (branch `basis-v0.14.0-spec`). All file:line references are against this tree.

## TL;DR verdict for the founder

The spec's biggest unverified assumption — "Hermes is fine on EFS, it has WAL fallback" — is **half-true and dangerous**. Hermes *detects* WAL-incompatible filesystems and silently downgrades to `journal_mode=DELETE`, but **DELETE mode does NOT prevent two-writer corruption on EFS/NFS** — it only prevents the WAL-shm crash. Worse, Hermes guards every cross-process lock with `fcntl.flock` (BSD advisory), **not** `fcntl.lockf` (POSIX byte-range) — and `flock` is the exact primitive CLAUDE.md says is a no-op on NFS (the OpenClaw codebase deliberately uses `lockf` for this reason). So the cross-process serialization Hermes relies on (cron `.tick.lock`, memory `.lock`) is silently a no-op on EFS. Hermes is architected for **single-host, multi-process** sharing (one box, gateway + CLI + worktree agents). It is NOT architected for two ECS tasks on one EFS access point — and the whole `HERMES_HOME` is a **single-tenant boundary** with no owner/agent_id attribution anywhere.

## On-disk persistence inventory under HERMES_HOME

`get_hermes_home()` resolves to `$HERMES_HOME` (or `~/.hermes`); `hermes_constants.py:14-30`. Everything below is relative to it:

| File | Format | Writer / lock | Source |
|---|---|---|---|
| `state.db` | SQLite (sessions, messages, FTS5) | WAL→DELETE fallback; `threading.Lock` + `BEGIN IMMEDIATE` + app-level jitter retry (15× / 20–150ms) | `hermes_state.py:34,309-425` |
| `kanban.db` (+ `kanban/boards/<slug>/kanban.db`) | SQLite | WAL→DELETE fallback; `isolation_level=None`, `timeout=30`, `synchronous=NORMAL`, CAS via `tasks.claim_lock` | `hermes_cli/kanban_db.py:282-304,893-939` |
| `cron/jobs.json` | JSON | atomic tmpfile+`os.fsync`+rename; **in-process `threading.Lock` only** | `cron/jobs.py:37-44,430-446` |
| `cron/.tick.lock` | lockfile | **`fcntl.flock(LOCK_EX|LOCK_NB)`** — at-most-once tick guard | `cron/scheduler.py:1684-1699` |
| `cron/output/<job>.*` | text | per-job output | `cron/jobs.py:45,1017` |
| `sessions/<session_id>.jsonl` | JSONL | append/rewrite, **no lock** (legacy transcript, dual-written alongside state.db) | `gateway/session.py:1251-1330` |
| `memories/MEMORY.md`, `memories/USER.md` | Markdown | `os.replace` + **`fcntl.flock` on `<file>.lock`** | `tools/memory_tool.py:57,126-176` |
| `retaindb_queue.db` | SQLite (only if `memory.provider=retaindb`) | plain `sqlite3.connect(timeout=30)` — **NO journal_mode pragma at all**, no WAL fallback | `plugins/memory/retaindb/__init__.py:351,508-511` |
| `config.yaml`, `.env`, `SOUL.md`, `skills/`, `scripts/` | YAML/env/MD | config | `hermes_constants.py:283-294`, `cron/scheduler.py:1284-1304` |

## SQLite-WAL-on-EFS risk verdict (the load-bearing one)

`apply_wal_with_fallback()` (`hermes_state.py:128-161`) tries `PRAGMA journal_mode=WAL`, catches `OperationalError` matching `_WAL_INCOMPAT_MARKERS = ("locking protocol", "not authorized", "disk i/o error")` (`hermes_state.py:54-58`), and falls back to `PRAGMA journal_mode=DELETE` with a one-shot WARNING. Both `state.db` (`hermes_state.py:353`) and `kanban.db` (`hermes_cli/kanban_db.py:928-929`) route through it. **Two ways this bites on EFS:**

1. **The corruption vector survives the fallback.** DELETE-mode (rollback-journal) still relies on SQLite's POSIX byte-range locking via the SQLite VFS — which works on EFS for a *single* connection but provides **no protection against a second ECS task** opening the same file. The memory `project_efs_sqlite_wal_two_task_corruption` documents exactly this for OpenClaw 6.6: any `forceNewDeployment` at ECS default `maxPercent=200%` puts two tasks on one EFS AP and corrupts the SQLite store; the `-shm` single-host assumption is the trigger for WAL, but for DELETE-mode the equivalent two-writer race corrupts the rollback journal / main file. Hermes's fallback only dodges the *WAL crash-at-open*; it does nothing for the **two-writer** problem the spec must actually solve. The spec cannot cite "Hermes has WAL fallback" as the EFS safety story.

2. **`retaindb_queue.db` has no fallback at all** (`plugins/memory/retaindb/__init__.py:351`) — if a user selects the RetainDB memory provider, that DB opens in default journal mode with zero NFS handling and will throw/corrupt on EFS. This is a latent, provider-conditional brick that the spec's "memory just works" line does not cover.

## Cross-process locking is NFS-unsafe (flock, not lockf)

Every cross-process guard in the persistence layer uses **`fcntl.flock`** (BSD whole-file advisory): cron tick (`cron/scheduler.py:1692`), memory read-modify-write (`tools/memory_tool.py:162`). CLAUDE.md explicitly states Isol8/OpenClaw uses `fcntl.lockf()` *because* `flock()` is "incompatible with NFS". On Linux NFS, `flock` is satisfied **locally only** (each client/task thinks it holds the lock) unless the mount sets `local_lock=none` — which EFS does not give you control over. Consequence on EFS with two tasks: cron's "at-most-once" tick guard (`cron/scheduler.py:1673-1714`, which advances `next_run_at` under the lock) **fails open** — both tasks run the same due job, double-firing every cron job; and memory `MEMORY.md`/`USER.md` writes can interleave/clobber. The cron `jobs.json` writer (`cron/jobs.py:44`) only has an *in-process* `threading.Lock` — zero cross-process protection by design.

## Run / session / agent attribution = single-tenant, no owner key

- `sessions` schema (`hermes_state.py:190-222`): identity columns are `source` (`'cli'|'telegram'|'discord'|…`) and `user_id` (the **chat-platform** user, e.g. a Telegram user id) — there is **no** `owner_id`, `tenant_id`, or `agent_id` column. Indexed only on `source` (`hermes_state.py:247`).
- Live attribution at runtime is `gateway/session_context.py` — process-local `contextvars` (`HERMES_SESSION_PLATFORM/CHAT_ID/THREAD_ID/USER_ID/KEY`, `session_context.py:51-78`) with **`os.environ` fallback** for CLI/cron (`session_context.py:148-156`). This is correct *only within one process*; it is the per-task concurrency fix for one gateway, not a multi-tenant boundary.
- Kanban is **explicitly single-board-per-HERMES_HOME by design**: "Profiles intentionally collapse onto a shared board… `hermes -p <profile>` joins the same board as the dispatcher" (`hermes_cli/kanban_db.py:1-18`). CAS atomicity is per-board (`hermes_cli/kanban_db.py:66-67`).
- **The tenant boundary IS `HERMES_HOME` itself.** The only multi-occupant mechanism is *profiles* (`<root>/profiles/<name>`, `hermes_constants.py:80-106,151`), and even those deliberately share one kanban board. So the spec's "one Hermes container per Isol8 owner" maps cleanly to one HERMES_HOME — but it confirms there is **no in-process multi-owner story** and **every per-owner store must be a separate container + separate EFS path**, exactly as the OpenClaw model already does. No attribution code to reuse for per-owner metering/audit; `source`/`user_id` are chat-channel identities, not Isol8 owners.

## Six bullets (return value)

- **EFS/SQLite-WAL verdict: NOT SAFE as the spec implies.** Hermes's `apply_wal_with_fallback` (`hermes_state.py:128-161`) downgrades WAL→DELETE on NFS markers, which dodges the WAL-open crash but does **nothing** against the two-task corruption vector (`project_efs_sqlite_wal_two_task_corruption`); the spec must still enforce single-writer ECS (`maxPercent=100`) and treat `state.db`/`kanban.db` exactly like OpenClaw's WAL DB. Plus `retaindb_queue.db` (`plugins/memory/retaindb/__init__.py:351`) has zero journal-mode handling.
- **Cross-process locks are `fcntl.flock`, which CLAUDE.md says is a no-op on NFS** — cron `.tick.lock` (`cron/scheduler.py:1692`) and memory `.lock` (`tools/memory_tool.py:162`). On EFS with two tasks, cron's at-most-once guard fails open (double-fires jobs) and memory writes can clobber. This is the *opposite* lock primitive from Isol8's intentional `fcntl.lockf`.
- **On-disk surface is larger than `state.db`:** state.db, kanban.db (+per-board copies), `cron/jobs.json` (+`output/`, `.tick.lock`), `sessions/*.jsonl` (legacy transcripts dual-written with no lock — `gateway/session.py:1251-1330`), `memories/MEMORY.md`+`USER.md`, and conditionally `retaindb_queue.db`. Any migration/backup plan must cover all of them, not just SQLite.
- **`cron/jobs.json` has only an in-process `threading.Lock`** (`cron/jobs.py:44`) — atomic tmpfile+rename gives single-host crash safety but **no** cross-process/cross-task safety; concurrent ticks from two tasks can lose writes despite the atomic replace.
- **Attribution is single-tenant with no owner/agent_id key.** `sessions` is keyed on `source`+`user_id` (chat-platform identity, `hermes_state.py:190-222`), kanban deliberately collapses all profiles onto one board (`hermes_cli/kanban_db.py:1-18`), and runtime context is process-local contextvars (`gateway/session_context.py`). There is no metering/audit attribution to reuse — the tenant boundary is the container/HERMES_HOME, same as OpenClaw.
- **Net: the persistence layer assumes single-host, multi-process (one box).** It is safe for one ECS task per EFS path with WAL→DELETE fallback engaged, but the spec cannot claim "WAL fallback handles EFS" — the real requirement is identical to OpenClaw's single-writer-deploy posture, and the spec inherits ALL of the same EFS-SQLite operational scar tissue (single-writer ECS, repair-by-deleting-the-db) **plus** a worse lock primitive (`flock`) and an unguarded RetainDB DB. Any eng-week estimate that doesn't budget for re-doing single-writer ECS + EFS layout for these stores is underscoped.
