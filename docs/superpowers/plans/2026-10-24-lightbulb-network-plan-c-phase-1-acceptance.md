# Lightbulb network, phase 1 (the workspace as a computer on the network): acceptance

> Clock note: the sections stamped 2026-10-25/26 were written against a wrong session clock; the GitHub run ids are the truth (deploy train 40079437330 ran 2026-10-24 08:47Z, so "10-25 ~20:35Z" is really 2026-10-24 ~05:30Z and so on, in order). Sections stamped 2026-10-24 09:xxZ onward carry real times.

Evidence file for Task 13 of `2026-10-24-lightbulb-network-plan-c-phase-1.md`. Each item records
what was run, when (UTC), the receipt states, and the log line or screenshot that proves it.
Nothing here is claimed without its evidence line.

## Builds under test

| piece | version | evidence |
|---|---|---|
| engine (`TheLightbulbCompany/cli`) | v0.3.0, tag at `be3964d` | release run 40067482361 (macos-arm64, linux-arm64, linux-x86_64, publish-npm, bump-brew all success); 7 assets; macOS asset signed + notarized by run 40068056920 |
| Hermes image (bridge Tasks 5–7, engine pinned by checksum `c478311c…`) | PR #4325 → candidate `h-<date>-<sha7>` (fill in) | `build-hermes-image.yml` run (fill in) |
| backend (Tasks 8, 9, 10, 12) | main `ae6116d74` and later | PRs #4313, #4316, #4320, #4322 |
| web (Task 11) | main | PRs #4314, #4315 |

## The two computers

| computer | state | evidence |
|---|---|---|
| owner's Mac (Apple silicon) | `lightbulb 0.3.0`, Developer ID `WZX4U3C22Y` verified, launchd `company.thelightbulb.connect` running (pid 30548), doctor: relay `https://use1-1.relay.n0.iroh.link/` | 2026-10-25 ~11:35Z, `lightbulb update` 0.2.6 → 0.3.0 in place |
| WSL on the owner's Windows server (`glenhardiewindows`, Tailscale 100.100.x over the owner's tailnet) | Ubuntu 26.04 LTS, x86_64, curl/tar/tmux/systemctl present, user systemd running, 577 GB free; `lightbulb 0.3.0` installed by the public `curl … install.sh \| sh` line (INSTALL_EXIT=0, Claude Code connected (hooks + tools)); daemon running in tmux session `lb`; doctor All good (relay default) | 2026-10-25 ~14:15Z–14:35Z through the Windows App remote session (screenshots inst2.png, d4.png in the session scratchpad) |

## Step 0 — the two computers on one team (before the member exists)

| when (UTC) | what | evidence |
|---|---|---|
| 2026-10-25 ~14:45Z | WSL `lightbulb join '<ticket>' --name 'WSL'` with the Mac's `ask`-mode invite (minted on the Mac via `lightbulb invite` in tmux, person-only) → WSL printed "Asking whoever sent the link… (this waits up to 10 minutes)"; the Mac's engine dialog: 'WSL (computer "GlenhardieWindows") wants to join your team using your invite link… Or run: lightbulb approve J8'; the owner clicked Allow (`lightbulb approve J8` then answered "J8 was already answered in the dialog") | j3.png; `lightbulb team` on the Mac: Team "prasiddhaparthsarthy" — 2 computers: You · PPs-MacBook-Pro · founder; WSL · GlenhardieWindows · member |

| 2026-10-25 ~15:05Z–15:35Z | Mac → WSL first message: `lightbulb send WSL/inbox "hello from the Mac — acceptance step 0"` (tmux) → "Sent. WSL has to allow it first (they see a prompt)", id `01M5P9AC9QMY5NYQ0THSK8VXQT`; on WSL `lightbulb held` listed it as H1, `approve H1` → "Allowed H1.", `listen --as inbox --once` printed it; the Mac's receipt then read "Delivered to WSL on GlenhardieWindows." | apprs.png; receipt line |
| 2026-10-25 ~15:40Z | WSL → Mac first message: `lightbulb send gjivepwnkmgn/inbox "hello from WSL - acceptance step 0, reverse direction"` → id `01M5P9WEWVEXHM0JQYJBYNT6GK`; the Mac held it as H9 (CLI list and the engine's native dialog); `approve H9` (tmux) → "Allowed H9."; `listen --as inbox --once` → "From WSL, at the terminal (not verified): hello from WSL - acceptance step 0, reverse direction" | wsends.png (the Mac dialog), approve2.txt |

## Step 1 — dev pin flip and roll

- [x] `hermes-version.json` `dev.tag` AND `prod.tag` → `h-2026.10.24-9566ac3` (#4329, owner: "just get prod out"). Deploy trains: 40073668276 (all CDK jobs green; its two Vercel prod jobs hung on the deploy runners and were cancelled) and 40075082206 (success end to end, carrying the `*/network/*` task-role grant #4330 that the first dev roll exposed as AccessDenied on sessions.json).
- [x] Rolls (one-off Fargate tasks from the backend task definitions): dev 7301360c… (3 owners updated) and 7f3c26a5… (4 owners current, sessions.json written); prod d62d8a1c… (12 owners updated); prod's second roll for sessions.json is pending (AWS SSO expired overnight).
- [x] Verified by the roll logs (`roll_runtime_image: … h-2026.10.19-255e3da -> …:h-2026.10.24-9566ac3 (drifted=True restart=True)` per owner) rather than `get-agent-runtime` (the SSO role is denied it).

## Dev member — first real run (2026-10-25 ~20:35Z, workspace "Network Phase One", org_3L8FQUPIdpUFHcptlCH3a4iScaO, Isol8 Admin's dev account)

| what | evidence |
|---|---|
| `GET /api/v1/settings/network` → 200 `{"connected": true}` after dev roll #2 (was `false` before sessions.json existed) | ledger 20:35Z |
| `POST /api/v1/settings/network/invite` → 200 in 3.9 s with a person invite (`lightbulb join 'eyJ…' --name 'admin@thelightbulb.company'`, 827 chars, expiresAt +24 h) — the engine sidecar minted it inside the AgentCore microVM on the first RPC | ledger 20:35Z |
| sessions.json: workspace "Network Phase One", six starter agents, `wake.url` = `https://api-dev.thelightbulb.company/api/v1/network/wake` + bearer token (never in the URL) | S3 head/body (names only) |
| Settings → Connections → "Connect your computer": "Your workspace is connected." + install line + Get my line + Reconnect workspace | `win/dev-settings-connect-your-computer.jpg` |
| Two-mac CI run 40076202039 (GitHub macOS runner): joined the workspace team through the person invite with NO Allow (`Team "Network Phase One" — 2 computers: You (ci-mac) · member; Network Phase One · founder`); its first message to `um6sfp7huaka/ceo` was `held` (600 s) → engine fix cli #34 (a computer admitted by a person link is that person; v0.3.1); no Activity item appeared for the hold → root-caused and fixed the same day, see "Hold → Needs-you" below | run log; ledger 21:35Z–22:30Z |

## v0.3.1 (the person-link fix) — 2026-10-25 ~23:20Z–2026-10-26 ~00:40Z

| what | evidence |
|---|---|
| cli #34 (a computer admitted by a person link is that person) merged on green CI; bump #35; tag v0.3.1; release run 40077828639 (macOS arm64, Linux arm64, Linux x86_64, npm, brew); signing run 40078384034 success; owner's Mac `lightbulb update` → 0.3.1, Developer ID verified | run ids |
| Hermes image re-pinned to v0.3.1 by checksum (#4332); image build 40078489921 → `h-2026.10.24-d1daf5d`; pins flipped (#4333); deploy train 40079437330 success end to end | run ids |
| Runtimes: still on `h-2026.10.24-9566ac3` until the morning rolls (dev and prod; prod's roll also writes sessions.json) | ledger |

## Hold → Needs-you (the missing item) — 2026-10-24 09:20Z–09:56Z

| what | evidence |
|---|---|
| Root cause (code read, no logs needed): the bridge's `turn._post_backend` prefixes every callback with `/api/v1/internal/messaging/`, and the hold-notice router was mounted at `/api/v1/internal/network`, so the bridge posted to `…/internal/messaging/network/holds/{owner}` → 404, logged as a 4xx, never retried. The bridge test asserted only the relative path. | #4335 description |
| Fix: #4335 (`0fc23c72e`) mounts the internal router under `/api/v1/internal/messaging/network`; backend only, no image change, Codex clean. Guard: #4336 (`9beb59943`, + #4337 for either quote style) resolves every bridge callback literal against `main.app`'s route table — fails on the old mount naming only the hold path. | PRs; dev push deploy 40082347779 success |
| Probe after the dev deploy: `POST /api/v1/internal/messaging/network/holds/org_probe` → 401 (route exists, hook token required); the old path → 404 | curl, 09:50Z |
| Re-test: two-mac run 40083685633 (GitHub macOS runner, `lightbulb 0.3.1`) joined "Network Phase One" through a fresh person invite (2 computers), sent to `um6sfp7huaka/ceo` → `0s held` (the dev member is still v0.3.0, so the hold is expected) → the admin's Activity list showed `nethold:H2` "ci-mac, at the terminal (not verified) wants to message um6sfp7huaka/ceo" with Send / Decline / Always allow and **no message preview**, filed 09:52:45Z (≈1 s after the send) → Send (`POST …/activity/nethold:H2/actions/send` → 200, item `resolved` 09:53:27Z) → receipt `46s delivered`; the runner left the team cleanly | run log + step summary; Activity JSON |
| Prod: deploy train 40083908721 dispatched from `612e4eb61` (covers #4335–#4337); the two covered push deploys cancelled | run id |

## Step 2 — the demo on dev

| # | item | result | evidence |
|---|---|---|---|
| 1 | Settings → "Get my line" on the Mac and on WSL; `lightbulb team` lists the workspace on both; Friday's first message held once on the Mac, answered with `lightbulb approve` on WSL | | |
| 2 | Parked runtime (> 30 min idle); send to `friday` from WSL; doorbell rings; `queued` → `delivered` in ~1 min; reply arrives; seconds send → delivered | | |
| 3 | WSL daemon stopped; Friday sends to it; daemon started; `queued` → `stored` | | |
| 4 | Mac off; ChatGPT through the hosted MCP sends to WSL; label "ChatGPT via <Person>"; retry with the same `request_id` → one message | | |
| 5 | Stranger computer (`two-mac-test.yml` runner via an `ask` link) first message to Friday → Needs-you on the phone; tap Send; hold closes with `decided_by` = tapper | | |
| 6 | `LIGHTBULB_RECYCLE_AFTER_S=600` with `--respec`; recycle keeps `state.db`, loses no message | | |
| 7 | Second engine with the member key on WSL → `network.member_key_contested` fires, P35 pages | | |
| 8 | Wake cost: network-only wakes over 24 h × 30 min idle, AgentCore billed session-hours from Cost Explorer → $ per workspace per month | | |

## Step 3 — prod

- [ ] `prod.tag` flip; `deploy.yml` dispatch (no run in flight); `roll_hermes_runtimes --all` on prod; items 1 and 2 repeated on the owner's prod workspace.

## Rollout notes

- A Mac on an engine older than v0.3.0 drops the ticket's `person` field and fails `verify` → every computer must be on v0.3.0 before Settings offers person lines (the owner's Mac is; WSL pending; the two-mac runner installs latest).
- The 0.3.0 engine asks macOS for the Local Network permission once (LAN peer discovery; the relay path works without it). The owner answers it.
