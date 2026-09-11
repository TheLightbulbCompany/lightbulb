# Native same-session probe — 2026-09-10

Both installed harnesses accepted follow-up input into their original, ordinary interactive session while a second process observed its native transcript. No SDK worker, relay model, provider fork, second rollout writer, or app-server resume was used. This establishes a useful feasibility path, **not completed V1**: output is completed text, attribution is limited, and native cancellation can hold queued work until local continuation (see the September 11 approval follow-up).

Original scope: disposable sessions on macOS, Claude Code **2.1.268**, Codex CLI **0.149.0**. Observations ran approximately 03:26–03:35 UTC on September 11 (September 10 in America/New_York). The follow-up below qualifies exactly Claude **2.1.269** as well; all other Claude versions remain unavailable. No dependency, production configuration, supervisor, UI, or sharing integration was added in the original probe.

## September 11 — Claude 2.1.269 compatibility

After the native automatic update, a fresh literal `claude` launch in `/private/tmp/lightbulb-claude269.1eNqD6` ran PID **12912**, conversation **c62981bb-082e-4b14-a5cc-aa7eead5bafc**, with both native UI and current-execution user/assistant metadata reporting **2.1.269**. Baseline response completed at **19:25:12.602 UTC**. Only trust for that empty disposable directory was accepted; no provider credentials, permissions, hooks, update settings, or real sharing enrollment were changed.

The production adapter's owned capture test passed. The existing production transport loopback test passed idle (**10.79 s**) and busy (**10.65 s**), proving correlated native user admission and a mirrored public answer in the same process. During the busy run, `claude agents --json` reported `busy`, and the native UI displayed request **b7ec1062-fea2-4358-99bc-827a295e3bb0** while integers were still streaming. It finished 1–400, persisted peer user UUID **b8fdf53b-0adc-486e-959c-0afbb6a43b15**, and answered `HOST_INTEGRATED_CLAUDE_d2c0f526af104de08a975b64d201a6c3` at **19:28:25.483 UTC**. A local draft typed during that output remained intact and, after a separate Enter, answered `CLAUDE269_BUSY_LOCAL_DRAFT` at **19:28:43.953 UTC**. The guarded probe also sent/observed `CLAUDE269_PROBE_REMOTE`, honestly reporting socket-written/delivery-unknown before the native response appeared.

The adapter and probe share one exact-version predicate and one bounded runtime verifier. The runtime version comes from consistent records after the PID birth cutoff, independently of installed CLI metadata: synthetic installed **269** / running **268** remains **268**. Mixed 268/269 current records, unsupported versions, missing/unknown timestamps, malformed evidence, stale-only evidence, wrong sessions, and unknown public wrappers remain unavailable or excluded. Correlated admission requires the recorded runtime's exact version. Executable/inode, process generation, transcript confinement, socket ownership, and native peer permission restrictions remain enforced. A replaced installed executable can still make an older process unavailable; no compatibility fallback bypasses that identity check.

The final library/probe command (`cargo test --lib --example local_session_probe --offline` with the existing test-only `TAURI_CONFIG` bundle override) passed **101 library tests**, with **3 live tests ignored**, and **9 probe tests**. The owned capture test passed again after the final evidence guard change. Changed Rust files pass formatting and `git diff --check`; whole-crate `cargo fmt --check` still reports pre-existing formatting differences in unrelated files.

The capture test now also requires `LIGHTBULB_TEST_CLAUDE_VERSION=2.1.269`; use the actual expected version explicitly. Native proof is local loopback only: no signed build, authenticated dev/prod relay or two-Mac acceptance is implied. Output remains completed public text, not token streaming.

## September 11 — actual approval dialogs and cancellation

On candidate `4529afb04`, a new owned Claude269 session with session-only `permissions.ask: ["Bash"]` displayed its native dialog for a harmless `printf`. The existing production-transport loopback test sent a follow-up into that same session: the contribution appeared while the dialog remained open, without approving the command. Local **No** denied it; native admission and the public answer were then mirrored. The test passed in **37.90s**. No global settings or real sharing enrollment changed.

The earlier Codex attempts lacked a dialog because the native turn context had `approvals_reviewer: auto_review`, independently of `approval_policy: on-request`. The installed schema documents reviewer `user`; a new disposable `codex --sandbox read-only --ask-for-approval on-request -c 'approvals_reviewer="user"' --no-alt-screen` session displayed the actual dialog for creating one empty marker in its owned directory. Queued remote input did not approve that tool. Native **Esc/No** left the file absent and the conversation interrupted. The production loopback test proved admission but **failed** its public-response condition after **60.73s**: cancellation did not automatically drain the queue.

One subsequent ordinary local message kept the tool denied and answered at **21:14:09.301 UTC**. The original queued production marker answered at **21:14:13.493**, followed by a second explicitly queued probe marker at **21:14:15.865**, all in the same native thread with no replay or fork. The denied file remained absent. Thus queued admission is not completion, and a cancelled Codex conversation can require local continuation. Do not force-resume it or describe this as a passing automatic post-denial mirroring case. Deployed UI/transport must present this limit honestly. Both owned sessions exited normally; upgrades/new hook trust were declined and no global permissions changed.

## Evidence matrix

| Case | Claude | Codex |
|---|---|---|
| Ordinary launch | Literal `claude`; interactive PID 98973 | Literal `codex`; interactive PID 98928 |
| Automatic discovery | Native `claude agents --json` reports session ID, PID, cwd, status; example scopes listing to supplied cwd | Example discovers ordinary CLI processes by cwd and their single open rollout; verifies `session_meta` ID/version/source. First terminal prompt must exist; pre-first-prompt process was found but had no rollout |
| Registration | Temporary SessionStart hook independently registered native listing + session ID + transcript path + exported inbox path; files mode 0600. Registration alone does not verify runtime version; guarded watch/send require a current-execution transcript record | Captured native thread identity from the original process's open rollout; never opened it for writing |
| Already running before observer | Yes; native session existed before probe binary built/launched | Yes; native session existed before probe binary built/launched |
| Idle input | No-auth Unix socket envelope delivered and answered `NATIVE_CLAUDE_REMOTE_IDLE` | `codex queue --thread UUID --message TEXT` delivered and answered `NATIVE_CODEX_REMOTE_IDLE` |
| Busy input | Arrived during 400-line response; completed current response, then answered `NATIVE_CLAUDE_BUSY_QUEUED` | Arrived during 400-line response; completed current response, then answered `NATIVE_CODEX_BUSY_QUEUED` |
| Native local input coexistence | Draft typed during busy output survived remote input; separate Enter afterward submitted it and produced `NATIVE_CLAUDE_LOCAL_COLLISION` | Same, producing `NATIVE_CODEX_LOCAL_COLLISION`. This proves draft preservation and sequential admission, not atomic simultaneous-writer ordering |
| Pending tool approval | With session-only `permissions.ask: ["Bash"]`, native Bash dialog stayed open after peer ingress; message shown below dialog. Local **No** rejected command, then queued message answered `NATIVE_CLAUDE_PENDING_APPROVAL` | **Unproven**: read-only/on-request overrides confirmed in turn context, but benign printf and disposable marker-write challenges executed without a visible approval dialog. No remote approval success claimed |
| Explicit inbound hold/refuse modes | Documented by provider, **not exercised** in this bounded probe | No equivalent mode established |
| Identical rapid retry | Two no-auth identical envelopes produced one persisted user row and one answer in this sample. Dedup window/key and persistence not established | Two sends produced distinct native queue IDs and two persisted answers. Caller must not automatically retry ambiguous sends |
| Receipt | Socket write success only; no correlated delivery receipt implemented | Native queue ID acknowledges enqueue, not consumption/completion |
| Terminal attribution | Native text says another Claude session sent the message and includes peer permission restrictions; `from` does not establish a human identity | Queued text appears as a normal user turn; no first-class remote human author identity established |
| Output | Complete public user/assistant text blocks from matching native JSONL event wrappers; thinking/tool blocks excluded | Completed `response_item` / `message` text only; observed assistant phases `commentary` / `final_answer`, channel absent. Unknown phase/channel excluded; no token deltas |
| Shared app-server attachment | Not applicable to inbox path | **Failed hypothesis**: no default control socket before/after ordinary launch; `daemon version` returns ENOENT; original PID has unnamed socketpairs. Queue ingress nevertheless works |
| Exited process | Guarded send rejects registration after native exit | Guarded send rejects registration after native exit |

The 400-line runs appeared incrementally in the terminal but as one completed text record in each provider's JSONL. The observer adds no model call. Native input triggers the original harness's normal model call.

Measured one-shot discovery took **247 ms Claude / 285 ms Codex**. Later discovery with two Codex candidates took 465 ms. With a 100 ms polling interval, three new Codex assistant records were observed **101, 45, 58 ms** after their native record timestamps; two Claude records took **152, 143 ms**. These are file-record timestamp-to-probe-read samples, not model-token latency, an SLA, or a transport benchmark. Historical replay rows were excluded.

## Wire provenance and limits

Claude's [official inbox documentation](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket) documents script ingress, exported socket/token, optional auth on macOS, and inbound rules; it omits the message envelope. The installed 2.1.268 binary's own `[uds-messaging]` diagnostic supplies the newline-delimited shape used here:

```json
{"type":"user","message":{"role":"user","content":"hello"}}
```

The example adds `from: "lightbulb-native-probe"`, as accepted by the inspected native handler. It never reads, sends, or saves `CLAUDE_CODE_MESSAGING_TOKEN`. Native code classifies this unauthenticated message as a peer; this is not an own-child proof or human approval. The envelope is version-specific evidence, **not a stable public SDK contract**. We did not establish a return-address/status protocol, permission approval API, interrupt operation, or reconnect dedup contract.

Codex's installed `queue --help` exposes UUID-targeted message enqueue directly. Although [app-server](https://developers.openai.com/codex/app-server/) offers stronger streaming/control interfaces, the running default CLI did not expose a shared control socket. The controller accepted the native queue route as the same-session alternative to the plan's original server hypothesis. No launch wrapper or persistent enrollment is required for this demonstrated route; typing `codex` remains the tested launch. Native queue lacks the `expectedTurnId` protection of `turn/steer`, so this probe reports next-turn follow-up, not deterministic mid-turn steering.

Discovery/identity guards compare installed provider version, native session, PID, process start/command, cwd, transcript, and Claude socket inode before sending. An installed version is **not** proof of a running Claude version. Guarded Claude watch/send additionally require a matching native user/assistant transcript record stamped after the later of native `startedAt` and the OS PID birth second plus one second. Every such current-execution record must have the supported version/session. Missing, historical-only, or unsupported current evidence fails closed. The OS bound excludes resumed history even if native `startedAt` retains a conversation creation time; its seconds-only precision conservatively excludes the entire birth second. Fresh ordinary-session registration was observed before its first message, but resume lifecycle behavior was not live tested. PID generation is checked again after verification.

The observer rejects changed process start, replaced/truncated file, mismatched session/version records, malformed JSON, and lines over 2 MiB. Its text projection accepts only the observed public wrappers/roles/block types; unknown phase/channel/wrapper records are skipped. Metadata-only review of the two owned Codex rollouts found 12 assistant messages: 2 `commentary`, 10 `final_answer`, all with absent channel and `output_text` blocks. No analysis-channel leak was observed; synthetic negative coverage closes that unverified path. These checks cover stale accidental registration, not an adversary who already controls the same OS user. There remains a check-to-send race: native queue targets a thread ID and could enqueue after its process exits between checking and sending. There is no atomic native generation-bound receipt here; remote control must not be represented as stronger than this.

## Rerun

Use only a newly created disposable directory and the exact processes started for the test. Existing provider authentication is used normally. Never select an unrelated session. All commands below run locally; nothing needs a push or deployment.

From the repository root:

```sh
TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --example local_session_probe --offline
TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo build --manifest-path apps/desktop/src-tauri/Cargo.toml --example local_session_probe --offline
probe_bin="$PWD/apps/desktop/src-tauri/target/debug/examples/local_session_probe"
probe_dir=$(mktemp -d /tmp/lightbulb-native-probe.XXXXXX)
```

The test-only `TAURI_CONFIG` override removes packaging sidecars/resources because fresh worktrees do not contain bundled binaries. It edits no Tauri file and is not a packaging recipe. Existing library warnings in `tray.rs` and `node_client.rs` are unrelated.

In two ordinary terminal windows, enter the new directory and launch `claude` / `codex` without a prompt flag. Complete native trust prompts for that disposable directory only; decline any global upgrade/settings prompts. Send `Reply exactly NATIVE_BASELINE and do not use tools.` from each terminal. The probe must start afterward to verify already-active discovery.

```sh
"$probe_bin" discover claude "$probe_dir"
"$probe_bin" discover codex "$probe_dir"
"$probe_bin" capture codex OWNED_CODEX_PID "$probe_dir/codex.json"
```

For automatic Claude registration, use a **new disposable session** with a temporary settings JSON file containing the following SessionStart hook. Substitute the actual absolute binary and registration file paths. Add `"permissions":{"ask":["Bash"]}` only for the approval challenge. Do not change global settings or existing hooks.

```json
{
  "hooks": {
    "SessionStart": [{"hooks":[{
      "type":"command",
      "command":"/absolute/path/local_session_probe hook /tmp/disposable/claude-registration.json"
    }]}]
  }
}
```

Run `claude --permission-mode default --settings /absolute/path/to/temporary-settings.json` in that disposable directory. The hook writes only non-secret registration metadata using `create_new`; use a fresh registration filename per launch. It checks the exported socket against the native process/listing and records `installed_version`, not a claimed runtime version. Submit a native terminal baseline prompt before guarded watch/send: until a supported-version record exists after the current execution cutoff, both commands fail closed. If the first prompt falls in the ambiguous PID birth second, submit another native prompt. Historical records alone never enable ingress after resume. The example's `capture claude PID NEW_FILE TRANSCRIPT_PATH` is also available for a known disposable transcript, but is not automatic hook enrollment. Registrations from the earlier probe schema must be recaptured; no migration is supplied for disposable files.

In an observer terminal and then a sender terminal:

```sh
"$probe_bin" watch /absolute/path/registration.json 45
"$probe_bin" send /absolute/path/registration.json 'Reply exactly NATIVE_REMOTE_IDLE and do not use tools.'
```

`watch` replays existing text then polls for up to 300 seconds. It prints native timestamps plus elapsed observer time; it does not persist transcript content. To measure fresh-record lag, compare receipt time with `timestamp` and exclude initial replay. To reproduce busy input, ask the native terminal for integers 1–400 and send a new marker while output is visibly ongoing. Type a local draft, then submit separately after observing its preservation. Send the same marker twice to inspect duplicate behavior.

For Claude approval, ask the temporary `permissions.ask` session to run `printf APPROVAL_PROBE`, wait for the visible native dialog, send a text marker through the probe, verify the dialog remains, and choose **No** locally. The peer response should appear after denial. Explicit hold/refuse modes and a reliably induced Codex approval dialog remain follow-up verification, not inferred passes.

Exit only owned sessions via native `/exit`; repeat `send` against stale registrations and expect a nonzero exit before ingress. Preserve or remove only your disposable artifacts; provider-native history and directory-trust records may remain. Do not resume or stop unrelated sessions.

## Verification / decision

Eight example tests cover JSON fragmentation at every split, partial UTF-8, malformed/oversized lines, session/process/generation/version mismatches, missing Claude identity, public-only message projection, Codex metadata replay, unsupported registrations rejected before provider commands, current-execution Claude version evidence (including historical-only rejection), and the real macOS PID-birth cutoff helper against the test process. All pass. Review-round guard changes were verified with synthetic records and the local process helper, without new live provider sessions. Controller's desktop library baseline: 56 tests passed with the same test-only bundle override.

Proceeding to production adapters requires an explicit capability decision around completed-text observation, generic native author attribution, ambiguous retries, and Codex approval-wait behavior. Tasks 2–8 remain unstarted and unauthorized in this spike.
