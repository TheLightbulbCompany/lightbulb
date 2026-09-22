# AgentCore Memory + Code Interpreter — design and recommendation

Status: **for owner review** (2026-09-22). Nothing is implemented. Delete this file in the PR that ships the approved slice.

## TL;DR

| | Recommendation | Why |
|---|---|---|
| **AgentCore Memory** | **Adopt, one narrow slice**: long-term memory that all agents in a workspace share | Today every agent's memory is private and tiny (~2.2k + 1.4k chars). A CEO decision is invisible to Engineering. Memory fills that gap for roughly 0.3% of a turn's cost. |
| **AgentCore Code Interpreter** | **Do not adopt now** | Hermes already runs shell and code inside the workspace's own per-owner microVM, on the same hardware class at the same price. A Code Interpreter sandbox can't see the profile, the git trees or the connector CLIs. Revisit once there is a concrete untrusted-code or heavy-compute need. |

## What we have today (verified on `eb60c16d8`)

- **Memory:**
  - Hermes built-in only. `hermes_profiles.py:366-373` sets `memory.provider: ""` and caps `memory_char_limit` at 2200 and `user_char_limit` at 1375.
  - Files are `memories/MEMORY.md` and `memories/USER.md`, plus an FTS-indexed SQLite `state.db` for searching past sessions.
  - Each file is per agent (`<owner>/hermes/<agent>/`) and saved to S3 at the end of the turn. **Nothing is shared between agents.**
- **Code execution:**
  - `terminal.backend: local`. The shell runs as uid 1000 inside the AgentCore microVM (`hermes_profiles.py:388`).
  - `execute_code` is enabled. Guards: a 1800 s run budget, stall guards and a short command deny-list.
- **Precedent:** Browser v2 is how an AgentCore capability was integrated.
  - A capability flag in the bridge `health` response (`bridge/server.py:292`).
  - A per-owner inline policy (`bedrock_aip.ensure_owner_browser_policy`).
  - A new statement in the permissions boundary (`container-stack.ts` `OwnerBedrockBoundary`).
  - A `pre_tool_call` hook in the `lightbulb` plugin.
  - Two docs are stale about it: `docs/architecture/current-system.md:90-96` still describes v1, and the browser-v2 spec still says "in progress".
- **Billing:** AgentCore runtime and Browser compute are **absorbed by the platform**, with no per-owner metering. Only Bedrock inference is metered, through the gate and meter to Metronome.

## AgentCore Memory

### Facts that constrain the design

- **Short-term memory** is `CreateEvent(actorId, sessionId, messages)`. Events expire after 7 to 365 days.
- **Long-term memory** runs up to 6 strategies (semantic, summary, user-preference, episodic, custom). They extract records **asynchronously, about 20 to 60 s after the event**, into templated namespaces. Records are read back with `RetrieveMemoryRecords`, which is a semantic search.
- **Tenancy is the "pool" pattern AWS documents:** one memory resource per environment, with isolation by namespace plus IAM.
  - Condition keys: `bedrock-agentcore:namespace` (exact match) and `bedrock-agentcore:namespacePath` (prefix match).
  - Quotas: 150 memory resources per account (adjustable), 30 TPS for retrieval, and 150k extraction tokens per minute per account (adjustable).
- **Price:** $0.25 per 1k events, $0.50 per 1k retrievals, and $0.75 per 1k records per month with built-in strategies (inference bundled, model undisclosed).
  - A turn costs about 1 event plus 1 retrieval, so roughly **$0.001 per turn**, against about $0.34 of inference.
- **Networking:** the data plane is reachable through PrivateLink (`com.amazonaws.us-east-1.bedrock-agentcore`). The control plane is not, so `CreateMemory` belongs in CDK or the backend, never in a runtime.
- **No Hermes provider exists** for AgentCore Memory, upstream or in AWS samples. Hermes does have a pluggable `MemoryProvider` system (`plugins/memory/<name>/`, one external provider active alongside the built-in files), so we would write about one small plugin.

### Proposed first slice: "workspace memory"

1. **Infra (CDK).**
   - One `CfnMemory` per environment with one **semantic** strategy. The namespace is `/owners/{actorId}/` with `actorId = ownerId`, so every agent of an owner shares it.
   - Event expiry is 30 days, encrypted with the existing KMS key.
   - A new boundary statement allows `CreateEvent`, `ListEvents` and `RetrieveMemoryRecords` on that memory ARN.
2. **Backend.**
   - A per-owner inline policy, `owner-memory`, modelled on `ensure_owner_browser_policy`. It allows `RetrieveMemoryRecords` only where `namespacePath` matches `/owners/<ownerId>/*`.
   - One new runtime env var, `AGENTCORE_MEMORY_ID`, in `build_hermes_runtime_env`.
3. **Image.**
   - A Hermes memory provider, `plugins/memory/agentcore/`. Before the turn it retrieves the top-k records and injects them. After the turn it sends one `CreateEvent` with the user and assistant messages.
   - The bridge enables `memory.provider: agentcore` only when `AGENTCORE_MEMORY_ID` is set, and reports `memoryProtocol: 1` in `health`.
   - **N-1 safe with no backend check:** an old image ignores the env var, and a new image without the env var behaves as it does today.
4. **Wipe.**
   - `account_wipe` and `DELETE /debug/user-data` also delete the owner's namespace, using the batch record delete.
   - `DELETE /debug/provision` leaves it alone, the same rule as profiles (M6-R42).
5. **Acceptance in dev.**
   - Tell the CEO a fact in #general. About a minute later, a DM to Engineering recalls it.
   - Check CloudTrail: a second test owner's role gets AccessDenied reading the first owner's namespace.

**Skipped until needed:** summary, preference and episodic strategies; a per-agent private namespace; a UI to view or edit memories; per-owner metering (under 1% of cost); a self-managed extraction model routed through our gate.

### Risks and what to verify before implementation (a 1-day spike)

- **Cross-tenant writes (the key risk).** The IAM namespace keys are documented for *reads*.
  - If `CreateEvent` cannot be scoped by `actorId` or namespace, owner A's runtime could write events as owner B, and those would be extracted into B's namespace. That is poisoning, not a leak, but it still breaks isolation.
  - Spike: test the conditions on `CreateEvent`.
  - Fallback: **one memory resource per owner**, created by the backend at provision time. That needs a quota raise past 150, plus teardown in the shared teardown seam.
- **Data handling.** Conversation content goes to an AWS-managed extraction model we don't choose. It is the same AWS trust boundary as Bedrock, but check it against the privacy policy and Termly copy.
- **Latency.** Retrieval adds one call to the start of every turn. Measure it; skip on timeout, never block the turn.
- **Hermes memory-provider interface in our pinned base.** Verify it in `nousresearch/hermes-agent@sha256:a5b2…` itself, not in upstream `main`.
- **Prompt injection through shared memory.** A record extracted from one room reaches every agent. That is the same trust level as `#general` today, but say so in the `lightbulb` skill.

## AgentCore Code Interpreter — why not now

- **It adds little.** Sessions are 2 vCPU / 8 GB with 10 GB of disk, 15 min by default and 8 h at most, priced at the same $0.0895 per vCPU-hour as Runtime. Our agents already run code in a per-owner microVM of the same class.
- **It takes a lot away.** The sandbox cannot see the profile, the working trees or the connector credentials. Files have to be copied in and out explicitly, and nothing persists between sessions.
- **No Hermes backend exists** (`tools/environments/` has local, docker, ssh, modal, daytona, vercel_sandbox and so on). Adopting it as the terminal would mean writing `agentcore.py` and breaking the git and CLI workflows.
- **When it would earn a place:** running code the agent did not write and should not trust (for example, a user-uploaded notebook), or heavy jobs that starve the turn. The narrow shape then is one `lightbulb` plugin tool, `run_sandboxed(code, files)`, calling the system `aws.codeinterpreter.v1` in `SANDBOX` network mode. It needs a boundary statement on `code-interpreter/aws.codeinterpreter.v1` and no custom interpreter or role. That is about a day of work when a need appears.

## Owner decisions needed

1. **Memory:** adopt the workspace-memory slice above? (Recommended: yes, after the 1-day isolation spike.)
2. **Memory scope:** shared across all agents of a workspace (recommended), or per agent?
3. **Code Interpreter:** defer (recommended), or build the `run_sandboxed` tool now?
