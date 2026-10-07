# AWS Security Agent against dev — runbook

Status: owner-approved for DEV only (2026-10-07). Prod is NOT a target.
Researched 2026-10-07 from the AWS user guide (the service is now branded
"AWS Security Agent (part of AWS Continuum)"; docs base
`https://docs.aws.amazon.com/securityagent/latest/userguide/`). Re-read the cited
pages before the console session: the service is new and moves.

Cost: pay-as-you-go, $50 per task-hour, metered per second; a 2-month free trial
starts at the first pentest run. Customer data is KMS-encrypted and not used for
training; test logs go to CloudWatch in our account
([FAQ](https://aws.amazon.com/security-agent/faqs/)).

## 0. Facts to know first

1. **Verifying a domain covers every subdomain.** Verifying `dev.thelightbulb.company`
   also covers `admin.dev.thelightbulb.company`. Never verify `thelightbulb.company`
   (it would cover prod hosts). Verify exactly `dev.` and `api-dev.`, and mark the
   admin host Out of scope (section 3).
2. **The dev admin surface reads PROD data.** It is the main risk of this run.
3. AWS publishes no egress IP range. Fixed IPs exist only if we attach a VPC with a
   NAT gateway and Elastic IPs; otherwise requests carry `User-Agent: securityagent`
   ([best practices](https://aws.github.io/aws-security-services-best-practices/guides/security-agent/)).
4. The Application, Agent Space, target domains and both IAM roles are infrastructure we
   define in CDK (section 8), not hand-made. The console is used only for the pentest task
   itself: the Verify click, credential selection, scope entry and starting the run.

## 1. Infra, then the console task (account 877352799272, us-east-1)

us-east-1 is a supported region. Profile `isol8-admin` (SSO). The pinned `aws-cdk-lib` 2.267.0
ships `aws_securityagent` (verified in the pnpm store: `CfnAgentSpace`, `CfnApplication`,
`CfnPentest`, `CfnSecurityRequirementPack`, `CfnTargetDomain`). CloudFormation documents
[`AWS::SecurityAgent::Application`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-securityagent-application.html)
(`RoleArn`, optional `IdCConfiguration`),
[`AgentSpace`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-securityagent-agentspace.html)
(`Name`; `AwsResources.{IamRoles,LogGroups,SecretArns,LambdaFunctionArns}`; `TargetDomainIds`) and
[`TargetDomain`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-securityagent-targetdomain.html)
(`TargetDomainName`, `VerificationMethod`: `DNS_TXT | HTTP_ROUTE | PRIVATE_VPC`).

1. Merge and deploy the infra PR (section 8) through CI. Omit `IdCConfiguration` for IAM-only
   access ([grant-user-access](https://docs.aws.amazon.com/securityagent/latest/userguide/grant-user-access.html)).
2. Console `https://console.aws.amazon.com/securityagent/` > Agent Spaces > `lightbulb-dev` >
   **Penetration test** tab. In the **Target domains** table copy the **Record name** and
   **Record value** ([enable-test-domain](https://docs.aws.amazon.com/securityagent/latest/userguide/enable-test-domain.html)),
   have the second infra PR add them to dns-stack, then select the domain and choose **Verify**.
3. Web app tab > **Admin access** > **Penetration tests** > **Create a penetration test**
   ([quickstart](https://docs.aws.amazon.com/securityagent/latest/userguide/quickstart.html)):
   name, Target URLs, service role, log group, credentials (section 4), **Network configuration**
   (Accessible / Out of scope, section 3).
4. **Create pentest**, review the scope with the owner, then **Create and execute**. Set the
   maximum task-hour limit (section 5). `CfnPentest` exists, but the owner reviews scope here.

Roles defined in CDK, per [create-iam-role](https://docs.aws.amazon.com/securityagent/latest/userguide/create-iam-role.html)
(both trust `securityagent.amazonaws.com` with `aws:SourceAccount` and `aws:SourceArn`):
- **Application role**: `sts:AssumeRole` + `sts:SetContext`; managed `AWSSecurityAgentWebAppPolicy`
  plus `logs:GetLogEvents` and `secretsmanager:CreateSecret`.
- **Agent Space service role**: `iam:GetRole`/`SimulatePrincipalPolicy` on itself,
  `secretsmanager:GetSecretValue` on `lightbulb-dev*` and our credential secret,
  `logs:CreateLogStream`/`PutLogEvents` on `/aws/securityagent/lightbulb-dev*`, and
  `lambda:InvokeFunction` on the credential Lambda only. No VPC or S3 (the VPC policy grants EC2 ENI on `*`).
- No AWS service-linked role is documented.

## 2. Targets

| Target | Verify | Notes |
|---|---|---|
| `https://api-dev.thelightbulb.company` | DNS TXT or HTTP route | API GW HTTP API to ECS FastAPI. |
| `https://dev.thelightbulb.company` | DNS TXT or HTTP route | Next.js on Vercel. **Conditional**: only with Vercel approval (below); otherwise OMIT it. |

Verification ([enable-test-domain](https://docs.aws.amazon.com/securityagent/latest/userguide/enable-test-domain.html)):
- DNS TXT: copy **Record name** and **Record value** from the Target domains table, add the
  TXT record (via CDK, section 8), choose **Verify**. **One-click verification** exists only for a domain
  "registered in Route 53 (same AWS account)"; we hold a hosted zone, so it may not apply.
- HTTP route: file `.well-known/aws/securityagent-domain-verification.json` containing
  `{"tokens": ["<token>"]}` over valid TLS. For `dev.` that means a public file in the
  Next.js app (a PR). DNS TXT is less invasive.
- Ownership is re-checked periodically during a job (third-party report, since 2026-03-03):
  keep the record until the run ends, then remove it. Max 20 target domains.

**Vercel policy** ([Penetration testing on Vercel](https://vercel.com/guides/penetration-testing-on-vercel),
updated 2026-07-30): Pro and Enterprise may test their own apps; Hobby may not; the Vercel
platform itself is off-limits. "Volumetric" testing ("a large number of requests from an
automated scanner") is Enterprise-only with advance notice through the CSM/AE. Vercel
recommends setting tester IPs to **Bypass** in Project Firewall and reverting afterwards;
we pay variable costs (bandwidth, functions). An AI pentest agent may count as volumetric, and the service has no rate setting, so
volume cannot be capped. Because AWS gives no IPs, a firewall Bypass by IP is not possible.
**Rule:** `dev.thelightbulb.company` is a target only with Vercel's written approval or
Enterprise-plan coordination with the CSM/AE. Otherwise it is OMITTED and the run covers
`api-dev` only. Our Vercel plan is an OPEN OWNER QUESTION.

**Out of scope (do not add as targets):** AppSync Events (subscribe-only Clerk OIDC, IAM
publish; not a domain web target), `ws-dev` / `bv-dev` WebSockets (device-JWT only), and
AgentCore runtimes (no public endpoint; reached only via `InvokeAgentRuntime`). Clerk
(`clerk.*`, `accounts.*`) and Stripe appear during login: mark discovered domains
**Accessible**, never targets.

## 3. HARD EXCLUSIONS (set as Out of scope)

Excluding a path also excludes everything nested beneath it
([best practices](https://aws.github.io/aws-security-services-best-practices/guides/security-agent/)).
List derived from `apps/backend/main.py` and `routers/` on 2026-10-07; re-run
`grep -n include_router apps/backend/main.py` before the run.

| Exclude | Why |
|---|---|
| host `admin.dev.thelightbulb.company`, and `dev.thelightbulb.company/admin*` | Dev admin READS PROD data (standing note). A subdomain of a verified host is in scope unless excluded. |
| `/api/v1/admin/*` (includes `/api/v1/admin/marketplace/*`) | Same data risk; wipe, refund, takedown actions. |
| `/api/v1/webhooks/*` | Clerk, Stripe (platform + Connect), SES inbound, Paperclip events, Metronome, Apple IAP: signature endpoints, noise, third-party side effects. |
| `/api/v1/billing/webhooks/stripe`, `/api/v1/hooks/*` | More signed third-party endpoints (Stripe billing, Meta, Paperclip trigger). |
| `/api/v1/debug/*` | `provision` and `user-data` destroy the workspace; the rest reprovision. Excluded by default; allow only on the throwaway org by explicit owner decision. |
| `/api/v1/launch/*` | Launch-competition writes: `referral`, `join`, `team-interest`, `connect-stripe`, `entry`, `display-name`. |
| `/api/v1/billing/checkout`, `/api/v1/billing/portal`, `/api/v1/billing/credits/top_up`, `/api/v1/billing/provider`, `/api/v1/billing/apple/*`, `/api/v1/marketplace/checkout`, `/api/v1/company/stripe/*` | Create Stripe/Apple objects, charge money, or bind keys. |
| `/api/v1/internal/*` | Hook-token machine routes (messaging runs/usage, provisioning, browser, mac-cdp, website-logins). |
| `/api/v1/auth/*` (desktop sign-in tokens), `/api/v1/desktop/*`, `/api/v1/desktop-sessions/*`, `/api/v1/desktop-execution/*` | Mint Clerk sign-in tokens, pair devices, start Mac sessions. |
| `/api/v1/oauth/*`, `/api/v1/connections*` | Real third-party OAuth flows. |
| `https://api-dev.thelightbulb.company/api/v1/users/me/delete` | Wipes the test org mid-run. |

Every row is a host, URL or path (no HTTP verb): the console takes URLs, and `*` marks everything beneath. Under `/api/v1/debug/*` the destructive routes are DELETE `provision` and `user-data`; the whole prefix is excluded.

Also exclude email, username and password change paths: "the agent could lock the
credentials it is testing". `/api/v1/messaging/*` stays in scope only if the owner
accepts the spend: each agent-room message wakes a real runtime.

## 4. Test identity

- Create a NEW throwaway Clerk org and user on the dev instance, address
  `sec-pentest+clerk_test@<our mail domain>`. Never `admin@...`: the bootstrap-admin
  collision bug (issue #2003) blocks Paperclip seeding. Free tier (`trial_local`). The dev
  Clerk instance is shared with prod previews, so no real person's email.
- Credential method. The credential **Access URL must be one of the test's target endpoints**
  ([provide-testing-credentials](https://docs.aws.amazon.com/securityagent/latest/userguide/provide-testing-credentials.html)),
  so for an `api-dev`-only run the Clerk login page on `dev.` cannot be the login. The API takes a
  Clerk session JWT and those live 60 s by default, so a static secret cannot work. Chosen path: the
  **Lambda** strategy ("dynamic credential generation", invoked synchronously, must finish in 30 s,
  receives `pentest_arn` and `actor_identifier`). A small Lambda in the infra PR reads the Clerk
  secret key (`lightbulb/dev/clerk_secret_key`) and the throwaway user id, creates a session for
  that user through the Clerk Backend API, calls `POST /v1/sessions/{id}/tokens`, and returns the
  token as plaintext. Login prompt: "Set the Authorization header to `Bearer <the credential>`
  on every request to https://api-dev.thelightbulb.company". Access URL
  `https://api-dev.thelightbulb.company`. The service role needs `lambda:InvokeFunction` only.
  **Fallback:** authenticated API testing needs the `dev.` target (and Vercel approval), using
  Secrets Manager `{"username","password"}` and the Clerk sign-in page as the login. SMS, push and
  hardware-key MFA are unsupported.
- Side effects: sign-up provisions a real workspace and AgentCore runtime (our cost) and
  spends trial OpenRouter credit (cap $0.25/day; later calls get 429 `trial_daily_cap`,
  expected, not a finding). The trial lasts 30 days.

## 5. Budget

AWS gives no per-app estimate in the docs. Its best-practices guide says an average test is
"roughly 24 task-hours" (about $1,200, "illustrative"); the quickstart says most runs finish
within 16 wall-clock hours; the minimum task-hour limit is 20.
Proposal: one pentest on `api-dev`, **limit 24 task-hours** = **$1,200** ceiling (a `dev.` pentest, if approved, adds $1,200). A reached limit stops the run and keeps findings. The first run
should fall inside the free trial: check Billing before starting. Revalidating a fix costs far less.

Alerts (budget + CloudWatch alarms) are defined in CDK, section 8.

## 6. During and after the run

- Watch: web app Run overview / Logs / Findings tabs; CloudWatch group
  `/aws/securityagent/lightbulb-dev*`; CloudTrail `securityagent.amazonaws.com`. Our side:
  ALB 5xx, ECS CPU, DynamoDB throttles. If anything pages, stop the run in the web app.
- Findings land in the web app **Findings** tab (description, severity, risk type, evidence).
  Prasiddha reads them first. Triage into one PR per finding with the evidence quoted
  (critical/high same day); nothing goes in a public issue. WAF-bypassed runs can over-report.
- Cleanup of the test org: as its sole member call `POST /api/v1/users/me/delete`
  (`wipe_own_workspace`: runs the Paperclip company deletion and removes the Aurora users).
  Do NOT use `DELETE /debug/user-data` for this: it leaves the Paperclip tenant behind. Only
  after that, delete the org and user in Clerk (dev). Then remove the TXT records, Lambda and
  (unless kept) the Agent Space stack through a CDK PR.

## 7. Unknowns (the docs did not answer)

- OPEN OWNER QUESTION: our Vercel plan and whether Vercel approves; decides if `dev.` is a target.
- Whether the Lambda credential is fetched once per run (a 60 s token would then expire) or per
  login. If once: use a dedicated Clerk JWT template with a long lifetime, if the backend accepts it.
- Whether a Clerk session token carries the org claim the API requires (`resolve_owner_id` 409s without it).
- Whether `CfnTargetDomain` exposes the TXT record value (docs list none): hence the two-step flow.
- Whether `api-dev` sits behind a WAF (branch `feat/waf-count-mode`): it may block the agent; no IPs to allowlist.
- The exact Budgets service name; the free-trial cap; whether dev Clerk bot protection blocks the sign-in.

## 8. Infra PR spec (apps/infra, dev stage only; follow-up to this docs PR)

Run `npx cdk synth` and commit the regenerated `deploy.yml`. Two deploys, because the TXT value is
only known after the TargetDomain exists.
- **New `security-agent-stack.ts`** (dev only): `CfnApplication` (RoleArn = application role, no
  IdC), `CfnAgentSpace` `lightbulb-dev` (`AwsResources`: service role, log group, credential
  Lambda ARN; `TargetDomainIds`), one `CfnTargetDomain` per host (`DNS_TXT`: `api-dev.` and, only
  if approved, `dev.`; never the apex), both roles, `/aws/securityagent/lightbulb-dev` log group.
  Plus the credential Lambda (python, Clerk BAPI calls), which reads the Clerk secret.
- **dns-stack.ts** (second deploy): one `TXT` record per host at the console's **Record name**.
  Removed by a later PR after the run.
- **auth-stack.ts**: the throwaway user's Clerk user id as a Secrets Manager secret
  `lightbulb/dev/security-agent/pentest-user` (no value in CDK; owner sets it from a mode-600 file).
- **observability-stack.ts**: an `aws-budgets` `CfnBudget` ($1,200/month, filtered to the
  AWS Security Agent service, alerts 50% actual and 100% forecast to the existing alarm SNS
  topic) and CloudWatch alarms on `AWS/SecurityAgent` `FailedPentestJobs` and
  `ConcurrentPentestJobs` (dimension `AgentSpaceId`). Dev stage only; the Budget is
  account-wide, so deploy it once.
