# Selected-Mac execution: staged release acceptance

Preparation only. No live desktop, production or account checks are asserted by
this document. Record evidence from the actual deployed build in the release/PR;
keep owner IDs, tokens, pairing codes, local paths, browser state and signed URLs
out of public logs. The historical [headless protocol probe](../../apps/backend/tests/manual/node_host_loopback.md)
and [isolated file/MCP fixture](../../apps/backend/tests/manual/test_desktop_resources_loopback.py)
are component checks, not substitutes for either physical Mac.

## Release identity and order

Complete this table separately for a disposable **dev** owner and an explicitly
authorized **production** owner. A git SHA or version label alone is not evidence
of what is serving or installed.

| Evidence | Dev | Production |
|---|---|---|
| owner/workspace and two Mac device IDs (private record) | pending | pending |
| backend source SHA, ECS task-definition revision, **each** tested task ID | pending | pending |
| frontend source SHA and Vercel deployment/build ID | pending | pending |
| `hermes-version.json` tag, ECR digest, selected AgentCore runtime image/version | pending | pending |
| desktop source SHA, `desktop-build.yml` run, signed/notarized release tag + DMG/updater signature | pending | pending |
| installed app version/build **after update**, native/CLI command grants, reciprocal pairing IDs/generations | pending | pending |
| room history fixture, OS/Chrome versions, timestamps and command/receipt IDs | pending | pending |

1. Ship backend compatibility and desktop first. `desktop-build.yml` is separate
   from the backend pipeline; confirm its final signed/notarized production release
   and updater artifact before testing production. The currently audited installed
   production app is **0.4.150**, not evidence that this branch's code is installed.
   Update that app from the final release, relaunch, verify the installed version
   and signed build, then bring its Clerk webview to a foreground signed-in state.
   The v4 pair-state gate rejects 0.4.150's persisted v3 state, so that sign-in
   automatically calls `/desktop/pair`; verify both reciprocal device entries,
   new pairing group and effective native/CLI commands in the backend ready rows.
   Do **not** delete Keychain keys or manually wipe app state. Do the
   corresponding signed dev-build check on the disposable dev Macs.
2. The CDK synth reads only `DESKTOP_EXECUTION_OWNER_DEV` or
   `DESKTOP_EXECUTION_OWNER_PROD` from GitHub Actions variables. Empty means
   `DESKTOP_EXECUTION_ENABLED=false` and an empty allowlist in that environment;
   one valid Clerk `user_…` or `org_…` owner ID enables **that owner only** on the
   next backend deployment. Confirm the synthesized task environment and all
   running backend tasks before submitting. Do not set a comma list, edit a live
   task definition, or infer enablement from a connected badge. Remove the
   variable and deploy to close new submissions. Status, cancellation and
   duplicate receipt lookup must remain available while the gate is closed.
3. Build a candidate Hermes image through `build-hermes-image.yml`; record its
   ECR digest. Land a **dev** pin change, then explicitly roll only the selected
   dev runtime with `apps/backend/scripts/roll_hermes_runtimes.py`; inspect the
   running runtime's image. Pinning alone does not roll it. The shared
   **AgentCore cloud browser** (#2818) is separate from Mac browser permission. After dev passes, production pin, runtime
   roll and `deploy.yml` dispatch require separate release authority. Watch every
   authorized push/deploy run and verify the served frontend/backend, not just CI.

## Disposable dev acceptance, then production repeat

Use one innocuous marker file/URL per physical Mac and record its expected
device ID, connection generation, task address, request ID and receipt ID. Each
call to admission/submit/status/cancel is short; poll a single receipt rather
than resubmitting. Verify the ordinary Hermes terminal still reports AgentCore
paths and the normal browser remains the cloud browser. Never substitute either
for an explicitly selected Mac.

- Force the public API request, native Mac socket and CLI socket onto different
  backend tasks; repeat with **two tasks and four tasks** during a rolling
  deployment. Capture ECS task IDs, relay/presence rows and routed task address.
  A unique marker must appear on only the selected Mac, not the second Mac or
  AgentCore. Verify stale generation, wrong owner/member, unpaired/legacy device,
  missing native grant and revoked device are refused before effect. Re-pairing
  and effective native **and** CLI commands, not version strings, are the proof.
- Run a shell command lasting **over 30 seconds** through short HTTP requests.
  Approvals are answered by the Mac from the consent chosen at pairing (no
  prompt): test exact argv/cwd/run binding, each switch off in Settings (Shell,
  Browser, Files, Screen, Paperclip actions) refusing, expiry, revoked pairing and
  policy not `allowlist/always`. Stop during a process with a child/grandchild; record PIDs,
  PPIDs, PGIDs, final receipt and marker absence after the deadline. Test timeout,
  backend kill, lost response **after** effect, duplicate submit, reconnect,
  app quit, sleep/wake, network change and rolling deploy. A cancel request
  without terminal/process proof is `cancel_requested` or `unknown`, not
  success. No automatic replay or second effect after an uncertain result.
- Exercise native `device.*`, notification permission granted/denied and toast
  fallback, real `screen.snapshot` with and without macOS Screen Recording
  permission. Confirm size/capacity rejection and tenant-isolated artifact
  retrieval; verify returned hashes, MIME and bytes rather than accepting a
  Mac-local path as a cloud artifact. Keep a healthy paired connection idle for
  ten minutes and observe protocol ticks and **no AgentCore wake** solely from
  pairing. Record denial, disappearance and stale-target error codes.
- **Mac browser (`use_browser`).** In a room ask an agent to use "my Mac" for a harmless
  page: it calls `lightbulb_desktop use_browser` with no arguments and the Mac opens a
  separate "Lightbulb agent" Chrome window (`<app_data>/agent-chrome`) without taking focus;
  the agent's own `browser_*` tools drive it over the ticketed CDP door
  (`core/services/mac_cdp.py`). Every page works there, `file://`, `chrome://` and
  `chrome-extension://` included; downloads land in `~/Downloads/Lightbulb agent`. Room
  members see its frames view-only. Repeat with browser grant on and shell grant off; with
  Browser off, `use_browser` answers "Browser is switched off for that Mac …". The person's
  everyday Chrome is never touched, and Chrome MCP existing-session attach stays
  **unavailable**.
- **Workspace sign-ins on the Mac agent Chrome** (`core/services/mac_browser.py` fences, the
  bridge's `browser_state.py` runs the load/save). Sign in to a harmless site in the cloud
  browser and let it save; an agent on the Mac opens it signed in. Sign in to a second site
  on the Mac during a run, end the run, then start the cloud browser: both sign-ins are
  there (`mac_browser.sync{kind=save,result=ok}` before `run.ended`). Switch Browser off,
  unpair the Mac, or remove the member while it is connected: the agent Chrome quits and
  `<app_data>/agent-chrome` is deleted (`mac_browser.clear{result=ok}`).
- From an explicitly approved disposable folder, test binary read, exclusive
  new-file write, list/archive, byte/hash integrity, symlink/traversal denial,
  oversize rejection and grant revocation. Overwrite is unavailable. Configure
  the harmless [stdio MCP fixture](../../apps/backend/tests/manual/desktop_mcp_fixture.mjs)
  through the desktop's local MCP UI; list the live catalog, call `echo`, then
  remove the tool/revoke the grant and reject the old catalog version. Prove
  wrong-device and wrong-owner catalogs do not leak. MCP descriptions are data,
  never globally registered agent tools.

Repeat the whole matrix for the named production owner **only after** dev evidence
is reviewed and the distinct production release is authorized. Camera, location,
screen recording, full computer input and canvas are not advertised capabilities.

## Desktop RAM gate: same-room before/after signed update

The 0.4.150 baseline and final signed update must use the **same Mac, room,
history size, window dimensions and interaction script**: cold launch, idle,
open the room, scroll/load the same history, perform the same reaction sequence,
then sample at 0, 30, 60 and 101 seconds. Record per-PID `ps -o pid,ppid,rss,command`
(RSS in KiB) and macOS `footprint -p PID` physical footprint, plus process
parentage/role from Activity Monitor. Measure Tauri native, its **direct
OpenClaw Node child**, each WKWebView WebContent, GPU and Network process
separately; record any extra launcher PID rather than folding it into a total.
Capture before/after samples and the installed build identity beside each.
If a WebKit helper is shared or cannot be attributed, mark it unknown rather
than assigning all of its memory to Lightbulb. Compare matched phases and
report peak/delta by process, not just app total.

#2821 proves the redundant Node launcher is gone in a pinned topology test;
it did **not** measure signed-build savings. #2820 bounds the initial web room
history to 100 messages; it did **not** establish a native renderer improvement.
The observed **+466 MB in 101 seconds** WebContent growth remains an open RAM
acceptance finding until this controlled before/after measurement supports a
different conclusion. Record failures honestly; do not infer savings from either PR.

## Rollback and evidence decision

Close new submissions by clearing the one-owner rollout variable and deploying
the backend. Preserve receipts and allow status/cancel while their owner task is
alive; once the task is lost, report `unknown` and investigate the Mac-side
effect before any human-decided retry. Do not clear identities, silently retry
effects, or treat a cancel event with no terminal proof as completed. Browser
clicks/uploads and file/MCP effects already applied cannot be undone. Use
**Done & stop** to save/stop Browser v2 before rolling its image back to v1;
the old CLI cannot adopt a v2-owned cloud session. Stop production expansion if
any two-task/four-task routing, approval, selected-tab, file/MCP, denial/loss or
RAM gate is unmeasured or fails; the release report should name that gap.
