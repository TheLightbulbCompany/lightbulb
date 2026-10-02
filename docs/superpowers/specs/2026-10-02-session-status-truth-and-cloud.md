# Session status: know it, don't poll it — and cover cloud sessions

Research synthesis, 2026-10-02 (five fan-out reports: Claude Code signals, Codex signals, cloud APIs, how other products do it, and a read of our own bridge). Delete when the work ships; PRs are the record.

## 1. What we actually do today (corrected)

| Provider | Status transitions | Discovery, title, cwd | Approvals / questions | Crash |
| --- | --- | --- | --- | --- |
| Claude Code | **Pushed by hooks** (UserPromptSubmit→busy, Stop→idle, PermissionRequest / PreToolUse(AskUserQuestion) / Notification(permission_prompt)→waiting, SessionEnd→ended) within ~1 s | **Polled**: `claude agents --json` every 60 s (a backstop), title from the transcript's `ai-title` row, cwd from the registry | Pushed (hooks) | pid tick (1 s) |
| Codex | **Polled**: rollout JSONL tail every 500 ms (`task_started`/`task_complete`) + SessionStart/UserPromptSubmit/Stop/SessionEnd hooks | Polled: rollout metadata | **None** — Codex never shows "waiting" | app-server socket / pid |
| Cloud (either) | nothing | nothing | nothing | n/a |

The 10 s device heartbeat renews a 30 s `onlineUntil` lease; the backend derives `offline` (lease lapsed) and `ended` (bridge said so). That part is already push-shaped and stays.

So the real problems are: Codex is polled and blind to approvals; Claude's discovery/title/cwd are polled and the VS Code-hosted Claude never fires `Notification`; neither vendor emits a crash signal; cloud sessions are invisible.

## 2. What the vendors push (verified)

**Claude Code 2.1.287.** Hooks cover every live transition: `UserPromptSubmit`, `Stop`, `StopFailure`, `PermissionRequest` (instant; `Notification` lags ~6 s and never fires in the VS Code host), `PreToolUse`/`PostToolUse`/`PostToolUseFailure` (tool running), `PermissionDenied`, `Elicitation`/`ElicitationResult`, `SubagentStart/Stop`, `CwdChanged`, `SessionStart` (with `source`: startup/resume/clear/compact/fork), `SessionEnd` (reasons: clear/resume/logout/prompt_input_exit/other — **no crash reason**). No hook carries a title change; the `statusLine` command fires on every assistant message with `session_name`, `cwd`, model. The registry `~/.claude/sessions/<pid>.json` (status, waitingFor, name, kind, entrypoint, updatedAt) is undocumented but watchable with FSEvents; `claude agents --json` is the documented reader. OTel export carries turn/tool/decision events but **no waiting and no session-end**; it is a cost/usage feed, not a state feed. Remote Control exposes nothing to third parties. The Agent SDK owns its own process and cannot attach to a TUI session. Channels inject into a session but do not report state out.

**Codex 0.159.3.** The app-server daemon socket (`~/.codex/app-server-control/app-server-control.sock`) pushes `thread/status/changed` with `activeFlags` **`waitingOnApproval` / `waitingOnUserInput`**, `turn/started|completed`, `item/started|completed`, `thread/name/updated`, `thread/settings/updated`, `thread/closed`, and the approval/question requests themselves. A second connection can `thread/resume` (no overrides) a live TUI thread and receive its pushes — verified read-only on this Mac on an idle thread; the docs give **no multi-subscriber guarantee**, so an active-turn soak test is required before relying on it. Hooks exist too and we install only four of them: `PermissionRequest` (instant waiting; async for observation), `Interrupt`, `PreToolUse`/`PostToolUse`, `SessionEnd` (archive/close/30 min idle; **no crash**). Codex desktop-app threads and `--no-daemon` TUIs are not live over the daemon (hooks + rollout only). Rollout files are undocumented. OTel: no turn/approval/end events. `notify` fires `agent-turn-complete` only.

**Cloud.** Anthropic **Managed Agents**: full API (list, status idle/running/terminated, event stream with `stop_reason: requires_action`, follow-up, `user.tool_confirmation`, signed webhooks) — but only for agents a workspace creates via the API, not claude.ai/code. **Claude Code cloud** (claude.ai/code, mobile, `--cloud`): no public list/status/transcript API; `claude -p "…" --cloud <id>` queues a follow-up (CLI, claude.ai login); OTel can be pointed at a customer collector via org-managed settings or environment variables; repo `.claude/settings.json` hooks run in single-repo cloud sessions. **Codex cloud**: `codex cloud list --json` and `codex cloud status <id>` under the user's ChatGPT login, poll-only, no transcript, no follow-up, no webhooks; undocumented `/wham/tasks` endpoints exist and are off limits. Of comparable products, only Cursor pushes (terminal states only); Devin and Jules expose "waiting" states but poll-only.

## 3. The model: one state machine, sourced and stamped

Every session row carries `status` (idle | busy | waiting | ended | offline | unknown), `waiting_on` (approval | question | elicitation | none), `ended_reason` (clean | crash | lease | archived), and for each the **source** that proved it (`hook`, `app_server`, `registry_watch`, `pid`, `cloud_poll`, `otel`) plus `observed_at`. Rules:

1. A push source sets state; a poll source only reconciles (it may add a session or correct a missed edge, never flap a fresher push).
2. `waiting` always comes from an approval/question signal, never from silence.
3. `ended` needs either a vendor end event or a dead process (`pid` / socket); silence alone is `offline`, as today.
4. Cloud rows carry `freshness` (seconds since the last poll/event) and show it; they never claim `waiting` unless a vendor said so.

## 4. Build (local, in order of value)

1. **Codex: subscribe, stop tailing for status** (~2 days). Bridge client on the daemon socket: `thread/loaded/list` + `thread/resume` per thread (no overrides), consume `thread/status/changed` → busy / waiting(approval|question) / idle, `thread/name/updated` → title, `thread/closed` → ended. Install `PermissionRequest` (async), `Interrupt`, `PreToolUse`/`PostToolUse` hooks as the fallback for desktop-app and `--no-daemon` threads. Keep the rollout tail for transcript mirroring only. Gate: a soak test with an active TUI turn proving the owning client is undisturbed and the flags arrive.
2. **Claude: finish the hook set, replace the 60 s poll with a watch** (~1 day). Add `PreToolUse`/`PostToolUse`/`PostToolUseFailure`, `StopFailure`, `CwdChanged`, `PermissionDenied`, `Elicitation*` (all `async: true`); FSEvents watch on `~/.claude/sessions/` as the fast discovery/status feed with `claude agents --json` run only on start, wake, and an unknown-session hook (documented fallback); a `statusLine` wrapper for titles if the registry `name` proves stale. Keep the pid tick.
3. **Crash = dead process, explicitly** (small). Mark `ended_reason: crash` when the pid dies with no end event; surface it as "ended unexpectedly" instead of a silent `offline`.

## 5. Cloud (honest scope)

- **Claude Code cloud:** (a) a Lightbulb OTLP ingest endpoint keyed by a per-user token in the URL, with admin instructions for `CLAUDE_CODE_ENABLE_TELEMETRY` + `OTEL_*` in the cloud environment → gives session-active, turn and tool timeline; (b) a repo-level `.claude/settings.json` hook set (SessionStart, UserPromptSubmit, Stop, PermissionRequest, SessionEnd) that POSTs to the same endpoint → gives the real edges incl. waiting, where hooks run (single-repo environments); (c) follow-up via the Mac app running `claude -p --cloud <id>`; (d) identity is the per-user token (Anthropic issues nothing verifiable). Shown as kind `cloud`, with freshness; `ended` is inferred from silence plus a SessionEnd when it fires. ~2 days.
- **Codex cloud:** the Mac app polls `codex cloud list --json` / `status` under the user's login every 15 s while the app is open; rows show title, status, URL, freshness; no follow-up, no waiting. File the OpenAI feature request for a tasks API/webhooks. ~0.5 day.
- **Managed Agents:** not a user-session surface; keep in view only if Lightbulb ever hosts its own agents there (full webhook support exists).

## 6. Decisions needed

1. Codex app-server subscription as the status source (pending the soak test) — yes/no.
2. Claude registry FSEvents watch (undocumented file, documented fallback kept) — yes/no.
3. Cloud scope as in §5, including that "waiting" is unavailable for Codex cloud and that Claude cloud needs org-admin OTEL/hook setup — yes/no.
