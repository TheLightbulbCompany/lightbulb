# Agent authorization / approvals / governance: startup and vendor landscape (2 Nov 2026)

Tags: [P] = primary page fetched this session (company site, docs, press release, repo, investor post). [S] = search summary or secondary only. Funding figures tagged [S] unless the primary release was fetched. Does not repeat 04-existing-products.md.

## 1. Summary findings

1. The money is in two places: developer "action layer" auth (Arcade $60M Series A, Jun 2026, lead SYN Ventures [S]; Keycard $38M, Oct 2025, a16z+Boldstart seed, Acrew A [P boldstart post]) and enterprise NHI/agent security (Oasis $120M B Mar 2026 [P]; Zenity $125M C Aug 2026 [P intelcapital]; Noma $100M B [S]).
2. Acquirers are buying the access-decision layer: CrowdStrike to buy SGNL ~$740M (Jan 2026) [S]; Snowflake to buy Natoma, an MCP gateway "enforcing identity, policy, audit at the tool-call level" (27 May 2026) [S]. Standalone authz is being absorbed, not left alone.
3. Per-user "agent can only do what its user can do" is the standard pitch now: Arcade ("only where the user's own rights and the agent's scope overlap") [P], Keycard ("agents can't access what their user can't") [P], Scalekit, Okta XAA [P], Pomerium [P]. This is not a gap anymore, but only inside one tenant/IdP.
4. Delegation chains exist at the token layer: Keycard Multi-Agent Apps (15 May 2026) uses RFC 8693 token exchange, narrows permissions each hop, preserves chain to originating user [P helpnetsecurity]. Closest thing found to person-to-person-agent delegation; it is one customer's identity domain.
5. Operand binding (amounts, recipients) is almost absent. Arcade's page names egress limits, rate limits, "blast-radius caps" but does not say it inspects arguments [P]. Keycard docs list identity, group, delegated authority as policy inputs, not tool arguments or resources [P]. Composio Firewall evaluates "tool requests and results" with decision models [P], so semantic not numeric. Only Paperclip binds an approval to hashes of the arguments (code, below).
6. Intent is a marketing word, mostly detection: Oasis "intent-based, just-in-time access" evaluates "a system's intended task" [P]; Zenity "intent-aware detection" and "deterministically allows, modifies or blocks an action before it occurs" [P]; Token's "Privilege Guardian" lets users define an agent's purpose [P]. No page shows intent compared against a specific amount or recipient.
7. Human approval as a standalone product did not become a company. HumanLayer (YC) now sells a "multiplayer coding agent IDE and cloud"; its repo has 12.7k stars and mentions no approval SDK [P]. gotoHuman survives as a niche approvals inbox, no price shown [P]. Approvals live inside Auth0 (CIBA async authorization) [P], Paperclip, Okta, Microsoft, LangGraph.
8. Orchestrators built approvals as an internal feature with one approver domain: Paperclip (MIT, ~110k stars) has company/agent/project budget hard stops and board approval, in code [P]. OpenClaw states it "is not a hostile multi-tenant security boundary" [P] and its exec-approval layer has repeated CVEs [S].
9. Big IAM incumbents ship "agent identity" as inventory plus ownership plus revoke: SailPoint (human owner per agent, review and revoke; no on-behalf-of stated) [P]; CyberArk now Palo Alto "Idira Secure AI Agents" (discover, task-duration access, audit; no on-behalf-of) [P]; Okta Agent Gateway/FGA per tool call, Agent as Principal [P], GA status unstated.
10. Detection-first vendors are well funded and now claim blocking: Zenity, Straiker ("detects and blocks", kill switch) [P]. Which block per action versus per prompt is not shown on any page.
11. Pricing signal: developer tools price per tool call (Arcade $0.01/call, Team $25/mo; Composio Pro $29/mo, Firewall $0.50 per 1M tokens evaluated [P]); security vendors are demo-only (Keycard, Oasis, Zenity, SailPoint, Idira: no price shown). Arcade lists governance (SSO, RBAC, audit) only on Enterprise [P].
12. Investors name identity/trust as the layer, not "approvals": boldstart "the missing trust layer" [P], a16z via Zane Lackey "the Auth0 moment for agent access" [S], Menlo on Clerk [P]. No primary Lightspeed/Greylock/Sequoia thesis on agent authorization was found.

## 2. Tables

### A. Auth and permissions for agents (developer-facing)

| Company | a. Wedge (their words) | b. Enforced vs observed | c. Cross-person / on-behalf-of | d. Intent / operands | e. Traction | f. Stage |
|---|---|---|---|---|---|---|
| Arcade.dev | "secure action layer for production AI agents" [S]; runtime that "enforces, executes, and governs agent actions" [P arcade.dev] | Enforced at tool call, before downstream; default deny [P arcade.dev/blog/one-question-every-tool-call] | Per-user: "act on behalf of the authenticated user, never through broad service accounts"; only overlap of user rights and agent scope. No cross-person delegation found [P] | Names egress limits, rate limits, blast-radius caps, DLP/PII redaction; argument inspection not stated; "Contextual Access" policy undescribed [P]. No human approval mentioned [P] | $60M Series A Jun 2026, lead SYN Ventures, Morgan Stanley and Wipro strategic [S: Business Wire 403, via search]. Logos: LangChain, Relevance AI, Snyk, Duracell, Madrigal [P]. Pricing: Free 2,000 calls; Team $25/mo + $0.01/call; Enterprise custom, governance only here [P arcade.dev/pricing] | Shipped |
| Keycard | "runtime authorization for AI agents"; per-agent identity, short-lived credential only if policy approves [P keycard.ai] | Enforced at token issuance (STS); traffic goes direct to resource unless optional gateway used [P] | Yes in-domain: policy sees person identity/groups + agent delegated authority; 15 May 2026 multi-agent delegation via RFC 8693, narrows per hop, chain back to originating user [P] | Policy inputs listed: person, groups, agent identity, delegated authority; tool arguments/resources not listed; step-up/human approval not on the page read [P docs.keycard.ai] | $38M: $8M seed co-led a16z + Boldstart, $30M A led Acrew, Oct 2025 [P boldstart; lead split disputed in press]. Named customer: Chime [P]. No price shown [P] | Early access Oct 2025 [S]; SDKs/product live May 2026 [P] |
| Composio | tool integration with managed OAuth for agents [P pricing] | Observed + enforced: Firewall add-on uses decision models to check tool requests/results vs policy [P] | Shared connections (org members reuse one account) via access control; no per-person intersection stated [P] | Semantic model check, no operand rules shown [P] | $25M Series A led by Lightspeed, 22 Jul 2025 [S]. Hobby $0 (100K calls), Pro $29/mo, Enterprise custom; Firewall $0.50 per 1M tokens [P] | Shipped |
| Auth0 for AI Agents (Okta) | Agents act "on behalf of users" across first/third-party APIs; Token Vault, async authorization (CIBA) for human-in-the-loop, FGA for RAG [P auth0.com/ai, auth0.com docs] | Token issuance + FGA checks; CIBA = ask human | Yes: on-behalf-of via Token Vault; agent modeled as distinct principal [P] | FGA relationship-level; no operand binding stated | Incumbent; no agent-specific numbers [P]. Pricing: "Talk to sales" [P] | GA status not stated on either page [P] |
| Okta (XAA, Agent Gateway, FGA) | "Cross App Access" short-lived scoped tokens for cross-app and agent-to-agent; Agent Gateway brokers credentials, logs tool calls [P okta.com/solutions/secure-ai] | Enforced per tool call (FGA, gateway), kill switch, real-time revoke [P] | XAA "preserves verified user context across chained workflows"; "cannot reach resources the delegating user cannot" [P]. Within Okta-managed apps | Relationship-based per tool call; no amounts | 25+ platform integrations for XAA [P] | GA dates not stated; roadmap disclaimer [P] |
| Stytch Connected Apps | Make your app an OAuth/OIDC authorization server so users can share data with agents/apps [P docs] | Consent + scopes at token issuance | User consent flow; no agent-specific features on page [P] | Custom scopes/resources | Not stated on page. Whether Stytch is still independent: not verified | Shipped |
| WorkOS (AuthKit for MCP) | AuthKit as OAuth server for your MCP server, CIMD + DCR, PKCE [P docs] | Token-level; resource indicators set `aud` | "Cross App Access" (early access) via ID-JAG; no on-behalf-of delegation described [P] | Only `openid/email/profile/offline_access` scopes listed [P] | No numbers fetched | Shipped; XAA early access |
| Descope | Identity infrastructure for building agents/MCP servers; consent, token mgmt, scope-based access [P descope.com] | Scopes + step-up for sensitive actions; mechanism for agents unspecified [P] | Not described | Scopes only | Logos: GoFundMe, Databricks, Navan etc. (none cited for agents) [P]. Pricing not on page | Shipped (Agentic Identity Hub page 404, not read) |
| Permit.io | "Fine-grained authorization for AI applications": prompt filtering, RAG data, external access with human approval for critical ops, response filtering [P permit.io/ai-access-control] | Enforced via PDP (5.6 ms P99 claimed) | "Request lineage": chain of on-behalf access across AI-to-AI interactions [P] | RBAC/ABAC/ReBAC; no amounts stated | SOC 2 Type II, HIPAA BAA [P]. Funding/pricing not fetched | Shipped |
| Cerbos | Authorization PDP for apps, workloads, "AI agents"; YAML policies, sub-ms decisions, RAG data filtering [P cerbos.dev] | Enforced at PDP call | Principal/resource/context; NHI supported; no delegation chain stated | ABAC conditions possible on context; agent-specific operands not shown | OSS PDP + Cerbos Hub; stars/funding/pricing not fetched | Shipped |
| Scalekit (AgentKit) | MCP gateway that holds per-user credentials so agents act as the user [P scalekit.com] | Enforced at gateway ("doesn't rely on instructions in your prompt"); SIEM-streamable logs | Per-user; each run limited to access its user granted [P] | Tool scoping per agent; no operand rules | Testimonials: Napkin.ai, Z47, Sprouts.ai. No funding, no price on page [P] | Shipped |
| Pomerium | "Least Agent Privilege": identity + tool policy on MCP requests, deterministic, outside the agent [P pomerium.com] | Enforced per request (PPL YAML, method/path) | Per-person permissions in "multiplayer sessions"; short-lived task-scoped sessions [P] | HTTP method/path rules, e.g. allow read, deny restart | Apache-2.0; logos incl. Mercedes-Benz, OpenAI, NVIDIA, Uber [P] (logos not labelled paying). Stars/price not on page | Shipped |
| SGNL | Runtime access enforcement between IdPs and resources for users, NHIs, agents [S] | Enforced (continuous/dynamic authorization) | not verified | not verified | CrowdStrike to acquire ~$740M, 8 Jan 2026 [S] | Acquired (closing FQ1 2027 [S]) |
| Natoma | MCP gateway enforcing identity, policy, audit per tool call [S] | Enforced | not verified | not verified | Snowflake definitive agreement 27 May 2026, terms undisclosed, ~20 staff [S] | Being acquired |
| Clerk | Identity for the AI era; Agent Toolkit (AI SDK, LangChain, MCP) [P Menlo post] | Auth layer | Agents "authenticate on behalf of users" per Menlo [P] | none | Menlo-led Series C, 15 Oct 2025 [P] | Shipped |
| Nango, Strata, Oso | not fetched (oso.com did not resolve; others not attempted) | | | | | unverified |

### B. Human-in-the-loop and approvals as a service

| Company | a. Wedge | b. Enforced/observed | c. Cross-person | d. Operands | e. Traction | f. Stage |
|---|---|---|---|---|---|---|
| HumanLayer | Now "multiplayer coding agent IDE and cloud" [P humanlayer.dev]. Originally an approval SDK; repo no longer mentions it [P github] | Steers coding agents; not an approval gate on the page | Team sessions | none | YC and others listed, no amounts; 12.7k stars, Apache-2.0; Pro $100/user/mo [P] | Shipped, pivoted |
| gotoHuman | "human supervision of AI agents": custom reviews to approve and edit AI actions; Agent Inbox, Slack/email, webhook back [P gotohuman.com] | Pauses workflow until human responds | Reviewer is a named human; no authority model | Review content is whatever workflow sends | Logos incl. Deloitte, Carrefour; PayFacto case study (40 hrs/month) [P]. No price/funding on page | Shipped |
| Auth0 CIBA | Async authorization for agent actions [P] | Enforced (blocks until user approves) | Approver is the user the agent acts for | Binding message (not verified here) | See A | status unstated |
| Dust, Relevance AI, Zapier/Make approval steps, Slack primitives | Not read (docs.dust.tt 404; others not attempted) | | | | | unverified |

### C. Agent identity and NHI

| Company | a. Wedge | b. Enforced/observed | c. Cross-person | d. Intent/operands | e. Traction | f. Stage |
|---|---|---|---|---|---|---|
| Oasis | "Agentic Access Management": intent-based, just-in-time access; vaulting, federation, ephemeral permissions; "governs access before the first action" [P newswire release] | Claims enforcement (no standing permissions) | not stated | "evaluates a system's intended task"; no amounts stated | $120M Series B, 19 Mar 2026, Craft; Cyberstarts, Sequoia, Accel; total $195M; "dozens of Fortune 500" unnamed; new ARR 5x YoY, no absolute [P] | Shipped |
| Token Security | NHI and agent security; MCP server, AI Discovery Engine, free "Privilege Guardian" [P token.security blog] | Mostly visibility; enforcement = lifecycle/least-privilege, mechanism unstated [P] | not stated | purpose-scoped access only in Guardian [P] | $20M Series A Jan 2025 Notable; named: HPE, HiBob, Betterhelp, Dayforce [P] | Shipped |
| Aembit | Blended identity (user + workload) in one policy (04 file) | Enforced (04 file) | Mechanism not described (04 file) | not stated | $25M Series A Sep 2024 Acrew; no later round found [S] | Shipped |
| SailPoint | "Agent Identity Security" evolved into "Agentic Fabric"; discovery, human owner per agent, review and revoke [P] | Visibility + revoke | Ownership only; on-behalf-of not covered [P] | none | Entro acquisition Jun 2026 per 04 [S]; no price [P] | GA status unstated |
| CyberArk / Palo Alto "Idira Secure AI Agents" | Discover agents, ownership, privilege controls, task-duration access, audit [P paloaltonetworks.com/idira] | Enforced privilege controls | On-behalf-of not mentioned [P] | none | Incumbent | GA unstated |
| Teleport, Clutch, P0, Britive, Ping | Teleport page 404; others not read | | | | | unverified |

### D. Runtime control planes and guardrails

| Company | a. Wedge | b. Enforced/observed | c. Cross-person | d. Intent/operands | e. Traction | f. Stage |
|---|---|---|---|---|---|---|
| Zenity | AI agent security and governance; "deterministically allows, modifies or blocks an action before it occurs" [P intelcapital] | Claims block; Gartner credits "intent-aware detection" [P] | not stated | intent-aware detection; no amounts | $125M Series C, 3 Aug 2026, Norwest; SoftBank VF2, Hitachi, LG; revenue tripled each of 2 years; SoftBank Corp named [P]. Total funding reported inconsistently ($175-185M) [S] | Shipped |
| Straiker | Discover AI, Ascend AI (red team), Defend AI "detects and blocks prompt injection, data leakage, tool manipulation"; "agentic kill switch" [P straiker.ai] | Blocks (content/behaviour) | none | none | Logos DirecTV, Comcast, Snowflake, Deloitte; testimonials Coupa, Amex GBT (not labelled customers) [P]. No funding on page | Shipped |
| Noma | Agent discovery, inventory, protection [S] | not confirmed | none found | none | $100M Series B Jul 2025, ~$132M total [S] | Shipped |
| Lasso, Pillar, Operant, Galileo, Guardrails AI, Arthur, WitnessAI, Cisco AI Defense, Prisma AIRS, CrowdStrike, Wiz | Only search snippets (Lasso ~$14.5M [S]); no primary pages read | | | | | unverified |

Per-action authorization vs detection only: I could confirm per-action blocking from the vendor's own page only for Arcade, Keycard (at credential issuance), Okta, Pomerium, Scalekit, Zenity (claim), Straiker (claim). SailPoint and Token are mainly visibility.

### E. Orchestration platforms with built-in approvals

| Platform | a. Wedge | b. Enforced/observed | c. Cross-person | d. Operands | e. Traction | f. Stage |
|---|---|---|---|---|---|---|
| Paperclip | "Open-source orchestration for teams of AI agents"; assign goals, track work and cost [P repo] | Enforced in code: budget policies with `hardStopEnabled` pause company/agent/project at limit, with pre-run reservations (`server/src/services/budgets.ts`, `budget-reservations.ts`); stop uses recorded spend and README says in-flight work can delay it [P]. Ask-first tool policies (`routes/tool-access.ts`); `bindVerifiedToolApprovalArguments` stores preview and verified hashes so an approval binds to the arguments [P, cloned repo 1 Nov 2026] | Approver is a board user (`decidedByUserId`) inside one company; a company flag `requireBoardApprovalForNewAgents` gates agent creation [P]. No cross-company or on-behalf-of | Amount caps are LLM spend in cents, not action amounts; approval bound to argument hash | MIT, ~110k stars, 18.2k forks, Paperclip Labs Inc. [P] | Shipped OSS |
| OpenClaw | Personal agent gateway | "One trust boundary per gateway"; "not a hostile multi-tenant security boundary" [P docs.openclaw.ai/gateway/security]. Exec-approval bypass CVEs (e.g. CVE-2026-31992, CVE-2026-53829) and malicious ClawHub skills (341 of 2,857 per Koi) [S] | none | Allowlists of commands | Large OSS (star figures in sources conflict) | Shipped |
| LangGraph / LangSmith Deployment | Agent runtime; pricing page lists no HITL interrupt line item [P langchain.com/pricing] | Interrupts are a code feature, not priced separately | n/a | n/a | Plus $39/seat/mo; metered compute 0.0675 LSU/vCPU-hour [P] | Shipped |
| OpenAI AgentKit / Agent Builder | "OpenAI is deprecating Agent Builder", shutdown 30 Nov 2026 [P developers.openai.com] | n/a | n/a | n/a | | Deprecating |
| Salesforce Agentforce | Guardrails and escalation to humans; pricing Flex Credits, Resolutions or per-user [P salesforce.com/agentforce] | Approval workflow not described on page | n/a | n/a | | Shipped |
| CrewAI Enterprise, Microsoft Agent Framework, Hermes | CrewAI page and MS docs URL 404; not read | | | | | unverified |

## 3. Investor theses

- boldstart, 21 Oct 2025 [P]: "Existing identity and access solutions weren't built for AI agents." Keycard is "the missing trust layer that moves AI agents safely from lab to production" giving "cryptographic proof of who an agent is, who it acts for, and what it can access." Frame: "Autonomous Enterprise", agents "ephemeral, autonomous, and spawn by the thousands."
- a16z (via Zane Lackey, Oct 2025) [S, biometricupdate; speaker disputed with Keycard CEO in another outlet]: "the Auth0 moment for agent access." No a16z essay on agent authorization found. a16z crypto "Know Your Agent" coverage is secondary only [S].
- Menlo, 15 Oct 2025 [P, Clerk Series C]: agents "authenticate on behalf of users, reach sensitive data, and keep detailed audit trails"; Clerk "the central nervous system for user identity." Wedge they cite is prebuilt React components, not agents. Menlo also led Astrix's $45M B (Dec 2024) [S].
- Craft, Mar 2026 [P Oasis release]: Oasis "the clear leader in this category."
- Norwest, Aug 2026 [P]: Zenity has "early-mover advantage in end-to-end AI agent security."
- Sequoia: co-published Oasis's Agentic Access Management framework (Nov 2025) [S]; no Sequoia own post found.
- YC: only secondary RFS coverage ("identity systems, permissions layers, and payment infrastructure designed for autonomous programmes") [S]; YC W26 company Agentic Fabriq pitched as "Okta for agents" [S]. Primary RFS page not fetched.
- Greylock co-led Oak's $60M seed, "control plane for every identity, human and non-human" [S, social repost]. Lightspeed led Composio; no thesis found.

## 4. The open space

- Crowded: agent identity/NHI inventory (Oasis, Token, SailPoint, Idira, Aembit, Astrix/Cisco); runtime guardrails (Zenity, Straiker, Noma); per-user tool auth (Arcade, Keycard, Scalekit, Composio, Okta, Auth0, WorkOS, Descope, Stytch, Pomerium, Cerbos, Permit); MCP gateways (Natoma, agentgateway). Acquirers are consolidating the last three.
- Enforced AND same-tenant per-user: sold by many. Enforced AND cross-person (person A's agent acting on person B's behalf with B's consent and a check): no page read describes it. Keycard multi-hop delegation and Okta XAA come closest, both inside one IdP/customer domain.
- Enforced AND operand-bound (amount, recipient, purpose compared to a stated rule at call time): nobody's page. Paperclip hashes approved arguments but its caps are LLM spend. Arcade names blast-radius caps without detail.
- Cross-vendor (works across Claude Code, Codex, Cursor, ChatGPT agents) AND enforced AND person-aware: Keycard claims MCP/A2A/OAuth compatibility with ChatGPT, Claude, Codex, OpenClaw [P]; no page shows an approval step bound to the actual operands.
- Cross-organization (two companies, neither controls the other's IdP): nothing found. Okta XAA and Keycard both assume one trust domain.
- Standalone approvals-as-a-service: tried (HumanLayer pivoted; gotoHuman small). Not a wedge by itself.
- Human approver who is a specific other person, not the requesting user's admin: only Paperclip (board users) and Auth0 CIBA (the same user) do anything like it.

## 5. What a pre-product entrant should do first

- Developers paid first for credentials plumbing, not governance: Composio, Arcade and Scalekit sell OAuth/tool access with a free tier and per-call pricing; Arcade puts governance only in Enterprise. Ship the plumbing people will install, then charge for the decision layer.
- Security buyers paid for visibility first (Oasis, Zenity, Token), with enforcement claimed in the same pitch. Enterprise logos are unnamed; revenue claims are growth multiples, not dollars.
- Standalone human-approval products did not get funded; HumanLayer pivoted. Ship approval as a consequence of a rule (amount, recipient, person), not as a product.
- Keycard's path is the template that raised: a named protocol position (OAuth 2.1, CIMD, RFC 8693), one named enterprise customer (Chime), SOC 2, SDKs. Copy the standards posture.
- Pick one narrow crossing nobody sells (cross-person or cross-org) and make the check operand-bound, in a domain with real amounts (money, access grants), so the demo is something Keycard/Arcade cannot show with scopes alone.
- Expect acquisition, not independence, as the exit (SGNL, Natoma, Astrix, Entro, Prompt, Aim).

## 6. Not verified

- Arcade round details (lead SYN Ventures, Morgan Stanley, Wipro, total $72M): Business Wire returned 403; figures from search summaries [S]. Keycard "Auth0 moment" speaker (Lackey vs CEO) disputed.
- Zenity block-per-action mechanism; Straiker, Zenity, Oasis claims are vendor statements.
- Auth0/Okta GA status of Token Vault, CIBA, Agent Gateway, Agent as Principal.
- OpenClaw CVE details and ClawHub counts: aggregator pages only; star counts conflict.
- Paperclip: I read the cloned repo (commit dated 1 Nov 2026) selectively: budgets.ts, tool-approval-execution.ts, a few grep hits. I did not trace end-to-end whether a hard stop blocks the run in `heartbeat.ts`, nor what the approval-bound hashes are compared to at execution.
- Not reached or 404: Descope Agentic Hub page, Oso (DNS), Cerbos AI page, Stytch independence, Nango, Strata, Dust, Relevance AI, Zapier/Make/Slack approvals, Teleport page, CrewAI, Microsoft Agent Framework approval docs, gotoHuman and Composio pricing detail beyond what is cited, Lasso/Pillar/Operant/Galileo/Guardrails AI/Arthur/WitnessAI/Cisco/Prisma AIRS/CrowdStrike/Wiz, Britive/Clutch/P0/Ping, Salesforce Agentforce approvals page (marketing page only).
- Investor theses: no primary a16z, Sequoia, Lightspeed, Greylock, YC RFS page was read; Menlo and boldstart only.
- SGNL, Natoma, Noma, Composio, Aembit funding are [S] (search results; Natoma/Snowflake release URLs found but not fetched).

## Primary pages fetched (WebFetch)

1. arcade.dev
2. arcade.dev/pricing
3. arcade.dev/blog/one-question-every-tool-call
4. keycard.ai
5. docs.keycard.ai/guides/how-keycard-works/
6. boldstart.vc/news/keycard-solving-the-ai-agent-identity-and-access-problem-welcome-to-boldstart/
7. helpnetsecurity.com/2026/05/15/keycard-for-multi-agent-apps/ (secondary report of product launch)
8. humanlayer.dev
9. github.com/humanlayer/humanlayer
10. gotohuman.com
11. auth0.com/ai
12. auth0.com/docs/get-started/auth0-for-ai-agents
13. okta.com/solutions/secure-ai/
14. composio.dev/pricing
15. stytch.com/docs/guides/connected-apps/overview
16. workos.com/docs/authkit/mcp
17. descope.com
18. permit.io/ai-access-control
19. cerbos.dev
20. scalekit.com
21. pomerium.com
22. github.com/paperclipai/paperclip and raw README, plus shallow clone of the repo
23. docs.openclaw.ai/gateway/security
24. newswire.com Oasis Series B release
25. intelcapital.com Zenity Series C post
26. token.security 2025 blog
27. straiker.ai
28. sailpoint.com/products/agent-identity-security
29. paloaltonetworks.com/idira (CyberArk)
30. langchain.com/pricing
31. developers.openai.com Agent Builder guide
32. salesforce.com/agentforce
33. menlovc.com Clerk Series C post
