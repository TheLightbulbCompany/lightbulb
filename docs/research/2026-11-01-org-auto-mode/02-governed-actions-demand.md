# 02 - What actions companies want governed, and evidence of demand (v2, 1 Nov 2026)
Tags: [P] primary page read this session; [S] secondary/search-snippet only. Row refs R1..R9 are in section 2 and the vendor sections.

## Summary (each line cites a [P] row)
1. Coding-agent vendors gate one technical set: shell commands, file writes, network egress, MCP/tools, PR merge/CI. Claude Code managed settings sit above every user/project/CLI value; lists merge so devs can add but not remove [P: code.claude.com settings + admin-setup, V1].
2. Copilot agent has the hardest human gate: it cannot approve/merge its own PR and workflows wait for a human click [P: docs.github.com, V4]. Cursor calls its allowlist "best-effort, not a security boundary" [P: V3].
3. Business-action gates exist only as per-action flags and connector blocks: Agentforce `require_user_confirmation` / `isConfirmationRequired` [P via developer.salesforce.com snippet, V7]; Copilot Studio blocks connectors, HTTP, channels, event triggers [P, V6]. Nothing gates by intent or amount.
4. Devin guardrails can log, warn or block a MESSAGE (prompt injection, exfiltration, policy); no approval workflow documented [P: docs.devin.ai ai-guardrails, V5].
5. Gartner (26 May 2026) says one governance level for all agents fails; Level 3 approvals "degrade under ... approval fatigue" and Level 4 needs circuit breakers and rollback [P: R1].
6. Governance is lagging: Deloitte 21% mature agentic governance, n=3,235 [P: R2]; Salt: only 17.5% can automatically stop a misbehaving agent in production [P: R4].
7. Unintended-action rates: 80% (SailPoint, n=353) and 49.8% (Salt, n>380) [P: R3, R4]. Both are vendor-run, different definitions.
8. What went wrong, by frequency: unauthorized systems 39%, inappropriate data access 33%, downloads 32%, sharing 31%, credentials 23%, ordering something 16% [P: R3].
9. Developers trust agents conditionally: 48% trust AI when they can validate answers; human-in-the-loop is "the dominant mode" [P: R5].
10. Regulation pulls oversight and logging for HIGH-RISK systems only (EU AI Act Art. 12/14) [P-mirror]; NIST initiative has no deliverables yet [P: nist.gov]. No evidence anyone pays for intent-level autonomous approval.

## What companies say they want governed, ranked by frequency across surveys
Different bases; read as ordinal, not additive. (1) Privileged/sensitive DATA access and sharing: top concern for 60% (access) and 57% (sharing) of SailPoint respondents; 33% actually saw inappropriate access, 31% inappropriate sharing, 32% downloads. (2) Staying in SCOPE / unintended actions generally: 58% name it a risk; 39% saw agents reach unauthorized systems; 80% any unintended action. (3) External API calls or TRANSACTIONS: 53.9% of Salt's affected orgs (49.8% of all respondents). (4) Modify/DELETE data: 41.9% of Salt's affected orgs. (5) Credential disclosure: 23% (SailPoint). (6) Agents ORDERING things / spend: 16% (SailPoint), lowest named. (7) Ability to STOP an agent: only 17.5% can (Salt). Not asked in any survey read: outbound messages to humans, hiring/creating agents, who approved. Caveat: SailPoint's 16% "ordered something" is the only spend datapoint.

## 1. What vendors gate today
V1 Claude Code [P: code.claude.com/docs/en/settings, admin-setup]. Precedence managed > CLI > local > project > user; "Nothing in your own settings files or `--settings` overrides a managed key." Delivery: claude.ai console (hourly), plist/HKLM, `managed-settings.json`; HKCU is not enforcement. Keys: `permissions.allow/deny` (merge), `allowManagedPermissionRulesOnly`, `disableBypassPermissionsMode`, `disableAutoMode`, `sandbox.network.allowManagedDomainsOnly`, `allowManagedMcpServersOnly`, `allowManagedHooksOnly`, plugin marketplace locks, `availableModels`, `allowedProviders`, `processWrapper`. Override: only stricter values from lower scopes; a local admin can edit the managed file; cloud sessions get only server-managed settings. Docs: denying WebFetch does not stop `curl` if Bash is allowed; sandbox closes that. Spend limits, OTel, Compliance API exist; nothing per business action.
V2 Codex [P: learn.chatgpt.com/docs/agent-approvals-security]. Sandbox read-only / workspace-write / danger-full-access; approval on-request, granular, never; `auto_review` reviewer agent "denies critical-risk actions and fails closed"; network off by default, proxy allow/deny; admin `allowed_approval_policies`, `allowed_approvals_reviewers`, `guardian_policy_config`; "full sandbox escalation can bypass" the managed proxy.
V3 Cursor [P: cursor.com/docs/enterprise, /llm-safety-and-controls]. Enterprise-only: Run Mode policy, browser domain allowlist, `.cursor` protection, org-wide sandbox mode, hooks via MDM, audit logs to SIEM. MCP "don't enforce policies"; no admin network control.
V4 Copilot agent [P: docs.github.com risks-and-mitigations; firewall page S]. Human approves workflows and PR; agent pushes to one `copilot/` branch; org firewall allowlist (3 Apr 2026 changelog [S]) covers only the agent's Bash tool, not MCP or setup steps [S].
V5 Devin [P: docs.devin.ai/enterprise/features/ai-guardrails.md, enterprise-security.md]. Guardrails: block / warn / log a flagged message for prompt injection, data exfiltration, policy violation; session kill not configurable. Security page: secrets via Secrets Manager, no approval workflow, no network detail, no audit detail. API: RBAC `ManageOrgSecrets`, `ManageEnterpriseSettings` [S]. Tried: docs.devin.ai/enterprise/security (404), /enterprise/security-and-compliance/overview (404). Unread but indexed: /cli/reference/permissions.md, /cli/sandbox.md, /enterprise/security-access/custom-roles.md, /enterprise/security-access/ip-access-lists.md.
V6 Microsoft [P: learn.microsoft.com Agent 365 overview, Copilot Studio admin-data-loss-prevention, updated Oct 2026]. Copilot Studio data policies (Power Platform admin center; Business / Non-business / Blocked groups; tenant-wide, real-time, no exemptions since early 2025) let admins block: Power Platform connectors as tools (also blocks MCP tools that rely on them), HTTP requests (or allow/deny endpoints), Azure Bot Service skills, publishing to Teams/Direct Line/Facebook/Omnichannel/SharePoint/WhatsApp, event triggers (agent acting without human prompt), no-auth agents. Agent 365: registry, Entra access, Purview DLP, Defender; GA 1 May 2026, per-user, "works best" with E5; $15/user [S].
V7 Salesforce Agentforce [P: developer.salesforce.com Secure Agentforce Custom Actions + Agent Script Actions ref via search; help.salesforce.com page blank in Chrome]. Per-action `require_user_confirmation` (Agent Script) / `isConfirmationRequired` (GenAiFunction metadata); guide recommends it for any action that alters org data or sends email "on a user's behalf". Private actions: require user verification. Confirmation is by the end user in conversation, not an admin approval queue. Admin permission sets/FLS: S only.
V8 ServiceNow AI Control Tower [S snippet of servicenow.com/docs]: AI steward sign-off before model deploy; "only limited control over blocking deployments and the activation of skills and agents".

### Comparison table (B=admin can block, A=human approval step, L=log/warn only, -=none found, ?=not verified)
| Action category | Claude Code | Codex | Cursor | Copilot agent | Devin | Copilot Studio / A365 | Agentforce | ServiceNow |
|---|---|---|---|---|---|---|---|---|
| Shell/command | B/A/allow rules | B/A, auto-review | A/allowlist (best-effort) | sandbox | ? | n/a | n/a | n/a |
| File write | B sandbox+deny | B protected paths | `.cursor` only | one branch | ? | n/a | n/a | n/a |
| Network egress | B domain lock | B proxy (bypassable) | browser only | B firewall (Bash only) | ? | B HTTP/endpoints | - | - |
| MCP/tools/connectors | B allow/deny lock | ? | trust mgmt | outside firewall | ? | B connector blocks | per-action | B models+MCP |
| Merge/deploy/CI | deny rules | rules | - | A | ? | B publish/channels | - | A limited |
| Secrets | deny Read | protected .aws | - | - | secrets mgr | Entra identity | - | passport [S] |
| Spend | org limits | ? | ? | - | ? | ? | - | - |
| Outbound messages | via MCP deny | - | - | - | L/B on flagged message | B channels; DLP | A confirm (send email) | - |
| Delete/modify data | deny patterns | auto-review | - | - | - | - | A confirm (alter data) | - |
| Create/hire agents | plugin/agent-view locks | - | - | - | - | B event triggers; registry | - | A steward |
| Audit | OTel, Compliance API | ? | SIEM | PR trail | ? | Purview | Audit Trail | yes |

## 2. Demand evidence
R1 Gartner 26 May 2026 [P, read in Chrome]: four levels. L1 Observe: read-only, "baseline controls such as scoped data access, user authentication, usage logging"; L2 Advise: no write access, add accuracy/hallucination testing and "user training on appropriate reliance"; L3 Act with Approval: writes/sends/config changes only "after explicit human approval for every action", needs "clear approval workflows with audit trails" and agent incident response; L4 Act Autonomously: humans review "exceptions, audit logs and aggregated outcomes", needs "continuous monitoring, enforced guardrails, rapid rollback mechanisms, circuit breakers ... and clear ownership". The 40% (enterprises demote/decommission autonomous agents by 2027) comes from the headline/subhead via search summary [S]; Chrome page text began at Level 1. Gartner 28 Apr 2026: 13% think governance is right [S].
R2 Deloitte State of AI in the Enterprise [P: deloitte.com ai-agents-scaling-faster, 24 Apr 2026]: "Only 21% of respondents say their organizations have a mature governance model in place for agentic AI." n=3,235 IT/business leaders, 24 countries, involved in AI programs; underlying report Jan 2026. Fieldwork dates, sampling and exact question wording NOT on the page.
R3 SailPoint / Dimensional Research, May 2025 [P: report PDF text]: n=353 qualified IT/security pros, 5 continents. 80% (20% said never). Actions beyond scope: unintended systems 39%, inappropriate data accessed 33%, downloaded 32%, shared 31%, used internet 26%, revealed credentials 23%, ordered something 16%, other 4%. Concerns: privileged data access 60%, unintended actions 58%, sharing privileged data 57%, decisions on inaccurate data 55%, inappropriate access/sharing 54% each.
R4 Salt Labs, Oct 2026 [P: salt.security/api-security-trends]: 49.8% confirmed/suspected an agent acted unintended/unauthorized in 12 months; of those 53.9% external API call or transaction, 41.9% modified or deleted data; 17.5% can automatically stop a misbehaving production agent. Page gives no n or fieldwork (n>380 is from ITPro [S]); no "who approved" question. Salt 1H 2026 (8 Apr, n=327): 92% lack advanced maturity [P].
R5 Stack Overflow Developer Survey 2026 AI page [P: survey.stackoverflow.co/2026/ai]: 48% trust AI when they can easily validate answers; 16% extend trust beyond important decisions; human-in-the-loop "the dominant mode"; coding agents top use (66%), 73% of those use daily; Claude Code 66%, Copilot 59%. n stated only for other charts (10,926 daily use; 13,857 avoidance), not trust. NOTE: earlier trade coverage quoted "6.6% / 26.2% / 30,000" - not on this page; discard.
R6 Okta, McKinsey, Gravitee, NeuralTrust, CSA: secondary only (see v1 notes); not relied on.

## 3. Regulatory pull
EU AI Act Art. 14 (oversight "commensurate" with risk, autonomy, context; ability to override and stop) and Art. 12 (automatic event logs over the system lifetime) [P-mirror artificialintelligenceact.eu; EUR-Lex empty]. Apply to high-risk systems, dates Dec 2027 / Aug 2028 per mirror (unverified). NIST CAISI AI Agent Standards Initiative, 17 Feb 2026: RFI (9 Mar), identity/authorization concept paper (2 Apr); deliverables "coming months" [P: nist.gov]. ISO 42001 / SOC 2: iso.org 403, not cited. Supports audit trails of approvals more than autonomy.

## 4. Buyer and budget [S unless noted]
Agent 365: $15/user, $99 E7 (blogs). Oasis $120M B Mar 2026, Cyera LOI ~$1B; Astrix to Cisco closed 29 Jun 2026 (~$400M); Noma $100M B Jul 2025; Zenity ~$185M total (conflicting); Token $20M A; Aembit ~$45M. Mostly identity/NHI, CISO-bought; coding-agent-on-laptop coverage unverified except Zenity claims. Lasso, Prompt, Obsidian, Beacon not researched.

## 5. Incident patterns [S]
Replit Jul 2025, PocketOS (Cursor+Opus 4.6, Apr 2026, token with all rights, backups on same volume), DataTalks.Club (Claude Code ran terraform destroy from stale state): all destructive infra/data via over-broad credentials. Survey rates above (R3, R4) are the only evidence for external transactions and exfiltration. No incidentdatabase.ai records reached.

## Not verified / couldn't reach
- Gartner 40% figure: page text from Chrome did not include the intro; relies on search summary of headline.
- Deloitte: question wording and fieldwork dates absent from the page; full report not opened.
- Salt: n and fieldwork not on primary page; "who approved" not asked there.
- Salesforce: help.salesforce.com article rendered blank in Chrome; confirmation setting sourced from developer.salesforce.com snippets. No admin-level permission-set docs read.
- Devin: two guessed URLs 404; approvals, network, permissions pages indexed but unread.
- Okta, McKinsey, CSA, iso.org, EUR-Lex, SOC 2, incidentdatabase.ai, Lasso/Prompt/Obsidian/Beacon: unchanged from v1.
