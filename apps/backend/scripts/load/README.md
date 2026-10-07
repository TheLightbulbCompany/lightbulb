# Load-test harness (dev only)

Design and pass bars: `docs/superpowers/specs/2026-10-07-load-testing.md`. Per-sub-test commands:
`k6/README.md`.

> **Never run anything here without the owner's go for that run.** Dev pools (work Lambda 20, gate 25,
> stage throttle 50 rps) are shared with the team, and Lambda/API GW/AppSync/AgentCore quotas are shared
> with **prod** (one AWS account). Announce the window first.

| Piece | What |
|---|---|
| `fixtures.py` | `setup` creates `load-NNNN+clerk_test@example.com` users and `load-org-NNN` workspaces and waits until each is ready; `run -- <cmd>` signs them in, writes `fixtures.json` (0600), runs the command, deletes the file; `teardown` removes every fixture |
| `k6/*.js` | the three scenarios; read `FIXTURES`, hard-code `api-dev` |
| `mac_sim.py` | synthetic Macs for scenario 3; runs the viewers' k6 command itself |

All three refuse anything but `api-dev` and a `development` Clerk instance. Every command has `--dry-run`
(prints the plan, calls nothing).

## Prerequisites

- `k6` on the operator machine (`brew install k6`). Nothing else: no npm packages.
- AWS SSO for the dev Clerk secret: `aws sso login --profile isol8-admin`. `fixtures.py` reads
  `lightbulb/dev/clerk_secret_key` from Secrets Manager at run time. Never copy it into a file or commit it.
- `cd apps/backend && uv sync` (uses the backend's `httpx`, `PyJWT`, `cryptography`, `websockets`).
- Clerk's dev instance is documented at 100 users; `setup` refuses past `CLERK_USER_CAP` (default 100).
  Verify the real cap before a run larger than the pilot.

## Pilot (2 users, 60 s) — the first run, proves auth, AppSync and the clocks

```bash
cd apps/backend
uv run python -m scripts.load.fixtures setup --orgs 1 --users-per-org 2 --dry-run   # read the plan
uv run python -m scripts.load.fixtures setup --orgs 1 --users-per-org 2              # waits for ready
uv run python -m scripts.load.fixtures run --orgs 1 --users-per-org 2 -- \
  k6 run -e SEND_S=60 -e STAGES='[{"target":1,"duration":"60s"}]' scripts/load/k6/messaging.js
uv run python -m scripts.load.fixtures teardown
```

The pilot also measures what the spec leaves open: Clerk FAPI per-IP limits, whether one client JWT
used by several k6 VUs keeps rotating cleanly, and whether AppSync keeps a subscription open past its
token's 60 s life (the harness refreshes HTTP tokens every 40 s but does not re-subscribe).

## Kill switch

- **Ctrl-C** once: k6 stops and prints its summary; `fixtures.py` deletes `fixtures.json` at once;
  `mac_sim.py` ends its sessions, closes its sockets, unpairs its devices and deletes `mac_sessions.json`.
  A second Ctrl-C hard-stops k6.
- Queued agent turns drain by themselves; each fixture org stops itself at its $0.25/day trial cap.
- Emergency only, owner-approved: purge the dev work FIFO (drops every dev user's pending turns).
- Stop if account-wide Lambda `ConcurrentExecutions` passes 50% of `aws lambda get-account-settings`,
  or any prod page alarm fires.

## Cleanup

`fixtures.py teardown` per fixture org: removes and deletes every non-creator member, waits until Clerk
lists only the creator, then the creator calls `POST /users/me/delete {"confirm_email": …}` (the sole
member's workspace wipe: runtime, Paperclip company, rows), retrying 409 for up to 2 minutes, then
deletes any Clerk org or `load-*` user left. It refuses an org with any member that is not a load
fixture. Load rooms go with the workspace. If residue survives a round, the owner permits the
`dev-clean-slate-reset` skill between rounds only, announced to the team first.
