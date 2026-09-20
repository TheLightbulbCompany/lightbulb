# Hermes Integration Credentials Design

**Status:** Approved by owner; implementation in progress

**Goal:** Make every advertised integration verifiably usable from the pinned Hermes runtime while giving every credential one explicit owner, storage location, delivery path, lifetime, and deletion path.

**Source of runtime truth:** Hermes Agent 0.21.3 (`v2026.9.14`), pinned by digest in `hermes-version.json`. OpenClaw file layouts and behavior are not compatibility requirements.

## Decisions

1. A connected integration is owned by the Clerk workspace, preserving the current team-sharing product model.
2. The platform credential broker is the authority for workspace credentials. Initially it uses Secrets Manager; an AgentCore Identity pilot may replace OAuth custody provider by provider.
3. DynamoDB stores connection state and non-secret metadata only.
4. S3 Hermes profiles store configuration, skills, memory, and user files. They are not the canonical store for third-party secrets.
5. Long-lived credentials are not copied into every agent profile. The active runtime receives only connected, supported workspace grants, and only for the adapters it initializes for that agent turn.
6. Runtime credential files are ephemeral, excluded from profile upload, and removed after the consuming turn or process ends.
7. An integration is not `ready` until a harmless operation succeeds through the same Hermes adapter the agent will use.
8. Browser cookies, OAuth grants, webhook secrets, backend-only credentials, and LLM provider credentials remain separate credential classes with separate lifecycles.

These decisions are intentionally stricter than the current implementation. Hermes 0.21.3 stores native MCP OAuth tokens per profile and does not share an OAuth connection across profiles. Lightbulb has one profile per roster agent, so copying one workspace refresh token to all profiles creates several refresh owners and violates that isolation model.

## Credential placement

| Credential or state | Canonical location | Runtime form | Persistence and removal |
|---|---|---|---|
| Integration connection metadata | DynamoDB integrations row | Provider key, state, account/resource identity | No secret fields. Delete on disconnect. |
| Platform OAuth client secret | Secrets Manager platform-app secret | Never sent to Hermes | Rotate centrally. |
| Owner-provided OAuth client secret | Secrets Manager owner/provider app secret | Sent only when a provider-specific runtime adapter requires it | Delete when owner removes the app, after dependent grants are disconnected. |
| OAuth refresh token | Secrets Manager initially; AgentCore Identity after a provider passes the pilot | Never placed in the durable S3 profile | Revoke at provider when supported, then delete canonical copy. |
| OAuth access token | Minted or refreshed just in time by the credential authority | Environment value, authorization header, or private ephemeral file | Bounded to the operation/session; never uploaded to S3. |
| Static API token | Secrets Manager | Private ephemeral environment value or file | Never uploaded to S3; delete canonical copy on disconnect. |
| AWS customer access | Role ARN and ExternalId in the existing backend-held connection state | One-hour STS credentials from the existing broker | No permanent AWS access keys stored or projected. Stop issuance immediately on disconnect. |
| Google/X/Instagram CLI state | Canonical token/client data in Secrets Manager | CLI-specific private files materialized for the active run | Export a rotated refresh credential back through a versioned backend write only when the provider actually rotates it; otherwise discard local files. |
| Browser website session | AgentCore Browser profile | Cookies/local storage inside the browser service | Separate opt-in lifecycle. It never satisfies API connection readiness. |
| Webhook signing secret | Existing trigger/backend secret store | Used only by the webhook verification path | Never sent to an agent unless the product explicitly requires it. |
| Runtime hook/gateway credentials | Existing per-workspace boot secret in Secrets Manager | Loaded by the bridge at boot | Platform credentials only; never reused as third-party credentials. |
| BYO LLM key | Existing per-owner Secrets Manager path | Existing inference path | Outside the integrations migration. |

## Runtime credential broker

The bridge will request a credential bundle from a new hook-token-authenticated internal endpoint before constructing the agent. The backend derives the workspace from the token, verifies the selected agent belongs to the current roster, loads only connected grants for adapters this runtime supports, and returns provider-specific runtime material. Hermes may initialize several MCP servers before knowing which tool the agent will choose, so this can include several workspace grants on one turn.

The bridge writes that material to a private temporary area or explicitly excluded profile-home paths. It does not write secrets to `config.yaml`, the durable `.env`, or ordinary workspace files. Hermes configuration references ephemeral environment names for remote MCP headers and stdio servers. CLI adapters receive only their required private files. The bridge removes the material after the turn and before changing profiles; any warm MCP client that retains an access token must be closed or rebuilt on disconnect.

The hook token is also available to the workspace's agent shell today. It authenticates the workspace, not a tamper-proof roster-agent identity. A supplied agent or run ID is an audit hint unless the backend can independently match it to a canonical run; Paperclip and mail turns do not all share the messaging run ledger. The broker must not claim that ephemeral delivery hides a token from the agent allowed to use it. The concrete security improvement is removing long-lived canonical grants from durable S3 profiles and preventing cross-workspace issuance.

For OAuth-protected remote MCP servers, the first implementation uses a backend-refreshed access token in an `Authorization` header referenced through Hermes's supported environment interpolation. Hermes's native `auth: oauth` flow remains appropriate when a human independently authorizes one Hermes profile, but it is not used to fan a single Lightbulb workspace grant across six profiles.

The credential response never contains providers that are disconnected, unavailable in the environment, or unsupported by the current Hermes image. Errors fail the affected integration closed without preventing unrelated integrations from loading.

## Refresh ownership

Every provider registry entry must name exactly one effective refresh owner:

- `backend`: the platform retains the refresh token, refreshes before delivery, and persists rotations atomically.
- `agentcore_identity`: Identity retains and refreshes the grant; Lightbulb retrieves an access token just in time.
- `none`: the credential does not refresh, such as a static API key or GitHub token.

The current `actor` designation is removed for workspace-shared grants. A token cannot be both backend-authoritative and silently mutated in an agent profile. Provider-specific CLI adapters that can rotate refresh credentials must return the changed value through a compare-and-swap update tied to the credential version they received. A stale concurrent run cannot overwrite a newer credential.

## Workspace and agent authorization

The canonical grant remains workspace-owned. For this repair, every active roster agent may use connected workspace grants, matching current product behavior. The broker records workspace-level issuance and a claimed agent/run hint; it labels the hint as verified only when a backend-owned run record supports it.

Per-agent grants are a later product feature, not implied by names such as Finance or by `GOG_ACCOUNT`. Adding them will require an explicit allowlist enforced by the broker. Account selectors remain routing defaults and are never described as access-control boundaries.

## Connection state

The backend exposes these states instead of treating an OAuth callback as success:

- `authorized`: the provider issued a grant and the canonical credential was stored.
- `projectable`: the current Hermes image has a supported adapter for the provider.
- `verified`: the target adapter completed a harmless real operation.
- `reconnect_required`: refresh or provider authorization failed.
- `browser_login_required`: the task needs website state rather than the API grant.
- `unavailable`: the provider is gated off or unsupported by the current runtime.

Only `verified` renders as ready. Providers without a working adapter cannot be newly connected.

## Provider verification contract

Every advertised provider must pass the same lifecycle against dev:

1. Authorize through the real Connections flow.
2. Confirm the canonical secret exists only in its approved store.
3. Start a cold Hermes runtime and materialize the credential for the target agent.
4. Execute one harmless provider operation through the real MCP, CLI, skill, STS, or backend adapter.
5. Exercise refresh or expiry behavior where supported.
6. Replace the runtime and repeat the operation.
7. Disconnect, revoke where supported, and prove both warm and cold execution are denied.
8. Confirm another workspace cannot retrieve or use the credential.

The repository gets one provider-independent contract harness plus the smallest provider-specific probe necessary for each rail. Live provider checks run against controlled dev accounts and are not ordinary unit tests.

## Migration sequence

### 1. Fail closed

- Reproduce the empty MCP-token handoff in a regression test.
- Stop marking a provider connected when projection or adapter support is missing.
- Hide Vercel and QuickBooks from new connections until their Hermes adapters pass verification.

### 2. Apply the AgentCore Identity decision gate

- Inspect the current AgentCore Identity API surface for grant import and individual deletion before provisioning any provider.
- If the API supports the complete lifecycle, use one dev-only bearer-token OAuth provider to prove consent, token retrieval, refresh, workspace binding, scheduled/background retrieval, disconnect, and deletion.
- If it does not, keep Secrets Manager as the authority and revisit Identity when AWS exposes a verifiable individual grant lifecycle.

The read-only gate happens before provider migrations. It does not block the fail-closed fixes or the broker and does not make Identity the system of record before the complete lifecycle passes. On 2026-09-20, the installed `boto3` AgentCore models exposed token retrieval and consent completion, but no individual grant deletion or existing refresh-token import operation. AWS documents `forceAuthentication=true` as clearing a refresh token while initiating another consent flow; it does not establish our complete disconnect and vendor-revocation contract. Therefore Identity is not the initial authority. [AWS token behavior](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-authentication.html) · [AWS provider deletion](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-delete-oauth-client.html)

### 3. Add the broker boundary

- Add the runtime credential endpoint with workspace, roster-agent, provider, and connection checks.
- Add bridge materialization and cleanup around a turn.
- Add redaction and tests proving secrets never enter logs, room messages, profile uploads, or exports.

### 4. Move existing rails

- Remote OAuth MCP: backend-owned refresh token, ephemeral bearer access token.
- Static/stdio MCP: canonical secret in Secrets Manager, ephemeral env/file delivery.
- GitHub and other static-token CLIs: prefer supported environment variables over durable CLI config files.
- Google, X, and Instagram: use provider-specific ephemeral CLI materialization with versioned rotation capture.
- AWS: retain the existing STS broker pattern.
- Backend-only providers: retain backend-only custody and prove nothing reaches Hermes.

After a provider passes, delete its old credential objects from every S3 agent profile and force the next turn to restore without them. Migration scripts are one-shot and are deleted with their tests after the backfill, per repository policy.

### 5. Make readiness visible

- Drive Connections UI from the new state model.
- Add the provider lifecycle harness and dev smoke run.
- Update the bundled Lightbulb integration reference to describe Hermes rails and remove OpenClaw-era paths.

### 6. Connect onboarding

After the first message establishes the brief, identify only the capabilities the selected agent needs. Ask an admin to authorize those capabilities, verify them through that agent, then resume the original work. Do not add a connect-everything wizard.

### 7. Revisit AgentCore Identity

Once AWS supports and Lightbulb verifies individual grant deletion and an acceptable migration path, pilot one OAuth provider behind the same runtime-facing broker interface. Expand provider by provider only when the adapter and lifecycle contract pass. Until then, do not add an Identity abstraction with no production implementation.

## Google

The current Google grant requests `gmail.modify`, full `calendar`, and full `drive`. `gmail.modify` and `drive` are restricted scopes. A public Lightbulb-managed OAuth application using those scopes through server infrastructure should be planned as requiring Google verification and an annual CASA assessment unless Google confirms an exception.

The current BYO-client model leaves OAuth-project ownership and applicable verification with the workspace owner. It remains usable for personal/few-known-user, test, and eligible internal applications, but it is not a low-friction general onboarding design.

Before proposing a Lightbulb-managed client, split Google into user-visible capabilities and request the minimum scopes needed by each. Sending mail, operating on files selected through Google Picker, and broad inbox/Drive automation have different scope and verification consequences. Broad Gmail/Drive automation may still require restricted scopes and CASA; the product must not imply otherwise.

## Acceptance criteria

- No integration can display ready without a successful adapter probe.
- No third-party refresh token or static API key is canonical in an S3 Hermes profile.
- No workspace grant is copied into six independently refreshing Hermes OAuth stores.
- Every registry provider names one credential authority and one tested adapter.
- Cold runtime replacement preserves working access without relying on a warm microVM.
- Disconnect prevents new credential delivery immediately and removes ephemeral material before another turn.
- Browser login state is never inferred from an API connection, or vice versa.
- Google setup accurately names who owns verification and when CASA is likely required.
- AgentCore Identity adoption can occur provider by provider without changing the agent-facing integration contract.

## Delivery and rollout

Each migration phase ships as a small PR with a failing test first. Changes go through CI/CD; no runtime or production server is modified directly. Dev verification uses controlled provider accounts, followed by the normal Hermes image build, pin, runtime roll, and acceptance sequence where an image change is required. Production promotion happens only after the provider lifecycle passes in dev.
