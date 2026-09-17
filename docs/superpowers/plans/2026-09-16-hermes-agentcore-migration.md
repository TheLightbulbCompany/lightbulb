# Hermes on AgentCore — Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move every Lightbulb workspace from a per-user OpenClaw container on ECS + EFS to one AgentCore runtime running Hermes Agent behind the Python bridge, with full parity (room turns, run frames, Paperclip runs, desktop node, browser, providers, config, marketplace) before any customer moves, then delete the OpenClaw stack.

**Architecture:** One AgentCore runtime + one session per workspace; all agents are Hermes profiles (complete `$HERMES_HOME`s under `profiles/<agentId>`) in one bridge process that speaks the existing `lightbulb-channel` ingress/RPC/hook-token contracts, so backend, messaging plane, Paperclip, web, mobile and desktop stay unchanged. State is per-profile directories synced to S3 (no EFS). The desktop node attaches lazily: the backend terminates the node protocol and the bridge calls one hook-token route per node command. Browser = AgentCore Browser through `agent-browser`'s native provider. Config = a per-profile `config.yaml` + `.env` writer in the backend, non-negotiables pinned in Hermes's managed overlay.

**Tech Stack:** Python 3.12 (backend FastAPI + the bridge, stdlib `http.server`, boto3/botocore, pytest), Hermes Agent v0.21.3 (image `nousresearch/hermes-agent`, plugins API: `register_terminal_environment_provider`, tool plugins, redaction prefix registry), Amazon Bedrock AgentCore Runtime + Browser (boto3 `bedrock-agentcore-control` / `bedrock-agentcore`), AWS CDK (TypeScript) for IAM/S3/ECR/env, Next.js frontend (`bedrock-agentcore` npm `BrowserLiveView`), Tauri desktop unchanged.

**Spec:** `docs/superpowers/specs/2026-09-16-hermes-agentcore-migration-design.md` (owner-approved 2026-09-16). Research reports with file:line citations: `.superpowers/sdd/2026-09-16-hermes-agentcore-migration/research/01..06-*.md` (gitignored scratch; regenerate from the spec's Sources if missing).

## Global Constraints

- Dev only until M6: never touch prod; prod stays on ECS at its current pin; customers migrate once, straight to Hermes (spec §2.6, §8).
- The reference OpenClaw workspace `org_3JCpE2dm4G8Dip7z5e6Lod5fqVY` stays untouched; the Hermes dev workspace is `org_3JCfoyOVF0AjDYjOnIZz5cK8XJL` (`#general` `dcfac9a5-913d-53bb-bb59-04dc385becc6`, CEO `c955ddc2-ea14-4d10-beac-b1d24b057475`, runtime `lb_hermes_3JCfoyOVF0AjDYjOnIZz-NL7Hk93XJY`).
- Zero changes to the messaging plane, the run ledger, AppSync channels, Paperclip, web/mobile message rendering, or the desktop binaries; the bridge conforms to the contracts in spec §5 exactly (field names, status codes, timing: `run.started` at accept, heartbeat every 30 s, sweeper 120 s / 90 s).
- One bridge process per workspace; never multiplex two workspaces into one process (Bedrock env vars are process-wide, spec §4.6).
- No secrets in `config.yaml` (only `${VAR}`), secrets in the profile `.env`; runtime `environmentVariables` carry identifiers only, the bridge fetches secrets from Secrets Manager at boot (spec §4.5, §4.9).
- Profile sync excludes `sessions/request_dump_*.json` and `logs/`; `HERMES_DUMP_REQUESTS` is never set (spec §4.5).
- Every profile `config.yaml` is complete (no inheritance); `model` is written as `{default: …}`; `auxiliary.<task>` pinned to `provider: bedrock` with `fallback_chain: []`; `bedrock.discovery.enabled: false` (spec §4.5).
- Node transport is lazy attach through the backend; the bridge never opens a WebSocket; `node.invoke.request` carries `paramsJSON` as a JSON string; `hello-ok` matches the report-02 checklist (spec §4.4).
- Freeze new features in the OpenClaw `lightbulb-channel` plugin; skills text stays tool-name-neutral (spec §7).
- Backend tests: `cd apps/backend && uv run pytest tests/unit -q` is the gate (never `tests/`, which wanders into live-AWS integration tests). Bridge tests: `cd apps/infra/hermes && python3 -m pytest tests -q` with no AWS credentials. Infra: `cd apps/infra && npx cdk synth` and commit the regenerated `.github/workflows/deploy.yml` in the same PR.
- Commit after every task; PRs stay small and independently green; the `claude-review` check is known-broken, review the diff yourself before merging (CLAUDE.md).

---

## File Structure

New (bridge and Hermes plugins, `apps/infra/hermes/`):
- the `bridge/` package replacing today's single `bridge.py`: `bridge/server.py` (HTTP server, `/ping`, `/invocations` envelope, route table, RPC method table), `bridge/turn.py` (one Hermes turn: profile scope, context payload, threading, ambient gate, run registry + abort/steer, hook-token callbacks, AppSync frames), `bridge/sync.py` (S3 profile restore/save with excludes, Secrets Manager fetch at boot, attachment materialization). Imports are `from bridge import sync` / `from bridge.turn import …`.
- `plugins/lightbulb/` (Hermes tool plugin: `send_room_message`, `hire_suggestion`, `local_sessions`, usage report, `pcp_` redaction pattern), `plugins/lightbulb_node/` (terminal environment provider + `nodes` tool, one plugin).
- `Dockerfile` (production image: Hermes base, plugins, skills, Linux skill binaries, bridge), `tests/` (pytest, no AWS).

New (backend, `apps/backend/`):
- `core/services/hermes_profiles.py` (per-profile config/env/SOUL writer + managed overlay), `core/services/hermes_provisioning.py` (fresh-provision path), `core/services/node_broker.py` (node protocol server + invoke correlation), `core/services/browser_sessions.py` (AgentCore Browser session/profile/live-view), `scripts/migrate_workspace_to_hermes.py` (one-shot migration job).

Modified (backend): `routers/messaging.py` (hook-token `POST /internal/node/invoke/{ownerId}` beside the other internal routes), `routers/container.py` (`POST /browser/session`), `routers/gateway_relay.py` (terminate the node protocol for AgentCore rows), `core/services/relay_hooks.py` (no-ops for AgentCore rows), `core/services/readiness_state.py` + `wake_policy.py` (park/wake judgement), `core/billing/bedrock_gate_tokens.py` (drain via `GetAgentRuntime`), `scripts/provision_runner.py` (step 1 → Hermes path behind a flag), `core/services/agent_export.py` + `catalog_service.py` (S3 profile prefixes), `core/services/openclaw_work.py` (agent-mail / Paperclip-run event kinds).

Modified (infra): `lib/stacks/service-stack.ts` (`AGENTCORE_HERMES_IMAGE`, the renamed replicas-bucket variable, browser IAM, ECR pull for `lightbulb/hermes-agent-arm64` in base + boundary), `lib/stacks/network-stack.ts` (bedrock-agentcore interface endpoint if absent), `.github/workflows/build-hermes-image.yml` (new).

Modified (frontend): `components/workspace/panels/BrowserDockPanel.tsx` (BrowserLiveView), `AgentCreateForm.tsx` (EFS path), `next.config.ts` (CSP).

Deleted at cutover (M6): spec §9.

## Milestone map

| # | Milestone | Depends on | Pass criteria (spec §6) |
|---|---|---|---|
| M1 | Room-turn parity | — | threaded reply, full frame vocabulary, abort/steer, `lightbulb` tool plugin, load-bearing RPC, aux pins, PDF tolerance, Paperclip-run door, mail eventKind |
| M2 | Node exec (hard gate) | M1 (bridge package, backend client) | `system.run` from a Hermes turn through the backend node route; vendored node host stays connected 10 min; cross-instance forwarding |
| M3 | Device commands | M2 | every device command works from a turn |
| M4 | Config writer, providers, provisioning, secrets, park/wake | M1 | fresh workspace provisions on Hermes; Bedrock/ChatGPT OAuth/BYO each answer; MCP refresh; hot patch on next turn |
| M5 | Browser + Shopify door | M2 (node relay for the callback), M4 (profile writer) | panel shows the AgentCore Browser; agent drives it; Shopify login persists |
| M6 | Migrate, roll out, delete | M1–M5 | founder migrated and used a day; prod dispatch; per-workspace flips; deletion PRs merged |

## Milestone plans

The tasks live in one file per milestone (each starts with a pointer back here; task numbering is local to its file). Execute in milestone order; within a milestone, in task order.

| file | tasks |
|---|---|
| `2026-09-16-hermes-agentcore-migration-M1-room-turn.md` | M1 room-turn parity (25 tasks: bridge package + secrets at boot, ingress + PDF, threading, context, AppSync frames + vocabulary, run.ended/usage, abort, steer, the `lightbulb` plugin, RPC, auxiliary pins, agent mail, Paperclip adapter door, delete the spike hatches, optional streaming deltas, ambient gate, attachments) |
| `2026-09-16-hermes-agentcore-migration-M2-node-exec.md` | M2 node exec (7 tasks: handshake frames, invoke correlation, relay termination + hook no-ops, presence task address + security-group self-ingress, internal route, `lightbulb_node` provider, vendored node-host loopback proof + dev proof) |
| `2026-09-16-hermes-agentcore-migration-M3-device-commands.md` | M3 device commands (6 tasks) |
| `2026-09-16-hermes-agentcore-migration-M4-config-providers-provisioning.md` | M4 config writer, providers, provisioning, secrets, park/wake (18 tasks) |
| `2026-09-16-hermes-agentcore-migration-M5-browser-shopify.md` | M5 browser + Shopify door (6 tasks) |
| `2026-09-16-hermes-agentcore-migration-M6-migrate-rollout-delete.md` | M6 migrate, roll out, delete (8 tasks, incl. retiring the VNC browser panel at cutover) |

Shared conventions every milestone file assumes: bridge tests run with `cd apps/backend && uv run pytest ../infra/hermes/tests -q` (the `conftest.py` there puts `apps/infra/hermes` on `sys.path`); backend tests with `cd apps/backend && uv run pytest tests/unit -q`; Hermes source is read from the image (`docker run --rm --entrypoint sed lightbulb/hermes-spike:1 -n '<range>' /opt/hermes/<path>`) until the production image exists; dev proofs run against the Hermes dev workspace named in Global Constraints with the scripts in the job tmp dir (`hermes-room.py`, `hermes-spike-invoke.py`, `hermes-build-push.sh`, `hermes-spike-runtime.py`).
