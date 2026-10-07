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
4. The run is autonomous and exercises live functionality. There is no request-rate
   setting; if it causes problems, stop the run.
5. This is a hand-done console change, which the repo's Critical Rules otherwise
   forbid. It is a one-time owner-approved exception: afterwards record the agent
   space, roles and DNS record in CLAUDE.md (AWS Configuration), or delete them (section 6).

## 1. Console steps (account 877352799272, us-east-1)

us-east-1 is a supported region. Profile `isol8-admin` (SSO). Source:
[Quickstart](https://docs.aws.amazon.com/securityagent/latest/userguide/quickstart.html).

1. Open `https://console.aws.amazon.com/securityagent/` in us-east-1; choose **Set up AWS Security Agent**.
2. Create ONE Agent Space named `lightbulb-dev`. The docs: an Agent Space "should be
   specific for every application you want to secure"; its name shows in the web app
   and prefixes the log group.
3. Under *User access configuration* choose **IAM-only access**. Console users open the
   web app with **Web app tab > Admin access**
   ([grant-user-access](https://docs.aws.amazon.com/securityagent/latest/userguide/grant-user-access.html)).
   Skip IAM Identity Center (one operator).
4. Choose **Set up AWS Security Agent**: "AWS Security Agent creates your Agent Space
   and establishes a web application".
5. Agent Spaces > `lightbulb-dev` > **Penetration test** tab > **Set up penetration test**:
   enter the target domain, pick a verification method, verify (section 2).
6. In *Configure additional capabilities* keep **Service access** pre-configured ("creates a
   service role with the required permissions"). Attach the CloudWatch log group and the
   credential secret (section 4).
7. Web app tab > **Admin access** > **Penetration tests** > **Create a penetration test**:
   name, Target URLs, service role, log group, credentials, **Network configuration**
   (Accessible / Out of scope), additional configuration.
8. **Create pentest**, review the scope with the owner, then **Create and execute**. Set the
   maximum task-hour limit (section 5).

### IAM roles it creates
Names are console-generated, not fixed. Per
[create-iam-role](https://docs.aws.amazon.com/securityagent/latest/userguide/create-iam-role.html):
- **Application role** (one per app): trusts `securityagent.amazonaws.com`
  (`sts:AssumeRole` + `sts:SetContext`, scoped by `aws:SourceAccount`). Policies: managed
  `AWSSecurityAgentWebAppPolicy` plus `logs:GetLogEvents` and `secretsmanager:CreateSecret`.
- **Agent Space service role** (one per space): `<role>-SecurityAgentBasePolicy` (read own
  role, read secrets named `lightbulb-dev*`, write `/aws/securityagent/lightbulb-dev*` logs),
  plus `-StaticCloudWatchPolicy` / `-SecretsPolicy` for what we attach.
- No AWS service-linked role is documented. Record the generated ARNs after the run.
- Do NOT attach a VPC, Lambda or S3 bucket (the VPC policy grants EC2 ENI actions on `*`).

## 2. Targets

| Target | Verify | Notes |
|---|---|---|
| `https://api-dev.thelightbulb.company` | DNS TXT or HTTP route | API GW HTTP API to ECS FastAPI. |
| `https://dev.thelightbulb.company` | DNS TXT or HTTP route | Next.js on Vercel; see the Vercel policy below. |

Verification ([enable-test-domain](https://docs.aws.amazon.com/securityagent/latest/userguide/enable-test-domain.html)):
- DNS TXT: copy **Record name** and **Record value** from the Target domains table, add the
  TXT record, choose **Verify**. **One-click verification** exists only for a domain
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
we pay variable costs (bandwidth, functions). An AI pentest agent may count as volumetric.
Because AWS gives no IPs, a firewall Bypass by IP is not possible. Action: confirm our
Vercel plan; if not Enterprise, get Vercel's written OK or keep the `dev.` run low-volume.

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
| `/api/v1/debug/*` | `DELETE /debug/provision` and `/debug/user-data` destroy the workspace; POST/PATCH reprovision. Excluded by default; allow only on the throwaway org by explicit owner decision. |
| `/api/v1/launch/*` | Launch-competition writes: `referral`, `join`, `team-interest`, `connect-stripe`, `entry`, `display-name`. |
| `/api/v1/billing/checkout`, `/billing/portal`, `/billing/credits/top_up`, `/billing/provider`, `/billing/apple/*`, `/api/v1/marketplace/checkout`, `/api/v1/company/stripe/*` | Create Stripe/Apple objects, charge money, or bind keys. |
| `/api/v1/internal/*` | Hook-token machine routes (messaging runs/usage, provisioning, browser, mac-cdp, website-logins). |
| `/api/v1/auth/desktop/*`, `/api/v1/desktop/*`, `/api/v1/desktop-sessions/*` | Mint Clerk sign-in tokens, pair devices, start Mac sessions. |
| `/api/v1/oauth/*`, `/api/v1/connections*` | Real third-party OAuth flows. |
| `POST /api/v1/users/me/delete` | Wipes the test org mid-run. |

Also exclude email, username and password change paths: "the agent could lock the
credentials it is testing". `/api/v1/messaging/*` stays in scope only if the owner
accepts the spend: each agent-room message wakes a real runtime.

## 4. Test identity

- Create a NEW throwaway Clerk org and user on the dev instance, address
  `sec-pentest+clerk_test@<our mail domain>`. Never `admin@...`: the bootstrap-admin
  collision bug (issue #2003) blocks Paperclip seeding. Free tier (`trial_local`). The dev
  Clerk instance is shared with prod previews, so no real person's email.
- Credential method: the service supports Input credentials, Secrets Manager, Lambda and
  IAM role ([provide-testing-credentials](https://docs.aws.amazon.com/securityagent/latest/userguide/provide-testing-credentials.html)).
  Pick **Secrets Manager (static)**: secret `lightbulb/dev/security-agent/pentest-user`
  as `{"username":"..","password":".."}`, same account and region, attached to the Agent
  Space so the console grants `GetSecretValue`. A Lambda buys nothing: Clerk sign-in tokens
  are one-time and we would have to build the function. The password is created from a mode-600
  file I open (secrets-via-opened-file rule); never pasted in chat or the console form.
- Login prompt: "Go to https://dev.thelightbulb.company/sign-in, enter the username and
  password from the secret, and submit." Access URL `https://dev.thelightbulb.company`.
  SMS, push and hardware-key MFA are unsupported; TOTP and email MFA are. Use **Save and test**
  to list the domains the login reaches.
- Side effects: sign-up provisions a real workspace and AgentCore runtime (our cost) and
  spends trial OpenRouter credit (cap $0.25/day; later calls get 429 `trial_daily_cap`,
  expected, not a finding). The trial lasts 30 days.

## 5. Budget

AWS gives no per-app estimate in the docs. Its best-practices guide says an average test is
"roughly 24 task-hours" (about $1,200, "illustrative"); the quickstart says most runs finish
within 16 wall-clock hours; the minimum task-hour limit is 20.
Proposal: two separate pentests (`api-dev`, `dev`), **limit 24 task-hours each**, ceiling 48
task-hours = **$2,400**. A reached limit stops the run and keeps findings. The first run
should fall inside the free trial: check Billing before starting. Revalidating a fix costs far less.

Alerts: an AWS Budgets cost budget (Billing > Budgets) filtered to the AWS Security Agent
service, $1,200 per month, alerts at 50% actual and 100% forecast to the existing alarm SNS
topic (exact Service name unverified until usage appears). Also a CloudWatch alarm on
`AWS/SecurityAgent` `FailedPentestJobs` and `ConcurrentPentestJobs` (dimension `AgentSpaceId`).

## 6. During and after the run

- Watch: web app Run overview / Logs / Findings tabs; CloudWatch group
  `/aws/securityagent/lightbulb-dev*` (stream prefix `lightbulb-dev`); CloudTrail source
  `securityagent.amazonaws.com`. Our side: ALB 5xx, ECS CPU, DynamoDB throttles, Vercel
  firewall. If anything pages, stop the run in the web app. A failed **Preflight** tab check
  stops the run before testing.
- Findings land in the web app **Findings** tab (description, severity, risk type, evidence).
  Prasiddha reads them first. Triage into one PR per finding with the evidence quoted
  (critical/high same day); nothing goes in a public issue. WAF-bypassed runs can over-report.
- Cleanup of the test org: `DELETE /api/v1/debug/provision`, then `DELETE /api/v1/debug/user-data`
  from the org's own session, then delete the org and user in Clerk (dev). Then delete the
  secret, the verification TXT record or file, and (unless kept) the Agent Space and roles.
- Record in CLAUDE.md: agent space id, role ARNs, verified hosts, run date.

## 7. Unknowns (the docs did not answer)

- Exact generated role names; whether any AWS service-linked role exists.
- Our Vercel plan, and whether this run counts as volumetric.
- Whether Route 53 one-click verification works for our hosted zone.
- The exact Budgets service name; whether the free trial caps at some task-hours.
- Whether `api-dev` sits behind a WAF (branch `feat/waf-count-mode` suggests so): it may
  block the agent or skew findings, and AWS publishes no IPs to allowlist.
- Whether dev Clerk has password sign-in on, and whether bot protection or captcha stops the agent.
- Whether the agent can test the API with a Bearer token beyond the browser login.
- The current supported-region list and task-hour pricing beyond the trial.
