# What "policy" means for AI agents and how policy engines work

Researched 2026-11-01. Primary sources (docs, specs, arXiv abstracts, READMEs). `[secondary]` marks write-ups. Facts below come from the fetched pages; anything I did not see is listed under "Not verified".

## Summary

1. A policy decision is a function: (subject, delegation chain, action, resource, context) -> decision, plus a record. Every mature system separates these jobs (XACML names them PEP/PDP/PIP/PAP).
2. Subject and delegation chain: who is asking, and on whose behalf. Identity systems own this (Entra OBO token in Agent 365, JWT claims in agentgateway, Okta groups). No agent-policy system I read has a first-class "agent acting for user U, who got this from manager M" chain. They carry the user and the agent id side by side.
3. Action and resource: tool name plus arguments. Claude Code, Progent, Invariant, OpenAI tool guardrails and Agent 365 key on these. Argument matching is where all the fragility is (see section 5).
4. Context: time, session, prior calls in the trace. Only trace-aware engines (Invariant: `call1 -> call2`; CaMeL: data-flow capabilities) can say "no send after reading inbox". Static allowlists cannot.
5. Decision: allow / deny / ask (escalate) / modify. Claude Code hooks have all four (`allow|deny|ask|defer` plus `updatedInput`). OpenAI tool guardrails: allow / reject_content / tripwire. Agent 365: allow / block only.
6. Record: OPA decision logs, Cedar "determining policies", OTel GenAI spans (all still "Development" status), EU AI Act Art. 12 (automatic event logs). No standard defines a decision record for an agent policy verdict.
7. Intent ("code freeze until Friday") is not a policy language anywhere. The primary sources split into: (a) models judge actions against intent at runtime (Anthropic classifier, Defender), (b) an LLM compiles the task into symbolic rules that a deterministic engine enforces (Progent, AgentSpec). (b) is the only one with measured guarantees, and it is lossy (AgentSpec LLM-made rules: 70.96% recall on embodied tasks).
8. Authority map ("who can decide what") is a relationship problem: Zanzibar/OpenFGA/SpiceDB model fits it. Intent/rules fit Cedar/OPA/CEL. A real layer likely needs both.
9. On a laptop where the agent is the same OS user, only OS-level enforcement (Seatbelt/bubblewrap) and root-owned managed settings are hard. Everything the harness does by prompt or rule is bypassable by other processes the agent can start (section 5).
10. Precedent learning exists in the weakest form: "don't ask again" writes an allow rule to a local file. Nobody I read resolves conflicts beyond "deny beats ask beats allow".

## 1. Authorization policy languages and engines

| Engine | Model | Decision | Audit | Inputs needed |
|---|---|---|---|---|
| OPA / Rego | Rules over JSON: `input` (the request) and `data` (stored docs). Rules are virtual documents; same-name rules OR together; `default` supplies fallback. Output can be any JSON value, so a rule can return `{allow, reason, obligations}` | Evaluation is declarative query; undefined body = undefined document. Complete rules with two values conflict -> error | `rule_labels` appear in decision-log events; decision log format page not fetched | Whatever the caller puts in `input`. You design the schema |
| Cedar (AWS) | Rules with fixed shape: principal, action, resource, context; effect `permit` or `forbid`, optional `when`/`unless` | Allow or Deny, response lists "determining policies" | Determining policies in every response is built in | Entities (with attributes/parents), context, policies. Optional schema validates policies against entity types |
| Zanzibar | Relationships: tuples (object#relation@user), userset rewrites, namespaces config | Check = is there a path of relations | zookies give causally consistent reads | The tuple store. Scale claims: trillions of ACLs, millions of checks/s, p95 under 10 ms, >99.999% availability over 3 years |
| OpenFGA (Zanzibar-style) | Authorization model (types, relations) + tuples; tuples can carry CEL conditions (ABAC) | `Check` returns `allowed: true/false`; `ListObjects`, `ListUsers` | Not covered in page | Tuples; contextual tuples can be passed per request without being stored |
| XACML 3.0 | Attributes + rule/policy/policy-set trees | Permit / Deny / NotApplicable / Indeterminate; combining algorithms (deny-overrides, permit-overrides, first-applicable, only-one-applicable). PEP must deny on anything but Permit | Not specified in what I read | Attributes via PIP through a context handler |

XACML roles (the reference architecture everything else reimplements): PEP enforces and asks; PDP decides; PIP supplies attributes; PAP authors policy. Obligations are actions the PEP must perform with the decision (a v3 PEP must deny if it cannot discharge them); advice may be ignored. This maps directly to "allow, but notify Aden" (obligation) vs "FYI" (advice).

Cedar evaluation (docs.cedarpolicy.com/auth/authorization): default deny ("no request is authorized ... unless there is a specific permit policy"); any satisfied `forbid` overrides every permit; a policy that errors is skipped (deliberately, to avoid sudden deny-all); determining policies are returned (empty list on default deny). Verified analyzability claims: only a design assertion that evaluation steps cannot error; no formal-proof statement on that page, so the SMT-analysis claims remain unverified.

Which fits "who may ask which agent for what, on behalf of whom"?
- "Who may ask which agent" = a relationship (Aden `can_instruct` Friday; manager `can_decide` code-freeze). Zanzibar/OpenFGA/SpiceDB-shaped. Delegation is transitive userset rewriting, which they do natively.
- "For what, on behalf of whom" = attribute conditions on the action (tool, args, time, freeze window). Cedar/OPA/CEL-shaped.
- XACML's delegation is a separate OASIS profile I did not fetch.
- agentgateway and OpenFGA both already embed CEL for the attribute half, which suggests the hybrid is the common pattern.

## 2. Policy for LLM agents

**NVIDIA NeMo Guardrails** (docs.nvidia.com rail-types page). Five rail types: input (before the LLM), retrieval (RAG chunks), dialog (conversation flow), execution (around tool/function calls: validates calls, arguments, results), output (after the LLM; can filter, edit, block). Colang itself was not described on the page I reached.

**Invariant Labs Guardrails** (github.com/invariantlabs-ai/invariant README). Rule format: `raise "<message>" if:` followed by conditions over trace bindings, e.g. `(call: ToolCall)`, `call is tool:get_inbox`, and `->` for "followed by". README examples: inbox read followed by email to non-company domain; banned-phrase scan; `get_website` output scoring >0.7 on a `prompt_injection` detector followed by `send_email`. Enforced by a gateway between app and LLM/MCP servers, before and after each call; also `LocalPolicy` in code. This is the only trace-aware rule language I found with a simple surface syntax. It only raises (deny); I did not see allow/modify.

**Microsoft Agent 365 via LiteLLM guardrail** (docs.litellm.ai microsoft_agent_365). Before an MCP tool call LiteLLM sends: agent id (LiteLLM key alias), tool name (+ description, schema when known), arguments, MCP server name, conversationId. Runs as the signed-in user via Entra OBO token. The user's prompt is never sent. Defender returns allow or block; block means the MCP server is never contacted (HTTP 400 on one path, 200 + `isError` on the `/mcp` transport). Evaluated-or-skipped: `unreachable_fallback` default `fail_closed` (503). Who writes the policy: "Microsoft Threat Detection policies configured by your administrator"; authoring not explained. Azure Prompt Shields: not fetched.

**Okta.** I found no official Okta "Agent Gateway" doc. What I found: Cross App Access (XAA): the enterprise IdP checks an admin-defined XAA policy, then issues an ID-JAG assertion the requesting app exchanges for an access token (auth0.com/docs/xaa-resource-app; Okta press release). It controls which apps/agents connect and scopes, not per-tool-call args. Okta as authorization server behind agentgateway is group-based (`"AI-Users" in jwt.groups`). Scalekit's blog [secondary] says Okta answers "can user access system", not "which action may agent take".

**Cloudflare MCP Server Portals** (developers.cloudflare.com). Access policies on the portal (who connects) and on each server (whether it shows in the portal for that user, "only appear ... for users who match an Allow policy"). Tool level: toggle tools off, or allowlist (`default_disabled: true` + `updated_tools`). Selectors (emails, groups, country, device posture) are enforced; MFA/purpose-justification are not for portal-authorized servers. Bypass: blocked users can still reach a server by direct URL unless Access is the server's OAuth provider. Logs: time, status, server, capability, duration; Logpush on Enterprise. No argument-level policy.

**agentgateway** (agentgateway.dev mcp-authz). `mcpAuthorization` rules are CEL expressions evaluated per MCP method (`list_tools`, `call_tools`). Available: `mcp.tool.name`, `mcp.tool.target`, `mcp.prompt.name`, `mcp.resource.name`, `mcp.methodName`, `mcp.sessionId`, JWT claims. `mcp.tool.arguments` is not set in these rules; arguments need a route-level `authorization` policy. Denied tools are filtered from `list` responses (agent never sees them). Page does not describe a deny effect or rule combining.

**Claude Code permissions** (code.claude.com/docs/en/permissions, hooks). Grammar: `Tool` or `Tool(specifier)`. Bash: `Bash(npm run build)` exact; `Bash(npm run *)`, `Bash(git log * main)`, `Bash(ls *)` (space matters: `ls*` also matches `lsof`); `:*` suffix is equivalent to trailing ` *`. Other: `WebFetch(domain:example.com)`, `mcp__server__tool`, `mcp__server__*`, `Agent(isolation:*)` (arg `key:value` match), `Read(*.env)` with `Read(!sample.env)` carve-out (same source only). Evaluation: deny, then ask, then allow; first match wins; specificity does not matter; "An allow rule can't carve an exception out of a deny rule". Deny at any scope beats allow at any scope. A bare `Bash` deny removes the tool from context. Compound commands are split on `&& || ; | |& &` and newlines; every subcommand must match an allow; deny/ask match if any subcommand matches. Wrappers stripped: `timeout time nice nohup stdbuf command`, bare `xargs`; `npx`, `docker exec`, `direnv exec` are not stripped.
Docs state their own limit: "Bash permission patterns that try to constrain command arguments are fragile" (`Bash(curl http://github.com/ *)` misses variations); `Bash(rm *)` does not match `/bin/rm` or `bash -c 'rm ...'`; `Bash(git push *)` does not match `git -C . push`.
Hooks (PreToolUse): input has `tool_name`, `tool_input`, `tool_use_id`, `permission_mode`, `session_id`, `transcript_path`, `cwd`. Output JSON `hookSpecificOutput.permissionDecision` in `allow|deny|ask|defer`, `permissionDecisionReason`, optional `updatedInput` (replaces args = "modify"). Exit 2 blocks (stderr = reason) even if JSON says allow; exit 0 with no output = no opinion; exit 1 is non-blocking (a footgun for enforcement); a timed-out hook does not block unless `onFailure: "block"` (v2.1.295+). Hooks cannot override deny/ask rules: "a matching deny rule blocks the call, and a matching ask rule still prompts even when the hook returned allow". The `if` filter on hooks is "best-effort".

**OpenAI Agents SDK guardrails** (openai.github.io/openai-agents-python/guardrails). Input guardrails: first agent only; run in parallel with the agent by default (agent may have burned tokens or run tools before the tripwire). Output guardrails: last agent only; tripwire rejects the final output. Tool guardrails wrap `FunctionTool` calls: input guardrail can skip the call, replace output with a message (`reject_content`), or tripwire; output guardrail can replace output or tripwire. Not covered: handoffs, hosted tools (WebSearch, FileSearch), built-in ComputerTool and ShellTool. Guardrail exceptions expose `guardrail_result` / `run_data`.

### The table

| System | Keyed on | Decisions | Enforced at |
|---|---|---|---|
| Claude Code permission rules | tool, args (glob), path, domain, MCP server/tool; settings scope | allow / ask / deny | harness (in-process) |
| Claude Code PreToolUse hook | everything in tool_input, session, cwd, mode; arbitrary code | allow / ask / deny / defer / modify (`updatedInput`) | harness; cannot beat deny/ask rules |
| Claude Code sandbox | filesystem paths, hostnames | allow / deny (OS errors) | OS (Seatbelt, bubblewrap) + proxy |
| Anthropic auto mode (known) | transcript, intent | allow / block / escalate | harness classifier |
| LangChain HITL (known) | tool name | approve / edit / reject / respond | SDK middleware |
| OpenAI Agents SDK | agent boundary, function tool + args, output content | allow / reject_content / tripwire (raise) | SDK; not shell/computer/hosted tools |
| NeMo Guardrails | message, retrieved chunk, tool call+args+result, dialog flow | block / edit / steer | SDK/server around LLM |
| Invariant Guardrails | whole trace: tool order, args, content detectors (injection, PII) | raise (deny) | gateway proxy for LLM + MCP, or in code |
| Agent 365 via LiteLLM | user (OBO), agent id, MCP server, tool, args, conversation id | allow / block, fail-closed fallback | gateway |
| agentgateway | JWT claims, MCP method, tool name/target; args only in route policy | allow (deny not described); list filtering | gateway |
| Cloudflare MCP portals | identity, group, device posture, country; server; tool toggle | allow / hide | edge portal; direct URL can bypass |
| Okta XAA | user/app/scope per IdP policy | issue / refuse token | IdP |
| Progent | tool name + args (symbolic, SMT-checked) | allow / block; expansion needs approval | agent runtime wrapper |
| AgentSpec | trigger + predicate over agent state | enforcement actions (rules chosen by author) | runtime monitor |
| CaMeL | data-flow capabilities of values, tool-call policies | allow / block at tool call | interpreter around the LLM |

Pattern: nothing above keys on delegation chain plus intent plus context together. Gateways see identity, tool and (sometimes) args. Harnesses see the transcript. Only the model-based approaches see intent.

## 3. Intent-based policy

- **Instruction hierarchy (arXiv 2404.13208).** Claims system prompts get the same priority as untrusted text and trains GPT-3.5 to ignore lower-privileged instructions. Abstract says "drastically increases robustness", "minimal degradations"; no numbers on the abstract page. It is a model-training result, not a policy mechanism: the "policy" is the role ordering (system > user > tool output). Input needed: correctly labeled message roles.
- **Progent (2504.11703, HTML).** Policy = JSON per tool, verbatim from Appendix C Fig. 9 (abbreviated by the paper):
  ```
  {"send_email": [{"priority": 100, "effect": "allow",
     "conditions": {"recipients": {"type": "array",
        "items": {"type": "string", "enum": ["alice@example.com"]}}},
     "fallback": "return msg", "fallback_msg": "tool blocked, ..."}], ...}
  ```
  Args predicates are JSON-schema-like (concrete values, `*`, regex). Forbid rules are checked before allow; no matching rule = blocked. Generation: an LLM writes the initial policy from the tools plus the user query (trusted). Updates are two steps: one LLM decides whether an update is needed seeing the call but not the tool result; a second proposes it with full context. An SMT solver labels it narrowing (auto) or expansion (approver configured as deny, approve, or human). Numbers: AgentDojo attack success 39.9% -> 1.0% (2.5% with initial policy only); ASB 70.3% -> 3.9%; utility "maintained" (exact values only in figures I could not read). AgentPoison not seen.
- **AgentSpec (2503.18666, HTML).** Verbatim (Fig. 7): `rule @stop_pouring_damage / trigger pour / check !is_into_wettable_object / enforce stop / end`. Structure: trigger = the action about to run; check = predicate; enforce = `user_inspection` (ask human), `llm_self_examine`, `invoke_action(...)`, or `stop`. Overhead: parsing ~1.42 ms, predicate ~2.83 ms (code) / 1.11 ms (embodied), vs 25.4 s / 9.82 s average run. The 95.56% / 70.96% figures are for LLM-generated embodied rules (body text calls 95.56% the share of risky cases enforced; abstract calls it precision).
- **CaMeL (2503.18813, HTML).** Control flow: a Privileged LLM sees only the user query and writes Python; a Quarantined LLM (no tools) parses untrusted data into a schema; an interpreter runs the code, tracks a dependency graph per variable, and checks policies before each tool call. Capabilities = tags on values: provenance (user, CaMeL, tool) and allowed readers (Public or a user set). Policies are Python functions (tool, args) -> Allowed/Denied with reason; the paper describes the calendar policy in prose only (participants may read event details; sharing with trusted-source participants allowed), no code listing. AgentDojo utility with CaMeL (Claude 3.5 Sonnet): workspace 31/40, banking 12/16, slack 14/20, travel 5/20 (travel failures blamed on under-documented APIs). 0 successful attacks over 949 in Sec 6.3, though Sec 6.2 says two tasks (banking, travel) succeed in some runs. Policies are developer-written per tool, not derived from user intent.
- **Anthropic classifier (known).** Judges each action against the transcript; the user's words are the only authorization.
- **Defender (Agent 365).** Server-side threat detection on tool + args; the prompt is deliberately not sent, so it cannot judge intent at all.
- **IBM / "policy as natural language":** not reached.

What this means for the org layer: every approach that is robust converts intent into symbolic form (tool/arg rules, data-flow labels) before enforcement. The measured loss is in the conversion (recall 71% to 87% for LLM-made rules), not in the enforcement. The conversion input is a structured statement of what is in scope (task, tools, time window), not free text alone.

## 4. Precedent: turning approvals into standing rules

- Claude Code: "Yes, and don't ask again" for Bash and WebFetch is saved permanently "per repository and command" to `.claude/settings.local.json` at the git repo root (resolved through worktrees to the main checkout); file edits last only until session end. The dialog writes the space-separated prefix form (`Bash(git commit *)`). State kept: an allow-rule string in a local file, no author, no timestamp, no reason, no expiry. Conflicts: deny > ask > allow across all scopes, first match, specificity irrelevant. Project `allow` rules are only applied after the workspace trust dialog; a tracked `settings.local.json` is held like project settings until trusted. Managed `allowManagedPermissionRulesOnly` stops persisted user rules from counting.
- Other products (from search results, [secondary], vendor docs): Gumloop "Don't ask again for this tool"; Strands "trust" = remember for the session; Kickscale approval lasting the current conversation; one framework with three modes (first use only, every call, or an eval model decides). No source here shows conflict resolution or expiry.
- Progent is the only primary source with a principled rule: narrowing automatic, expansion needs approval.
- LangSmith approval memory, Devin playbooks, Gartner levels: my fetch of a LangSmith URL 404'd and search returned nothing on Devin; not verified.

Gap that matters: no source records who approved, for whom, why, and until when. Lightbulb would be inventing that schema.

## 5. Enforcement points on a developer laptop

What Claude Code docs state:
- Precedence: managed settings highest; "no other level, including command line arguments, can override a managed permission rule". Deny anywhere beats allow anywhere. Keys like `disableBypassPermissionsMode` / `disableAutoMode` can be set in any scope. `allowManagedPermissionRulesOnly` makes managed the only source of rules. Some "security-sensitive keys" are exceptions to managed precedence (page linked, not read).
- Delivery: server-managed, MDM/OS-level (macOS preference domain `com.anthropic.claudecode`, Jamf/Kandji), file `/Library/Application Support/ClaudeCode/managed-settings.json` (+ `managed-settings.d/`). Invalid entries are stripped with a warning; the rest still applies (v2.1.169+) [from search summary of docs].
- Mods exception: an installed mod's `tool.check` handler "can approve a call that a deny rule refuses or a PreToolUse hook blocked" unless managed settings or a Team/Enterprise login is the credential. So the hard guarantee depends on how the session is authenticated.
- Sandbox (Seatbelt on macOS, bubblewrap on Linux): "The OS applies these limits to the running process, so they hold regardless of what command the model chose." Writes confined to working dir/temp; `.claude`, `.mcp.json`, shell rc, `.gitconfig`, `.git/hooks` protected. Network only via a proxy checking hostnames against `allowedDomains` (empty by default).
- What the sandbox does not cover (doc's own list): built-in Read/Edit/Write/WebFetch/WebSearch tools; hooks, local MCP servers, plugin monitors, LSP, status line, `apiKeyHelper` run with full access; env vars inherited (secrets visible); `~/.ssh`, `~/.aws/credentials` readable unless denied; `!` prompt commands usually unsandboxed; reads allowed machine-wide by default.
- Escape hatches: `dangerouslyDisableSandbox` retry (approval depends on mode; a matching allow rule such as `Bash(curl *)` approves it silently; disable with `allowUnsandboxedCommands: false`); `excludedCommands` (textual match; an interpreter in the list lets the agent write a file and run it outside); `filesystem.disabled`; weakening flags; custom proxy ports. Filtering is hostname-only with no TLS inspection. `allowManagedDomainsOnly` does not limit WebFetch.
- A policy inside the agent process (hooks, rules) is a request to the harness. The only things the agent's OS user cannot rewrite are root-owned files, MDM profiles, and OS sandbox profiles, and only if the agent cannot start an unsandboxed child.

Codex (learn.chatgpt.com/docs/agent-approvals-security, former developers.openai.com): approval policies `on-request`, `never`, `granular` (chosen prompt categories interactive, rest auto-rejected), and `approvals_reviewer = "auto_review"` (a reviewer agent that denies critical-risk actions and fails closed); `--sandbox danger-full-access` removes sandbox and approvals. Sandbox: macOS Seatbelt via `sandbox-exec`; Linux `bwrap` + seccomp; Windows MXC (elevated/unelevated legacy); WSL2 = Linux. Network off by default; `network_access = true` in `workspace-write`, then `network_proxy` domain rules. `.git`, `.agents`, `.codex`, `.aws` stay read-only recursively. Admin locks: precedence "MDM and legacy managed-device requirements ... Agent Security ... device's system requirements file"; managed `allowed_approval_policies`, `allowed_approvals_reviewers`, `guardian_policy_config`, `allowed_web_search_modes`, `experimental_network`, approved `mcp_servers`. The `requirements.toml` file itself is not documented on that page; Codex cloud network policy not read.
Cloudflare precedent: even edge policy leaks via direct server URL unless the server itself trusts the same IdP; the same shape as laptop bypass.

## 6. Audit and replay

- **EU AI Act Art. 12** (artificialintelligenceact.eu): high-risk systems "shall technically allow for the automatic recording of events (logs) over the lifetime of the system". Logs must support traceability for (a) identifying risk situations or substantial modification, (b) post-market monitoring (Art. 72), (c) monitoring operation (Art. 26(5)). Minimum content is specified only for Annex III point 1(a) systems (start/end time of each use, reference database, matching input data, identity of persons who verified results under Art. 14(5)). Applies 2 Dec 2027 (Annex III) / 2 Aug 2028 (Annex I). Retention (Art. 19/26) not read. Note: the human who verified is a required log field there.
- **NIST AI RMF:** the playbook landing page says nothing on logging; I did not reach AI 100-1 or the playbook subcategories. Not verified.
- **OpenTelemetry GenAI semantic conventions** (semantic-conventions-genai repo): spans `create_agent`, `invoke_agent`, `invoke_workflow`, `plan`, and tool execution `execute_tool {gen_ai.tool.name}`. Attributes: `gen_ai.agent.id/name/version`, `gen_ai.conversation.id`, `gen_ai.tool.call.id`, opt-in `gen_ai.tool.call.arguments` and `.result`, opt-in input/output messages and system instructions. All marked Development (unstable); only `error.type`, `server.*` are Stable. No attribute for policy decision, policy id, or approver. Teams would add custom ones.
- **OPA decision log event** (openpolicyagent.org/docs/management-decision-logs): `decision_id`, `trace_id`, `span_id`, `bundles` (policy bundle + `revision`), `path`, `query`, `input`, `result`, `requested_by`, `timestamp`, `metrics`, `labels` (instance), `rule_labels`, `ids`, `req_id`, `nd_builtin_cache` (non-deterministic builtin outputs "for debugging and replay"), and `erased`/`masked` JSON pointers. Masking via `data.system.log.mask`; drop rules via `system.log.drop`. This is the best primary template for a decision record: it already pins the policy revision, the exact input and the result. Cedar returns determining policies per response.
- Synthesis, from what the engines return and the standards ask for. A per-decision record needs: request id and trace/span id; subject (user), agent id, delegation chain; action (tool + args or hash); resource; context snapshot (policy version, freeze window active, session id); decision plus which policy/rule decided; obligations discharged; escalation target and who resolved it and when; outcome. Replay requires the policy version and the attribute values at decision time (PIP inputs), because re-running against today's data gives a different answer. This is my inference, not a standard.

## What an org-intent policy would need, from the evidence

Minimal structured form an intent must be converted into. Each field is supported by a source; fields with no source are marked as inference.

| Field | Why | Source |
|---|---|---|
| subject chain (user, agent, on-behalf-of, authority granter) | gateways carry user and agent separately; no source models the chain itself | Agent 365 (user via OBO + agent id); agentgateway (JWT claims); chain itself: inference, Zanzibar usersets for transitivity |
| action (tool name, operation) | every system keys on it | Claude Code rules, Progent, Invariant, OPA/Cedar action |
| resource / args predicate | Progent's per-argument JSON predicate; agentgateway keeps args out of its MCP rules; Claude Code docs call Bash arg patterns fragile, so prefer structured tool args over shell text | Progent Fig. 9; agentgateway; Claude Code permissions |
| condition (time window, prior calls, data labels) | "code freeze until Friday" is a time condition; "no send after reading inbox" needs trace or data-flow state | Cedar `when/unless` + context; Invariant `->`; CaMeL capabilities |
| effect: allow / deny / ask (escalate) / modify | the four outcomes in Claude Code hooks; AgentSpec adds `user_inspection` | Claude Code hooks; AgentSpec |
| combining rule | deny beats ask beats allow; unmatched = block | Claude Code (deny > ask > allow); Cedar (forbid overrides, default deny); Progent (forbid first, default block) |
| obligations (notify, redact) | PEP must discharge or deny | XACML obligations vs advice |
| priority / fallback message | tells the agent why it was blocked so it can re-plan | Progent (`priority`, `fallback_msg`); Claude Code (`permissionDecisionReason`) |
| author / approver | no source records it; Art. 12 requires logging who verified results for one class of system | Art. 12 para 3 (partial); otherwise inference |
| expiry / scope (session, repo, until date) | session-vs-permanent approvals exist but no dated expiry in any source | Claude Code (session vs permanent per repo); Strands/Kickscale [secondary]; dated expiry is inference |
| source statement (the original sentence, who said it, when) | needed to explain and to re-derive when the compile step is lossy (LLM-made rules recall 71 to 87%) | AgentSpec numbers; Progent (query is the trusted input); statement field itself: inference |
| policy revision id + input snapshot | replay | OPA decision log (`bundles.revision`, `input`, `nd_builtin_cache`) |
| expansion rule: narrowing auto, widening needs approval | keeps standing rules from growing silently | Progent monotonic confinement |
| fail mode | block when the evaluator is unreachable | Agent 365 `fail_closed`; Claude Code `onFailure: "block"` |

Evidence-based cautions: compile intent with an LLM but have a deterministic engine enforce (Progent 39.9% -> 1.0%, AgentSpec ms overhead); keep a human-readable source statement because the compile step loses recall; enforce at a layer the agent cannot rewrite (OS sandbox or root-owned managed config), since in-process rules and hooks are advisory against a same-user agent.

## Not verified / couldn't reach

- Cedar SMT/formal-analysis claims (authorization page only asserts a design property); SpiceDB docs; Zanzibar full paper (only abstract page).
- XACML delegation profile.
- NeMo Colang syntax; Azure Prompt Shields docs; an official Okta "Agent Gateway" product (not found, may not exist under that name).
- Codex sandbox/approvals pages and Codex cloud network policy; Claude Code `settings.md#exceptions-to-managed-settings-precedence`; the last ~180k characters of the hooks page (incl. exact `updatedInput` section).
- Progent utility numbers and CaMeL per-suite attacked numbers (figures only); instruction-hierarchy numbers; IBM natural-language policy work.
- NIST AI RMF logging subcategories; EU AI Act Art. 19/26 retention.
- LangSmith approval memory (404), Devin playbooks, Gartner levels.
- Reddit skipped by rule; Chrome fallback not used.
