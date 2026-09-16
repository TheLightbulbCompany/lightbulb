# Hermes-on-AgentCore spike (3 days, pass/fail)

**Spec:** `docs/superpowers/specs/2026-09-16-hermes-on-agentcore-evaluation.md` §7 holds the six criteria and the pass/fail rule. This plan is how to run it. Dev only, fix forward, nothing on ECS or on the OpenClaw runtime is touched.

**Isolation from the OpenClaw work:** worktree `.claude/worktrees/hermes-evaluation`, branch `docs/hermes-on-agentcore-evaluation` (spike code goes on `spike/hermes-agentcore`), throwaway dev org `org_3JCfoyOVF0AjDYjOnIZz5cK8XJL` (E2E fixture, `trial_local`, ECS parked, access point `fsap-0dc7ac24938a9c991`), runtime name prefix `lb_hermes_`, ECR repo `lightbulb/hermes-gateway-arm64`. The OpenClaw runtime on `org_3JCpE2…` stays as the reference.

## Facts fixed before starting (2026-09-16)
- Official image `nousresearch/hermes-agent:latest` = Hermes v0.21.3 (2026.9.14), multi-arch (arm64 present), 2.69 GB uncompressed on arm64 (compressed size measured in step 1), runs as **root**, `HERMES_HOME=/opt/data`, entrypoint `/opt/hermes/docker/entrypoint-dispatch.sh`, Python 3.13.5, **Node 26 present** (our contract server runs unchanged), `uv` present; no `aws`, `gh`, `sqlite3`, `litestream`, `chromium`.
- Bundled Anthropic SDK 0.87.0: `AnthropicBedrock` has **no `api_key` (bearer) parameter**. The SDK's main branch has it (`api_key` defaults to `AWS_BEARER_TOKEN_BEDROCK`, `base_url` defaults to `ANTHROPIC_BEDROCK_BASE_URL`, bearer skips SigV4). Route to the gate = bump `anthropic` in our image if released, else install from the SDK git ref; Hermes's `build_anthropic_bedrock_client` passes no static AWS keys, so the SDK defaults apply. Fallback if the bump breaks Hermes: a ~30-line subclass in a `sitecustomize` that sets the bearer in `_prepare_request`.
- The gate (`apps/infra/lambda/bedrock-gate/core.mjs`) accepts only `/model/{urlEncodedId}/{converse|converse-stream|invoke|invoke-with-response-stream}` with `Authorization: Bearer`; the SDK URL-encodes an ARN with `safe=':'` and the gate `decodeURIComponent`s it — same as OpenClaw today.
- AgentCore runs the container as uid 1000 (P3 finding): the spike image must make `HERMES_HOME` writable by 1000.
- Warm pool: ~10 pooled instances per runtime version run the CMD → the contract server's lazy start (gw-v6) is reused as-is: `OPENCLAW_GATEWAY_PORT=8642`, `OPENCLAW_GATEWAY_CMD=hermes`, `OPENCLAW_GATEWAY_ARGS=gateway`.

## Day 1 — the image and the gate (criteria 1 and 4)
1. `apps/infra/hermes/Dockerfile.spike`: `FROM nousresearch/hermes-agent:latest`; `COPY agentcore/contract-server.mjs agentcore/boot-layout.sh` (from the OpenClaw work); bump `anthropic`; `USER 1000` with `/opt/data` and a profiles dir owned by 1000; `CMD ["/opt/agentcore/boot-layout.sh"]` pointing at `/mnt/oc` for the EFS access point. Measure compressed size (must be ≤ 1 GB).
2. Push to ECR `lightbulb/hermes-gateway-arm64:spike-1`.
3. `config.yaml` per profile: `model: {provider: bedrock, default: <owner sonnet AIP ARN>}`, `bedrock: {region: us-east-1}`, `gateway: {api_server: {enabled: true, port: 8642, key: ${API_SERVER_KEY}}, multiplex_profiles: true}`. Env on the runtime: `ANTHROPIC_BEDROCK_BASE_URL=<gate url>`, `AWS_BEARER_TOKEN_BEDROCK=<owner bearer>`, `AWS_REGION`, `API_SERVER_KEY`, `HERMES_HOME`, the contract-server vars.
4. Create the runtime by hand (boto3, admin profile, same request shape as `build_runtime_request`, role = the owner's Bedrock role, EFS AP `fsap-0dc7ac24938a9c991` at `/mnt/oc`). Invoke `GET /v1/health` through the contract server, then `POST /v1/runs` with a one-line prompt. **Pass 4** = a reply and a settled row in the gate ledger for the owner. **Measure 1** = cold and warm turn latency from the invoke.

## Day 2 — profiles, our messaging plane, state (criteria 2, 3, 5)
5. Six profiles (the org's Paperclip roster ids) under `/mnt/oc/hermes/profiles/<agent>/`, multiplexed; `/p/<agent>/v1/runs` per agent; two concurrent runs; RSS from the diagnostic hatch.
6. A room turn: temporary backend path in the worktree that, for a `runtime_host=hermes` row, submits the room message as a run with `conversation_history` from the room and relays the SSE events into the run ledger and the reply into the room. Reuse `runtime_transport` (envelope), `ensure_runtime` (with the Hermes image/env), the messaging writer. **Pass 3** = the CEO agent's reply appears in `#general` of the spike org.
7. State, serverless first (owner's question 2026-09-16: the EFS + symlink + litestream layer exists only because OpenClaw's state is a filesystem). Run Hermes with **no EFS mount**: profile dirs on the ephemeral disk, synced with S3 at first invocation and after each turn (`aws s3 sync` in the contract server's hooks, or the AWS sample's `workspace_sync.py`); config generated at boot from the backend, chat history from the room, secrets from the env. `StopRuntimeSession`, re-invoke, confirm memory and skills survive; record restore time and the S3 object count. Compare against an EFS-mounted profile dir only if S3 sync is too slow (> 5 s) — and note Hermes `state.db` on NFS is expected to misbehave, same as OpenClaw's. Either way the warm pool must show zero gateway starts. If S3 sync passes, the deletion list for the cutover is: litestream, the local-volume-in-EFS layout, `boot-layout.sh`'s symlink spine and fence, the `.isol8-guard`/`.isol8-protocol` lock protocol, chokidar polling.

## Day 3 — migration and verdict (criterion 6)
8. `hermes claw migrate --preset user-data --source /mnt/oc` for the spike org: memories + skills present in a profile; `lightbulb` and `paperclip` skills load; MCP servers from the org's integration rows rendered into `config.yaml`.
9. ChatGPT OAuth into the Codex provider for one profile (from an existing dev OAuth row).
10. Write the verdict against the six criteria into the spec (§7 outcome table), with measured numbers, and the recommended next plan.

## What is deliberately out of scope
Browser, Slack/mail hooks, session sharing relay, marketplace, prod, any change to the OpenClaw runtime or the ECS fleet.
