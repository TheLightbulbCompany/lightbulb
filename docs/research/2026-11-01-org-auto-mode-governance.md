# Auto mode for the organization: what the evidence says (2026-11-01)

Research for the thesis that Lightbulb's next layer is **company intent and authority deciding what agents may do, across people and vendors, with humans seeing only escalations**. Four questions from the founder: how agents talk to each other over networks and what breaks; what actions companies want governed; what "policy" concretely means and how it works; what already exists online. A fifth strand inventories what Lightbulb's own code enforces today.

Five researchers read primary sources (specs, vendor docs, papers, code) and wrote one file each; those are in [`2026-11-01-org-auto-mode/`](2026-11-01-org-auto-mode/) with a source link on every claim and a "not verified" list at the end of each. This document is the synthesis. `[S]` marks a claim that rests on a secondary source. Where a number comes from a vendor-run survey, it says so.

The incident that started this: Friday (a cloud agent) asked a session of the founder's, "for Aden", to read the founder's private business-plan session. Nothing attested who was really asking. The only control was the receiving agent's judgment; it declined, its reply failed, and Friday got silence. A legitimate request produced no answer, and nothing would have stopped a less careful agent from reading the file.

## Summary

1. **No standard carries the original person's request across agent hops.** A2A says "Identity information is handled at the protocol layer, not within A2A semantics" and warns that in-band credentials "can allow credentials to be passed across chains of multiple A2A agents". RFC 8693 keeps the actor chain in `act` but says prior actors "are informational only" and "not to be considered in access control decisions". ID-JAG covers one hop and asserts which user and client, not what the user asked. MCP forbids token pass-through outright. The "for Aden" problem is a hole in the standards, not just in our code. ([01](2026-11-01-org-auto-mode/01-a2a-security.md) §1-3)
2. **OWASP names our incident class.** ASI03 "Identity and Privilege Abuse" lists "Cross-Agent Trust Exploitation (Confused Deputy)", "Synthetic Identity Injection" and "Identity Sharing". Its mitigation is per-action authorization and "Bind OAuth tokens to a signed intent that includes subject, audience, purpose, and session", plus "Prevent privilege inheritance across agents unless the original intent is re-validated." ([01](2026-11-01-org-auto-mode/01-a2a-security.md) §5)
3. **Injection spreads between agents, and signing helps.** Prompt Infection (arXiv 2410.07283) shows self-replicating prompts crossing a chain; tagging alone cut attack success by only 5%, tagging plus marking blocked all. A 2026 lab paper reports message signing with provenance cut inter-agent injection 91% and that 67% of agents violated scope despite system-prompt guardrails. ([01](2026-11-01-org-auto-mode/01-a2a-security.md) §4)
4. **What companies want governed, in order:** access to and sharing of privileged data (60% and 57% name it a top concern; 33% and 31% have seen it), agents going out of scope (58%; 39% saw an agent reach unauthorized systems), external API calls or transactions (53.9% of affected orgs), modifying or deleting data (41.9%), credential disclosure (23%), ordering things or spend (16%, the lowest). Only 17.5% say they can automatically stop a misbehaving production agent. All vendor-run surveys. ([02](2026-11-01-org-auto-mode/02-governed-actions-demand.md) §2)
5. **Vendors gate the technical set, not business intent.** Claude Code, Codex, Cursor, Copilot and Devin gate shell, file writes, network egress, MCP and merge. Business actions are gated only as per-action flags (Agentforce `require_user_confirmation` [S]) or connector blocks (Copilot Studio DLP). Nothing gates by stated intent or amount. Cursor's own docs call its allowlist "best-effort, not a security boundary". ([02](2026-11-01-org-auto-mode/02-governed-actions-demand.md) §1)
6. **Governance maturity is low and Gartner says uniform controls fail.** Deloitte: 21% have a mature agentic governance model (n=3,235). Gartner's 26 May 2026 release defines four autonomy levels; Level 3 "act with approval" needs "explicit human approval for every action" and "degrades under approval fatigue"; Level 4 "act autonomously" needs "continuous monitoring, enforced guardrails, rapid rollback mechanisms, circuit breakers". ([02](2026-11-01-org-auto-mode/02-governed-actions-demand.md) §2)
7. **"Policy" is a function plus a record.** Every mature engine computes (subject, delegation chain, action, resource, context) -> allow / deny / ask / modify, and logs it (XACML's PEP/PDP/PIP/PAP). Authority ("who may instruct whom") is relationship-shaped and fits Zanzibar/OpenFGA; intent and rules ("code freeze until Friday") are attribute-shaped and fit Cedar/OPA/CEL. No agent system read has a first-class delegation chain; they carry user id and agent id side by side. ([03](2026-11-01-org-auto-mode/03-policy-how-it-works.md) §1-2)
8. **Intent is never the policy language; it gets compiled.** The robust approaches (Progent, AgentSpec, CaMeL) have an LLM turn the task into symbolic rules over tool and arguments, then a deterministic engine enforces. Progent cut AgentDojo attack success from 39.9% to 1.0%; AgentSpec enforces in 1-3 ms. The loss is in the compile step: LLM-written rules recall 71-87%. Anthropic's classifier is the one approach that judges intent at runtime, and it reports a 17% miss rate. ([03](2026-11-01-org-auto-mode/03-policy-how-it-works.md) §3)
9. **On a laptop, only the OS sandbox and root-owned managed settings are hard.** Claude Code's docs say Bash argument patterns are "fragile", that hooks cannot override deny rules but exit-1 is non-blocking, and that the sandbox does not cover built-in tools, hooks, MCP servers, `~/.ssh` or the unsandboxed retry. Codex is fail-closed when its managed requirements cannot be fetched. Nothing in-process is a boundary against a same-user agent. ([03](2026-11-01-org-auto-mode/03-policy-how-it-works.md) §5)
10. **Nobody found governs one person's agent asking another person's agent, across vendors or companies, by stated company intent.** Harness controls are single-vendor (Claude Code `autoMode.*` prose rules; Codex `requirements.toml`). Gateways govern agent-to-tool and in-cluster agent-to-agent inside one operator's domain (agentgateway A2A plus CEL; Google Agent Gateway default-deny on agent identity; AgentCore Policy in Cedar, GA 3 Mar 2026; Aembit user plus agent in one policy). Cross-vendor laptop enforcement is claimed only by Unbound AASB [S]. This is an absence from the pages read, not proof. ([04](2026-11-01-org-auto-mode/04-existing-products.md))
11. **Lightbulb has the identity half and almost none of the decision half.** Enforced today: signed roster, computer sign-in proofs, person-only ops, network-worker Allow/Ask/Refuse with signed approvals, Link purchase caps, the inference gate. Not there: message holds are dead code, hooks are observers with no PreToolUse, only `money_moves` of the company policy map is read, any workspace member can approve a Teams card, trust is flat, and there is no org audit log. Two doc claims overstate the code (the Hermes "threat guard" is a 4-pattern deny list; governed-shell approval is `mode: off`). ([05](2026-11-01-org-auto-mode/05-lightbulb-inventory.md))

## 1. Agents communicating over networks: what breaks

**The standards authenticate the caller, not the request.**
- A2A: servers "MUST authenticate every incoming request" and then authorize "based on the authenticated identity and its own policies"; the policy model is undefined. Task scoping is per authenticated *client*. Agent Cards "MAY be digitally signed" and clients "SHOULD verify"; a signature proves who published the card, not who is asking. `TASK_STATE_AUTH_REQUIRED` "MUST NOT" be treated as authorization.
- MCP: tokens must be audience-bound and servers "MUST NOT accept or transit any other tokens". So every hop mints its own token, which is correct for safety and also means the chain is severed at each hop unless something else carries it.
- RFC 8693 (token exchange) and ID-JAG (the Okta/MCP enterprise extension) carry an actor chain, but access control is told to ignore the prior actors, and ID-JAG assumes both sides share one enterprise IdP. A network of different people, companies and vendors has no such IdP.
- Identity products (Entra Agent ID, Okta Cross App Access, SPIFFE) attest *which agent and which user inside one trust domain*. SPIFFE's own text: workload isolation is "beyond the scope of SPIFFE". None attest that a message reflects the person's real request.

**The measured attacks are chain attacks.**
- Prompt Infection: data theft needs at least three cooperating agents; self-replication is "the only scalable method for compromising more than two agents" under local messaging; a stronger model ignores more attacks but executes them better once compromised.
- AgentWorm (arXiv 2603.15727, abstract only): a worm against 40,000+ OpenClaw instances, 63% aggregate success, and "the controls capable of stopping the infection loop were not enabled in any observed deployment."
- Incidents share one shape: a token or tool with broad reach, attacker-controlled text, and no check tying the action to the human's intent. ServiceNow's second-order injection is the closest to ours: a low-privileged user's text in a ticket led a reader agent to discover and delegate to a "Record Management" agent that copied a protected ticket under the *admin's* privileges, with injection protection on; agents were discoverable by default and ran with the initiating user's rights, and ServiceNow "confirmed the behaviors were intended". GitHub MCP: confirmation prompts failed because users pick "Always Allow".

**What this means for our network.** The signed roster and sign-in proofs already give us the part the standards leave open: a message is signed by a computer whose proof names a person. What nothing gives us is the *purpose*: who originally asked, for what, and whether the receiving side's owner has agreed to that class of request. OWASP's "signed intent (subject, audience, purpose, session)" is the missing object, and no standard defines it, so we would be defining it.

## 2. What companies want governed

**Ranked by what they fear and what they have seen** (different bases; read as order, not sums):

| Rank | Action class | Concern | Seen | Source |
|---|---|---|---|---|
| 1 | Access to / sharing of privileged data | 60% / 57% | 33% accessed, 31% shared, 32% downloaded | SailPoint, n=353, May 2025 |
| 2 | Going out of scope | 58% | 39% reached unauthorized systems; 80% any unintended action | SailPoint |
| 3 | External API call or transaction | | 53.9% of the 49.8% with an incident | Salt, Oct 2026 (no n on page) |
| 4 | Modify or delete data | | 41.9% of affected | Salt |
| 5 | Credential disclosure | | 23% | SailPoint |
| 6 | Ordering things / spend | | 16% (lowest named) | SailPoint |
| - | Ability to stop an agent at all | | 17.5% can automatically | Salt |

Not asked in any survey read: outbound messages to humans, creating or hiring agents, who approved. Developers: 48% trust AI when they can validate it; human-in-the-loop is "the dominant mode" (Stack Overflow 2026).

**What vendors let an admin do today** (B block, A human approval step, L log only; full table in [02](2026-11-01-org-auto-mode/02-governed-actions-demand.md)):

| Action | Claude Code | Codex | Copilot agent | Copilot Studio | Agentforce |
|---|---|---|---|---|---|
| Shell / file / network | B (managed deny, sandbox) | B (sandbox, proxy, auto-review) | sandbox, firewall on Bash only | n/a | n/a |
| MCP / connectors | B lock | ? | outside firewall | B connector blocks | per action |
| Merge / deploy / publish | deny rules | rules | **A** (cannot approve or merge its own PR) | B channels | - |
| Outbound message | via MCP deny | - | - | B channels, DLP | A end-user confirm [S] |
| Delete / modify data | deny patterns | auto-review | - | - | A end-user confirm [S] |
| Spend | org token limits | ? | - | ? | - |
| By intent or amount | - | - | - | - | - |

The pattern: the strongest controls are technical (what the process may touch) and the business-action controls are a confirm button for the end user, not a rule an organization can state. Gartner's Level 3 is exactly that confirm button, and Gartner says it degrades under approval fatigue; Anthropic measured the fatigue at 93% acceptance.

**Regulation** pulls logging and oversight for high-risk systems only (EU AI Act Art. 12 automatic event logs, Art. 14 oversight "commensurate" with autonomy; dates Dec 2027 / Aug 2028 per a mirror, unverified). NIST's agent standards initiative (Feb 2026) has an identity/authorization concept paper and no deliverables yet. Money today goes to identity and visibility: Oasis $120M, Noma $100M, Astrix to Cisco ~$400M [S], Agent 365 $15/user [S]; CISO-bought. **No evidence was found that anyone pays for intent-level autonomous approval.**

## 3. What "policy" means and how it works

**The shape every engine shares.** A decision is a function over (subject, delegation chain, action, resource/arguments, context) returning allow / deny / ask / modify, plus obligations the enforcement point must discharge (XACML: a PEP must deny if it cannot), plus a record. Four roles: the point that enforces, the point that decides, the sources of attributes, the place policy is authored.

**Two kinds of statement, two kinds of engine.**
- "Aden may instruct Friday", "the platform lead decides production freezes": relationships, transitive. Zanzibar / OpenFGA / SpiceDB model this natively (tuples plus userset rewrites; OpenFGA tuples can carry CEL conditions).
- "No production change until Friday", "no outbound email without review", "purchases under $500": attribute conditions. Cedar (principal, action, resource, context; `forbid` overrides `permit`; default deny; returns the determining policies) or OPA/Rego (any JSON in, any JSON out, decision logs with policy revision and input snapshot).
- agentgateway and OpenFGA both embed CEL for the attribute half, so a hybrid is the common pattern.

**Intent never is the language.** Three primary approaches, with numbers:

| Approach | How | Measured | Input it needs |
|---|---|---|---|
| Progent (arXiv 2504.11703) | LLM writes JSON per tool (args predicate, effect, priority, fallback message) from the tools plus the trusted user query; SMT solver labels each update as narrowing (automatic) or expansion (needs an approver) | AgentDojo attack success 39.9% -> 1.0%; ASB 70.3% -> 3.9% | the task statement and the tool schemas |
| AgentSpec (arXiv 2503.18666) | `rule / trigger / check / enforce` DSL; enforce = ask human, LLM self-examine, invoke action, or stop | 1-3 ms per check; LLM-written rules recall 70.96% (embodied) / 87.26% (risky code) | state predicates the rule can read |
| CaMeL (arXiv 2503.18813) | a privileged LLM writes code from the user query only; a quarantined LLM parses untrusted data; an interpreter tags every value with provenance and allowed readers and checks a policy before each tool call | 0 successful attacks in 949 runs (two tasks succeed in some runs); utility 31/40, 12/16, 14/20, 5/20 by suite | developer-written per-tool policies |
| Anthropic auto mode | a classifier judges each action against the transcript; "everything the agent chooses on its own is unauthorized until the user says otherwise" | 0.4% false positive, 17% miss on real overeager actions | the user's own words |

The lesson: compile intent into symbolic rules with a model, enforce with a deterministic engine, and keep the original sentence next to the rule because the compile step loses 13-29% of cases. Progent's rule for change is the right default: narrowing a standing rule is automatic, widening it needs a person.

**Precedent, today.** "Don't ask again" in Claude Code writes a bare rule string (`Bash(git commit *)`) to a local file with no author, reason or expiry; conflicts resolve only as deny beats ask beats allow. No source read records who approved, for whom, why, and until when. We would be defining that record.

**What an org-intent policy would need** (fields; each sourced in [03](2026-11-01-org-auto-mode/03-policy-how-it-works.md)): subject chain (user, agent, on behalf of, who granted the authority); action; resource or argument predicate (structured tool arguments, not shell text, which the Claude Code docs call fragile); condition (time window, prior calls, data labels); effect; combining rule (deny > ask > allow, unmatched = block); obligations (notify, redact); fallback message so the agent can re-plan; author; expiry; the source statement; policy revision plus input snapshot for replay; the narrowing-automatic / widening-approved rule; fail-closed when the evaluator is unreachable. Subject chain, author, expiry and source statement have no primary source: those are the parts nobody has standardized.

**Where it can be enforced.** On a laptop where the agent is the same OS user, the Claude Code docs are explicit: managed settings are the highest precedence and `permissions.deny` there is a floor no scope can override, the OS sandbox "hold[s] regardless of what command the model chose", and everything else (hooks, rules, prompts) is a request to the harness. The sandbox's own exclusion list covers built-in tools, hooks, MCP servers, env secrets and `~/.ssh`. A mod's `tool.check` can override a deny unless managed settings or an Enterprise login is the credential. Codex: Seatbelt on macOS, bwrap plus seccomp on Linux, network off by default, admin keys lock approval policy and sandbox mode, and a failed fetch of managed requirements errors "rather than silently starting without the cloud-managed requirements layer". So an org layer has three real enforcement points: the network (what passes between agents), the hosted runtime (our cloud computers, where we own the sandbox), and the vendors' managed-settings channels (which we can write to, not replace).

**Audit.** The OPA decision log is the best primary template: `decision_id`, `bundles.revision`, `input`, `result`, `requested_by`, `rule_labels`, `nd_builtin_cache` for replay, masking. OpenTelemetry's GenAI agent spans exist but are all "Development" status and have no attribute for a policy decision or approver. EU Art. 12 requires logging "the identity of the natural persons involved in the verification" for one class of system. Replay needs the policy version and attribute values at decision time; today's data gives a different answer.

## 4. What already exists

| Layer | Who | Governs | Agent-to-agent across people or vendors? | Laptops? | Status |
|---|---|---|---|---|---|
| Harness, single vendor | Claude Code managed settings + `autoMode.environment/allow/soft_deny/hard_deny` (prose rules; dev `allow` can override org `soft_deny`; `permissions.deny` is the floor) | tool access, classifier | No | Yes | shipped |
| | Codex `requirements.toml` (approval policies, sandbox modes, MCP servers, hooks, network; cloud outranks system file; fail-closed) | same | No | Yes | shipped |
| Gateway, one operator's domain | agentgateway (LF, Apache-2.0, 5.9k stars): LLM, MCP and A2A traffic, CEL policies on JWT claims, method, tool | agent-to-tool, in-domain A2A | Within the operator | if routed | OSS |
| | Google Agent Gateway: default-deny on agent identity plus registry, CEL, "semantic governance" plain-language rules per tool call | agent-to-anywhere egress for Google-hosted agents | A2A yes; "cross-org not addressed" | No | not labelled GA |
| | AWS AgentCore Policy: Cedar on principal/action/resource, natural language compiled to Cedar, enforce or log-only | agent-to-tool | No | No | GA 3 Mar 2026 |
| | Cloudflare MCP portals, Entra MCP firewall, Okta XAA, Auth0 | which identity reaches which server/tool | No | partly | shipped / preview |
| Identity | Aembit (user plus agent evaluated in one policy, both logged), Keycard [S], Entra Agent ID, Okta | who the agent and user are | one tenant | servers | shipped |
| Endpoint telemetry | Agent Beacon (MIT): captures 20+ runtimes incl. Claude Code, Codex, Cursor, OpenCode; never claims blocking | visibility | No | Yes | OSS |
| Endpoint enforcement, cross-vendor | Unbound AASB [S]: tool policies on MCP and terminal, approvals, MDM; pages returned 429 | claimed block | not stated | Yes (claimed) | announced Mar 2026 |
| Platform registries | Agent 365, Copilot Studio, MuleSoft Agent Fabric, ServiceNow AI Control Tower, Workday, IBM watsonx Orchestrate [S] | inventory, policy inside one org | hub-and-registry inside one org | No | shipped |
| Networks | A2A v1.0 signed Agent Cards (authenticity, not authority); NANDA registry [S] | transport | No authority model | - | - |

Consolidation: Prompt Security -> SentinelOne, Aim -> Cato, Lakera -> Check Point, Invariant -> Snyk, Acuvity -> Proofpoint, Astrix -> Cisco, Natoma -> Snowflake, Promptfoo -> OpenAI, Koi -> Palo Alto [S except Cato and Snyk]. The runtime-guardrail startups are being bought by security platforms; the identity ones are raising.

**The gap, stated carefully.** Every product governs one principal against one policy domain: a tenant, a cloud account, a gateway, or one vendor's harness. The closest things to "the org's intent decides" are Claude's prose `autoMode` rules and Google's semantic governance, both per-vendor and both rule-shaped with no place to put an org statement or an authority map. Nothing read decides whether this agent may do this for this person given what the company has said, across vendors. That is the same gap the standards have (section 1).

## 5. What Lightbulb has today

From [05](2026-11-01-org-auto-mode/05-lightbulb-inventory.md), read at `origin/main` of both repos.

**Enforced, and relevant to the thesis**
- Identity: Ed25519 computer keys, a signed hash-chained roster, sign-in proofs that name a person (`proof.rs:115`), `not_a_member` refusal at receive (`mail.rs:1060`), `revoked_keys`. This is the attested "which computer, which person" half that section 1 says the standards lack across domains.
- Person-only operations (`net_ops.rs:853` `person_only`, ~20 call sites) by process ancestry, with the same-uid ceiling stated in code.
- Network-worker policy: per-member Allow / Ask / Refuse, latest signed record wins, signed `approvals` rows (`workers.rs:43`, `store.rs:63`). This is the one place a per-person standing rule exists, and it is local to one computer.
- Permission holds with a signed record (`holds.rs:108`, `:347`); desktop approval that answers only with a hash of the exact tool input (`approval.rs`).
- Backend: Link purchase caps keyed on owner, project, merchant and amount, fail-closed (`purchases.py:191`); inference gate (`chat_gate.py:41`); `revoke-all` kill switch; hire approvals.

**Stored or documented, not enforced**
- `holds.rs` message kind is dead code; `docs/security.md` is right that "nothing is held" for messages.
- `hook.rs` has no PreToolUse; hooks observe status and deliver mail. The CLI cannot block a tool call today.
- `company_policy.py`: only `money_moves` is read (`purchases.py:276`); `outreach`, `publishing`, `provisioning` are read by nothing.
- Hermes workspace agents: `approvals.mode: "off"`, `unattended_mode: "approve"`, a 4-pattern command deny list (`hermes_profiles.py:161`). The docs page's "threat guard ... blocks commands, file writes, and URL fetches" and "before a governed shell command runs, the agent asks" are not backed. `request_approval` is a tool the model chooses to call.
- Teams approvals: any workspace member can approve (`teams/approvals.py:61-172`), no approver role, no self-approval block.
- Any workspace person can mint a join line; roster authority is flat; the room `AGENT_REPLY_CAP = 2` is the only hop cap in the backend (the CLI's is 20).
- No org audit: `admin_actions` is platform-staff only; CLI approvals never leave `state.db`.

So: Lightbulb can already say *who* sent a message and *which person* stands behind the computer. It cannot say for whom, for what purpose, or whether the owner agreed, and it cannot stop an action on either the laptop or the cloud computer beyond four shell patterns.

## 6. What this does to the thesis

1. **The product object is the signed request, not the approval.** OWASP's "signed intent (subject, audience, purpose, session)" plus Progent's compiled rule plus the OPA-style decision record is the thing no standard or product defines. Our network is the only place in the stack that already sees both ends of a cross-person, cross-vendor message, so it is the natural place to attach it.
2. **Two engines, one policy.** Authority is a relationship graph (Zanzibar-shaped); intent is attribute rules (Cedar/CEL-shaped). Build both; let an LLM compile statements into rules; enforce deterministically; keep the sentence, the author and the expiry on every rule; narrowing automatic, widening by a person.
3. **Three enforcement points, honestly labelled.** The network (we can hold or refuse any message; today we hold none). The hosted runtime (we own the sandbox; today approvals are off). The vendors' managed-settings channels (Claude Code managed `permissions.deny` and `autoMode.*`, Codex `requirements.toml`): we can *write* org rules into them, and should, rather than build a fourth in-process enforcer the docs already call bypassable. On a laptop we never claim more than the vendor's own boundary.
4. **The demand evidence points at data and scope first, spend last.** The top fears are privileged-data access and sharing and agents going out of scope, which is exactly the Friday case (a status summary that may hold investor material) and the ServiceNow case. Production-change freezes are the sharpest incident class (Replit, Kiro, Cursor/PocketOS) but surveys do not rank them. "Who approved" is not asked anywhere, which cuts both ways: no one is measuring it, and no one is asking for it.
5. **The buying evidence is against "governance" as the pitch.** Budgets go to identity and visibility, the runtime-guardrail startups are being acquired rather than growing, and only 17.5% of orgs can stop an agent at all. The sellable sentence is closer to "your agents can act for other people without anyone getting silence or a leak", measured by incidents prevented and requests answered, not "governance".
6. **Our own docs overclaim.** Before any enterprise conversation, the approvals page must match `hermes_profiles.py` and `docs/security.md` must mention worker policy. Those are small PRs and they are credibility.

## 7. What to test before building

- **Replay test (cheap, Codex subscription):** compile a set of org statements (freeze, data classes, who may ask whom) into rules with a model, then run the public incidents plus our Friday case through a deterministic check. Report recall and precision the way Anthropic reports 17%. If LLM-compiled rules land near the 71-87% of the papers, the human-readable source statement and the escalation path are load-bearing, not optional.
- **Five calls with platform-engineering or security leads** at companies running two or more coding agents: what did an agent do that it should not have, who found out, who decided what happened next, and could they have stopped it. Ask the two things no survey asked: outbound messages to people, and who approved.
- **Network hold prototype:** revive the message hold (`holds.rs` already has the table and the signed record) keyed on sender person, receiver owner's rule, and a purpose field; send the Friday request through it. This is one PR on code that exists.
- **Verify Unbound AASB** from a primary page (enforce or log, price), since it is the only claimed cross-vendor laptop enforcer.

## Not verified

Each strand's own list is at the end of its file. The ones that matter most here: Unbound AASB (429 on every page); the Gartner 40% headline (page text started at Level 1; levels are verbatim); Salt's sample size; Salesforce confirmation settings (developer docs via snippets; help page rendered blank); the 2026 injection papers (abstracts only); the A2A spec read through a summarizer; Paperclip's enforcement of budgets and execution stages (separate fork); whether any SAML/SCIM code exists (not searched). Reddit was not attempted.

## Round 2 (2 Nov 2026): harness auto modes, the labs' positions, the delegation literature

Three more strands, primary sources only, each with a "not verified" list: `06-harness-auto-modes.md` (29 vendor pages), `07-big-labs-positions.md` (29 lab pages), `08-delegation-and-intent-literature.md` (26 papers and drafts).

**What it confirms**
1. Per-call owner approval fails, measured. In Anthropic's test of 1,053 testers, people caught 13.6% of planted dangerous commands and auto mode caught 89%; people reject 3% of individual prompts but 39% of plans; block rates fell from about 17% to about 5% after 50 prompts (claude.com, Aug 2026). Green's survey of 41 oversight policies finds people "unable to perform the desired oversight functions". The Frontier Model Forum says click-through approval can "create the illusion of oversight". Review at the task or plan level, escalate on deviation.
2. The labs have already moved to a model judge on top of a hard floor: Claude Code auto mode (0.4% false positives on 10,000 real calls, 17% misses on 52 real overeager actions), Codex auto-review (99.1% of escalated actions approved, 90.3% recall, no false-positive rate published), Cursor auto-review. All three disclaim it as not a security boundary. The single-person, single-harness judge is commodity. We should not build a classifier.
3. Their judges read one person's chat. Claude Code: a message from another session "never counts as your consent" and "can't approve anything". Managed Agents: a message between session threads is not intent. So every cross-person request today routes back to the agent's own user, which is the Friday case exactly.
4. Org locks exist per vendor: Codex `requirements.toml` (cloud above system, MDM or HTTPS endpoint, admin-replaceable reviewer policy), Claude managed settings and `disableAutoMode`, Cursor team config over user and project, Gemini CLI's root-owned admin policy tier, Factory's `maxAutonomyLevel`. A company-controlled decision point is not novel for one vendor and one person. The cross-person, cross-vendor decision and its record are.
5. The gap is named by the labs and standards and closed by nobody. DeepMind's "Intelligent AI Delegation" (Feb 2026, research) wants attenuation per hop, signed attestation chains and "liability firebreaks" that re-ask the human principal, and says MCP and A2A lack them. Google's AP2 ships signed user "mandates" for payments only. Microsoft's red-team taxonomy v2 names "inter-agent trust escalation". NIST's Feb 2026 concept paper asks how an agent can "convey the intent of its actions" and how to handle "on behalf of". OWASP's 2026 Top 10 proposes an "intent capsule" and "Intent Gate". A May 2026 review of 21 production agent systems finds "zero production deployment" of intent anchoring.
6. No decision record says which rule or authority decided. Claude Code's OpenTelemetry `tool_decision` lumps managed policy, project settings, flags and the classifier under `source: config`. Managed Agents' per-event `evaluated_permission` plus `reason_code` is the nearest shape.

**What it changes**
7. The signed request must bind operands, not just purpose. PAuth: OAuth "inherently overprivileges agents" because it authorizes TRANSFER, not "transfer $100 to Bob"; 634/634 adversarial calls blocked when operands are explicit. AgentDojo: in 17% of cases the task's own tools suffice for the attack. Every signed-intent proposal (South et al., Agentic JWT, the Asor delegation chain, the WSO2 on-behalf-of draft) binds identity and scope, not operands. So the object is: authority chain, operation, operands, policy revision, and the decision point checks the actual call's operands against it. The desktop's hash-of-exact-tool-input (`approval.rs`) is the right instinct.
8. Deterministic enforcement on structured fields, LLM only to draft rules. LLM task-to-scope matching recall falls from 0.98 on one-tool tasks to 0.57-0.61 on three-tool tasks (Cisco ASTRA). Auto mode's 17% miss is the classifier floor.
9. Escalation to the resource's authority holder is measured nowhere. Kolt's agency-law framing supports it (the authority holder decides sub-delegation); no paper tests whether that person decides better than the requester. We would be first and must record catch rate and fatigue from day one, defined the way Anthropic and OpenAI define theirs.
10. Honest laptop channels exist for the record even where enforcement does not: Claude Code `tool_decision` events, Gemini CLI `tool_call` decisions, Cursor hooks and OpenTelemetry, Managed Agents' server verdicts. Write the decision record from those, enforce at the hosted point and the network.

**Recommendation after round 2**
- Product object: a signed request carrying the authority chain (who asked, through which agents, for whom), the operation and its operands, scope and expiry, and the policy revision it was decided under. Not free-text purpose alone.
- Decision: deterministic rules over that object plus a relationship graph for authority; an LLM compiles the company's sentences into rules and shows them back for correction; narrowing lands automatically, widening waits for a person.
- Human role: approve at task level, see escalations and the log, never per-call prompts. Escalate to the resource's authority holder; inform the agent's owner.
- Scope first: the cross-person request, because every shipped harness routes it back to the owner today and nothing in production anchors intent.
- Do not build: a classifier, a gateway, a policy language.
- Replay test must include operand-substitution attacks (AgentDojo style) and must report false positives, misses and prompt-acceptance the way Anthropic and OpenAI do, plus the cross-person cases nobody reports.

**Not verified in round 2 (full lists in each file):** OpenAI's "Running Codex safely" and the ChatGPT agent card HTML (403); DeepMind's AGI safety paper (summary only); Copilot's org-policy page, Devin's enterprise page, Warp's permissions page (404); CSA's IAM paper (gated); OWASP "Threats and Mitigations" (secondary); NIST AI 600-1 agent sections; Anthropic's 93% vs 97% acceptance gap is unexplained on its own pages; Anthropic's human study is the vendor testing its own product with testers who knew they were evaluated.

## Round 3 (2 Nov 2026): the horizontal search. What this is, who pays, what to build first

Three strands, primary sources only: `09-horizontal-analogues.md` (how organizations already encode "who may do what for whom" outside AI, 29 pages), `10-agent-authz-landscape.md` (who sells the agent version, wedge, traction, pricing, 33 pages), `11-agents-for-other-people-in-practice.md` (where agents already act for other people and what broke, 27 pages, 19 cases).

**What we are building, in words that already exist**
- The decision record is a *mandate*. EU payment law (RTS 2018/389 Art. 5) requires the user's approval to be "specific to the amount and the payee", and "any change to the amount or the payee results in the invalidation" of it. Google's AP2 copies that shape: the person signs an *open* mandate of constraints (payee, amount range, budget, expiry, bound to one agent's key); the agent signs the *closed* mandate for the concrete action; verifiers only ever see the closed one, and the receipt chain is "evidence as to what the user, and each role saw". AP2's authors say the model "could be applied more generally". That generalization, for actions other than payments, is our object.
- The policy is a *catalog of standard actions plus an authority map*. ServiceNow "standard changes" are pre-authorized and skip the board; emergency changes go straight to authorization and are reviewed after. Army doctrine (ADP 6-0) writes intent as purpose, key tasks and end state so subordinates act without asking and "report when able". DORA found "no evidence" that external change review lowers failure rates; peer review recorded in the platform satisfies segregation of duties.
- The exception path is *just-in-time access*: a required reason, a capped duration, no self-approval, N-of-M approvers (Teleport), plus a break-glass path that is allowed, alerted on every use and reviewed afterwards (Entra).
- The buyer exists because of audit. SOX 404 makes "who approved this" a question with a budget. SailPoint sells segregation-of-duties *exceptions* with an expiry. FINRA (2026): "Agents may act beyond the user's actual or intended scope and authority"; firms should track agent actions.

**Where it is already felt (round 3, strand 11)**
- Agents acting for other people are common now, not future. Shared agents in work tools run with the agent's or maker's permissions (Notion: "the agent's own access, not the permissions of the person who triggered it"; Copilot Studio: an end user "might perform actions that only the maker's account is permitted to do") or with the invoker's permissions, which is how ServiceNow was exploited. Both sides of the binary break. Microsoft's fix for oversharing makes scheduled and autonomous agents "fail".
- Customers ask for the intersection and are refused. Atlassian ticket AUTO-2337, "use intersecting permissions with invoking user", answered "intended behaviour".
- Coding fleets solved it with a second person and a sandbox: GitHub blocks the requester from approving the agent's PR; Stripe asks "another Stripe engineer"; Ramp passes authorship with every prompt but has no rule on what each person may make the agent do. Do not lead with coding agents; plug into their review as an adapter.
- In 19 cases, no incident fix added an approver other than the requester. Fixes narrow reach. Microsoft's agent sponsor is the only shipped escalation to someone else, for access grants, not operations.
- The "owner approves everything" model is popular and breaks with more than one person: Paperclip (110k stars) sends every approval to one board; OpenClaw's advisories are about who is speaking ("inherit owner tool authority").

**Who sells what (strand 10)**
- Money: developer action-layer auth (Arcade $60M, Keycard $38M) and enterprise agent identity and security (Oasis $120M, Zenity $125M, Noma $100M). Acquirers buy the access-decision layer (SGNL to CrowdStrike about $740M, Natoma to Snowflake). Standalone approvals-as-a-service did not become a company (HumanLayer pivoted).
- Standard now: "the agent can do only what its user can do", inside one tenant. Keycard's multi-hop delegation (RFC 8693, narrowing per hop) is the closest, within one IdP. Operand binding is almost absent; "intent" is marketing or detection. Okta for AI Agents (GA Oct 2026) is the incumbent to watch: registration, agent-to-agent rules, kill switch, logs; no operand binding or resource-holder escalation on the page.
- Nobody's page: enforced and cross-person; enforced and operand-bound; cross-organization; an approver who is a specific other person.

**What to build first, reconciled across the three strands**
1. The product object: a signed decision record per operation, AP2-shaped. Who asked, the agent chain with an origin that cannot change, on whose behalf, the operation and its operands, which rule or person decided, receipt hash. Reuse AP2's open and closed mandate shape and UCAN-style argument caveats; invent no format.
2. The policy: a catalog of pre-approved actions per resource plus an authority map from resource to holder. Decision = requester's permission intersected with the agent's grant intersected with the resource's rule. Inside the catalog, allow and record. Outside, escalate to the holder: reason required, expiry, no self-approval, sponsor-to-manager fallback. Break-glass allowed, alerted, reviewed.
3. The first surface: shared agents that act for whoever triggers them, over shared business tools. We own one: the hosted workspace agents (Friday) on the network. The Friday-to-session request is the demo. Adapters for Slack, Notion, Copilot Studio and Rovo come next; coding-agent review is an adapter, not the wedge.
4. The first operand-bound action: agent spend. We already have Link purchases with fail-closed caps; the mandate makes the cap specific to amount and payee, the way payment law already requires.
5. The buyer: the platform or security team that owns shared agents, with the controller or head of IT as the audit sponsor. The trigger: an auditor or an incident asking "who approved this agent's action". Price per governed agent or monthly active user, the way Okta and Teleport price; plumbing free, decisions paid.
6. Do not build: a classifier, a gateway, a policy language, identity inventory, an MCP gateway, a standalone approvals product, or an owner-approves-everything queue.
7. Validation without usage data: 15-20 calls with teams running shared or triggered agents (Copilot Studio, Notion, Rovo, Slack bots, internal assistants), not coding-agent teams; a desk count of public asks like AUTO-2337 across vendor trackers; the replay test with operand-substitution attacks.

**Precedent added 2 Nov 2026 (relayed by the orchestrator; primary doc read):** DeepSeek's `dsh-superagent-prototype/docs/002-authorization.md` ships host-enforced Mandates within one host (explicit vs inferred admission, observe mode, context-only subscriptions whose delivery "grants no new authority", revalidation before resume, a policy version whose change re-queues triggers). It does not address delegation across people, organizations or agent boundaries and does not sign decisions. The round-3 claim is therefore narrowed: no shipped system carries a signed, operand-bound authority object across people and vendors with escalation to the resource holder; single-host mandates exist.

**Not verified in round 3 (full lists in each file):** finance approval vendors beyond Ramp; SailPoint and PAM pricing; Mastercard Agent Pay and Visa field details (403 or absent); Slack's permission model when user B triggers user A's agent; Meta's March 2026 internal incident (secondary); Arcade's round (Business Wire 403); Zenity and Straiker blocking claims (vendor statements); Paperclip's stop path end to end; Shopify, Uber, Airbnb, Coinbase, Klarna, Intercom not researched (fetch budget).
