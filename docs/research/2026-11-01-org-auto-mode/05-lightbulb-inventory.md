# Lightbulb policy, approval, identity and audit inventory (2026-11-01)

Read-only inventory by a code-reading agent. CLI read at `origin/main` of `lightbulb-network.nosync` (local checkout 33 commits behind; line numbers are `origin/main`). Monorepo read at `origin/main` (working tree was on `codex/lantern-font`).

## Summary
1. Enforced per-person machinery exists, but it is all owner-scoped: CLI roster, `team.remove` and other person-only ops, the sign-in-proof `members` gate, the network-workers Allow/Ask/Refuse policy with signed approvals, the backend `purchases._policy` caps.
2. The only approval/decision rail is Paperclip "Needs you" cards plus CLI holds, and each answer is by one human.
3. The backend `company_policy` map is partly read: only `money_moves`, at `purchases.py:276`. `outreach`, `publishing`, `provisioning` are read by nothing.
4. Three doc claims lack code backing: Hermes governed-shell approvals are `mode: off`; the research doc's "hold keyed per person" is retired; the research doc's "2-hop cap" is wrong for the CLI (20 hops).
5. No org-level policy engine, no org audit log (`admin_actions` is platform-admin only), no authority delegation, no SCIM.

## CLI engine (`lightbulb-network.nosync`)

| Item | File:line | Keyed on | Decisions | Enforced? | Stored only? | Tests |
|---|---|---|---|---|---|---|
| Holds: permission kind | `src/holds.rs:108` `file_permission`, `:278` `answer`, `:44` `PERMISSION_S=600`, `:703` op `hold.file` | The agent session; prompt text filed by the Lightbulb app or a worker harness (`workers.rs:613`) | allow / decline / expired (10 min) | Yes: worker `oneshot` (`Registry::answered`, `workers.rs:188`) releases or denies the harness; app-filed Claude prompts answer through the desktop hook (`approval.rs`) | Signed `approvals` record (`holds.rs:347`) holds dialog text and verdict | `tests/holds.rs`; `tests/workers.rs` (~70 tests) |
| Holds: message kind | `holds.rs:34`, `:370` | n/a | expires at once (`not_allowed`) | **Dead.** `Kind::Message` is never created; `insert` (`:88`) called only by `file_permission` and `workers::take_assign`; `answer` rejects `Kind::Message` (`:296`) | n/a | `tests/holds.rs:302` `a_teammates_message_is_stored_not_held` |
| Holds: worker kind | `holds.rs:38`, `workers.rs:130` `take_assign` | Asker's member key, per host computer | Allow / Ask / Refuse | Yes: asker refused `declined` (`:149`); Ask creates hold shown as dialog `H<n>` | n/a | `tests/workers.rs` rows 3, 4, 6, 13 |
| Network-workers policy | `workers.rs:43` `policy`, `:68` `set_policy`, `:857` op `workers.policy`; gate `config.rs:43` `network_workers` default false | Member key; latest signed record per member (`always`, `refused`, `revoked`); own key Allow, others Ask | Allow / Ask / Refuse; `workers_max`, `workers_max_deadline_s` | Yes: `mail.rs:1017` refuses non-message kinds when flag off; `workers::offers` and `mcp.rs:101` hide tools; person-only (`workers.rs:~810`) | Per-member choices are `approvals` rows (`hold_id` NULL) | `workers.rs:1606`; `tests/workers.rs` rows 4, 10, 12; per-harness worktree-bypass refusal tests |
| `approvals` table | `store.rs:63`: `hold_id, verdict, scope, by, nonce UNIQUE, decided_at, record` | Signature is the computer's Ed25519 key | allow / decline / revoke; scope once / always / refused / revoked | n/a | Local to `state.db`; not synced to teammates or backend; no org signer | via hold and worker tests |
| Hooks | `hook.rs:28-35`: SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd, PermissionRequest, Notification | Agent session | **Observers only.** Set status (PermissionRequest -> `waiting`), hand over mail; Stop may return `{"decision":"block"}` only to deliver mail (`:126`) | No PreToolUse; no blocking of tool calls (PreToolUse payload normalises to none, `hook.rs:~769` test) | Tool, time, duration, <=200-char summary | `tests/hooks.rs`; unit tests |
| Admission | `admission.rs` `check:265`, `person_confirms:~437`, `join:~455` | Invite ticket (live, once, secret) | admitted / declined / `not_answered` / expired / `name_taken` | Yes: roster accepts `admit` only via `roster::pass`; limits (20, 5, 1) | n/a | `tests/join.rs` |
| Roster authority | `roster.rs:76` `Role` (Founder/Member); `pass()` ~`:315-420`; `:501` `take` | Signer key | Any member may admit, invite, remove (not itself, never the founder) | Yes: deterministic replay; removed signer's concurrent entries dropped; `net_ops.rs:55` `gate`: `team.remove` person-only, member-only | Signed, hash-chained | `roster.rs` unit tests; `tests/net_two.rs` |
| Native join confirm | `admission.rs:~437`; `ipc.rs` `team.join`; `docs/security.md:122-134` | Agent's `team.join` on macOS | Join / Don't join (default no, 10 min) | Yes; with no dialog the join is refused | n/a | `tests/join.rs` |
| Same-uid bypass | `hook.rs:1-7`, `net_ops.rs:853` `person_only` ("same-uid ceiling"), `security.md:122-134` | Process ancestry of socket peer | n/a | Code states the ceiling: a detached same-user process passes | n/a | `net_ops.rs:887` |
| "Person at a terminal" | `ipc.rs:168` `Caller::is_person`, `net_ops.rs:853` `person_only` (walks pid ancestors; true only if no ancestor is a known agent; failed lookup = false) | Process tree | person / agent / `not_a_person` | Yes on ~20 call sites: `team.remove`, `memory.forget`, `memory.share`, `memory.team`, `sessions.upload`, `team.leave`, `ui.claim`, `share.grant`, `workers.*`, `holds`; `send` from `<member>/terminal` by a non-person refused (`ipc.rs:~305`) | n/a | `tests/embed.rs:194`, `tests/envelope.rs:579` |
| Workspace member gate / sign-in proof | `mail.rs:1060` `not_a_member`; `proof.rs:115` `admitted`; `net_ops.rs:772` `revoked_of`; `net/frames.rs:24` | Computer key; proof is EdDSA JWS `{sub, computer, exp}` naming a person in workspace `members`; `None` = no gate | accept / `not_a_member` | Yes at receive; `proof::admitted` filters `revoked_keys` | `members` and `revoked_keys` come from the backend's sessions document | `tests/member.rs:653, 693, 726, 810` |
| Config gates | `config.rs:23-60`: `lan`, `relay`, `folders_off`, `memory_team`, `capture_upload`, `capture_turns`, `network_workers`, `workers_offer`, `workers_max`, `workers_max_deadline_s`, `share_paths`, `preview_to_workspace` | The computer | on/off, values | Yes (e.g. `memory_team` at `ipc.rs:463`) | Local `config.toml`; no org push | `config.rs` tests |
| Memory sync scopes | `memory.rs` `Scope::Team`/`Me`, `ipc.rs:463`, `:891-940` | `team` to every member; `me` to the person's own signed-in computers | n/a | Yes; `memory_team` off stops team writes; `memory.forget`/`memory.share` person-only | n/a | `tests/memory_sync.rs:62, :144` |
| Loop caps | `mail.rs:30` `MAX_HOPS=20`, `:574` `hops_ok`; rate 12 per 10 min per sender to one session | Hops; sender and session | `loop` refusal naming `hops` or `rate` | Yes at send (`mail.rs:~625`) and receive (`:~1075`) | n/a | `mail.rs:1551`; `tests/envelope.rs:579` |
| `docs/security.md` | `:104-116` "Everyone on the network is trusted... nothing is held and there is no per-person Allow or policy" | | | True for messages; stale for worker holds and `workers policy` | | |

## Monorepo backend (`apps/backend/`)

| Item | File:line | Keyed on | Decisions | Enforced? | Stored only? | Tests |
|---|---|---|---|---|---|---|
| Company policy map | `routers/company_policy.py:3-6, 42-57`; `billing_repo.py:1518-1553` `CAPABILITY_POLICY_KEYS` | owner (org); `money_moves`, `outreach`, `publishing`, `provisioning`; absent = True (needs owner approval) | boolean per capability | **Partly.** Only `money_moves` read, `core/services/purchases.py:276` (off + allowlisted merchant => Link spend request created without the owner's click; card still filed). Others read by nothing. Docstring still says "NOT enforced... P1" | billing-row field | `test_company_policy.py`; no purchases test of the branch found |
| `revoke-all` | `company_policy.py:64-190` | owner | kill switch: cancels provisioning workflows, deletes credentials, disconnects apps/Slack, revokes Link wallet, severs Stripe | Yes, explicit call | n/a | `test_wipe_never_deprovisions.py` |
| Purchase limits (Link wallet) | `purchases.py:191` `_policy`, `:254` `request_purchase`, `:353`; `billing_repo.py:1556-1585` | owner, project, merchant, amount (`perPurchaseCents`, `projectMonthlyCents`, merchants allowlist; $500 hard max) | allow / refuse (`no_limits`, `over_purchase_cap`, `over_project_cap`) / needs-owner card then Link approval | **Yes**, fail-closed, under `owner_lifecycle_guard`; re-checked at click; rejecting cancels (`teams/approvals.py:118`) | ledger `PURCHASE#<id>` | `test_link_wallet.py` |
| Provisioning approvals | `provisioning_approvals.py` (markers `provider_consent`, `provider_connect`, `provider_information`, `payment_setup`; `DEFAULT_USAGE_LIMIT` 5000 c/month); callers `provisioning_workflow.py:512, 565` | owner; provider/service | Paperclip card approve/reject | Workflow stops until owner decides | card payloads only | not located |
| Company Stripe approval | `company_stripe_approval.py:21` | owner | informational card | **No** (notice only) | | |
| `wake_policy` | `wake_policy.py` | owner / trial | none ("a REPORT, not a gate") | No | | |
| Teams approvals BFF | `routers/teams/approvals.py:61-172` | company id from member's Paperclip session | approve / reject / revision / resubmit | Company fence only (`:39`). **Any workspace member can approve**; no role check, no approver≠requester rule | decisions in Paperclip fork | not located |
| Teams budgets | `routers/teams/budgets.py:23-64` | company, agent; monthly cents | set requires org admin; reads open | BFF passes through to Paperclip; enforcement there, not verified | Paperclip | `test_budgets.py` |
| Admin audit | `admin_audit.py` `@audit_admin_action`; `admin_actions_repo`; `routers/admin.py:193` | **platform admin** `/admin/*` writes | | Yes for platform routes only; `users.py:346` one extra write | `admin_actions` | `test_admin_audit.py` |
| Inference gate | `chat_gate.py:41` `gate_chat`; callers `messaging.py:1308, 1340, 4388`, `apps_run.py:358` | owner_id: subscription, free-tier $3/day, Metronome credit block; hard stop is the AWS meter Lambda | blocked with code / allowed | Yes; fails closed | | `test_chat_gate.py` |
| Hermes "threat guard" | **No code in this repo.** `hermes_profiles.py:161` `APPROVALS_DENY` (4 patterns: `rm -rf /`, `mkfs*`, `dd if=* of=/dev/*`, fork bomb); `:366-372` `approvals: {mode: "off", cron_mode: "deny", single_query_mode: "deny", unattended_mode: "approve"}`; `hermes/managed/config.yaml:11-17`; writes fenced by `HERMES_WRITE_SAFE_ROOT=/opt/data` (`bridge/sync.py:220`) | agent runtime | deny-list match blocks | Only the 4-pattern list, by upstream Hermes; no file-write or URL-fetch rule list found | | `test_hermes_managed_overlay.py` (not read) |
| Agent `request_approval` tool | `infra/hermes/plugins/lightbulb/__init__.py:585-712` | agent asks; owner decides | files Paperclip `request_board_approval` card; approving wakes the agent | **Voluntary**: the model chooses to call it | | not located |
| Board executionPolicy | `teams/issues/executionPolicy.ts`, `panels/routineExecutionPolicy.ts` | issue; participants `agent:<id>`/`user:<id>` in review/approval stages | stage type review or approval | **Client-side merge helpers**; enforcement in Paperclip; governed cards forced human-only (`interaction-audience.ts:158`) | | `*.test.ts` |
| Hosted MCP `send_message` | `hosted_mcp.py:544` `_sender`, `:710`, `:810` author `"{client} via {human}"`; network sends via `_through_member` (`:650`) through the person's pinned computer | OAuth client or agent key plus the signed-in human | posted / refused | Auth is the OAuth token; no per-tool or per-recipient policy; authority is the person | | `test_hosted_mcp_*.py` |
| Hosted workers | `hosted_mcp.py:1015`; `config.py:452` `HOSTED_NETWORK_WORKERS` | deploy flag | hidden / shown | Yes (global) | | |
| `/network/team`, `/network/join-line` | `routers/network.py:120, 137`; `network_member.py:253` `mint_line` | paired Mac or signed-in computer (`X-Lightbulb-Proof`); person must be in workspace; Clerk browser token refused | once link / 409 / 403 / 503 | Yes; **any workspace person can mint**; `/network/leave` admin-only | | `test_network_login.py` |
| `networkSessionId` | `local_session_relay.py:78, 150, 178, 734`, `local_sessions.py:149, 340, 1116` | session descriptor | | identifier only; no policy attached | session row | |
| Agent reply cap (rooms) | `messaging.py:55` `AGENT_REPLY_CAP = 2`, `:4930-4945` | room; human message hop 0; agent post at `hop >= 2` wakes nobody | wake / no wake | Yes; this is the only "2-hop" cap and it lives in backend rooms | | `test_agent_reply_cap.py` |
| Network holds notice | `routers/network.py:211-228` `HoldNotice`; `network_member.py:357` | bridge hook token | open / closed | notification path | | |

## Desktop

| Item | File:line | Keyed on | Decisions | Enforced? | Tests |
|---|---|---|---|---|---|
| Local-session approval | `apps/desktop/src-tauri/src/local_sessions/approval.rs` (1063 lines) | Claude Code `PermissionRequest` hook; answered only with approval id + SHA-256 of exact tool input (`deny_unknown_fields`) | allow / allow+mode (`acceptEdits`, `plan`, `default`) / allow+Claude's suggested rule / deny; never `updatedInput` or a persistent rule | Yes for the hook. Remote allow needs `full` (whole input <=10 KiB) or `exact` (Read/WebFetch/WebSearch); else deny-only remotely. Holds <=55 min; answers only on a backend-admitted command from an authorized human with a live shared mirror | ~20 `#[test]` (`:525-1036`) |
| Same-uid note | `lifecycle.rs:340, 348, 891` | datagram/side file anything running as the same user can write | | Mitigated by digest re-check (`approval::reply_allowed`); not a boundary against same-uid code | in `lifecycle.rs` |

## Documentation claims vs code

`apps/frontend/src/content/docs/pages/approvals-security-privacy.md`
- "Before a governed shell command runs, the agent asks" (l.13): **not backed**. Shipped Hermes profile `approvals.mode: "off"`, `unattended_mode: "approve"`, only the 4-pattern deny list. The desktop `PermissionRequest` path covers Claude Code on a person's Mac, not Hermes workspace agents.
- "Hire approvals" (l.14): backed (`teams/approvals.py`, `marketplace_hire.deploy_for_approval`).
- "Board approvals for governed work" (l.15): partly; agent must voluntarily call `request_approval`; Paperclip enforces stages.
- "Agents can't change plan, provider, billing, integrations, publish" (l.16): not verified; spot checks support it; agent-side tool surface not audited.
- "A threat guard... blocks commands, file writes, and URL fetches" (l.30): only a 4-entry command deny list and the write fence; no URL-fetch rule in this repo; upstream Hermes behaviour.
- Free-tier allowance and per-request credits (l.34-35): backed by `chat_gate` and the openrouter gate.
- "Monthly budget per agent in Teams" (l.35): BFF exists; enforcement in Paperclip, not verified.

`docs/security.md` (CLI): roster, proof, `not_a_member`, `revoked_keys`, person-only ops, memory scopes backed. "Nothing is held and no per-person Allow or policy" true for messages, stale for network workers. Says 20 hops; code agrees.

`docs/research/2026-10-21-enterprise-agent-beacon-ramp-glass.md` §3: "hold keyed per person; one switch; 2-hop cap" is stale or wrong (message holds retired; CLI cap is 20; room cap is 2; "one switch" is the `capability_policy` map, of which only `money_moves` is read). Messaging table and `admin_actions` claims confirmed. Per-owner caps confirmed, owner level only. No SAML/SCIM: plausible, not searched.

## Gaps

Documented but not enforced
- `outreach`, `publishing`, `provisioning` policy keys stored and shown in UI; nothing reads them.
- Hermes governed-shell approval is off; the "threat guard" is a 4-pattern deny list.
- Company Stripe card and `wake_policy` are explicitly non-gating.
- `request_approval` is advisory.

Enforced, but per owner / person / computer only
- CLI worker policy and approvals are signed by a computer key and stored locally; an org cannot push or share them.
- `person_only` is a same-uid process-ancestry check; a detached same-user process passes.
- Purchase caps, `chat_gate`, capability map keyed on `owner_id` and amount; no agent, vendor-category or role dimension; no per-person limit.
- Teams approvals: any workspace member can approve; no approver role, no self-approval block, no threshold routing.
- `/network/join-line`: any workspace person can mint; only leaving is admin-only.
- Roster authority is flat: any member may invite or remove anyone except the founder.

No org-level concept
- No org policy engine over (actor, action category, amount, counterparty) -> allow/deny/ask/hold.
- No delegation or authority levels.
- No customer-facing audit trail (`admin_actions` platform-only; CLI approvals local; Paperclip decision history is the only decision log, export not inspected).
- No org-wide kill switch beyond `revoke-all`; nothing pauses agent actions by category.
- No counterparty identity layer (merchant allowlist only).
- No escalation routing; every hold goes to the one owner and expires at 10 min (CLI) or 55 min (desktop).
- No human-only classification except the UI's `governed_action` narrowing, enforced by the Paperclip fork.

Not verified: Paperclip's enforcement of budgets and execution stages; purchases tests; `test_hermes_managed_overlay.py`; any SAML/SCIM code.
