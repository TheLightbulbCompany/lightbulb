# Each app owns its conversation; apps don't belong to rooms — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Each app has its own conversation (beside its preview in Canvas) where its builder works, so apps build in parallel; an app belongs to a workspace, not a room; people in the origin room and any agent or developer using the CLI can talk to the app's builder and change the app.

**Spec:** docs/apps/app-conversation-design.md (approved by the founder 4 Nov 2026, with the condition that room messages and other agents using the CLI can all interact with and shape the app).

**Repos:** monorepo TheLightbulbCompany/lightbulb (backend `apps/backend`, Canvas `apps/frontend`, skill `apps/infra/hermes/skills-src/lightbulb/references/apps.md`); CLI TheLightbulbCompany/cli (`src/apps/`).

## Global Constraints

- The app's conversation is a **hidden channel room** (`kind="channel"`, meta `{"hidden": true, "app": {"app_id": <id>}}`), id deterministic: `uuid5(NAMESPACE, "app-conversation:" + app_id)` with a new fixed namespace constant. Follow `apps/backend/core/services/session_chat.py` (`_create`, `open_chat`, seating, `extra`) — reuse, don't duplicate, shared helpers.
- App record gains `conversation_id` (str | None) and `origin_room_id` (str | None); `room_id` becomes optional. Older apps keep `room_id`; their conversation is created lazily on first open.
- New app state `requested` (reserved by `start`, no source yet). `put_source` for a `requested` app of the same owner and name adopts it.
- Who can talk to an app's builder: anyone who can manage the app (workspace member with `can_publish`/admin), via the conversation, `POST /apps/build/{id}/ask` (CLI `app ask`), or Canvas. Messages relayed from elsewhere are attributed: `"{name} (in #{room}): {text}"` or `"{agent} (via CLI): {text}"`.
- Room lines: origin room gets only the start line and the ready line. Approval, stuck, first-version and ready lines post into the app's conversation. Exact start line: `"Started {display}. It's being built in its own conversation in Canvas: {canvas_url}"`. Exact origin ready line: `"{display} is ready in Canvas: {canvas_url}"`.
- Sharing `open_to` values: `me`, `invited`, `workspace`, `public`, plus legacy `room` (requires `origin_room_id`/`room_id`). Default for new apps without a room: `workspace`.
- Publish gate (`_asked_to_publish`): person callers are never gated; agent callers need a human message asking to publish (same regex) since the app was created, in the app's conversation or its origin room.
- Two writers: `up`/`source` take optional `base_commit`; when it is present and differs from the app's latest source commit, refuse `stale_base` with next `"run lightbulb app pull, merge your change, then lightbulb app up"`. Missing `base_commit` = today's behaviour.
- No new dependencies. In-process tasks keep the existing `drive`/`watch` pattern. Copy is plain and never claims something false. Never `git stash`. Commit trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests: backend `uv run pytest -q --no-cov` on touched suites + ruff; CLI `cargo test --test apps`, `cargo test --lib apps::`, clippy, `cargo fmt --check` (CARGO_TARGET_DIR inside the worktree, deleted after); frontend vitest for canvas + tsc + eslint on touched files.

## Review Focus

1. A person who can open the app but isn't seated in its conversation must still see it in Canvas (seat on open).
2. A relayed room message must reach the builder exactly once (idempotent on the source message id).
3. Two `start` calls for the same name in one workspace: one app, not two.
4. An app with no room at all (CLI-only) must publish, share, and show in Canvas.
5. Existing apps with `room_id` and no conversation keep working unchanged until opened.

---

### Task 1: The app's conversation, start and ask (backend)
Files: new `apps/backend/core/services/app_conversation.py` (or extend `apps_room.py` if small), `apps_service.py`, `routers/apps.py`, `apps_room.py`, tests.
- `open_conversation(auth, app_id)` → create/find the hidden room (seat caller + builder agent), set `conversation_id`. Route `POST /apps/{app_id}/conversation` (Canvas) returns `{room_id}`.
- `start(caller, name, request_text, origin_room_id=None, requested_by=None)` → reserve app (`requested`, `origin_room_id`), conversation, post the request into it attributed to the requester, wake the builder there (builder = calling agent if an agent called, else the workspace default builder used by `apps_room._builder`), return `{app_id, conversation_id, canvas_url, next}` where `next` tells a room agent to post the exact start line in the origin room and end its turn. Builder route `POST /apps/build/start`.
- `ask(caller, app_id, text, origin=None)` → post into the conversation attributed, wake the builder; idempotent on an optional `client_message_id`. Builder route `POST /apps/build/{app_id}/ask` and member route `POST /apps/{app_id}/ask`.
- `put_source` adopts a `requested` app (same owner + name).
- Route approval/stuck/first-version/ready lines to the conversation when `conversation_id` is set; post the start/ready lines to the origin room.
Tests for each, plus Review Focus 2, 3, 5.

### Task 2: Apps don't need a room (backend)
- `_check_room` only when a room is given; `room_id` optional on create/up; `open_to` modes per Global Constraints; `list_apps`/`can_open` by workspace and mode; `_asked_to_publish` per Global Constraints; `_look`'s room-run check uses the conversation (and origin room if set).
Tests: CLI-only app (no room) creates, deploys, publishes as a person, shows in `list_apps` for workspace members; agent publish gated by the conversation; Review Focus 4.

### Task 3: Pull and stale base (backend)
- `GET /apps/build/{app_id}/source` returns the latest source archive (bytes) + its commit; `base_commit` on `/build/source` and `/build/up` per Global Constraints.
Tests: pull returns latest; stale base refused; matching base accepted; missing base unchanged.

### Task 4: CLI — start, ask, pull, no room
Repo CLI, new branch from origin/main.
- `lightbulb app start "<request>" --name <n> [--room <id>]` → calls start, prints/returns its JSON (`--json`) including `next`.
- `lightbulb app ask <name|id> "<text>"` → calls ask.
- `lightbulb app pull <name|id> [dir]` → downloads the latest source into a git working copy (fresh dir or fast-forward an existing app dir) and records its commit as the base.
- `app new` adopts a `requested` app (backend does the adopting; CLI just doesn't fail on the name), `--room` optional everywhere, `up`/source send `base_commit` (the last pulled or up'd commit, recorded in `.lightbulb/app.json`).
Tests for each (stub backend, as `tests/apps.rs` does).

### Task 5: Canvas — conversation beside the preview
- `AppView` shows the app's conversation beside the preview (desktop: side panel; narrow: a tab), reusing `SessionChat`'s parts (`RoomTimeline`, `RoomComposer`, `useRooms` selectRoom/sendMessage) via `POST /apps/{id}/conversation`. "Ask the agent to fix it" posts into the conversation. Register the visible room like SessionChat does so it isn't toasted.
Tests: conversation renders for an app; sending posts to its room; AskAgent targets the conversation.

### Task 6: Skill — room agent hands over; anyone can shape the app
- In a room: an app request → `lightbulb app start` and post its start line, end the turn. A message about an existing app → `lightbulb app ask <app> "<text>"`, one short acknowledgement.
- In an app's conversation: the existing build loop (first version, verify last, three lines).
- Any agent changing an app it didn't start: `lightbulb app pull` first, then `up`; on `stale_base`, pull and merge.
