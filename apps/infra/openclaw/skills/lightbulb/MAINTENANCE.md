# Maintaining the `lightbulb` skill

Engineer-facing. This lived inside SKILL.md as an HTML comment, which
meant every agent on the fleet paid for it on every load — the comment
is invisible in rendered markdown but not to a model reading the file.
Moved out so the skill body carries only what an agent needs.

MAINTENANCE (Lightbulb engineers): every claim in this skill is anchored to
  code. This skill is the fleet's knowledge of the platform. It is SPLIT: a
  thin SKILL.md (routing table + non-negotiables) plus references/ read on
  demand. Each anchor below names the file that now carries the claim.
  - Capability inventory .... references/platform-primitives.md
                              <- routers/teams/issues.py (add_issue_comment wake) + core/services/paperclip_adapter_config.py;
                                 routers/channels.py (SUPPORTED_PROVIDERS — keep the channel list in lockstep);
                                 routers/ses_inbound.py (email wake — allowlisted senders only);
                                 extended-image gog binary (gmail settings watch = Pub/Sub)
  - Control Panel tabs ...... references/surfaces.md
  - Workspace search ........ references/surfaces.md
                              <- apps/frontend/src/components/workspace/WorkspaceHeader.tsx (search button + shortcut label)
  - Message actions ......... references/surfaces.md
                              <- apps/frontend/src/components/messaging/{RoomMessageItem,RoomMessageContent}.tsx (reactions,
                                 reply-in-thread, edit/delete); messageLinks.ts (copy-link permalink format);
                                 RoomPanel.tsx renderRun (muted "<Agent> finished · no reply" / "stopped" / "run failed"
                                 status line for a silent agent turn)
  - Inbox page .............. apps/frontend/src/components/teams/inbox/InboxPage.tsx (the three bands);
                              InboxNeedsYou/InboxHappeningNow/InboxRecently.tsx;
                              apps/frontend/src/components/workspace/panelRegistry.ts (INBOX_SECTIONS + the teams-activity alias)
  - Tasks groupings ......... apps/frontend/src/components/teams/issues/TasksList.tsx (TASKS_GROUP_BY / GROUP_OPTIONS);
                              panelRegistry.ts (the teams-goals / teams-projects aliases + the groupBy param)
                              <- apps/frontend/src/components/control/ControlSidebar.tsx (NAV_ITEMS)
  - Questions / secrets ..... references/surfaces.md
                              <- apps/frontend/src/hooks/useQuestions.tsx + components/chat/QuestionCards.tsx (question.requested/resolved,
                                 question.resolve incl. secretStoreAllowedHosts); AgentProgressCard.tsx (progressCard.get/changed);
                                 ChatInput.tsx editable queue; core/containers/config.py TOOL_DENY_BASE (ask_user + secrets are NOT denied)
  - Integrations wiring ..... references/integrations.md
                              <- apps/backend/routers/integrations.py; core/containers/config.py (integration_mcp_servers merge)
  - Marketplace flows ....... references/marketplace.md
                              <- apps/backend/routers/{marketplace_listings,marketplace_import}.py; core/services/{marketplace_service,agent_export,catalog_service,claude_code_import}.py
  - Starter team + roster ... references/company-and-plans.md
                              <- apps/backend/core/services/paperclip_provisioning.py (STARTER_ROSTER + provision_org);
                                 core/services/{agent_provenance,paperclip_agent_lifecycle}.py;
                                 core/repositories/paperclip_repo.py (starter_agent_ids)
  - Plans / providers ....... references/company-and-plans.md
                              <- apps/backend/core/services/{billing_service,provision_gate,owner_facts}.py
  - Provisioning rail ....... references/money.md
                              <- apps/backend/routers/company_provisioning.py (container routes, refusal codes);
                                 core/services/provisioning_workflow.py (STATES); core/services/owner_facts.py (the `provisioning` facts list)
  - Channels ................ references/integrations.md <- apps/backend/routers/channels.py
  - Skills (install/scope) .. references/skills.md
                              <- fork src/cli/skills-cli.ts (install <skill-ref>, --agent, --global, --as);
                                 src/skills/lifecycle/archive-install.ts (<workspaceDir>/skills);
                                 src/config/types.agents.ts (AgentEntry.skills allowlist REPLACES defaults);
                                 src/gateway/methods/core-descriptors.ts (skills.install/upload.* = operator.admin);
                                 paperclip fork server/src/routes/agents.ts — listSkills exists for
                                 claude/codex/cursor/gemini/grok/opencode/pi but NOT openclaw_gateway,
                                 which is why the company-skills route answers supported:false
  - Skills / self-learning .. references/marketplace.md
                              <- apps/backend/core/containers/config.py (skills.workshop block);
                                 apps/frontend/src/components/control/panels/{SkillsTab,SkillProposalsPanel}.tsx
  - Company wiki ............ references/platform-primitives.md
                              <- apps/backend/core/containers/config.py (memory_plugin_entries — memory-wiki shared global vault)
  - Brain panel ............. references/surfaces.md
                              <- apps/frontend/src/components/teams/panels/BrainPanel.tsx (Notes/Dreams tabs); BrainPage.tsx + BrainLanding.tsx + brainVault.ts (rendered pages, pills, Related, index cards); apps/frontend/src/components/control/panels/DreamsPanel.tsx (embedded prop)
  - Browser live view ....... references/surfaces.md
                              <- apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx + WorkspaceHeader.tsx (Chrome button);
                                 apps/backend/routers/browser_view.py (VNC relay); apps/infra/lib/stacks/container-stack.ts (Xvnc/matchbox/websockify boot);
                                 core/containers/config.py (browser.headless=false + executablePath — the agent drives real /usr/bin/google-chrome)
  - Sage threat guard ....... SKILL.md, the boundaries table
                              <- apps/infra/openclaw/Dockerfile (Layer 13c); apps/infra/openclaw/sage/config.json
  - Connectable catalog ..... references/integrations.md
                              <- apps/backend/routers/platform_info.py (live GET /api/v1/platform-info);
                                 core/services/credentials/registry.py (all_providers); core/containers/config.py (CHANNEL_PROVIDER_IDS);
                                 core/containers/ecs_manager.py (ISOL8_API_URL env the container reads)
  - Agent-started connect ... references/integrations.md
                              <- apps/backend/routers/connections.py (authorize_from_container — hook-token sibling of authorize;
                                 ISOL8_OWNER_ID + OPENCLAW_HOOK_TOKEN env in ecs_manager.py);
                                 /app/skills/lightbulb/scripts/lightbulb-connect.sh (the mint door — never an inline header, see the redaction note)
  - Paperclip org model ..... references/company-and-plans.md
                              <- TheLightbulbCompany/paperclip fork: server/src/services/agent-permissions.ts,
                                 services/agents.ts (getChainOfCommand), adapters/registry.ts (openclaw_gateway)
  - Launch competition ...... references/surfaces.md
                              <- apps/backend/routers/launch.py; core/services/launch_{phase,settings,service,referral,close,revenue}.py
  - Email ................... references/email.md
                              <- /app/skills/lightbulb/scripts/lightbulb-mail.sh + backend routers/agent_mail.py + core/services/agent_mail_send.py (one send door; policy table in its docstring)
  - Hook-token calls ........ SKILL.md (the non-negotiable) + references/money.md (the status tables)
                              <- /app/skills/lightbulb/scripts/lightbulb-api.sh — every OPENCLAW_HOOK_TOKEN call goes through a script on disk; OpenClaw's
                                 transcript redaction masks that header including $VAR refs, so an agent-typed curl poisons its own next turn (401 loop)
