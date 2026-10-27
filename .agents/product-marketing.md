# Product Marketing Context — Lightbulb

**Document version:** v9 (draft for founder approval; v8 is on the local `codex/lantern-font` branch, not on main)
**Last updated:** 2026-10-27
**Sources:** v8 (2026-10-13) carried forward where unchanged; the founder's 26 Oct ruling (become messaging; the network leads; "parity with Reason Network, then improve" stands); `docs/marketing/26-messaging-repositioning.md`; `origin/main` at `ba2753093`; TheLightbulbCompany/cli at v0.6.4; PostHog reads by Gauge (ISOA-960, 961, 962); reasonmachines.com and github.com/reason-machines/reason-network on 27 Oct. Every number has a date.

## Product Overview
**One-liner:** Messaging for people and their AI agents: the agents talk to each other and to the people, across vendors and computers, with one record of who did what and who said yes.
**What it does:** Two layers. The **network** is free and open source (Apache-2.0): one line installs the `lightbulb` command, which wires Claude Code, Codex, Cursor, Grok CLI and OpenCode on that computer and lets those sessions message each other and the sessions of people you invite, peer to peer and end-to-end encrypted, with no account. The **workspace** is the paid layer that joins the network as one more member: agents with their own runtime, memory, email and tools that keep working when every laptop is closed, rooms where people and agents talk, a task board, approval cards answered on the web or a phone, shared Claude Code and Codex sessions a teammate can watch and pick up, and hosted connections for ChatGPT, Claude and other clients. Every workspace starts with ready-made agents (CEO, Engineering, Finance, Growth, Analytics, Customer Operations); a team can bring its own.
**Product category:** AI-native communication, internally; on the page, messaging for people and their AI agents. The shelf is the one Slack and Microsoft Teams own (team messaging); the neighbours on the free layer are Reason Network and AgentRelay (agent-to-agent messaging for coding agents).
**Product type:** Open-source CLI and engine (free) plus B2B SaaS workspace (web, Mac app, iPhone app on TestFlight).
**Business model:** The network is free, no plan, no card, no account. The workspace is $50 a month flat for up to 5 members, +$20 a month per member from the 6th, custom enterprise pricing; managed AI as credits at provider price × 1.2, or bring your own ChatGPT account or API key; 15% marketplace fee. 30-day no-card trial for self-serve. Founding-teams offer: a card on file at the setup session, 30 days free, first charge $50 on day 31, price locked two years; no guarantee, no discounts, no coupons for outside teams (13 Oct). The site quotes prices only from the billing API.

## Target Audience
**Who installs (entry):** Anyone running two or more AI agents, starting with developers who use Claude Code, Codex or Cursor every day. They may be alone: a solo developer's Claude Code can message their own Codex on the same Mac or their other computer with nobody else on the network. The invite is the loop; the second person is the activation event.
**Who pays (the wedge, unchanged from 13 Oct):** Founder-led teams of 3 to 15 that already run two or more AI agents for real work, where one person routes context between people and agents by hand, and who want an agent that keeps working when the laptops close. The founder decides, pays and sets it up. The seven qualifying criteria in the plan (§5) gate the guided setup, not the network. Small businesses running back-office work through an agent stay the fallback named in Risks (Anna's shape).
**Decision-makers:** The founder/CEO decides and pays; a technical cofounder is the technical influencer and, on the network, often the first installer; teammates join by invite (network) or join link (workspace).
**Primary use case:** Let the agents a team already runs talk to each other and to the team, and hand company work to agents the whole team can see, redirect and approve.
**Jobs to be done:**
- Stop being the switchboard between people's agents: one session's findings reach another person's session without a human copying them.
- Get real work done by agents (books, outreach, research, code, email) with a person's yes on anything that spends, sends or deploys.
- Keep the company running when nobody's laptop is open (routines, email, nightly CEO run).
**Use cases:**
- Network: a cofounder's Codex updates tests after a Claude Code session on another Mac changes an endpoint; a reviewer's agent asks the author's agent what changed; one developer's Claude Code and Codex split a task on one machine.
- Workspace (observed): QuickBooks invoicing and check matching (Anna); a seller-acquisition plan waiting on a ~$5,000 approval (Second Catch); staff scheduling (AB Detailing, pilot); standup → task board; cold-email drafting with approval; shared coding sessions between founders.

## Personas
| Persona | Cares about | Challenge | Value we promise |
|---------|-------------|-----------|------------------|
| Developer running two or more agents (installer, technical influencer) | Not re-explaining context; agents that work together | Every agent only talks to the person who runs it; copy-paste between sessions | One line, and your Claude Code talks to your Codex and to your teammates' agents |
| Founder (champion, decision maker, buyer) | Getting more done with a tiny team; staying in control | Is the human router between teammates and three AI tools; context lives in their head | One place where the team and the agents talk, and every decision comes to you for a yes |
| Operator (user, e.g. bookkeeping/ops) | Fewer manual steps in QuickBooks, email, scheduling | Repetitive work across tools; no engineer to automate it | An agent that does the task in the real tool and asks before anything sensitive |
| Invited teammate (user) | Seeing what's going on without a new habit | Another app to check | The same chat shape as Slack, with agents already in the rooms |

## Problems & Pain Points
**Core problem:** An agent can only talk to the person who runs it. When a second person, or a second agent, needs what it knows, a human copies the context out by hand. Inside a company two more things break: **visibility** (nobody sees the whole company's agent work in one place) and **handoff** (moving agent work to a teammate means rebuilding context; ownership blurs).
**Why alternatives fall short:**
- Slack and Teams carry people's messages and add a vendor's assistant; an agent is an add-on to a person's workspace, not a member with its own tools, history and permissions, and no other vendor's agent can join.
- Reason Network and AgentRelay connect coding agents to each other, for code, on your laptop; nothing keeps working when it closes, there are no rooms people share with agents, no approvals, no email, no non-code work.
- Claude Code, ChatGPT and consumer agents are built around one user.
- The status quo (a group chat plus each person's own ChatGPT, a shared markdown repo, Linear as the bus) is free and good enough for solo tasks, so it survives until the team actually shares work.
**What it costs them:** Rebuilt context on every handoff; duplicated work; the founder as the bottleneck; agent output nobody else can audit.
**Emotional tension:** "I'm the only one who knows what the AI did"; agents that email, spend and deploy can embarrass you, so the yes must stay yours; tool fatigue ("comfortable enough with what they have"); trust in an early product ("will this hold up in front of my team?").

## Competitive Landscape
**Direct, free layer:** Reason Network (Reason Machines; launched 16 Oct 2026; 1,265 stars and 108 forks by 27 Oct; MIT or Apache-2.0; macOS, Linux, Windows; Claude Code, Codex, Cursor, Gemini CLI, Copilot CLI, Grok CLI, opencode, pi, DeepSeek Harness, Reason; hosted assistants via its MCP server; sign-in required with Google, GitHub, Apple or email; iroh peer to peer with encrypted relay fallback; feeds the paid Cloud Reason coding agent). AgentRelay (hosted hub, 926 stars, Apache-2.0; their server reads messages; telemetry on by default; bypasses agent permissions). Both: agent-to-agent, code only, nothing that runs when the laptop closes.
**Direct, paid layer:** Slack (Slackbot "your personal AI agent"; Agentforce "available soon"; Business+ $15/seat annual) and Microsoft Teams (Copilot Business $21/user/month, promo $18; Business Standard with Copilot $23.50) — AI added to people-chat, per-seat, the agent belongs to the vendor. Also Bloome (people and agents in one group chat, any model) and Nimbalyst (shared workspace for people and their Claude Code/Codex agents, $160 for 8, free in beta). Facts checked 13 Oct (docs/marketing/11-competitors.md), Reason and AgentRelay 27 Oct.
**Secondary (session handoff, code only):** Zed Delta, Egregore, Lody, Pairon, Athrd, ZeroShot, Claudebin, ccshare, claude-share, ccpair, ccx, claude-peers, Claude Code Agent Teams. About forty products sell shared context, session handoff or team memory for coding agents (02-prospecting). None gives the whole company one record.
**Secondary (AI coworkers):** Grok Bot (a cloud computer per person; "one user cannot reach another user's computer"), Atlassian AMP, Muse, Instinct.
**Indirect:** A group chat plus one person's ChatGPT; a VA or bookkeeper; doing it by hand.
**Founder rulings:** a crowded space is a reason to learn from them, not to avoid it (13 Oct); "parity with Reason Network, then improve" (22 Oct); our free network must not be stricter than Reason's (24 Oct); the free layer is table stakes, the workspace is the difference, and no competitor is named on the site (26–27 Oct).

## Differentiation
**Key differentiators:**
- Your agents can talk to each other: any agent, any vendor, any computer; free, encrypted, no account.
- The work keeps going when you log off: workspace agents with their own runtime, memory, email and tools; routines and the nightly CEO run.
- A person stays in the loop: approval cards for spend, sends, deploys and hires; on the network every member you admit is trusted and `lightbulb remove` takes it back; the CLI never touches agent permissions.
- Rooms shared by people and agents, with one record of who did what and who approved it; shared Claude Code and Codex sessions a teammate can watch and pick up.
- Any model: Lightbulb-managed, your ChatGPT account, or your own key. One workspace price, the first five people free.
**How we do it differently:** One direction of dependency: the public engine knows nothing about the workspace; the workspace joins the network through a bridge as one more member. The workspace is the product; the network is how people find it.
**Why that's better:** Work can move between people and agents, across vendors and computers, without losing context, and the company keeps a record of who did what and who approved it.
**Positioning (26 Oct, for founder approval):** For people who already run two or more AI agents, and the teams they work in, Lightbulb is messaging for people and their AI agents: the agents talk to each other and to the people, across vendors and computers, with one record of who did what and who approved it. Unlike Slack and Teams (messaging for people, with a vendor's assistant added), Reason Network and AgentRelay (agent-to-agent only, for code) or Grok Bot (a computer per person nobody else sees), Lightbulb joins a free, open-source network to a workspace where agents from any vendor keep working when every laptop is closed, so work assigned in chat gets finished and the first five people are free.
**Three claims:** your agents can talk to each other · the work keeps going when you log off · a person stays in the loop.
**Contrast lines (calls and the plan; the site names no competitor):** vs Slack and Teams "Slack is messaging for people. Lightbulb is messaging for people and their agents." · vs agent-to-agent tools "Add a workspace and your agents keep working after you close the laptop." · vs hosted hubs "Ours runs between your computers; nothing in transit is readable." · vs Grok Bot "A team of bots per person, or one set of agents the whole team can see." · vs DIY "Stop being the switchboard." · model "People are free up to five; you pay for the workspace and the work your agents do."

## Objections
| Objection | Response |
|-----------|----------|
| A prospect raises Reason Network or AgentRelay (we never do) | One sentence, no comparison: the difference is the workspace, agents that keep working when your laptop is closed, rooms your team reads, and a person's yes on anything that spends, sends or deploys. |
| "Basic Claude takes care of all that easy." | For one person on simple tasks, yes. Lightbulb earns its place when a second person, or a second agent, needs what your session knows. |
| "I don't need 5 agents to do things for me." | On the network you need none of ours; your own agents talk. In a workspace you talk to one, the CEO; the others are its team, and you can switch them off. |
| "We don't collaborate on projects." | Then the network is still useful alone, and the workspace isn't for you yet. |
| "We already have Slack/iMessage." | Keep them. Lightbulb is where your agents talk; your team joins the rooms where something is happening. |
| "Will it do things without asking?" | On the workspace, spend, sends, deploys and hires wait for a person. On the network, every member you admit is trusted; removal takes it back. |
| "What does the relay see?" | Ciphertext only, and only when a direct route fails. Usage counts are anonymous and off with one command. |

**Anti-persona:** A solo operator with simple, episodic tasks that one ChatGPT tab handles (the network is still fine for them; the workspace is not); a pair whose scopes of work don't overlap ("my brother and I have diff scopes of work"); a team shopping for a cheaper Slack; a deterministic-automation buyer.

## Switching Dynamics
**Push:** Copying context between people's AI sessions; the founder relaying status; agent work nobody else can audit.
**Pull:** One line and the agents talk; agents that finish real work while you're away, visible to the whole team.
**Habit:** The group chat they already have plus a personal ChatGPT; "comfortable enough with what they have" (Aden, 1 Oct).
**Anxiety:** A daemon on my machine (answer: open source, no account, telemetry off, nothing in transit readable); agents acting without permission; bugs in an early product; one more subscription.

## Customer Language
**How they describe the problem:**
- "your agent remembers what you did. it has no idea what your co-founder just spent four hours figuring out." (the founders of Egregore, Hacker News, Feb 2026; a builder's voice)
- "I use Codex, my non-technical co-founder uses Claude" (HN, 19 Aug 2026); "My cofounder and I each run our own Claude Code agent" (HN, 9 Jul 2026); "getting the humans and agents to talk together is really fragmented" (HN, 21 Jul 2026); "most coding agent sessions are user-local" (HN, 12 Aug 2026)
- "none of it syncs" (r/smallbusiness, 6 Oct 2026); "re-explaining the stack in their prompts every session" (r/ChatGPTCoding, 6 Oct 2026); "6 people all living in their own ChatGPT/Claude bubbles" (r/smallbusiness, 7 Oct 2026); "20 or so PRs that nobody on my team actually wrote" (r/ChatGPTCoding, 6 Oct 2026)
- "people on my team tend to work more in isolation with agents. I've never seen them collaborate ON the chat." (HN, Delta thread, Aug 2026); "Handoffs are where this breaks." (r/smallbusiness, 30 Aug 2026)
- "I'm just a kid with a simple business… Basic Claude takes care of all that easy." / "I'm not the right avatar. My brother and I have diff scopes of work." (Santi, Airgun Capital, 29 Sep)
- "The first thing that broke was adding the cofounder." (Second Catch, Oct)
**How they describe us:**
- "I'm really impressed with how fast it works and how helpful it can be for us now and moving forward when it comes to freeing up more time." (Anna, Anna's Frozen Treats, cleared 12 Oct)
- "Overall not too intuitive and added too many complications which ended up in slowing down work." (Santi)
**Words to use:** your agents, talk to each other, message, network, invite, join, session, keeps working when you log off, your whole team, approve, continue, "who owns a change", "single-player", "starts from zero", "comes back where the whole team can see". "AI employees" for the workspace's ready-made agents, never a headline.
**Words to avoid in public copy:** multiplayer, AI-native (the category name in the plan and interviews only), orchestration, bot, prompt, autonomous; internal names (Paperclip, Hermes, AgentCore, Jev; Hermes and Paperclip only as "built on"). Never a hold or Allow step on the network (it is flat); never Windows (WSL only); never a competitor's name in public copy (founder, 27 Oct).
**Glossary:**
| Term | Meaning |
|------|---------|
| Network | The free layer: computers whose coding agents can message each other; joined by invite; flat, no admins |
| Invite / join | `lightbulb invite` prints a four-word code (once, 10 min), a link and a join line (7 days); `lightbulb join` with any of them; an invite is the approval |
| Session | A Claude Code, Codex, Cursor, Grok CLI or OpenCode session on someone's computer; addressed by `<computer>/<agent>:<id>` |
| Member | A computer on a network; a workspace joins as one more member |
| Workspace | One company: its people, agents, rooms, board and runtime |
| CEO agent | The front door of a workspace; plans, delegates, reports back |
| Room | A channel, DM or group chat; agents are members of the rooms you add them to |
| Approval card | A decision an agent files for a person: spend, send, deploy, hire |
| Shared session | A Claude Code or Codex session teammates can watch, join or pick up |
| Usage counts | The CLI's anonymous events (`cli_installed`, `network_joined`, `cli_daily`…), off with `lightbulb telemetry off` |

## Brand Voice
**Tone:** Plain, specific, calm. No hype, no exclamation marks. Says what's early. Never names a competitor in public copy.
**Style:** Short sentences; concrete nouns (session, invoice, PR, channel) over abstractions; commands shown as commands.
**Personality:** Direct, competent, honest, a little dry.
**Competitors' voice (checked 27 Oct):** Reason's page is confident and short ("Let your agents talk to each other." / "Sign in, and you have a network."); Slack upbeat with AI in every heading; Atlassian in protocol terms. Nobody in the set writes plainly about what the agent did for a named small team, or says what their server can see. That is the gap our voice owns.

## Proof Points
**Metrics (dated):** 12–13 Oct: 57 accounts, 46 workspaces, about 5 outside workspaces active in a week; 33 of 38 outside workspaces have one person; $55 collected ever; 0 paying at list. 19–25 Oct: 11 verified external users, 38 active user-days, 32 messages and 26 agent replies from 8 users. 20 Oct launch day: 54 visitors, one external sign-up start, one external workspace, none verified. Network, 26 Oct: repo public since 22 Oct, 0 stars, 2 unique visitors; installs indistinguishable from founder testing.
**Customers:** Anna's Frozen Treats (QuickBooks invoices sent by the CEO agent since 11 Oct); Second Catch (two founders; a seller-acquisition plan waiting on ~$5,000 approval; may be named); AB Detailing (pilot); Habits365 (inbound). No other team is named until three pay (B2).
**Testimonials:**
> "I'm really impressed with how fast it works and how helpful it can be for us now and moving forward when it comes to freeing up more time." — Anna, Anna's Frozen Treats
**Value themes:**
| Theme | Proof |
|-------|-------|
| Your agents can talk to each other | The install line; the live directory; a message from Claude Code answered by Codex (demo to be recorded, T45) |
| Real work in real tools | Anna's invoices sent from QuickBooks |
| Work the team can approve | Second Catch's $5k plan waiting on an approval card |
| Keeps going while you're away | Nightly CEO run, routines, team email |
| Nothing readable in transit | Ed25519 per computer, signed roster, relay sees ciphertext (CLI docs/security.md) |

The "$2M+ in combined annual revenue" homepage line comes off until it has a source (plan open decision 10).

## Goals
**Business goal (unchanged):** Ten founding teams set up with a card on file by 4 Jan 2027, about three paying by 12 Jan, six paying by May; a quarter of new teams active in week four; then open sign-up; raise $2M SAFE at $14M post. Added for the network (placeholders for the founders to replace): 100 computers on networks of two or more by 4 Jan; a quarter of activated networks trying a workspace.
**Conversion action:** Copy the install line (or paste the "tell your agent" line); then invite a second computer; then start a workspace. "Request a guided setup" stays one level down for founding teams.
**Homepage headline (for founder pick, 27 Oct):** "Your agents can talk to each other now." Alternatives: "Messaging for people and their AI agents." / "One line, and your Claude Code talks to your cofounder's Codex."
**Current metrics:** about ten visitors a day; 8% click sign-up; activation unmeasurable until `chat_completed` (merged 27 Oct) is verified in production; network activation (a message delivered between two computers) to be read from `cli_daily` once the Monday rows exist.

## Changelog
*Newest first. One line per revision: what changed and why.*
- v9 (2026-10-27) — Repositioned from "team workspace with AI employees" to "messaging for people and their AI agents" (founder, 26 Oct): the free network is the entry product and the workspace the paid layer; Product Overview, Target Audience (entry vs paying), Competitive Landscape (Reason Network, AgentRelay, the session-tool set), Differentiation (statement, claims, contrast lines), Objections (Reason, relay), Customer Language (network words; "AI-native" allowed as the category name internally), Proof Points (network baseline, launch-day correction), Goals (conversion action = the install line). Draft for founder approval; downstream skills generate against this context once approved.
- v8 (2026-10-13) — The founder answered all 72 decisions in `docs/marketing/00-decisions.md`. No guarantee; one plan; founding teams put a card on file at setup; fix-forward rule; 90-day promise re-dated; Second Catch may be named; subhead "in your team's channels"; Telegram, Discord and WhatsApp off the homepage; email copy says the owner decides who agents can email; Ask to join for outsiders; launch competition post-product; referrals stage 2.
- v7 (2026-10-13) — Steps 4–7 in the plan: product items 1–9, go-to-market stages, the website brief (headline "Your team and your AI employees, in one place"), five Monday numbers; §9 rewritten with verified numbers.
- v6 (2026-10-13) — Step 3: pricing confirmed ($50 up to 5, +$20 from the 6th; markup ×1.2); founding offer; no discounts; contrast line "People are free up to five".
- v5 (2026-10-13) — Step 2: positioning statement, three claims and contrast lines approved; competitor facts verified.
- v4 (2026-10-13) — Step 1 research: wedge defined as the founder-router situation; operator-owner moved to Risks; Reddit/HN customer language added; coding-session competitors added with the crowded-space ruling.
- v3 (2026-10-12) — Founder rulings: wedge order stands; the $2M homepage claim stays for now; Anna's quotes may be used; "multiplayer"/"AI-native" dropped from public copy.
- v2 (2026-10-12) — Founder comments applied: visibility added to the core problem; emotional tension elaborated; secondary competitors widened; complexity objection marked as product work; board message house added.
- v1 (2026-10-12) — Initial context drafted from the business plan, origin/main, prod data and customer feedback.
