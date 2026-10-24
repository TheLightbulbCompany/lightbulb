# Lightbulb network, phase 1 (the workspace as a computer on the network): acceptance

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

## Step 1 — dev pin flip and roll

- [ ] `hermes-version.json` `dev.tag` → candidate; deploy run id; `roll_hermes_runtimes --all` exit code; `get-agent-runtime` `containerUri` ends with the tag.

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
