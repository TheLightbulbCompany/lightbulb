# Chat sessions + composer controls — design

- **Date:** 2026-07-01
- **Status:** Draft (design approved; pending spec review)
- **Scope:** A focused increment on the current `/chat` surface. Not the 3-home IA
  redesign (`docs/superpowers/specs/2026-06-21-three-home-ia/`), though it is a
  building block that redesign reuses.

## Problem

Today Isol8's chat is **one session per (agent, user)** — the backend hardcodes the
OpenClaw session key as `agent:<agentId>:<userId>` (`apps/backend/routers/websocket_chat.py:1523`).
There is no way to hold more than one conversation with an agent, so **users create a
brand-new agent for every one-off task** — the wrong mental model. Separately, the
composer has no per-run **model** or **reasoning-effort** control, even though OpenClaw
supports both per session.

## Goals

1. **Sessions:** one agent → many sessions. Users start a fresh session per task, and
   switch/rename/reset sessions, from the chat surface.
2. **Composer controls:** a model + reasoning-effort picker on the chat bar, persisted
   per session, correct per provider.

## Non-goals

- The `/chat` + `/teams` unification (3-home redesign) — separate project.
- A curated per-agent model allowlist (`agents.defaults.models`) — deferred; the picker
  simply shows the owner's provider models.
- A new Isol8 "sessions" table — OpenClaw's session store remains the source of record.

## Decisions (locked with the user)

| Decision | Choice |
|---|---|
| Scope | Focused increment on current `/chat` |
| Session-of-record | OpenClaw's session store (no new Isol8 table) |
| Model picker | Owner's provider models via `models.list`, no allowlist |
| Session placement (UI) | Nested under each agent in the sidebar |
| Chat-bar layout | One combined **model · effort** pill → sectioned menu |

All findings below are verified against OpenClaw source at tag `v2026.6.10-isol8.1`
(two adversarial verification workflows) and current Isol8 `main`.

## Session model & key

- Session key: **`agent:<agentId>:<userId>:<opaqueSessionId>`**, **constructed
  server-side**. The client sends only the trailing `opaqueSessionId`; the backend
  prepends the `agentId` and the **authenticated `userId`** (from the JWT). The client
  can never influence the `userId`/`agentId` segments.
- **`opaqueSessionId` = `crypto.randomUUID()`** (RFC-4122 v4, lowercase hex + hyphens).
  Rationale: OpenClaw lowercases the *entire* key before store and lookup
  (`session-key-utils.ts:206`), so a mixed-case id (nanoid/base58) would alias-merge two
  sessions under the case-fold. Lowercase UUID is byte-stable and collision-free.
- **Server-side validation (load-bearing security guard):** before building the key, the
  backend rejects any trailing segment that is not a strict lowercase UUID
  (`/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/`). This blocks
  colon injection (`:thread:` / `:subagent:` child-key forks) and reserved-token abuse
  (`:heartbeat` → OpenClaw reaps the session; `group`/`channel`/`direct`/`dashboard` →
  misclassification).
- **Legacy session preserved:** the existing `agent:<agentId>:<userId>` conversation is
  surfaced as a normal "General" session so no history is orphaned.
- **New session:** the client mints a fresh UUID; the first `chat.send` materializes the
  session (OpenClaw implicit-create — no mandatory `sessions.create`). "New session" may
  also use `sessions.reset {reason:"new"}`.

Verified: `parseAgentSessionKey` accepts `parts.length ≥ 3` with no upper bound
(`session-key-utils.ts:240`), classifies as `'direct'` (`classify-session-kind.ts:39`),
store is `Record<fullKey, SessionEntry>`; max key length 512 (`primitives.ts:25`, ours
~80–110); no reserved collision for `user_`/`org_` + UUID.

## Backend changes

1. **Write-path symmetry (load-bearing).** Thread the opaque `sessionId` through the
   `agent_chat` / `agent_abort` / `agent_steer` envelopes and construct the 4-part key
   server-side, replacing the hardcoded `agent:<agentId>:<userId>` at
   `websocket_chat.py:1523` (send), `:1779` (abort), `:1884` (steer). Today the read path
   (`chat.history`) already carries a session name but the write path ignores it — so a
   fresh session's messages would pile into the old one while its view stays empty.
   **Message send stays on the gated `agent_chat` path** (the subscription/credit gate
   `gate_chat` lives only there, `:1604`) — do **not** reroute through `sessions.send`.
2. **`connection_pool._parse_session_key` (load-bearing).** Recognize the 4-segment key →
   `source="webchat"` with `member_id` + `agent_id` (`connection_pool.py:2102-2196`), and
   confirm `is_isolated_session` does not treat it as isolated. Otherwise a 4-part key
   falls to `source="unknown"` → isolated → streamed `chunk/done/error` frames are
   **silently dropped** (`:1204/:1219/:1416`) and `member_id` (org fan-out + billing) is
   lost.
3. **Event-frame session id (load-bearing).** Add the originating `sessionId` to each
   forwarded frame in `connection_pool._handle_message` (~`:1291-1313`, which today carries
   only `agent_id` + `runId`) so the client can demux streaming events per session (needed
   once one agent has many sessions / background tabs).
4. **`sessions.list` ownership filter (load-bearing).** In the backend proxy, return only
   rows whose parsed `rest` begins with the caller's `userId`, excluding
   `paperclip:*`/`cron:*`/`channel`/`subagent` and other members' sessions — `sessions.list`
   otherwise returns **every** session on the shared per-owner container and `kind`
   collapses them all to `'direct'`. Apply the same ownership check to
   `sessions.patch/reset/delete` (they have no server-side guard).
5. **Auto-prune defense.** Set `session.maintenance.mode: "warn"` in
   `apps/backend/core/containers/config.py` (OpenClaw defaults to 500 entries / 30d /
   `enforce`; heavy Paperclip users exceed 500 and silently evict old user chats). `warn`
   never deletes.

## Frontend changes

- **Sidebar:** nested session list under each agent (from the filtered `sessions.list`,
  with `includeDerivedTitles`/`includeLastMessage`, bounded `limit`) + "New session".
- **Routing:** send the active `sessionId` on chat/abort/steer; demux streaming events by
  `sessionId`; `chat.history` already keys by session.
- **Chat bar — combined model · effort pill** → sectioned menu (Model / Reasoning /
  Speed), persisted via **`sessions.patch {model | thinkingLevel | fastMode}` through the
  backend operator connection** (OpenClaw rejects session mutations from webchat
  connections; the backend connects as operator). Rules:
  - **Model:** from `models.list` scoped to the active agent; pass exact `provider` + `id`
    separately and rebuild `${provider}/${id}` (replaces `ModelSelector.extractProvider`,
    which splits on `.` and breaks on Bedrock AIP ARNs). Surface `entry.available` +
    keep the "model access denied" retry. Patch **only when the session is idle** (a
    mid-run model switch throws `LiveSessionModelSwitchError`).
  - **Reasoning/effort (`thinkingLevel`):** hide/disable on non-reasoning models (it
    otherwise silently clamps to `off`).
  - **Fast-mode (Speed):** shown **only for BYO-OpenAI** (gate on the owner's
    `provider_choice`). Hidden for Bedrock (never reads it), BYO-Anthropic (config-sourced,
    inert per-session), and ChatGPT-OAuth (no `fastMode` reference in the responses path —
    cosmetic).

## Error handling

- Model catalog discovery timeout/empty (`models.list` does per-agent discovery, ~750ms)
  → fall back to the agent's default model; never show "no models".
- Invalid model passed to `sessions.patch`/`create` → clean error, keep prior model.
- Mid-run model switch → disabled in the UI while a run is active.

## Testing

- **Backend:** server-side key construction; trailing-segment UUID validation rejects
  colons + reserved tokens + non-UUID + uppercase; `_parse_session_key` 4-part →
  not-isolated + `member_id` preserved; `sessions.list` ownership filter drops
  paperclip/cron/other-member rows; `gate_chat` still fires on send.
- **Frontend:** create/switch session loads the right transcript; streaming events demux
  across two concurrent sessions; chat-bar persistence via `sessions.patch`; provider
  gating (effort hidden on non-reasoning; fast-mode only BYO-OpenAI); idle-only model
  switch.

## Phasing (suggested)

1. **Backend session-key plumbing** (write-path symmetry + parse + event frames + list
   filter + maintenance) — nothing user-visible yet; the single-session app keeps working.
2. **Sidebar sessions UI** (list + new + switch) on top of (1).
3. **Composer model · effort pill** (`sessions.patch` + provider gating).

## References

- Verification: two adversarial workflows over OpenClaw `v2026.6.10-isol8.1` +
  Isol8 `main` (session RPC support, routing, per-session overrides, model catalog,
  lifecycle, proxy/scope; then key-format legality + uniqueness/normalization).
- Related: 3-home IA redesign (`2026-06-21-three-home-ia/`); overflow fix
  (`contextPruning`, PR #1115).
