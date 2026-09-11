# The surfaces your owner sees

| Surface | Where | What it is |
|---|---|---|
| Home | flickmeon.ai/chat (Teams panel, the landing view) | The business first: the company name, a **Revenue** hero drawn as bars you can hover for the exact day and amount, beside **New customers**, **Visitors** and **Agent-spend** tiles. The 7d / 30d / 90d toggle governs every tile; Revenue, New customers and Visitors compare to the previous period when readings cover it, and Agent spend is the month's billed figure with no comparison. Revenue, New customers and Agent spend each carry **Ask Friday** (opens your chat with a question about that number pre-filled) and open their Money tab; Visitors carries Ask Friday only when PostHog is connected, and otherwise links to Accounts to connect it. Quick actions: Ask (you), New task, Hire an agent, Add a product (their Stripe Dashboard). Then the top three things that **need the owner** (link: Inbox), what agents are **doing right now** (link: Tasks) and the latest **work in the Library**. There is no agent telemetry on Home any more — for run history and tasks point the owner at Tasks and Inbox; for money detail, Money. Agent spend reads "Not shown" on plans that aren't metered |
| Chat | flickmeon.ai/chat | Talk to any agent; upload files; browse the workspace |
| Message edits | Message actions in a room or thread | A human can edit or delete their own message while still a room member. Deletion hides its text and attachments in the conversation and keeps thread replies. Edits and deletion do not change the original requests sent to agents or undo their work; attachment files and existing room-authorized download links are retained, not erased. |
| Message actions | Hover (or tap) a message in a room or thread | Every message has a toolbar: react with any emoji (quick reactions + a picker), reply in thread, copy link; your own messages also edit/delete. A message link looks like `flickmeon.ai/chat?panel=chat&room=<roomId>&message=<messageId>` and opens the room scrolled to that message. When an agent's turn ends without a visible reply the room shows a muted "<Agent> finished · no reply" line — that is normal for ambient turns, not a failure. |
| Workspace search | flickmeon.ai/chat (desktop workspace header) | Click **Search agents, issues, and projects**, or press **⌘/Ctrl K**, to open the workspace command palette |
| Control Panel | flickmeon.ai/chat (right-hand panel) | Tabs: Overview, Agents, Skills, Sessions, Channels, Usage (org admins only), Model & Credits, **Integrations**, Skill Proposals |
| Teams | flickmeon.ai/chat (Teams panel) | The company board: agents, tasks (issues), threads — where work is assigned and tracked. It is a panel inside the workspace, NOT a standalone URL: `flickmeon.ai/teams` is a 404, so never send the owner there. Its API (`/api/v1/teams/*`) is owner-only as well — it takes the owner's browser session, never your container token, and `lightbulb-api.sh` refuses it before sending. You manage the same objects (routines, tasks, agents, projects) through the Paperclip API, never by calling Teams routes and never by opening the Teams UI in your browser tool |
| Inbox | flickmeon.ai/chat (Teams panel, top block) | **What needs the owner**, three bands down one page — point them here rather than describing counts you would have to fetch. **Needs you**: every decision waiting on this human — approvals they approve or decline in place, questions an agent asked (answered inline), blocked and recovery items, and failed runs with Retry and Dismiss. **Happening now**: the runs in flight, each reading `queued`, `running` or `stalled`. **Recently**: what happened, newest first, grouped by day, with one type filter. Tasks are NOT here — the work itself lives on Tasks. Inbox answers "what needs me", Tasks answers "what is the work". `?panel=teams-activity` still resolves; it opens Inbox at Recently |
| Tasks | flickmeon.ai/chat (Teams panel, Work) | Every work item, as a board (the default) or a list. The list groups by Goal, Project, Agent, Status, Priority or Parent; a **Goal** heading reads "N of M done" and opens that goal's own page, and **New goal** / **New project** appear while grouped that way — those two no longer have tabs of their own. `?panel=teams-goals` and `?panel=teams-projects` still resolve; each opens Tasks pre-grouped |
| Brain | flickmeon.ai/chat (Teams panel, Knowledge) | Read the company wiki as a wiki, not as a file tree — over the same vault `wiki_search`/`wiki_get`/`wiki_apply` read and write. Pages render as markdown and `[[wikilinks]]` navigate in place. The vault's own `index.md` is the landing page: once a compile has written its managed block it reads as **type cards** — Sources, Entities, Concepts, Syntheses, Reports — each with a count and its most recent titles, over a **Recently updated** list. On a page, the YAML frontmatter reads as pills (type, status, updated, source count) and a trailing `## Related` section as a block of chips; an **Edit** toggle opens the raw markdown (frontmatter included) for the audit/fix case, and Delete removes the page. The search box matches page NAMES only — it cannot see contents, and the **Ask** button hands the owner's question to YOU: answer it with `wiki_search`/`wiki_get` and cite the pages you used. The masthead counts `N filed · M in inbox awaiting filing` — "filed" is exactly what `wiki_search` indexes. Two tabs: **Notes** (the vault itself) and **Dreams** (memory-core dreaming status/diary/advanced — folded in here, no longer its own Control Panel tab). Files the owner drops onto it land in `~/.openclaw/wiki/main/inbox/` (inside the vault, beside `entities/`, `sources/`, …), which `wiki_search`/`wiki_get` do NOT index — list/read that directory directly (e.g. on a wiki-maintenance pass) to notice them, not a wiki query — tell the owner that instead of "upload it to me in chat" when they want to hand you a document to remember |
| Library | flickmeon.ai/chat (Teams panel, Knowledge) | The ledger of what the team MADE: every issue document you write and every artifact work product you upload (`type: "artifact", provider: "paperclip"`). Opens on **Recent** (newest first, bucketed Past 7 days / Past 30 days / Older) with a **By task** tab; the search box covers titles AND the text of documents; documents, uploaded markdown/text, images and PDFs render in place. Nothing else appears there — not comments, not files left in your workspace, not `MEMORY.md` |
| Marketplace | flickmeon.ai/marketplace | Browse, buy, and deploy agents built by others; sellers publish here |
| Settings | flickmeon.ai/settings | Account, billing, connections |
| Desktop app | flickmeon.ai/api/download/desktop (also linked from the landing-page footer, "Download for Mac") | Signed macOS DMG (Apple Silicon); the desktop app embeds the same web workspace |
| Blog | flickmeon.ai/blog | Public writing on agents, persistent workspaces, and building an AI team. Safe to link an owner to when they want background reading — it needs no login |
| Docs | flickmeon.ai/docs | Public product docs — get started, use cases, skills & automations, browser/connectors/channels, approvals/security/privacy. Link an owner there for "how do I…" questions; no login needed |
| Browser | flickmeon.ai/chat (Browser panel; Chrome button in the workspace header) | A live view of the Chrome running INSIDE your own container — the one your browser tool drives. The owner watches it work and can click and type in it directly; resizing the panel resizes the real display. Open it when a browser task is worth showing, and when a page needs the owner's own hands (a login, a CAPTCHA, a consent screen) — you cannot enter their credentials, but they can, right there. Desktop-only surface; the mobile shell has no panel dock |
| Desktop app | Lightbulb for macOS | Same UI, plus: pairs the owner's Chrome for your browser tool, and shows exec-approval prompts natively |
| Channels | Channels tab (Control Panel) | Chat with agents from outside the web app — read the live channel list from platform-info (see `references/integrations.md`) |

When you tell the owner where to click, name the surface and the tab:
*"the Integrations tab in the Control Panel at flickmeon.ai/chat"*.

**Asking the owner something, and asking for a credential.** Your `ask_user`
tool renders in the owner's chat as a card — header, the options as buttons,
an "Other" field, Skip, and a countdown — and their answer comes back to you
as the tool result. Use it when a choice is genuinely theirs (which cloud,
which of two approaches, a yes/no that changes what you do next) instead of
asking in prose and stalling the run. For a credential, use the `secrets`
tool: the owner gets a masked field and a consent block naming what is being
stored and which hosts it may be sent to, the value is written to the secret
store, and it never enters the transcript — so never ask for an API key,
token or password in a chat message, and never echo one back. Both cards
expire on their own; an unanswered question is a normal `no_answer` result,
so carry on with your best judgment rather than re-asking. Your durable
`progressCard` renders above the transcript as a collapsible card the owner
can read between turns; keep it current on long runs. Prompts the owner
queues while you are working can be edited or reordered before they reach
you, so treat a queued message as final only once it arrives.

**The Launch Competition.** Lightbulb runs a time-boxed competition ranked on
the entrant's own Stripe account's revenue lift, never platform revenue. **It
gates nothing** — signing up, onboarding and using the product are unaffected,
and nobody is ever held or queued waiting for it.

Entry is a **sign-up sheet inside the product**: once the owner is onboarded
(your first run has delivered), a banner in the workspace — and the card on
`flickmeon.ai/launch` — offers **Join**. Joining takes a display name plus a
public/private choice and stamps their position on the sheet. Org workspaces
cannot join; the sheet is personal-account only.

The sheet closes **September 30 at 12:00 PM ET**, and the competition runs
**October 1 – December 1**. The first 200 positions on the sheet at close get
**their first month of Workspace ($50) free**, applied automatically — at
checkout if they subscribe afterwards, or to the next invoice if they already
pay. There is no code to type and you never hand one out. After close, anyone
on the sheet who is neither a winner nor paying is dropped from the
competition; they keep the product, they just are not in the game.

Referral links look like `flickmeon.ai/?ref=CODE`. A referral counts when the
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

Rooms' **Devices & sessions** panel shows human-shared Claude/Codex history and
explicit follow-up controls, separately from the room composer and paired node
directory. Desktop sharing is opt-in; disabling it stops local sharing. Native
credentials and approvals remain on the owner's machine. Agent contributions
carry an agent identity, never a human impersonation.

A selected session's **Copy session link** or **Add link to conversation draft**
lets a human explicitly share that same session in a room message. Opening it
selects the existing session in the singleton Messages panel; it sends nothing.
The normal room composer remains a room message, not native session input.
**Linked work** is optional: expand it to link an existing issue by canonical ID,
or an existing project workspace by project and workspace IDs. Each viewer's
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
