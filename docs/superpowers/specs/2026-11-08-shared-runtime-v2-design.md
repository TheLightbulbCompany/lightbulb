# One shared Hermes runtime per tier (phase 2 task 3): design note

8 Nov 2026. Status: **design only, no code.** A flag-gated backend switch is not safe to ship by itself. The reasons and the order of work are below.

## Why a backend-only flag is not safe

A runtime is one tenant today, and the code depends on that in three places:

1. **The tenant is fixed when the runtime boots, not sent with each request.** `build_hermes_runtime_env` (`apps/backend/core/services/hermes_provisioning.py:136-150`) bakes `ISOL8_OWNER_ID`, `S3_PREFIX=<owner>/hermes`, the boot-secret id, the browser profile and the memory id into the runtime's environment. The bridge reads them once at import (`apps/infra/hermes/bridge/__init__.py:11,37`), and so does the memory plugin (`plugins/memory/agentcore/__init__.py:216`). If the backend pointed every owner at one ARN, every request would land in a bridge that thinks it belongs to whichever owner the shared runtime was booted for.
2. **The IAM role is the only fence between tenants.** Each owner's execution role is limited to `arn:aws:s3:::<bucket>/<owner>/*` (`core/billing/bedrock_aip.py:43-66`) and to that owner's boot secret. The agent's shell can read the role's credentials from the microVM metadata service (`runtime_transport._vault_header` docstring). That is why the vault token travels on the invoke and not in the secret. A shared runtime has one role. Unless credentials are scoped per session, any tenant's agent could read every tenant's profiles, boot secrets and browser profiles.
3. **Session ids and the wake lease are keyed by owner, not by runtime** (`session_id_for`, `_ensure_session`). This part already works for a pool, because AgentCore isolates each session id in its own microVM.

So the switch is the last step, not the first. A flag that ships before the image and IAM work would either stay dead code or, once on, put one owner's state into another owner's turn.

## Order of work

| # | Change | Where | Gate |
|---|---|---|---|
| 1 | The bridge takes the tenant from each invoke (an `X-Lightbulb-Tenant` header in the envelope, which the backend already wraps). It refuses a session whose first tenant differs, and sets owner/prefix/memory/browser profile per session instead of from env. | `apps/infra/hermes/bridge/*`, memory plugin | env var on the image; old runtimes ignore it |
| 2 | Per-session scoped credentials. The backend sends a short-lived STS token for the owner (an assume-role with an inline policy that confines S3, the boot secret and the browser profile to `<owner>`) on the invoke, the same way the vault token travels. The shared execution role keeps only Bedrock, logs and memory. AWS's multi-tenant AgentCore guide uses the same pattern (STS session tags / token vending, research 11 §1). | backend `runtime_transport`, IAM in `container-stack.ts` | tested on dev |
| 3 | `HERMES_SHARED_RUNTIME_ARN` setting (per tier). `runtime_transport`/`runtime_runs` use it instead of `container["agentcore_runtime_arn"]` when it is set, and every envelope carries the tenant. | backend | flag, default off |
| 4 | With the flag on: skip `provision_hermes_runtime` on org.created (profiles still go to S3), skip the per-owner role, the boot secret and the `roll_hermes_runtimes` loop. One roll per tier. | backend | same flag |
| 5 | Runtime **v2** (`platformVersion: V2`, idle memory freed after 120 s). Measure cold start on dev before removing the wake lease. AWS hasn't published it. | infra + backend | measurement |

Steps 1 and 2 have to land and be tested on dev before step 3's flag is turned on anywhere.

## $ (research 11 §3, 14 rows 5/10)

Today there are 25 prod runtimes at 54-57 s cold start, about $216/mo idle AgentCore memory (11 TL;DR #2), plus $14-25 per business per month as the pool grows. A pooled v2 runtime costs only per session: about $0.05 per business per month for Worker tasks at the 11 §4 load. Most of the $216 is already falling because of task 1 (idle 900 → 300 s, no typing prewake). This change removes what is left and the per-business growth.

## Needs a founder decision

- **Silo for regulated customers.** AWS keeps a dedicated runtime for "regulated industries and enterprise customers requiring dedicated infrastructure" (11 §1). Should the medical/dental tier keep a per-owner runtime (today's code path, kept behind the flag), or does everyone go to the pool?
- **Tenant isolation is credential scoping (step 2), not a separate VM per tenant.** A bug in the scoping would leak data across businesses. Is that acceptable risk for the pool tier?
