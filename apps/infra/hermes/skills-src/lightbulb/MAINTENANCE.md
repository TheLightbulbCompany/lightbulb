# Maintaining the `lightbulb` skill

Engineer-facing. This lived inside SKILL.md as an HTML comment, which
meant every agent on the fleet paid for it on every load — the comment
is invisible in rendered markdown but not to a model reading the file.
Moved out so the skill body carries only what an agent needs.

This tree is the source of the Hermes image's bundled skills, and its only
consumer: `apps/infra/hermes/build-skills.sh` copies it, swaps a handful of
tokens (`/app/skills/` -> `/opt/hermes-skills/`, `exec` -> `terminal`,
`~/.openclaw/lightbulb-facts.json` -> `$HERMES_HOME/lightbulb-facts.json`,
`skill_workshop` -> `skills`), and drops the engineer-facing MAINTENANCE.md files. There is no
overlay: this tree is the only copy, so write Hermes wording here directly when a
token swap cannot express it (a per-file overlay drifted silently and shipped
stale text until it was deleted). Hermes tool names are anchored to its
`CONFIGURABLE_TOOLSETS`
(`terminal`, `file`, `web`, `memory`, `skills`, `browser`, `secrets`) plus our
own plugin tools in `apps/infra/hermes/plugins/lightbulb/__init__.py`
(`send_room_message`, `hire_suggestion`, `request_approval`, `lightbulb_read_rooms`, `lightbulb_local_sessions` — read-only, `resources` and `read`;
an agent messages a session with `lightbulb_network`, through the engine).

MAINTENANCE (Lightbulb engineers): every claim in this skill is anchored to
  code. This skill is the fleet's knowledge of the platform. It is SPLIT: a
  thin SKILL.md (routing table + non-negotiables) plus references/ read on
  demand. Each anchor below names the file that now carries the claim.
  Budget: keep SKILL.md near 10k chars and the front-matter `description`'s
  first 57 chars a trigger — Hermes's skill index shows only those. The
  "What you cannot do" table and "Quick answers" moved to
  references/boundaries.md on 2026-10-08.
  - Capability inventory .... references/platform-primitives.md
                              <- routers/teams/issues.py (add_issue_comment wake) + core/services/paperclip_adapter_config.py;
                                 routers/ses_inbound.py (email wake — any authenticated sender while the row's
                                 `send_to_anyone` is on, the default; else allowlisted senders only) and
                                 core/services/agent_mail_send.py (the same flag gates `--to`/`--reply`);
                                 the Google scopes in providers.yaml (gmail.modify, calendar, drive)
  - Send a file in chat ..... references/platform-primitives.md ("Send a file in chat" row)
                              <- apps/infra/hermes/bridge/turn.py (MEDIA_PREFIX, MEDIA_CONTENT_TYPES,
                                 ATTACHMENT_MAX_BYTES_BY_TYPE incl. video, MAX_ATTACHMENTS, _within: own profile or
                                 HERMES_HOME/outbox/ (copied into <profile>/outbox/), no dot parts; _note replaces an
                                 unsendable line; videos go S3-direct via _upload_attachment) + the room-turn system
                                 prompt in the same file; sync.py save_profile (only the profile dir persists).
  - Shell working folder .... SKILL.md (scripts non-negotiable: "your cwd is this conversation's own folder")
                              + references/email.md
                              <- apps/infra/hermes/bridge/turn.py (conversation_dir, _terminal_scope TERMINAL_CWD).
                              `.scratch/` for worktrees <- the upstream base image's HERMES_WRITE_SAFE_ROOT=/opt/data
                                 (write_file/patch refuse /tmp, which is why the old "/tmp/wt" rule never worked);
                                 sync.py SCRATCH_DIR in EXCLUDE_GLOBS (never synced); save_profile (only the profile dir persists).
                              one worktree per task (a Paperclip task's made and reaped by the bridge), installs only in small projects, none for a large repo (sentence identical in SKILL.md, software-projects.md and turn.py `_repo_rule`)
                                 (SKILL.md scripts bullet + software-projects.md "Keep the workspace small"):
                                 .scratch/ and /tmp share the microVM's ~4 GB disk across teammates until it is
                                 recycled; numbered slots filled it twice for the founder's engineering agent
                                 (2026-10-07/08). Mirrors bridge/turn.py `_repo_rule`; edit both together.
  - gog scope-denied ........ references/integrations.md ("Google Workspace (`gog`)": ask for a reconnect in
                              Connections; never a browser sign-in)
                              <- Google is the platform app, unverified under the 100-user exemption; its scopes are
                                 the providers.yaml `google` list. A 403 means a grant from before the scopes widened.
  - Plain words, no plumbing  SKILL.md ("Talk to people in plain words, never plumbing")
                              <- observed prod 2026-09-29: an agent told #general "PAPERCLIP_RUN_ID is empty (this is
                                 a webchat session, not a heartbeat)". No code enforces it; the verifier footer that
                                 leaked the same class of text is off in apps/infra/hermes/managed/config.yaml
                                 (display.file_mutation_verifier).
                              The progress-line + stale-notice sentences in that bullet, detailed in
                                 references/chat-vs-document.md "What a room message may say":
                                 <- apps/infra/hermes/bridge/turn.py _progress_poster (Hermes interim_assistant_callback:
                                 text between tool calls is posted to an addressed room turn as a `progress` line) +
                                 backend core/services/chat_gate.py ("trial_daily_cap" notice text). Observed dev
                                 2026-10-14 (Walker Studio): "Let me try creating with --status todo" posted to #general,
                                 and the CEO read an old allowance notice as a live cap. A running turn proves the
                                 openrouter-gate admitted it, which is why "you are running, so the allowance is not
                                 stopping you" holds (gate is per owner key, so for every teammate).
  - Decisions are approvals . SKILL.md ("A person's decision is an approval") + references/chat-vs-document.md
                                 (step 3 disposition, "The first run" step 2) -> the paperclip skill's
                                 references/interactions.md "When a person must decide" -> the `request_approval`
                                 tool (apps/infra/hermes/plugins/lightbulb/__init__.py _request_approval: picks
                                 approve_ceo_strategy only for companyPlan + root CEO by role, resolves WAL-6 identifiers,
                                 links them, moves them to in_review, returns the app link). Also named in
                                 references/chat-vs-document.md step 3 + "The first run" step 2 and backend
                                 core/services/onboarding_brief.py _CEO_DECISION/_SPECIALIST_DECISION.
                              <- apps/frontend/src/components/messaging/OnboardingBand.tsx (approvalAnswered: any
                                 answered row of /teams/approvals) + apps/backend/routers/teams/approvals.py.
                                 Observed dev 2026-10-14: the CEO filed "Approve pricing packages" as a task assigned
                                 to the person and the approvals list stayed empty. Observed again on image
                                 h-2026.10.14-f8eebfc (the raw POST + jq instructions): "Send the launch plan to me for
                                 approval" got "Reply 'Plan approved' here" and no approval for 9 minutes; hence the tool.
  - App host for links ...... SKILL.md ("Links: use this workspace's own app host")
                              <- core/services/owner_facts.py (`app_url` = settings.FRONTEND_URL)
  - Workspace memory ........ references/platform-primitives.md ("Queryable company state" row)
                              <- apps/infra/hermes/plugins/memory/agentcore/__init__.py (one /owners/<owner>/
                                 namespace for every agent; workspace_memory_search tool; tasks_board =
                                 the backend board in a turn with an audience, recall and on_pre_compress); bridge/sync.py normalize_profile_backends
                                 (on only while AGENTCORE_MEMORY_ID is set); ~2 min = the memory
                                 strategy's async extraction lag, not a code constant.
                              Scopes + review: DM = private to that person (phase 1), other rooms = team;
                                 "Confirmed by a person:" <- core/services/workspace_memory.CONFIRMED_PREFIX;
                                 people review/delete/Share <- routers/memory.py, frontend AgentMemoryTab
                                 (Inspect agent -> Memory tab, references/surfaces.md "Agent inspector" row), mobile
                                 MemoriesCard; the agent cannot delete (no tool), MEMORY.md is editable in
                                 AgentInstructionsTab.
  - Website sign-ins .......... references/boundaries.md ("Type a website password" row; names the web AND iPhone doors) + references/surfaces.md (Browser row)
                              <- apps/infra/hermes/bridge/website_logins.py (vault store + ask callbacks);
                                 core/services/website_logins.py; frontend WebsiteLoginsSection (Connections).
                                 Codes/CAPTCHA are browser-only (owner ruling 2026-10-07): the bridge's
                                 _code_prompt posts the line and watches the page; there is no code form.
  - Background subagents .... references/surfaces.md ("Background subagents" row) <- core/services/subagents.py + packages/shared/src/messaging/subagents.ts
  - Members / join link ..... references/surfaces.md ("Members & join link" row)
                              <- routers/orgs.py (one link per workspace; any member invites + copies, admin-only
                                 replace/revoke + pending list; open join as org:member); app/join/invite
                                 (invitation landing); frontend WorkspaceMembersSection (Settings →
                                 Members), workspace/InvitePeoplePanel + InvitePeopleDialog (email invites, pending/revoke,
                                 opened by the chat sidebar's Invite someone row, messaging/RoomDirectory.tsx), app/onboarding JoinTeamBlock (paste a join link), and
                                 MessagingWorkspaceContext.copyJoinLink (channel/group-chat Members section, not DMs).
  - Data page ............... references/surfaces.md ("Your data" row) <- apps/frontend/src/app/security/page.tsx
  - Away email / daily summary references/surfaces.md ("Away email and daily summary" row)
                              <- core/services/needs_you_email.py (5 min, quiet hours, per-room cap, membership),
                                 core/services/daily_digest.py, components/settings/DailySummaryCard.tsx
  - Browser live view ....... references/surfaces.md
                              <- Mac viewer input: apps/frontend/src/components/workspace/panels/BrowserFrameView.tsx
                                 (event producer); apps/backend/routers/container.py and
                                 apps/backend/core/services/mac_live.py (room membership and routing);
                                 apps/desktop/src-tauri/src/agent_cast.rs (input and human-yield signal).
                              <- apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx;
                                 apps/backend/core/services/browser_sessions.py; apps/backend/core/services/browser_control.py; apps/infra/hermes/Dockerfile (agent-browser pin);
                                 apps/infra/hermes/bridge/browser_control.py (shared-screen yield; one lease for
                                 the bridge, released ~30 s after the last run browses) + bridge/cdp_mux.py (one
                                 AWS connection, each run its own tab, the run's tabs closed at its end except a
                                 sign-in page: cdp_mux._SIGN_IN).
                                 The shared-screen facts (no take-control/Done, autosave, fresh snapshot after a
                                 person's input, sign-in by the person or the vault) must match both.
  - New session (small form with Start, no first message, web and desktop only; iPhone still asks) ... references/surfaces.md ("New session" paragraph)
                              <- apps/frontend/src/components/messaging/NewSessionButton.tsx;
                                 apps/desktop/src-tauri/src/session_start.rs;
                                 apps/mobile/src/app/(app)/session/new.tsx (still has the first message)
  - Open saved CLI conversation (owner desktop restores automatically; search hits stay on history) ... references/surfaces.md ("Opening your saved CLI conversation" paragraph)
                              <- apps/frontend/src/components/messaging/LocalSessionPanel.tsx + SessionResume.tsx;
                                 packages/shared/src/messaging/sessionControls.ts;
                                 apps/desktop/src-tauri/src/local_sessions/transport.rs (resume idle grace)
  - Open in a terminal (owner only, app sessions, starts a forked copy) ... references/surfaces.md ("Open in a terminal" paragraph)
                              <- apps/backend/core/services/desktop_session_start.py (open route);
                                 apps/frontend/src/components/messaging/LocalSessionPanel.tsx;
                                 apps/desktop/src-tauri/src/session_start.rs + resume.rs
  - Session browser (shared Claude/Codex sessions browse in the Lightbulb browser) ... references/surfaces.md ("Session browser" paragraph)
                              <- apps/desktop/src-tauri/src/session_browser.rs (port 39222, peer process, offers);
                                 apps/desktop/src-tauri/src/local_sessions/native_mcp.rs (lightbulb-browser entry);
                                 apps/backend/core/services/session_browser.py (lease, sync, refusals);
                                 apps/infra/hermes/bridge/browser_control.py (attach_session: the cloud path); apps/backend/core/services/mac_cdp.py (use "session" door);
                                 packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md ("Browsing");
                                 apps/frontend/src/components/messaging/SessionBrowserTab.tsx + LocalSessionPanel.tsx (the tab);
                                 BrowserFrameView.tsx (session mode); routes /messaging/sessions/{id}/browser/{viewing,input}
                                 the engine's `lightbulb` MCP (use_browser, signed by the computer); apps/backend/core/services/session_browser.py (place_by_native);
                                 routers/messaging.py (/local-sessions/device/sessions/browser-place, computer_person)
  - Browser tool choice ..... SKILL.md ("references/surfaces.md § Browser" row, a pointer only) + references/surfaces.md
                              (Browser row: "Which tool", extensions, recording, downloads) +
                              references/platform-primitives.md (pointer)
                              <- Hermes's own browser_cdp / browser_dialog / computer_use: the bridge points
                                 every CDP client at the run's loopback endpoint (browser_control.install,
                                 cdp_mux) and computer_use's driver at apps/infra/hermes/cua_driver.py (AWS
                                 InvokeBrowser; HERMES_CUA_DRIVER_CMD in bridge/__init__.py); managed overlay
                                 browser.allow_private_urls (chrome://, file:// etc.);
                                 IAM: bedrock_aip.ensure_owner_browser_policy + container-stack
                                 BoundaryAgentCoreBrowserUse (InvokeBrowser). Download dir and file:// facts
                                 were measured live 2026-09-27.
  - computer_use procedure .. references/surfaces.md (Browser row: "Operating computer_use")
                              <- cua_driver.py returns no elements (no element index, ax is empty);
                                 captures reach the model as images because hermes_profiles never
                                 pins auxiliary.vision, for any provider (else Hermes describes each)
  - Browser extensions ...... references/surfaces.md (Browser row: agent installs from the Web Store via
                              computer_use, the ask route that keeps it, member adds, the helper's Save-login bar)
                              <- apps/backend/core/services/browser_extensions.py (Web Store fetch, helper
                                 zip, the ask); routes in routers/container.py (/browser/extensions*,
                                 /browser/helper/*); apps/backend/browser_helper/ (the in-browser helper);
                                 the ask's Activity item in core/services/activity.py; frontend
                                 BrowserExtensionsSection + WebsiteLoginsSection (Connections, and the
                                 Browser panel bar popovers in BrowserDockPanel).
  - Agent inspector + Settings groups ... references/surfaces.md
                              <- apps/frontend/src/components/settings/settings-nav-config.tsx (SETTINGS_NAV_SECTIONS)
  - Paired Mac browser ...... references/surfaces.md ("Browser on a paired Mac": use_browser routing for
                              localhost/LAN/"my Mac", the "Lightbulb agent" window, show only for a login/2FA/CAPTCHA
                              ask, Paperclip asks in the issue thread; "Paired Mac tools" + "Local browser setup" rows)
                              + references/platform-primitives.md (lightbulb_desktop paragraph)
                              <- apps/infra/hermes/bridge/browser_control.py (select_mac/select_cloud, the Mac's
                                 sync + save before run.ended, the §7 close messages); bridge/cdp_mux.py (named
                                 upstreams, Lightbulb.humanInput, Lightbulb.show); bridge/browser_state.py
                                 (source "mac": cookies + localStorage via a Fetch-answered background tab, vetting); apps/backend/core/services/{mac_browser,mac_cdp}.py
                                 (MESSAGES are the agent's verbatim errors);
                                 apps/desktop/src-tauri/src/agent_chrome.rs;
                                 apps/frontend/src/components/desktop/LocalBrowserSetup.tsx.
  - Plugin without the app .. references/surfaces.md ("Claude Code or Codex without the Mac app")
                              <- packages/claude-plugin/README.md (install commands, Mac caveat);
                                 .github/workflows/sync-claude-plugin.yml (public repo TheLightbulbCompany/lightbulb-plugin).
  - Paired Mac execution .... references/platform-primitives.md
                              <- apps/infra/hermes/plugins/lightbulb/desktop.py; bridge/turn.py;
                                 apps/backend/routers/desktop_execution.py;
                                 apps/backend/core/services/{desktop_execution,desktop_pair,connection_service}.py;
                                 apps/frontend/src/components/desktop/{DesktopPermissionsGate,NodeStatusPill}.tsx.
  - Other rooms (read) ...... references/platform-primitives.md ("Other rooms") + SKILL.md (routing row,
                              "A DM you read stays private" non-negotiable)
                              <- apps/infra/hermes/plugins/lightbulb/__init__.py (lightbulb_read_rooms);
                                 apps/backend/routers/messaging.py (agent_reader, /messaging/agent-read/*);
                                 apps/backend/core/services/hosted_mcp.py (rooms_for, messages_for: the MCP's own reads);
                                 apps/backend/core/services/local_session_grants.py (the requester);
                                 apps/backend/core/services/local_sessions.py (require_caller).
  - Shared local sessions ... references/platform-primitives.md + references/surfaces.md
                              <- apps/infra/hermes/bridge/ + apps/infra/hermes/plugins/lightbulb/__init__.py
                                 (lightbulb_local_sessions); apps/backend/core/services/{local_session_grants,local_sessions,local_session_relay,openclaw_work}.py;
                                 apps/backend/core/repositories/local_sessions_repo.py; apps/backend/routers/messaging.py;
                                 apps/desktop/src-tauri/src/local_sessions/{workspace,transport}.rs;
                                 apps/desktop/src-tauri/src/local_sessions/screen.rs (attach terminals);
                                 apps/desktop/src-tauri/src/local_sessions.rs (track);
                                 apps/frontend/src/components/messaging/{LocalSessionPanel,NativeSessionTranscript,RoomDirectory,SessionFocus,RoomComposer}.tsx;
                                 apps/frontend/src/components/messaging/SessionRemoveButton.tsx + local_sessions.remove_session ("Remove from list"; a live one is ended first, by an End marked `removesOnEnd`; a Claude background session by `claude stop`, local_sessions/transport.rs);
                                 local_sessions.ENDED_ON_PURPOSE / _without_moved + local_session_relay._ended + desktop session_states::end_reason ("Ended on purpose is not listed");
                                 packages/shared/src/messaging/mentions.ts (human session tokens);
                                 packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md; SKILL.md (sessions routing row).
                                 Optional adapter/tool availability requires the promoted Hermes image; docs are not deployment evidence.
  - Agent ask in the band ... references/surfaces.md (shared-sessions paragraph: "<Agent> wants to message this session: …" /
                              "… wants to stop this session"; Allow sends, Decline declines)
                              <- packages/claude-plugin/lightbulb/hooks/state.ts (requestLine);
                                 apps/desktop/src-tauri/src/local_sessions/{presence,mod_socket,transport}.rs (agentMessageAsks);
                                 apps/backend/core/services/session_presence.py (agent_message entries)
  - Session Details dialog .. references/surfaces.md (status line, Details tabs, Access switches, footer)
                              <- apps/frontend/src/components/messaging/LocalSessionPanel.tsx (status line, Details + Pick up
                                 buttons); SessionDetails.tsx (About/Changes/Access/Linked work tabs, the Access switches,
                                 footer Message owner / Pick up); SessionShareLink.tsx (Who can see this: Only me with its
                                 second click / My workspace / Anyone with the link, Copy link, the Let people continue
                                 this session switch); LocalSessionWork.tsx (Link existing work).
  - Session chat ............ references/surfaces.md ("Session chat" paragraph: hidden room, CEO on mentions,
                              answer in the chat, never into the session)
                              <- apps/backend/core/services/session_chat.py; apps/backend/routers/messaging.py (POST .../chat);
                                 apps/frontend/src/components/messaging/{SessionChat,SessionFocus}.tsx;
                                 apps/mobile/src/app/(app)/session/[sessionId].tsx (Chat button)
  - Session links + Continue  references/surfaces.md ("Session links" paragraph)
                              <- apps/frontend/src/app/s/page.tsx (the /s page: the one signed-out refusal that leads with sign in, the
                                 live terminal for an "anyone" link, the member redirect, Continue, the
                                 `lightbulb continue '<link>'` command); apps/backend/core/services/session_links.py
                                 (open, viewing: the link viewer's seat, copies, redaction); core/services/local_sessions.py
                                 (put_link: one link, audience, publicId); core/services/session_pickup.py (set_pickup: the
                                 continue switch); apps/infra/lambda/pub-authorizer; apps/desktop/src-tauri/src/session_continue.rs
                                 (`lightbulb continue`).
  - Session live vs transcript  references/surfaces.md (running = live terminal; a session inside an app (packages/shared/src/messaging/sessionStates.ts conversationOnly) = its conversation under sessionScreenNotice's one line, in LocalSessionPanel and on the phone; a running session with no live terminal (plain claude, codex --no-daemon) = its conversation (transcript, composer, approvals) for members on web, desktop and phone under one hint line; the public link stays terminal-only; ended = final screen while available, then saved conversation history; Continue for the owner, Resume for teammates; folder refusals link to Desktop settings; offline = "Mac offline" + Resume greyed + Pick up; one sharing switch per Mac, on at pairing, sets Share every new session with it (LocalSessions.tsx setEnabled); no transcript beside a live terminal; sidebar lists live, All sessions lists every session; no message box under a live terminal)
                              <- apps/desktop/src-tauri/src/holder/run.rs (ended mode), local_sessions/session_states.rs,
                                 apps/backend/core/services/{local_sessions,session_screen}.py (screenAvailable, terminalReason),
                                 apps/frontend/src/components/desktop/DesktopPermissionsGate.tsx (live default).
  - Session status/title/id .. references/surfaces.md (sessions list: waiting-approval = waits for its person; title = person's name else conversation title; one conversation = one sessionId)
                              <- apps/frontend/src/components/messaging/{LocalSessionPanel,RoomDirectory,SessionTerminalView}.tsx.
  - Session teammates/control references/surfaces.md ("Teammates and control" + "Ask to join from the public page")
                              <- apps/backend/core/services/local_sessions.py (may_control = owner, teamControl, allowedSenders;
                                 request_join refuses members; decide_join / remove_sender); core/services/session_links.py
                                 (ask_to_join, link viewers only, always on); core/services/native_session_tools.py (sendable);
                                 apps/frontend/src/components/messaging/LocalSessionPanel.tsx ("Only <owner> can type" line);
                                 SessionDetails.tsx (People you let in, Remove).
  - Session live terminal ... references/surfaces.md ("Live terminal" paragraph)
                              <- apps/frontend/src/components/messaging/{LocalSessionPanel,SessionTerminalView}.tsx
                                 (terminal in the panel, ended/offline/notice panels, no transcript, "<name> typing" + typist cursor, read-only for
                                 non-admitted); core/services/local_sessions.py (may_control / allowedSenders gate on input);
                                 core/services/session_screen.py (type_into / watch: active-membership fence);
                                 apps/desktop/src-tauri/src/holder/{run,panel}.rs (the panel joins a `lightbulb claude` terminal;
                                 `run::CLASSIC_RENDERER` / `run::CODEX_INLINE`: held Claude Code sessions run in the classic renderer and held Codex sessions inline, so a watcher scrolls back).
  - Which sessions are shared references/surfaces.md ("Which sessions are shared" paragraph)
                              <- apps/desktop/src-tauri/src/local_sessions.rs (track: every session, named to its holder when it has one);
                                 apps/desktop/src-tauri/src/local_sessions/enrollment.rs (every session's hook reaches the app);
                                 apps/desktop/src-tauri/src/local_sessions/{claude,codex}.rs (`job`: which plain sessions get a live terminal) +
                                 holder/manager.rs (`attach_argv`: `claude attach`, `codex resume --remote unix://`);
                                 apps/desktop/src-tauri/src/session_start.rs (terminal_start / start / cli_start);
                                 apps/desktop/src-tauri/src/cli.rs (`lightbulb claude|codex`);
                                 apps/frontend/src/components/messaging/StartSessionDialog.tsx;
                                 packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md.
  - Session resume .......... references/surfaces.md ("Resume" paragraph)
                              <- apps/backend/core/services/session_resume.py (who may resume, plan mode, 24 h, revoke);
                                 apps/desktop/src-tauri/src/local_sessions/resume.rs (same folder, approved now; refusal codes);
                                 packages/shared/src/messaging/sessionControls.ts (RESUME_REFUSED, "Not resumed: …").
  - Session pick-up ......... references/surfaces.md ("Pick up" paragraph)
                              <- apps/frontend/src/components/messaging/SessionPickup.tsx (Ask to pick up / Requested / declined / Preparing… / Pick up;
                                 desktop-only, browser copy); SessionShareLink.tsx (Let people continue this session is the
                                 pick-up switch: needs sharing on, 7-day retention, Codex not yet; a link alone only shows the session);
                                 core/services/local_sessions.py decide_pickup_request (Allow creates a Workspace only link).
  - Workspace search ........ references/surfaces.md
                              <- apps/frontend/src/components/workspace/WorkspaceHeader.tsx (search button + shortcut label);
                                 apps/frontend/src/components/teams/command-palette/CommandPalette.tsx (the Conversations group,
                                 rendered first); components/messaging/roomCommands.ts (useRoomCommandResults — rooms then
                                 people, degrades to [] via useMessagingWorkspaceOptional when no messaging provider is mounted,
                                 e.g. InlineTeamsPanel.tsx)
  - Workspace rail + Activity references/surfaces.md ("Workspace rail" and "Activity" rows)
                              <- apps/frontend/src/components/workspace/WorkspaceHeader.tsx (DESTINATIONS: rail order and labels);
                                 apps/frontend/src/components/workspace/panelRegistry.ts (teams-dashboard, teams-inbox,
                                 teams-approvals, teams-activity → activity aliases);
                                 apps/frontend/src/components/activity/activityPage.ts (For you: chips, Unread, one row per item,
                                 where/snippet) + ForYou.tsx (rows, Review for decisions, decline, infinite scroll) + TeamFeed.tsx
                                 (brief, posts, Discuss); apps/frontend/src/components/workspace/panels/ActivityDockPanel.tsx;
                                 apps/frontend/src/components/teams/panels/AgentsListPanel.tsx (Add an AI employee);
                                 apps/frontend/src/components/messaging/RoomDirectory.tsx (Invite someone row at the bottom of the chat sidebar)
  - Library ................. references/surfaces.md
                              <- apps/frontend/src/components/workspace/WorkspaceHeader.tsx (workspace-rail Library item);
                                 apps/frontend/src/components/workspace/panelRegistry.ts (teams-artifacts panel title);
                                 apps/frontend/src/components/teams/panels/ArtifactsPanel.tsx (Library surface)
                              What the Library lists (SKILL.md deliverable bullet + references/chat-vs-document.md intro +
                                 the paperclip skill's references/artifacts.md first paragraph) <- backend
                                 routers/teams/issues.py list_company_artifacts -> paperclip fork
                                 server/src/services/company-artifacts.ts (issue documents created/updated by an agent,
                                 minus SYSTEM_ISSUE_DOCUMENT_KEYS; work products; attachments; never comments or the
                                 description)
  - Mail ..................... references/surfaces.md
                              <- apps/frontend/src/components/workspace/WorkspaceHeader.tsx (Mail rail item + badge);
                                 apps/frontend/src/components/mail/ (MailPanel sections, chips, meeting detail, "new");
                                 apps/backend/routers/agent_mail.py (_meeting_view status/reason/notes_room_id)
  - Message actions ......... references/surfaces.md
                              <- apps/frontend/src/components/messaging/{RoomMessageItem,RoomMessageContent}.tsx (reactions,
                                 reply-in-thread, edit/delete, the menuExtras-gated "more" menu open to every member);
                                 RoomPanel.tsx renderMessage (pin/unpin against pinnedMessageIds, mark-unread-from-here
                                 gated `!inThread`); messageLinks.ts (copy-link permalink format);
                                 RoomPanel.tsx renderRun (muted "<Agent> finished · no reply" / "stopped" / "run failed"
                                 status line for a silent agent turn)
  - Conversation list ....... references/surfaces.md
                              <- apps/frontend/src/components/messaging/RoomDirectory.tsx (group order, Manage sections
                                 dialog, drag/keyboard section reorder, Leave confirm); RoomDirectoryRow.tsx (the ⋯ menu,
                                 Move to section); roomSections.ts (groupRooms — Starred · custom · Channels · Direct);
                                 sidebarPrefs.ts (per-browser collapse + sort)
  - Lightbulb channel ....... references/surfaces.md
                              <- apps/backend/core/services/messaging.py (ensure_vendor_room, on_org_member_joined,
                                 is_vendor_room / _deny_vendor_human_change); core/config.py VENDOR_ORG_ID;
                                 apps/frontend/src/components/messaging/{RoomDirectoryRow,RoomHeader,RoomPanel}.tsx (Shared marker, gates)
  - Composer ................ references/surfaces.md
                              <- apps/frontend/src/components/messaging/RoomComposer.tsx (typeaheads, formatting toolbar,
                                 drag-and-drop, up-arrow edit); composerText.ts (caret grammar); mentions.ts
                                 resolveTypedMentions (typed tokens resolve on send); respondHint.ts (must mirror
                                 core/services/messaging.py select_dispatch_targets); drafts.ts (lightbulb.messaging.draft| keys)
  - Room header ............. references/surfaces.md
                              <- apps/frontend/src/components/messaging/RoomHeader.tsx (topic edit, member facepile,
                                 star/mute, the pinned-count affordance, and the ⋯ overflow actions);
                                 RoomPanel.tsx (Add people picker, channel/chat authorization, DM-to-group conversion,
                                 current-member filtering, and Details tab pinned count); apps/mobile/src/components/room/
                                 RoomDetails.tsx + PickerSheet.tsx (mobile Add people flow); apps/backend/core/services/
                                 messaging.py (server authorization and in-place DM conversion); messageLinks.ts
                                 (roomPermalink copy-link format); RoomDetailsPanel.tsx (Members/Pins/Files/Links/About)
  - Inbox (retired) ......... references/surfaces.md ("Inbox (retired)" row) <- panelRegistry.ts RETIRED_PANEL_ALIASES
                              (teams-inbox/teams-approvals/teams-activity → activity); its items are For you's
  - Tasks groupings ......... apps/frontend/src/components/teams/issues/TasksList.tsx (TASKS_GROUP_BY / GROUP_OPTIONS);
                              panelRegistry.ts (the groupBy param; teams-goals/teams-projects are their own
                              TEAMS_LIST_KEYS panels, not aliases, since 2026-09-24)
                              <- apps/frontend/src/components/control/ControlSidebar.tsx (NAV_ITEMS)
  - Questions / secrets ..... references/surfaces.md
  - Integrations wiring ..... references/integrations.md
                              <- apps/backend/routers/integrations.py; core/services/hermes_profiles.py (translate_mcp_servers)
  - Starter team + roster ... references/company-and-plans.md
                              <- apps/backend/core/services/workspace_provisioning.py (STARTER_ROSTER + provision_org);
                                 core/services/{agent_provenance,paperclip_agent_lifecycle}.py;
                                 core/repositories/paperclip_repo.py (starter_agent_ids)
  - Starter channel first message + pre-typed replies + first-reply intake ... references/company-and-plans.md
                              <- apps/backend/core/services/messaging.py (default_room_welcome, FIRST_REPLIES);
                                 core/services/onboarding_brief.py (first_message_intake);
                                 apps/frontend/src/components/messaging/FirstReplyChips.tsx
  - The invite ask after the first run ... references/company-and-plans.md <- core/services/messaging.py ask_who_should_see (INVITE_ASK)
  - Plans / providers ....... references/company-and-plans.md (two selectable paths; BYO is legacy compatibility only)
                              <- apps/backend/core/services/{billing_service,provision_gate,owner_facts}.py;
                                 apps/backend/routers/{billing,settings_keys}.py; apps/frontend/src/lib/providerCards.ts
                                 (billing_service.create_workspace_checkout: confirm=True to change a live plan; prorated onto the next invoice)
  - Trial end (recap, reading-only, pause, 90 days) ... references/company-and-plans.md ("How a trial ends")
                              <- apps/backend/core/services/{trial_recap,trial_lifecycle}.py;
                                 docs/runbooks/pause-and-reading-only.md
  - Voice calls ............. references/company-and-plans.md ("Voice calls")
                              <- core/billing/voice_plan*.py + routers/billing.py (opt-in $250/1,500/$0.35 voice business,
                                 explicit workspace-bound admin terms/card setup, collected bounded entitlement,
                                 UTC scoped periods, confirmed period-end cancellation; provider/runtime gates share coverage)
                              <- apps/backend/core/billing/voice_billing.py (free 10 min/UTC day, credits mode, MIN_CALL_SECONDS);
                                 core/services/voice_calls.py (30 min cap, one free call at a time, no transcript);
                                 core/services/{voice_routing,voice_persona}.py (room-call routing; each agent speaks as itself in its own voice)
                                 routers/huddles.py, core/services/livekit_huddles.py, apps/huddle-agent/ (Live in every room kind; agents join only where one listens; addressed by name, "Lightbulb" or a tile)
  - Phone voice / Spanish / stock samples ... references/voice.md ("Phone voice and Spanish")
                              <- backend routers/{voice,voice_phone}.py + core/services/voice_preferences.py;
                                 apps/huddle-agent/src/huddle_agent/{phone,live}.py;
                                 frontend components/voice/PhoneVoiceSettings.tsx + native counterpart;
                                 packages/shared/src/voicePreferences.ts + frontend public/voice-samples/
  - Voice requests .......... references/voice.md + SKILL.md routing row
                              <- apps/infra/hermes/bridge/server.py + turn.py (a voice run is locked and scoped on the room
                                 session key, lightbulb_session_key, and recorded in the call's own transcript,
                                 call_session_key; the room session gets one call record per answered turn, question cut to
                                 300 characters and answer to 500, and a new call starts with the room's newest 10; frames
                                 relayed to the backend, never posted);
                                 plugins/lightbulb/__init__.py (VOICE_ROOM_REFUSAL in _send_room_message; the
                                 `lightbulb_voice_profile` tool: rename|add_note|replace_notes|clear_notes, VOICE_ONLY
                                 outside a voice run); scripts/lightbulb-post.sh (voice.md forbids it in a voice run;
                                 /agent-posts has no voice guard); backend POST /internal/agents/voice-profile/{owner}
                                 (409 name_taken, 429 rate_limited). Pinned by apps/infra/hermes/tests/test_voice_skill.py.
  - Buying things ........... references/money.md ("Buying things")
                              <- plugins/lightbulb/__init__.py (`request_purchase`, PURCHASE_SCHEMA); backend
                                 routers/company_link_wallet.py (agent door, refusal codes), core/services/purchases.py (policy),
                                 core/services/link_wallet.py. Every route 404s until Link's OAuth client is configured. Pinned by
                                 apps/infra/hermes/tests/test_plugin_request_purchase.py.
  - Budgets (monthly caps) .. references/money.md
                              <- apps/frontend Settings -> Budgets panel; routers/teams/budgets.py (BFF proxy, admin-gated writes);
                                 Paperclip budget engine (PATCH companies/agents budgets, GET budgets/overview). Real spend fed by
                                 core/services/paperclip_cost.py + openrouter-settlement report.
  - Software projects ....... references/software-projects.md + SKILL.md routing row
                              + the Engineering persona (workspace_provisioning.py STARTER_CAPABILITIES["engineering"])
                              <- core/services/credentials/providers.yaml (github scopes + gh_cli; vercel_cli paste_token +
                                 skill_cli/VERCEL_TOKEN; aws aws_sts tiers, PowerUserAccess = no IAM; cloudflare/sentry/posthog
                                 actor: mcp); credentials/skills/vercel/SKILL.md (the pinned npx form);
                                 apps/infra/hermes/bridge/sync.py (a save deletes from S3 what the agent deleted, M6-R51; HOME_EXCLUDE_GLOBS .gitconfig,
                                 _REPRODUCIBLE_DIRS node_modules/*venv/.next/dist/build, work/ of other conversations never
                                 restored, save at end of turn);
                                 apps/infra/hermes/Dockerfile (global npm installs as root, then USER 1000; no dockerd);
                                 Hermes's bundled github / requesting-code-review skills (/opt/hermes/skills, the base image);
                                 PyPI package graphifyy (its `graphify` entry point; `graphify query --graph`,
                                 `graphify update . --no-cluster` = AST only), mirrored by this repo's own
                                 .github/workflows/code-graph.yml;
                                 the review routine = skills-src/paperclip/references/routines.md (an agent may create a
                                 routine assigned to itself; `schedule` trigger) + references/platform-primitives.md
                                 (Automations: a GitHub trigger added from the routine's own detail panel, owner-side).
  - Skills (dirs/scope) ..... references/skills.md
                              <- apps/backend/core/services/hermes_profiles.py (SKILLS_DIR_LIGHTBULB,
                                 SKILLS_DIR_HERMES_BUNDLED, SKILLS_EXTERNAL_DIRS, the `skills` config block);
                                 scripts/reapply_hermes_skills_dirs.py; the image's /opt/hermes/tools/skills_tool.py
                                 (the Ambiguous-skill-name refusal);
                                 paperclip fork server/src/routes/agents.ts — listSkills exists for
                                 claude/codex/cursor/gemini/grok/opencode/pi but NOT hermes_gateway,
                                 which is why the company-skills route answers supported:false
  - Skills / self-learning .. references/skills.md
                              <- apps/frontend/src/components/control/panels/SkillsTab.tsx
  - Connected integrations / roster manifest
                              references/integrations.md ("What YOU already have connected") +
                              references/platform-primitives.md ("Teammates in a room")
                              <- core/services/owner_facts.py (`connected_integrations`, `teammates`;
                                 refreshed on connect/disconnect in routers/connections.py and on
                                 agent create/delete)
  - Connectable catalog ..... references/integrations.md
                              <- apps/backend/routers/platform_info.py (live GET /api/v1/platform-info);
                                 core/services/credentials/registry.py (all_providers);
                                 core/services/hermes_profiles.py (build_profile_env — the ISOL8_API_URL the runtime reads)
  - Microsoft 365 (Graph) ... references/integrations.md
                              <- apps/backend/core/services/credentials/providers.yaml (microsoft365 scopes);
                                 apps/backend/routers/runtime_rpc.py (runtime_credentials — the per-turn token key);
                                 bridge/turn.py (runtime_mcp_env — the MCP_<KEY>_ACCESS_TOKEN name)
  - Agent-started connect ... references/integrations.md
                              <- apps/backend/routers/connections.py (authorize_from_container — hook-token sibling of authorize;
                                 ISOL8_OWNER_ID + OPENCLAW_HOOK_TOKEN env in hermes_profiles.build_profile_env);
                                 /app/skills/lightbulb/scripts/lightbulb-connect.sh (the mint door — never an inline header, see the redaction note)
  - Connections door ........ references/integrations.md + references/surfaces.md + references/boundaries.md table
                              <- apps/frontend/src/components/control/panels/ConnectionsPanel.tsx (Settings → Connections) + components/settings/AgentGuardrails.tsx; apps/frontend/src/components/workspace/WorkspaceHeader.tsx (Settings gear; the rail has no Connections button)
  - Paperclip org model ..... references/company-and-plans.md
                              <- TheLightbulbCompany/paperclip fork: server/src/services/agent-permissions.ts,
                                 services/agents.ts (getChainOfCommand), adapters/registry.ts (hermes_gateway)
  - Launch competition ...... references/surfaces.md
                              <- apps/backend/routers/launch.py (/join refuses every org; /team-interest is the hand-raise);
                                 core/services/launch_{phase,settings,service,referral,close,revenue}.py;
                                 apps/frontend/src/components/launch/{SheetCard,TeamInterestForm}.tsx
  - Email (one shared team address) references/email.md
                              <- /app/skills/lightbulb/scripts/lightbulb-mail.sh + backend routers/agent_mail.py + core/services/agent_mail_send.py (one send door; policy table in its docstring)
  - Texts (the business's number) .. references/text.md
                              <- /app/skills/lightbulb/scripts/lightbulb-text.sh + backend routers/agent_text.py + routers/photon_inbound.py
  - Front desk (customers by text, OpenCLI adapters) references/front-desk.md
                              <- core/services/hermes_profiles.FRONT_DESK_SOUL + core/services/onboarding_brief.FRONT_DESK_BUILD_PROMPT + references/text.md
  - Welcome mails as the CEO .. references/email.md ("Sending")
                              <- backend core/services/welcome_email.py (owner: user_provision after #general;
                                 teammate: routers/webhooks.py membership.created) + agent_mail_owner_send.send_to_member
  - Meetings (invite the team address) references/email.md ("Meetings")
                              <- backend core/services/meeting_invites.py (handle_team_mail: member's Meet invite -> hidden room with every agent, RSVP, bot; output_room; advance for series),
                                 meeting_bots.py (bot_name + chat event subscription), routers/meetings.py (Recall status, endedAt, roster relay + signed chat),
                                 core/services/meeting_chat.py + plugins/lightbulb/router.py (meeting_chat tool; occurrence-scoped receipts);
                                 core/services/meeting_context.py + routers/runtime_internal.py (read-only meeting_context pages; immutable original principal and occurrence), meeting_notes.py (transcript + notes run to the
                                 default room by the CEO or a fallback agent in it, notesSkipped, app-only notes and occurrence history), voice_calls.post_late_answer (a Meet's unheard
                                 answers -> meeting.unsaid); who an agent answers in a Meet: apps/huddle-agent huddle.py (_mine, MEET_FOLLOWUP_S)
  - Teammates in a room ..... references/platform-primitives.md ("Teammates in a room") + SKILL.md non-negotiable + routing row
                              <- backend core/services/messaging.py (AGENT_REPLY_CAP, the hop on create_agent_message: human 0,
                                 run reply = trigger + 1, no-run post = 1; capped messages get no dispatch targets, mentions
                                 included, and one system "… paused after N replies" notice) + openclaw_work.py (Jev
                                 fallback wakes nobody on an agent-authored message); design 2026-09-25 agent conversation
  - Post outside a turn ..... references/platform-primitives.md ("Post outside a room turn") + SKILL.md routing row
                              <- /app/skills/lightbulb/scripts/lightbulb-post.sh + backend routers/messaging.py (post_agent_post,
                                 /internal/messaging/agent-posts; the GET agent-messages/.../receipts/{eventId} pre-check is what prints "Already posted:") + core/services/messaging.py (resolve_agent_post_room — the
                                 room > user/handle > issue creator > #general order; then create_agent_message)
  - Feed (Activity) ......... references/feed.md + SKILL.md routing row
                              <- backend core/services/messaging.py (FEED_DEFAULT_TOPIC, FEED_WELCOME, ensure_feed_room,
                                 agent_feed + routers/messaging.py GET /messaging/feed/u/{owner} = the "read first" call
                                 and its 20-post window FEED_AGENT_READ_LIMIT, AgentPostRequest.feed/post_reason and
                                 FEED_POST_REASON_MAX_CHARS = the 280-char --why cap); lightbulb-post.sh --feed/--why;
                                 the schedule (08:00/13:00/18:00, owner's users.timezone else UTC, CEO-assigned "Feed post
                                 HH:00" routines) is core/services/user_provision.py FEED_ROUTINE_HOURS /
                                 FEED_ROUTINE_DESCRIPTION / _ensure_feed_routines; no push for feed posts is
                                 apps/infra/lambda/messaging-projector/index.py _is_feed_room. Agents are the feed's
                                 authors, never members: messaging.room_agent (roster stand-in) and the feed rule in
                                 select_dispatch_targets (a Discuss @mention wakes that agent). Spec 2026-10-14 §4.2.
  - Chat vs document ........ references/chat-vs-document.md + SKILL.md (non-negotiable + routing row)
                              First-run result contract ("The first run" section, pointed at from SKILL.md's
                                 deliverable bullet and company-and-plans.md, because the first room turn can run
                                 before fulfilment stages BRIEF.md) mirrors
                                 apps/backend/core/services/onboarding_brief.py _BRIEF_TEMPLATE; edit both together.
                                 The connect link (also references/integrations.md) is the app-link kind `connect` in
                                 apps/frontend/src/components/messaging/appLinks.ts, rendered by RoomAppLinks.tsx with ConnectButton.
                              <- paperclip fork server/src/routes/issues.ts (PUT /issues/:id/documents/:key:
                                 assertAgentIssueMutationAllowed — own/unassigned idle issue, no run id needed;
                                 lockedDocumentStrategy create_new_document, hence "link the returned key");
                                 packages/shared/src/validators/issue.ts issueDocumentKeySchema;
                                 apps/frontend/src/components/messaging/RoomDocumentCards.tsx +
                                 packages/shared/src/messaging/documentLinks.ts (the card the link becomes);
                                 plugins/lightbulb/__init__.py OWN_THREAD_REFUSAL (why no mid-turn acknowledgement)
  - Cards in chat ........... references/ui.md
                              <- packages/shared/src/ui/library.ts (the vocabulary; the signatures block is GENERATED and pinned by
                                 packages/shared/src/ui/reference.test.ts: refresh with
                                 `OPENUI_REFERENCE_WRITE=1 pnpm -F @lightbulb/shared exec vitest run src/ui/reference.test.ts`),
                                 apps/frontend/src/components/messaging/openui/ (web renderers, press -> reply),
                                 apps/mobile/src/components/chat/openui/ (phone; lands with the mobile PR). SKILL.md: the "Ask with a card"
                                 non-negotiable and "How you reply in chat" (docs/research/2026-10-13-employee-voice-and-consumer-agents.md rung 1).
  - Workspace actions ....... SKILL.md (routing row + "Lightbulb's own data changes only through Lightbulb tools" non-negotiable)
                              + references/workspace.md (the action list and each action's args)
                              <- apps/infra/hermes/plugins/lightbulb/__init__.py (`lightbulb_workspace`) -> apps/backend/routers/messaging.py
                                 AGENT_ACTIONS / agent_actor (acts as the turn's requester; no person = refused). Args mirror
                                 UpsertRoomMemberRequest/CreateRoomRequest/PatchRoomRequest (core/services/messaging.py): edit both together.
                                 Lesson 2026-10-18: a hand-written member row skipped the delivery cursor and silenced an agent.
                                 lightbulb-api.sh's owner-only refusal points room/member actions at the tool.
  - Writing a document ...... references/documents.md
                              <- owner complaint 2026-10-16 (a GTM document opened with "Observed … UTC",
                                 "Decision", "Current-state audit" and task-id bullets); the pane header now carries
                                 title/author/date/version so the body never repeats them; cards via references/ui.md.
  - Hook-token calls ........ SKILL.md (the non-negotiable) + references/money.md (the status tables)
                              <- /app/skills/lightbulb/scripts/lightbulb-api.sh — every OPENCLAW_HOOK_TOKEN call goes through a script on disk;
                                 apps/infra/hermes/plugins/lightbulb/__init__.py (_register_redaction) masks that header including $VAR refs,
                                 so an agent-typed curl poisons its own next turn (401 loop)
                              <- apps/infra/hermes/bridge/ (the run payload's message {id, threadRootMessageId}) and
                                 apps/infra/hermes/plugins/lightbulb/__init__.py (send_room_message's threadRootMessageId);
                                 paperclip skill references/monitors.md;
                                 spec docs/superpowers/specs/2026-09-16-proactive-agents-design.md (delete when shipped).
- Roster + connected integrations routing row ("Who is on the team?" / "What's connected?") — `references/boundaries.md` Quick answers; source: `core/services/owner_facts.py` (`teammates`, `connected_integrations`, #3851). Added 2026-10-08 after a dev check where the CEO answered a roster question from the room's member list.
- Ask and count rules (SKILL.md, the two lines after "A person's decision is an approval in Needs you") — tool `ask_person` in `apps/infra/hermes/plugins/lightbulb/__init__.py`; the paperclip skill's `references/interactions.md` "When you need an answer, not a sign-off: ask_person". Added 2026-10-30 from G1's failures (comments nobody read; "3 open cards" with 55 open). The count rule points at the paperclip skill's list route; the `lightbulb_board` wrapper was removed 30 Oct (same route, same key). Test: `tests/test_ask_and_count_skill.py`.
- Task handoffs (super-agent only) — `references/tasks.md`, deliberately NOT in SKILL.md's routing table: only a super-agent Worker run loads it, because its instructions name it (`WORKER_INSTRUCTIONS` in `apps/backend/core/temporal/activities.py`). Every other run keeps the Paperclip handoffs in platform-primitives.md, company-and-plans.md, software-projects.md. Added 2026-11-05 after #4760 rewrote the global text (skills are fleet-wide). Test: `apps/backend/tests/unit/super_agent/orchestration/test_task_board_signals.py::test_task_wording_is_super_agent_only`.

- Voice Settings/provider confirmation — references/surfaces.md and references/company-and-plans.md; anchors: VoiceSettings.tsx, chat/page.tsx, ProvisioningStepper.ProviderPicker and ChatGPTOAuthStep.tsx. Authentication and explicit provider confirmation are separate; cancellation preserves the saved provider.
