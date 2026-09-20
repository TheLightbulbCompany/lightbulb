# Migration closeout and browser v2 research plan

Status: marketplace/IAM closeout implemented in PR #2808; browser v2 remains a feasibility/design task. No production deployment from this PR yet.

## Scope

Finish marketplace migration gaps, assess provisioning recovery, remove verified migration residue, and design human browser interaction with persistent authentication. Code Interpreter, desktop nodes, AgentCore Memory, OpenRouter, Shopify, ambient-gate backlog review, and general performance work belong to the other Codex session.

Use smaller subagents for bounded research. Separate source evidence, live observations, upstream reports, and proposed behavior. Do not implement a new subsystem just because an old backlog comment names it.

## Baseline and live evidence

Source baseline: `0bc33fb6a1569e63f781f69dc14fe2655e73edab`. AWS read-only verification on 2026-09-20, account `877352799272`, region `us-east-1`:

| Item | Observed |
| --- | --- |
| Dev running ECS tasks | Backend revision 234: two tasks; Paperclip revision 5: one task. Both definitions have `volumes: []`. |
| Prod running ECS tasks | Backend revision 84: two tasks; Paperclip revision 4: one task. Both definitions have `volumes: []`. |
| Provisioner definitions | Dev revision 234 and prod revision 84 both have `volumes: []`. |
| Retained EFS | `fs-0d6e8fc1351dce5c8`, available, reported 24,576 bytes, zero mount targets, zero access points. |
| Step Functions | `lightbulb-dev-provision` and `lightbulb-prod-provision` exist. Five latest returned prod executions succeeded on September 17–18. |
| Retired DynamoDB prefix | `list-tables` returned zero `isol8-*` tables. |

Conclusion: the checked Lightbulb workloads have no active EFS mounts. One historical filesystem still exists; this is not an account-wide, all-region resource audit. Its small reported size does not prove its contents are disposable. No resources were deleted.

Step Functions remains the provisioning watchdog: claim a row, start the durable execution, attempt provisioning in a backend subprocess, recover through an ECS provisioner task if necessary. The provisioner creates the Hermes AgentCore runtime. Normal agent turns do not use Step Functions. Source: `apps/backend/core/services/provision_dispatch.py`, `apps/backend/scripts/provision_runner.py`, and `apps/infra/lib/stacks/service-stack.ts`.

## Research workstreams and deliverables

### 1. Current architecture graph

- [x] Refresh Graphify's code graph after the Hermes deletion chain; preserve its semantic cache.
- [x] Report included/excluded source scope, extraction cost, and remaining stale semantic material.
- [ ] Trace message ingress, runtime invocation, profile persistence, provisioning recovery, and browser control.
- [ ] Classify every relevant EFS/OpenClaw/ECS reference as active behavior, compatibility contract, stale prose, or retained resource.

Do not treat old graph edges or names such as `openclaw_work.py` as proof of a running OpenClaw service. Graphify is the available tool; an installed tool named “archify” was not found.

Completed: `graphify update . --no-cluster` processed 1,431 uncached code files, producing 32,303 nodes and 83,072 edges, with no semantic/LLM calls (zero LLM extraction tokens). It preserved six previous graph artifacts under `graphify-out/2026-09-20/`. Semantic document relationships and community labels were not refreshed. Two TSX files were partially parsed (`ImportBackupDialog.tsx`, `HomeSection.test.tsx`); these are extraction limits, not established application defects. Sixteen metadata/config files produced no AST nodes. Source inspection independently confirms AgentCore-only runtime invocation, S3 profile state, AppSync delivery, and the active Step Functions provisioner.

### 2. Marketplace install tracking

Confirmed gap: `apps/backend/routers/marketplace_listings.py::my_installed_templates` returns an empty list. The historical `apps/frontend/src/hooks/useCatalog.ts` reads this list, but a repository-wide search found no application callers of that hook. Do not claim a currently mounted sidebar filter is restored. The live Teams roster does consume marketplace provenance. `apps/backend/core/services/catalog_service.py` documents the missing replacement for the old EFS install marker.

Correction to the initial audit: deploy retry idempotency is not absent. The router's stable Paperclip create key (listing, placement, version) and owner-wide deploy claim remain. The missing record affects installed-template discovery, marketplace-origin badges, and the early completed-install shortcut. A retry can fetch the artifact and emit another deployment event without minting a second agent. Preserve the working creation and locking mechanisms.

- [ ] Trace deploy, hire, purchase retry, deletion, reinstall, and rollback callers; identify which already have durable idempotency.
- [ ] Prefer existing purchase records or trusted roster metadata if they can represent completed installs. Compare with a backend-owned S3 marker only if necessary.
- [ ] Define authoritative listing/slug/version/agent identity, completion boundary, and deletion semantics.
- [ ] Cover concurrent duplicate requests, failed partial installs, retry after completion, deleted agents, two tenants using the same listing, and seller-controlled metadata attempting to forge installed identity.
- [ ] Verify the real API response and sidebar behavior after an install; distinguish hiding a suggestion from forbidding intentional multiple hires.

Implementation review correction: store the install marker at `marketplace-installs/<owner>/<agent>.json` in the existing state bucket, outside the runtime's `<owner>/*` S3 grant. A marker inside the agent profile would be forgeable even if ordinary runtime synchronization excludes it. Grant this prefix only to the backend; no Hermes image change is required. Read it through `agent_provenance._load_agent_sidecars` and the installed-template endpoint. Purchase rows remain entitlement records, not install identity. Write only after each caller's existing rollback envelope succeeds; do not add new routine recreation to approval hires as part of this fix. Delete metadata with the agent or account, preserve it on routine runtime reprovision, and filter reads through the live roster.

Existing focused suites: `tests/unit/services/test_catalog_service_s3_deploy.py`, `tests/unit/services/test_agent_provenance.py`, marketplace router and approval-hire tests, and frontend `useCatalog.test.tsx`.

Related audit: `/me/agents-summary` returns zero skill/channel/cron counts. Decide which counts can be derived accurately under Hermes and which unavailable capabilities should be represented explicitly rather than given invented counts.

### 3. Provisioning recovery

`provision_dispatch.py::describe_dispatch_execution` references an unbuilt SWEEP worker. This is a historical backlog item, not proof that the current watchdog fails.

- [ ] Build a failure matrix for death before execution start, inline failure, backend roll, execution timeout, closed execution with a nonterminal row, and concurrent recovery attempts.
- [ ] Map each failure to existing stale-claim logic, watchdog retries, and status-poll recovery.
- [ ] Reproduce only the uncovered cases in tests before adding a worker.
- [ ] If a periodic reconciler is necessary, reuse existing scheduling and claim fencing. A second recovery attempt must never seize a healthy provisioning run or resurrect a deliberately stopped/deleted workspace.

Acceptance: one deterministic recovery path for each demonstrated wedge, bounded retries, no duplicate provisioning, and a useful operator-visible failure state. Keep Step Functions unless a separately justified design replaces its durability.

Research recommendation: do not add a periodic worker on the evidence gathered so far. Existing dispatch tests cover watchdog attachment failure, closed executions, inline crash/timeout, queue saturation, and fencing. The specific remaining no-watchdog window is process death after claiming a row but before starting the execution. Status polling plus the stale-lease path recovers it, but unattended recovery without any poll is not guaranteed. Record that limitation explicitly; add a sweep only if no-poll recovery is a required behavior or a reproduced incident justifies it. Do not remove the stale-lease recovery.

### 4. Migration residue

- [ ] Narrow `apps/infra/lib/table-names.ts` migration-prefix IAM grants after auditing callers and rendered CDK policies. Preserve the existing policy-size and cross-stack-export constraints.
- [ ] Check dev and prod CDK diffs: no unrelated replacement, no cross-environment access expansion, no removed current table/index permissions.
- [ ] Correct EFS-era comments, browser-panel documentation, and architecture references in maintained `CLAUDE.md`; regenerate its Codex mirror with the repository's existing procedure.
- [ ] Inventory retained filesystem/image resources separately from active dependencies. Establish retention/backup requirements before proposing deletion.

Research correction: `bothTableArns` is unused (only dead imports remain); the live backend grant uses `allTableArns`. Remove the unused helper/imports independently. The candidate narrowing is the existing wildcard pair from `table/*-{env}-*` to `table/lightbulb-{env}-*`, after rendered-policy and table-consumer checks—not enumeration of every table, which previously exceeded the policy-size budget. The meter already uses explicit current ARNs.

Do not sweep live identifiers such as `X-Isol8-Public-Host`, existing session cookies, environment contracts, or stored event formats. Coordinate any rename across producers, consumers, and deployed images.

### 5. Browser v2 feasibility

Proposed experience: a human can open a browser without an active agent turn, navigate and sign in, hand it to an agent, take it back, and retain supported authentication across sessions. Workspace-shared browser sessions and saved sign-ins were approved by the user on 2026-09-20. Per-human isolation is deferred to the backlog; see `../specs/2026-09-20-browser-v2-design.md`. Cross-workspace authorization and control handoffs remain required.

Current constraints: released `agent-browser` binary, no fork/custom provider; preserve profile-based ownership checks and short-lived live-view authorization. Current v1 ends sessions with the agent turn, so user-owned lifetime is a real lifecycle change.

Source research completed: the native provider always calls StartBrowserSession; it exposes no attach-by-existing-session-id path. The app's `BrowserLiveView` component supplies only a signed URL and viewport size. The inspected SDK implementation hides the toolbar but does not explicitly disable input; therefore “watch-only” describes the current app affordances, not a proven server-enforced input lock. Verify the exact shipped SDK and live DCV behavior before making either claim.

Compare three scoped approaches:

1. **Staged authenticated browsing (recommended first feasibility target):** a human opens a backend-managed session, signs in, explicitly saves/closes it, and the CLI creates its own later session from the saved workspace profile. This avoids requiring same-session attach. It does not yet provide seamless takeover or automatic persistence of subsequent agent-side changes. It needs new backend lifecycle authorization and concurrency handling, but no patched CLI/provider. Treat this as a proposal, not a demonstrated result.
2. **Same-session handoff:** keep one existing agent-owned browser, suspend tool execution, allow human input, then reconnect/reacquire automation state. Prove this on Hermes and the released SDK before designing controls. Turn cleanup, cancellation, and idle timeout must honor human ownership.
3. **Upstream lifecycle support:** use a future official CLI release with explicit attach and save-before-stop. This best preserves the native-provider integration but depends on upstream availability; no release or timeline is assumed. Any alternative provider or patched binary requires revisiting the user's earlier constraint.

Do not equate the CLI's missing automatic save with AWS lacking a save API. Conversely, a successful manual profile save does not establish automatic end-of-turn persistence.

- [ ] **Persistence probe:** disposable dev profile; persistent cookie plus localStorage value on a controlled origin; explicit save while the session is active; verify save completion; stop; start a fresh session; independently verify both values. Confirm isolation with a second workspace. Record failures as failures, including localStorage-only failure.
- [ ] **Handoff probe:** use the exact Hermes/agent-browser stack and current DCV SDK. Pause automation; type/click as a human; return control; reconnect/reacquire the current page; successfully perform another agent action. Test navigation and login redirects, stale handles, disconnect, cancellation, and expiration.
- [ ] **Attach probe:** establish whether the released provider can attach to a human-created session without a patched binary or custom signer. If not, compare an upstream released fix with the smallest supported session integration; do not assume a backend-created browser can be joined by the current CLI.
- [ ] **Lifetime probe:** ensure ending an agent turn does not close a browser while the human controls it, and that an abandoned session is eventually stopped.
- [ ] **Concurrency probe:** serialize browser controllers and profile writes across agents and backend replicas; reject stale handoffs and prevent an older session overwriting newer authentication.

Live explicit-save probe, 2026-09-20 (`us-east-1`): a disposable profile and two fresh AWS sessions were used against `https://example.com`. A persistent cookie and localStorage value were both present before save. Save completion was confirmed. In the second session, the cookie was present and localStorage was absent at the first fully loaded page read. Both sessions were confirmed TERMINATED and the profile deleted. This verifies cookie restoration through the AWS API, not automatic CLI saving, site-specific authentication, or eventual localStorage restoration after a longer delay. The recorded result matches the upstream localStorage report; do not advertise general browser-state persistence.

Live DCV probe, 2026-09-20: exact `bedrock-agentcore@0.4.4` was installed in a disposable Vite harness. The signed DCV URL matched session `01M2ZXKDA466RPME4E7HSZ8QRP`, which AWS reported READY at 17:25:11 UTC. CDP exposed one page target; navigation, Target.activateTarget, and Page.bringToFront succeeded, and CDP read the synthetic form title/URL. DCV continued displaying New Tab. This excludes stale URL, wrong session, and an unactivated target in this probe, but does not establish the root cause or prove all Hermes handoffs fail. End-to-end input/handback remains unverified. The session was stopped, its profile deleted, and the probe tab closed. Do not ship takeover on this evidence. Cookie-only scope versus broader persistence has been presented to the user for a product decision.

Upstream facts and uncertainty:

- AWS documents explicit save/restore: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-profiles.html
- Current inspected agent-browser provider passes the profile at start but does not save it before stop: https://github.com/vercel-labs/agent-browser/blob/main/cli/src/native/providers.rs
- Takeover issue #518 concerns AWS's managed Harness; it is not proof of failure on Hermes: https://github.com/aws/bedrock-agentcore-sdk-python/issues/518
- Issue #647 reports cookie restoration with missing localStorage despite a save: https://github.com/aws/bedrock-agentcore-sdk-python/issues/647

Browser implementation begins only after these probes determine a supported integration. A final design must name the owner of start/attach/pause/resume/save/stop, the sharing policy, failure behavior, and exact authorization checks. Avoid introducing two competing session managers.

## Delivery order

1. Consolidate graph, source, and live evidence; resolve stale claims.
2. Ship independently justified marketplace and IAM/documentation changes with focused tests and review.
3. Run disposable dev browser probes; choose the smallest supported v2 design.
4. Implement that design in isolated work, including lifecycle and ownership checks.
5. Add provisioning recovery only for reproduced gaps.
6. Verify dev end-to-end, then use existing image build, pin promotion, runtime roll, and CI/CD procedures. Do not conflate a merged pin with an updated existing runtime.

Retained-resource deletion remains a separately reviewable action. Production verification must cover the deployed artifact and runtime version, not just a green local test or a successful workflow.

Delete this working plan once its scoped work ships; preserve results in PR descriptions and maintained runbooks.


## Execution results

- Published the source-reviewed architecture walkthrough, rendered SVG and filtered Graphify export in PR #2808. Opened and visually verified the diagram in the user's Chrome browser.
- Implemented protected marketplace install records, live-roster reads, approval completion checks, and agent/account cleanup. Runtime roles cannot write the record prefix. Existing migrated agents without authoritative records are not backfilled by guessing from names or purchases.
- Narrowed the live DynamoDB wildcard to the current Lightbulb environment namespace and removed unused migration helpers.
- Corrected maintained architecture documentation and regenerated AGENTS.md from CLAUDE.md.
- Local checks: 74 focused backend tests plus 2 storefront commit-order/rollback tests; 402 full infrastructure tests; exporter regression/determinism; Ruff lint and formatting. The supplementary default `tsc --noEmit` command is not a supported check for this package (infra-ci.yml documents its test-global/dependency-type problems); infrastructure verification uses the repository's synth/test workflow.
- Infrastructure CI at commit 25107c5ee passed all jobs, including image smokes. Backend CI is monitored separately; local focused results are not a claim that the whole backend suite passed.
- Browser probe refinement: the first raw-CDP/standalone-viewer results had harness/viewport uncertainty. A subsequent exact released-CLI probe, with AWS-reported viewport 1456×819 and matching session identity, did display the correct form. Automated keyboard input still was not observed in the remote form. Native UpdateBrowserStream disable/enable calls succeeded in a separate probe, but that run lacked a visible form, so takeover was not proven. These observations do not establish that the deployed v1 viewer is broken: its prior production screenshot visibly showed Example Domain. No browser-v2 controls are shipped by this PR.
