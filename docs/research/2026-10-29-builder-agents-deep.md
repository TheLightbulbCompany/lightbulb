# How the other builders' agents work, and what their UX hides

Research note, 29 Oct 2026, for Lightbulb Apps (an agent in a chat room builds and hosts an app for a person). Products: Bolt.new and open-source bolt.diy, Replit Agent, v0, Base44, Anything (formerly Create.xyz). Lovable gets one-line comparisons only (see `2026-10-29-how-lovable-builds.md`).

This note goes deeper than `2026-10-29-app-builder-workflows.md` (stages, template pickers, ownership) and `2026-10-27-small-software-stack-teardown.md` (stack, pricing, hosting) and does not repeat them.

**Tags.** [P] primary: vendor docs, vendor source code, vendor release notes. [S] secondary: engineering blogs, talks, press, third-party write-ups. [U] unverified, including every line from leaked prompts. Each tag carries a source key (e.g. `[P B4]`), and each product section ends with its source list: URL, fetched 2026-10-29. Tools used: WebSearch, WebFetch, `gh`/raw GitHub, curl of public docs `.md`/`llms.txt`. No browser. Reddit and X were not reachable.

---

## 1. Bolt.new and bolt.diy

Bolt.new is StackBlitz's hosted product. bolt.diy is the open-source fork, now maintained under `blueberry-llm`, with its source on GitHub. The hosted agent's code is closed. bolt.diy shows how a Bolt-style agent is built today, and the help center shows what the hosted product's users see. Do not assume the two agents are identical.

### 1.1 The person's loop

- **First prompt.** Type in the homepage chatbox, then click **Build now**. Options: the **Plan** toggle (turns blue), the agent picker (**Standard**, **Max** on paid plans, **Bolt Forge** for open-weight models), and "Enhance your prompt", which "lets Bolt ask you clarifying questions" [P B1][P B2][P B3]. The QuickStart prompt (a pet-name app with sign-up and favorites) takes "approximately five minutes" and used about 204K tokens. Free is 1M tokens a month with a 300K daily cap [P B1].
- **Database without asking.** "Bolt creates a database automatically if your project needs one... You don't need to set anything up manually." To stop it you write "Don't use a database, I want to use local storage" [P B4]. The person is not asked about tables. The database icon shows them afterwards [P B1].
- **Preview and iterate.** The preview runs in a WebContainer in the person's own browser tab, and Chromium is needed [P B1]. To iterate you use the **Select** tool (click an element, then prompt), visual edits, preview **comments** that "Editors and Co-owners can select... and send them to Bolt as a prompt" with pin context (5–11 Oct 2026), and Code view, where edits cost no tokens. "version history doesn't cover those changes" [P B1][P B5].
- **Publish.** Click **Publish**, then **Publish** again. Wait "about a minute", then follow the link in chat to `*.bolt.host`. Paid plans can publish **Private**, with **Manage access** for emailed viewers and **Trusted domains** [P B6]. **Run security audit** sits in the Publish menu and changes to **Security audit up to date** when done [P B7].
- **Share.** Collaborator roles; private sites by email or domain allow-list [P B6]. On mobile "a preview card now appears in the chat after each build" (Oct 2026) [P B5].

### 1.2 The agent's loop and tools

**Hosted Bolt (from docs).** Tools are undocumented. Plan Mode reads the codebase, does web research and ends in buttons **Implement this plan** / **Show an example** / **Refine this idea** [P B2]. Skills apply when the prompt matches their description [P B8]. MCP connectors add to token use [P B9].

**bolt.diy (from source, `main` on 2026-10-29).** Two protocols live side by side.

1. **Legacy XML protocol.** `<boltArtifact>` containing `<boltAction type="file|shell|start|build|supabase">`, whole files only ("Write full code; no partial/diff updates") [P B10][P B11][P B12].
2. **Native tool protocol (current default when `projectTools` is on).** `prompt-library.ts` routes the default prompt to `getProjectToolPrompt` [P B13]. The tools: `project_read_file`, `project_write_file`, `project_patch_file`, `project_delete_path`, `project_create_directory`, `project_list_paths`, `project_search_content` (ripgrep in a worker), `project_find_related_files`, `project_run_command`, `project_get_process_output`, `project_stop_process`, plus `web_search`/`web_fetch` [P B14][P B15]. The workstyle prompt says "Do not emit <boltArtifact> or <boltAction> tags for new work" [P B16].

**Edit protocol (bolt.diy).** Optimistic concurrency. `project_read_file` returns a `revision`. `project_patch_file` takes `expectedRevision`, `oldText` (which must match exactly once) and `newText`. "If patching fails with a conflict, read the file again." A full rewrite through `project_write_file` needs a `replacementReason`. "Invalid JS/TS syntax is rejected without changing the file" [P B13][P B16]. `run_command` takes a `purpose` of run, test, install or other, and "Commands require user approval" [P B14].

**The loop is a state machine.** This is the deepest finding in this section. `app/lib/common/agentLoop.ts` and `.server/llm/agent-loop.ts` add two server-side tools, `agent_plan` and `agent_complete` [P B17][P B18]:
- `agent_plan` requires a goal, 1–6 steps, 1–8 criteria with stable IDs, and `requiresPreview` [P B18].
- On every step, `prepareStep` re-derives progress from the tool history: plan? edited? ran (a `purpose:"run"` exit 0 *after* the last edit, or `previewReady`)? tested (a `purpose:"test"` exit 0 after the last edit *and* the last failure)? It then **forces the next tool**. With no plan, `toolChoice` is forced to `agent_plan`. While repair is needed, only read/edit/search tools are active. Otherwise `toolChoice: 'required'`, so the model cannot answer in prose [P B18].
- The injected instruction is literally: "Follow Plan -> edit -> run -> test -> repair… NEXT REQUIRED ACTION: … Do not send a prose response while this action is pending. Remaining task steps: N" [P B17].
- `agent_complete` is **rejected by the server** unless a plan exists, the app ran, a check passed after the latest edit, the preview is ready if required, there are no pending calls, and every planned criterion ID is reported. Evidence is attached automatically from successful tool-call IDs (commit 7fd6e157, 13 Oct 2026: "attach agent completion evidence automatically") [P B17][P B19].
- "If no automated test script exists, use an available typecheck, lint, focused runtime assertion, or smoke check and explain its limits; do not invent passing tests" [P B17].
- **Run length.** `MAX_AGENT_STEPS = 32`. A starter-build continuation has `MAX_STARTER_BUILD_STEPS = 12`. Hitting the limit flips the state to `limited`, sets `toolChoice` to none, and makes the model "report actual progress, unmet completion criteria" [P B17][P B20]. Informational prompts (regex: explain/what/how… with no build verb) skip the loop [P B17].

### 1.3 Testing before claiming it works

- **Hosted Bolt.** No documented browser-testing agent. Errors come through **autofix**: "Bolt now autofixes errors for you… autofixed errors tracked as separate error types in your chat history". Otherwise you click **Attempt fix**: "each attempt uses tokens. Avoid clicking **Attempt fix** over and over" [P B9][P B5]. The help center admits that "A white screen can mean there's a runtime error that Bolt can't automatically detect", and asks the person to open the browser console and paste errors [P B23]. The security audit is the only pre-publish check, and "Neither the audit nor its fixes use your tokens" (paid plans, 30 per day) [P B7].
- **bolt.diy.** The completion gate in §1.2 *is* the test policy: no `agent_complete` without a passing `purpose:"test"` command after the last edit [P B17]. Older UI: `ChatAlert` shows "We encountered an error while running the preview. Would you like Bolt to analyze and help resolve this issue?" with an **Ask Bolt** button that sends "*Fix this preview error*" plus the log [P B24]. A failed start raises a **Dev Server Failed** alert carrying the command output [P B25].

### 1.4 Backend and data

- **What the person is asked.** Nothing by default. Bolt Database is created automatically [P B4]. Supabase is "select it during setup" (Pro/Teams). "There's currently no supported way to migrate an existing Bolt database to Supabase", although an existing Bolt Database can be **claimed** into your Supabase account (Pro/Teams; owner, co-owner or admin only) [P B4][P B26]. Auth "may not be added automatically". You prompt for it, then configure email confirmation and Google sign-in in settings [P B4].
- **Stages.** One. Unpublished projects' databases auto-pause after "low usage for six days or more" with the banner "Restoring your paused database…" [P B4].
- **Migrations (from source).** For every DB change the bolt.diy prompt demands **two actions, a migration file and the same SQL run as a query**, "Always enable RLS", "IF EXISTS/IF NOT EXISTS", "no number prefixes", and a markdown summary at the top of each migration [P B12]. The tool-protocol prompt: "use additive migrations with row-level security. Never invent credentials or apply destructive changes" [P B13]. The older open-source prompt (§1.6) forbids `DROP`/`DELETE` outright [U L5].
- **Rollback.** Version history, chat-history restore, a downloaded zip (restored through StackBlitz) or GitHub. "Restoring to an earlier project version will not change your current Bolt or Supabase databases." Undoing a security fix by version restore "also removes the security changes" [P B27][P B7].

### 1.5 Templates and stacks

- **Hosted.** Bolt Templates marketplace ("real data and working features"), team templates, Figma or Google Stitch import, Lovable import [P B28]. Design systems from GitHub, NPM, Storybook, a website or files (Teams) [P B29]. Mobile: say "mobile app" in the **first** prompt, because "Projects created for web do not easily switch over to mobile". Expo Go QR via **Device Preview**, then EAS to TestFlight or Play [P B30].
- **bolt.diy picker (regex, since d8ccd5a6).** Commit d8ccd5a6 (11 Oct 2026) is titled "Speed up template responses and parse XML file calls" [P B31]. The rules: information request without a build verb → `blank`; `next.js` → NextJS Shadcn **only if** "shadcn" is present, else `blank`; `shadcn` → Vite Shadcn; then astro, expo, remix, qwik, svelte, vue, angular, solid, react → Vite React; "script/snippet/one-off" → `blank`; **everything else → Vite React** [P B32].
- **Starter hand-off.** Template files are streamed in as a `type="bundled"` artifact. The project commands are detected and run as a second artifact. `.bolt/prompt` becomes "TEMPLATE INSTRUCTIONS". `.bolt/ignore` files become READ-ONLY ("Any attempt to modify these protected files will result in immediate termination"). Lock files are now kept "FOR IMPROVED INSTALL TIMES". The closing user message: "The user's original prompt is authorization to complete the requested work… Do not stop after a plan, ask whether to proceed…" [P B32]. A starter build is "ready" only when a file was written or patched **and** `previewReady` is true, within 12 steps [P B20].

### 1.6 Agent rules from prompts

Published (bolt.diy) [P B12][P B13][P B16]: "Treat tool output as the source of truth. Report failed commands as failed"; "A command result with status running only confirms it started"; "Never repeat an install or start another server while the existing process is still running"; "Avoid `npm run build` unless the user explicitly asks"; "Never modify .env apart from creating it"; progress updates are "1-2 sentences" and never contain file contents or commands.

Older open-source prompt, every line [U L5]: "DATA INTEGRITY IS THE HIGHEST PRIORITY"; "FORBIDDEN: Any destructive operations like `DROP` or `DELETE`"; "NEVER update existing migration files"; migration summaries readable by "non-technical stakeholders"; "Email confirmation is ALWAYS disabled unless explicitly stated".

### 1.7 Failure UX

- **Cost of a fix.** Every fix prompt and every **Attempt fix** click uses tokens. Restoring a version, using Publish and editing in Code view do not. The help center tells people to "use buttons instead of prompting" [P B9]. QuickStart: a one-word title change cost about 70K tokens [P B1].
- **Stuck states the docs name** [P B23][P B33][P B34]: blank or white preview; "Your preview will appear here" never loads; Out of memory (the WebContainer uses *your* RAM); "Bolt didn't do everything you asked" (blamed on large multi-part prompts); "Prompt too long" (split files over ~700 lines, prune agent-written `.md` files that "it reads… into your context on every prompt"); unresponsive (`/clear`); "Project is too large to deploy"; RLS, CORS and missing-secret errors.
- **Pattern.** The failure recovery Bolt documents is mostly *the person* pasting console or terminal output into chat. In hosted Bolt the agent does not see the browser runtime [P B23].

### 1.8 Ownership

"All code that you create with Bolt and StackBlitz is your own code… including commercial purposes" [P B35]. Hosting is bolt.host or Netlify. Data is Bolt Cloud (claimable to Supabase on paid plans) or your own Supabase [P B26]. Export: zip download, GitHub sync [P B27]. Model training on project content is **on by default** outside the EU/UK/CH for content from 7 Oct 2026, and Forge prompts are "anonymized and shared with Bolt's partners" [P B5].

**Sources (all fetched 2026-10-29).**
B1 https://support.bolt.new/get-started/quickstart.md · B2 https://support.bolt.new/prompting/plan-mode.md · B3 https://support.bolt.new/building/agents.md · B4 https://support.bolt.new/bolt-cloud/database.md · B5 https://support.bolt.new/release-notes.md · B6 https://support.bolt.new/bolt-cloud/hosting/publish.md · B7 https://support.bolt.new/building/security.md · B8 https://support.bolt.new/building/skills.md · B9 https://support.bolt.new/prompting/maximizing-token-efficiency.md · B10 https://github.com/stackblitz-labs/bolt.diy/blob/main/app/lib/runtime/action-runner.ts · B11 …/app/lib/runtime/message-parser.ts · B12 …/app/lib/common/prompts/prompts.ts · B13 …/app/lib/common/prompts/project-tools.ts and …/app/lib/common/prompt-library.ts · B14 …/app/lib/runtime/projectMcp.shared.ts · B15 GitHub tree of stackblitz-labs/bolt.diy main · B16 …/app/lib/.server/llm/prompt-workstyle.ts · B17 …/app/lib/common/agentLoop.ts · B18 …/app/lib/.server/llm/agent-loop.ts · B19 https://github.com/stackblitz-labs/bolt.diy/commit/7fd6e157 (commit list via GitHub API) · B20 …/app/lib/common/starterBuild.ts · B21 …/app/lib/.server/llm/stream-recovery.ts · B22 …/app/lib/.server/llm/constants.ts, stream-text.ts · B23 https://support.bolt.new/troubleshooting/preview-issues.md · B24 …/app/components/chat/ChatAlert.tsx · B25 action-runner.ts (`Dev Server Failed`) · B26 https://support.bolt.new/bolt-cloud/database/advanced.md · B27 https://support.bolt.new/managing-projects/rollback-backup.md · B28 https://support.bolt.new/building/start-project.md · B29 https://support.bolt.new/design-system.md · B30 https://support.bolt.new/integrations/expo.md · B31 https://github.com/stackblitz-labs/bolt.diy/commit/d8ccd5a6f3 · B32 …/app/utils/selectStarterTemplate.ts · B33 https://support.bolt.new/troubleshooting/issues.md · B34 https://support.bolt.new/troubleshooting/publish-issues.md and …/integrations-issues.md · B35 https://support.bolt.new/account/corporate-commercial.md · L5 https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools/blob/main/Open%20Source%20prompts/Bolt/Prompt.txt

---

## 2. Replit Agent (Agent 3 → Agent 4)

Agent 3 shipped in Sept 2025 [P R1]. Agent 4 launched 11 Mar 2026, and the docs now open with "Introducing Replit Agent 4" [P R2][S R3].

### 2.1 The person's loop

- **First prompt.** Start a chat and optionally pick an output ("Website", web app, mobile app, slides, design, data visualization). "If you already described what you want… Agent figures out the right setup automatically" [P R2][P R4]. The first-app guide teaches a five-step loop: "Describe one useful outcome. Add observable success criteria. Set boundaries for what not to build yet. Test the result in Preview. Ask for one focused improvement" [P R4].
- **Modes, picked before sending.** **Free** (intelligent routing; the Core/Pro allowance "resets every five hours" with weekly limits), **Power**, **Max**, and **Auto** model routing inside Power/Max. "If a request needs more capability than Free Mode, Agent may offer a paid **Allow once** option… Agent returns to Free Mode afterward" [P R2][P R5].
- **Plan.** The **Plan** toggle produces "an ordered task list". The buttons: **Revise**, **Cancel**, **Build here**, **Build in background**. Background tasks "wait for you to review and apply their changes" unless **Automatically apply changes** is on. Plan Mode is billed ("You are charged for all Agent work in Plan mode") [P R6].
- **Preview → publish.** "The preview URL is private by default, temporary". **Publish** opens a quick dialog with the domain (`.replit.app`) and an access level: **Public**, **Password protected**, **Workspace only**, **Invite only**. Under **Suggested add-ons** you can choose **Run scan** (Deep security scan) and **Block publishing of critical vulnerabilities**. Then Replit "provisions the deployment, runs security checks, builds and bundles the app, and promotes the release". Edits do not go live until **Republish** [P R7].

### 2.2 The agent's loop and tools

- **Architecture [S].** A manager agent, editor agents and a verifier agent, each "limited to the smallest possible task". Tools are called through "a restricted Python-based DSL" rather than native function calling (30+ tools). Few-shot examples, XML-sectioned prompts, "LLM-based memory compression". The verifier "often pauses to ask the user". "We don't strive for full autonomy. We want the user to stay involved" (2024–25 era) [S R8].
- **Tool list (leaked, older) [U L1].** `str_replace_editor`, `bash`, `search_filesystem`, `packager_tool` (instead of shell installs; "will automatically reboot all workflows"), `workflows_set_run_config_tool`/`restart_workflow` (dev servers are "workflows" bound to the **Run** button), `create_postgresql_database_tool`, `execute_sql_tool` ("not… for database migrations. Use a migration tool like Drizzle"), `ask_secrets` ("very expensive to run"), `web_application_feedback_tool` ("captures a screenshot and checks logs… asks user a question, and waits"), `suggest_deploy` ("a terminal action"), `report_progress`.
- **Run length.** Agent 1 ran ~2 minutes and Agent 2 up to 20 [S R9]. Agent 3 runs "up to 200 minutes with Live Monitoring" [P R1]. After launch, users found it "too autonomous and does more work than they want", and Replit added an **Autonomy Selector: Low / Medium / High / Max**, estimating "around $10/hr" of autonomous running [S R10]. In Agent 4, "every request… is broken into discrete tasks that run in parallel in the background", and "specialized sub-agents" resolve conflicts [S R3]. The docs show "worker chats" as cards in the main chat, a **Main agent** switcher and an **Agents** tab. Active background tasks: Core 1, Pro 10 [P R11][P R2].
- **When it stops.** Leaked: `suggest_deploy` and `report_progress` end a turn, and the old verifier waited for the user's answer after a screenshot [U L1]. Today's docs: Agent "plans the artifact, creates the app, starts it, and checks the result" until "Agent presents a Website artifact and Preview is running" [P R4].

### 2.3 Testing before claiming it works

- **App Testing.** "Agent navigates through your application like a real user would, clicking around… entering mock data when necessary", then "reports back with a summary of its tests and automatically fixes any issues". It does not test after every message. "Agent will periodically decide to test itself when it thinks enough has changed" [P R12].
- **What the person sees.** A browser pane inside the Agent pane (or inside the background task) with Agent's cursor visible. Afterwards there is a **video replay** with "section-by-section navigation". It covers web apps only, it is a toggle under Advanced settings, and it is "off" in Free Mode [P R12].
- **Take over.** When the test hits a login (e.g. Gmail), a **Begin take over** button lets the person drive the test browser. **Skip** ends the test. "If you do not respond within 10 minutes, the Agent will continue" [P R12].
- **Cost.** "Testing is charged based on the effort spent" [P R12]. Replit claims its tester is "up to 3x faster and 10x cheaper than computer-use models" [S R9].

### 2.4 Backend and data

- **What the person is asked.** For a database: nothing technical. The guide's prompt names Neon, the fields and the rules ("Attach every reservation to the signed-in customer", "Prevent… overlapping dates"), then "Tell me what to test after the change". "There's no Neon dashboard signup and no keys to paste" [P R13]. Secrets go through `ask_secrets`. The leaked guidance says never to ask for `PHONE_NUMBER`/`PASSWORD` as secrets and never for always-present `REPLIT_DOMAINS` [U L1].
- **Stages.** Development and Production, with the agent fenced off production (detail in the earlier note). New fact: since 4 Dec 2025, dev databases run on Helium, Replit's own Postgres 16, not Neon [P R14].
- **Checkpoints.** One per request in paid modes ("One checkpoint per request eliminates intermediate checkpoints and reduces billing noise"). Each holds files, packages, "AI conversation context", "Agent memory", environment and publishing config, and "Database contents". Rollback restores the database **only when selected** ("Database contents (optional)"), and **roll forward** exists. Checkpoints also appear as Git commits in the Git pane, and each carries "Billing information… cost tracking per checkpoint" [P R15][P R16].

### 2.5 Templates and stacks

- No starter-picker is documented. "Agent figures out the right setup". A project can hold "multiple outputs… web apps, mobile apps, slides, and videos sharing the same backend" ("Multi-Artifacts") [P R2].
- Design comes first if wanted: **Replit Design** canvas, frames, templates, Figma/website/Claude-design import, and "Build" from a frame [P R17]. Mobile is Expo: simulator plus **Expo Go** on device, then "Launch" to App Store Connect/TestFlight. Add-on guides cover Clerk login, RevenueCat and App Storage [P R18].

### 2.6 Agent rules from prompts

Published: the first-app prompt pattern (outcome, observable criteria, boundaries) is Replit's own teaching [P R4]. Leaked, older "Replit Assistant" [U L1]: "MUST be precise and accurate WITHOUT creative extensions unless explicitly asked"; shell proposals carry `is_dangerous`; send the person to the Secrets tool for keys and to Deployments for "publish".

### 2.7 Failure UX

- **Costs.** Effort-based: "Simple requests… typically cost less than complex builds", billed per checkpoint, plus third-party model calls "at the provider's public API rate" [P R16]. Users complain of "rollbacks without refunds" and of poor results on older projects [S R10].
- **Named publish failures** [P R19]: preview works but publish fails (logs, run/build commands, **production Secrets**, long-running processes, host/port, health-check timing); publish succeeds but the URL is broken; static-deployment issues; size limits; DNS problems on `replit.app`/`replit.dev`. Database: "Reservations save in Preview but not on the public URL: ask Agent to confirm the Production Neon credentials are wired" [P R13].
- **Pattern.** Replit recovers mostly by agent self-testing and per-task billing receipts ("End of run summary") [S R10]. The person's job is "describe the symptom to Agent instead of trying to diagnose the code yourself" [P R4].

### 2.8 Ownership

Replit hosts on its own cloud (Autoscale, Reserved VM, Static, Scheduled) and bills usage from credits [P R7][P R16]. Code is in a Git-backed project with a Git pane [P R15]. Data lives in Replit's Helium Postgres. Legacy Neon users keep their old string as `NEON_DATABASE_URL` [P R14]. Lovable comparison, one line: Lovable shares one DB between preview and live, while Replit splits dev and prod and fences the agent off prod.

**Sources (all fetched 2026-10-29).** R1 https://docs.replit.com/updates/2025/09/12/changelog.md · R2 https://docs.replit.com/replitai/agent.md · R3 https://tessl.io/blog/replits-agent-4-coordinates-multiple-ai-agents-to-build-apps-in-parallel/ and https://gamesbeat.com/replit-introduces-agent-4-to-treat-software-development-as-creative-work/ (via search) · R4 https://docs.replit.com/build/your-first-app.md · R5 https://docs.replit.com/chat/agent-modes.md, https://docs.replit.com/chat/auto-mode.md · R6 https://docs.replit.com/replitai/plan-mode.md · R7 https://docs.replit.com/build/publish-your-app.md · R8 https://langchain.com/breakoutagents/replit · R9 https://en.startuprecipe.co.kr/?p=22303 (via search) · R10 https://replit.discourse.group/t/replit-is-listening-agent-3-updates/7228 · R11 https://docs.replit.com/build/code/coordinate-work.md · R12 https://docs.replit.com/replitai/app-testing.md · R13 https://docs.replit.com/build/add-database.md · R14 https://docs.replit.com/features/data-and-storage/development-and-production.md · R15 https://docs.replit.com/replitai/checkpoints-and-rollbacks.md · R16 https://docs.replit.com/billing/ai-billing.md · R17 https://docs.replit.com/llms.txt (Design, Connectors, Skills index) · R18 https://docs.replit.com/build/mobile-app.md · R19 https://docs.replit.com/build/troubleshooting.md · L1 https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools/tree/main/Replit (Prompt.txt, Tools.json; last changed 2025-07-26)

---

## 3. v0 by Vercel

v0 was rebuilt on 4 Feb 2026 around Git, Projects, a full editor, and previews running in a Vercel Sandbox VM. The VM "replaces the previous browser-based preview, which couldn't run server code, API routes, or real database connections" [P V1][P V2].

### 3.1 The person's loop

- **First prompt.** Type in the composer. The toolbar holds a model picker, a design-system picker, and a **+** menu with the permission mode (§3.2). Screenshots, Figma links or files can go in as references. The docs' own short version: "Start simple. Plan your project. Provide context. Specify and make v0 clarify. Iterate." [P V3].
- **Preview.** It runs in the chat's own VM. The **Preview** tab shows it, the **Console** panel has **Logs** and **Terminal**, and the **Code viewer** lets the person add code comments as context for the next prompt [P V2].
- **Iterate.** Every generated message carries version controls (inspect, diff, restore). "Restoring an old version creates a new, most recent version", so history stays linear. Deploy always uses the latest version [P V4].
- **Publish (first time).** **Publish** → **Create project** → **Visibility** → **Domains** (`vercel.app` or custom) → **Commerce mode, when needed** ("Review unclaimed Stripe test sandboxes or Shopify development stores and choose whether to claim them") → **Publish**. Later releases use **Publish Changes**. With GitHub connected, Publish "creates or reuses a pull request, merges it into the chat's base branch, and waits for the production deployment". "v0 never pushes directly to `main`" [P V5][P V1].
- **Share.** Chat sharing and team roles. Templates can be published to the v0 Templates gallery and "forked by other users" [P V6].

### 3.2 The agent's loop and tools

- **Documented capabilities** [P V7]: web search with citations; **browser use** ("open the apps it builds, use them… sends you screenshots"); automatic error fixing "as part of the generation loop"; Marketplace integrations; MCP; a Bash terminal. The UI shows **tool execution cards**, **Stop** and **Auto-continue**.
- **Terminal permission layer** [P V8]. **Ask / Auto (default) / Full Permissions**, remembered across chats. Auto runs a built-in read-only list (file reads, `git log/diff`, read-only `gh`/`vercel logs`, `agent-browser`) and "decides whether to request confirmation" for anything else. User and team rules like `Bash(pnpm test:*)` go in `allow`/`ask`/`deny`; the most specific wins, and compound commands take the most restrictive verdict. The built-in `rm -rf` deny is "a guard, not a complete sandbox safety boundary".
- **Sandbox** [P V2]. One VM per chat, filesystem kept, lease extended while active, capped at "24 hours from its creation". "A sandbox is never the same machine as your deployed app." A `claude` CLI is pre-installed in it, billed through the AI Gateway [P V9].
- **Leaked tool surface [U L2].** The 2026 prompt names `AskUserQuestions`, `Write`/`Edit`/`Move`/`Delete` (with "optimistic concurrency checks"), `Bash`, `GenerateImage`, `ToolSearch` for MCP, and a virtual log file `user_read_only_context/v0_debug_logs.log`. The older Tools.json adds `SearchRepo` (a sub-agent), `TodoManager` (3–7 milestones), `InspectSite` and `GetOrRequestIntegration` ("Automatically requests missing integrations from users").
- **When it stops.** No documented step cap. The leaked rule for sandbox outages: "stop retrying after 2 consecutive failures. Inform the user" [U L2]. The postamble is "2-4 sentences" [U L2].

### 3.3 Testing before claiming it works

- In-loop autofix plus browser use with screenshots in chat [P V7]. Through the terminal it can "launch a headless browser session against your live preview to verify user flows", "Run the unit tests and fix any failures", and "Check the latest deployment logs in Vercel" [P V8].
- **Fix with v0** appears in the deployment popover "When a deployment has errors or warnings". It "sends the error logs to v0". Paid plans get some free uses per day; the number is blank in the docs as fetched. Free fixes apply only to "**unedited code**", and after the cap a fix "consumes credits like a normal prompt" [P V7]. On GitHub-backed projects the button changes with the problem: **Review Code**, **Fix Build**, **Fix CI**, **Fix Conflicts**, **Fix CI + Conflicts** [P V5].

### 3.4 Backend and data

- **What the person is asked.** Accepting a Marketplace provider's click-through terms. From chat ("Click the suggested action, or Ask v0 to add a database integration") or from **Project menu → Settings → Integrations → Create**. "Adding an integration provisions a new user account on that service and adds the necessary environment variables". Other variables trigger a chat prompt to add them [P V10].
- **Defaults (leaked) [U L2].** "v0 MUST recommend Supabase as the default choice for both authentication and the primary database, and Vercel Blob for file storage". "NEVER uses localStorage for data persistence unless explicitly requested". With Neon, "build custom authentication with… bcrypt". "NEVER implements mock authentication". AWS → "Aurora PostgreSQL". Payments: "MUST prompt the user to install the payments integration before building UI to display products".
- **Stages.** Vercel's own: preview deployments per working branch, a production deployment, and env scopes Production/Preview/Development [P V5]. The sandbox gets "Environment variables from your connected Vercel project" [P V2]. The docs do not say which scope the sandbox uses [U]. Community reports from the earlier note say production values [S].
- **Migrations.** "For SQL-based integrations, [v0] can generate and execute SQL. This lets you create, update, and drop tables." No migration workflow is documented [P V10].
- **Rollback.** Restore an earlier chat version, then publish again, or use Vercel's deployment inspector **Rollbacks**. Database state is never part of a v0 version [P V5][P V4].

### 3.5 Templates and stacks

- **Default stack.** Next.js with shadcn/ui (new-york style) pre-installed. Leaked: "All Projects come with a default set of files… you never generate these" (`components/ui/*`, `lib/utils.ts`, `globals.css`…). Switching to Vite means deleting the Next files first [U L2]. Package manager: pnpm [U L2].
- **Templates.** The v0 Templates gallery, made of community-published chats, which you fork [P V6]. shadcn "registries" open in v0 [P V11].
- **Design Systems 2.0** is now a *skill*. Import CSS, fonts and components; "v0 builds a small starter app with your theme and pauses for your review"; you approve to save it; teams can set a default. Legacy themes retire 30 Nov 2026 [P V12].

### 3.6 Agent rules from prompts

Leaked 2026 prompt [U L2], beyond the rules above: "Do not write code that imports a new package before the install command has completed"; prefer dedicated file tools over Bash edits; "Don't Stop at the First Match… Understand the Full System"; rules on when to save memories.

### 3.7 Failure UX

- **Costs.** Token-metered credits. "When credits are used up, generation pauses." Monthly credits roll over and "expire after 65 days" [P V13]. A ChatGPT Plus/Pro plan can pay for OpenAI-model responses, but each request still carries "a small platform fee in v0 credits" [P V13].
- **Community pain [S V14].** "v0 fixes the same error at least 60 times", "Erroring out but consuming credits", and staff admitting the UI "is not clear about when you're past the free limit" for Fix with v0. Documented stuck states: build fails, GitHub publish blocked, publish disabled by team policy, missing domain [P V5].

### 3.8 Ownership

The code lives in your GitHub repo and Vercel project. "Vercel doesn't own the code generated" (teardown). The database is a Marketplace account in your Vercel team [P V10]. Hosting is your Vercel project, whose plan sets the limits. **Platform API/SDK**: `v0.chats.create({message})` returns `latestVersion.demoUrl` for an iframe, plus files, continue-chat, deployments and restore-message endpoints. Third parties can embed v0's whole loop in their own UI [P V15]. Lovable comparison, one line: Lovable hosts on its own cloud, while v0 always lands in the user's Vercel team.

**Sources (all fetched 2026-10-29; all v0 pages read from the full-text https://v0.app/docs/llms.txt, which lists each page's path).** V1 /docs "New v0 Update" (released 4 Feb 2026) · V2 /docs/sandbox · V3 /docs/text-prompting, /docs/instructions · V4 /docs/versions (version controls, restore) · V5 https://v0.app/docs/deployments · V6 /docs/templates (publishing a template) · V7 https://v0.app/docs/agentic-features · V8 https://v0.app/docs/terminal-commands · V9 /docs/pre-installed-agents · V10 https://v0.app/docs/databases · V11 /docs/design-systems (registry) · V12 /docs/design-systems-2 and legacy-theme migration · V13 /docs/pricing (credits) · V14 https://community.vercel.com/t/v0-fixes-the-same-error-at-least-60-times-me-running-out-of-credits/12228, https://community.vercel.com/t/v0-erroring-out-but-consuming-credits/16212, https://community.vercel.com/t/is-fix-with-v0-free/27771, https://community.vercel.com/tag/v0-agent (via search) · V15 /docs/api/platform overview and SDK quickstart · L2 https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools/tree/main/v0%20Prompts%20and%20Tools (Prompt.txt last changed 2026-05-10; Tools.json older)

---

## 4. Base44 (Wix)

### 4.1 The person's loop

- **First prompt.** Describe the app at app.base44.com. "If your prompt leaves something important open… the AI asks about it in the chat before it builds… It asks 1 question at a time, and after each answer it checks whether anything important is still open" [P A1].
- **Plan mode.** The Mode dropdown ("shows **Build** by default") → **Plan**. A side panel, **Plan progress**, ticks off four sections: **Intent and goal**, **Audience and roles**, **Core flows**, **Design**. The design question offers "4 working previews of your app's main screen to choose between". Then **Start Building** [P A1]. You can also **Start from URL** (**Content & Design** or **Design only**), Figma, or the template marketplace [P A1][P A2].
- **Iterate.** In-editor modes are **Build** ("Acts instantly") and **Discuss** (a proposed plan appears above the chat box with **View** / **Approve** / **Reject**). **Edit** mode is a click-to-edit toolbar with **Edit Tailwind**, **Code** and **Edit Element** (sends to AI), with 50-step undo. **Branches** each get their own chat and preview, and can be merged [P A3][P A4].
- **Publish.** **Publish** opens a panel with **Web** and **Mobile app** tabs, **Get domain**, **Share your app**, **App visibility**, **Security status** (**No risks** / **Out of date** with **Scan** / **Risks found**; "warns you about risks but doesn't stop you from publishing"), and **Publish** [P A5][P A6]. Version history can "publish it directly while you keep working on your draft" [P A7].

### 4.2 The agent's loop and tools

- **Documented behaviour.** "After the AI applies your changes, it automatically checks the output in the background and fixes any errors it finds, without interrupting your workflow". The agent "knows when to search the web, check your files, or pull in data", with automatic model selection [P A3].
- **Person-set controls** [P A3]: model picker, an **effort level** (Auto, Low, Medium, High, Extra high, Max), **Auto approve** ("most steps that would pause for your approval run on their own… Changes that widen access to your data always ask first, even if you requested them"; anything skipped is labelled **auto-approved**), **Freeze Files** (main only), **Custom Instructions** (main only), and `/` to pull in a workspace skill or connector.
- **Open sandbox.** "Every Base44 app has a sandbox: a private cloud environment that holds the app's code and runs its development server… the same code… the app editor uses". It can be reached by CLI, MCP or API. **Send to Coding Agent** writes a ready prompt for Claude Code, Cursor or Codex. `sandbox write`/`edit` commit on return. "There's no lock… treat it the way you would treat a colleague editing the same project." Sandbox edits still need **Publish** [P A8][P A9].
- **Keys.** "Paste an API key into the AI chat and Base44 recognizes it, saves it as an app secret, and replaces it in your message with the name" (Oct 2026) [P A10].

### 4.3 Testing before claiming it works

- **Testing agent** (More actions → **Testing agent**). **Generate tests with AI** "from your app's context and chat history", or **Add test manually** with a **Test name** and **Prompt scenario**. **Run Tests** opens a real browser you can watch. Results have an **Issues** tab (Critical/Warning, then **Fix with AI**) and an **Activity** tab with every step. Tests persist and can be re-run, so they work as a saved regression suite [P A11].
- **"Something is wrong"**: typing that phrase makes Base44 analyse the app and suggest fixes [P A12]. **Issues Found → Resolve with AI**: "this doesn't use your credits" [P A13].
- **Security scan** with one-click fixes, a **Credit protection** check (places where "someone could spend your credits without signing in"), scheduled scans, and Tenzai AI pen-tests on the live app [P A6][P A10].

### 4.4 Backend and data

- **What the person is asked.** Nothing upfront: "Base44 securely stores all your app's data, manages account and login systems… You do not need to set up or manage anything" [P A5]. Entities are tables with typed fields. "The AI sets up your tables" [P A14].
- **Stages.** Opt-in **Test Data** (covered in the earlier note). Branches do **not** isolate data: "Your app's data is the same live records everywhere, and record changes happen on main", and integrations, secrets, automations and in-app agents are shared too [P A4]. Table menu: **Test Permissions in preview**, **Backup & Restore** "(in Production only)", **Recently deleted** [P A14]. Records the AI chat created while building are labelled as such [P A14].
- **Checkpoints.** A restore point "anchored to a specific commit", created by `sandbox checkpoint` [P A8]. Version history restores code. "Asking the AI to undo something makes a new change rather than going back to an earlier version"; use **Revert** under the message. "**Can't revert while the app is building**" [P A3].

### 4.5 Templates and stacks

- **Stack.** A React SPA on Base44's backend (entities, Deno functions). Covered in the teardown.
- **Templates.** A public marketplace with ratings, workspace templates, and publishing your own [P A2].
- **Design.** Plan mode's 4 previews, a design system and component library, Edit mode [P A1][P A3].
- **Mobile.** No Expo. "Your mobile app runs your published Base44 app inside a secure web view… a lightweight native wrapper", so most changes reach the store app without resubmission [P A15].
- **Base Code.** The same agent works on an existing GitHub repo, on branches, through PRs [P A16].

### 4.6 Agent rules

Published only: the "1 question at a time" clarifier, and that access-widening changes always ask [P A1][P A3]. No leaked prompt found [U].

### 4.7 Failure UX

- **Credits.** "Your first build uses a small, fixed amount, about 1 credit, no matter how detailed your prompt". After that, cost scales with work and model. "Credits are used only when the AI builds or updates something… previewing… always free". Error fixes through **Resolve with AI** are free [P A13]. Running out "can stop the AI partway through" [P A3].
- **Named stuck states** [P A3][P A12]: chat stuck or ignoring stop; the AI keeps getting the change wrong; "undo" didn't restore; can't revert while building; editor won't load or blank screen; changes don't go live; backend functions error; the link works for the owner but not others; domain issues; HTTP 400/401/… error-code glossary.

### 4.8 Ownership

Two-way GitHub sync, which replaced a legacy one-way "Export to GitHub" [P A17]. The backend stays Base44-hosted (teardown). Data export per table [P A14]. Lovable comparison, one line: like Lovable, Base44 runs the backend for you, but Base44 has no "bring your own Supabase" path.

**Sources (all fetched 2026-10-29).** A1 https://docs.base44.com/Getting-Started/starting-from-your-first-prompt.md · A2 https://docs.base44.com/Getting-Started/App-templates.md · A3 https://docs.base44.com/Building-your-app/AI-chat-modes.md · A4 https://docs.base44.com/Building-your-app/working-with-branches.md · A5 https://docs.base44.com/Getting-Started/Quick-start-guide.md · A6 https://docs.base44.com/Setting-up-your-app/running-a-security-scan.md · A7 https://docs.base44.com/Getting-Started/app-editor-overview.md · A8 https://docs.base44.com/developers/app-code/sandbox/overview.md · A9 https://docs.base44.com/developers/app-code/sandbox/bring-your-own-agent.md · A10 https://docs.base44.com/changelog/product.md (Oct 2026) · A11 https://docs.base44.com/documentation/managing-app-data/testing-agent.md · A12 https://docs.base44.com/Community-and-support/Troubleshooting.md · A13 Base44 docs pages on credits and errors (grep of downloaded pages; https://docs.base44.com/Account-and-billing/Credits.md) · A14 https://docs.base44.com/Building-your-app/Managing-your-app-data.md · A15 https://docs.base44.com/documentation/building-your-app/uploading-to-app-stores.md · A16 https://docs.base44.com/Getting-Started/base-code.md · A17 https://docs.base44.com/developers/app-code/local-development/github.md

---

## 5. Anything (formerly Create.xyz)

### 5.1 The person's loop

Prompt → preview → **Publish** (top right) → choose a `.created.app` subdomain → "Select the pages and functions to include" → set routes → **Publish**. Preview is visible "Only you". Mobile publish goes to TestFlight or Play internal testing, with a checkbox "**Also publish web and backend**" so the live app works [P Y1]. Templates are bought or used free from a marketplace with ratings, then "forks the template into a new project you own" [P Y2].

### 5.2 The agent's loop and tools

- **Modes** in the selector: **Thinking** (default, "the most efficient credit mode"), **Fast**, **Discussion**, **Plan**, **Max** [P Y3]. The model switcher lists Claude Opus 4.6–4.8, GPT-5.3 Codex/5.4, Gemini 3.1 Pro/3.0 Flash and Kimi K2.6. By default "the agent uses multiple models and picks the best one for each step" [P Y3].
- **Tools named** [P Y3]: web search, `/` integrations, image generation, error fixing ("say 'fix me' or paste error messages"), a version-history tool that "Finds and suggests previous versions to restore" as a chat card with **Restore**, codebase exploration "with a live activity indicator", runtime logs "without you having to copy-paste them", and file/Figma inputs. Each step expands to show its reasoning.
- **Max** ($200+/month plus credits): "writes code, opens your app in a real browser, uses it like a person, and keeps going until the task is complete… sometimes runs 100+ steps over 30 minutes". It also tests mobile "on a simulated iOS device" and runs backend logic. Parallel Max runs go in separate threads [P Y4].
- **Drivable by other agents.** The `anything` CLI ("built for scripting and for AI coding agents") and a REST API: create project, **request a change**, read messages/files/dev logs, publish/unpublish, deployments with **rollback**, **reset development database**, secrets, App Store submission [P Y5][P Y6].

### 5.3 Testing

Browser, iOS-simulator and backend testing only in Max [P Y4]. Other modes rely on in-loop error fixing and log reading [P Y3]. The database troubleshooting order: "Database structure → Functions ('ask the agent to test the function') → Page to function ('try the app in Demo mode and watch the database viewer')" [P Y7].

### 5.4 Backend and data

"Every project gets two databases: one for development, one for production" (Neon Postgres). The agent designs tables, writes Functions, builds pages and wires them together. "When you publish, Anything pushes the database structure… Data stays separate" [P Y7]. Auth: "Ask the agent and it handles the rest", and the person says which pages are protected [P Y8]. The v2 architecture migration copies domain, database and users into a **new** project and leaves the original untouched ("Start Migration", emailed when ready) [P Y9].

### 5.5 Templates and stacks

React (web) and React Native (mobile) [P Y3]. Expo Go on device needs "the same Expo account in the builder and on your phone" [P Y10]. Paid templates cost credits [P Y11].

### 5.6 Agent rules

Only the published prompting advice: "Tell it *what* your app should do, not *how*" [P Y3]. No leaked prompt found [U].

### 5.7 Failure UX

Max is "slower and uses more credits". Max plans run $199–$899/month for 220K–990K credits. Published apps also spend credits per AI-integration run, unless you call your own keys through Functions [P Y11][P Y4]. Deleted projects can be recovered for 30 days [P Y5].

### 5.8 Ownership

"You own all the code". **Download Project** gives a zip. "GitHub sync is coming soon" [P Y12]. Hosting and the database stay on Anything.

**Sources (all fetched 2026-10-29).** Y1 https://www.anything.com/docs/launch/publish.md · Y2 https://www.anything.com/docs/apps/templates.md · Y3 https://www.anything.com/docs/builder/agent.md · Y4 https://www.anything.com/docs/builder/max.md · Y5 https://www.anything.com/docs/builder/cli.md · Y6 https://www.anything.com/docs/llms.txt (API reference index) · Y7 https://www.anything.com/docs/apps/databases.md · Y8 https://www.anything.com/docs/apps/auth.md · Y9 https://www.anything.com/docs/builder/upgrade-to-v2.md · Y10 https://www.anything.com/docs/apps/mobile/testing.md · Y11 https://www.anything.com/docs/account/credits.md · Y12 https://www.anything.com/docs/share/export.md, https://www.anything.com/docs/faq.md

---

## 6. Leaked prompts, side by side (every line [U])

Repository: `x1xhlol/system-prompts-and-models-of-ai-tools`, fetched 2026-10-29. Leaks can be stale, partial or fabricated. Treat them as hints that the vendor docs above either confirm or don't.

| | Replit (L1, last changed 2025-07-26) | v0 (L2, Prompt.txt 2026-05-10) | Bolt (L5, older open-source) |
|---|---|---|---|
| Identity | "Replit Assistant" (the IDE assistant, not Agent 3/4) | "v0, Vercel's highly skilled AI-powered assistant" | "Bolt, an expert AI assistant" |
| Edit format | `<proposed_file_replace_substring>` with unique `<old_str>`; whole-file replace; insert at line | Dedicated `Write/Edit/Move/Delete` tools with "optimistic concurrency checks" | `<boltAction type="file">`, full file, "NEVER use diffs" |
| Shell | proposals carry `is_dangerous`; servers through "workflows", not shell | `Bash`; install packages *before* writing importing code | `type="shell"`, `type="start"`; never re-run dev server |
| Data defaults | Postgres tool injects `DATABASE_URL`; migrations through Drizzle/flask-migrate, not raw SQL | Supabase + Vercel Blob; "NEVER… localStorage"; no mock auth | Supabase; migration + query pair; "FORBIDDEN… DROP or DELETE" |
| Secrets | `ask_secrets` ("very expensive"); never ask for always-present vars | env vars come with the integration; else prompt the user to add | "Never modify .env apart from creating it" |
| Stop / hand-off | `suggest_deploy`, `report_progress` are terminal; feedback tool waits for the user | `AskUserQuestions` not in parallel; stop after 2 sandbox failures | "Do NOT be verbose" |
| Design | "WITHOUT creative extensions unless explicitly asked" | 3–5 colours, ≤2 fonts, no purple, no emoji icons, no blobs | 8pt grid, 3–5 colours, "stunning" |

What the leaks add beyond the docs: (1) **secrets are a dedicated, expensive, user-facing tool** in Replit; (2) v0 reads logs through a **virtual file** and is told to ignore stale errors by server markers; (3) every one of them hard-codes a **default data stack** so the agent never asks "which database?".

---

## 7. Comparison (products × the 8 headings)

| | Bolt.new / bolt.diy | Replit Agent 4 | v0 | Base44 | Anything |
|---|---|---|---|---|---|
| **1. Person's loop** | **Build now** → WebContainer preview in *your* tab → Select/comments → **Publish**×2 → bolt.host, public or private | Output type + mode → plan (**Build here / in background**) → private preview → **Publish** with access level → **Republish** | Composer → VM preview → versions per message → **Publish** wizard (Visibility, Domains, Commerce) → PR merge | 1-question clarifier or **Plan** (4 sections, 4 design previews) → **Build/Discuss/Edit** → **Publish** panel | **Thinking** default → preview → **Publish** (pick pages/functions/routes) |
| **2. Agent loop & tools** | diy: read-revision-patch tools, approved commands, **server-gated plan→edit→run→test→complete**, 32 steps | Manager/editor/verifier [S]; parallel background tasks; up to 200 min | Bash + Ask/Auto/Full rule engine; browser use; VM 24 h cap; Claude Code inside | Auto-check after each change; Auto approve; effort level; open sandbox for your own agent | Multi-model per step; Max runs 100+ steps / 30 min; CLI/API for other agents |
| **3. Testing** | Hosted: autofix + **Attempt fix** (tokens); diy: no completion without passing test | **App Testing** browser with video replay, **Begin take over**, billed | Browser screenshots; **Fix with v0** on failed deploys (some free/day) | **Testing agent** with saved named tests, Issues → **Fix with AI**; **Resolve with AI** free | Max only: browser, iOS sim, backend |
| **4. Backend & data** | Auto-created DB, one stage; migration+query pair, RLS; restore never touches DB | Dev/Prod named; agent can't touch prod; schema pushed on publish; checkpoint restores DB *optionally* | Marketplace click-through; SQL can drop tables; Vercel env scopes; no DB in versions | Auto entities; opt-in Test Data; branches share live data; Backup & Restore in prod | Dev + prod Neon per project; schema pushed, data separate |
| **5. Templates & stacks** | Regex → Vite React default; `.bolt/prompt` + `.bolt/ignore`; marketplace; Expo | Agent picks; Design canvas; Expo + Launch | Next.js + shadcn baked in; community templates; Design Systems 2.0 skill | Marketplace + workspace templates; web-view mobile wrapper | React / React Native; paid templates |
| **6. Rules** | Published prompts: tool output is truth, additive migrations, keep scope small | Leaked: no creative extensions, secrets tool, deploy is terminal | Leaked: Supabase default, no localStorage, install-then-import, design limits | Docs only: access-widening always asks | Docs only |
| **7. Failure UX** | Tokens per fix; console paste; OOM; context too long → delete `.md`, split files | Effort-billed per checkpoint; ~$10/hr autonomous [S]; publish failures named | Credit-burn loops [S]; Fix free only on unedited code | ~1 credit first build; free error fixes; stuck chat → Revert | Max costs most; integration runs bill credits |
| **8. Ownership** | Code yours; Bolt Cloud or own Supabase; zip/GitHub | Replit cloud + Helium DB; Git pane | Your GitHub + Vercel + Marketplace DB | Base44 backend; 2-way GitHub; sandbox API | Code yours (zip); hosting/DB theirs; GitHub "coming soon" |

Lovable, one line per heading for reference: chat → preview → Publish; agent edits with its own tools; no browser-testing agent documented; one shared DB, Cloud or own Supabase; React/TanStack default; leaked prompt in the same repo; credits per message; code yours, Cloud backend theirs (see `2026-10-29-how-lovable-builds.md`).

---

## 8. What this means for Lightbulb's builder agent and Apps UX

Twenty recommendations. Each names its source. "Forge" is the builder in the Apps spec. Commands are the spec's `lightbulb app new|up|publish|rollback|status|destroy`.

**The agent loop**

1. **Gate "done" in code, not in the prompt (bolt.diy).** Re-derive state from tool results on every step: plan recorded? edit after plan? `up` succeeded after the last edit? check passed after the last edit and the last failure? Refuse Forge's "preview is ready" room post until all are true, and attach the tool-call evidence automatically [P B17][P B18].
2. **Force the next action (bolt.diy).** When a check fails, narrow Forge's tools to read/search/edit until it repairs. Inject "NEXT REQUIRED ACTION" and "remaining steps" into every step [P B18].
3. **Hard step budget with an honest stop (bolt.diy, Replit).** Use a fixed cap (bolt.diy uses 32 for a task, 12 for the starter hand-off). At the cap, post "what works, what doesn't, which checks failed" and never claim completion [P B17][P B20]. Replit's "too autonomous" backlash and $10/hr estimate show why the cap needs to be visible [S R10].
4. **Read-revision-patch edits (bolt.diy, v0).** Every patch carries the revision it read. A full rewrite needs a stated reason. Invalid syntax is rejected before it lands [P B13][U L2]. This matters most when two agents (or a person in the code) touch one app, which Base44 explicitly allows with "no lock" [P A8].
5. **Runtime facts in the first lines of the prompt (bolt.diy).** State what the box can and cannot run, the package manager, and "poll a started process until it exits; a started server is not a passed check" [P B12][P B16].
6. **Install, then write imports (v0 leak).** This is a one-line rule that removes a whole class of first-preview failures [U L2].

**Testing and fixing**

7. **One scripted browser pass before the preview link, with the recording in the thread (Replit, Base44, Anything).** Post a short replay or screenshots under the preview line, like Replit's video replay with sections [P R12]. Keep it to one pass per `up`, because Replit testing on every change ran "too long" [S R10].
8. **Save the tests the agent wrote as the app's regression list (Base44).** Base44's testing agent turns flows into named, re-runnable tests from chat history [P A11]. Forge should write 2–4 flow tests at `new` and re-run them on every `publish`.
9. **Take-over when a test hits a login (Replit).** Post a "Begin take over"-style card in the room. On no answer, skip the test and say so. Replit continues after 10 minutes [P R12].
10. **Feed deploy and runtime logs to the agent directly (v0, Anything).** Fix with v0 sends the build logs. Anything reads runtime logs "without you having to copy-paste" [P V7][P Y3]. Bolt's help center makes *people* paste console errors because hosted Bolt can't see them [P B23]. Never ask a room member to paste a log.
11. **Never charge the person for the agent's own mistakes (Base44, v0).** Base44's **Resolve with AI** is free. v0's free fixes apply only to unedited code, and the forums show what happens when that line is fuzzy [P A13][P V7][S V14]. Make repair of a failed `up` free and say so in the card.

**Data, stages, rollback**

12. **Name two stages and fence the agent off live data (Replit, Anything).** "Agent can't touch" production. Schema goes forward on publish and data stays separate [P R14][P Y7]. Base44 branches sharing live data, and the agent working on production by default, are what to avoid [P A4].
13. **Additive migrations as files and say what each does in plain words (bolt.diy).** A migration file plus its execution, IF NOT EXISTS, RLS on, and a top-of-file summary "non-technical stakeholders can understand" [P B12][U L5]. Post that summary to the room on `publish`.
14. **Make rollback's data choice explicit (Replit).** Replit restores the database "optional[ly]" and Bolt never does [P R15][P B27]. The spec says "code and data go back together". Keep that as the default, but show the rows that would be lost and let the owner pick "code only".
15. **Checkpoints carry the conversation and a cost (Replit).** Each `up`/`publish` line in the thread is the checkpoint: commit, deploy, test result and the spend for that step, as Replit puts "Billing information" on each checkpoint [P R15].

**The person's UX**

16. **At most one question, one at a time; the request is the authorization (Base44, bolt.diy).** Base44 asks one gap-filling question at a time. bolt.diy treats the prompt as "authorization to complete the requested work" [P A1][P B32]. In a room, the preview link is the next question.
17. **Keep scope small by rule (bolt.diy, Replit).** "A simple or minimal page should stay simple; do not add… authentication, databases or dependencies merely to expand the task" [P B13]. Replit teaches "boundaries for what not to build yet" [P R4]. Bolt's top support issue is the large multi-part prompt [P B33].
18. **Secrets never sit in the transcript (Base44, Replit).** Detect a pasted key, store it, and replace it with its name, as Base44 does. Ask for keys through a dedicated card, as Replit does with `ask_secrets` [P A10][U L1].
19. **Permission tiers for commands, with a team policy (v0).** Use Ask/Auto/Full plus `allow/ask/deny` patterns, with the most specific winning and compound commands taking the most restrictive verdict [P V8]. Base44's version for data: anything that widens data access always asks, even under auto-approve [P A3]. Map both onto Lightbulb approval cards.
20. **Let other agents drive the same loop (Base44, Anything, v0).** Base44's **Send to Coding Agent** and sandbox API, Anything's CLI "built for… AI coding agents", and v0's Platform API with an embeddable `demoUrl` [P A9][P Y5][P V15]. `lightbulb app` already is this. Keep every Forge action a CLI command so a laptop Claude Code user gets the same gates (recs 1–3) and the same checks.

**What their UX hides**, in one paragraph: the "it just works" preview is a state machine with a step budget (bolt.diy), a model router (Replit Free/Auto, Anything per step), a billing meter per checkpoint (Replit) or per fix (Bolt, v0), and a stage split nobody explains until production data goes missing (Replit's "Reservations save in Preview but not on the public URL" [P R13]). The best products make each of those visible exactly once, at the moment it matters.
