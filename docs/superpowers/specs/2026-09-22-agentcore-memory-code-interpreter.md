# Workspace memory + code execution: options and recommendation

Status: **for owner review** (2026-09-22). Nothing is implemented. Delete this file in the PR that ships the approved slice.

**Owner decisions so far:**
- Memory is shared by **all agents in a workspace**.
- The owner asked for a comparison of memory backends (self-hosted Honcho, AgentCore Memory, and others).
- The owner asked for a comparison of code-execution options (crabbox, the Hermes ssh backend, and others).

## TL;DR

| | Recommendation | Runner-up |
|---|---|---|
| **Memory** | **AgentCore Memory**, via our own Hermes MemoryProvider plugin. Gated on a 1-day isolation spike. | Mem0 OSS in-process, on Bedrock with a per-owner S3 Vectors index. Choose this if every inference must go through the bedrock-gate. |
| **Code execution** | **An EC2 exec box per owner, reached through Hermes' built-in `ssh` backend.** Start with the founder workspace only. | AgentCore Code Interpreter with a per-owner S3 Files mount. It needs a new Hermes backend and a tenancy spike. |

## Today (verified on `eb60c16d8`)

- **Memory:**
  - Hermes built-in only (`hermes_profiles.py:366-373`, `provider: ""`).
  - `MEMORY.md` (about 2.2k chars), `USER.md` (about 1.4k chars) and an FTS-indexed `state.db`.
  - All of it is per agent under `<owner>/hermes/<agent>/`. **Agents share nothing.**
- **Code execution:**
  - `terminal.backend: local` inside the workspace's AgentCore microVM.
  - The terminal, the file tools and `execute_code` all share that one backend.
  - Working trees are lost when the microVM restarts unless they are inside the profile.
  - Heavy jobs run next to the agent loop and next to the gate bearer and hook token.
- **Pinned Hermes:** `v0.21.3` is upstream tag `v2026.9.14`.
  - Memory providers it bundles: byterover, hindsight, holographic, honcho, mem0, openviking, retaindb, supermemory.
  - Terminal backends it bundles: local, docker, singularity, ssh, modal, managed_modal, daytona, vercel_sandbox.
  - Neither list has an AgentCore option.
- **Integration precedent: Browser v2.**
  - A capability flag in the bridge `health` response.
  - A per-owner inline policy (`bedrock_aip.ensure_owner_browser_policy`).
  - A statement in the `OwnerBedrockBoundary` permissions boundary.
  - A `lightbulb` plugin hook.
  - `docs/architecture/current-system.md:90-96` and the browser-v2 spec are both stale on this (they still describe v1 / "in progress").
- **Billing:** AgentCore compute is absorbed by the platform. Only Bedrock inference is metered per owner.

## Memory options

Load assumed: 50 workspaces × 200 turns/day ≈ 300k turns/month.

| Option | Shared across agents | Tenancy | Where LLM/embedding calls run, and who pays | Infra | ≈ $/month | Top risk |
|---|---|---|---|---|---|---|
| **AgentCore Memory** | yes (`actorId = ownerId`) | IAM `actorId` (events) + `namespace` (records) conditions — **proven in dev spike** | An AWS-bundled model, outside our gate. The platform pays. | one `CfnMemory` + our plugin | **~$300** all-in | record get/delete actions have no condition keys → must be explicitly denied |
| **Mem0 OSS in-process** | yes (`user_id = ownerId`) | IAM on a per-owner S3 Vectors index | Bedrock **through our gate**, so billed and capped per owner | S3 Vectors bucket | ~$60 on Nova Micro; ~$2k on Haiku | Gate changes on the money path; extraction latency inside the turn |
| **Honcho self-hosted** (AGPL) | yes (best model: workspace → peers) | workspace-scoped JWTs | OpenAI/Anthropic/Gemini only, **no Bedrock** (would need a LiteLLM proxy). Spend is central and not attributable per owner. | ECS API + deriver, Redis, pgvector DB | ~$1.1–1.6k | Ops burden, AGPL, central store of every tenant's memory |
| **Hindsight self-hosted** (MIT) | yes (a bank per owner) | Postgres schema per tenant; needs a custom extension | Bedrock, central | ECS + Postgres | Honcho-class or higher | Heaviest ops |
| **Holographic / ByteRover** | **no** (lives in the per-agent profile) | per profile | none | none | $0 | Sharing would need new sync and a single-writer guarantee |

Ruled out:
- **RetainDB, Supermemory Cloud:** data leaves AWS.
- **OpenViking:** AGPL server to run.
- **Memori:** not in our Hermes version.

**Recommendation: AgentCore Memory.** It needs no servers, isolation comes from IAM, data stays in AWS, and extraction is bundled at about $0.001 per turn. Mem0 is the answer only if "every inference through the gate" outranks ops.

### Existing AgentCore providers for Hermes (checked 2026-09-22)

**Upstream:** neither `plugins/memory/` at `main` nor at `v2026.9.21` (v0.21.4) contains an AgentCore provider.

**Community:** two exist. Neither is usable as-is.

- **`sfalsin/hermes-agentcore-memory`** (MIT, v0.1.0, a single commit on 2026-08-23, 0 stars, no tests, about 150 lines). It targets the same `agent.memory_provider.MemoryProvider` / `RecallStatus` interface our pinned `v2026.9.14` has, so it is useful as a reference. It does not fit for four reasons:
  - It writes raw `User:/Assistant:` transcripts with `BatchCreateMemoryRecords`. That **skips the extraction strategies entirely**, so it stores whole turns, not facts.
  - It calls `retrieve_memory_records` synchronously with no timeout, which can block a turn.
  - Its namespaces are per agent and taken from env, with no `actorId`/owner scoping.
  - Its `sync_turn` blocks the turn.
- **`guanquntang/hermes-agentcore-memory`** (**no license**, so we can't reuse it; April 2026, 0 stars). It is a different job: it mirrors the built-in `MEMORY.md` into AgentCore as a primary store with a local cache and offline queue. It is not shared semantic memory.

**Decision:** write our own provider of about 150 lines, based on sfalsin's MIT shape (credit it in the file header). The differences:
- `CreateEvent` with `actorId = ownerId`, so AgentCore extracts facts.
- A timeout-bounded `prefetch`.
- `sync_turn` off the turn thread.
- The namespace comes from the owner, not from free-form env.

### First slice (AgentCore Memory)

0. **Spike: DONE 2026-09-22 (dev, `spike-g-*` resources, all deleted).** Isolation works on ONE shared memory resource, so no per-owner resource is needed.
   - **Condition keys**, from AWS's service reference:
     - `CreateEvent`, `ListEvents`, `GetEvent`, `DeleteEvent`: `actorId` and `sessionId`.
     - `ListSessions`: `actorId`.
     - `RetrieveMemoryRecords`, `ListMemoryRecords`: `namespace` (exact match) and `strategyId`.
     - `GetMemoryRecord`, `DeleteMemoryRecord`, `BatchDeleteMemoryRecords`, `ListActors`: **none**, so they must be explicitly Denied.
     - There is **no `namespacePath` key**. A `namespacePath` call fails closed under a `namespace` Allow, so the plugin must always pass `namespace`.
   - **The per-owner policy that worked:**
     - Allow `CreateEvent`, `ListEvents`, `GetEvent` and `ListSessions` when `actorId` equals the owner.
     - Allow `RetrieveMemoryRecords` and `ListMemoryRecords` when `namespace` equals `/owners/<owner>/`.
     - Deny `BatchCreate`, `BatchUpdate`, `BatchDelete`, `DeleteMemoryRecord`, `DeleteEvent`, `GetMemoryRecord` and `ListActors`.
   - **Isolation results.** Owner B was AccessDenied on every cross-owner call:
     - `CreateEvent` as owner A (the write-poisoning case);
     - list and retrieve on A's namespace, on `/` and on `/owners/`;
     - `namespacePath`;
     - record get and delete by id.
     - Sending `namespace` and `namespacePath` together is rejected with a ValidationException.
   - **Latency**, measured from a laptop over the public endpoint:
     - memory resource to ACTIVE: 142 s;
     - `CreateEvent`: p50 115 ms;
     - `RetrieveMemoryRecords`: p50 256 ms, p95 411 ms;
     - **extraction: 96–133 s**, so a fact is not recallable on the very next turn.
   - **Wipe.** `ListMemoryRecords(namespace)` + `BatchDeleteMemoryRecords` in chunks of 100, and `ListSessions(actorId)` → `ListEvents` → `DeleteEvent`. 2 records and 12 events took 1.8 s, and were gone from reads within about 22 s (eventually consistent).
     - Empty session and actor shells stay listed (`ListActors` still shows the owner id) until events expire. No API deletes them. This is metadata-only residue; note it for GDPR.
1. **CDK.**
   - One `CfnMemory` per environment: a semantic strategy only, namespace `/owners/{actorId}/`, 30-day event expiry, existing KMS key.
   - A boundary statement in `container-stack.ts`.
2. **Backend.**
   - A per-owner inline policy, `owner-memory` (`bedrock_aip.py`).
   - `AGENTCORE_MEMORY_ID` in `hermes_provisioning.build_hermes_runtime_env`.
   - The wipe paths (`account_wipe`, `DELETE /debug/user-data`) delete the namespace. `DELETE /debug/provision` does not.
3. **Image.**
   - A `plugins/memory/agentcore/` MemoryProvider:
     - `prefetch`: top-5 retrieve with a 1.5 s timeout, skip on failure.
     - `sync_turn`: one `CreateEvent`.
   - The bridge sets `memory.provider: agentcore` only when the env var is present, and reports `memoryProtocol: 1` in `health`.
   - **N-1 safe:** an old image ignores the env var.
   - Rollout is the usual two steps: flip the image pin, then roll the runtimes.
4. **Dev acceptance.**
   - Tell the CEO a fact in #general. About 2–3 min later, Engineering recalls it in a DM.
   - A second owner's role gets AccessDenied, checked in CloudTrail.

**Which LLM does extraction, and who pays** (per CROSS-TRACK §1):
- The built-in semantic strategy runs on an AWS-managed model that AWS doesn't name.
- It is billed to our AWS account through the Memory record price ($0.75 per 1k records per month).
- It adds **zero** OpenRouter calls and no bedrock-gate traffic, so it cannot drain the shared free-model pool.
- Free-tier workspaces get memory at platform cost, about $0.001 per turn.

**Shared files** (per CROSS-TRACK §4/§5). Message these owners before editing:
- `hermes_provisioning.py` (one env line) → provisioning.
- `service-stack.ts` (backend grants for the wipe path) → provisioning and Jev.
- The image plugin under `apps/infra/hermes/` → coordinator. It is one bridge change on the serialized train.

**Skipped:** summary, preference and episodic strategies; private per-agent memory; a memory UI; per-owner metering (under 1% of cost).

**Other risks:**
- Content goes to an extraction model AWS doesn't name. Check against the privacy/Termly copy.
- Shared memory lets anything said in one room reach every agent, a prompt-injection path. Say so in the `lightbulb` skill.

## Code execution options

Load assumed: 50 workspaces × about 2 exec-hours/day.

| Option | Working trees persist | Isolated from agent credentials | Startup | ≈ $/month | Runs where | Hermes support in v0.21.3 | Top risk |
|---|---|---|---|---|---|---|---|
| **EC2 box per owner + `ssh` backend** | yes (EBS) | yes (separate box, no credentials unless opted in) | 15–45 s from stopped | ~$220 stop-when-idle; ~$1.35k always on | our VPC | **zero code** (config + key in the boot secret) | a fleet of 50 small VMs to patch, wake and garbage-collect |
| **AgentCore Code Interpreter + S3 Files access point** | yes (mount persists) | yes (its own role) | seconds (unmeasured) | ≤ ~$650, active CPU only | our account | new `agentcore.py` backend | a shared interpreter role that can mount every owner's access point (spike) |
| **Daytona** | yes (stopped sandboxes) | yes | sub-second (claimed) | ~$500 | Daytona cloud | zero code | data leaves AWS; one platform key in every microVM |
| Vercel Sandbox / Modal / E2B | snapshots | yes | ~1 s | $0.6–1.1k | third party | zero code / zero code / new backend | data leaves AWS; cost |
| Fargate exec worker + `ssh` | no (EFS is gone) | yes | 30–60 s | ~$240 | our account | zero code | no persistent disk |
| **crabbox** (`openclaw/crabbox`) | yes (it leases EC2 and friends) | yes | same as EC2 | EC2 + a coordinator service | our account + coordinator | none; would sit in front of `ssh` | **wrong way round**: it syncs a local checkout *out* to a throwaway box, while our agent lives in the cloud and needs the box to *keep* state. It also adds a service from the OpenClaw ecosystem. |
| `managed_modal` | — | — | — | — | Nous gateway | — | needs a Nous account/token. Ruled out. |

**How the `ssh` backend works:**
- It holds one ControlMaster connection, and each call is a fresh `ssh … bash -c`.
- The working directory and env persist between calls.
- `file_sync.py` tar-pushes the credential files registered in `credential_files`, the skills and the cache into the remote `~/.hermes`.
- Our connector credentials (`gh`, `aws`, `gog`, `xurl`) are plain profile files and are **not** registered. So they don't reach the box unless we opt each one in. That is isolation by default, but those CLIs don't work remotely until we do.

**Recommendation:** EC2 box per owner through `ssh`. It needs no Hermes code, stays in our VPC, keeps working trees on a real disk, and gets heavy or untrusted work out of the agent's microVM. Runner-up: Code Interpreter with S3 Files, if we'd rather not own VMs and can spend a backend's worth of code plus a tenancy spike.

### First slice (founder workspace only)

1. **The box.**
   - One t4g.medium in a private subnet.
   - Its security group admits port 22 only from `ContainerSecurityGroup`.
   - No useful instance role.
   - sshd `AcceptEnv` limited to our passthrough names.
2. **The key.** An ed25519 key in the founder's boot secret.
3. **The profile.** The founder's profile gets `terminal: {backend: ssh, host, user, key}`. This is a one-owner switch at `hermes_profiles.py:388`, applied with `PATCH /debug/provision`.
4. **Prove it.**
   - A clone survives a microVM restart.
   - Tests running on the box don't starve the agent.
   - Measure per-call latency and `file_sync` time.
5. **Credentials.** Then opt in `gh` only, through `credential_files`.

**Deferred:** automated box lifecycle (backend starts and stops the box from `last_interactive_at`), a per-agent directory convention, and the fleet rollout.

**Owner-gated:** launching an EC2 box is a new resource. In prod it has to go through CDK, not a hand launch; the hand launch is for the dev spike only.

## Owner decisions needed

1. **Memory backend:** AgentCore Memory (recommended), or Mem0-through-the-gate?
2. **Code execution:** EC2 + `ssh` (recommended), Code Interpreter + S3 Files, or Daytona?
3. **Order:** memory first (recommended; it is smaller and nearly independent), or both in parallel?
