# How coding-agent harnesses decide what runs without asking (as of 2 Nov 2026)

Tags: [P] = vendor page fetched this session; [S] = search summary or third party. Every claim below is [P] unless tagged. Fetch tool returns a summarised extraction of each page, so quotes are what the extractor returned as verbatim; pages over 100k chars were read only in the first 100k.

## 1. Summary findings

1. Claude Code auto mode is a two-stage model classifier (fast single-token filter, then chain-of-thought only when flagged) that sees user messages and tool calls but not assistant prose or tool results. Source: anthropic.com/engineering/claude-code-auto-mode [P].
2. Anthropic publishes its own numbers: 0.4% false positives on real traffic (8.5% stage 1 alone), 17% false negatives on real overeager actions ("the honest number"), 5.7% on synthetic exfiltration, and users accepted "93%" of manual prompts. Same post [P].
3. Escalation is mechanical: 3 blocks in a row or 20 total pauses auto mode and returns to prompting (not configurable); in `claude -p` the process is terminated or the action simply does not run. code.claude.com/docs/en/permission-modes [P] and the engineering post [P].
4. Org lock in Claude Code is `permissions.disableAutoMode` plus deny/ask rules evaluated before the classifier; in the Agent SDK order is hooks, deny, ask, mode, allow, `canUseTool`, and a hook `allow` cannot beat deny or ask. code.claude.com/docs/en/agent-sdk/permissions [P].
5. Claude Managed Agents (hosted) has the clearest server-side verdict record: every tool-use event carries `evaluated_permission` allow/ask/deny and `reason_code` ("keep in audit records"), the client "cannot override" a server denial, and a pause waits "indefinitely" for a `user.tool_confirmation`. platform.claude.com/docs/en/managed-agents/permission-policies [P].
6. Claude Code records decisions through OpenTelemetry only: `tool_decision` (decision, source, classifier_verdict, permission_mode, classifier_category) and `safety_event` (added 21 Oct 2026, v2.1.307; the 27 Oct changelog says it stopped being sent in v2.1.312). code.claude.com/docs/en/monitoring-usage [P] and changelog [P]. The two pages disagree on whether safety_event is current; I did not resolve it.
7. Codex has the closest org-lock peer: `requirements.toml` via cloud Admin Console, MDM, system file or self-hosted HTTPS endpoint, with cloud above system, allowlists for approval policy, reviewer, sandbox, rules, MCP identity, network; and an `auto_review` Guardian reviewer whose tenant policy section admins can replace. learn.chatgpt.com/docs/enterprise/managed-configuration [P].
8. Cursor "Auto-review" is a classifier on small Cursor-managed models (Gemini 3.5 Flash Lite, Haiku 4.5 fallback) reading plain-English `allow_instructions`/`block_instructions`, and Cursor says in so many words it is "not a security boundary"; team dashboard config overrides user and project files. cursor.com/docs/agent/security/run-modes [P].
9. Cursor's audit log excludes prompts, agent responses, generated code and cloud-agent runs; tool-call history is expected from hooks or Enterprise OpenTelemetry export. cursor.com/docs/enterprise/compliance-and-monitoring [P].
10. Gemini CLI has a real policy engine with tiers (Default 1, Extension 2, Workspace 3 disabled, User 4, Admin 5), root-owned admin policy directories, and decisions allow/deny/ask_user; YOLO is a mode that adds a high-priority allow-all default and can be switched off with `security.disableYoloMode`. geminicli.com/docs/reference/policy-engine [P], /docs/cli/enterprise [P].
11. GitHub Copilot cloud agent does not use an approval-per-action model; control is structural: one branch (`copilot/...`), the requester cannot approve its PR, Copilot "cannot approve or merge", Actions workflows wait for "Approve and run workflows", firewall on by default and covers only Bash-tool processes. docs.github.com risks-and-mitigations [P], customize-the-agent-firewall [P].
12. Across all vendors I found no first-class "agent acting for person B on A's request" check, no business-rule/intent policy ("no prod changes until Friday") and no spend/amount cap in the permission layer. The nearest are prose rules (Claude `autoMode` rules, Cursor `*_instructions`, Codex `guardian_policy_config`) judged by a model. Absence from the pages read, not proof.

## 2. Comparison table

| Vendor | (a) no-ask decision | (b) org lock | (c) ask + "always" | (d) recorded | (e) net/sandbox in auto | (f) cross-person / intent / amount |
|---|---|---|---|---|---|---|
| Claude Code | rules first, then read-only/workdir edits auto, "Everything else goes to the classifier" (Sonnet 5 default) | managed settings (earlier files), `disableAutoMode`, deny/ask rules, `allowManagedPermissionRulesOnly`; server-managed settings applied without prompt since 2.1.316 | dialog; 3/20 block fallback; "Recently denied" tab, `r` to retry; "don't ask again" = local allow rule (03 file) | OTel `tool_decision`, `safety_event`; local transcripts | sandboxed cmds no network by default; hosts named on the command are reviewed by classifier and opened for that command only | none; classifier treats user words as only authorization; no amount cap |
| Claude Managed Agents | `auto` server evaluation, or `always_allow`/`always_ask` per toolset/tool | per-agent config in API; no org console found | session pauses `requires_action`; `user.tool_confirmation` allow/deny + `deny_message` | every tool_use event has `evaluated_permission` + `evaluation` + `reason_code`; transcripts persist until deleted | hosted sandbox | `user.message` counts as intent; message between session threads is not read as intent; no cross-person authority |
| Codex | sandbox + approval policy (`on-request`, `never`, `granular`), optional `auto_review` reviewer agent (critical risk denied, fails closed) | `requirements.toml` (cloud, MDM, file, HTTPS endpoint); cloud > system | prompts; reviewer; rules file `prompt`/`forbidden` | local transcripts under CODEX_HOME; OTel off by default incl. approval decisions; Compliance API (page not read) | network off by default; managed proxy domains | none |
| Cursor | allowlist, sandbox, classifier (Auto-review), or Run Everything | dashboard team config overrides user/project; enterprise sandbox network, read policy, hooks | dialog; Protections still prompt | audit log: admin events only; tool calls via hooks / OTel (Enterprise) | sandbox network modes: sandbox.json only / + defaults / allow all | none |
| Copilot cloud agent | not per-action; structural limits | org/enterprise policy, firewall at org level, rulesets | PR review; workflow-run approval | session logs; audit log events for admins | firewall default on, recommended allowlist; Bash-tool processes only | requester cannot approve own PR |
| Gemini CLI | policy engine rules + approval modes | admin policy dir (root-owned), system settings override, `disableYoloMode` | dialog; "allow for all future sessions" scoped by mode | OTel `tool_call` with `decision` accept/reject/auto_accept/modify | docker sandbox via `tools.sandbox`; Seatbelt on macOS | none |
| Devin / Windsurf (merged) | allow/ask/deny rules + mode | org-level rules, sandbox, network for Enterprise | request card, session or permanent allow | not documented on pages read | sandbox derived from permission scopes | none |
| Antigravity | presets: Request Review, Default, Auto (Preview), Turbo; Deny>Ask>Allow | Auto "not available for business or enterprise accounts"; admin mechanism not on page | prompts | not documented | Default/Auto: sandbox, workspace+temp, no network | none |
| Factory Droid | autonomy `off/low/medium/high` + `permissionRules` | `maxAutonomyLevel` ceiling, `networkPolicy`, `mcpPolicy`, `modelPolicy`, `allowManagedHooksOnly` | prompts below level | not on page (`sessionRetentionDays`) | CIDR/IP outbound policy | none |
| OpenCode | allow/ask/deny patterns, last match wins | not on page | `once`/`always`/`reject`; `always` is session-only | not on page | n/a | none |
| Amp, Warp, Jules, Junie, Augment | not obtained (see section 5) | | | | | |

## 3. Per vendor

### 3.1 Anthropic

(a) Decision order [P permission-modes]: "Actions matching your allow, ask, or deny rules resolve immediately"; "Read-only actions and file edits in your working directory are auto-approved, except writes to protected paths and the first read outside the working directories"; "Everything else goes to the classifier"; "If the classifier blocks, Claude receives the reason." What it sees: "user messages, tool calls other than read-only lookups such as file reads and searches, and your CLAUDE.md content. Tool results are stripped from those requests, so hostile content in a file or web page can't manipulate the classifier directly." Engineering post: "We strip assistant text so the agent can't talk the classifier into making a bad call." Blocked by default: `curl | bash`, sending sensitive data externally, "Production deploys and migrations", force push, "Merging a pull request no human has approved, approving Claude's own pull request, or disabling CI checks", printing live credentials. Allowed: workdir ops, lockfile installs, read-only HTTP, "Pushing to any branch of the repository you're working in, including the default branch" (with deploy-like-name exceptions). Page caveat: "Auto mode reduces permission prompts but does not guarantee safety."

(b) `permissions.disableAutoMode: "disable"` in managed settings removes auto from Shift+Tab. Auto is on by default for Team and Enterprise; "built-in starting permission mode" from v2.1.283. Managed `autoMode` list semantics (additive, project settings not read) are in 04-existing-products. New: 2.1.316 (31 Oct) a managed `autoMode.allow` without `"$defaults"` no longer gets built-in exceptions back from `--settings` or `/auto-mode-setup`; server-managed settings fetched from Anthropic's API "now apply without an approval prompt, and a stored copy that doesn't verify is refused". 2.1.314: `permissions.blockWritesOutsideWorkingDirectories`. 2.1.305: `allowManagedPermissionRulesOnly` now strips `allowed-tools` from Desktop-mounted plugins.

(c) Agent SDK order [P agent-sdk/permissions]: hooks, deny, ask, permission mode, allow, `canUseTool`. "A hook that returns `allow` doesn't skip the deny and ask rules." Org-set connector tools: "Your organization requires approval for this tool" reaches the callback "even in `bypassPermissions` mode and even when an allow rule matches". Subagents inherit auto only when auto is available to them; `bypassPermissions` is never inherited as a subagent value (v2.1.267+). SDK sessions start in `default` since v2.1.302. Auto-approved tools never reach `canUseTool` (docs warning). Escalation: "if the classifier blocks an action 3 times in a row or 20 times total, auto mode pauses and Claude Code resumes prompting. Approving the prompted action resumes auto mode." Headless: "the action doesn't run and Claude keeps working" (docs) vs "we instead terminate the process" (post); the two pages differ. 2.1.314: `/permissions` Recently blocked "refuse[s] approving a call you can't read in full".

(d) [P monitoring-usage] `claude_code.tool_decision`: `decision`, `source` (config, hook, user_permanent, user_temporary, user_abort, user_reject), `classifier_verdict` (allow, block, no_verdict, unavailable; v2.1.301), `classifier_category` and `tool_parameters` only with `OTEL_LOG_TOOL_DETAILS=1`, `permission_mode`, `request_id` (v2.1.310). Note: "config" lumps managed policy, project settings, CLI flags and the classifier together, "The value doesn't say which one matched." `safety_event` (v2.1.307) holds "no command text, file paths, host names, or MCP server and tool names". Export is to the customer's own OTLP backend. No Anthropic-side admin audit log of tool decisions found.

(e) Sandbox: "Sandboxed commands don't get network access by default. Claude names the hosts a command needs on the command itself, the classifier reviews them with the command, and an approved list opens those hosts for that one command alone." Sandbox exclusions and escape hatches: 03 file. New: 2.1.309 excluded commands ask before writing to write-list paths when sandbox cannot run; 2.1.305 `sandbox.allowNestedUserNamespaces` "lets them get around the sandbox's file protections".

(f) None. Managed Agents: "What you post in `user.message` events counts as your intent, and it can lead the server to allow a call it would otherwise deny. The server does not read intent from a tool result, a fetched webpage, an MCP server's response, or a message between session threads." and "If you relay untrusted end-user input in `user.message` events, the server reads that input as your intent too." Also "`auto` is not a human checkpoint."

Managed Agents (b/c/d) [P]: policies set per toolset or per tool when creating the agent; defaults: agent toolset `always_allow`, MCP toolsets `always_ask`; "Running sessions keep the toolset configuration they were created with." Denied under auto: error result "Permission to use {tool_name} has been denied."; confirming a denied call returns 400. Beta, ZDR not eligible.

Claude in Chrome [P support.claude.com]: side panel starts in "Automatically approve" mode, "checks each action for safety, and pauses to ask you when it needs approval"; admins can enable/disable the extension and set site allowlists and blocklists on Team/Enterprise. Per-site permission details and blocked categories not on the page; the permissions guide was not fetched.

(g) Numbers (post [P]): stage 1 FPR 8.5%, full 0.4%; FNR on real overeager actions 17% (stage 1 6.6%); synthetic exfiltration FNR 1.8% -> 5.7% after stage 2; 93% of manual prompts accepted. Threat model lists overeager behaviour, honest mistakes, prompt injection, misaligned model; tuned mainly for the first two.

(h) Last 90 days [P changelog, only 15-31 Oct was visible]: 2.1.301 (15 Oct) classifier_verdict in OTel; 2.1.302 (16 Oct) `CLAUDE_CODE_AUTO_MODE_MODEL`, server-side classifier compacts long conversations instead of denying; 2.1.304 (19 Oct) `/auto-mode-setup` on Bedrock/Vertex/Foundry, mods get `decider: "person"`; 2.1.307 (21 Oct) safety_event; 2.1.312 (27 Oct) safety_event no longer sent; 2.1.314 (30 Oct) Cowork cloud auto mode treats a hook's MCP allow as "no answer"; 2.1.316 (31 Oct) see (b). Aug-Sep entries not read. [S] aiweekly.co says auto became default 14 Aug; not fetched.

### 3.2 OpenAI Codex

(a) [P learn.chatgpt.com/docs/agent-approvals-security] Sandbox modes `read-only`, `workspace-write`, `danger-full-access`; Auto preset = `workspace-write` + `on-request`; "OS-level mechanisms enforce sandbox policies." `approval_policy = "untrusted"` retired. `approvals_reviewer = "auto_review"` "route[s] eligible approval requests through a reviewer agent before Codex runs the request"; only actions that already need approval are reviewed; "denies critical-risk actions and fails closed when prompt-build, review-session, or parse errors occur." So the order is sandbox first, reviewer second, human third. Cloud tasks run in OpenAI-managed containers.

(b) [P managed-configuration] Cloud: Admin Console > Agent security (or "Edit as TOML"); local: system requirements file or macOS MDM; self-hosted HTTPS endpoint for non-ChatGPT logins. "Cloud requirements take precedence over system requirements. A cloud `false` turns off a profile that the system file allows." Keys: `allowed_approval_policies`, `allowed_sandbox_modes`, `allowed_approvals_reviewers`, `allowed_permission_profiles`, `allowed_web_search_modes`, `[permissions.filesystem] deny_read`, `[[remote_sandbox_config]]` by hostname, `[rules] prefix_rules` with `decision = "prompt"|"forbidden"`, `[mcp_servers.<n>].identity`, managed hooks, `[experimental_network]`, `[application.network]`, `[browser_use]`, `[computer_use]`. "Managed `guardian_policy_config` takes precedence over local `[auto_review].policy`"; it "replaces the tenant-specific section of the automatic review policy" while "the local runtime still uses the built-in reviewer template and output contract." `allowed_approvals_reviewers = ["auto_review"]` requires automatic review. Policies can target user, group or role; editors: workspace owners, admins, Codex admins. This is the only vendor found where an admin can write prose into the reviewing model's policy and have it outrank the developer.

(c) Prompts via CLI/app; granular policy keeps chosen categories interactive. Desktop app shows auto reviews as Reviewing/Approved/Denied. 0.166.0 (29 Oct) preserved acknowledged approvals on resume/fork. What "always allow" writes: not covered on pages read.

(d) OTel off by default; when on, "tool approval decisions" and results, with `log_user_prompt = false` default; decision events record approved/denied and "whether it came from configuration or the user." Local transcripts under `CODEX_HOME`. "Compliance API and audit events" is linked but was not read.

(e) Network off by default; `network_proxy` domain list starts empty; local/private destinations blocked; `.git`, `.agents`, `.codex`, `.aws` read-only inside writable roots. Managed: `managed_allowed_domains_only = true`; deny beats allow.

(f) None found.

(g) No numbers on auto_review found.

(h) [P changelog, October only visible] 0.165.0 (27 Oct): managed network review domains before allow rules; managed config loaders for custom config servers; config loading decoupled from ChatGPT auth. 0.166.0 (29 Oct): opt-in synchronous Guardian review for native tool calls; state-changing multi-agent calls routed through Guardian; isolated reviews in the Guardian pool; `network_proxy` in managed requirements; managed OTel configuration requirements. Aug-Sep not read.

Agents SDK needs_approval/interruptions and Codex cloud/ChatGPT approvals pages were not fetched (see section 5).

### 3.3 Cursor

(a) [P run-modes] "Run Modes control how the Cursor agent runs tool calls, and when Cursor interrupts you for approval." Modes: Auto-review (recommended): "Allowlisted calls run immediately. Other shell commands run in the sandbox when possible. Calls that can't use the sandbox go to the classifier."; Allowlist (no classifier); Run Everything ("no sandbox and no classifier"). Classifier reads plain-English `autoRun.allow_instructions` / `block_instructions` in `permissions.json` (user and project merged). "Auto-review is not a security boundary. The classifier can make mistakes in both directions." Models: Gemini 3.5 Flash Lite, Claude 4.5 Haiku fallback. Protections (Browser, File-Deletion, External-File) can still prompt.

(b) "A team configuration defined in the dashboard takes priority, and Cursor ignores the user and project files." Admins set available modes, sandbox networking, read policy (Enterprise). Team policies "apply on top, so local files can't weaken them." Blocking Haiku can disable Auto-review and members fall back to Allowlist. Distribution is cloud dashboard (plus MDM for hooks per [S] search summary).

(c) Sandbox escape: "Commands that need full system access bypass the sandbox, and Cursor asks you first."

(d) [P compliance-and-monitoring] Audit logs: who changed what (sign-ins, roles, API keys, settings, cloud-agent environments). "Payloads carry identifiers, names, and changed settings, never prompts, agent output, generated code, or credentials." Agent responses and tool calls "Not logged"; "Use hooks to log development activity." Export CSV, Admin API, stream to Splunk HEC/Sumo/webhook/S3 by contacting Cursor. Enterprise only. OTel export (tokens, tool calls, costs) separate, Enterprise only.

(e) Seatbelt (macOS), Landlock+seccomp (Linux, kernel 6.2+); network modes sandbox.json Only / + Defaults (default) / Allow All.

(f) None.

(h) [P changelog, 23 Sep-20 Oct only] Oct 6 remote control for local agents off by default for Enterprise; Sep 23 Rollouts and Security Review ("Rollouts does not merge or roll back on its own today"); Oct 16 Environments API beta, 300 repos; Oct 20 Agent SDK. No auto-review entries seen; the page window did not include August.

### 3.4 GitHub Copilot coding agent ("cloud agent")

(a) [P risks-and-mitigations] No per-action approval: "Copilot cloud agent only has the ability to push to a single branch" (`copilot/` or the PR's branch); branch protections and required checks apply; "The user who asked Copilot to create a pull request can't approve it. Copilot also 'cannot approve or merge a pull request.'" By default Actions workflows do not run on Copilot's changes until someone with write access clicks "Approve and run workflows". [P about-cloud-agent] "The page doesn't describe an explicit approval gate before Copilot acts"; 59-minute session cap; "Without write access to a repository, your Copilot session is read-only."

(b) [P access-management] Enterprise owners: AI controls > Copilot > "Enabled for selected organizations"; org owners can block repos; "Tool-call permissions, MCP servers, and third-party agents are configured at the enterprise level through `managed-settings.json` or the MCP policy." Firewall [P customize-the-agent-firewall]: org owners set it; repo admins can toggle only if org says "Let repositories decide".

(d) "Session logs and audit log events are available to administrators"; each commit message links to the session logs. Audit event names not found.

(e) Firewall on by default plus "recommended allowlist" (package registries etc.). It "applies only to processes the agent starts through its Bash tool"; does not cover MCP server processes or setup steps; "should not be considered a comprehensive security solution."

(f) The requester-cannot-approve rule is a one-person separation-of-duties rule; nothing cross-person beyond it.

(h) Not found (no dated entries fetched).

### 3.5 Google

Gemini CLI (a, b) [P policy-engine]: rule fields `toolName`, `mcpName`, `argsPattern`, `commandPrefix`, `commandRegex`, `decision`, `priority`, `modes`, `interactive`, `denyMessage`. Decisions `allow`, `deny`, `ask_user` ("In non-interactive mode, it is treated as deny"). Tiers Default 1, Extension 2, Workspace 3 (currently disabled), User 4, Admin 5; the page's examples contradict the table (User 3.1, Admin 4.02), so treat as unclear. Modes: `default`, `autoEdit`, `plan`, `yolo`; "In YOLO mode, a high-priority default rule allows all tools." Admin policy dirs: `/etc/gemini-cli/policies`, `/Library/Application Support/GeminiCli/policies`, `C:\ProgramData\gemini-cli\policies`; on Linux/macOS the directory "must be owned by root and not writable by group or others" or policies are ignored. [P enterprise] `security.disableYoloMode: true` ("highly recommended in an enterprise environment"); system settings override "has the final say"; measures "should not be considered a foolproof security boundary." Persistent approvals are scoped by mode: granted in yolo applies only to yolo; granted in default applies to default, autoEdit, yolo.
(d) [P telemetry] `gemini_cli.tool_call` with `decision` accept/reject/auto_accept/modify; approval_mode_switch events; prompts logged by default; to Cloud Logging/OTLP/file. Page does not say admins can force telemetry.
(h) [P changelogs] v0.58.0 (1 Sep) safety checkers on policy-config writes; v0.59.0 (8 Sep) fail-closed workspace trust; v0.60.0 (15 Sep) sandbox directory isolation; v0.61.0 (23 Sep) filesystem boundary hardening. No approval-mode entries in range.

Antigravity [P antigravity.google/docs/permissions]: presets Request Review, Default, Auto (Preview), Turbo. "Conflicting rules are strictly evaluated in priority order: Deny > Ask > Allow." Default/Auto run commands in a sandbox "with access restricted to your workspace and temp directories and no network access"; Auto "automatically" evaluates out-of-sandbox commands (page does not say a model does it); "Auto requires v2.23.0+ and is not available for business or enterprise accounts." No admin-lock or log description on page.

Jules [P jules.google/docs]: plan approval before code changes; runs in a VM; no network, PR, enterprise or log details on the page.

Gemini Code Assist agent mode: not fetched.

### 3.6 Cognition (Devin, Windsurf)

Windsurf docs now redirect to docs.devin.ai/desktop (307), i.e. Cascade lives under "Devin Desktop". [P devin-local/permissions]: "Deny rules block actions entirely (highest priority)", Ask always prompts, Allow auto-approves; rules at project, user or organization level; "Enterprise admins can enforce permission rules, sandbox mode, and network rules for every member"; mode "decides how much the agent asks for actions that no rule covers" (modes page not read). Ask card lets user edit the command; always-allow for session or permanent; MCP tool grants per tool or server. Sandbox writable paths derive from permission scopes. Logs, auto mode, and cloud-Devin autonomy settings not found: docs.devin.ai/admin/enterprise-security returned 404. Search found a federal air-gapped JSON Lines audit log (10 MiB rotation) [S, search summary only]. The Cascade page did not cover terminal auto-execution levels.

### 3.7 Brief

- Factory Droid [P docs.factory.com settings]: `autonomyLevel` off/low/medium/high ("`low`, `medium`, and `high` pre-authorize work at or below that risk level"); org ceiling `maxAutonomyLevel` ("Higher levels users select are clamped"); `networkPolicy` CIDR/IP, `mcpPolicy`, `modelPolicy`, `allowManagedHooksOnly`, `webSearchDisabled`, pushed by Factory App or managed settings.json; `permissionRules` allow/ask/block; legacy lists block > deny > allow. Audit not on page. Closest vendor to a risk-level ceiling an org sets.
- OpenCode [P opencode.ai/docs/permissions]: allow/ask/deny, most default to allow, last matching rule wins, `always` lasts only the session, per-agent overrides; no managed config on page.
- Amp [P ampcode.com/manual]: page returned only intro and nav listing Enterprise items (MCP Registry Allowlist, Thread Visibility, Workspace Entitlements, Allowed Models, Minimal Data Retention); nothing on permissions fetched.
- Warp: docs URL 404. JetBrains Junie, Augment: not fetched.

## 4. What this changes for Lightbulb's thesis

- Every harness decides locally in one person's process. The only org-lockable "auto" judgments are Codex `guardian_policy_config`, Claude `autoMode` prose rules, Cursor team `*_instructions`; all are prose given to a model, and all vendors say it is not a hard boundary (Cursor: "not a security boundary"; Claude: additive).
- The one org-level rule that is separate-person is Copilot's "requester cannot approve own PR" and Claude's default block on approving "Claude's own pull request". Nobody has an authority map; the escalation target is always the same human at the keyboard (or `requires_action` to the API client).
- Managed Agents `evaluation`/`reason_code` on every event is the nearest published decision-record shape; Claude Code's `source: config` hides which rule decided, so the "which rule/authority decided" field is genuinely missing and is a gap Lightbulb can claim.
- Message-source trust is already a design idea at Anthropic: user messages count as intent, "a message between session threads" does not. A signed who-asked-for-whom statement is the missing generalisation.
- Anthropic's own numbers (17% miss on overeager actions; 93% of prompts accepted anyway) support routing out-of-rule requests to a resource authority rather than relying on a model judge or an approving owner.
- Caution: Codex, Cursor and Factory already ship org-clamped ceilings through cloud consoles, so "company-controlled decision point" is not novel for single-vendor, single-person cases; the cross-person, cross-vendor part is.

## 5. Not verified

- Secondary or unfetched: Antigravity classmethod blog and Google forum bug (search summary only); Devin federal audit log format (search summary only); hoop.dev content (ignored); aiweekly claim that auto became default 14 Aug 2026; digitalapplied "numbers published" article (not used; numbers taken from the Anthropic post instead).
- Pages that 404 or returned nothing useful: docs.github.com manage-agents, cloud-agent concept firewall page, docs.devin.ai/admin/enterprise-security, docs.warp.dev agent-permissions, Amp manual (nav only), Jules (thin).
- Truncated: Claude Code permission-modes (last 17k chars unread), changelog (only 15-31 Oct read of 1.15M chars; Aug-Sep entries unread), monitoring-usage (last 72k unread; safety_event attribute list incomplete), Codex managed-configuration (last 25k unread, including precedence section and Compliance API), Codex changelog (only October).
- Not fetched: Claude Code managed settings/"don't ask again" (taken from 03 file), Claude in Chrome permissions guide and admin article, Agents SDK `needs_approval`/interruptions, Codex cloud/ChatGPT approvals and rules "always allow" write location, Codex Compliance API, Gemini Code Assist agent mode, Gemini Enterprise Antigravity controls, Devin cloud autonomy settings and playbooks, Devin modes page, Windsurf terminal page, Cursor hooks and permissions.json "add to allowlist" write target, Copilot audit log event names, Copilot managed-settings.json schema, Junie, Augment, Warp.
- Disagreements I did not resolve: headless behaviour at the block limit ("terminate the process" in the post vs "action doesn't run and Claude keeps working" in the docs); `safety_event` added 21 Oct and "stop sending" 27 Oct; Gemini policy tier numbers.
- The Cursor compliance page mentions "Grok Bot" features I did not research; kept only as quoted.
- Published acceptance, miss and incident numbers exist only for Anthropic; none found for Codex auto_review, Cursor Auto-review, Copilot, Gemini, Devin.

## 6. Primary pages fetched (29 successful)

1. https://code.claude.com/docs/en/permission-modes
2. https://code.claude.com/docs/en/changelog
3. https://www.anthropic.com/engineering/claude-code-auto-mode
4. https://code.claude.com/docs/en/agent-sdk/permissions
5. https://code.claude.com/docs/en/monitoring-usage
6. https://platform.claude.com/docs/en/managed-agents/permission-policies
7. https://support.claude.com/en/articles/12012173-get-started-with-claude-in-chrome
8. https://learn.chatgpt.com/docs/agent-approvals-security
9. https://learn.chatgpt.com/docs/enterprise/managed-configuration
10. https://learn.chatgpt.com/docs/changelog
11. https://cursor.com/docs/agent/terminal
12. https://cursor.com/docs/agent/security/run-modes
13. https://cursor.com/changelog
14. https://cursor.com/docs/enterprise/compliance-and-monitoring
15. https://docs.github.com/en/copilot/concepts/agents/coding-agent/risks-and-mitigations
16. https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/customize-the-agent-firewall
17. https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent
18. https://docs.github.com/en/copilot/concepts/agents/cloud-agent/access-management
19. https://geminicli.com/docs/cli/enterprise/
20. https://geminicli.com/docs/reference/policy-engine/
21. https://geminicli.com/docs/cli/telemetry/
22. https://geminicli.com/docs/changelogs/
23. https://antigravity.google/docs/permissions
24. https://jules.google/docs/
25. https://docs.devin.ai/desktop/devin-local/permissions.md
26. https://docs.devin.ai/desktop/cascade/cascade
27. https://docs.factory.com/cli/configuration/settings
28. https://opencode.ai/docs/permissions/
29. https://ampcode.com/manual (nav only)
