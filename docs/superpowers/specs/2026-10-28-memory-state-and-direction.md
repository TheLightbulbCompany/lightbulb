# Memory: what is built, what it meets, where it goes (28 October 2026)

Status: written for the founder. Every "built" line below comes from the code, not from docs; the full read-only inventories with `path:line` are `docs/research/2026-10-28-inventory-cli-code.md` (cli `main` c0edf5e) and `docs/research/2026-10-28-inventory-workspace-code.md` (`feat/memory-workspace` 83e2edd84, merged as PR #4459). Spec of record: `2026-10-27-memory-layer-design.md` (v5); a v6 amendment follows §3.

## 1. Built: the CLI (cli `main` c0edf5e; v0.6.6 binary, v0.7.0 not released)

| Area | What the code does | Where |
|---|---|---|
| Entries | Signed, per-author-numbered entries: subject, fact, kind (fact / decision / lesson / preference), scope `me` or `team`, `replaces` ids, `when`, source (harness, repo, session). `forget` is a signed tombstone that drops the body. Size cap 16 KB; blank subject refused | `src/memory.rs` (store, replay), `src/store.rs` migrations |
| Contested | Entries grouped by trimmed lowercase subject or a shared `replaces` target; more than one distinct value in a group is contested. Scope, repo and author are ignored, so a `me` value contests a `team` value | `src/memory.rs:337-363` |
| Secrets | Seven regexes (credential label, private-key block, AWS, `sk-`, vendor keys, URL credentials, JWT); run on `remember` (subject and fact) and on the captured tool summary (stored as `[refused: <name>]`); not run on entries accepted from peers | `src/memory.rs:118`, `src/memory.rs:518-535`, `src/capture.rs:92-95`, `src/memory.rs:159-176` |
| Sync | Memory frames gated on a `memory` capability in `hello`; heads per author, paged; `team` to every peer; `me` only to a peer whose sign-in proof carries the same `sub`; forgets go to every peer (a `me` forget leaks only the id) | `src/net/memsync.rs:13-32` |
| Recall | FTS5 `porter unicode61`; 37 stopwords dropped; remaining words quoted, `*` prefix on words of 3+ chars, joined by OR; bm25, no weights, no recency; page order contested, current with history, lessons; MCP `recall` takes only `question`; facts carry the author key, not the name | `src/store.rs:113`, `src/memory.rs:470-506`, `src/mcp.rs:638-643`, `src/daemon/ipc.rs:419-426` |
| Remember | MCP `remember(subject, fact, kind, scope=me, replaces)`; answers `entry_id` plus `neighbours` (an OR search on the subject's words); refusals `team_off`, `secret:<name>`, size; any member may `replace` or `forget` anyone's entry | `src/mcp/tools.json`, `src/daemon/ipc.rs:461-500`, `src/memory.rs:179-180`, `src/memory.rs:311-335` |
| Start block | Built at SessionStart for Claude Code and Codex only; needs `<cwd>/.git` with an `origin` remote (no walk up to the repo root); repo key is the bare origin name without owner; page budget 1,600 characters, the first line that does not fit ends every later section; any forget clears every session's last block | `src/hook.rs:112-118`, `src/hook.rs:201-203`, `src/mcp.rs:237-269`, `src/memory.rs:636-637`, `src/memory.rs:213`, `src/hook.rs:225` |
| Person gate | Daemon walks the caller's ancestry for claude, codex, cursor-agent, grok, grok-native, opencode (+ Cursor's node, `/claude/versions/`, `/app-server-daemon/`). Refused from an agent: memory forget, share, team, drafts, approve, dismiss; every `sessions` command except `upload` with no mode. Open: memory list, show, export, last. Capture writes skip the walk | `src/agents/caller.rs:22-40`, `src/daemon/net_ops.rs:838-846`, `src/daemon/ipc.rs:381-383`, `src/daemon/ipc.rs:899` |
| Capture | Sessions (harness, computer, repo, branch, start, end, tokens) and tool calls (seq, time, tool, duration, ok, summary ≤ 256 chars from the first matching field: command, file_path, pattern, description). Tokens only for Claude Code, from its transcript at SessionEnd; cache tokens not counted. Hook waits ≤ 10 ms. Hidden folders and dot folders directly under HOME skipped | `src/capture.rs:73-92`, `src/capture.rs:265-266`, `src/hook.rs:312`, `src/hook.rs:370-374` |
| Upload | Off by default; `metadata` sends a row per tool call (tool, seq, time, duration, ok) without the summary, `full` adds the summary; `POST /sessions` every 300 s, 200 sessions / 5,000 calls per batch, a session over 5,000 calls is cut; null tokens sent as 0; drafts / approve / dismiss call the backend; `approve` writes a local entry (team with `--team`) | `src/config.rs:72-73`, `src/upload.rs:15-16`, `src/upload.rs:48-93`, `src/upload.rs:151` |
| Defaults | `memory_team` on; `capture_upload` off. `team off` only blocks writing team entries and `share`; syncing and recall of team entries continue | `src/config.rs:72-73`, `src/daemon/ipc.rs:463`, `src/daemon/ipc.rs:939` |

Tests: `memory_store` 15, `memory_sync` 8, `memory_tools` 3, `capture` 12, `upload` 6, plus memory cases in `hooks` (20), `cli` (60), `embed` (21), `mcp_tools` (29). Verified end to end on 28 Oct in two Linux containers on `main`: team sync, `me` stays local, contested, replace with history, forget while offline, share, agent-shell refusals, team-off refusal, capture with tokens, vendor-key refusal.

Not done on the CLI: the v0.7.0 release; the two-Mac CI acceptance; `me` sync between two signed-in computers has unit tests only.

Gaps found by the inventory, for the next CLI PR: walk up to the repo root for the start block; repo key with owner; fill the block per line instead of dropping later sections; contested within a scope; run the secret check on entries accepted from peers.

## 2. Built: the workspace (monorepo `main` after PR #4459; deploys to dev on merge, prod by hand)

| Area | What the code does | Where |
|---|---|---|
| AgentCore | One memory per env shared by tenants (IAM-separated); custom semantic strategy on `us.anthropic.claude-haiku-4-5-20251001-v1:0` with two prompt files; six indexed keys `kind`, `entity`, `attribute`, `status`, `validFrom`, `validTo`; a second memory for session episodes (episodic strategy per actor) | `apps/infra/lib/stacks/container-stack.ts:24-28`, `:85-186`, `:267-299` |
| Recall | Haiku splits a question into ≤ 5 parts (skipped on the agents' internal route); arms: facts (current, contested), turns, entries, private, exact entity; RRF k=60; Cohere `rerank-v3-5` over ≤ 100; order contested, current, turns, entries; budget tokens ≈ chars/4 | `apps/backend/core/services/memory_recall.py:159-229`, `apps/backend/routers/memory.py:192` |
| Sync | Facts → log through the member's `memory.import`; log → `/entries/<o>/` (a `me` on behalf of a member → that person's private scope); idempotent mirrors (clientToken); watermark on the `x-amz-agentcore-memory-updatedAt` system field; background from recall when the last sync is > 60 s old, plus a 300 s sweep behind a per-owner lease; a parked member is never cold-started, ops wait as `MEMOP` rows (30 d, 5 attempts) | `apps/backend/core/services/memory_sync.py:142-152`, `:241-248`, `apps/backend/core/services/memory_api.py:81-139`, `apps/backend/core/services/network_member.py:536-633` |
| Writes | Member route, hosted MCP, plugin channel, internal hook-token routes, one `memory_api`; 422 on empty or > 2,000-char questions; secret pre-check on the member route is keyword-only (a credential-shaped value can be queued for a parked runtime); the plugin scrubs subject and fact before sending | `apps/backend/core/services/memory_api.py:185`, `apps/infra/hermes/plugins/memory/agentcore/__init__.py:376` |
| Sessions | Upload (metadata / full) to S3 under `sessions/{free,workspace}/` with Dynamo rows and `expiresAt`; share by signed link for 7 days, unredacted; delete cascades to the evaluator's marker, events and drafts | `apps/backend/routers/sessions.py`, `apps/backend/core/services/session_store.py:162-184` |
| Evaluator | Full uploads become events (session ids mapped to AgentCore's pattern; a failed ingest deletes the chunks it wrote); reflections become drafts; a draft matching the secret patterns or keywords is a finding (author-only); lessons with evidence in the workspace are mirrored to the workspace list; approve → remember through the member; dismiss drops it | `apps/backend/core/services/evaluator.py:30-50`, `:63-131`, `:181-230` |
| Memory tab | Lists `/owners/<o>/` (extracted team facts) and `/owners/<o>:<u>/` (private); never `/entries/<o>/`, so team log entries and approved lessons do not appear; delete of a mirrored record removes only the search copy (`entry_for` knows imported facts only) | `apps/backend/core/services/workspace_memory.py:384-398`, `apps/backend/routers/memory.py:52-65`, `apps/backend/core/services/memory_sync.py:224-226` |
| Wipes | Member leaves: private scope now, that workspace's sessions and drafts, forgets by every computer key in the NETKEY registry and by `on_behalf_of`, `MEMWIPE` retry row plus wake when parked. Owner wipe: all namespaces, members' private scopes, drafts, sessions; it never tombstones the cloud member's own entries on members' computers | `apps/backend/core/services/workspace_memory.py:219-289`, `apps/backend/core/services/memory_api.py:229-272` |

Tests: memory 143, evaluator 33, sync 39, sessions 20 (backend), 3 frontend; full backend suite 12,633 passed on 28 Oct. Reviewed: one Opus whole-branch review with two fix rounds; three Codex passes with two fix rounds.

Not done: dev deploy smoke, the one-time import, the runtime image roll, the manual delete of old `/users` episodes on dev. Open at deploy time: the override model id, metadata on batch-created records (the comment at `workspace_memory.py:314` says it is dropped; the AWS metadata doc of 28 Oct says it is kept without a strategy id).

Gaps found by the inventory, for the follow-up PR: list `/entries/<o>/` in the tab; the plugin refuses instead of scrubbing; a record→entry link for mirrored records so a tab delete forgets the log entry; the owner wipe tombstones the cloud member's entries on members' computers; credential-shaped values refused before queueing on the member route.

## 3. Measured

Nothing. No benchmark has been run on either engine.

## 4. Direction (founder, 28 October 2026)

1. The free engine gets good at $0 to us with nothing leaving the machine: recall by meaning on the laptop beside the keyword index, a local index of the session's own turns, extraction by the harness through the end-of-turn hook that already makes the agent continue (Claude Code, Codex and Cursor treat a Stop `block` with a reason as "keep going"; OpenCode's plugin sends a new prompt). A local model runtime the person already has is the fallback.
2. BEAM is the target for both tiers, one runner with two backends; the spend is approved.
3. The paid tier sells what needs a server: memory that is up when laptops are closed, extraction from room conversations, consolidation across the team, the evaluator, the Memory tab, usage and timeline, admin wipes.
4. At par or better than Beacon, past.dev and Reason Network: `docs/research/2026-10-28-memory-parity.md`.

Open: local recording of the person's own prompts and replies, on by default or opt-in.

Order of work: dev deploy, runtime roll, import, smoke → follow-up PR (gaps above) → v6 amendment → plan → CLI recall v2 → runner → runs → publish.
