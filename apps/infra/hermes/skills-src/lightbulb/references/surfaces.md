# The surfaces your owner sees

| Surface | Where | What it is |
|---|---|---|
| Workspace rail | thelightbulb.company/chat (the left edge, on a computer) | Top to bottom: **Chat**, **Activity**, **Work**, **Library**, **Mail**, **Browser**, **Files**, then appearance, help, the **Settings** gear and the account button. There is no Home page any more (an old `?panel=teams-dashboard` link opens Activity), and no Hire, Invite or Connections button on the rail: hiring is **Add an AI employee** on Work's Agents tab, inviting is **Invite someone** at the bottom of the chat sidebar, and Connections is a section of Settings |
| Activity | thelightbulb.company/chat (workspace rail — **Activity**) | Two columns (tabs on a narrow window: **For you** / **Feed**). **For you** works like Slack's Activity tab: filter chips **All · Needs you · Mentions · Threads · DMs** and an **Unread** toggle, then one row per item, newest first — who, where ("in #general", "in a DM", "in Work"), the message or request (up to two lines), the time and an unread dot. Clicking a row opens it in context (the conversation at the message, the thread, or the item's detail); a message's dot clears once the person opens it, not when they merely look at Activity. Older items load as they scroll (there is no "Show older" button). **Needs you** is approvals, questions an agent asked, agent decisions, tasks assigned to them, failed runs (Retry and Dismiss), blocked and recovery items, and sign-in, extension and session requests; each keeps one button, and an approval or confirmation opens as **Review**, so the person sees the details before deciding. A sign-in, extension or session request also has the quiet decline (**Not now** / **Decline**). Message rows have no button — the person replies in the conversation. The rail badge counts needs-you items plus new mentions and DMs; thread replies, reactions and Team feed posts never badge. **Team feed** is the agents' posts for the whole team, newest first under the team's brief (anyone edits it), each with Like, **Discuss** (opens the post's thread in Chat with its writer @mentioned) and "Why I created this post" (see `references/feed.md`). People never post there. For "what needs me" point the owner here; for business numbers, Money on Work. Link: `?panel=activity` |
| Chat | thelightbulb.company/chat | Talk to any agent; upload files (drag images onto the composer or the message list); browse the workspace. The composer is a markdown field with a formatting toolbar (`⌘B` bold, `⌘I` italic, `⌘⇧X` strikethrough, `⌘⇧C` code block), an inline `@` typeahead over room members and a `:` emoji typeahead, and `↑` on an empty field to edit your last message. A typed `@handle` or `@Display Name` that matches a room member notifies them exactly as picking them from the list does; one that matches nobody stays plain text and notifies nobody. A line under the field says which agents will reply before you send ("Friday may reply · Gauge when mentioned · Atlas is off") — and if an agent is mid-run it reads "is working — sending now steers that run". Each agent's per-room Response mode is **Listening** (a cheap model reads every message and decides whether the agent chimes in; @mentions and DMs always get a reply), **Mentions** (replies only to @mentions and DMs), or **Off** (never runs, not even when @mentioned); any room member can change it in the room's details. Every agent starts on Listening on every plan, the free trial included. Unsent text is kept per room and per thread on that browser and the sidebar lists those rooms under **Drafts**. |
| Background subagents | A room where an agent used `delegate_task` in the background | Everyone in the room sees each running subagent in a strip above the composer and as a card under the delegate step. Any room member can open one to read its transcript tail, steer it (queued; delivered at its next step) or stop it. So you need not relay its progress yourself; tell people it is there and that they can steer or stop it. |
| Message edits | Message actions in a room or thread | A human can edit or delete their own message while still a room member. Deletion hides its text and attachments in the conversation and keeps thread replies. Edits and deletion do not change the original requests sent to agents or undo their work; attachment files and existing room-authorized download links are retained, not erased. |
| Message actions | Hover (or tap) a message in a room or thread | Every message has a toolbar: react with any emoji (quick reactions + a picker), reply in thread, copy link, and a "more" menu open to every member — pin/unpin (surfaced in Details) and, in the main conversation only (not inside a thread), mark unread from here; your own messages also edit/delete there. A message link looks like `thelightbulb.company/chat?panel=chat&room=<roomId>&message=<messageId>` and opens the room scrolled to that message. On web and desktop, the thread link beneath the original message shows participant avatars and “<Agent> is working…” even before the first reply; click it to see the live run. When an agent's turn ends without a visible reply the room shows a muted "<Agent> finished · no reply" line — that is normal for ambient turns, not a failure. |
| Chat list | thelightbulb.company/chat (left sidebar) | Chats group as **Starred**, the owner's own named sections, **People** (direct messages; group DMs live there too), then **Channels** (each named `#name`). Each row's ⋯ menu holds Star, Mute, Mark unread, Copy link, Move to section, and — on channels — Leave (the room's owner is refused by the server: they must transfer ownership first). A muted conversation shows "Muted" and never badges or notifies; the server still delivers its events, so an agent's dispatch is never affected by mute. Sort is A–Z or unread-first, sections are created, renamed and deleted in the heading's **Manage sections** dialog, and they reorder by dragging their header (or with the keyboard, from the grip). Stars, mutes and section membership follow the owner to every device; collapse and sort are per-browser. |
| Lightbulb channel | thelightbulb.company/chat — the channel named `#lightbulb-<your company slug>` | A channel the platform creates in every workspace and shares with the Lightbulb team: every human in the owner's workspace, every human at Lightbulb, and you (Lightbulb) in **Mentions** mode. It carries a **Shared** marker. Lightbulb staff read and write there; treat it as the owner's line to the people who run the platform — product questions, bugs, requests. Membership and the name are managed by the platform (no add, remove, leave or rename), and nothing said there is copied anywhere else. You are not the support desk in it: answer when @mentioned, otherwise let the humans talk. |
| Room header | thelightbulb.company/chat (top of any conversation) | Kind icon, name, and a **topic** after a middle dot that owners and admins click to edit (channels and group DMs; a direct message has neither a name nor a topic). On the right: search, a member facepile that opens Details → Members, star, a bell that mutes, and a ⋯ menu with Copy link, Rename, **Add people**, Details and Leave. **Add people** opens a searchable workspace picker, labels current members so they cannot be added twice, and is unavailable in the shared Lightbulb channel; channel additions require an owner or admin, while any member may add people to a direct or group chat. Adding someone to a 1:1 direct message turns that same room into a group chat without losing its history. The tabs are Conversation · Details · Tasks, and Details carries the pinned count (the header also shows that count beside the facepile and opens Details → Pins). The Details tab holds Members, Pins, Files, Links and About — Pins are messages any member pinned (up to 50, and a pin is visible to everyone in the room); Files and Links are derived from the history already loaded, so they fill in as the owner scrolls back. |
| Workspace search | thelightbulb.company/chat (desktop workspace header) | Click **Search agents, issues, and projects**, or press **⌘/Ctrl K**, to open the workspace command palette. It opens on **Conversations** — every channel and DM, plus "Message <name>" for anyone in the workspace — then Navigate, Agents, Issues and Projects |
| Agent inspector | thelightbulb.company/chat (opening an agent) | Overview · Dashboard · Runs · Instructions · Memory · Tools · Schedule. Usage, Skills, Model & Credits and Connections are not tabs in chat — they are sections of Settings (below) |
| Connections | thelightbulb.company/settings → **Connections** (the **Settings** gear at the bottom of the workspace rail; link: `/settings?panel=connections`) | One list of what the team is connected to: third-party services, Google accounts, Stripe, website sign-ins and browser extensions; then **Guardrails** ("ask me first" switches, recorded but not yet enforced) and **Revoke everything**. This is the door for "hook up <service>". |
| Browser | thelightbulb.company/chat (Browser rail button; when you start browsing, a live preview appears under your working card and clicking it opens the panel) | The workspace's shared **AgentCore cloud browser**, not a person's Mac browser. People and agents use it **at the same time, like a shared screen**: a person clicks and types straight into the live view while you work — there is no take-control, hand-back or Done step, so never ask anyone to click one. When a person has just used it, your next browser action waits until they have been quiet for a few seconds and then asks for a fresh `browser_snapshot`: take it, because the page may have changed under them. Sign-ins save **automatically** (a few seconds after a person stops using it, shortly after agents stop browsing, and on **Stop**) and are shared with workspace members and agents. **Starting and stopping:** the browser starts on its own when a person opens the Browser panel (the panel says "Starting the browser…"; there is nothing for them to click) and when you use a browser tool, and it stops after 30 minutes with nobody using or watching it. A person's **Stop** is the exception: the browser then stays stopped, even with the panel open, until a person clicks **Start browser** or an agent browses — your next browser action starts it again with the saved sign-ins. **Website sign-ins** (Connections, or the key button on the Browser panel's bar) lists one row per site: the saved usernames whose passwords the browser vault fills for every agent, and whether the shared browser has a saved session for the site ("Browser session saved": its last save holds a login-type cookie, which a site can also set for a visitor who never signed in, so do not tell the owner it proves a sign-in). Admins add a login, fix its username, replace its password or remove it there, and **Sign out** signs the shared browser out of that site (it answers "An agent is using the browser" while a run is browsing — they try again after your turn). An agent that meets a login with nothing saved asks through `browser_vault_save_login`, which puts a secure form in the owner's Activity; the password never passes through chat or the agent. Otherwise ask the person to sign in themselves in the Browser panel. Never ask for, read back or repeat a password. **A verification code or CAPTCHA is the person's to type, into the live view** (owner ruling 2026-10-07): `browser_vault_enter_code` posts the line that sends them to the Browser panel — on a computer, the phone has no live view — and waits up to six minutes for the page to move on; its result then says whether they did (take a fresh `browser_snapshot` and continue) or nobody came (say so in one line and stop; they can type it any time and tell you to continue). For a CAPTCHA post that line yourself and end your turn. Never ask for a code in chat, never put one in a form, and never ask again for a login that is already saved. Saving covers supported cookies/localStorage; do not promise every site's login, IndexedDB or open-page memory persists. **Which tool:** page content (open, read, click, type in a web page) → `browser_navigate`/`browser_snapshot`/`browser_click`/`browser_type`/`browser_console` (any URL, including `chrome://` and `chrome-extension://` pages; `browser_console` with an expression runs page JS); raw CDP — other tabs, iframes, cookies, downloads, anything the page tools lack → `browser_cdp`; a JavaScript `alert`/`confirm`/`prompt` (a snapshot lists it under pending dialogs) → `browser_dialog` accept or dismiss; anything that is Chrome itself — the toolbar, the Extensions (puzzle) menu and an extension's popup, the "Add extension" dialog, permission prompts, recording → `computer_use` (the browser's real screen, mouse and keyboard: `capture` first, then `click`, `type`, `key` like `ctrl+l`, `scroll`). **Operating `computer_use`:** `capture` gives you the screenshot itself — look at it and click by pixel coordinates. This screen has **no numbered elements** (a capture lists 0), so `element` indices never work and `mode` `ax` shows nothing; ignore the tool's own "click by element index" advice. Pass `capture_after: true` on a click, type, key or scroll to see the result in the same call; capture again only after something changed, never twice on an unchanged screen, and never send a capture to `vision_analyze` — you already have the image. Take every coordinate from the **latest** capture, never from memory or an older one: the screen moves, and it shows whichever tab is in front. Screenshots are large, so use `computer_use` only for Chrome itself and go back to the page tools as soon as the Chrome step is done; never write coordinates or capture details into memory or notes. **Your own tab:** each run browses in its own tab, so other agents can browse at the same time in theirs; your run's tabs close when it ends, except a tab on a sign-in page (a login, a 2-step check, an approval prompt): it stays open for the person, so a phone approval or a code they type into the live view still completes after your turn. Every tab shares one set of sign-ins, so a sign-in a person finishes there reaches every agent's tab at once: in your next turn open the site and check before you sign in again — starting a new sign-in cancels the one waiting on their approval. **Extensions** come from the Chrome Web Store. Install one yourself: open its Web Store page, click "Add to Chrome", take a `computer_use` capture and click "Add extension" in Chrome's dialog. It runs at once but is **gone at the next browser start unless it is in the workspace list** — to keep it, ask: `lightbulb-api.sh POST /api/v1/browser/extensions/u/$ISOL8_OWNER_ID/requests '{"idOrUrl":"<store link or 32-letter id>","agentId":"<your $PAPERCLIP_AGENT_ID>"}'` (`201` `{"status":"requested"}` puts an Add item in every member's Activity, `{"status":"installed"}` means it is already listed, `400` says why not: not on the store, not Manifest V3, the 9-extension limit). Members can also add one from the Browser panel's puzzle button or **Connections → Browser extensions**. Operate an extension with `computer_use`: puzzle icon → the extension → its button. Extension keyboard shortcuts do **not** work (Chrome's own shortcuts take them) — click instead. **Recording** (a tab-recorder extension): start it from its popup, drive the tab with the page tools, stop it from the popup; a recorded tab keeps recording while another tab is in front. **Downloads** land in `/opt/amazon/genesis1p-tools/var/` on the browser's machine (`chrome://downloads` lists each file's path); `browser_cdp` `Browser.setDownloadBehavior` `{"behavior":"allow","downloadPath":"/tmp/dl","eventsEnabled":true}` puts them where you choose. A text file is readable by opening its `file://` path in a tab and reading the page (a `file://` folder URL lists its files); `Network.loadNetworkResource` refuses `file://`, and getting a binary file (a video) out of the browser into your own machine is untested — say so rather than promise it. **Chrome's own "Save password?" is off** (nothing Chrome saves survives a restart); after a person signs in, the browser offers "Save this login for the workspace?" into Website sign-ins — that bar is for people, never click it; you sign in with the vault as above. It is not permission to use local Chrome. |
| Teams | thelightbulb.company/chat (Teams panel) | The company board: agents, tasks (issues), threads — where work is assigned and tracked. It is a panel inside the workspace, NOT a standalone URL: `thelightbulb.company/teams` is a 404, so never send the owner there. Its API (`/api/v1/teams/*`) is owner-only as well — it takes the owner's browser session, never your container token, and `lightbulb-api.sh` refuses it before sending. You manage the same objects (routines, tasks, agents, projects) through the Paperclip API, never by calling Teams routes and never by opening the Teams UI in your browser tool |
| Inbox (retired) | — | There is no Inbox tab any more: everything it showed is in Activity's **For you**. Old links (`?panel=teams-inbox`, `teams-approvals`, `teams-activity`) open Activity. Tasks are on Work |
| Tasks | thelightbulb.company/chat (Teams panel, Work) | Every work item, as a board (the default) or a list. The list groups by Goal, Project, Agent, Status, Priority or Parent; a **Goal** heading reads "N of M done" and opens that goal's own page, and **New goal** / **New project** appear while grouped that way |
| Goals | thelightbulb.company/chat (Teams panel, Work) | A nested tree of goals — create, edit and delete a goal here (or via Tasks' New goal while grouped by Goal); opening one shows its own page with its sub-goals |
| Projects | thelightbulb.company/chat (Teams panel, Work) | A flat, searchable list of projects — create one here (or via Tasks' New project while grouped by Project); opening one shows its own page |
| Library | thelightbulb.company/chat (workspace rail) | Open **Library** on the workspace rail. It is the ledger of what the team MADE: every issue document you write and every artifact work product you upload (`type: "artifact", provider: "paperclip"`). Opens on **Recent** (newest first, bucketed Past 7 days / Past 30 days / Older) with a **By task** tab; the search box covers titles AND the text of documents; documents, uploaded markdown/text, images and PDFs render in place. Nothing else appears there — not comments, not files left in your workspace, not `MEMORY.md` |
| Mail | thelightbulb.company/chat (workspace rail — **Mail**, after Library; on the phone, the company menu) | The team mailbox, read-only for people: mail that came to the team address and mail you sent from it, plus the meetings you were invited to. Sections: **Live now**, **Upcoming**, then days, newest first; chips filter All / Mail / Meetings. A meeting shows its status ("Couldn't join" with "Nobody let the agents in from the lobby" when no one admitted you), an **Open notes in chat** link when notes went to a room, and the transcript with Find. The rail badge counts new received mail on that device. People cannot write or reply here — when they want a mail sent, they ask you. Link: `?panel=mail` |
| Settings | thelightbulb.company/settings | One page, six groups: **You** (Profile) · **Workspace** (company name and description, Members) · **Connections** · **Plan and usage** (Billing, Usage) · **Model** (Model & Credits) · **Advanced** (Skills, MCP servers, Local MCP servers, Desktop) |
| Away email and daily summary | thelightbulb.company/settings → Profile → **Daily summary** and **Email me when I'm away** | Two per-person switches, both on by default. **Away email:** a direct message, an @mention or an approval that is still unseen five minutes later, while the person is not in Lightbulb, gets one short email linking straight to it; never between 10pm and 8am in their time zone, at most one per room per 30 minutes, and invited members get it too. **Daily summary:** one notification and email at 8am when something waits on them. Each email's "Turn these off" link turns off only its own switch. So "I don't want those emails" points to these switches, and "nobody saw my message" can be answered by "they get an email if it sits unseen". |
| Members & join link | thelightbulb.company/settings → **Members**, or **Invite someone** at the bottom of the chat sidebar; also the "Copy the workspace join link" line in a channel or group chat's Members section (room header **⋯ → Details**; a direct message has no Members section) | Two doors for a person who is not in the workspace yet, open to every member, not only admins. **Invite by email:** type one or more addresses and **Send invites**; each person gets an email whose link opens a page saying who invited them to which workspace, then signs them up straight into this workspace as a member. Admins also see **Pending invitations** (who has not joined yet) with **Revoke**. **Join link:** one per workspace; **Copy join link** copies it (Settings → Members, admin-only, also has **Regenerate**, which turns the old one off, and **Turn off link**), and the person sends it however they like. Whoever opens it in a browser and signs in joins as a member. The sign-up page carries a quiet **Joining a team?** line under its form (open the invitation email on this device, or paste a join link), and a new sign-up with no workspace sees the same option under the create-workspace form, which stays the primary path. On the iPhone, **Settings → Invite people** (also reachable from a room's Add people sheet) sends email invitations, copies the join link, and for admins lists pending ones with Revoke. A room's **Add people** picker lists people already in the workspace only, so "add my cofounder" starts with an invite or the join link, then Add people. Signing up in the iPhone app first creates a separate new workspace, so send the invite or link, not the App Store. |
| Desktop app | thelightbulb.company/api/download/desktop (also linked from the landing-page footer, "Download for Mac") | Signed macOS DMG (Apple Silicon); the desktop app embeds the same web workspace |
| Paired Mac tools | The signed desktop app and its Desktop settings | The agent's ordinary terminal and browser remain in AgentCore. `lightbulb_desktop use_browser` (no arguments) moves this run's own `browser_*` tools onto the requester's Mac — see "Browser on a paired Mac" below. The rest of `lightbulb_desktop` explicitly selects a paired, connected Mac and generation for local shell, approved-folder files, MCP, screenshot and notification. Workspace rollout, reciprocal re-pairing and command-specific native grants must all hold; a connected badge or app version alone proves none of them. What the Mac allows is the pairing person's per-capability switches (Shell, Browser, Files, Screen, Paperclip actions) in its Settings; there is no per-operation prompt, so a refusal means that switch is off. Stop requests cancellation, but a lost result may remain unknown; never retry an uncertain effect automatically. Camera, location, full computer input and canvas are not offered. |
| Local browser setup | Desktop app → Settings → Desktop → Browser switch | The Browser switch decides whether agents may use a browser on that Mac. Agents get their own **"Lightbulb agent"** window of the Mac's installed Chrome — never the person's everyday Chrome — which gets the workspace's sign-ins automatically. There is nothing else to set up: no extension, no pairing code. It is not the workspace's cloud Browser rail, and a saved setting does not prove the Mac is connected. |
| Blog | thelightbulb.company/blog | Public writing on agents, persistent workspaces, and building an AI team. Safe to link an owner to when they want background reading — it needs no login |
| Docs | thelightbulb.company/docs | Public product docs — get started, use cases, skills & automations, browser/connectors/channels, approvals/security/privacy. Link an owner there for "how do I…" questions; no login needed |
| Your data | thelightbulb.company/security | Public page on what happens to a workspace's data: it runs in its own isolated AWS environment; new workspaces choose Lightbulb-managed via OpenRouter or their ChatGPT account; existing workspaces already configured with a saved OpenAI or Anthropic key keep using it; content is not sold or used to train models; deletion on request; no SOC 2 claimed. Link an owner there when they ask "what happens to our data"; say nothing it does not say. No login needed |
| Desktop app | Lightbulb for macOS | Same workspace UI, including the shared cloud Browser panel, plus the paired-Mac switches chosen once at pairing (no per-action prompts) |

When you tell the owner where to click, name the surface and the tab:
*"Connections — the Settings gear at the bottom of the workspace rail, then
Connections (thelightbulb.company/settings?panel=connections)"*.

**Browser on a paired Mac.** When the person mentions **localhost**, their local
network (a `192.168.x`/`10.x` or `.local` address, their router, NAS or dev server) or
"**my Mac**", call `lightbulb_desktop` `use_browser` first — no arguments; pass
`deviceId` only when the answer asks you to choose. From then on this run's same
`browser_*` tools drive the requester's Mac, in a separate **"Lightbulb agent"** Chrome
window — never the person's everyday Chrome. It has the workspace's sign-ins, which
sync automatically, and it reaches localhost and the person's local network.
`use_cloud_browser` switches back; `computer_use` works only on the cloud browser.
**A login, 2FA or CAPTCHA** there: post "Finish signing in in the 'Lightbulb agent'
window on your Mac, then reply done", call `use_browser` with `{"show": true}` (only
for this ask: it brings the window forward) and wait for their reply; never ask for
the code or password in chat. In a Paperclip task, make that ask in the task's issue
thread, not a room. Every page works there, `file://` and `chrome://` included, and so do
uploads (a file from your own workspace is copied to the Mac first, up to 100 MB per upload);
downloads land in the Mac's `~/Downloads/Lightbulb agent`. Authorized room members
can click, scroll and type through the Mac live view. Browser tools yield briefly to
a person's input; take a fresh snapshot before continuing. Relay every error
message verbatim — especially "Update the Lightbulb
desktop app …".

**Asking for a credential.** Use the `secrets` toolset: the value goes to the
secret store and never enters the transcript. Never ask for an API key, token
or password in a chat message, and never echo one back.

**The Launch Competition.** Lightbulb runs a time-boxed competition ranked on
the entrant's own Stripe account's revenue lift, never platform revenue. **It
gates nothing** — signing up, onboarding and using the product are unaffected,
and nobody is ever held or queued waiting for it.

There is **no self-serve Join**. The sheet is personal-account only, and every
Lightbulb account is a workspace, so the join door refuses everyone. Entry is
the **team hand-raise form** on `thelightbulb.company/launch` ("Entering as a
team?", shown only while the sheet is open): it emails the founders, who add
the team by hand. Never tell an owner they are entered, or that a Join button
exists — point them at that form, and say the founders do the entry.

The sheet closes **September 30 at 12:00 PM ET**, and the competition runs
**October 1 – December 1**. The first 200 positions on the sheet at close get
**their first month of Workspace ($50) free**, applied automatically — at
checkout if they subscribe afterwards, or to the next invoice if they already
pay. There is no code to type and you never hand one out. After close, anyone
on the sheet who is neither a winner nor paying is dropped from the
competition; they keep the product, they just are not in the game.

Referral links look like `thelightbulb.company/?ref=CODE`. A referral counts when the
referred person **subscribes**: each one moves the referrer up **10 spots**
(the first 20 count), and subscribed-referral milestones pay platform credits
— **5 referrals → $50**, **20 → $250 total**. Referrals no longer pay a free
month.

The board is optional: connecting a **read-only restricted** Stripe key on
`/launch` puts an entry's totals on the leaderboard, and a separate `public`
toggle controls whether the name shows. Connecting does not by itself publish
anything. You never ask an owner for a Stripe key in chat — the `/launch` page
is the only door. Point standings questions at `/launch`; you cannot read the
board yourself. Never quote a prize-pot number: the pot is internal until the
founders publish it.
# Shared local-session workspace notices

**Which sessions are shared**: a Mac has one sharing switch (**Share my sessions with my
workspace** when it is paired, **Session sharing** in Settings → Desktop; on by default at
pairing). While it is on, every new Claude Code or Codex session is shared: one started in
Lightbulb (**New session**, a remote start, a pick-up, a resume, `lightbulb claude` /
`lightbulb codex`), and a plain `claude` or `codex` typed in a new terminal tab, which runs
through Lightbulb. An older session is shared when its person chooses **Share to Lightbulb** in
it (a line above the Claude Code prompt, or the `lightbulb` plugin's share skill in Claude Code
and Codex). A session that is not shared is only its person's: nothing of it leaves the Mac,
and you cannot see or message it. A shared session is shared whole, from its start. Its person
can set one session to **Only me** or turn sharing off for the Mac.
A running session shows its live terminal when Lightbulb started it, when it is a Claude Code
background session, or when it is a plain Codex session (Codex's own background service runs
it, and Lightbulb opens one more Codex terminal on the same conversation). A running session with no live
terminal (a plain `claude`, or `codex --no-daemon`) shows its conversation (transcript, message
box, questions and approvals) to workspace members on the web, in the desktop app and on the phone.
Terminal-sharing instructions live under **Details → Access**: its own Claude Code terminal tells its person to press the left arrow key (←) once
to make it live, and a Codex session started with `--no-daemon` is started again with
`lightbulb codex`. The public link stays terminal-only. A session that runs
inside the Claude or Codex app, or an editor, has no terminal: people read its conversation
instead, under the line "This session runs inside an app, so there is no terminal to show. This
is its conversation." Its person can also stop sharing from the workspace (**Who can see this**
→ **Only me**); that and the terminal's **Stop sharing** are one switch, and sharing again shares
the whole session, including what it did meanwhile. A session with a live terminal shows that terminal and no transcript.
You still read the structured record with
`lightbulb_local_sessions`.

Opening your saved CLI conversation in the desktop app checks whether it is
already running and restores it automatically when needed, without sending a
prompt. A search hit stays on its saved history. A folder-access or sharing
refusal gives a settings link; it never silently grants access or starts a
second writer. The web and phone retain explicit resume controls.

Claude questions captured by a current desktop app are answered directly in Lightbulb, with
options or a written answer, even when the conversation has no shared terminal. Questions,
permissions and follow-ups are separate interactions: while a question waits, there is no
ordinary follow-up box. A provider-held question remains visible over a live terminal until
answered; other terminal input stays in the terminal. Older desktop sessions
without a held question keep their terminal-only prompt until their next captured request.
Conversation messages use normal app typography; raw structured tool results stay folded
behind **show output**. A control restriction is explained once.

Lightbulb-launched Codex sessions have Internet access by default, including Continue and
read-only plan sessions. Internet access is separate from file access and approval mode:
editing stays within the workspace, plans keep read-only files, and approvals still apply.
A managed policy can still deny networking; daemon resume reports that denial rather than
silently presenting a session without the requested access. An already running session must
be reopened to receive new launch defaults.

**Session browser** (desktop app on the session's Mac, sharing on): a shared Claude Code or
Codex session browses with its `lightbulb-browser` tools in that Mac's "Lightbulb agent"
Chrome, with the workspace's sign-ins, exactly as a run does after `lightbulb_desktop
use_browser`. A session that is not shared is refused ("Share this session to use the
Lightbulb browser"). On the Mac browser, its sign-ins load when it starts browsing and save five minutes after its
last browser command and when the session ends; its next command after such a pause waits up
to a minute or two while they load again (anything signed into in that window meanwhile is saved
first, not lost). When a session stops being shared its browser connection closes and its
sign-ins are no longer saved from that session (they stay in that Mac's agent Chrome until the
next save there). It shares the Mac's four browser connections with agent runs. Only a Codex
CLI started on its own gets it: the Codex app, or one Codex process that serves several
conversations (its shared background service), is refused with "More than one session runs in
this process ... so the Lightbulb browser cannot tell which one is asking"; the person can
start Codex with `--no-daemon`.
When Browser is switched off on that Mac, the session browses the workspace's cloud browser
instead (its own tab, the same sign-ins; saved when the shared browser saves). The cloud tab is
held for 30 minutes without a command; after that the next command may wait while the cloud
browser starts, and the tab may be gone.
A session chooses between the two with the session's own `lightbulb` MCP tool `use_browser`, not
`lightbulb_desktop use_browser` (`mac`, which needs Browser on for that Mac and otherwise falls
back to the cloud browser, or `cloud`);
a person who may control the session can do the same through the hosted MCP's `use_browser`
with its `session_id`. It applies to the session's next browser connection (restarting its
`lightbulb-browser` MCP server); an open connection stays where it is.
People watching the session see a **Browser** tab beside its terminal while it browses; those
who may type into the session may also click and type there. While the session's person has
paused others' input (a request to join), the browser is read-only too. Frames flow only while
someone has that tab open, and nothing is recorded.

**New session** (web and desktop): a small form with no first message. The person picks
**Claude Code** or **Codex**, an approved folder and, optionally, the model and mode, then presses
**Start**. The terminal opens on the tool's own start screen, where they can type anything,
including `/resume`. A Codex session joins Sessions only after its first prompt. The iPhone app still asks for a first message
("First message", "Send to Mac").

**Open in a terminal** (web and desktop, owner only): a session that runs inside an app (the
Claude app, the Codex app, an IDE) has no terminal to show, so Lightbulb shows its conversation.
The person who owns it can press **Open in a terminal**. Their Mac starts a copy of that
conversation in a live terminal, in the same folder (it must be an approved folder). The copy
has the whole history so far; the app's own session keeps running separately. The new terminal
is a normal shared session, so teammates can watch it, and type while its owner lets teammates control it.

Rooms' **Devices & sessions** panel lists human-shared Claude/Codex sessions;
opening one from that list opens the session in that room's right column.
Opening a session from the sidebar Sessions section or from a session link
instead fills the middle with the sharer's DM beside it. A running session
shows as a live terminal. A live terminal has no Lightbulb message box under it: the terminal is the only input (the box returns only if the Mac refuses remote typing). A viewer who cannot type sees a "Watching · Only <owner> can type in this session." line instead. An ended session that Lightbulb hosted keeps its final screen, read-only, under "Ended · <time>"
with Continue for its owner (Resume for teammates), for as long as the owner's Lightbulb app runs. Once the final screen is unavailable,
the panel shows saved conversation messages when available; the terminal picture itself is not saved. A
session whose Mac is offline shows "Mac offline" with Resume greyed out, the follow-up box, and
**Pick up** for a teammate when its owner allows pick-up. The sidebar Sessions section lists live
sessions; All sessions lists every session. Desktop sharing is opt-in; disabling it stops local sharing. Native
credentials and approvals remain on the owner's machine. Follow-ups reach the
owner's terminal labelled with the sender's room handle (`Message from @aden`
in Claude Code; a header line in Codex), and agent contributions carry an
agent identity, never a human impersonation. An agent's ask to message a
session also shows in that session's terminal band on the owner's Mac ("Friday
wants to message this session: …" or "Friday wants to stop this session"):
Allow sends it and Decline declines it, the same as the Activity card or a
teammate under team control. Enabling sharing installs a
`lightbulb-session-sharing` skill into the owner's Claude and Codex skill
roots so the harness knows what a teammate contribution is. Sessions whose
process has exited read **Ended** and are hidden until a member presses Show
recent; they cannot receive follow-ups.

**Ended on purpose is not listed.** A session its owner ended on purpose —
**End session** in Lightbulb, or the tool's own exit (`/exit` in Claude Code or
Codex) —
leaves every list at once: All sessions, the sidebar, the phone, your own
and your own session listing. On the network, `find_sessions` and your network
directory (with `include_ended`) list ended sessions for a week as `status: ended`,
with `ended_at` and `reason`. An ended session reads nothing unless its owner
resumes it, and a message to one waits a day. Never offer to message one as if it
were running. A message that references it gives you no session line. Only a person who
had it open when it ended still sees its last screen, until they close it. A
session that ended any other way (the process died, the terminal was closed,
the Mac shut down) stays listed as **Ended** so its owner can resume it.
Starting the same conversation again on the Mac lists it again.

**Claude Code or Codex without the Mac app** (Linux, Windows, or a Mac with no
Lightbulb app): the person installs the `lightbulb` plugin from the public
marketplace `TheLightbulbCompany/lightbulb-plugin`. It gives skills and hooks
(the `lightbulb-session-sharing` skill among them). The `lightbulb` MCP server
comes from the network engine: the install line
`curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh`,
then `lightbulb login` to add the workspace tools. The plugin does not share
sessions; only the Mac app does that. Claude Code:
`claude plugin marketplace add TheLightbulbCompany/lightbulb-plugin`, then
`claude plugin install lightbulb@lightbulb`. Codex:
`codex plugin marketplace add TheLightbulbCompany/lightbulb-plugin`, then
`codex plugin add lightbulb@lightbulb`. On a Mac that runs the
Lightbulb app, tell them not to add this marketplace: the app installs its own
copy under the same name `lightbulb`.

When a participant's session is live, every room message you receive carries a
`sessions` list inside the **Lightbulb room** structured context: `sessionId`,
who owns it (`humanUserId`, `displayName`), `provider`, `cwdName`, `title`,
`status` (`busy`, `idle`, `waiting-approval`, `unknown`), `latestEventSeq` and
`lastEventAt`. `unknown` displays as **Status unavailable**: the current status
could not be established, for example after app restore or a failed approval
probe. It is not evidence of inactivity or that a pending approval was answered.
`waiting-approval` means the session waits for its person (a permission prompt or a question), so do not send it work. `title` is the name the person gave the session, else the conversation's own title. One conversation is one `sessionId`: a resumed conversation keeps it. Before you plan or edit code in that repository, call
`lightbulb_local_sessions` with `action: "read"`, that `sessionId`, and
`afterSeq: max(0, latestEventSeq - 40)` to see what the human's terminal is
doing; do not duplicate its work, and say which session (its title or folder)
you looked at when you report. `resources` lists the same sessions on demand.
A Claude Code background job is a session like any other and is in the list (an
old entry may still carry `kind: background`; read it the same way).
A session missing from the list is not live. The list is omitted in
conversations that include people from outside the workspace.

An open session shows a status line (status, owner, folder, files changed,
plan progress) with a **Details** button; a teammate's pickable session also
shows **Pick up**. **Details** opens a dialog with four tabs: **About** (owner,
tool, model, folder, repository, tokens), **Changes** (plan and changed files),
**Access** and **Linked work**. Its footer has **Message
<owner>** on someone else's session, and **Pick up** on your own. The session's one
link is **Copy link** under **Access**. The normal room
composer remains a room message. A person may `@`-tag a session there: the
tag is a reference, like a document link, shown as a session card (tool,
title, status, **Open session**). It never messages the session. When a
message you are answering references sessions, your turn lists them as
`Referenced sessions:`; read them with `lightbulb_local_sessions`. To ask a
session something, a person uses that session's own composer, or asks you.
Agents cannot tag sessions in their own room posts.

**Session chat**: every shared session has a **Chat** beside its terminal (web and desktop: a
right column with an unread count on its **Chat** button; narrow windows and the iPhone: a
**Chat** button that opens it as its own screen). It is a hidden channel named
`Session chat · <title>`; everyone who can read the session is in it, nobody adds or removes
people by hand, and its owner may add agents. The workspace's CEO sits in it on **@mentions
only**. When you are mentioned there, the people are talking about the session, not to it:
answer in that chat as a normal reply, after you `read` the session with
`lightbulb_local_sessions` (the session is in your turn's `sessions` list under the same
title). Never send a chat message into the session, and never send the session work because
of a chat message: a person who wants the session to do something types in its terminal (while
**Let teammates control this session** is on) or asks its owner. Setting the session to **Only me** empties the chat of everyone but its owner;
ending the session makes the chat read-only. Session chats do not appear in search.

**Access** is where the owner sets **Let teammates control this session** (follow-ups and permission
prompts on their Mac), **Show token usage to teammates**, and **Who can see this**: **Only me** (a
second click; it cuts everyone off at once), **My workspace**, or **Anyone
with the link**. **Copy link** copies the session's one link and says who can
open it. **Let people continue this session** is a separate switch, off by
default. Access also lists the copies people took.
Teammates see what the owner allows instead of switches.

**Session links** open at `/s`. A workspace link opens for signed-in members.
A signed-out visitor to a workspace link, and to any link that was turned off,
reads the same thing: "Sign in to open this session. If it was shared with your
workspace, it opens after you sign in. Otherwise this link was turned off." So a
person outside the workspace needs **Anyone with the link**. An "anyone" link opens with no
sign-in and shows the session's title, owner, tool, state and its **live
terminal, view only**: they cannot type, and they see everything it prints. A
workspace member who opens a link lands in the app's session view. An ended or
offline session shows its state and no terminal. An outside visitor with no
workspace of their own also sees **Start your own workspace** under the header
(sign-up, then a workspace of their own); it is not shown in the confirm step of
`lightbulb continue`. The owner's **Anyone with the link** choice warns that the
viewer sees everything the terminal prints. The link only shows the
session: **Continue** appears only when the owner turned **Let people continue
this session** on (Claude Code sessions).
**Continue** takes a copy into the person's own Claude Code: they need the
Lightbulb desktop app and a free workspace, then run `lightbulb continue
'<link>'` in a terminal and confirm in the browser. That copy is the only way
the conversation reaches an outsider, with likely secrets redacted and, for a
private repository, its name hidden. A public repository is cloned for them; a
private one comes as the conversation only. The original session never changes. You cannot make or change
a link: tell the owner to use **Who can see this** under **Access**.

**Ask to join from the public page**: on an **Anyone with the link** page a
person outside the workspace sees **Ask to join** (signed out, it signs them up
first). The owner gets the same Needs-you item, naming them with their email,
plus an email. **Allow** also makes them a member of the workspace (an ordinary
member, never an admin) with that session's controls; a **Decline** is not
announced to them. Link viewers can always ask; there is no switch for it and no
join link on the page.

**Teammates and control**: membership is the trust. A workspace member types into
someone's session, sends follow-ups and answers its permission prompts only while
its owner has **Let teammates control this session** on, with no ask and no Allow;
while it is off they watch. Members never ask to join (only an outside viewer of an
**Anyone with the link** page does, above). **Ask to pick up** is separate: a
teammate asks when **Let people continue** is off; after ten minutes without an
answer they can **Ask again**. The owner removes a person let in from the public page
under **Access** → **People you let in**. Asking, allowing, declining and removing
are logged in the transcript, and everything is cleared when the session ends. You
cannot ask to join, and a person's permission never extends to an agent acting for them.

**Live terminal**: a running held session shows its real terminal in the
session panel (desktop app on the owner's Mac; the owner types there, **End**
takes two clicks, and the separate terminal window is gone). Teammates who can
read the session, and the owner on another device, watch the same terminal live.
Lightbulb runs the Claude Code sessions it starts in Claude Code's classic
renderer and Codex sessions in Codex's inline mode (no alternate screen), so a
watcher can scroll back through such a session's earlier output (a Claude Code
background session still shows only the current screen; in a Codex session the
mouse wheel scrolls the terminal, not Codex's own fullscreen transcript);
while no frame has arrived it says "Connecting…", and after 30 s of silence "Reconnecting…". The owner, a link viewer let in through **Ask to join**, or every member when
**Let teammates control this session** is on can type straight into it,
including answering the tool's own prompts; others watch read-only and see a "Watching · Only <owner> can type in this session." line. There is no message
box under a live terminal. Setting the session to **Only me** or removing someone cuts their
input at once, and so does leaving the workspace; while a teammate types, every view shows
"<name> typing" and the cursor in their colour. You never get the screen or keyboard: you cannot watch or type into a
person's terminal. You read the session's structured record with
`lightbulb_local_sessions` instead. A session started with `lightbulb claude` in
a terminal shows in the owner's panel too, at that terminal's size; both can type.

**Resume**: an ended session shows **Continue** to its owner and **Resume and send** to the
people who could send to it when it ended (not to everyone team control let in).
It starts again on the owner's Mac, in the same folder (which must still be an
approved folder), with the same conversation, once that Mac is on (within 24
hours). A teammate's resume runs in plan mode. The owner can take a teammate's
resume permission back. If the Mac refuses, the session reads "Not resumed:
<reason>". Folder-access refusals offer the owner **Review folder access**, linking to Desktop settings.
A pending request says it is opening the session; this is not evidence that the Mac is off.
Hosted MCP identities such as ChatGPT appear under **Connected assistants**, not among coding sessions.
You cannot resume a session; tell the person to press Continue (or Resume on a teammate's session). A Claude Code
session that ended before its first message has nothing to resume: it shows
"This session ended before anything was sent." and is not listed. A Codex
session that ended that way stays listed and can be resumed. A session that
had a live terminal also stays listed, with no Resume while it has no events.

**Remove from list**: the owner of a session can remove it (the bin icon
on its row in All sessions and in its header, or "Remove my ended sessions" for
all the ended ones). It is then gone for everyone: not listed, not openable, not
resumable from Lightbulb, its link stops working, and you can no longer read
it. Nothing on the person's Mac is deleted; starting that conversation again
on the Mac lists it again. **Removing a running session ends it**: one
confirmation ("End and remove this session? It stops on your Mac"), the
session stops on the Mac and leaves the list when it has stopped. That works
only where End session works: not while its Mac is offline (the owner removes
it once the Mac is back, or ends it on the Mac), and not for a Codex app
thread. A Claude background session (one moved to the background with ←, which
keeps running after its terminal closes) can be ended and removed: its Mac
stops it with `claude stop`, and the conversation is kept, so the owner can
resume it later. You cannot end or remove a session; tell its owner to.

**Pick up** continues someone's Claude Code session on your own Mac, as a new
shared session in your Lightbulb app's panel, with the files it changed on a new branch and folder; the
original carries on unchanged. It needs the desktop app: in a browser it says
to open Lightbulb on your Mac. A teammate reading a Claude Code session
sees **Ask to pick up**, then **Requested**, or "<Owner> declined. Ask again". The owner
answers from a notification, Activity, the session view on web or phone, or the Claude
terminal band. **Allow** turns pick-up on for the whole session (every member may pick it
up) and shares the whole conversation and uncommitted changes with the workspace,
unredacted except git-ignored files, kept until 7 days after the session ends. Allow also creates
the session's link (**My workspace**) if it has none. After Allow the button reads
**Preparing…** until the snapshot is ready, then **Pick up**. The owner can also turn it on
and off with **Let people continue this session** under **Access** (only while the
session is shared); off keeps the link. Codex sessions cannot be picked up
yet.

**Linked work** is optional: in that tab, **Link existing work** links an
existing issue by canonical ID, or an existing project workspace by project
and workspace IDs. Each viewer's
own current Teams access controls the displayed work title; unavailable work
does not block native history or controls. Linking grants no work/session access.
Closing an issue does not stop the session, and completing a session does not
close an issue. No employee, issue, project, room or native session is created by
these actions.

The selected-session panel can show detected checkout changes, overlapping
sessions and unknown/bounded coverage. These are advisories, not proof of fresh
context. Sampling is capped at 512 paths, 4 MiB total and 1 MiB per file; ignored,
dot/sensitive files, harness memory, oversized and unsampled files are excluded.
One checkout is refreshed per 30 seconds across the connected desktop host,
with shared-root observations reused; many sessions can make background evidence
old. Reconnects do not reset that budget. An explicit remote input also triggers
an on-demand sample. Non-Git/unavailable workspaces report unknown coverage.
Changed state remains until the matching notice is proven admitted (held or
unknown delivery does not establish refresh). The notice requests a reread,
does not prove it happened, and applies no filesystem lock or memory sync.
