# Desktop session pipeline: how a teammate's Claude session gets into the sidebar, stays "Idle", and leaves

All citations are `origin/main` (fetched 2026-10-30). The desktop paths are under `apps/desktop/src-tauri/src/`. Short names used below:
`claude.rs`, `transport.rs`, `lifecycle.rs`, `enrollment.rs` = `local_sessions/<file>`; `local_sessions.rs` = `local_sessions.rs` (module root);
`shell_integration.rs`; backend `local_sessions.py` = `apps/backend/core/services/local_sessions.py`, `relay.py` = `apps/backend/core/services/local_session_relay.py`;
labels = `packages/shared/src/messaging/localSessionLabels.ts`; row = `apps/frontend/src/components/messaging/SessionListRow.tsx`.

## 1. Discovery on the owner's Mac (Aden's)

**The only thing that creates a mirror is `claude agents --json`.**
- `claude::discover` runs `claude agents --json` (`claude.rs:670-674`, `709-722`) and calls `capture` on every row it returns.
- The discovery loop runs it at least every 60 s, sooner when a hook wakes it, and never more than once a second (`transport.rs:1559-1625`, sleep/wake at `1622`). Claude, Codex and hooked providers (Cursor/Grok) are discovered in parallel (`transport.rs:1569-1571`).
- `observe` turns each captured `NativeSession` into a mirror, keyed by `(provider, profile, conversation)` (`transport.rs:475`, key `transport.rs:2876-2878`). If two processes run one conversation, the newest is kept (`local_sessions.rs:1556-1569`).
- Hooks never create a mirror. `apply_hook` only acts on an existing, not-ended mirror and returns `None` for unknown sessions, because a start hook only wakes discovery (`transport.rs:916-937`).

**Other files read during capture or liveness. None of these adds a candidate.**
- `~/.claude/sessions/<pid>.json` (the "registry"):
  - `entrypoint`, used for the headless and in-app checks (`claude.rs:388-402`).
  - `spare` (`claude.rs:42`).
  - `nameSource`/`name`, used for title hygiene (`claude.rs:47-52`).
  - the version (`claude.rs:136-160`).
  - the "still on this conversation" check (`claude.rs:464-483`).
- `~/.claude/daemon/roster.json` workers. Read only by `Holders` for liveness (`claude.rs:552-562`).
- `~/.claude/jobs/<job>/state.json`. Read only for a blocked job's `needs` note (`claude.rs:425-446`).
- Transcripts `~/.claude/projects/**/<conversation>.jsonl`, via `find_transcript` (`claude.rs:207-221`). Used for the title, lineage, generation check and uploads (`claude.rs:91-114`).

**Excluded at capture (`claude.rs:24-44`):**
- Listing rows whose `kind` is neither `interactive` nor `background` (`claude.rs:26-29`).
- A bad pid or a non-UUID `sessionId` (`claude.rs:32-39`).
- Headless runs, meaning `entrypoint` starts with `sdk-` (`claude -p`, Agent SDK, plugin hook runs) (`claude.rs:43`, `448-450`).
- Pre-warmed daemon spares (`spare == true`) (`claude.rs:43`).
- Any row whose `capture` errors, for example the process changed mid-capture (`claude.rs:173-175`). Discovery silently drops these (`claude.rs:716-720`).

**Not excluded:**
- In-app sessions (`entrypoint` other than `cli`) are only flagged: `app: !background && in_app(..)` (`claude.rs:203`, `456-458`). This changes `terminalReason` to "app" (`local_sessions.rs:1344-1353`), not whether the session is listed.
- Unverified or unsupported runtimes are not dropped either. They get a `restriction` (`claude.rs:163-172`) and are listed without send control.

**Enrollment gate.** The host runs only while sharing is enabled on the Mac: `sharing_on` requires `enabled && !paused && !remote_disabled` (`enrollment.rs:585-588`). It runs either in the window or in the `lightbulb-desktop serve` launch agent (`enrollment.rs:263-317`).

**Sharing: per session, or automatic.** This is decided when the mirror is first created (`transport.rs:716-719`):
```
shared = ours || self.share_every.unwrap_or_else(|| crate::shell_integration::is_on(&self.home))
```
- `ours` (`transport.rs:555-563`) covers two cases:
  - The session has a Lightbulb holder: started with `lightbulb claude`, New session, a remote start, a resume or a pick-up.
  - It is a person's continuation (←, `/background`) of an already-shared conversation that has a transcript.
- `shell_integration::is_on` is the Settings toggle "Share every new session". It checks for the guarded block `# >>> lightbulb live sessions >>>` in `.zshrc`, `.bashrc` or the bash login file (`shell_integration.rs:1-8`, `84-88`).
  - When the toggle is on, **every** newly discovered Claude conversation is shared, not only ones launched through the shell function.
  - When it is off, only `ours` sessions are shared.
- After creation, the person can share or stop per session through `SHARE_ASKS`/`share_tick` (`transport.rs:791-...`, `195-210`).
- A withheld (unshared) mirror registers, uploads and heartbeats nothing (`local_sessions.rs:2030-2038`, `transport.rs:202-211`). The exception is a session that was shared and then paused: it still heartbeats as `shared:false`, and only its owner sees it (`transport.rs:206-211`).

## 2. Liveness on the desktop

**The liveness tick (`transport.rs:1363-1424`).** It runs on every 100 ms connection tick (`transport.rs:1764`, `1990`) and once at connection start (`transport.rs:1794`). A mirror ends when all of the following hold:

1. **`runs()` is false** (`transport.rs:1367-1374`).
   - `still_running(process)` (same pid and the same birth time, `transport.rs:2890-2892`) must hold.
   - **For an interactive Claude session (no job)**, `claude::still_registered(pid, conversation)` must also hold (`claude.rs:464-483`):
     - A missing `sessions/<pid>.json` returns false.
     - A record with another `sessionId` returns false.
     - A non-empty `parkedJobId` returns false.
     - An unreadable or unparsable file returns **true** ("not evidence").
   - For a background job (`job.is_some()`), only the worker's pid is checked.
2. **No successor is running** (`transport.rs:1398-1399`).
3. **`held()` is false.** `Holders::read(claude_config).holds(conversation, parked=false, except=own pid)` (`transport.rs:1382-1387`, `claude.rs:511-576`).
   - `Holders.records` come from `<config>/sessions/*.json`. A record counts only if its `updatedAt` is under 24 h and its pid is alive with the same `procStart` (`claude.rs:540-551`).
   - `Holders.workers` come from `<config>/daemon/roster.json` `workers`. A worker counts if its pid is alive with the same `procStart`. **There is no age check on workers** (`claude.rs:552-562`).
   - `holds()` (`claude.rs:566-575`):
     - A record counts only when it is not parked (because `parked=false` here) and it is not the mirror's own pid.
     - A worker counts unless it is the mirror's own pid, or its `replPid` is the mirror's own pid.
4. **A background job gets a grace period.** It ends only after 3 s unheld with no transcript write for 3 s (`JOB_HANDOVER`, `transport.rs:312`, `1402-1410`).

The end reason is `"exited"` if the pid is still alive (it left the conversation on purpose), or `"process-exited"` if it is gone (`transport.rs:1414`). The session is then ended through `end_session` (`transport.rs:1419-1423`).

**Other end paths:**
- **`SessionEnd` hook.** `prompt_input_exit`/`logout` map to `on-purpose`; anything else, including `clear`, maps to `exited` (`lifecycle.rs:409-417`). For Claude the hook is **ignored** (handed over) if the session is a job, or if another holder (as above) holds the conversation (`transport.rs:1066-1077`).
- **A fork listed while its origin is not.** The origin ends as `exited` (`transport.rs:480-497`, applied at `789-791`).
- **Stranded holder** older than the login session ends as `login-session-ended` (`transport.rs:1153-1174`).
- **Idle-stop of an unattached, unwatched Lightbulb-held session** ends as `idle-stopped` (`transport.rs:1180-1215`). This never applies to plain `claude` sessions, which have no holder.

**Answers to the specific questions:**
- **Terminal process gone, but a daemon worker holds the same conversation id:** the row **stays**. `held()` is true and the worker needs no `updatedAt` freshness (`claude.rs:552-562`, `transport.rs:1400`). If discovery lists the worker, the mirror is re-pointed at it as a successor (`transport.rs:678-689`) and becomes `terminal: attach` ("Running in background" or "Done"). If it is not re-listed, the row keeps its last status (see §5).
- **Terminal gone, a background job holds it:** the row stays while the job's worker pid lives (`transport.rs:1370`). After the worker dies it gets the 3 s handover grace. `claude agents` keeps listing a job "while it was working or blocked" (`claude.rs:684-686`).
- **Terminal parked into a job (`parkedJobId`):** the parked record does **not** keep the row alive (`parked=false` at `transport.rs:1386`; `still_registered` returns false at `claude.rs:479-482`). The conversation "went on as the job", and the job's own row carries it, either by `forkedFrom` lineage or by the same id held by a worker.
- **Claude process alive but moved to another conversation (`/clear`, `/resume`, agent view):** the row **ends** with reason `exited`, because `still_registered` sees another `sessionId` or a missing file (`claude.rs:460-464`, `transport.rs:1371`). The only exceptions are another live holder of that conversation, or an unreadable registry file.
- **The same pid coming back to the conversation** revives the mirror with a new generation (`transport.rs:601-627`).
- **A live interactive Claude that `claude agents` stops listing is kept.** Liveness never reads the listing for interactive sessions, and a "listing gap alone never ends anything" (`transport.rs:476-479`, `511-527`). There is no idle or age timeout for a plain `claude` sitting at its prompt.

## 3. What the desktop sends, and how the backend classifies

**Register** (`local.session.register`, `transport.rs:3016-3031`). Fields:
- `provider`, `profileFingerprint`, `nativeConversationId`, `generation`, `providerVersion`
- `status`, `activeTurnId`, `controlUnavailableReason`
- `title`, `cwdName`, `holderId`, `workspace`
- `shared:false` when withheld; attention fields; `previousGeneration`

**No pid and no full cwd are sent.** Only `cwdName`, the basename (§4).

**Heartbeat** (`local.device.heartbeat`). One frame per device. Cadence:
- every 10 s (`heartbeat_interval`, `transport.rs:1533`);
- brought forward, at most once a second, when a mirror's fields moved (`transport.rs:1895-1904`);
- an empty idle heartbeat every 30 s when there are no sessions (`IDLE_HEARTBEAT`, `transport.rs:321`, `1910-1912`).

The heartbeat includes every mirror that is registered, not ended, not stopped (or paused) and has a `session_id` (`transport.rs:3262-3264`). Per-session fields (`transport.rs:3265-3270`): `sessionId`, `generation`, `status`, `activeTurnId`, `controlUnavailableReason`, `title`, `cwdName`, `lastAssistantMessage`, `workspace`, plus stats, capabilities, terminal/reason, attention and signals.

**Where status comes from:**
- Hooks: `UserPromptSubmit` sets busy, `Stop` sets idle (`lifecycle.rs:412-413`).
- The listing: `listing_status` (`claude.rs:762-783`).
- The rule that a live, not-waiting mirror whose status is `unknown` becomes `idle` (`transport.rs:511-527`).
- After an app restart every live mirror starts as `unknown` (`transport.rs:1488-1492`).

**Backend:**
- **Lease.** `LIVENESS_SECONDS = 30` (`relay.py:33`). Register sets `onlineUntil = now + 30` (`relay.py:738`), and each heartbeat renews it and writes `status` (`relay.py:1032-1040`). A heartbeat for an unknown, superseded or ended row, or one from a different connection or generation, is rejected so the Mac re-registers (`relay.py:1001-1023`).
- **Projection** (`local_sessions.py:835-849`):
  - `endedAt` set means `ended`.
  - Otherwise `onlineUntil <= now` means `offline`.
  - Otherwise the status the Mac reported (`idle`/`busy`/`waiting-approval`/`unknown`).
  - "Offline" is computed lazily on read. Nothing writes it on disconnect.
- **Ended** is written only by the Mac's `local.session.ended` frame (`relay.py:1173`). Re-registering un-ends it (`relay.py:793-795`).
- **Listing** `GET /api/v1/messaging/sessions` maps to `list_workspace_sessions` (`apps/backend/routers/messaging.py:344-348`, `local_sessions.py:1351-1378`):
  - It includes live rows and offline rows **with no age limit** (`local_sessions.py:1336-1342`), plus ended rows from the last 7 days (`RECENT_ENDED_SECONDS`, `local_sessions.py:1347`), capped at 20 ended and 100 live (`1348`, `56`).
  - It drops rows whose device is no longer enrolled (`1325-1335`), removed rows (`1321`), unshared rows except for the owner (`1367`), and `kind != interactive` legacy rows.
  - `_without_moved` hides stopped or offline origins of a fork, and empty ended sessions (`796-832`).
  - Retention: "transcripts are kept -- nothing reaps them" (`local_sessions.py:1355-1357`).
- **Desktop side.** Ended mirrors are reaped from disk after 1 day (`ENDED_RETENTION_SECS = 86_400`, `transport.rs:15`, `1430-1450`).
- **Client labels** (`localSessionLabels.ts`):
  - `sessionState`: `ended` shows as **Stopped**; `offline` as **Offline**; `waiting-approval` as Waiting on you; `done` as Done; `terminal === "attach"` as Running in background.
  - Anything else is "here", labelled by `sessionStatus`: `idle` shows "Idle" and `unknown` shows "Online" (`labels.ts:27-50`, `75-79`).
  - The sidebar drops offline rows whose `lastEventAt` is 3 or more days old (`SESSION_ARCHIVE_AFTER_MS`, `labels.ts:167-172`, `207`).

**So "Idle" means three things are all true:**
- the Mac heartbeated this row within the last 30 s;
- the last status it reported is `idle`;
- the row is interactive (terminal `held` or `none`), not a job.

## 4. "in isol8"

- `SessionListRow.tsx:71`: `where = why ?? (session.cwdName ? \`in ${session.cwdName}\` : null)`. The second line uses `session.cwdName` too (`SessionListRow.tsx:74`).
- `cwdName` is `basename(row["cwd"])` of the `claude agents --json` row, truncated to 128 characters (`claude.rs:179`, `950-953`). It is the **folder name of the process's cwd**, not the git repo or worktree root.
- A session started in a subfolder or a worktree shows that folder's last path component. The full path never leaves the Mac.
- The title is separate: `displayTitle`, then `title`, then "New chat" (`labels.ts:23-24`). The title comes from the transcript's `custom-title`/`ai-title`, or the first prompt (`claude.rs:796-...`, `100-112`). A session with no transcript yet, or no title, shows "New chat".

## 5. Ways a row shows "Idle" without a matching live Claude process

1. **A daemon worker holds the conversation** (`Holders.workers` has no freshness check, `claude.rs:552-562`).
   - The terminal pid that owned the mirror is gone, but a live roster worker names the same `sessionId`. The row is kept (`transport.rs:1400`).
   - If the worker is not re-listed by `claude agents`, `native` still points at the dead pid. The `unknown`-to-`idle` rule needs a live process (`transport.rs:521`), so the status is **frozen at its last value**, usually `idle`.
   - The desktop keeps heartbeating it, so the backend keeps it "Idle". This is a live Claude process, but not one that a per-terminal or listing count (like the P2P CLI's) would attribute to that conversation.
2. **Another live interactive record names the conversation.** For example, a second terminal ran `--resume` on it, so its `sessions/<pid>.json` names it with `updatedAt` under 24 h. The row is kept through `held()` with the dead pid's frozen status. This is also the multi-process case `reconcile_sessions` collapses (`local_sessions.rs:1556-1569`).
3. **An unreadable or unparsable `sessions/<pid>.json` counts as still registered** (`claude.rs:472-478`). If the pid is alive but has actually moved on (to another conversation or the agent view), the row stays with stale status.
4. **No idle or age timeout for an idle interactive Claude.** A `claude` left at its prompt for days stays "Idle" for as long as its pid and registry entry live (`transport.rs:1367-1374`).
   - Interactive sessions are **not** checked against the `claude agents` listing (`transport.rs:476-479`).
   - With hooks off or unreachable, status also is not refreshed except by the 60 s listing. A live mirror the listing omits is forced from `unknown` to `idle` regardless of real activity (`transport.rs:511-527`).
5. **Heartbeats keep going whenever the desktop service runs.** They are sent for every registered, non-ended mirror (`transport.rs:3262-3270`), from the app window or the headless `serve` launch agent (`enrollment.rs:263-317`). The backend never ages a row past what the Mac reports. As long as heartbeats arrive, `onlineUntil` is renewed and the row is shown with the reported status (`relay.py:1032-1040`).
6. **Ends lost or late.**
   - An end decided while the socket was down is queued and sent on reconnect (`transport.rs:1794-1810`, `ended_pending`).
   - If the desktop stops running entirely, rows turn **Offline** after 30 s, not Idle. The client hides them after 3 days, and the server lists them indefinitely (`local_sessions.py:1336-1342`, `labels.ts:167-172`).
   - After an app restart, live mirrors read `unknown` ("Online") until a hook or listing settles them (`transport.rs:1488-1492`).

**Most likely explanation for 4 "Idle" vs 2 live on the P2P network.** The desktop keeps a row while *any* live holder (a registry record under 24 h, or a roster worker of any age) names the conversation. It never drops an interactive row because `claude agents` stopped listing it, and it sends the last known status unchanged. To confirm which case applies, on Aden's Mac compare:
- `claude agents --json`
- `~/.claude/sessions/*.json` (sessionId, pid, procStart, parkedJobId, updatedAt)
- `~/.claude/daemon/roster.json` workers

against the 4 titles. The desktop's mirror files under the local-sessions state dir (`<key>.json`) show each mirror's `native.process.pid` and `status`.
