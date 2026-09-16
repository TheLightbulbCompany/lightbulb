# Hermes-on-AgentCore spike (3 days, pass/fail)

**Spec:** `docs/superpowers/specs/2026-09-16-hermes-on-agentcore-evaluation.md` §7 holds the six criteria and the pass/fail rule. This plan is how to run it. Dev only, fix forward, nothing on ECS or on the OpenClaw runtime is touched. Ponytail-reviewed 2026-09-16: no backend changes, no new envelope, no agent registry, no EFS leg.

**Isolation from the OpenClaw work:** worktree `.claude/worktrees/hermes-evaluation`; spike code on `spike/hermes-agentcore`; throwaway dev org `org_3JCfoyOVF0AjDYjOnIZz5cK8XJL` (E2E fixture, `trial_local`, ECS parked); runtime name prefix `lb_hermes_`; ECR repo `lightbulb/hermes-agent-arm64`; S3 prefix `hermes/<org>/` in `lightbulb-dev-openclaw-replicas`. The OpenClaw runtime on `org_3JCpE2…` (v4 = gw-v7) stays untouched as the reference.

## Design: the bridge speaks the contract the backend already speaks
Taken from AWS's own reference (`aws-samples/sample-host-hermesagent-on-amazon-bedrock-agentcore`, `bridge/contract.py`), then cut down. The microVM runs one Python process (~150 lines): `GET /ping` and `POST /invocations`. `/invocations` unwraps the `{path, method, headers, body}` envelope `runtime_transport` already sends and serves the two paths the `lightbulb-channel` plugin serves today:

- `POST /plugins/lightbulb-channel/inbound/<agentId>` → `202 {"status":"accepted"}`, then in a thread: restore that agent's profile dir from S3 if not yet restored, construct `AIAgent(model=$ANTHROPIC_MODEL, provider="bedrock", session_id=<room:agent>, …)` for the turn (Hermes builds a fresh agent per message anyway; no registry), `run_conversation(user_message, system_message, conversation_history)`, then `run.started`/`run.ended` to `POST $ISOL8_API_URL/api/v1/internal/messaging/runs/<owner>/events` and the reply to `…/agent-messages/<owner>`, both with `$OPENCLAW_HOOK_TOKEN`; save the profile dir to S3.
- `POST /plugins/lightbulb-channel/rpc` → `health` and `agents.list` only.
- `{diagnostic: true, cmd}` escape hatch for bring-up, as in the OpenClaw contract server; deleted after the spike.

So the work consumer, `runtime_transport`, `container_rpc`, the run ledger and the reply path run **unchanged**; moving the org is one DynamoDB update (`runtime_host=agentcore`, `agentcore_runtime_arn=<hermes arn>`, `status=running`). Chat history arrives in the ingress body (the room's recent messages), so the profile's `state.db` is a cache plus memory.

State: no EFS. `HERMES_HOME=/opt/data`, one profile dir per agent (`/opt/data/profiles/<agentId>/`: `config.yaml`, `SOUL.md`, `memories/`, `skills/`, `state.db`), restored lazily per agent on its first turn and saved after each turn and on SIGTERM: `boto3` `download_file`/`upload_file` over `os.walk`, SQLite copied with `sqlite3.Connection.backup()`, `*.db-wal` skipped. Same path on every host, so OpenClaw's "workspace path alias" bug class cannot occur.

Two platform facts (measured by the OpenClaw session on 2026-09-16) the bridge respects by construction: ~10 warm-pool instances per runtime version run the container CMD before any session exists, and a session's mounts attach at session assignment. Nothing session-bound happens before the first `/invocations`; `/ping` answers `HealthyBusy` while a turn or a restore is running, `Healthy` otherwise.

## Facts fixed before starting (2026-09-16)
- Official image `nousresearch/hermes-agent:latest` = Hermes v0.21.3 (2026.9.14), multi-arch, **0.95 GB compressed on arm64 (46% of the 2.048 GB cap)**, 2.69 GB uncompressed, runs as **root**, `HERMES_HOME=/opt/data`, entrypoint `/opt/hermes/docker/entrypoint-dispatch.sh`, Python 3.13.5, Node 26, `uv`; no `aws`, `gh`, `sqlite3`, `litestream`, `chromium`. AgentCore runs the container as uid 1000: the spike image sets `USER 1000` with `/opt/data` owned by 1000.
- Bundled Anthropic SDK 0.87.0: `AnthropicBedrock` has **no `api_key` (bearer) parameter**; current releases (1.x, latest 1.6.0) have `api_key` defaulting to `AWS_BEARER_TOKEN_BEDROCK` and `base_url` defaulting to `ANTHROPIC_BEDROCK_BASE_URL`, and a bearer skips SigV4. Hermes's `build_anthropic_bedrock_client` passes no static AWS keys, so the SDK defaults apply once the package is bumped. Fallback if 1.x breaks Hermes: a ~30-line `sitecustomize` subclass setting the bearer in `_prepare_request`.
- The gate (`apps/infra/lambda/bedrock-gate/core.mjs`) accepts only `/model/{urlEncodedId}/{converse|converse-stream|invoke|invoke-with-response-stream}` with `Authorization: Bearer`; the SDK URL-encodes an ARN with `safe=':'`; the gate `decodeURIComponent`s. Same as OpenClaw today.
- `AIAgent(model=, provider=, base_url=, api_key=, session_id=, platform=, user_id=, chat_id=, enabled_toolsets=, quiet_mode=, tool_start_callback=, tool_complete_callback=, stream_delta_callback=, status_callback=, …)`; `run_conversation(...)` → `{completed, api_calls, messages, final_response}`. Per-profile `HERMES_HOME` is context-local (`hermes_constants.get_hermes_home()`; the override multiplexing uses) — read `hermes_constants.py` inside the image on day 1 for the exact call.
- The AWS sample's "warm-up agent" (a boto3 Converse stand-in answering during the 10–30 s Python load) is not adopted: a stand-in reply in a room is wrong for us; the backend's 10 s ingress retry absorbs the load. Measure whether it does.
- Runtime env = what `build_runtime_env` (#2705) already produces (`ISOL8_OWNER_ID`, `ISOL8_API_URL`, `OPENCLAW_HOOK_TOKEN`, `ANTHROPIC_BEDROCK_BASE_URL`, `AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_MODEL`, `AWS_REGION`, …) plus `S3_BUCKET`, `S3_PREFIX`, `HERMES_HEADLESS=1`. No new names.

## Day 1 — image, bridge, gate (criteria 1 and 4)
1. `apps/infra/hermes/Dockerfile.spike`: `FROM nousresearch/hermes-agent:latest`; `uv pip install` the bumped `anthropic` (and `boto3` if absent); `COPY bridge.py /opt/bridge/`; `USER 1000`; `CMD ["python3", "/opt/bridge/bridge.py"]`. Record the compressed size.
2. Push to ECR `lightbulb/hermes-agent-arm64:spike-1`.
3. Create the runtime by hand (boto3, admin profile; the `build_runtime_request` shape minus `filesystemConfigurations`; role = the owner's Bedrock role; the env above). Invoke `rpc health`, then one ingress turn with a two-line `conversation_history`. **Pass 4** = a reply posted to the room through the hook-token route and a settled row in the gate ledger for the owner. **Measure 1** = cold and warm turn latency from the invoke.

## Day 2 — profiles, our messaging plane, state (criteria 2, 3, 5)
4. Six profiles (the org's Paperclip roster ids); two ingress turns for two agents in flight at once; RSS from the diagnostic hatch. **Pass 2** = < 1 GB resident, both complete.
5. Flip the org's row (one DynamoDB update) and send an `@CEO` message in `#general` from the E2E admin session. **Pass 3** = the CEO agent's reply appears in the room, `run.started/ended` in the ledger, with no backend change.
6. `StopRuntimeSession`, re-invoke, confirm each profile's memories and skills survive via S3; record restore time and object count; the warm pool constructs nothing. **Pass 5** = survives, restore ≤ 5 s. If restore is slow, fix the sync (parallel downloads, skip unchanged keys). If it passes, the cutover deletion list is: litestream, the local-volume-in-EFS layout, `boot-layout.sh`'s symlink spine and fence, the `.isol8-guard`/`.isol8-protocol` lock protocol, chokidar polling.

## Day 3 — migration and verdict (criterion 6)
7. `hermes claw migrate --preset user-data --source <the org's ~/.openclaw copied from EFS>` into a profile: memories + skills present; the `lightbulb` and `paperclip` skills load.
8. Verdict against the six criteria written into the spec (§7 outcome table) with measured numbers, and the recommended next plan.

## Out of scope
Provider setup beyond Bedrock (ChatGPT OAuth, BYO keys), MCP config rendering, browser, Slack/mail hooks, session-sharing relay, marketplace, prod, any change to the backend, the OpenClaw runtime or the ECS fleet.
