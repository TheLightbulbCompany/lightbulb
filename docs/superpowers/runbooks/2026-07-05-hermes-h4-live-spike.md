# Hermes H4 — Live Dev Spike Runbook

The backend build (Phases B → H3b) is implemented, reviewed, and parked as a
stacked PR set; a final whole-stack review returned **"coherent + OpenClaw
byte-identical: YES."** H4 is the only remaining phase and it is **hands-on** —
it crosses the publish boundary (fork push, image publish, deploy) and needs
AWS SSO. This runbook is the ordered checklist to take a dev owner from
"stack merged" to "chatting with a Hermes-backed Friday end-to-end."

**Goal (the spike):** a dev owner with `runtime_kind="hermes"` chats with a
Hermes Friday through `/chat`, proving the port abstraction holds for a
non-OpenClaw runtime.

**Boundary note:** every step here is deliberately gated on the user — nothing
below is done autonomously. Fork patches sit LOCAL-only at
`~/Desktop/hermes-agent.nosync` (branch `isol8/v2026.7.1-isol8.1`) until Step 1.

---

## Preconditions

- `aws sso login --profile isol8-admin`
- Discover the dev cluster + backend service/task dynamically (names drift):
  ```bash
  CLUSTER=$(aws ecs list-clusters --profile isol8-admin --region us-east-1 \
    --query 'clusterArns[?contains(@,`isol8-dev-container`)]' --output text)
  BACKEND_SERVICE=$(aws ecs list-services --cluster "$CLUSTER" --profile isol8-admin \
    --region us-east-1 --query 'serviceArns[?contains(@,`isol8-dev-service`)]' \
    --output text | awk -F/ '{print $NF}')
  BACKEND_TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$BACKEND_SERVICE" \
    --profile isol8-admin --region us-east-1 --query 'taskArns[0]' --output text | awk -F/ '{print $NF}')
  ```

---

## Step 0 — Merge the stack (dev only)

The parked stack must reach dev to deploy. Merge in order, or cut a throwaway
integration branch that contains all of B1→H3b for a dev-only deploy.

- Stack order: B1 #1142 → B2 #1146 → B3 #1147 → B4 #1148 → H0 #1151 →
  H2 #1153 → H1 #1154 → H3a #1156 → H3b #1158.
- **Rebase caveat:** the stack branched from a `main` that is behind current
  `origin/main` (it predates #1155). Rebase B1 onto current `origin/main`
  before merging so the merge is clean; the rest of the stack auto-retargets.
- Everything is additive + dark — merging does NOT change any OpenClaw owner
  (`runtime_kind` absent ⇒ openclaw). Safe to merge to dev without flipping
  any owner.

---

## Step 1 — Publish the Hermes base image (fork)

The extended image `FROM`s `ghcr.io/isol8ai/hermes-agent:v2026.7.1-isol8.1`,
which the fork must publish (upstream `docker.yml` was gated to NousResearch;
the local patch repoints it to our GHCR package).

```bash
cd ~/Desktop/hermes-agent.nosync
git log --oneline -3   # expect: docker.yml repoint, WAL→DELETE knob, usage cache-fields
git push origin isol8/v2026.7.1-isol8.1
git tag v2026.7.1-isol8.1 && git push origin v2026.7.1-isol8.1   # triggers the fork's docker.yml
```

- Watch the fork's Actions run publish `ghcr.io/isol8ai/hermes-agent:v2026.7.1-isol8.1`.
- Confirm the GHCR package exists and is **private** (the isol8 build workflow
  authenticates to pull it; per-user ECS tasks never pull the base directly).

---

## Step 2 — Create the extended-image ECR repo (out-of-band)

`build-hermes-image.yml` pushes to ECR `isol8/hermes-extended`, which must
exist before the first run (mirrors `isol8/openclaw-extended`).

```bash
aws ecr create-repository --repository-name isol8/hermes-extended \
  --image-scanning-configuration scanOnPush=true \
  --profile isol8-admin --region us-east-1
```

---

## Step 3 — Build + publish the extended image

`build-hermes-image.yml` builds on changes to `apps/infra/hermes/Dockerfile`
or `hermes-version.json`. After Step 0's merge it may have already run; else
dispatch it manually.

```bash
gh workflow run build-hermes-image.yml --repo Isol8AI/isol8
gh run watch <run-id> --repo Isol8AI/isol8 --exit-status
```

- Confirm the tag in `hermes-version.json` (`dev.tag`) now exists in ECR
  `isol8/hermes-extended`.

---

## Step 4 — Deploy backend + CDK to dev

Merging Step 0 triggers `backend.yml` (FastAPI image → ECS) and `deploy.yml`
(CDK). The CDK adds the `isol8-dev-hermes` base task-def family (port 8642,
EFS `/opt/data`, `HERMES_HOME=/opt/data`, no `API_SERVER_KEY`).

```bash
# Confirm the Hermes base task-def family exists after the CDK deploy:
aws ecs list-task-definitions --family-prefix isol8-dev-hermes \
  --profile isol8-admin --region us-east-1
```

---

## Step 5 — Switch a dev owner to Hermes + provision

Use your own dev account (NOT `isol8-e2e-testing@mailsac.com`). From the
browser console on a dev Isol8 page (Clerk loaded):

```javascript
const T = async () => 'Bearer ' + await Clerk.session.getToken();
// Flip runtime → hermes (persists runtime_kind on the row + primes the cache)
await fetch('https://api-dev.isol8.co/api/v1/debug/runtime-kind?kind=hermes',
  {method:'POST', headers:{Authorization: await T()}}).then(r=>r.json())
// Reprovision so a Hermes container boots (POST /debug/provision is now runtime-aware:
// it mints API_SERVER_KEY + writes config.yaml via the port).
await fetch('https://api-dev.isol8.co/api/v1/debug/provision',
  {method:'DELETE', headers:{Authorization: await T()}}).then(r=>r.json())
await fetch('https://api-dev.isol8.co/api/v1/debug/provision',
  {method:'POST', headers:{Authorization: await T()}}).then(r=>r.json())
```

Verify a Hermes container came up:
```bash
# The per-user service task def should be cloned from isol8-dev-hermes;
# the containers row should carry api_server_key + runtime_kind=hermes.
OWNER=org_xxx   # your dev owner id
aws dynamodb get-item --table-name isol8-dev-containers \
  --key "{\"owner_id\":{\"S\":\"$OWNER\"}}" --profile isol8-admin --region us-east-1 \
  --query 'Item.{status:status.S,runtime_kind:runtime_kind.S,has_key:api_server_key.S}'
```

If the container crash-loops, the first suspect is `config.yaml` (an invalid
Hermes config bricks the daemon at boot — analogous to the OpenClaw config
brick). Read the container logs; the config path in-container is
`$HERMES_HOME/config.yaml` = `/opt/data/config.yaml`.

---

## Step 6 — H4.2a transport smoke (de-risk before the UI)

Prove transport end-to-end WITHOUT `/chat`, via the parked harness. Run it
inside the backend task (it has AWS creds, settings, and network reach to the
container's private IP):

```bash
aws ecs execute-command --cluster "$CLUSTER" --task "$BACKEND_TASK" \
  --container backend --interactive --profile isol8-admin --region us-east-1 \
  --command "/bin/sh -c 'cd /app && uv run python -m scripts.hermes_transport_smoke \
    --owner-id $OWNER --message \"Say hello and tell me your name in one sentence.\"'"
```

Expected: the harness prints translated ChatEvents (chunks/tool markers/done),
a terminal `ChatResult status='completed'`, and `[verdict] PASS`. It exits
non-zero if the run didn't complete or produced no chunks.

**Confirm the H2-inferred live-unknowns here** (this is the cheapest place):
- Approval field name — if the run wedges, check `POST /v1/runs/{id}/approval`
  accepts `{"choice":"always"}` (inferred from the `choices` enum, not verified
  against `_handle_run_approval`). The config should suppress prompts; if not,
  the adapter auto-approves.
- `run.completed.usage` cache fields — the fork patch surfaces
  `uncached_input_tokens`; confirm the frame shape so metering isn't
  cache-inclusive.
- JWT refresh — only if the owner is on the ChatGPT-OAuth provider path.

---

## Step 7 — H4.2b chat via `/chat`

Open `/chat` as the dev owner and send a turn end-to-end (WS → backend → the
Hermes branch → container).

- Confirm the page **mounts** (control-plane `agents.list`/`sessions.list`
  answered for the Hermes owner) and the roster shows Friday.
- Confirm the reply **streams and renders** identically to OpenClaw (chunks,
  tool markers, done), the busy-gate releases (the routing envelope stamps the
  frontend `client_run_id` as `runId`), and multi-bubble/per-session demux is
  intact against real streamed frames.
- Confirm `X-Hermes-Session-Key` actually populates the container's
  `gateway.session_context` bag (tools that read thread context depend on it).

---

## Known follow-ups (out of spike scope — do NOT block on these)

- **Abort not Hermes-aware:** Stop routes through the OpenClaw pool `chat.abort`
  (WS-RPC to an HTTP+SSE container) — the UI detaches cleanly, but the Hermes
  run is not actually cancelled (runs to completion + meters). Next slice.
- **`agents.list` roster uncached:** fresh admin client + Paperclip round-trip
  per call (15s timeout → `[]`). Low frequency; fine for the spike.
- Beyond the spike: marketplace/ArtifactCodec for Hermes agents, the signup-UI
  runtime picker, prod rollout, the N-daemon lifecycle.

---

## Rollback

Flip the owner back and reprovision — the container returns to OpenClaw:

```javascript
await fetch('https://api-dev.isol8.co/api/v1/debug/runtime-kind?kind=openclaw',
  {method:'POST', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())
// then DELETE + POST /debug/provision as in Step 5.
```

Nothing in the parked stack changes OpenClaw behavior, so leaving the code
merged with all owners on `openclaw` is itself a safe resting state.
