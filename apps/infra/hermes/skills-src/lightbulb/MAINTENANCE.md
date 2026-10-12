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
(`send_room_message`, `hire_suggestion`, `lightbulb_read_rooms`, `lightbulb_local_sessions` — its direct-or-ask
send rule is `agent_command` in `apps/backend/core/services/local_sessions.py`).

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
                                 the image's gog binary (gmail settings watch = Pub/Sub)
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
                              ONE worktree, removed at task end, installs only in small projects, none for a large repo (sentence identical in SKILL.md, software-projects.md and turn.py `_repo_rule`)
                                 (SKILL.md scripts bullet + software-projects.md "Keep the workspace small"):
                                 .scratch/ and /tmp share the microVM's ~4 GB disk across teammates until it is
                                 recycled; numbered slots filled it twice for the founder's engineering agent
                                 (2026-10-07/08). Mirrors bridge/turn.py `_repo_rule`; edit both together.
  - gog API-not-enabled ..... references/integrations.md ("Google Workspace (`gog`)": quote the error and name
                              the API to enable before any browser workaround)
                              <- owner request 2026-10-05. No code enforces it (Google is BYO: the Cloud
                                 project and its enabled APIs are the owner's, so only they can fix it).
  - Plain words, no plumbing  SKILL.md ("Talk to people in plain words, never plumbing")
                              <- observed prod 2026-09-29: an agent told #general "PAPERCLIP_RUN_ID is empty (this is
                                 a webchat session, not a heartbeat)". No code enforces it; the verifier footer that
                                 leaked the same class of text is off in apps/infra/hermes/managed/config.yaml
                                 (display.file_mutation_verifier).
  - App host for links ...... SKILL.md ("Links: use this workspace's own app host")
                              <- core/services/owner_facts.py (`app_url` = settings.FRONTEND_URL)
  - Workspace memory ........ references/platform-primitives.md ("Queryable company state" row)
                              <- apps/infra/hermes/plugins/memory/agentcore/__init__.py (one /owners/<owner>/
                                 namespace for every agent); bridge/sync.py normalize_profile_backends
                                 (on only while AGENTCORE_MEMORY_ID is set); ~2 min = the memory
                                 strategy's async extraction lag, not a code constant.
                              Scopes + review: DM = private to that person (phase 1), other rooms = team;
                                 "Confirmed by a person:" <- core/services/workspace_memory.CONFIRMED_PREFIX;
                                 people review/delete/Share <- routers/memory.py, frontend AgentMemoryTab
                                 (Inspect agent -> Memory tab, references/surfaces.md "Control Panel" row), mobile
                                 MemoriesCard; the agent cannot delete (no tool), MEMORY.md is editable in
                                 AgentInstructionsTab.
  - Website sign-ins .......... references/boundaries.md ("Type a website password" row; names the web AND iPhone doors) + references/surfaces.md (Browser row)
                              <- apps/infra/hermes/bridge/website_logins.py (vault store + ask callbacks);
                                 core/services/website_logins.py; frontend WebsiteLoginsSection (Connections).
                                 Codes/CAPTCHA are browser-only (owner ruling 2026-10-07): the bridge's
                                 _code_prompt posts the line and watches the page; there is no code form.
  - Background subagents .... references/surfaces.md ("Background subagents" row) <- core/services/subagents.py + packages/shared/src/messaging/subagents.ts
  - Members / join link ..... references/surfaces.md ("Members & join link" row)
                              <- routers/orgs.py (one link per workspace; admin-only mint/copy/revoke;
                                 open join as org:member); frontend WorkspaceMembersSection (Settings →
                                 Members), workspace/InvitePeoplePanel + InvitePeopleDialog (email invites, pending/revoke,
                                 rail Invite), app/onboarding JoinTeamBlock (paste a join link), and
                                 MessagingWorkspaceContext.copyJoinLink (channel/group-chat Members section, not DMs).
  - Browser live view ....... references/surfaces.md
                              <- Mac viewer input: apps/frontend/src/components/workspace/panels/BrowserFrameView.tsx
                                 (event producer); apps/backend/routers/container.py and
                                 apps/backend/core/services/mac_live.py (room membership and routing);
                                 apps/desktop/src-tauri/src/agent_cast.rs (input and human-yield signal).
                              <- apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx;
                                 apps/backend/core/services/browser_sessions.py; apps/backend/core/services/browser_control.py; apps/infra/hermes/Dockerfile (agent-browser pin);
                                 apps/infra/hermes/bridge/browser_control.py (shared-screen yield; one lease for
                                 the bridge, released ~30 s after the last run browses) + bridge/cdp_mux.py (one
                                 AWS connection, each run its own tab, the run's tabs closed at its end).
                                 The shared-screen facts (no take-control/Done, autosave, fresh snapshot after a
                                 person's input, sign-in by the person or the vault) must match both.
  - New session (small form with Start, no first message, web and desktop only; iPhone still asks) ... references/surfaces.md ("New session" paragraph)
                              <- apps/frontend/src/components/messaging/NewSessionButton.tsx;
                                 apps/desktop/src-tauri/src/session_start.rs;
                                 apps/mobile/src/app/(app)/session/new.tsx (still has the first message)
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
  - Control Panel tabs ...... references/surfaces.md
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
  - Session live vs transcript  references/surfaces.md (running = live terminal; a session inside an app (packages/shared/src/messaging/sessionStates.ts conversationOnly) = its conversation under sessionScreenNotice's one line, in LocalSessionPanel and on the phone; a running session with no live terminal (plain claude, codex --no-daemon) = its conversation (transcript, composer, approvals) for members on web, desktop and phone under one hint line; the public link stays terminal-only; ended = final screen + "Ended · <time>" + Resume while the owner's app runs, then "screen is gone"; offline = "Mac offline" + Resume greyed + Pick up; Share every new session off at pairing; no transcript beside a live terminal; sidebar lists live, All sessions lists every session; no message box under a live terminal)
                              <- apps/desktop/src-tauri/src/holder/run.rs (ended mode), local_sessions/session_states.rs,
                                 apps/backend/core/services/{local_sessions,session_screen}.py (screenAvailable, terminalReason),
                                 apps/frontend/src/components/desktop/DesktopPermissionsGate.tsx (live default).
  - Session status/title/id .. references/surfaces.md (sessions list: waiting-approval = waits for its person; title = person's name else conversation title; one conversation = one sessionId)
                              <- apps/frontend/src/components/messaging/{LocalSessionPanel,RoomDirectory,SessionTerminalView}.tsx.
  - Session ask to join ..... references/surfaces.md ("Ask to join" paragraph)
                              <- apps/backend/core/services/local_sessions.py (request_join / decide_join / remove_sender,
                                 allowedSenders + joinRequests, may_control; ASK_AGAIN_AFTER_S = the ten-minute Ask again
                                 window, mirrored by ASK_AGAIN_AFTER_MS + joinRequestView in
                                 packages/shared/src/messaging/sessionControls.ts: no time-out, reminder, "closed before" from
                                 closed_joins / joinClosed -> joinRequestClosed, said for JOIN_CLOSED_TTL_S); core/services/activity.py (join_request item,
                                 Allow / Decline); core/services/local_session_relay.py (cleared on Ended and on a new
                                 generation); apps/frontend/src/components/messaging/LocalSessionPanel.tsx (Ask to join /
                                 Requested / declined line); SessionDetails.tsx (People you let in, Remove).
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
  - Library ................. references/surfaces.md
                              <- apps/frontend/src/components/workspace/WorkspaceHeader.tsx (workspace-rail Library item);
                                 apps/frontend/src/components/workspace/panelRegistry.ts (teams-artifacts panel title);
                                 apps/frontend/src/components/teams/panels/ArtifactsPanel.tsx (Library surface)
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
  - Inbox page .............. apps/frontend/src/components/teams/inbox/InboxPage.tsx (the three bands);
                              InboxNeedsYou/InboxHappeningNow/InboxRecently.tsx;
                              apps/frontend/src/components/workspace/panelRegistry.ts (INBOX_SECTIONS + the teams-activity alias)
  - Tasks groupings ......... apps/frontend/src/components/teams/issues/TasksList.tsx (TASKS_GROUP_BY / GROUP_OPTIONS);
                              panelRegistry.ts (the groupBy param; teams-goals/teams-projects are their own
                              TEAMS_LIST_KEYS panels, not aliases, since 2026-09-24)
                              <- apps/frontend/src/components/control/ControlSidebar.tsx (NAV_ITEMS)
  - Questions / secrets ..... references/surfaces.md
  - Integrations wiring ..... references/integrations.md
                              <- apps/backend/routers/integrations.py; core/services/hermes_profiles.py (translate_mcp_servers)
  - Marketplace flows ....... references/marketplace.md
                              <- apps/backend/routers/{marketplace_listings,marketplace_import}.py; core/services/{marketplace_service,agent_export,catalog_service,claude_code_import}.py
                                 routers/marketplace_purchases.py (checkout: seller cannot buy own listing)
  - Starter team + roster ... references/company-and-plans.md
                              <- apps/backend/core/services/paperclip_provisioning.py (STARTER_ROSTER + provision_org);
                                 core/services/{agent_provenance,paperclip_agent_lifecycle}.py;
                                 core/repositories/paperclip_repo.py (starter_agent_ids)
  - Starter channel first message + pre-typed replies + first-reply intake ... references/company-and-plans.md
                              <- apps/backend/core/services/messaging.py (default_room_welcome, FIRST_REPLIES);
                                 core/services/onboarding_brief.py (first_message_intake);
                                 apps/frontend/src/components/messaging/FirstReplyChips.tsx
  - Plans / providers ....... references/company-and-plans.md
                              <- apps/backend/core/services/{billing_service,provision_gate,owner_facts}.py
                                 (billing_service.create_workspace_checkout: confirm=True to change a live plan; prorated onto the next invoice)
  - Voice calls ............. references/company-and-plans.md ("Voice calls")
                              <- apps/backend/core/billing/voice_billing.py (free 10 min/UTC day, credits mode, MIN_CALL_SECONDS);
                                 core/services/voice_calls.py (30 min cap, one free call at a time, no transcript);
                                 core/services/{voice_routing,voice_persona}.py (room-call routing; each agent speaks as itself in its own voice)
                                 routers/huddles.py, core/services/livekit_huddles.py, apps/huddle-agent/ (Live in every room kind; agents join only where one listens; addressed by name, "Lightbulb" or a tile)
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
  - Provisioning rail ....... references/money.md
                              <- apps/backend/routers/company_provisioning.py (container routes, refusal codes);
                                 core/services/provisioning_workflow.py (STATES); core/services/owner_facts.py (the `provisioning` facts list)
  - Software projects ....... references/software-projects.md + SKILL.md routing row
                              + the Engineering persona (paperclip_provisioning.py STARTER_CAPABILITIES["engineering"])
                              <- core/services/credentials/providers.yaml (github scopes + gh_cli; vercel_cli paste_token +
                                 skill_cli/VERCEL_TOKEN; aws aws_sts tiers, PowerUserAccess = no IAM; cloudflare/sentry/posthog
                                 actor: mcp); credentials/skills/vercel/SKILL.md (the pinned npx form);
                                 apps/infra/hermes/bridge/sync.py (a save deletes from S3 what the agent deleted, M6-R51; HOME_EXCLUDE_GLOBS .gitconfig,
                                 _REPRODUCIBLE_DIRS node_modules/*venv/.next/dist/build, work/ of other conversations never
                                 restored, save at end of turn);
                                 apps/infra/hermes/Dockerfile (global npm installs as root, then USER 1000; no dockerd);
                                 the provisioning-rail fallback = core/config.py STRIPE_PROVISIONING_ENABLED +
                                 routers/company_provisioning.py container_catalog (`provisioning_unavailable`);
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
  - Skills / self-learning .. references/marketplace.md
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
                              <- apps/frontend/src/components/control/panels/ConnectionsPanel.tsx (rail panel); apps/frontend/src/components/workspace/WorkspaceHeader.tsx (rail label)
  - Paperclip org model ..... references/company-and-plans.md
                              <- TheLightbulbCompany/paperclip fork: server/src/services/agent-permissions.ts,
                                 services/agents.ts (getChainOfCommand), adapters/registry.ts (hermes_gateway)
  - Launch competition ...... references/surfaces.md
                              <- apps/backend/routers/launch.py (/join refuses every org; /team-interest is the hand-raise);
                                 core/services/launch_{phase,settings,service,referral,close,revenue}.py;
                                 apps/frontend/src/components/launch/{SheetCard,TeamInterestForm}.tsx
  - Email (one shared team address) references/email.md
                              <- /app/skills/lightbulb/scripts/lightbulb-mail.sh + backend routers/agent_mail.py + core/services/agent_mail_send.py (one send door; policy table in its docstring)
  - Meetings (invite the team address) references/email.md ("Meetings")
                              <- backend core/services/meeting_invites.py (handle_team_mail: member's Meet invite -> hidden room with every agent, RSVP, bot; output_room; advance for series),
                                 meeting_bots.py (bot_name), routers/meetings.py (Recall status, endedAt, roster relay), meeting_notes.py (transcript + notes run to the
                                 default room by the CEO or a fallback agent in it, notesSkipped, organizer mail), voice_calls.post_late_answer (a Meet's unheard
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
  - Chat vs document ........ references/chat-vs-document.md + SKILL.md (non-negotiable + routing row)
                              <- paperclip fork server/src/routes/issues.ts (PUT /issues/:id/documents/:key:
                                 assertAgentIssueMutationAllowed — own/unassigned idle issue, no run id needed;
                                 lockedDocumentStrategy create_new_document, hence "link the returned key");
                                 packages/shared/src/validators/issue.ts issueDocumentKeySchema;
                                 apps/frontend/src/components/messaging/RoomDocumentCards.tsx +
                                 packages/shared/src/messaging/documentLinks.ts (the card the link becomes);
                                 plugins/lightbulb/__init__.py OWN_THREAD_REFUSAL (why no mid-turn acknowledgement)
  - Hook-token calls ........ SKILL.md (the non-negotiable) + references/money.md (the status tables)
                              <- /app/skills/lightbulb/scripts/lightbulb-api.sh — every OPENCLAW_HOOK_TOKEN call goes through a script on disk;
                                 apps/infra/hermes/plugins/lightbulb/__init__.py (_register_redaction) masks that header including $VAR refs,
                                 so an agent-typed curl poisons its own next turn (401 loop)
                              <- apps/infra/hermes/bridge/ (the run payload's message {id, threadRootMessageId}) and
                                 apps/infra/hermes/plugins/lightbulb/__init__.py (send_room_message's threadRootMessageId);
                                 paperclip skill references/monitors.md;
                                 spec docs/superpowers/specs/2026-09-16-proactive-agents-design.md (delete when shipped).
- Roster + connected integrations routing row ("Who is on the team?" / "What's connected?") — `references/boundaries.md` Quick answers; source: `core/services/owner_facts.py` (`teammates`, `connected_integrations`, #3851). Added 2026-10-08 after a dev check where the CEO answered a roster question from the room's member list.
