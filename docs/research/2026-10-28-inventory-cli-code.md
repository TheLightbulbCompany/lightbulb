# CLI memory, from the code (cli main c0edf5e, 28 October 2026)

Read-only inventory of src/ and tests/ only; no docs read. Every line cites path:line.

# Lightbulb CLI: memory, sync, recall, capture, upload. Code inventory

Repo `/Users/prasiddhaparthsarthy/Desktop/lightbulb-network.nosync`, `main` at `c0edf5e`, crate `lightbulb` 0.6.6 (`Cargo.toml:3`), rusqlite 0.40 bundled (`Cargo.toml:29`). Only `src/`, `tests/` and `Cargo.toml` were read. Paths are relative to the repo. Tests are named as `file::fn`.

---

## 1. Memory entries

### Storage (SQLite, `src/store.rs`)
- Migrations run forward only. Twelve exist (V1–V12), one transaction each (`src/store.rs:7`, `src/store.rs:248-254`). A db from a newer build is refused (`src/store.rs:236-242`).
- **V9 `memory_entries`** (`src/store.rs:124-149`): `rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, author TEXT, seq INTEGER, entry TEXT, received_at INTEGER, tombstone INTEGER DEFAULT 0, UNIQUE(author, seq)`. `entry` holds the whole signed entry as JSON.
- `memory_tombstones (target TEXT PRIMARY KEY)` (`src/store.rs:129`).
- FTS triggers `memory_ai` / `memory_ad` / `memory_au` index `json_extract(entry,'$.op.subject')` and `'$.op.fact'` (`src/store.rs:133-146`).
- View `memory_heads` = `SELECT author, MAX(seq) ... GROUP BY author` (`src/store.rs:147-148`).
- **V10 `memory_last (session TEXT PRIMARY KEY, block TEXT, at INTEGER)`** (`src/store.rs:92-93`).
- **V12** drops `memory_fts` and recreates it with `tokenize='porter unicode61'`, rebuilt from the entries (`src/store.rs:110-117`). It also adds `memory_seen (author TEXT PRIMARY KEY, seq INTEGER NOT NULL)` (`src/store.rs:118`). Test: `memory_store::an_older_index_is_rebuilt_stemmed`.
- `memory_import_keys (key, id)` is created lazily by `memory.import` on an instance only, not by a migration (`src/daemon/ipc.rs:782-790`).

### Entry struct (`src/memory.rs`)
- `Entry { v: u32, id, author, seq: u64, op: Op, at: i64, sig }` (`src/memory.rs:64-74`). `v` is always `1` (`src/memory.rs:134`).
- `Op` is tagged `t`, either `remember` or `forget` (`src/memory.rs:46-62`):
  - `Remember { subject, fact, kind, scope, when: Option<i64>, source: Source, replaces: Vec<String> }`
  - `Forget { target }`
- `Source { harness, repo?, session?, on_behalf_of?, shared_from? }` (`src/memory.rs:35-44`).
- **Kinds:** `fact | decision | lesson | preference` (`src/memory.rs:19-26`).
- **Scopes:** `me | team` (`src/memory.rs:28-33`). The default is `me` in the daemon (`src/daemon/ipc.rs:449-452`) and in the tool schema (`src/mcp/tools.json:78`).

### Ids, signing, numbering
- `id` = lowercase hex SHA-256 of the canonical unsigned entry with `id` blanked (`src/memory.rs:81-85`). `sig` = identity signature over the entry value (`src/memory.rs:147-148`).
- `seq` = 1 + MAX over `memory_heads` and `memory_seen` for the author (`src/memory.rs:127-132`). A seq past `i64::MAX` is refused with `memory seq out of range` (`src/memory.rs:138-142`). Writes take an IMMEDIATE transaction (`src/memory.rs:124`). Test: `memory_store::remember_assigns_increasing_seq_and_verifies`.

### Limits and refusals
- Size: canonical entry > `MAX_ENTRY_BYTES` = 16 KiB is refused as `too_long` (`src/roster.rs:19`, `src/memory.rs:149-151`). A peer's oversize entry is refused too (`src/memory.rs:160-162`). Test: `memory_store::entry_over_16kb_is_refused`.
- Blank subject: `subject.trim().is_empty()` gives `no_subject`. It is checked in `memory::remember` (`src/memory.rs:115-117`) and earlier in the daemon (`src/daemon/ipc.rs:460-462`). Test: `cli::remember_refuses_a_blank_subject`.
- Secrets are refused, never scrubbed. They are checked on subject, then fact (`src/memory.rs:118-120`).

### Secret patterns (`src/memory.rs:518-536`), verbatim
| name | regex |
|---|---|
| `credential` | `(?i)(api[_-]?key\|secret\|token\|password)\s*[:=]\s*\S{8,}` |
| `private_key` | `-----BEGIN [A-Z ]*PRIVATE KEY-----` |
| `aws_key` | `AKIA[0-9A-Z]{16}` |
| `api_key` | `sk-[A-Za-z0-9_-]{20,}` |
| `vendor_key` | `\b(?:[sr]k_(?:live\|test)_[A-Za-z0-9]{16,}\|gh[pousr]_[A-Za-z0-9]{30,}\|github_pat_[A-Za-z0-9_]{20,}\|xox[abprs]-[A-Za-z0-9-]{10,}\|AIza[0-9A-Za-z_-]{35}\|glpat-[A-Za-z0-9_-]{20})` |
| `url_credentials` | `https?://[^\s/]+:[^\s@]+@` |
| `jwt` | `eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}` |

(`\|` above is table escaping for `|`.)

Where the patterns are applied:
- **remember** (subject and fact): `src/memory.rs:118`. Tests: `memory_store::refuse_secret_matches_the_seven_patterns`, `memory_store::remember_refuses_secret_with_reason`, `memory_tools::remember_refuses_secret_with_reason`.
- **memory.import** goes through `memory::remember`, so it is covered (`src/daemon/ipc.rs:803`).
- **capture**: on the tool-call summary text (`tool: detail`), before truncation. The summary is stored as `[refused: <name>]` (`src/capture.rs:92-95`). Test: `capture::test_capture_refuses_secret_in_summary`.
- **Not applied** to a peer's entry in `accept` (`src/memory.rs:159-176`), to capture session fields (repo, branch), or to the upload body.

### replaces / forget / contested
- `replaces` is a list of entry ids carried on the new `remember`. Replay treats any id named by any live entry's `replaces` as `Replaced` (`src/memory.rs:311-314`, `src/memory.rs:332-335`). The replacer does not have to be the target's author, and nothing checks authorship.
- History = everything replaced, followed transitively, oldest first (`src/memory.rs:365-381`). Test: `memory_store::replace_chain_yields_one_current_with_history`.
- **Contested**: unreplaced entries are union-found by key `s:<trimmed lowercase subject>` and by each `t:<replaced target>` (`src/memory.rs:321-344`). A group is contested when it has more than one member and more than one distinct value (`src/memory.rs:356-363`). Grouping ignores scope, repo and author, so a `me` and a `team` entry with the same subject and different values contest each other. Two identical values do not contest. Tests: `memory_store::two_replaces_of_same_target_are_contested`, `memory_tools::test_remember_conflict_without_replaces_is_contested`.
- **Forget** is its own signed entry, `Op::Forget{target}`. On insert (`src/memory.rs:201-214`):
  - the target id goes into `memory_tombstones`;
  - the target row's body is NULLed with `tombstone=1` (only when that row is a `remember`);
  - **all of `memory_last` is deleted**.
- A body that arrives after its forget is stored without text (`src/memory.rs:189-199`).
- Any member's forget applies, not only the author's (`src/memory.rs:179-180`). A forget of a forget does nothing (`src/memory.rs:206`).
- Tests: `memory_store::test_tombstone_before_entry_wins`, `memory_store::non_author_forget_before_body_wins`, `memory_store::forget_of_known_entry_by_other_member_applies`, `cli::memory_forget_clears_the_last_block`.
- Accepting a peer's entry requires a valid signature, the expected id, and an author who is a current roster member (`src/memory.rs:163-171`). Test: `memory_sync::test_removed_member_entries_refused`.

---

## 2. Sync

- **Frames:** `Frame::MemoryHeads { heads: BTreeMap<author, seq> }` and `Frame::Memory { entries, more }` (`src/net/frames.rs:115-122`). The wire tags are `memory_heads` and `memory` (`src/net/frames.rs:225-226`).
- **Capability gate:** `Hello.memory: bool`, serde default false, skipped when false (`src/net/frames.rs:109-111`). This build always sends `memory: true` (`src/net/mod.rs:1437`). On receiving a hello, `l.memory = memory` and `send_memory_heads = memory` (`src/net/mod.rs:1121-1125`).
- Heads go out only if the peer's hello said `memory` (`src/net/mod.rs:1170`). Incoming `MemoryHeads` / `Memory` are handled only when `speaks_memory`, meaning the link said memory and the protocol is Ok (`src/net/mod.rs:557-559`, `src/net/mod.rs:1179`, `src/net/mod.rs:1206`).
- **Older peer:** gets no memory frames. The enum has no unknown-variant tolerance, so this gate keeps an old build reading (`src/net/frames.rs:115-116`). Mail is unaffected. Test: `memory_sync::old_peer_without_capability_gets_no_memory_frames_and_keeps_mail`.
- **Heads and paging (`src/net/memsync.rs:38-84`):**
  - For each author we hold, send entries after the peer's head, using `memory::since(author, after, PAGE_ENTRIES)`.
  - Bounds: `PAGE_ENTRIES = 500` and `PAGE_BYTES = 512 KiB` (`src/net/mod.rs:47-48`). A page closes before the next entry would pass `PAGE_BYTES` (`src/net/memsync.rs:67-70`). `more` is true on a full batch.
  - A short batch ends the stream, because seqs have gaps (`src/net/memsync.rs:62-63`).
  - Tombstoned bodies are skipped. Their `Forget` is in the stream (`src/memory.rs:251-257`).
  - Test: `memory_sync::gap_of_300_entries_pages`.
- **Push loop:** checks heads each second and pushes to every memory link when ours changed (`src/net/mod.rs:790-821`). A new link is told at its hello (`src/net/mod.rs:805-807`). Tests: `memory_sync::entry_written_on_live_link_arrives_without_reconnect`, `memory_sync::me_latest_on_both_sides_does_not_ping_pong`.
- **`me` rule:**
  - `sendable` lets a `remember` with `scope: me` go only when `same_person`. `Forget` entries always go, including forgets of `me` entries (`src/net/memsync.rs:13-22`). Unit test: `net::memsync::tests::me_goes_only_to_the_same_person`.
  - `same_person` (`src/net/memsync.rs:26-32`): our stored sign-in proof verifies for our key, the peer's `hello` proof verifies for its key (kept in `d.proofs`, `src/net/mod.rs:1101-1106`), and both `sub` values are equal. Both computers must be signed in. Test: `memory_sync::test_me_scope_syncs_only_same_person`.
  - What a `me` filter held back counts as covered and "is not owed again" on that link (`src/net/memsync.rs:36-37`, `src/net/mod.rs:814`, `src/net/mod.rs:1197-1200`).
- **Seq bounds on a peer claim (`memory::saw`):** a peer's head for our own key is stored in `memory_seen` unless `seq >= i64::MAX/2` or `seq > ours + MAX_LOST`, where `MAX_LOST = 1_000_000` (`src/memory.rs:220-240`). An ignored claim is logged once per daemon (`src/net/mod.rs:1184-1192`). Tests: `memory_store::an_absurd_seen_head_is_ignored`, `memory_sync::a_restored_node_numbers_past_what_peers_hold`.
- A dropped entry is logged without its text (`src/net/memsync.rs:88-95`).

---

## 3. Recall

- **FTS table:** `CREATE VIRTUAL TABLE memory_fts USING fts5(subject, fact, content='memory_entries', content_rowid='rowid', tokenize='porter unicode61')` (`src/store.rs:112-114`).
- **Query build (`src/memory.rs:480-503`):**
  - Split on whitespace and strip `"` from each word.
  - Drop 37 stopwords, matched in lowercase (`src/memory.rs:470-474`): how do does we i you the a an of to in on is are was were be it that this what when where which who why before after with for and or not our my your.
  - Each word is quoted. A word of 3 or more chars gets a `*` prefix.
  - Words are joined with **` OR `**.
  - If every word was a stopword, the whole question is matched as one quoted phrase. An empty question returns nothing.
  - Tests: `memory_store::search_matches_natural_questions`, `memory_store::a_stopword_question_matches_as_a_phrase`, `memory_store::search_finds_by_subject_and_fact`.
- **Ranking:** `ORDER BY bm25(memory_fts)` with default (equal) column weights and no recency term (`src/memory.rs:504-507`). Results are then mapped through replay. Replaced matches are dropped by the caller (`src/daemon/ipc.rs:665`).
- **`memory.recall` (`src/daemon/ipc.rs:643-679`):**
  - Default budget is `budget_chars` 1600 (`src/daemon/ipc.rs:648`).
  - An empty question returns all facts (`memory::facts`, newest first, `src/memory.rs:391-401`). With `repo` set, the results are filtered to `source.repo == repo` and team facts are put first (stable sort, `src/daemon/ipc.rs:651-657`).
  - A non-empty question uses `search(q, 50)` (`src/daemon/ipc.rs:660`). Recall covers both scopes.
- **Page composition (`memory::page`, `src/memory.rs:561-643`):**
  - Sections come in order: `Contested:` (grouped by subject and joined with ` vs `), then `Current:` (every kind except lesson), then `Lessons:`.
  - Line formats: `subject: value (who, day; was <old>, <who>, <day>)` and `value (who, day)` for each contested side.
  - The budget is in chars. The **first line that does not fit stops all later sections** (`break 'sections`, `src/memory.rs:636-637`).
  - Authors are shown by member name, else the first 12 chars of the key (`src/memory.rs:567-572`).
  - Test: `memory_tools::recall_page_puts_contested_first_and_respects_budget`.
- **What the MCP `recall` tool returns:**
  - The daemon reply minus `id` and `ok` (`src/mcp.rs:645-650`): `page`, `block`, and `facts[]`.
  - Each fact has `{id, subject, value, kind, scope, author, when, status (current|contested), history[{id,value,when}]}` (`src/daemon/ipc.rs:419-426`). `author` here is the raw member key, not a name. Only `memory_manage` maps names (`src/daemon/ipc.rs:885-889`).
  - The only declared input is `question` (`src/mcp/tools.json:57-66`). The MCP server forwards declared props only, plus `session` (`src/mcp.rs:638-643`). So an agent cannot set `budget_chars` or `repo`.

---

## 4. Remember

- **Tool schema:** `subject`, `fact`, `kind` are required; `scope` (default `me`) and `replaces[]` are optional (`src/mcp/tools.json:68-83`). `session` is stamped by the MCP server, and undeclared args like `when` or `source` are dropped (`src/mcp.rs:638-643`). Test: `mcp_tools::remember_through_mcp_stamps_the_session_and_ignores_undeclared_args`.
- **Returns** `{stored, entry_id?, neighbours, refused?}` (`src/daemon/ipc.rs:505-523`).
  - `neighbours` = up to 5 `search(subject)` hits, computed **before** the store, with replaced ones removed (`src/daemon/ipc.rs:500-505`).
  - They are full fact JSON: id, value, scope, status, history.
- **Refusals:**
  - `team_off`: scope team while `config.memory_team` is false gives `{stored:false, refused:"team_off"}` with no neighbours (`src/daemon/ipc.rs:463-465`). Test: `cli::memory_team_off_refuses_team_scope`.
  - `secret:<name>` and `too_long` give `{stored:false, refused, neighbours}` (`src/daemon/ipc.rs:512-518`).
  - `no_subject` is an error (`bad_request` with `reason: no_subject`, `src/daemon/ipc.rs:461`, `src/daemon/ipc.rs:415-417`). Through MCP it surfaces as `code(r["error"])`, which is `bad_request`, not `no_subject` (`src/mcp.rs:652`).
- **Source:**
  - `harness` is the live session's provider name, or `unknown`. `repo` is the session row's repo (`src/daemon/ipc.rs:468-492`).
  - `on_behalf_of` is honoured **only on an instance** (embedded/workspace), where `harness: "workspace"` (`src/daemon/ipc.rs:475-490`). Test: `embed::cloud_remember_signs_as_workspace_with_name_in_source`.
  - A **person's** terminal may pass its own `source` (used by `memory approve`) (`src/daemon/ipc.rs:470-473`).
  - There is no key field on remember. `key` exists only on the instance-only `memory.import` (`src/daemon/ipc.rs:727-826`, `src/daemon/ipc.rs:183`).
- No person check exists on `memory.remember` (`src/daemon/ipc.rs:390-392`). Agents may write.

---

## 5. Session-start block

- **Produced only on `SessionStart`, and only when the session's cwd yields a repo** (`src/hook.rs:220-235`).
  - The hook calls `memory.recall {question:"", repo, budget_chars:1600, session:key}` (`src/hook.rs:225`). A failure means no block, and the mail still goes (`src/hook.rs:223`). Test: `hooks::a_failing_memory_recall_still_hands_over_the_mail`.
  - The block is appended after the framed mail, joined with a blank line (`src/hook.rs:236-240`).
  - It is delivered as `hookSpecificOutput.additionalContext` (`src/hook.rs:120-130`).
- **Which harnesses:** `takes_mail` is false at SessionStart for `cursor`, `grok` and `opencode`, which only take mail on PostToolUse/Stop (`src/hook.rs:112-118`). The hook returns before the block is built (`src/hook.rs:201-203`). **Only Claude Code and Codex get the block.** A Claude hook fired by Cursor does nothing (`src/hook.rs:145-150`).
  - Tests: `hooks::session_start_includes_memory_block_for_repo` (Claude) and `hooks::user_prompt_submit_never_includes_memory`.
- **Repo choice:** `mcp::git(cwd)` (`src/hook.rs:187`, `src/mcp.rs:233-275`).
  - It reads `<cwd>/.git` only, whether a dir or a `gitdir:` file. It does not walk up to parent folders.
  - The repo is the **last path segment of `[remote "origin"] url`**, with `.git` stripped. The owner is not included. No origin means no repo, which means no block.
  - `facts(repo)` matches `source.repo` exactly (`src/memory.rs:396`).
- **Budget:** 1600 chars (`src/hook.rs:225`, `src/daemon/ipc.rs:648`). It applies to the page only. The heading and instruction lines are added on top (`src/memory.rs:554-559`). Test: `hooks::test_session_block_caps_at_1600_chars`.
- **Template** (`src/memory.rs:550-559`):
  ```
  Memory (team and yours):
  <page>
  Call `recall` when a task needs something a teammate may know; call `remember` when you finish a task or are told to remember. Personal unless the person says to share.
  ```
  An empty page gives an empty block.
- **Kept for `memory last`:** stored in `memory_last` only when `repo` and `session` are both given and the block is non-empty (`src/daemon/ipc.rs:667-674`).
  - `memory last` reads the calling session's row, else the most recent row (`src/store.rs:363-375`).
  - The CLI passes the session of the agent above it, else none (`src/cli.rs:315-323`, `src/cli.rs:592-595`). Test: `cli::memory_last_prints_block`.
- **What clears it:** any accepted `Forget`, local or synced, deletes every row (`src/memory.rs:212-213`).

---

## 6. Person-only gate

- **Daemon classification:** `Caller::is_person()` = `Socket(Some(pid))` and `person_only(pid)` (`src/daemon/ipc.rs:158-160`).
  - `person_only` walks from the socket peer pid up to pid 1. It requires every step to resolve and none to be an agent, so it fails closed (`src/daemon/net_ops.rs:834-846`).
  - An instance caller (embedded bridge) is treated as a person for the ops that take `for_a_person` (`src/daemon/ipc.rs:222`).
- **Agent executables** (`classify`, `src/agents/caller.rs:22-40`):
  - by name: `claude`, `codex`, `cursor-agent`, `grok`, `grok-native`, `opencode`, `opencode.exe`;
  - `node` under `/cursor-agent/`;
  - any path containing `/claude/versions/` or `/app-server-daemon/`.
  - The Cursor editor app (`/Cursor.app/`) is *not* classified as an agent. `is_cursor` is used only for hook dedup (`src/agents/caller.rs:116-119`).
  - Test: `agents::caller::tests::classify_table`. Daemon test: `daemon::net_ops::tests::person_only_accepts_people_and_refuses_agents`.
- **CLI-side** `walked_to_a_person` skips sh/bash/zsh/fish/dash/env and fails on pid ≤ 1 (`src/agents/caller.rs:14`, `src/agents/caller.rs:60-71`). The code says it stops accidents, not a hostile same-uid agent (`src/agents/caller.rs:58-59`).
- **Debug pin:** `LIGHTBULB_CONNECT_HOOK_CALLER=<pid>:<birth_micros>` makes that process count as an agent. It exists in debug builds only and is `None` in release (`src/hook.rs:18-21`, `src/hook.rs:425-432`, `src/agents/caller.rs:90-112`).
- **Requires a person:**
  - `memory.forget` and `memory.share` (`src/daemon/ipc.rs:899-901`);
  - `memory.team` (`src/daemon/ipc.rs:712-715`);
  - `sessions.list`, `sessions.show`, `sessions.forget`, `sessions.export` (`src/daemon/ipc.rs:383`, `src/daemon/ipc.rs:606`);
  - `sessions.upload` (`src/daemon/ipc.rs:695-698`);
  - `person`, used by `memory drafts/approve/dismiss` (`src/daemon/ipc.rs:388-389`);
  - `send` as a person (`src/daemon/ipc.rs:295-297`);
  - `team.join` needs a resolvable actor (`src/daemon/ipc.rs:333-335`).
- **Agent-allowed:**
  - `memory.remember`, `memory.recall` (`src/daemon/ipc.rs:390-393`);
  - `memory.list`, `memory.show`, `memory.last`, `memory.export`, which have no check (`src/daemon/ipc.rs:399-400`, `src/daemon/ipc.rs:899`);
  - `capture.*`. Capture skips the walk on purpose, because it is slower than the hook wait (`src/daemon/ipc.rs:381-383`).
- **Instance-only:** `memory.import`, `memory.forget_author`, and `memory.list {since_seq}` (`src/daemon/ipc.rs:181-183`, `src/daemon/ipc.rs:218-221`).
- Tests: `cli::memory_share_from_agent_shell_is_refused`, `cli::sessions_upload_from_agent_shell_is_refused`.

---

## 7. Capture

- **Tables (V11, `src/store.rs:94-105`):**
  - `capture_sessions(id PK, harness, computer, repo, branch, started, ended, prompt_tokens, completion_tokens)`;
  - `capture_tool_calls(session_id, seq, at, tool, summary, duration_ms, ok, PK(session_id, seq))`.
- **Per session:** id = `<harness>:<conversation>` (`src/daemon/ipc.rs:534-537`). `computer` comes from `config.computer`, and repo/branch from the hook's `git(cwd)`. `started` is the daemon's now. A restart reopens the row, setting `ended=NULL` and updating repo/branch (`src/capture.rs:57-68`, `src/daemon/ipc.rs:551-566`).
- **Per tool call:**
  - `tool` comes from `tool_name|toolName|tool` and `args` from `tool_input|args`, as capped JSON. `duration_ms` comes from `payload.duration`, and `ok = tool_response.is_error != true` (`src/hook.rs:331-344`).
  - The hook caps args to top-level string fields only, each cut to 256 chars, at most 4 KiB total (`src/hook.rs:301-322`).
  - The daemon stores a `summary` = `tool: <first non-empty of command|file_path|filePath|path|pattern|url|query|description>`, whitespace collapsed, cut to 200 chars (`SUMMARY_MAX`). If the text matches a secret pattern, `[refused: <pattern>]` is stored instead (`src/capture.rs:12-13`, `src/capture.rs:70-96`).
  - Seq = max + 1 per session. Nothing is stored if the session has no row (`src/capture.rs:100-118`).
  - A session already running gets a row at its next tool call (`src/hook.rs:347-356`). Test: `capture::a_running_session_is_captured_from_its_next_tool_call`.
- **Hook wait bound:** `CAPTURE_WAIT = 10 ms` per answer on a separate connection. Past it, one stderr line is printed and the rest of the event's capture is skipped. Failures are silent (`src/hook.rs:248-287`). Tests: `capture::a_slow_capture_write_does_not_hold_the_hook`, `capture::test_capture_write_failure_is_silent`.
- **Skipped folders:**
  - a dot-folder directly under `$HOME` (e.g. `~/.config`) (`src/capture.rs:39-53`);
  - folders turned off with `lightbulb folder <path> off` (`presence::hidden`, `src/daemon/ipc.rs:540-545`, `src/daemon/ipc.rs:566`).
  - A repo's own dot folders (`.claude/worktrees`) *are* captured (`src/capture.rs:40-41`). Test: `capture::hidden_folder_is_not_captured`.
- **Token source:**
  - **Stop** reads only `payload.usage.input_tokens/output_tokens` and overwrites the row via COALESCE, not summed (`src/hook.rs:357-365`, `src/capture.rs:121-136`).
  - **SessionEnd** reads the Claude Code transcript only. Usage is deduped per `message.id` and only `input_tokens` and `output_tokens` are summed; cache tokens are not counted. A transcript over 50 MiB, or with no usage, gives null (`src/hook.rs:367-378`, `src/capture.rs:236-275`). Other harnesses get null at the end.
  - Tests: `capture::tokens_are_read_at_session_end_never_at_stop`, `capture::a_stop_without_usage_leaves_tokens_null`, `capture::an_oversized_transcript_yields_null_tokens`.
- Reads (`sessions.show` / `sessions.export`) include summaries (`src/daemon/ipc.rs:614`, `src/daemon/ipc.rs:620`). `sessions.forget` deletes the row and its calls (`src/capture.rs:226-234`). Test: `capture::forget_deletes_only_the_matching_session`.

---

## 8. Upload

- **Modes** `off | metadata | full` (`src/upload.rs:20`). The default is `off` (`src/config.rs:73`).
- `set_mode` writes the `upload.mark` watermark as now when turning on, so only sessions finishing from then go. It deletes the watermark when turning off (`src/upload.rs:33-42`).
- **When it runs:** a daemon task every **300 s** (`src/upload.rs:45-65`, spawned at `src/daemon/mod.rs:292`). Debug override: `LIGHTBULB_CONNECT_DEBUG_UPLOAD_MS` (`src/upload.rs:18`).
- **Skips** when the mode is off/unknown, or when signed out or the sign-in has ended. It makes no request in those cases (`src/upload.rs:99-106`). Test: `upload::nothing_uploads_when_signed_out`, `upload::test_upload_mode_off_stops_uploads`.
- **Batch:** ended sessions whose `started` or `ended` is after the mark (`src/upload.rs:119-122`, `src/capture.rs:164-172`), at most 200 sessions and 5000 calls. A single session over 5000 calls is truncated (`src/upload.rs:15-16`, `src/upload.rs:123-134`).
- **Body:** `{mode, sessions[], tool_calls[]}` (`src/upload.rs:146-150`).
  - session: `id, harness, computer, repo ("" if none), branch ("" if none), started (ISO), ended (ISO), prompt_tokens (0 if null), completion_tokens (0 if null)` (`src/upload.rs:75-82`)
  - tool_call: `session_id, seq, at (ISO), tool, duration_ms (0 if null), ok`. **`full` adds `summary`** (`src/upload.rs:84-93`).
  - **`metadata` still sends every tool name and its timing.** Test: `upload::upload_metadata_omits_summaries`, `upload::upload_full_sends_summaries_and_only_once`.
- **Endpoint:** `POST /sessions`, label `upload` (`src/upload.rs:151`). The watermark moves only after a 2xx, and not if the mode was turned off during the request (`src/upload.rs:161-165`). Test: `upload::an_upload_off_during_the_request_keeps_the_mark_gone`.
  - A 401 logs "sign in again". Other failures log once and retry next tick (`src/upload.rs:56-61`, `src/upload.rs:151-160`).
- **Signing (`mcp::rest`, `src/mcp.rs:461-511`):**
  - Headers: `X-Lightbulb-Proof` (the stored sign-in proof), `X-Lightbulb-Challenge` (base64url of `"<now> <METHOD> <base-path><path> <sha256(body)> <label>"`), and `X-Lightbulb-Sig` (the identity's `tools` proof signature).
  - Signed out gives `not_signed_in` (`src/mcp.rs:468-473`). Test: `upload::uploader_signs_batches`.
- **Drafts** (`src/cli.rs:680-822`). Every subcommand first asks the daemon `person`, so agents are refused (`src/cli.rs:685-688`). The calls use the `memory` label.
  - `GET /memory/drafts` returns `{drafts:[{id, kind, text, evidence[]}]}`. The CLI lists id (8 chars), kind, the first line of the text, and the evidence count (`src/cli.rs:717-751`).
  - `POST /memory/drafts/<id>/dismiss`, no body (`src/cli.rs:770-782`).
  - `POST /memory/drafts/<id>/approve`, body `{"scope": "team" | null}` (`src/cli.rs:784-794`). An id prefix must match exactly one draft (`src/cli.rs:752-768`). A 409 means "settled somewhere else" (`src/cli.rs:702`).
  - `approve --team` with team memory off is refused **before** any server call (`src/cli.rs:689-698`). Test: `cli::memory_approve_team_with_team_memory_off_calls_no_server`.
- **What `approve` does locally:**
  - If the server's reply has `subject` and `fact`, the CLI calls `memory.remember {subject, fact, kind, scope}` with `source {harness:"cloud-evaluator", repo:null, session: evidence[0]}` (`src/cli.rs:795-822`).
  - Without them ("workspace"), the entry is expected to arrive over the network (`src/cli.rs:795-799`).
  - If the local store refuses, the CLI prints the lesson text and exits 1 (`src/cli.rs:818-822`).
  - Tests: `cli::memory_approve_stores_locally_for_solo_user`, `cli::memory_approve_in_a_workspace_waits_for_the_network`, `cli::memory_approve_prints_the_lesson_when_the_local_store_refuses`, `cli::memory_dismiss_settles_the_draft`, `cli::memory_drafts_lists_the_proposed_ones`.

---

## 9. Registered CLI commands (clap, `src/main.rs`)

- **`memory`** (`src/main.rs:54-96`, dispatch `src/main.rs:399-410`):

  | subcommand | from an agent shell |
  |---|---|
  | `list [--team \| --me] [--repo R]` | allowed |
  | `show <id>` | allowed |
  | `forget <id>` | **refused** |
  | `share <id>` | **refused** |
  | `export [--json]` | allowed |
  | `last` | allowed |
  | `drafts` | **refused** |
  | `approve <id> [--team]` | **refused** |
  | `dismiss <id>` | **refused** |
  | `team on\|off` | **refused** |

- **`sessions`** (`src/main.rs:98-114`, dispatch `src/main.rs:411-417`):

  | subcommand | from an agent shell |
  |---|---|
  | `list` | **refused** |
  | `show <id>` | **refused** |
  | `forget <id>` | **refused** |
  | `export` | **refused** |
  | `upload <mode>` | **refused** |
  | `upload` with no mode | allowed: it reads config locally and prints (`src/cli.rs:840-856`) |

- There is no `remember` or `recall` CLI command. These are MCP tools or raw IPC only.

---

## 10. Hooks and tools per harness

All five harnesses get all five events: SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd (`src/hook.rs:36-42`). Each also gets an MCP server entry that runs `lightbulb mcp`.

| harness | config written | timeouts | notes |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` hooks; MCP in `~/.claude.json` `mcpServers`; skill under `~/.claude` (`src/setup/claude.rs:10-40`) | 5 s, SessionEnd 1 s | denies the hosted MCP via `deniedMcpServers` (`src/setup/claude.rs:43-65`) |
| Codex | `~/.codex/hooks.json` with trust hashes; MCP in `config.toml` `[mcp_servers]` (`src/setup/codex.rs:12-50`) | 5 s, SessionEnd 1 s | |
| Cursor | `~/.cursor/hooks.json` events `sessionStart`, `beforeSubmitPrompt`, `postToolUse` (matcher `*`), `stop`, `sessionEnd`; `~/.cursor/mcp.json`; allow rule `Mcp(<server>:*)` (`src/setup/cursor.rs:13-60`) | 10 s | |
| Grok | `~/.grok/hooks/lightbulb.json` (PostToolUse matcher `*`); MCP in `~/.grok/config.toml`; allow `MCPTool(<server>__*)` (`src/setup/grok.rs:15-60`) | 10 s, SessionEnd 3 s | |
| OpenCode | plugin `~/.config/opencode/plugins/lightbulb.js`; MCP at `mcp.<name>` in `opencode.json[c]` (`src/setup/opencode.rs:17-60`) | 10 s (`src/setup/opencode-plugin.js:11`) | the plugin maps `session.created`→session-start, `tool.execute.after`→post-tool-use, `session.idle`→stop, `session.deleted`→session-end (`src/setup/opencode-plugin.js:132-160`, `src/setup/opencode-plugin.js:207-221`); it has **no user-prompt-submit** |

- **Start block:** Claude Code and Codex only (see section 5).
- **Tools:** `recall` and `remember` are `scope: "network"`, so they are listed for every harness, signed in or not (`src/mcp.rs:853`, `src/mcp/tools.json:56-83`). Test: `mcp_tools::tools_json_lists_remember_and_recall`.

---

## Claims to check against docs

1. Cursor, Grok and OpenCode never get the session-start memory block (`src/hook.rs:112-118`, `src/hook.rs:201-203`). Only Claude Code and Codex do, and only the Claude path is tested.
2. No block unless `<cwd>/.git` exists **in the start folder itself** and has an `origin` remote. A session started in a subfolder of a repo gets no block and no repo stamp on its memories (`src/mcp.rs:237-247`).
3. "Repo" is the bare repo name from origin, with no owner (`src/mcp.rs:263-269`). Two orgs' `api` repos share memory filtering.
4. The 1600-char budget covers the page only. The heading and instruction add about 200 chars (`src/memory.rs:550-559`). One line that does not fit drops all later sections, so Lessons can vanish entirely (`src/memory.rs:636-637`).
5. Team memory is **on by default** (`memory_team: true`, `src/config.rs:72`). Uploads are **off by default** (`src/config.rs:73`).
6. `team_off` gates only *writing* team entries and `share`. Syncing team entries from peers and recalling them continue when off (no other `memory_team` reads: `src/daemon/ipc.rs:463`, `src/daemon/ipc.rs:939`, `src/cli.rs:692`).
7. `metadata` upload sends every tool name, seq, time, duration and ok flag. That is not "metadata only" in the sense of no per-call rows (`src/upload.rs:84-93`).
8. Upload interval is 5 minutes. Batches are 200 sessions / 5000 calls, and a session with more than 5000 calls is silently cut (`src/upload.rs:15-16`, `src/upload.rs:48`, `src/upload.rs:134`).
9. Token counts:
   - At SessionEnd, only Claude Code has tokens, from its transcript (`src/hook.rs:370-374`).
   - Cache-read and cache-creation tokens are not counted (`src/capture.rs:265-266`).
   - Stop overwrites rather than adds (`src/capture.rs:130-133`).
   - Upload sends null tokens as `0`, not null (`src/upload.rs:80`).
10. Secret scanning:
    - It is not run on entries received from peers (`src/memory.rs:159-176`) or on capture repo/branch.
    - Capture scans the summary text, which is the first matching field only, cut to 256 chars by the hook first (`src/hook.rs:312`, `src/capture.rs:73-92`). A secret in another arg field is never summarized, so it is never stored.
11. `memory list/show/export/last` are readable from an agent shell (`src/daemon/ipc.rs:899`). Only forget, share, team, drafts, approve and dismiss are person-only.
12. Anyone may `replace` or `forget` anyone's entry. There is no authorship check on `replaces` (`src/memory.rs:311-335`) or on a forget (`src/memory.rs:179-180`).
13. Contested grouping ignores scope and repo. A personal `me` value contests a `team` value with the same subject (`src/memory.rs:337`).
14. Recall is plain bm25 OR-of-prefix-words with no recency or weight tuning (`src/memory.rs:487-495`, `src/memory.rs:506`). Recall covers both `me` and `team`.
15. The MCP `recall` cannot pass `repo` or `budget_chars` (`src/mcp.rs:638-643`). The MCP `remember` cannot set `when` or `source`.
16. The MCP `recall` facts carry the raw member key as `author`, not the member name (`src/daemon/ipc.rs:419-426`).
17. A blank subject through MCP returns `bad_request`, not `no_subject` (`src/daemon/ipc.rs:461` with `src/mcp.rs:652`).
18. `neighbours` are found by an OR search on the subject's words, so they can include unrelated subjects that share one word (`src/daemon/ipc.rs:500`).
19. A forget of a `me` entry is sent to every memory peer, not just the same person. Only the target id leaks (`src/net/memsync.rs:13-22`).
20. Any forget wipes **every** session's `memory last` block, not just the blocks that quoted the entry (`src/memory.rs:213`).
21. The person gate does not treat the Cursor editor app, Claude desktop app or other GUI hosts as agents. Only the listed CLI executables count (`src/agents/caller.rs:26-38`).
22. Capture write ops skip the person walk on purpose. Any local process can write capture rows (`src/daemon/ipc.rs:381-383`).
23. `sessions upload` with no mode works from an agent shell, read-only (`src/cli.rs:840-856`).

---

## Test files covering memory / capture / sync

| file | `#[test]` + `#[tokio::test]` count |
|---|---|
| `tests/memory_store.rs` | 15 |
| `tests/memory_sync.rs` | 8 |
| `tests/memory_tools.rs` | 3 |
| `tests/capture.rs` | 12 |
| `tests/upload.rs` | 6 |
| `tests/hooks.rs` (memory block tests among them) | 20 |
| `tests/cli.rs` (memory / sessions / drafts tests among them) | 60 |
| `tests/embed.rs` (cloud memory import/list/forget_author among them) | 21 |
| `tests/mcp_tools.rs` (remember/recall listing and stamping among them) | 29 |
| `tests/sessions_ipc.rs` | 1 |
| `src/net/memsync.rs` (unit) | 1 |
| `src/hook.rs` (unit) | 7 |
| `src/memory.rs`, `src/capture.rs`, `src/upload.rs` (unit) | 0 |
