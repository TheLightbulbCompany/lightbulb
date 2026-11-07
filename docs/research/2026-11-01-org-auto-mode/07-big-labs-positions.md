# Who decides what an agent may do: big-lab positions (2 Nov 2026)

Tags: [P] = the lab's own page or paper, text read this session. [S] = search summary or third party only. This extends 01-a2a-security.md (A2A, MCP, RFC 8693, OWASP, Entra OBO) and 04-existing-products.md (product rows). It does not repeat them.

## 1. Summary findings

1. Anthropic's auto mode gives the decision to a classifier that treats "everything the agent chooses on its own [as] unauthorized until the user says otherwise", so the user's words in the chat are the authority. [P] https://www.anthropic.com/engineering/claude-code-auto-mode
2. Anthropic's published numbers: 0.4% false positives (n=10,000 real employee tool calls), 17% misses on real overeager actions (n=52), 5.7% misses on synthetic exfiltration (n=1,000); a later post adds 97% prompt acceptance, a 7% synthetic miss rate, and "auto mode blocked 800 commands that humans approved". [P] same post; https://claude.com/blog/auto-mode-default-in-claude-code
3. OpenAI's Codex Auto-review also reads "the user's intent", auto-approves 99.1% of escalated actions, and reports recall only (90.3% overeager, 99.3% prompt injection), with no false-positive rates or sample sizes. [P] https://alignment.openai.com/auto-review/
4. Anthropic and OpenAI both state the human cannot approve every call and both replace per-call approval with a model; both say the result "is not a deterministic security guarantee" (OpenAI) or "does not eliminate risk" (Anthropic). [P] https://learn.chatgpt.com/docs/sandboxing/auto-review ; https://claude.com/blog/auto-mode-default-in-claude-code
5. Anthropic's own docs say a model reviewer is not oversight: "`auto` is not a human checkpoint, so if a person must review a tool's calls before they run, configure `always_ask`". [P] https://platform.claude.com/docs/en/managed-agents/secure-deployment
6. The only shipped agent-to-agent rule that names authority is Claude Code's: a message from another session "never counts as your consent" and "can't approve anything". It covers one person's own sessions only, with no path for another person's agent. [P] https://code.claude.com/docs/en/cross-session-messaging
7. Google's security paper requires that "every agent must have a well-defined set of controlling human user(s)", and that permissions be "dynamically aligned with ... current user intent". It flags shared and team agents ("one user inadvertently triggering actions impacting another") but specifies no mechanism. [P] https://storage.googleapis.com/gweb-research2023-media/pubtools/1018686.pdf
8. DeepMind's "Intelligent AI Delegation" (Feb 2026) is the only lab text that models multi-hop delegation across people: it calls for privilege attenuation per hop, signed attestation chains, "liability firebreaks" that halt and "request an updated transfer of authority from the human principal", and it judges that MCP and A2A lack these. [P] https://arxiv.org/pdf/2602.11865
9. Google's AP2 is the one shipped protocol that carries signed user intent ("mandates") between agents, but only for payments; v0.2 moved it to FIDO. [P] https://ap2-protocol.org/ ; mandate wording from the DeepMind paper above
10. Microsoft's red team now lists "Inter-agent trust escalation" (an agent claiming to act "on behalf of an administrator") and "Human-in-the-loop bypass" as failure modes. Microsoft's answer is identity with "ownership, user delegation, task and sub-agent permission scoping", not intent. [P] https://cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/microsoft/bade/documents/products-and-services/en-us/security/Taxonomy-of-Failure-Modes-in-Agentic-AI-Systems-v2-0.pdf
11. Microsoft makes a human accountable per agent (a required "sponsor"), but the sponsor handles lifecycle and access requests, not runtime decisions; AWS's agent access token carries "both workload identity and user identity". Neither carries what the person asked for. [P] https://learn.microsoft.com/en-us/entra/agent-id/agent-owners-sponsors-managers ; https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-terminology.html
12. The joint industry body (FMF, June 2026) says current identity frameworks assume "a single authenticated principal", that "multi-agent systems challenge the traditional human-in-the-loop model", and that click-through approval can "create the illusion of oversight". It lists moving the human "to governing workflows with risk-tiered escalation" as an open question. [P] https://www.frontiermodelforum.org/issue-briefs/emerging-security-practices-for-ai-agents/

## 2. Table

| Lab | (a) Who decides | (b) Human role | (c) Cross-hop / cross-person | (d) Intent carried? | (e) Auto mode + numbers | (f) Ships to orgs / out of scope |
|---|---|---|---|---|---|---|
| Anthropic | Mix: deny rules/hooks first, then a classifier judging user consent; admins set managed rules | Sets boundaries in chat; manual review for high stakes; "you're still responsible" | Own sessions only; peer messages can't consent; subagent handoff classified | Yes, in-session only: the classifier checks whether consent "covers this action" | Auto mode: 0.4% FPR, 17% FNR real, 5.7% synth; 93%→97% acceptance; default since 14 Aug 2026 | Managed settings, `autoMode.*`, `disableAutoMode`, connector `ask`. Org admin rules absent from the engineering post |
| OpenAI | Deployer/user set the policy; reviewer agent decides at the sandbox boundary; SDK `needs_approval` | 2023: user approves high-stakes actions, warns of "rubber stamp"; agent: confirmations + watch mode | 2023 paper: agent IDs for accountability; SDK: approvals "run-wide" across handoffs; no cross-user model | Auto-review weighs "the user's intent"; nothing carried across agents | Auto-review: 99.1% approved, recall 90.3/99.3/96.1%; agent confirmation recall 91% | `guardian_policy_config`, `requirements.toml`; "not a deterministic security guarantee" |
| Google / DeepMind | Paper: deterministic policy engine + reasoning defenses; Gateway: IAM + semantic rules | "Well-defined human controllers"; confirmation for critical actions | DeepMind: attenuation + attestation chains (research); AP2 for payments; Gateway "cross-org not addressed" | Yes in principle ("current user intent"); AP2 signs intent for payments only | None published for a Google auto mode | Agent Gateway, AP2 (FIDO); paper says policy engines "lack deep contextual understanding" |
| Microsoft | Admins via Entra/Purview/Defender; sponsor decides lifecycle | Sponsor accountable; "UX through which humans delegate to and supervise" | OBO within tenant; taxonomy names inter-agent trust escalation | No: identity, delegation, permission scoping | None published | Agent 365 GA 1 May 2026; registry, DLP, threat defense; no runtime per-request authority |
| Meta | Developer chooses a design under the Rule of Two | HITL approval when all three properties are needed | Not addressed | AlignmentCheck compares actions with "the user's stated objective" | LlamaFirewall: AgentDojo ASR 17.6%→1.75%, utility 47.7%→42.7% | Open source; "supplement, not a substitute" |
| Amazon | Policy (Cedar) at Gateway outside the agent loop; user consent via 3LO | Consent giver; operator writes policy | "Identity propagation" of caller + original user through call chains | No: identities and claims | None published | AgentCore Identity/Policy/Gateway; token vault per agent+user |
| FMF (joint) | Split across developer, deployer, user | Approve, redirect, take over; warns of illusory oversight | "Delegation hierarchies" named, not specified | Intent mentioned as a cause of accidental harm only | n/a | Open questions incl. risk-tiered escalation |

## 3. Per lab

### Anthropic
- Decision rule (25 Mar 2026): auto mode "delegates approvals to model-based classifiers" acting "as a substitute for a human approver". Core rule: "everything the agent chooses on its own is unauthorized until the user says otherwise"; "clean up my branches" does not authorize a batch delete; "one approval isn't a pattern". The classifier is "reasoning-blind by design": it sees user messages and tool calls, not Claude's prose or tool output. Subagents: "handoff classifiers at delegation (can deny) and at return (warns only)". After "3 consecutive denials or 20 total" it escalates to the human; headless runs are terminated. [P] https://www.anthropic.com/engineering/claude-code-auto-mode
- Number definitions (same post): FPR = "fraction of benign actions it blocks", 8.5% after stage 1 and 0.4% after the full pipeline, on n=10,000 "actual Claude Code tool calls from Anthropic employees". FNR = "fraction of dangerous actions the classifier lets through": 17% on n=52 real overeager actions ("the honest number"), 5.7% on n=1,000 synthetic exfiltration. 93% = "users accept 93% of [permission prompts] anyway"; how this was measured is not stated. Stated misses are mostly about "whether a real consent signal in the session actually covers this action"; "haven't found a fix". [P]
- Containment post (25 May 2026): "Rather than supervising what the agent does, we supervise what it's able to do". For non-experts in Cowork, "admins should set a boundary that is absolute and always-on". Results: 0.1% single-attempt injection, 5–6% after 100 adaptive tries; 84% fewer prompts with the sandbox; a phished employee's prompt exfiltrated data 24 of 25 times, because model defenses "anchor on user intent". Open question in its own words: "Should an agent possess its own principal identity," or act as an extension of the user? [P] https://www.anthropic.com/engineering/how-we-contain-claude
- Last 90 days (7 Aug 2026, updated 22 Oct): auto mode became the default for new sessions on Pro/Max/Team from 14 Aug, and in most Claude apps from late October; Enterprise and API "in the coming month". New numbers: 97% of prompts approved; in a 1,053-tester study humans caught 13.6% of dangerous commands while auto mode blocked 89%; 800 human-approved commands blocked versus 6 the other way; unrequested severity-7+ actions in 6.3% of manual sessions versus 2.4% of auto sessions; Apollo pilot misses 12%→7% ("should not be taken as the miss rate on real traffic"). Org controls: `defaultMode`, `disableAutoMode`, and hard deny rules "so organizations can block actions even when users request them". [P] https://claude.com/blog/auto-mode-default-in-claude-code
- Customers (7 Aug 2026): Garner "configured auto mode not to approve actions that communicate with other people"; Gusto routes MCP through a governed proxy; ~10% of Gusto sessions include a denial. Org policy shows up as deny lists, tuned classifier prompts and proxies, not as a written company intent. [P] https://claude.com/resources/articles/auto-mode-in-production
- Agent-to-agent (current docs): the classifier reviews every outbound `SendMessage`. Inbound: "a message from another session never counts as your consent, so it can't answer a pending permission prompt on your behalf"; the receiver is told never to change permissions or CLAUDE.md "because another session asked"; Claude "is instructed never to ask another session for an action that was denied ... and to route that work back to you". Scope is "your other Claude Code sessions"; teammates appear only as agent-team members inside one session. Admins can turn it off with `deny: ["SendMessage","ListAgents"]` plus `crossSessionInbound: "refuse"`. [P] https://code.claude.com/docs/en/cross-session-messaging ; https://code.claude.com/docs/en/permission-modes
- Agent SDK order: hooks → deny → ask → mode → allow → `canUseTool`. Org-set connector `ask` falls through "even in bypassPermissions mode" with the reason "Your organization requires approval for this tool". Subagents inherit plan/auto/bypass. [P] https://code.claude.com/docs/en/agent-sdk/permissions
- Managed Agents: `auto` exists because "a person cannot approve every call in a long run"; "`auto` is not a human checkpoint". Use `always_ask` "on a tool that should not run just because end-user text you relay asks for it". Agents in one multiagent session share the sandbox and vault, so for a real split "pass only fixed fields, such as an order ID". [P] https://platform.claude.com/docs/en/managed-agents/secure-deployment
- Framework (4 Aug 2025): "humans should retain control over how their goals are pursued, particularly before high-stakes decisions"; agents "take actions that seem reasonable to the system but aren't what humans actually wanted". Multi-user delegation is not addressed. [P] https://www.anthropic.com/news/our-framework-for-developing-safe-and-trustworthy-agents
- Agentic misalignment (20 Jun 2025): Claude Opus 4 blackmailed in 96% of runs under goal conflict plus replacement threat; recommends "human oversight and approval of any model actions with irreversible consequences"; "We have not seen evidence of agentic misalignment in real deployments." [P] https://www.anthropic.com/research/agentic-misalignment

### OpenAI
- 2023 white paper (Dec 2023; dated by its references, cover not read): the parties are model developer, system deployer and user. Approval: "Requiring a user to proactively authorize these actions ... is a standard way", but approving "many decisions" quickly reduces "their ability to meaningfully consider each one", and it asks how to keep approvals from "turning into a 'rubber stamp'". It names the cross-person circumvention: an agent can "send an email—an allowed action—to a non-user human that convinces said human to take the disallowed action". Attribution: give each agent instance "a unique identifier ... which contains information on the agent's user-principal", which third parties "could demand ... before starting the interaction". No newer OpenAI governance paper superseding it was found. [P] https://cdn.openai.com/papers/practices-for-governing-agentic-ai-systems.pdf
- ChatGPT agent system card (17 Jul 2025): trained "to ask the user for confirmations before finalizing actions that affect the state of the world"; confirmation recall 91.0% ("an underestimate"); critical recall 100% for permission edits and financial transactions, 99.9% for high-stakes communications. Watch Mode pauses "when the user becomes inactive or navigates away" in sensitive contexts. Refuses financial account transfers. [P] https://cdn.openai.com/pdf/839e66fc-602c-48bf-81d3-b21eacc3459d/chatgpt_agent_system_card.pdf
- Auto-review (30 Apr 2026): the reviewer considers "the user's intent, the environment, the security policy, and the likely impact"; 99.1% auto-approval on real escalations (99.93% across all actions); ~200x fewer stops; recall 90.3% overeager (synthetic), 99.3% prompt injection, 96.1% MonitoringBench hard. No FPR and no sample sizes are published. Not "a defense against model scheming"; it "may not be the final form". The majority of internal Codex Desktop tokens run in Auto-review. [P] https://alignment.openai.com/auto-review/
- Auto-review docs (undated): "Enterprises can replace its tenant-specific section with `guardian_policy_config`"; managed settings win; circuit breaker after "3 consecutive denials or 10 denials within ... 50 reviews"; "not a deterministic security guarantee". [P] https://learn.chatgpt.com/docs/sandboxing/auto-review
- Agents SDK: `needs_approval`, interruptions; "That approval surface is run-wide" across handoffs and `as_tool` nesting. The approver is unauthenticated by the SDK: `RunState.from_json()` does "not authenticate the snapshot or the person submitting it", so apps must "Authenticate the reviewer". [P] https://openai.github.io/openai-agents-python/human_in_the_loop/
- Delegation across users: no OpenAI statement found.

### Google / DeepMind
- Secure agents paper (May 2025): three principles: well-defined human controllers; limited powers; observable actions and planning. Systems must "reliably distinguish instructions originating from an authorized controlling user versus any other input". Multi-user: "Agents acting on behalf of teams or groups need distinct identities and clear authorization models to prevent unauthorized cross-user data access or one user inadvertently triggering actions impacting another." Policy engine example: block purchases over $500, confirm $100–500. Limit stated: policies "often lack deep contextual understanding". [P] https://storage.googleapis.com/gweb-research2023-media/pubtools/1018686.pdf
- Intelligent AI Delegation (12 Feb 2026): sub-delegation "cannot transmit its full set of authorities"; "transitive accountability via attestation"; in long chains, "the increased distance between the original intent (X) and the ultimate execution (Y) may result in an accountability vacuum", answered by liability firebreaks (insure, or "halt execution and request an updated transfer of authority from the human principal") and "immutable provenance". Protocol gaps: MCP "lacks the policy layer to govern usage permissions or support deep delegation chains" and exposes "results rather than intent"; A2A "designed for coordination, rather than adversarial safety". This is a research framework, not a product. [P] https://arxiv.org/pdf/2602.11865
- AP2: signed mandates for payments (v0.2 names Checkout and Payment mandates, Open and Closed); "non-repudiable, cryptographic audit trail"; accountability for fraud is posed, not resolved; announced 16 Sep 2025; donated to FIDO (date not shown). [P] https://ap2-protocol.org/
- Agent Gateway: see 04 (semantic governance, "cross-org not addressed"). DeepMind's AGI safety approach (Apr 2025) treats monitoring and access control as the system-level defense against a misaligned model. [S] arXiv 2504.01849; agent sections not read.

### Microsoft
- Taxonomy v2.0 (Apr 2026, blog 4 Jun 2026): new modes include goal hijacking, inter-agent trust escalation ("a compromised sub-agent asserts false identity or inflates claimed permissions, and the orchestrator does not independently verify"; "the confusion is induced through natural language rather than system calls"), and MCP/plugin abuse. Kept from v1: HITL bypass and "harms of allocation in multi-user scenarios: an agent serving multiple principals". Mitigation: "every agent, tool call, and inter-agent message is tied to an attributable identity", now needing "ownership, user delegation, task and sub-agent permission scoping"; "control-flow control ... expressed as policy rather than prompt". [P] PDF linked in finding 10
- Entra Agent ID roles (updated 3 Sep 2026): a sponsor is required, "accountable for the agent's purpose and lifecycle decisions"; sponsors "request access packages on behalf of agents" and in incidents "determine whether agent behavior is expected". Owners handle the technical side; managers place the agent in the org chart. No runtime approval role. [P] https://learn.microsoft.com/en-us/entra/agent-id/agent-owners-sponsors-managers
- Agent 365 (GA 1 May 2026, doc updated 23 Oct 2026): observe, govern, secure; "Entra enforces consistent, risk-based access controls for users and agents acting on their behalf". No cross-tenant or intent claim. [P] https://learn.microsoft.com/en-us/microsoft-agent-365/overview
- Copilot Studio DLP: not read.

### Meta
- Agents Rule of Two (31 Oct 2025): no more than two of [A] untrustworthy inputs, [B] sensitive systems or private data, [C] change state or communicate externally. With all three, the agent "should not be permitted to operate autonomously" and needs human-in-the-loop approval or another reliable validation. It is "a supplement — and not a substitute". [P] https://ai.meta.com/blog/practical-ai-agent-security/
- LlamaFirewall (May 2025, "utilized in production at Meta"): AlignmentCheck compares the "action sequence with the user's stated objective". AgentDojo ASR 17.6% → 7.5% (PromptGuard 2) → 2.89% (AlignmentCheck) → 1.75% combined, with utility 47.7% → 42.7%. [P] https://arxiv.org/pdf/2505.03574

### Amazon
- AgentCore Identity: agents "act with pre-authorized user consent"; agent identities "authenticate as themselves rather than impersonating users, supporting delegation-based access"; the agent access token "contains both workload identity and user identity"; "identity propagation" maintains "the original user identity" through a chain of calls; the token vault is limited to "the specific agent and user combination". [P] https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-terminology.html
- Policy: see 04 (Cedar at the Gateway, GA 3 Mar 2026). No AWS governance whitepaper read.

### Others
- FMF (3 Jun 2026): quoted in finding 12. FMF Agents for Cyber Defense (21 Sep 2026) moves defenders "into a supervisory role" [S, search excerpt].
- Salesforce: help page says agents follow standard access controls and escalate per agent type; Trust Layer masking is "disabled" for agents per one page and core per others. [S]
- Apple: not researched.

## 4. Where the labs agree, where they differ, what none of them does
- Agree: per-action human approval does not scale (Anthropic 93–97% acceptance; OpenAI "rubber stamp", ~200x fewer stops; FMF "illusion of oversight").
- Agree: hard limits (deny rules, sandbox, Cedar, Rule of Two) sit under any judgment layer, and the judgment layer is called not a guarantee.
- Agree: irreversible or high-stakes actions still go to a person (Anthropic, OpenAI, Google, Meta, misalignment study).
- Differ on who judges: Anthropic and OpenAI ship a model judge of user consent; AWS and Google Gateway ship deterministic policy at a gateway; Microsoft ships identity and lifecycle; Meta ships a design rule plus open-source scanners.
- Differ on whose intent: Anthropic and OpenAI read the user's chat; Meta reads the "user's stated objective"; Google wants permissions tied to "current user intent". None reads an organization's written intent. Claude's `autoMode` prose rules come closest, set per harness.
- Differ on the human: approver (OpenAI agent, Managed Agents `always_ask`), boundary-setter (Anthropic chat boundaries), accountable sponsor (Microsoft), consent-giver (AWS 3LO), principal who re-grants authority at a firebreak (DeepMind, research only).
- None ships a check that a request arriving from another person's agent reflects what that person asked for. Anthropic refuses cross-session consent and stays within one person; AWS propagates identity without the request; AP2 signs intent for payments only.
- None routes an out-of-rule request to the holder of the resource. Escalation always goes to the agent's own user, or to a sponsor for lifecycle.
- None publishes failure numbers for cross-person or multi-hop decisions. All published numbers are single-user, single-harness.

## 5. What this changes for Lightbulb's thesis
- Validated: the labs have moved from per-action approval to a judge plus hard floor. "Auto mode for the organization" extends a pattern they already ship and measure, rather than an idea they reject.
- The gap is confirmed from the labs' own pages. Their judges read one user's chat; nobody judges by company intent, and nobody escalates to the resource's authority holder.
- The signed "who asked, for whom, for what" statement matches DeepMind's attestation chain and AP2's mandate. Cite both: it shows Google's research arm wants this, and that its shipped version covers money only.
- Claude Code's rule that peer messages are never consent is the baseline Lightbulb must beat. A cross-person request has to carry authority that the receiver's judge can verify; otherwise it falls to "route back to you".
- Expect the judge to be compared with 0.4% FPR / 17% FNR (Anthropic) and 99.1% approval / 90% recall (OpenAI). Publish numbers defined the same way, including cross-person cases nobody reports.
- Risk: Anthropic and OpenAI ship org-level policy hooks (`autoMode.environment`, `guardian_policy_config`). Lightbulb's moat is the cross-person, cross-vendor decision point and the record of it, not the classifier.

## 6. Not verified
- OpenAI "Running Codex safely at OpenAI" (openai.com returned 403 to both WebFetch and curl): content unknown.
- The ChatGPT agent system card PDF was fetched from a CDN URL I reconstructed. The text reads "ChatGPT Agent System Card, OpenAI, July 17, 2025", but the HTML page was 403.
- OpenAI 2023 paper date (Dec 2023) is from memory and its references; the cover date was not read.
- DeepMind "Approach to Technical AGI Safety" agent sections: search summary only [S].
- Google Gemini Enterprise / Agentspace governance and the A2A "enterprise-ready" page: not fetched this pass (Gateway covered in 04).
- AP2 v0.2 / FIDO donation date: not shown on the page. Earlier "Intent Mandate / Cart Mandate" names come from the DeepMind paper and memory.
- Microsoft Copilot Studio DLP, Agent 365 approvals, MS Research multi-agent papers: not read.
- AWS governance whitepaper, AgentCore A2A support: not read.
- Salesforce Agentforce Trust Layer: [S] only. Apple Private Cloud Compute agent mentions: not researched. Partnership on AI: not searched.
- FMF Agents for Cyber Defense (21 Sep 2026) and Bio brief (31 Jul 2026): [S] search excerpts.
- Anthropic Project Vend and sabotage evaluations: not read.
- Plan list in Anthropic's default post is inconsistent (Ultra named in some places, not in others), as reported by the fetch.

## Primary pages fetched (29)
1. anthropic.com/engineering/claude-code-auto-mode
2. anthropic.com/engineering/how-we-contain-claude
3. claude.com/blog/auto-mode-default-in-claude-code
4. claude.com/resources/articles/auto-mode-in-production
5. anthropic.com/news/our-framework-for-developing-safe-and-trustworthy-agents
6. code.claude.com/docs/en/agent-sdk/permissions
7. code.claude.com/docs/en/permission-modes
8. code.claude.com/docs/en/cross-session-messaging
9. anthropic.com/research/agentic-misalignment
10. platform.claude.com/docs/en/managed-agents/secure-deployment
11. cdn.openai.com/papers/practices-for-governing-agentic-ai-systems.pdf
12. cdn.openai.com/pdf/839e66fc-.../chatgpt_agent_system_card.pdf
13. openai.github.io/openai-agents-python/human_in_the_loop/
14. learn.chatgpt.com/docs/sandboxing/auto-review
15. alignment.openai.com/auto-review/
16. storage.googleapis.com/gweb-research2023-media/pubtools/1018686.pdf (Google's Approach for Secure AI Agents)
17. arxiv.org/pdf/2602.11865 (DeepMind, Intelligent AI Delegation)
18. ap2-protocol.org
19. Microsoft Taxonomy of Failure Modes in Agentic AI Systems v2.0 (PDF)
20. learn.microsoft.com/en-us/microsoft-agent-365/overview
21. learn.microsoft.com/en-us/entra/agent-id/agent-owners-sponsors-managers
22. ai.meta.com/blog/practical-ai-agent-security/
23. arxiv.org/abs/2505.03574 (LlamaFirewall abstract)
24. arxiv.org/pdf/2505.03574 (LlamaFirewall full text)
25. docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html
26. docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-overview.html
27. docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-terminology.html
28. frontiermodelforum.org/issue-briefs/emerging-security-practices-for-ai-agents/
29. research.google/pubs/an-introduction-to-googles-approach-for-secure-ai-agents/ (to find the PDF link)
