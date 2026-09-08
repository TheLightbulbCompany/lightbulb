# Goal prompt: OpenClaw founder upgrade and Lightbulb multiplayer

This is a proposed execution instruction for the user to submit when ready. Drafting or reading this document during preparation does not itself start the goal.

---

Create a goal to upgrade our OpenClaw fork and deliver the founder's production multiplayer experience described below. Then write the detailed implementation plans and execute them using **superpowers:subagent-driven-development**. Do not stop at research, plans, code changes, or successful builds: the outcome is reviewed, tested software working for the explicitly approved founder production target.

## 1. Approved direction and sources

Use this approved delivery plan as the starting specification:

/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/docs/superpowers/plans/2026-09-08-openclaw-9x-founder-multiplayer-delivery-plan.md

Read the access report alongside it:

/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/docs/superpowers/plans/2026-09-08-openclaw-execution-readiness.md

The actual Lightbulb checkout is:

/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync

Its remote is TheLightbulbCompany/lightbulb. The fork remote is TheLightbulbCompany/agent; an existing local checkout is /Users/prasiddhaparthsarthy/Desktop/agent. Do not assume that checkout represents the shipped fork: preparation found a detached HEAD and an old 2026.7.2-beta.3 package version. Preserve it and any user work. Use a verified isolated checkout/worktree of the correct source refs.

The old isol8.nosync path now holds planning artifacts, not a Git repository. Start execution in the Lightbulb AI project with appropriate workspace access; do not initialize a new repository over that old directory. Copy the approved source documents into the implementation worktree as a normal documentation change if needed, preserving provenance.

Reconcile current source, actual production state, open PRs and relevant prior investigations before writing tasks. Several 8.1 findings are already fixed and some earlier diagnoses were withdrawn. Do not repeat completed work or treat historical notes as current instructions.

The researched stable target was v2026.9.3. Recheck official upstream stable releases, compare any newer candidate's implications, then freeze a stable tag and commit. Record the choice. Do not track main or a floating latest image.

## 2. Product outcome and scope

Our product is a network of humans, durable agents, conversations and authorized execution locations. Preserve the distinct meanings of human, agent, session/room, run and node.

Deliver the founder runtime first, then these product slices in this preferred user-facing order:

1. Shared founder/company rooms with correct human attribution, participation, presence, typing and mentions.
2. Visible specialist handoffs with reliable parent continuation/result delivery and Paperclip lineage.
3. Persistent side conversations, session-aware panels, history, navigation and deep links.
4. Authorized device tools, followed by verified full-session worker placement.
5. Bounded Swarm and Code Mode with progress, cancellation, approvals and cost visibility.
6. Per-person provider accounts and explicit session account/payer/fallback behavior.
7. Workshop review integration, revocable public text sharing, Slack stop/title integration, and applicable model/usage/recovery/diagram/browser improvements.
8. Meeting-library and optional Team Reports integration as separately gated founder feature slices, with authorized sources and persistence.

Track every approved feature in a provenance and delivery matrix: already shipped in 8.1, added/changed in the frozen target, fork adaptation, configuration, backend, client UI, infrastructure, tests and production evidence. A disabled flag, absent credential or unimplemented screen is not a completed feature.

Confidential invite-only group rooms and cross-company agent exchange are the next architecture milestone in the approved plan. Produce concrete scoped designs and proof requirements, but do not enable new trust boundaries or connect other organizations without separate approval. Do not silently represent these roadmap designs as a working production federation feature.

The founder pilot may use explicitly company-readable rooms among trusted colleagues. Existing personal history must remain private under its established product contract. If implementing the approved capabilities requires changing that contract, stop for a decision.

## 3. Authority and boundaries

When I submit this prompt for execution, I authorize:

- Scoped implementation, dependency installation required by the approved work, local tests, isolated worktrees, commits, feature-branch pushes and PR creation in the two named repositories.
- Existing CI/build workflows, candidate image publication and isolated dev deployment/testing, after verifying their actual targets and side effects.
- Synthetic test data and controlled, low-risk test runs using existing development resources and configured spend limits.
- Read-only inspection of the named Lightbulb AWS account, founder runtime, configuration metadata and relevant operational evidence.
- Necessary non-production merges after review and required checks pass, only if the merge cannot activate production changes implicitly.

Before the first production-affecting merge, shared backend/frontend deployment, or founder image/config/state change, present one release approval package and ask for my approval. It must identify the exact PR/commit/image, shared-service blast radius, founder owner/service, expected interruption, backup/restore evidence, acceptance tests, cost implications and rollback/forward-repair decision. A founder-only image change does not make shared application deployments founder-only.

After that approval, continue autonomously within the approved release scope. Ask again if the scope, security/privacy contract, costs, recovery point or production blast radius materially changes.

Do not upgrade other production owners or change the new-user production image default under this goal. Do not publish transcripts, connect real third-party accounts, send messages to real people, buy credits, increase resource/spend limits, weaken IAM/network/auth controls, or perform destructive restoration without the specific authority those actions require. Use test fixtures or explicit user consent for consequential feature tests.

Never bypass sandbox approvals, branch protection, identity checks, billing limits or failing release gates. Do not use blanket full-disk/admin permission changes as an access fix. Do not expose secrets in commands, logs, reports, screenshots or transcripts. Request interactive login rather than pasted credentials.

## 4. Access preflight: prove capability, do not infer it

Revalidate these before their dependent work:

- Git/worktrees and correct remotes, including dirty/untracked user work and current open PRs.
- Repository-pinned package managers and supported Node/Python versions; required test/build tools.
- GitHub CLI/connector access, required workflow permissions and available build runners.
- Chrome page listing, a real page snapshot, authenticated dev/founder identity and organization, and an approved second test identity for multiplayer.
- AWS STS identity, scoped ECS/ECR/log/state access, build/pull/publish permissions where needed, and the actual founder service/task/digest.
- Docker or the established CI build alternative. Local Docker is arm64; production image qualification must cover the actual target architecture.
- Existing secure paths for database/config/state inspection and backup/restore, relevant provider test credentials, Paperclip, Slack, and paired devices.

AWS requests must explicitly use profile **isol8-admin** and region **us-east-1**, and verify account **877352799272**. The default profile was observed pointing to a different account. Fail closed on an account mismatch; never rely on ambient defaults for deployment.

Preparation identified founder owner org_3H3viGAfq2SXc9dIdcatmGXg8Td. Treat this as a candidate locator, not deployment authority. Reconfirm the current Clerk organization/owner, container row, exact ECS service and intended founder before mutation. Do not accept first-organization or personal-user fallbacks.

Preparation found ECR repository lightbulb/openclaw-extended is MUTABLE despite workflow commentary claiming otherwise. Resolve artifact-integrity guarantees deliberately. Do not assume a tag is immutable or silently change a shared repository policy. Preserve explicit release metadata and verify digests.

A successful read or an administrator-named role does not prove deployment permissions. A browser landing page does not prove authenticated application access. Expiring SSO sessions must be refreshed through the supported flow.

### Authorized dev authentication and testing

I authorize you to use the dev Clerk key stored in AWS Secrets Manager at lightbulb/dev/clerk_secret_key with the verified Lightbulb AWS account. Read it directly into process memory, require a development/test key, and use Clerk's documented test-user authentication methods. Do not display, commit, write plaintext credential files, or place the key or sign-in/session tokens in shell/model-visible tool arguments or reports. Normal browser-managed session state is permitted. Do not fetch or use the production Clerk secret for this purpose.

Prefer an existing +clerk_test account. Use reserved test-address verification or a short-lived supported test sign-in token as appropriate; do not weaken authentication settings or bypass a provider challenge. You may create isolated dev test users when a scenario needs them, keeping a record of test data and resources without deleting pre-existing users or work.

You may use the free trial in dev to test real onboarding and container provisioning through the existing product flow. Stay within configured trial/spend limits and do not reset or extend trials to evade them. Avoid leaving unnecessary running test resources after the scenario, and do not delete material data without a recoverable, approved cleanup scope.

If a real dev user is needed, first check whether my personal-email account already exists in the dev Clerk instance and whether its ChatGPT connection is usable. You may use that account and our existing Sign in with ChatGPT flow for the necessary dev test. Preserve my existing account/data/connection; let me complete interactive OpenAI login or consent when required. This does not authorize purchases, new paid subscriptions, credential extraction or changes to my production account.

These are dev-specific permissions. Keep the already authenticated founder production browser session separate, and use an approved second test identity for multiplayer. Trial onboarding, pre-existing-account login and provider-authentication tests are distinct scenarios and must not be reported as interchangeable evidence.

If a required capability fails, report the exact missing capability without dumping secrets. Continue independent safe work; do not fabricate verification or redesign around disabled access controls.

## 5. Plan and execute with subagents

Use the applicable brainstorming/design, writing-plans, worktree, TDD, subagent-driven-development and verification/review skills. The approved direction above does not remove the need to resolve concrete interface/security design gates. Do not restart a broad brainstorming exercise for decisions already settled.

Decompose the program into dependency-ordered implementation plans that each produce independently testable software. The existing delivery document is the roadmap/specification, not a substitute for actionable tasks.

Each task must contain exact affected files, interface contracts, required behavior, regression cases, commands, expected outcomes and acceptance evidence. Perform a cross-task file/interface conflict scan before implementation. Identify the supported schema/auth/storage combinations explicitly rather than leaving placeholders for an implementer to guess.

Act as the controller:

- Use a fresh implementer with a narrowly constructed context per independently reviewable task.
- Give implementers bounded requirements, required interfaces, relevant global constraints and report paths, not the entire conversation.
- Keep one implementation writer at a time in a shared worktree. Parallelize independent read-only research/review when useful; do not allow conflicting edits or concurrent deployments.
- Require implementation tests and self-review, then an independent task review with both specification-compliance and code-quality verdicts.
- Fix and re-review substantive findings before dependent work relies on them. Perform a final whole-branch/integration review before release.
- Choose explicitly supported models/efforts proportionate to task risk; reserve strongest review for auth, privacy, concurrency, state migration and whole-release judgment.
- Keep durable per-plan ledgers: task status, commits, review decisions, test evidence, deployment records, accepted limitations and next action. Resume from the ledger after compaction rather than redoing completed tasks.

Preserve the goal's ledgers and release evidence for handoff; do not automatically delete them at completion. Review-loop limits do not authorize shipping a known security, data-loss or load-bearing correctness defect. Record contested/deferred findings honestly and obtain the necessary decision when they block release.

Do not ask “should I continue?” between ordinary tasks. Make reasonable in-scope implementation decisions and record their tradeoffs. Ask when new authority or a material product/security choice is needed; persistence does not broaden authority.

## 6. Non-negotiable engineering and rollout gates

Preserve:

- Clerk/Lightbulb human admission and spend control, OpenClaw canonical sessions/runtime, Paperclip durable work ownership, and owner-isolated ECS service boundaries.
- Local SQLite/WAL plus complete Litestream restoration, with durable Workshop files and consistent per-agent ownership.
- Existing RPC default-deny policy, approvals/questions, credential redaction and safe provider/browser/device behavior.
- Old-client and mixed-runtime compatibility until explicitly retired.

Implement and prove:

- Carried-fork-patch ledger and regression evidence; compatible plugin/schema/runtime builds.
- Central per-owner release/capability resolution used by config and every relevant container lifecycle path.
- Dual old/candidate schemas and staged config activation after the old writer stops.
- Founder release persistence across restart, resize, park/wake and stopped-service reprovision.
- Consistent pre-upgrade DB/files/config backup separated from new replica history, and a rehearsed state-aware restore.
- Native per-human identity compatible with service/Paperclip/node paths. No cosmetic author field over a shared admin connection and no blind trusted-proxy/token config flip.
- Safe legacy creator/visibility handling before native company-session discovery.
- Honest company-readable versus private/public audience labels. Native room membership alone is not an invite-only read ACL.
- Authorized subscriptions, history recovery, idempotency and concurrent-send handling.
- Deterministic device binding: do not present per-turn selection over a session-wide execNode race.
- Explicit Swarm/depth/concurrency policy and nested-run/tool spend/approval enforcement.
- Secure personal account attachment and visible payer/fallback behavior.
- Narrow publication/worker ingress only; do not expose the entire gateway.
- No new container per conversation, second transcript database, generic gateway mesh, or replacement of the existing ECS/Paperclip control plane without a justified approved need.

Complete clean-owner and representative migrated-state dev qualification before founder production. Confirm the final ECR/running image digest and real workload behavior—not just CI green, ECS RUNNING, a health endpoint, or an update row labeled applied.

For any rollback after founder use, preserve and account for newly created work and external task side effects. Do not silently restore an older snapshot over new data.

## 7. Acceptance, persistence and completion

The founder runtime milestone is complete only after live image/config verification, critical existing workflows, lifecycle durability, migration evidence and rollback rehearsal.

The full founder feature milestone must demonstrate founder plus a second trusted person collaborating with correct authorship, Friday delegating, persistent child conversations, approved device work, human approvals, reconnect/replacement recovery, one correctly linked deliverable, bounded Swarm cancellation, explicit account routing, durable Workshop changes and public-share revocation using approved test content. Test private-history protection, organization removal and cross-owner rejection, not just happy paths.

Web, desktop and mobile must either implement an enabled feature correctly or clearly gate it without corrupting routing or history. Report any unimplemented platform surface explicitly.

Observe the approved founder release through a representative work cycle and the specified failure/recovery scenarios. If observation requires scheduled continuation, use the product's supported monitoring mechanism within the approved scope; stay quiet on unchanged state and report meaningful failures, completion or required decisions. Do not claim unattended monitoring without actually arranging it.

Maintain a completion matrix for every approved feature. Unavailable integrations, missing consent and deferred network/confidential-room designs stay visibly pending or excluded by explicit scope—not quietly “done.”

Mark the goal complete only when its approved founder deliverables and production gates are genuinely satisfied. Do not substitute a merged PR, successful build, disabled flag or release-ready report for a live outcome.

Final handoff must include source refs and image digests, exact founder scope, PRs/commits, feature/platform availability, config/migration decisions, test and browser evidence, operational metrics, rollback instructions, remaining risks, and recorded rulings. Leave the rest of production and the new-user default unchanged unless I separately approve a wider rollout.

Start by verifying access and current state, reconciling the approved plan, and producing the first concrete implementation plan. Continue into the authorized work using the workflow above.
