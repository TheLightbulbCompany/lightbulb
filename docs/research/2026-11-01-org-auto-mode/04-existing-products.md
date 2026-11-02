# Who already governs AI agents across people and vendors (as of 1 Nov 2026)

Tags: [P] = primary page fetched this session (text read). [S] = search summary or secondary only. Rows with no tag are unverified. Second pass fetched: Claude Code permission-modes + auto-mode-config, Codex managed-configuration, agentgateway README, Aembit how-it-works, AWS AgentCore Policy blog, Agent Beacon README, Google Agent Gateway overview. Unbound's pages returned HTTP 429, so Unbound stays [S]. Correction from pass 1: Codex does NOT fail open (see table).

## Gap summary (each claim points to its [P] row)

1. Claude Code auto mode is the only org-configurable "auto-approve" classifier found. Admins set `autoMode.environment/allow/soft_deny/hard_deny` in managed settings as prose rules, and developers' entries combine additively, so it is not a hard boundary. `permissions.deny` in managed settings is the hard floor. Claude Code only. [P row 1]
2. Codex `requirements.toml` constrains approval policy, sandbox, web search, MCP servers, hooks, permission profiles, network. Admin-enforced, cloud-delivered, outranks system file. Codex only. [P row 3]
3. Cross-vendor laptop telemetry exists: Agent Beacon covers 20+ runtimes (Claude Code, Codex, Cursor, Copilot CLI, OpenCode...) but is a capture/memory layer and never claims blocking. [P row 14]
4. Cross-vendor laptop enforcement is claimed only by Unbound AASB ([S]: audit/block tool policies, approvals, MDM). Not confirmed from a primary page.
5. Agent-to-agent traffic is governed at gateways: agentgateway has an A2A Gateway with a CEL policy engine (Apache-2.0, LF, 5.9k stars); Google Agent Gateway covers agent-to-agent egress over MCP/A2A, default-deny, keyed on agent identity plus registry entries. Both are operator-domain gateways; Google does not address cross-org. [P rows 12, 6]
6. Delegation of a human's authority: Aembit evaluates user identity plus agent workload identity in one policy and logs both, but its page says nothing on crossing tenants or companies. AgentCore Policy keys on principal (OAuth user), action (tool), resource (gateway) with token claims and tool arguments, enforced at the Gateway outside the agent loop. [P rows 8, 5]
7. None of the fetched primary pages describe a person's agent sending a request to another person's agent in a different company or vendor, decided by stated company intent. Absence from the pages read; not proof.
8. Everything above is rule-shaped (allow/deny/Cedar/CEL/prose rules per harness). Closest to "intent" is Claude's prose `autoMode` rules and Google's "semantic governance" plain-language rules checked per tool call. [P rows 1, 6]

## 1. Platform-native

| Product | Governs | Enforces at | A2A / cross-person | Laptop coding agents | Scope | Status | Pricing | Tag |
|---|---|---|---|---|---|---|---|---|
| Claude Code (managed settings, `permissions.*`, `autoMode.*`) | tool access, content via classifier; `permissions.deny` is a hard, un-overridable floor | harness plus classifier model | No A2A. `autoMode` read from user, managed, `--settings` scopes, NOT from project settings (repo-injection guard). Managed entries can't be removed by devs; dev `allow` can override org `soft_deny` ("additive, not a hard policy boundary"). `permissions.ask` forces a prompt even in auto. Classifier reads user messages and CLAUDE.md; a boundary stated in chat can be lost to compaction | Yes | Single | Shipped, default-on Team/Enterprise, admin can disable via `permissions.disableAutoMode`; `/auto-mode-setup` needs v2.1.283+ on Enterprise | In seat | [P] code.claude.com/docs/en/auto-mode-config, permission-modes |
| Anthropic Managed Agents | vaults (per-user MCP creds), environments | hosted sandbox | none found | No | Single | beta header `managed-agents-2026-04-01` | API | [S] |
| OpenAI Codex `requirements.toml` | `allowed_approval_policies`, `allowed_sandbox_modes`, `allowed_permission_profiles`, `allowed_web_search_modes`, `mcp_servers`, `[rules]` command rules, managed hooks, `[permissions.filesystem] deny_read`, network, `guardian_policy_config` | harness; cloud (Codex Policies page, by group), system file, or MDM; cloud outranks system | none found | Yes | Single | Business/Enterprise. If fetch fails/times out with no valid cache it errors rather than "silently starting without the cloud-managed requirements layer" (fail-closed; pass-1 "fails open" was wrong). Behaviour with a valid cache unconfirmed | In plan | [P] learn.chatgpt.com/docs/enterprise/managed-configuration (redirect from developers.openai.com) |
| OpenAI Agents SDK guardrails | content | SDK | No | No | Single | not checked | free | unverified |
| GitHub Copilot enterprise policies | feature/model/agent/MCP, cloud-agent per-org, runner/firewall | GitHub, IDE, CLI | repo-scoped | Partly | Single | docs surfaced; Aug 2026 team override unconfirmed | seat | [S] |
| Cursor Enterprise (enforced rules, hooks, sandbox) | terminal, git, network, hooks | harness + OS sandbox | none | Yes | Single | Enterprise-only hooks/sandbox enforcement | plan | [S] |
| Google Agent Gateway (Gemini Enterprise Agent Platform) | IAM Unified Access Policies (default-deny, allow/deny + CEL), Model Armor, semantic governance (plain-language rules vs each proposed tool call), MCP attribute rules; token mgmt and extension bindings "coming soon" | gateway; agent-to-anywhere egress incl. other agents over MCP/A2A/REST/gRPC; keyed on agent identity and Agent Registry | Agent-to-agent covered; cross-org "not addressed"; direct Gemini Enterprise agent-to-connector calls skip the gateway | No | Google-hosted agents | "currently available" for agents on the platform; GKE/Cloud Run/cross-cloud coming soon; page not labelled GA | not stated | [P] docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview |
| AWS Bedrock AgentCore Policy | principal/action/resource in Cedar; NL compiled to Cedar; conditions on OAuth claims and tool args | AgentCore Gateway, outside agent reasoning loop; enforce or log-only | agent-to-tool | No | AWS-hosted | GA 3 Mar 2026 (update on post; was preview) | not stated | [P] aws.amazon.com/blogs/aws/amazon-bedrock-agentcore-adds-quality-evaluations-and-policy-controls-for-deploying-trusted-ai-agents |
| Salesforce Agentforce / MuleSoft Agent Fabric | registry, policy on agent interactions, identity propagation | MuleSoft | A2A/MCP, inside the org | No | Cross-vendor | not verified | n/a | [S] |
| ServiceNow AI Control Tower / AI Gateway / AI Passport | inventory, identity, runtime controls | platform, gateway | MCP/A2A fabric; off-platform enforcement unconfirmed | No | Claims any vendor | 2025-26 | n/a | [S] |
| Workday Agent System of Record + Agent Gateway | registry, lifecycle | platform | partners register | No | Cross-vendor | GA per blog | n/a | [S] |
| Microsoft Agent 365 (given) | registry, block/reassign, telemetry | M365/Entra | cross-tenant not found | Partly | MS-centric | GA 1 May 2026 | $15/user | given |

## 2. Agent security and identity startups

| Company | What | Coding agents on laptops | Cross-person / delegation | Status, funding | Tag |
|---|---|---|---|---|---|
| Aembit | "Blended identity": user identity + agent workload identity evaluated in one Access Policy ("this user, using this agent"); Aembit Edge blocks denied requests; MCP Identity Gateway proxies and injects credentials; logs carry both identities | not specific | Mechanism for passing the user identity not described; cross-tenant/company not addressed | independent | [P] docs.aembit.io/get-started/how-aembit-works |
| Unbound AI "AASB" | discover Claude Code/Cursor/Windsurf/Copilot/Cline, sub-agents, MCP servers; Tool Policies on MCP actions and terminal commands with audit and block; approval workflows; MDM (Jamf, Intune, JumpCloud, Kandji) or light agent; SOC 2, AWS Marketplace; metadata not code | YES (closest enforcement competitor) | not stated | announced 20 Mar 2026; no public price (demo) | [S] (pages 429) |
| Zenity | discovery, policy; Claude Enterprise Compliance API integration 12 Jun 2026; claims Cursor/Claude Code/Copilot | Claimed | visibility | independent | [S] |
| Noma | agentic risk map, MCP exposure | not confirmed | map only | $100M Series B Aug 2025 | [S] |
| Keycard | task-scoped short-lived agent tokens, revocation | not stated | "who they act for" framing | $38M, launched Oct 2025 | [S] |
| Arcade.dev, Composio | tool auth, permission intersection | no | agent-to-tool | independent | [S] |
| Teleport Agentic Identity | per-agent X.509, MCP access | servers | agent-to-infra | Feb 2026 | [S] |
| WitnessAI, Lasso, Straiker, Pillar, Knostic, Oasis, Token | visibility, NHI, runtime | not confirmed | none found | WitnessAI $58M Jan 2026 | [S] |
| Acquired (all [S] except where noted): Prompt Security->SentinelOne (Aug 2025), Aim->Cato (3 Sep 2025, Cato release), Lakera->Check Point (Sep 2025), Invariant->Snyk (24 Jun 2025, Snyk page), Acuvity->Proofpoint (Feb 2026), Astrix->Cisco (reported May 2026), Natoma->Snowflake (announced May 2026), Promptfoo->OpenAI (Mar 2026), Koi->Palo Alto (Feb 2026), Entro->SailPoint (Jun 2026) | | | | | [S] |

## 3. Open source

| Project | Governs | Cross-person / A2A | Laptops | Status | Tag |
|---|---|---|---|---|---|
| agentgateway | LLM, MCP, and A2A traffic; A2A Gateway with "capability discovery, modality negotiation, task collaboration"; CEL policy engine for RBAC; JWT, API key, OAuth auth. Page does not say policies key on JWT claims or per tool | A2A yes, within operator's gateway | if pointed at it | Apache-2.0, Linux Foundation, 5.9k stars; releases section empty | [P] github.com/agentgateway/agentgateway |
| Agent Beacon (Asymptote Labs) | local JSONL endpoint telemetry, forwardable; 20+ runtimes incl. Claude Code, Claude Cowork, Cline, Codex CLI/Desktop, Cursor, Copilot CLI, Gemini CLI, OpenCode, OpenHands, Devin; browser chat; cloud agents; CI; SDKs. Described as capture/memory layer; never says it blocks; approvals appear only as a recorded event type | none | Yes | MIT | [P] github.com/Asymptote-Labs/agent-beacon (enforcement-on-roadmap claim from pass 1 is [S]) |
| ToolHive, Docker MCP Gateway | MCP containerization, identity/policy, signature verify, secret blocking | agent-to-tool | local | Apache-2.0 / OSS | [S] |
| Galileo Agent Control, WSO2 Agent Manager, Preloop | control-plane policies | not stated | not stated | 2026 | [S] |
| kagent, Solo Agent Mesh | agent identity + gateway enforcement on K8s | in-cluster A2A | no | CNCF/LF | [S] |
| NeMo Guardrails, Guardrails AI, LiteLLM, Envoy AI GW, Kong, Portkey, SPIFFE, OpenFGA/SpiceDB, Paperclip | | | | not checked | unverified |

## 4. Cross-agent networks

| Product | Message from person A's agent to person B's agent governed? | Tag |
|---|---|---|
| Google A2A v1.0 (LF) | transport, signed Agent Cards; no authority model; date disputed | [S] |
| Google Agent Gateway | agent-to-agent egress governed by IAM/semantic policy on agent identity; cross-org not addressed | [P] |
| agentgateway A2A Gateway | routed and CEL-authorized at a gateway the operator runs | [P] |
| IBM watsonx Orchestrate control plane | imports A2A/Langflow/LangGraph/Bedrock agents into one org's control plane | [S] |
| MS Agent 365 / Copilot Studio, MuleSoft, ServiceNow, Workday | hub-and-registry inside one org | [S] |
| NANDA (MIT) | discovery/verification, no authority | [S] |
| ANP, AgentWorkforce relay, Mastra, Vercel AI Gateway | not checked | unverified |

## Not verified / couldn't reach

- Unbound AASB: vendor and aggregator pages returned HTTP 429; enforcement, harness list, MDM and price rest on search summaries of its press release. Retry later or use Claude in Chrome.
- Codex: behaviour when a valid cache exists but the fetch fails; the Compliance API's coverage of agent actions.
- Agent Beacon: the roadmap sentence on enforcement was not on the README text returned; beacon.sh docs not read.
- agentgateway: what policies key on (CEL variables), per-tool authz, latest release.
- Aembit: how user identity is transported; Keycard docs not fetched.
- Google: pricing, GA label, semantic-governance page, whether Agent Gateway works for non-Google agents.
- AgentCore: pricing; A2A support in Gateway.
- All [S] rows: Copilot, Cursor, Managed Agents, Salesforce, ServiceNow, Workday, IBM, Microsoft Agent Framework cross-tenant, startups' funding/acquisition details (acquirer releases not read except Cato, Snyk via search).
- Not searched: ANP, AgentWorkforce relay, Mastra, Vercel AI Gateway, NeMo, Guardrails AI, LiteLLM, Envoy, Kong, Portkey, SPIFFE, OpenFGA, Paperclip.
