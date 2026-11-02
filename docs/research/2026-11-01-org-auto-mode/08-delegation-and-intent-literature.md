# Delegation, intent and human approval for agents: what the literature says (2 Nov 2026)

Scope: (1) carrying a person's authorization and intent across agent chains, (2) policy vs asking a human, (3) measured effectiveness of enforcement. Extends 01-a2a-security.md and 03-policy-how-it-works.md. It does not restate their numbers (Progent, AgentSpec, CaMeL, Prompt Infection, AgentWorm, 2609.22949, RFC 8693, ID-JAG, OWASP ASI03) except to correct them.

Tags: [P] primary (the paper, draft or vendor's own page, read in full text or in the relevant sections); [P-abs] only the abstract or first page was read; [S] secondary. Quotes are verbatim from the fetched text. PDFs were pulled with curl and pdftotext, so the quotes come from the extracted text.

## 1. Summary findings (12)

1. People do not catch bad approvals. In Anthropic's controlled test with 1,053 paid testers, humans caught 13.6% of planted dangerous commands. The classifier caught 89%. Human block rates fell from about 17% to about 5% after 50 or more prompts. [claude.com auto-mode default, Aug 2026, P]
2. The "93% accept" figure is real and Anthropic-published, but Anthropic itself has two numbers: 93% (engineering post, 25 Mar 2026) and 97% (claude.com, 7 Aug 2026). The same page says users reject 39% of *plans* and only 3% of individual permission prompts. Neither page explains the 93-to-97 gap. [P]
3. Approval at the plan or task level gets real scrutiny; approval per call does not (39% vs 3% rejection). Every policy paper that names approval fatigue (OpenAI 2023, Chan 2025, South 2025, IsolateGPT, OWASP ASI09) lacks this measurement, so this one is the strongest. [claude.com, P]
4. Ben Green's survey of 41 human-oversight policies concludes that "people are unable to perform the desired oversight functions." Requiring oversight "legitimize[s]" faulty systems, so he argues for justified, empirically tested *institutional* oversight instead. [Green 2022, P]
5. The leading academic delegation proposal (South, Marro, Chan et al. 2025) binds *who* (user ID token hash plus agent-ID token), *scope limits*, *validity*, and an optional "summarized goal." It reports no measurements and leaves multi-hop to logged NL interpretations, not to token chaining. [P]
6. Operator scopes cannot express intent. PAuth (UW/OSU/MSR, 2026) argues that OAuth "inherently overprivileges agents" because it authorizes TRANSFER, not "transfer $100 to Bob." It reports 100/100 benign tasks allowed and 634/634 adversarial calls blocked, but only on tasks rewritten so that every operand is explicit. The underspecified AgentDojo originals are blocked by design. [P]
7. LLM-based task-to-scope matching degrades fast with task complexity. In Cisco's ASTRA benchmark, recall falls from 0.98-0.99 on one-tool tasks to 0.57-0.61 on three-tool tasks, so the matcher denies valid requests (an under-scoping false negative rate of up to 0.78). [Cisco 2510.26702, P]
8. Least privilege by tool selection works until the needed tools are also the attack tools. AgentDojo's tool filter cut targeted ASR from 57.69% to 6.84%. But "the tools required to solve the task are also sufficient to carry out the attack" in 17% of cases, and the filter fails when tools cannot be planned up front. [AgentDojo, P]
9. Classifier-style intent checking has a measured floor. Anthropic's auto mode reports 0.4% FPR on 10,000 real actions, but a 17% FNR on 52 real overeager actions and 5.7% on 1,000 synthetic exfiltrations. It anchors on "whether the user authorized the specific action," taken from user messages only. [Anthropic engineering, P]
10. Standards now name intent as the gap but do not close it. OWASP's Agentic Top 10 (Dec 2025) recommends an "intent capsule" and an "Intent Gate" PEP/PDP. NIST's NCCoE concept paper (Feb 2026) only *asks* "How might an agent convey the intent of its actions?" and "How do we handle delegation of authority for 'on behalf of' scenarios?" [P]
11. The token drafts split into two camps: identity-only drafts (the WSO2 OBO draft: one hop, `sub` plus `act`, scope only, expired Feb 2026) and attenuation drafts (Asor WIMSE delegation chain: offline-verifiable subset-of-parent across hops). Txn-Tokens (draft-11, WG consensus) carry an immutable `tctx` and a narrow-purpose `scope`, but "MUST NOT be accepted outside the Trust Domain." None binds the person's actual request text. [P]
12. Industry and academia have diverged. A May 2026 review of 21 production agent systems finds that every one keeps humans in the loop through policy, approval or scope settings (14, 15 and 16 of the 21 respectively), while "intent anchoring and trust labeling" have "zero production deployment." [Wang, Li, Tian 2605.24309, P]

## 2. Table

| Source | Year | Mechanism | Fields bound | Numbers | Open problem |
|---|---|---|---|---|---|
| South, Marro, Chan et al., Authenticated Delegation (2501.09674) | 2025 | OIDC ID token + Agent-ID token + delegation token signed by the human | user (hash of ID token), agent ID, scope limits, optional "summarized goal", expiry/revocation, audit URL; no audience claim defined | none | NL-to-policy translation reliability, multi-hop, prompt-fatigue, OP as surveillance point |
| Chan et al., Infrastructure for AI Agents (2501.10114, TMLR) | 2025 | Catalog: identity binding, certification, IDs, channels, oversight layers, inter-agent comms, commitment devices, incident reporting, rollbacks | ID links agent to user/legal entity and sub-agents | none | which actions to link to identity; flag frequency; rollback moral hazard |
| Chan et al., Visibility into AI Agents (FAccT 2024) | 2024 | Agent identifiers, real-time monitoring, activity logs | agent, user, developer, deployer; "deployed by its user directly or by another agent" | none | detecting sub-agent creation; spoofing; privacy |
| Chan, Kolt et al., IDs for AI Systems (2406.12137) [P-abs] | 2024 | IDs per AI system *instance* (e.g. one chat session) | instance-level info | none | issuer and limits not in abstract |
| Kolt, Governing AI Agents (Notre Dame L. Rev. 101) | 2025 | Agency-law framing: information asymmetry, authority, loyalty, delegation | n/a (legal) | none | when an AI agent may appoint subagents; disclosure up the chain |
| Shavit et al., Practices for Governing Agentic AI (OpenAI) | 2023 | Seven practices incl. approval, attributability, interruptibility | unique agent ID with "user-principal" info | none | which actions need approval; minimum review times vs "rubber stamp" |
| Anthropic, Claude Code auto mode (engineering post) | Mar 2026 | Two-stage transcript classifier; sees user messages + tool calls, not tool outputs | implicit: user's own messages are the authorization source | FPR 0.4% (n=10,000), FNR 17% (n=52), 5.7% (n=1,000 synthetic exfil); 93% prompt acceptance | blast radius of approval-like evidence; out-of-repo trust |
| Anthropic/claude.com, auto mode default | Aug-Oct 2026 | Same; human study | n/a | humans caught 13.6% (143/1,053), classifier 89% (937/1,053); 97% prompt approval; 39% plan rejection; miss rate 12%->7% after red-teaming; 0/720 injections vs Claude 5-series | real-traffic miss rate not reported |
| Green, Flaws of human oversight policies (CLSR 45) | 2022 | Survey of 41 policies | n/a | 41 policies | replace human oversight with justified institutional oversight |
| AgentDojo (NeurIPS 2024 D&B) | 2024 | Benchmark: 97 tasks, 629 security cases | n/a | GPT-4o targeted ASR 57.69% none; tool filter 6.84%; PI detector 7.95% at 41.49% utility; 17% of cases tool-filter-proof | dynamic tool needs, multi-task context |
| InjecAgent (ACL Findings 2024) [P-abs] | 2024 | 1,054 IPI cases, 17 user tools, 62 attacker tools | n/a | ReAct GPT-4 24%, ~2x with hacking prompt | |
| ASB (ICLR 2025) [P-abs] | 2024 | 10 scenarios, 400+ tools, 27 attacks/defenses, 13 LLMs | n/a | highest avg ASR 84.30%; "limited effectiveness" of defenses | |
| GuardAgent (2406.09187) | 2024-25 | LLM guard agent writes guardrail code from "safety guard requests" | access-control rules per role (EICU-AC) | >98% (EICU-AC), 83% (Mind2Web-SC) accuracy | |
| ShieldAgent (2503.22738, ICML 2025) | 2025 | Rules extracted from policy docs into probabilistic rule circuits; verifies trajectories | policy rules over actions | 90.1% rule recall, 4.8% FPR, +11.3% avg over prior; -64.7% API queries, -58.2% time | hallucination-type risks |
| IsolateGPT (NDSS 2025) | 2025 | Hub-and-spoke isolation; inter-app messages routed via hub with user consent | ephemeral app IDs, typed request/response formats | "no loss of functionality"; overhead <30% for 3/4 of queries | an automated permission model "is needed" |
| Cisco, Semantic Task-to-Scope (2510.26702) | 2025 | Authorization server semantically matches task to requested scopes, issues minimal token | task text -> scopes | single-tool LLM matcher F1 0.96; 3-tool recall 0.57-0.61 | "preserving requester context ... at multiple points within the delegation chain" |
| PAuth, Task-Scoped Authorization (2603.17170) | 2026 | Each server derives an "NL slice" from the task; server-signed envelopes bind values to provenance | operator + every operand, derived from task text | 100/100 benign, 634/634 adversarial blocked; $0.002-0.038 per task, once | needs fully specified tasks; per-server cost; proxy-trust fallback |
| Agentic JWT (2509.13597) | 2025 | Intent token + delegation assertion + PoP keys + agent checksum grant | workflow_id, workflow_step, executed_by, initiated_by, delegation_chain, step_sequence_hash, execution_context, scope | "sub-millisecond overhead"; no security results ("forthcoming") | intent = pre-registered workflow, not the person's request |
| Wu et al., Automating Data Access Permissions (2511.17959, IEEE S&P 2026) | 2025 | User study (205 Prolific) + permission prediction model | data type x recipient x context | 85.1% accuracy (85.2% recall); 94.4% on 74.1% high-confidence subset; 66.9% without history | users under- and over-permission |
| Wang, Li, Tian, AHI position (2605.24309) | 2026 | Review: 59 papers, 21 products, 26 plugins | n/a | 14/15/16 of 21 use policy/approval/scope; intent anchoring 0 deployments | AHI design principles and evaluation |
| NIST NCCoE concept paper, Agent Identity & Authorization | Feb 2026 | Proposed demonstration project on OAuth, OIDC, SCIM, SPIFFE | asks about intent and OBO, does not define them | none | everything under "Authorization" is a question |
| OWASP Top 10 for Agentic Applications 2026 | Dec 2025 | Guidance; ASI01 intent capsule, ASI02 Intent Gate, ASI03 signed intent, ASI09 trust exploitation | "subject, audience, purpose, and session" (ASI03) | none | no format specified |
| draft-oauth-ai-agents-on-behalf-of-user-02 (WSO2) | Aug 2025 (expired) | `requested_actor` + `actor_token` in auth code flow | sub (user), act.sub (agent), azp, scope | none | single hop; no purpose/task |
| draft-ietf-oauth-transaction-tokens-11 | Jul 2026 | Txn-Token propagated unchanged through a call chain in one trust domain | txn, sub, scope (narrow purpose), tctx (immutable), rctx, req_wl | none | cross-domain needs identity chaining |
| draft-asor-wimse-agent-delegation-chain-00 | Aug 2026 | RAR-carrying JWTs, par_hash commitment, offline subset check | authority (RAR), aud/locations, del_depth/max_depth, expiry, PoP | none | parent stays valid; revocation latency; fan-out unbounded |
| draft-williams-intent-token-02 | Sep 2026 | "intent+jwt" signed human-declared envelope per action | principal, declared_intent{action_class, scope, bounds}, parent_token_jti | none | individual draft tied to a patent application (GB2603013.0) |

## 3. Per-source detail

### 3.1 South, Marro, Chan et al., "Authenticated Delegation and Authorized AI Agents" (arXiv 2501.09674, Jan 2025) [P, read chars 0-100k of 129k]
URL: https://arxiv.org/abs/2501.09674
- (a) Three tokens on OAuth 2.0/OIDC: the user's ordinary OIDC ID token; an Agent-ID token ("issued as an OAuth2.0 Native Client", with "the owner of the AI Agent control[ling] all keying material"); and a delegation token "issued and signed by the human delegator" that "carries references to (e.g., hash of) the corresponding user's ID token" and the Agent-ID token. The delegation token may carry "the summarized goal of the agent and its scope limitations" and "should specify validity conditions, such as expiration time or revocation endpoints"; audit URLs are optional. No audience claim is defined. Scope is "a unique and interesting challenge" (s3.4). Enforcement is meant to sit on structured resource policies (XACML, SQL policies) that an LLM drafts from the user's NL and "the user reviews and finalizes," which the paper calls "the key final step" (s4.4). NL on its own is "not reliable enough to be used as standalone policy mechanisms."
- Multi-hop (s4.4.1): agent Alice forwards the user's authorization description to agent Bob, Bob returns a structured interpretation of its permissions, and that interpretation is "logged and approved by either the user or Alice." There is no cryptographic re-delegation or attenuation.
- (b) No measurements.
- (c) Default rule: "any resource that is neither explicitly approved nor explicitly forbidden requires human approval"; warns of "prompt fatigue" (s4.2).
- (d) NL-to-policy robustness; prompt injection producing policies beyond intent; instruction drift; the OP becoming "a single point of surveillance"; VC/GNAP alternatives; standardized scope definitions (s5).

### 3.2 Chan, Wei, Huang, Rajkumar, Perrier, Lazar, Hadfield, Anderljung, "Infrastructure for AI Agents" (arXiv 2501.10114 v3, Jun 2025, TMLR) [P, read chars 0-100k of 126k]
URL: https://arxiv.org/html/2501.10114v3
- (a) Three functions: attribution, shaping interactions, detecting and remedying harm. Identity binding links an agent to a legal entity; "linking an identity to an agent does not mean making the identity publicly visible" (s3.1). IDs: "An ID could link the activities of these sub-agents to the original agent" (s3.3). Oversight layers: the overseer may be a user, a manager, or "a trusted, automated system could take the place of a user" (s4.2).
- (b) None.
- (c) "Humans could ignore such flags if they are too frequent or present information in a complex manner"; "Humans could also have predispositions to trust automated systems without sufficient verification of their behaviour" (s4.2). Rollbacks create moral hazard: "developers could have weaker incentives to implement effective oversight layers" (s5.2).
- (d) Open: "When should IDs link to information identifying the user?"; how to let users "set the degree of oversight"; "Beyond encryption, how could inter-agent communication protocols be secured?"; certificate revocation; "certifications could provide a false sense of security."

### 3.3 Chan et al., "Visibility into AI Agents" (arXiv 2401.13138 v6, FAccT 2024) [P, read chars 0-100k of 145k]
- (a) Agent identifiers, real-time monitoring, activity logs. An identifier can link an action to "an AI agent and its user, developer(s), and deployer." The agent card can record "how the agent was deployed (e.g., by its user directly or by another agent)."
- (c) Flagged high-impact actions can be paused until the deployer verifies explicit user approval; KYC for high-risk actions.
- (d) "we lack methods for determining when an agent has created a sub-agent"; logs "may have to be combined if one agent can be viewed as a continuation of another"; visibility "is not by itself sufficient."

### 3.4 Chan, Kolt et al., "IDs for AI Systems" (arXiv 2406.12137 v3, Oct 2024, NeurIPS RegML) [P-abs]
- (a) IDs attach to *instances*, e.g. "a particular chat session with Claude 3"; "IDs seem most warranted in settings where AI systems could have a large impact upon the world." I did not read the field list or issuer model.

### 3.5 Kolt, "Governing AI Agents" (arXiv 2501.07913 v2, Feb 2025; Notre Dame L. Rev. vol. 101) [P, full text]
- (a) Authority, quoting Frankel: "the purpose for which the fiduciary is allowed to use his delegated power is narrower than the purposes for which he is capable of using that power." Agents "act only within the scope of the authority granted to them," but instructions "are often, if not always, ambiguous." Under the Restatement, the agent must "infer, in a reasonable manner, what the principal desires" and can be obligated to seek clarification, so authority "is not a rigid or static mandate but a dynamic and iterative process of interpretation and interaction."
- Delegation (s II.D): the default rule is that an agent cannot appoint a subagent (*delegatus non potest delegare*) except where "the agent reasonably believes, based on a manifestation from the principal, that the principal consents," or in emergencies. Open questions: when an AI agent may appoint subagents (including humans), and whether subagents must disclose "also to the human principal that appointed the original AI agent."
- (c) Conventional fixes ("incentive design, monitoring, and enforcement") may fail for agents that are uninterpretable and operate at speed and scale. Proposes the principles inclusivity, visibility, liability.
- Implication for authority maps: the law already distinguishes the principal (who grants authority) from the agent who holds it, and makes sub-delegation default-deny. That matches "escalate to the resource's authority holder," and it argues that sub-delegation should need an explicit manifestation from the principal.

### 3.6 Shavit, Agarwal, Brundage et al. (OpenAI), "Practices for Governing Agentic AI Systems" (Dec 2023) [P, full text]
URL: https://cdn.openai.com/papers/practices-for-governing-agentic-ai-systems.pdf
- The seven practices (s4.1-4.7): Evaluating Suitability for the Task; Constraining the Action-Space and Requiring Approval; Setting Agents' Default Behaviors; Legibility of Agent Activity; Automatic Monitoring; Attributability; Interruptibility and Maintaining Control.
- (c) Approval: "Requiring a user to proactively authorize these actions ... is a standard way to limit egregious failures," but this is "made harder when the user must approve many decisions and thus must make each approval quickly." Also: "An AI agent could circumvent a hard-coded restriction by causing another party to take the action on the system's behalf ... an agent could send an email—an allowed action—to a non-user human that convinces said human to take the disallowed action." Open questions: "which interactions need human approval, as a function of a model's empirical reliability"; "minimum review times ... to avoid their turning into a 'rubber stamp'"; "How can the effectiveness and meaningfulness of human oversight be evaluated?"
- (a) Attributability: "each agentic AI instance assigned a unique identifier ... which contains information on the agent's user-principal"; third parties "could demand such identification before starting the interaction."
- Interruptibility: "a critical backstop"; open question: "How can system deployers ensure that agents only spawn sub-a[g]ents that can be similarly turned off?"

### 3.7 Anthropic, "How we built Claude Code auto mode" (25 Mar 2026) [P]
URL: https://www.anthropic.com/engineering/claude-code-auto-mode
- (c) "Users accept 93% of manual prompts" (approval fatigue). The classifier sees only user messages and tool calls; assistant prose and tool outputs are stripped ("reasoning-blind by design"), which is the main prompt-injection defense. Authorization comes from the user's words: it checks "whether the user authorized the specific action," not whether the action relates to the goal. "clean up my branches" does not authorize a batch delete; "can we fix this?" is a question, not a directive. A tool-calls-only variant had the lowest FNR but lost "the user-intent anchor." After 3 consecutive or 20 total denials it escalates to the human; headless runs terminate.
- (b) Stage 1 / full pipeline: FPR on real traffic (n=10,000) 8.5% / 0.4%; FNR on real overeager actions (n=52) 6.6% / 17%; synthetic exfiltration FNR (n=1,000) 1.8% / 5.7%.
- (d) The trust boundary is the git repo; "not a replacement for careful human review on high-stakes infrastructure"; the misaligned-model threat is out of scope.

### 3.8 Anthropic/claude.com, "Auto mode is now the default in Claude Code" (7 Aug 2026, updated 22 Oct 2026) [P]
URL: https://claude.com/resources/articles/auto-mode-default-in-claude-code
- (c)(b) "users approve 97% of permission prompts in Claude Code"; users reject 39% of plans but 3% of permission requests. Controlled study: 1,053 paid professional testers on a study environment, with one prompt swapped for a clearly dangerous command. Humans caught 13.6% (143/1,053), auto mode 89% (937/1,053); auto mode blocked 800 that humans approved, humans blocked 6 that auto mode allowed. Human block rate went from ~17% early in a session to ~5% after 50+ prompts. Miss rate on synthetic attack sets went from 12% to 7% after Apollo Research hardening (not a real-traffic rate). Trajectory Labs: 0/720 injection attempts succeeded against Claude 5-series models in auto mode, versus 5.83% against GPT-5.6 Sol in Codex Auto-review.
- Caveat: vendor-run study of the vendor's own product, and testers knew they were being evaluated. Still, it is the only published measurement of human approval accuracy for agent actions that I found.

### 3.9 Green, "The flaws of policies requiring human oversight of government algorithms" (Computer Law & Security Review 45, 2022; arXiv 2109.05067) [P]
- (c) Survey of 41 policies: "people are unable to perform the desired oversight functions," and oversight policies "legitimize government uses of faulty and controversial algorithms," providing "a false sense of security." Automation bias "persists even after training and explicit instructions to verify." Proposal: agencies must justify use and show "that any proposed forms of human oversight are supported by empirical evidence," followed by democratic review.
- Relevance: the remedy is an institution that justifies policy in advance, not a human checking each decision. That maps to the company's written intent and authority map, with humans reserved for escalations.

### 3.10 AgentDojo (Debenedetti et al., NeurIPS 2024 D&B; arXiv 2406.13352 v3) [P]
- (b) GPT-4o, "important message" attack, Table 5: no defense: benign utility 69.0%, utility under attack 50.01%, targeted ASR 57.69%. Delimiting 41.65%. PI detector ASR 7.95% but benign utility 41.49%. Repeat prompt 27.82%. Tool filter ASR 6.84% at 73.13% utility. (The body text says "lowering the attack success rate to 7.5%"; the table says 6.84%.) Slack suite ASR 92%. "Most models incur a loss of 10%–25% in absolute utility under attack." "More capable models tend to be easier to attack."
- (d) The tool filter fails "when the list of tools to use cannot be planned in advance" or "when the tools required to solve the task are also sufficient to carry out the attack (this is true for 17% of our test cases)", and across multi-task sessions an injection can "wait." It is also vulnerable when an injection "solely aims to alter the result of a given tool call." Cost: ~US$35 for the 629 cases on GPT-4o.

### 3.11 InjecAgent (Zhan et al., ACL Findings 2024) [P-abs]
- 1,054 cases, 17 user tools, 62 attacker tools, 30 agents; ReAct GPT-4 vulnerable 24% of the time, "nearly doubled" with a hacking prompt.

### 3.12 Agent Security Bench (Zhang et al., ICLR 2025) [P-abs]
- 10 scenarios, 10 agents, 400+ tools, 27 attack/defense methods, 13 backbones; "highest average attack success rate of 84.30%"; current defenses of "limited effectiveness."

### 3.13 GuardAgent (Xiang et al., arXiv 2406.09187 v3, May 2025) [P]
- (a) A guard agent turns "safety guard requests" into a plan and then executable guardrail code. EICU-AC is a role-based access-control benchmark for healthcare agents.
- (b) "over 98% and 83% guardrail accuracies" on EICU-AC and Mind2Web-SC.
- Note: this is policy written in NL and compiled to code, the same family as South's NL-to-policy step, with numbers.

### 3.14 ShieldAgent (Chen, Kang, Li, arXiv 2503.22738 v2, Nov 2025; ICML 2025) [P]
- (a) Extracts "verifiable rules from policy documents" into "action-based probabilistic rule circuits" and verifies the protected agent's trajectory.
- (b) ShieldAgent-Bench (3K pairs, 6 web environments, 7 risk categories): FPR 4.8%, rule recall 90.1%, +10.2% accuracy over the best baseline (the abstract says +11.3% averaged over benchmarks); -64.7% API queries, -58.2% inference time. Gains are noted on ST-WebAgentBench's "User Consent" and "Boundary and Scope Limitation" categories.
- (d) Weaker on hallucination-type risks that need knowledge outside the policy.

### 3.15 IsolateGPT (Wu, Roesner, Kohno, Zhang, Iqbal; NDSS 2025; arXiv 2403.04960) [P]
- (a) Hub-and-spoke: apps run in isolated spokes and cross-app requests go through the hub with "ephemeral identifiers" and typed `<functionality, request|response>` formats. The hub cross-checks each request against its own plan and warns the user when a request is unexpected (dialog: Allow Once / Session / Always / Don't Allow).
- (b) "without any loss of functionality"; overhead "under 30% for three-quarters of tested queries."
- (c) "requires users to manually screen messages exchanged between spokes, as currently there are no foolproof mechanisms to automatically detect malicious natural language instructions," with "several measures to ease the user fatigue." (d) "a comprehensive permission model is needed for a more automated regulation of actions." (SecGPT is the earlier name of this work; I read only this version.)

### 3.16 Cisco, "Delegated Authorization for Agents Constrained to Semantic Task-to-Scope Matching" (El Helou et al., arXiv 2510.26702, 30 Oct 2025) [P]
- (a) The authorization server inspects the task semantically and issues tokens "constrained to the minimal set of scopes necessary for the agents' assigned tasks." A "trusted proxy" captures user intent. It names TBAC (task-based access control).
- (b) Single tool: LLM matcher accuracy 0.96, recall 0.98-0.99, F1 0.96 (embedding matcher recall 0.55). Two tools: recall 0.82-0.88. Three tools: recall 0.57-0.61 on ASTRA (0.86 on Toucan); under-scoping FNR "reaching 0.78 on the three-tool dataset."
- (d) "applying delegated authorization in this way across chains of agents demands mechanisms for preserving requester context and enforcing appropriate scopes at multiple points within the delegation chain." Under-scoping "can hinder, stall, or cause the failure" of tasks.

### 3.17 PAuth, "Beyond OAuth: Task-Scoped Authorization for AI Agents via Natural Language Slices" (Sharma, Jiang, Chen, Lin; arXiv 2603.17170 v2, 25 Aug 2026) [P]
- (a) OAuth "grants permissions over operators (e.g., TRANSFER), not operations (operator plus operands)"; "Operator-scoped authorization therefore inherently overprivileges agents." Submitting a concrete NL task "implicitly authorizes exactly the operations its faithful execution requires, even when the agent is compromised." Each server derives an "NL slice" (a symbolic spec of the expected call), and server results are wrapped in "signed envelopes that bind concrete values to symbolic provenance." Bound: the operator, every operand, and the provenance of each operand. Threat model: the agent is untrusted and enforcement is server-side, framed explicitly as the confused deputy.
- (b) AuthBench (on AgentDojo; cross-validated on OpenClaw): 5 suites, 100 benign tasks, 634 adversarial calls; all benign authorized, all adversarial blocked. Cost $0.002-$0.038 per task, paid once per server; runtime checks are deterministic.
- Caveats from the text: tasks were *refined* so that operands are inline; the "original underspecified version" is blocked by design. Adversarial calls are constructed operand changes, not adaptive attacks. NL ambiguity is declared "an orthogonal problem," handled by a confirmation loop. Without server adoption, a trusted proxy holds the OAuth tokens, "so the security guarantee reduces to the proxy's integrity."
- (c) "frees users from having to approve each call with concrete operand values, including intermediate results they never specified."

### 3.18 Agentic JWT (Goswami, arXiv 2509.13597, Sep 2025) [P]
- (a) Intent token claims: `intent{workflow_id, workflow_step, executed_by, initiated_by, delegation_chain[], step_sequence_hash, execution_context{...}}`, plus `scope`, `cnf` (PoP key thumbprint), and an `agent_checksum` grant (hash of prompt, tools and config). Services that ignore the claims can use it as a regular JWT.
- (b) "sub-millisecond overhead"; the evaluation is functional only, and "A comprehensive performance and security evaluation ... will appear in our forthcoming journal submission."
- (d) "Intent" here means a pre-registered workflow and step, not the person's actual request. It binds *which code path*, not *what was asked*.

### 3.19 Wu, Yang, Roesner, Kohno, Zhang, Iqbal, "Towards Automating Data Access Permissions in AI Agents" (arXiv 2511.17959, Nov 2025; IEEE S&P 2026) [P]
- (c) "constantly interrupting users for permissions is incompatible with the automation promised by AI agents." Vignette study with 205 Prolific participants: "under- and over-permissioning are common," and fewer participants grant automatic enforcement when the agent makes mistakes.
- (b) Prediction model: 85.1% accuracy (recall 85.2%); 94.4% when restricted to high-confidence predictions (covering 74.1% of data); 66.9% without history, rising 10.8 points with 1-4 history samples.

### 3.20 Wang, Li, Tian, "Reframing LLM Agent Security as an Agent–Human Interaction Problem" (arXiv 2605.24309, May 2026) [P, first pages]
- (c) 59 papers, 21 production systems, 26 plugins (April 2026). Policy specification, runtime approval and scope configuration are used by 14, 15 and 16 of 21 systems; "intent anchoring and trust labeling" have "zero production deployment." "No deployed system trusts an LLM alone to determine whether an action is safe." The current mechanisms leave users "caught between approval fatigue and uncontrolled agent autonomy."

### 3.21 NIST NCCoE, "Accelerating the Adoption of Software and AI Agent Identity and Authorization" concept paper (Booth, Fisher, Galluzzo, Roberts; 5 Feb 2026; comments closed 2 Apr 2026) [P]
URL: https://www.nccoe.nist.gov/sites/default/files/2026-02/accelerating-the-adoption-of-software-and-ai-agent-identity-and-authorization-concept-paper.pdf
- Questions, verbatim: "What are the mechanisms for an agent to prove its authority to perform a specific action?"; "How might an agent convey the intent of its actions?"; "How do we handle delegation of authority for 'on behalf of' scenarios?"; "How do we bind agent identity with human identity to support 'human-in-the-loop' authorizations?"; "How do we establish 'least privilege' for an agent, especially when its required actions might not be fully predictable"; "How can we ensure that agents log their actions and intent in a tamper-proof and verifiable manner?"; "How do we ensure non-repudiation for agent actions and binding back to human authorization?" Scope ranges "from controlled human-in-the-loop approval to autonomous action." It is built on OAuth/OIDC/SCIM/SPIFFE, and no answers are given.

### 3.22 OWASP GenAI, "Top 10 for Agentic Applications for 2026" (9 Dec 2025) [P]
URL: https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/
- Items: ASI01 Agent Goal Hijack, ASI02 Tool Misuse and Exploitation, ASI03 Identity and Privilege Abuse, ASI04 Agentic Supply Chain, ASI05 Unexpected Code Execution, ASI06 Memory & Context Poisoning, ASI07 Insecure Inter-Agent Communication, ASI08 Cascading Failures, ASI09 Human-Agent Trust Exploitation, ASI10 Rogue Agents.
- (a) ASI01: "validate both user intent and agent intent before executing goal-changing or high-impact actions. Require confirmation - via human approval, policy engine, or platform guardrails"; "intent capsule ... bind the declared goal, constraints, and context to each execution cycle in a signed envelope." ASI02: "Policy Enforcement Middleware ('Intent Gate') ... A pre-execution Policy Enforcement Point (PEP/PDP) validates intent and arguments ... issues short-lived credentials." ASI03 (the "subject, audience, purpose, and session" text, already in 01) adds "Prevent privilege inheritance across agents unless the original intent is re-validated."
- (c) ASI09 names automation bias, "Fake Explainability," and "Consent laundering through 'read-only' previews"; a hijacked agent makes "the human into performing the final, audited action, making the agent's role in the compromise invisible to forensics." Mitigations: risk-scored confirmations, "confidence weighted cues," and UI differentiation. This says approval itself is an attack surface.
- Correction to 01: I read only the 2026 Top 10. The separate "Agentic AI - Threats and Mitigations" v1.0a (Feb 2025, T1-T15) was seen only via secondary summaries.

### 3.23 draft-oauth-ai-agents-on-behalf-of-user-02 (Senarath, Dissanayaka / WSO2; 26 Aug 2025; expired 27 Feb 2026; Informational, no WG) [P]
- (a) `requested_actor` (auth request) and `actor_token` (token request). The access token carries `sub` (user), `act.sub` (agent), `azp`, `scope`; the code is bound to user, client and actor. Consent "MAY" be shown. One hop; no purpose or task parameter.

### 3.24 draft-ietf-oauth-transaction-tokens-11 (30 Jul 2026; WG consensus, waiting for write-up) [P]
- (a) `txn`, `sub`, `scope` (TTS-set, should capture purpose "as narrowly as possible"), `tctx` ("values that remain immutable throughout the Call Chain"), `rctx` (environment), `req_wl`. It propagates unchanged ("MUST include the Txn-Token as it was received"). Replacement may only reduce: it "MUST NOT enable modification to asserted values that expand the scope." "MUST NOT be accepted outside the Trust Domain"; cross-domain use goes via identity chaining.
- Relevance: the nearest standards-track thing to "a signed statement of who asked, for what, carried through the chain," but it is intra-domain only.

### 3.25 draft-asor-wimse-agent-delegation-chain-00 (Asor; 27 Aug 2026; Standards Track intended, individual) [P]
- (a) Each hop gets a JWT with RAR authority, `par_hash` (SHA-256 of the parent's signing input), `del_depth`/`del_max_depth`, pinned `aud`/locations, PoP (RFC 9449) and status-list revocation. Offline verification enforces "monotonic attenuation, bounded depth, and monotonic expiry." It states that RFC 8693's `act` "is informational only and cannot enforce attenuation across a chain of depth two or more" (consistent with 01).
- (d) "Parent tokens remain valid after attenuation"; revocation lags under offline verification; fan-out "is not limited by the token format." The confused-deputy claim (s9.3) is only that a child cannot exceed the *parent's* authority. An injection that stays inside the parent's authority is not addressed.

### 3.26 draft-williams-intent-token-02 (Williams, independent; Sep 2026; Informational) [P]
- (a) "intent+jwt": `principal{id,type,auth_method}`, `declared_intent{action_class, scope, bounds{...}}`, `parent_token_jti`, an `ibt_level` hierarchy, and sub-tokens a strict subset of the parent. It states that existing standards "do not propagate declared human intent through the delegation chain."
- (d) Cites the "Intent-Bound Authorization (IBA) framework, patent application GB2603013.0." It is an individual draft with IPR attached, so treat it as a signal of demand, not a standard to build on.

## 4. What the literature says a working design must have, and what nobody has measured

Must have (convergent across sources):
1. Identity of the person, the agent and each hop, linked by signature or hash (South; Asor; A-JWT; OBO draft).
2. Authority that can only shrink per hop, checked at each enforcement point (Asor; Txn-Tokens replacement rule; Kolt's default-deny on sub-delegation).
3. Authorization bound to the *operation* (operator plus operands) or the task, not the operator scope (PAuth; Cisco; AgentDojo's 17% residue).
4. The person's own words as the intent anchor, isolated from tool output and agent prose (Anthropic auto mode; CaMeL in 03).
5. A deterministic enforcement point the agent cannot talk past (OWASP "Intent Gate"; PAuth; South "structured permissions").
6. Escalation instead of per-call approval: approve at plan or task level, escalate only on deviation (39% vs 3%; 13.6% catch rate; Green).
7. Tamper-evident logs tied back to the human authorization (NIST; Visibility; OWASP ASI09 forensics).
Nobody has measured:
- Any delegation-token scheme (South, A-JWT, Asor, Intent Token, OBO) against prompt-injection benchmarks. All report zero or functional-only results.
- Intent checks across *two different people's* agents, or across organizations. Every benchmark is single-user.
- Escalation routed to a resource's authority holder rather than the requester: no study of whether that person decides better than the requester (the 13.6% figure is the requester-approver case).
- The utility cost of requiring fully specified tasks (PAuth blocks the underspecified originals but reports no rate).
- Multi-hop recall of task-to-scope matching (Cisco stops at 3 tools, one hop).

## 5. What this changes for Lightbulb's thesis

- The strongest evidence supports the thesis's core move. Per-call owner approval fails (13.6% caught; 3% rejection), plan-level review gets scrutiny (39%), and Green argues for institutional, pre-justified rules. "Company's written intent decides, humans see escalations" is the design the evidence points to.
- A "signed statement of who asked, for whom, for what" is necessary but not sufficient. Every signed-intent proposal binds identity, scope or workflow-id, not operands. PAuth and AgentDojo show the attack lives in the operands ("send to *this* IBAN"). The decision point must check operands against the request, or the signature launders an injected action.
- Escalating to the resource holder rather than the agent's owner is new territory. Kolt's agency law supports it (the authority holder decides sub-delegation), but no paper measures it, so Lightbulb would be first and should instrument catch rate and fatigue from day one.
- Cross-person and cross-vendor chains are exactly where the standards stop: Txn-Tokens are intra-domain, ID-JAG and OBO are one hop, Asor attenuates but does not bind intent. A company-controlled decision point that re-mints per hop fills a real, named gap (NIST's open questions).
- Expect the LLM part of any intent check to have a floor: about 17% FNR in auto mode, recall of 0.57-0.61 on three-tool tasks in Cisco. Deterministic checks on structured fields, with an LLM only for drafting rules (South, GuardAgent, ShieldAgent), are the measured path.

## 6. Not verified

- CSA "Agentic AI Identity and Access Management: A New Approach" (18 Aug 2025): the download is gated; I saw only the listing description (DIDs, VCs, zero trust, MAESTRO). Its arXiv companion 2505.19301 was not fetched.
- OWASP "Agentic AI - Threats and Mitigations" v1.0a (Feb 2025) and OWASP "Agent Name Service": secondary sources only.
- NIST AI 600-1 agent sections and any 2026 NIST agent profile beyond the NCCoE concept paper: not fetched.
- ID-JAG and WIMSE architecture drafts: not re-read (covered in 01). Huawei draft-song-oauth-ai-agent-collaborate-authz-00 and draft-sweeney-wimse-credential-delegation: search snippets only.
- Linux Foundation AAIF / MCP governance work on authority: not found or fetched.
- InjecAgent, ASB and IDs for AI Systems: abstracts only. SecGPT as a separate paper: not read (IsolateGPT is its successor).
- Green & Chen's 2019 experimental studies of human-algorithm decisions: not read; Green 2022 was read instead.
- Anthropic's "How we contain Claude across products" (said to carry ~0.4% FPR and ~17% miss) and the alignment-blog red-teaming post: not fetched. The 93% vs 97% gap is unexplained in the primary pages.
- The South et al. full author list and the last 29k characters of that paper (likely appendices) were not read. Infrastructure and Visibility were each read to 100k characters.
- Prior-file numbers (Progent, AgentSpec, CaMeL, 2609.22949, AgentWorm) were not re-checked here.
- No 2025-2026 arXiv paper was found that evaluates attenuated capability tokens against LLM prompt injection; the search found only drafts and blogs.

## Primary documents fetched (26)

1. South et al., arXiv 2501.09674 (HTML)
2. Chan et al., Infrastructure for AI Agents, arXiv 2501.10114 (abs + HTML v3)
3. Chan et al., Visibility into AI Agents, arXiv 2401.13138 (HTML)
4. Chan, Kolt et al., IDs for AI Systems, arXiv 2406.12137 (abs)
5. Kolt, Governing AI Agents, arXiv 2501.07913 (abs + PDF)
6. Shavit et al., OpenAI Practices for Governing Agentic AI Systems (PDF)
7. AgentDojo, arXiv 2406.13352 (abs + PDF)
8. InjecAgent, arXiv 2403.02691 (abs)
9. Agent Security Bench, arXiv 2410.02644 (abs)
10. GuardAgent, arXiv 2406.09187 (PDF)
11. ShieldAgent, arXiv 2503.22738 (PDF)
12. IsolateGPT, arXiv 2403.04960 (PDF)
13. Green, arXiv 2109.05067 / CLSR 45 (PDF)
14. Anthropic, How we built Claude Code auto mode (anthropic.com/engineering)
15. Claude, Auto mode is now the default (claude.com/resources)
16. Cisco, arXiv 2510.26702 (PDF)
17. PAuth, arXiv 2603.17170 (PDF)
18. Agentic JWT, arXiv 2509.13597 (PDF)
19. Wu et al., arXiv 2511.17959 (PDF)
20. Wang, Li, Tian, arXiv 2605.24309 (PDF)
21. NIST NCCoE concept paper (PDF)
22. OWASP Top 10 for Agentic Applications 2026 (resource page + PDF)
23. draft-oauth-ai-agents-on-behalf-of-user-02 (datatracker)
24. draft-ietf-oauth-transaction-tokens (datatracker, rev -11)
25. draft-asor-wimse-agent-delegation-chain-00 (ietf.org archive)
26. draft-williams-intent-token-02 (ietf.org archive)
