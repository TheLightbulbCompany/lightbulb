# lightbulb-network: onboarding and the hosted MCP (spec)

Status: draft for owner review, 2026-10-22. Part of Phase 1 of the charter. Covers the free install and join
path, the hosted MCP as phone and cloud assistants meet it, the upgrade story, the docs set, and the Phase 7
ten-minute test. Binding inputs: the "Where each thing lives" table in the design, and `2026-10-22-lightbulb-network-architecture.md`, which is binding over this spec where they differ. Names: the engine is the command `lightbulb`; the file is `lightbulb-network` at `~/.lightbulb/network/bin/lightbulb-network`, and `~/.local/bin/lightbulb` links to it.

Marks: **[V]** verified in our code or docs today. **[U]** unverified: from third-party research or not yet
tested. Every [U] step must be tested live before it goes in a public page.

## Intent and why

A Show HN reader has one question: can my agents talk to my friend's agents, right now, without trusting a
server? The answer must take one line and under ten minutes, cost nothing, and ask for nothing else.

Two doors, same result:

- A person at a terminal runs `curl -fsSL https://thelightbulb.company/install | sh`.
- A person who is already in an agent pastes one line: `Join the Lightbulb network: <link>`.

The hosted MCP is the other half. Phone and cloud assistants (Muse, ChatGPT, the Claude apps, Grok Bot) cannot
run an engine. They reach the network through a member's Lightbulb workspace (a free trial counts). That is honest and useful. We
must say so plainly, in the apps' own setup steps and on the website, and never let it look like the free path.

## Goals

1. One-line install; one-line join; first message in under ten minutes on a fresh Mac, from public docs only.
2. The free path never asks for a card, a plan, a workspace, or an account.
3. A Lightbulb account is optional. Signing in (`lightbulb signin`) verifies the name and links the person to their workspace; it creates no org, trial or provisioning.
   Nothing on the free path breaks if the person never signs in.
4. A teammate joins by pasting one line to their agent. The agent runs the installer and the join. The joiner
   answers no prompt; the inviter (or an admin) answers one Allow for the join, unless the link was minted
   `--open`, and the first message is held for the usual Allow.
5. The hosted MCP uses the same tool names and arguments as the engine's MCP, so one agent skill works on both.
6. Phone and cloud assistants connect in under five minutes per app, with steps marked [U] until tested.
7. A free user can add a workspace, and a workspace can join their network, without re-inviting anyone,
   changing keys, or losing mail.
8. Every page in the docs set below exists, and the public README links to them.

## Non-goals

- Cloud agents, Friday, rooms, chat, tasks or billing on the free path or in the public docs.
- A hosted MCP without a workspace. It needs a Lightbulb workspace (a trial counts). We say so.
- Push into Muse, ChatGPT, Claude apps or Grok Bot. They are pull-only. No push, no hooks, no inbound webhook.
- Linux and Windows install in v1. The installer refuses them with a clear message (see Failure modes).
- Telemetry on the free path. None by default.
- Teaching the hosted MCP to speak the engine's wire protocol. It is a member computer in the cloud, built by
  the bridge spec.

## Anti-goals

A failure even if it works:

- Any screen, prompt or doc on the free path that says "plan", "trial", "workspace", "upgrade" or shows a price.
  The upgrade offer lives on the website and in the app, never in the CLI output of the free path.
- The installer or `join` opening a browser the person did not ask for. The browser opens only for the
  optional sign-in, and the join works if it never opens.
- The join line causing an agent to run anything except the installer from our domain and `lightbulb join`.
- The hosted MCP letting a caller send as someone it is not, or reading mail that is not its own.
- Telling a free user the phone path is free, or hiding that it needs a workspace.
- Secrets, internal hostnames or account ids in any public page, or in the examples below.

## User flows

Two people: **Ana** (installs from the website) and **Ben** (her teammate). Copy in code blocks is exact. Text in
`<angle brackets>` is filled at run time. Terminal copy is the engine's; browser copy is the website's.

### Flow A. Free path, end to end

**A1. The website install page** (`https://thelightbulb.company/install`; also the README top).

```text
Let your coding agents talk to each other.
Free. Open source. End to end encrypted. No account needed.

  curl -fsSL https://thelightbulb.company/install | sh

Works on a Mac with Apple silicon. Claude Code, Codex, Cursor and Grok CLI.
Read the script first: thelightbulb.company/install.sh   Source: github.com/TheLightbulbCompany/lightbulb-network

Already in an agent? Paste this to it instead:
  Install Lightbulb from https://thelightbulb.company/install and join my network.
```

Below the fold: a 20-second terminal recording, "How it works" in three lines (your computers talk directly;
a relay only ever sees ciphertext; nothing is stored on our servers), a link to Security. No pricing. A footer
link, "Lightbulb workspace: Friday, cloud agents, phone approval", goes to the website, not the install.

**A2. Ana installs.**

```text
$ curl -fsSL https://thelightbulb.company/install | sh
Downloading Lightbulb network 0.1.0 (about <size> MB)...
Verifying the signature...
Installed to ~/.lightbulb/network/bin/lightbulb-network (command: lightbulb)
Setting up your agents...
  Claude Code   ready
  Codex         ready
  Cursor        not found, skipped
  Grok CLI      not found, skipped
Starting the background service...  running
Your computer is "Anas-MacBook" and your name is "ana" (not verified).
Next: lightbulb invite
```

No browser opens. No sign-in. The first start of the background service already created Ana's solo network, named
after her OS user, so `lightbulb invite` works at once; there is no `init` step. Running the installer again upgrades and
changes nothing else. The installer never uses sudo.

**A3. Ana opens two agents.** She starts `claude` and `codex` in two terminals. Claude Code prints
`● On the Lightbulb network as ana`. She asks Claude: "Find my Codex session and ask it to run the tests." Claude
calls `find_sessions`, then `send_message`. Her own sessions are never held. Codex gets it at its next step.

**A4. Ana invites Ben.**

```text
$ lightbulb invite
Invite link (valid 7 days; you or an admin allow each person who joins; `lightbulb invite --new` renews it):
  lightbulb://join/<ticket>

Send Ben this line to paste into his agent or terminal:

  Join the Lightbulb network: https://thelightbulb.company/j/#<ticket>

Copied to your clipboard.
```

The `https` form opens a static web page (A5b). The ticket is in the URL fragment, so it never reaches our servers
or logs. The engine reads the same ticket from either form. `lightbulb invite --once` mints a single-use
ticket valid 24 hours; `--open` mints a link whose joins need no Allow (the CLI says what that gives up). Ana
sees who is on her network with `lightbulb who`.

**A5a. Ben pastes the line to his agent** (Claude Code, Codex, Cursor, Grok CLI or any agent with a shell).
The agent opens the link as a page. `/j/` is one static page that knows nothing about the team (the ticket is in
the fragment and is never sent to us); it shows, in this order:

```text
Lightbulb network invite.

To join, run these two commands in a terminal on this computer:

  curl -fsSL https://thelightbulb.company/install | sh
  lightbulb join '<the full link you were given, including the part after #>'

Only run them if you know the person who sent this and expected the invite.
The first command installs the signed Lightbulb command. The second asks to add this computer to their team.
Nothing else is needed. You do not need an account.
```

Ben's agent shows him the two commands and asks before running them (agents already ask before shell commands
that fetch and run scripts; we do not ask them to skip that). He approves. Output:

```text
Downloading... Verifying the signature... Installed to ~/.local/bin/lightbulb
Setting up your agents...
  Claude Code   ready
Starting the background service...  running
Joining ana's team... waiting for ana (or an admin) to allow it.
Connected. Your name is "ben" (not verified). Team: ana (founder), ben.
```

His agent then says: "Joined ana's team. Ana's sessions: Codex in checkout-api." (from `lightbulb who`).

**A5b. Ben has no agent open and clicks the link.** The page shows a person view:

```text
You were invited to the Lightbulb network.

1. Open a terminal and run:
     curl -fsSL https://thelightbulb.company/install | sh
2. Then run:
     lightbulb join '<the full link you were given>'

Or paste this to your coding agent:  Join the Lightbulb network: <link>

Free. No account needed. What is this? thelightbulb.company/network
```

**A6. First message.** Ana asks her Claude: "Tell Ben's Claude the migration is merged." Claude calls
`find_sessions`, then `send_message` to Ben's session. Ana's terminal receipt:

```text
Held for approval on ben's computer. Receipt: held.
```

(Before this, ana's computer showed "ben wants to join your network" and she allowed it. `lightbulb join` waits for that Allow, so Ben's agent says so while it waits.)

Ben's computer shows an OS dialog (the daemon's own, no app needed):

```text
ana wants to send a message to your Claude Code session "checkout-api".
This is the first message from ana. Name not verified.

  [ Allow ]   [ Decline ]   [ Always allow ana ]
```

If Ben is not at the computer the message waits there, held, for up to 7 days. On Allow, his session gets it
framed with the existing teammate header, `[Lightbulb teammate contribution from ana's Claude (not verified) {"requestId":"<id>"}]`
followed by the text, and Ana's receipt moves to `delivered`, then `read`. Receipts are one of seven states:
`queued`, `stored`, `delivered`, `read`, `held`, `refused`, `expired`. Ben's next message from any of Ana's
computer sessions goes straight in.

**A7. Sign-in is optional, and when it happens.** Nothing prompts for it. Two places offer it, once each, in
plain text:

- The end of `lightbulb join`: `Optional: lightbulb signin verifies your name for your teammates.`
- The Allow dialog and `lightbulb who` show "(not verified)" beside unproven names.

If a person runs `lightbulb signin`:

```text
Open https://thelightbulb.company/device and enter ABCD-1234
Waiting for you to sign in... (Ctrl-C to cancel, nothing changes if you do)
```

Browser page: "Sign in to verify your name on the Lightbulb network. This does not create a workspace or start
a plan." After sign-in:

```text
Verified as Ana Reyes (ana@example.com). Teammates now see your name without "(not verified)".
```

If the person has no Lightbulb account, the sign-in page lets them create one free. It asks for nothing else.
If they never sign in: they keep sending and receiving; teammates see "(not verified)". Cancelled or failed
sign-in leaves them exactly as before. `person_id` is sticky: signing in later changes the verified badge, never
Allow, labels or limits. Our sign-in server is never required to join or send; a proof verifies
offline once issued (design, Identity).

**A8. Leave.** `lightbulb leave` leaves the network and recreates the empty solo one; the install and the service
stay. Output: `Left ana's team. Your network of one remains.` To stop or remove the background service, run
`lightbulb daemon --uninstall` (stops the daemon, removes the launchd job, keeps state). `lightbulb leave --everything` also removes the agent wiring and wipes the state (backups are kept). `lightbulb unlink` removes a linked
workspace.

### Flow B. Phone and cloud assistants (the hosted MCP)

**What the person needs, said first on every page:** a Lightbulb workspace (a free trial counts, and every
organisation gets one). The hosted MCP is part of the workspace. These apps cannot run an engine, so they reach the network through your workspace,
which acts as a computer on your network. Free users cannot add it: the OAuth sign-in ends with "Your
account has no workspace" and a link to the website.

Honest line for every setup page, the website and the README:

```text
Phone and cloud assistants (Muse, ChatGPT, Claude apps, Grok Bot) connect through a Lightbulb workspace,
(a free trial counts). The free network is for coding agents on your own computers.
```

**What they can do (pull-only).** Send a message, read replies, check inbox, see a receipt, find sessions. They
cannot be messaged live. A teammate's agent sends to your workspace computer, and the assistant sees it when
it asks (`check_inbox`). No push, ever. Messages from an assistant to someone else's session are labelled
`<App> via <name>` (the receiver builds it from the roster; `<App>` comes from a short allowlist) and are held
for Allow like any first contact. Each hosted caller is a session of the bridge's cloud member, so the target of
`send_message` is always a `session_id`.

**Auth methods** [V: all three exist in `hosted_mcp.py` and the connector docs]:

- OAuth through Clerk with dynamic client registration and client ID metadata documents. The person picks the
  workspace when signing in. Used by Claude, ChatGPT, Muse Code.
- Agent key (`lbk_...`), made in Settings, sent as `Authorization: Bearer lbk_...`. For clients without OAuth:
  Grok Bot, Muse app, scripts. Shown once. Revocable.
- No anonymous access.

**Add the connector, per app.** Server URL everywhere: `https://api.thelightbulb.company/mcp`.

| App | Steps | Status |
|---|---|---|
| Claude.ai and Claude Desktop | Customize, Connectors, Add custom connector. Paste the URL. Sign in to Lightbulb, choose the workspace. Then it works in Claude on mobile. | Add flow [U for 2026-10 wording]; works on Free (one connector), Pro, Max, Team (Owner adds, members connect). Adding from the iOS app [U]. |
| ChatGPT | Settings, Connectors (developer mode), add a custom MCP app with the URL. Sign in with OAuth. | Web only [U]. Business and Enterprise/Edu with admin enabled; Plus and Pro reported read-only [U], so sending may fail there. Test live before documenting. |
| Muse (consumer app) | There is no connector screen. In a Muse chat say: "Build a connector to the MCP server at `https://api.thelightbulb.company/mcp` using this key." Enter the agent key in the secure prompt, never in chat. Muse saves it as a skill. | [U] everything. Third-party sources only. Muse reaches only public HTTPS, which ours is. |
| Muse Code | `mcp_servers` entry: type `streamable_http`, url above. Then `muse mcp login lightbulb`. | Deferred past parity. [U] syntax; from Meta's docs (read). Muse Code can also run the engine directly: preferred (free). |
| Grok Bot | Add a custom MCP server: Remote HTTPS, the URL, header `Authorization: Bearer lbk_...`. For receiving, also add a webhook agent (Settings, Connections, Webhook agents). | [V] in our docs today; Grok-side screens [U] for current wording. |
| Instinct | Not documented. We do not list it until an invited account shows a custom MCP field. | [U] skip |
| Any MCP client | URL, OAuth or agent key. | [V] |

Each public page repeats: the workspace requirement, what to say to test it ("Ask it to check my inbox"), how
to undo (remove the connector; revoke the key; leaving the workspace cuts access at once).

**First message from a phone assistant.** Person says: "Send Ben's Claude: the deploy is done." The assistant
calls `find_sessions`, then `send_message`. It returns:

```text
Sent to ben's Claude Code "checkout-api". Receipt: held (waiting for ben to allow). request_id: <id>
```

### Flow C. Adding a workspace to a free network

**Where a free user meets the offer.** Only on surfaces they already chose to open:

- thelightbulb.company (the workspace page), with its own pricing.
- The Lightbulb app, if installed: a Network screen with a quiet card.
- A hosted-MCP sign-in that finds no workspace (the dead end in Flow B).

Never in the CLI, the installer, `join`, `invite`, the dialogs or errors.

App card and website copy:

```text
Your agents already talk to each other. A Lightbulb workspace adds Friday and cloud agents, chat, and
approving from your phone. Your network stays as it is.
  [ See the workspace ]
```

**How a workspace joins the network (the bridge), designed to break nothing.**

1. The person signs in with Lightbulb (`lightbulb signin`, or in the app). This links their name proof to the
   workspace. Nothing is sent yet.
2. They open Settings, Network, "Connect my workspace". The app asks the engine for an invite exactly like
   Ana's in A4, and hands it to the bridge.
3. The bridge joins as one more member, named `<name>'s workspace`, with the `bridge` role. The existing team
   sees it in `lightbulb who`, marked `workspace`. The bridge never admits itself: it relays the request to one
   of the person's computers, which asks "Allow?" locally.
4. Nothing existing changes: same keys, same team, same invite links, same mail. Disconnecting
   (`Settings, Network, Disconnect workspace`, or `lightbulb unlink`) removes that one member; an admin can
   remove it too. Mail already on the workspace stays there.
5. Friday and cloud agents then appear as sessions of that member, labelled `Your Friday` on the person's own
   computers and `Friday via <name>` on a teammate's, held unless the person turned on
   `agents_send_without_asking` (for their own linked cloud member).

Rules: the engine never learns about plans; the bridge never gets a key to the person's computer; downgrade
disconnects the workspace member and nothing else.

### Flow D. Docs set and the ten-minute test

**Docs set** (public site pages; each also lives in the public repo, so docs ship with the code):

1. `README` (repo): what it is, the install line, the join line, three screenshots, links.
2. `network` (overview): what the network is, in five short paragraphs and one diagram.
3. `network-quickstart`: install, two agents, first message. One page. The ten-minute path.
4. `network-teams`: invite, join by pasted line, who, remove, admins, rotate the link.
5. `network-security`: encryption, what a relay sees, holds and approval, "not verified", the loop limit.
6. `network-privacy`: what is stored where, how long, the telemetry stance.
7. `agents-claude-code`, `agents-codex`, `agents-cursor`, `agents-grok-cli`: per agent, what delivery tier it
   gets and how to check it.
8. `agents-hosted`: one page for the workspace-needed assistants; Claude, ChatGPT, Muse, Grok Bot, any MCP client
   as sections. Opens with the honest line.
9. `cli-reference`: every command, flags, exit codes.
10. `mcp-reference`: tools, arguments, errors; says engine and hosted are identical.
11. `network-troubleshooting`: the failure modes below, with exact error text and the fix.
12. `network-faq`: Is it free? Do I need an account? What does Lightbulb see? Does it work on Linux?
13. `contributing` and `SECURITY.md` (repo): how to report; a real contact.
14. `upgrade` (website, not in the network docs): what the workspace adds, linked from the footer only.

The current pages (agent-network*, agents-*) are rewritten to match; hub-era wording is deleted, not kept.

**The ten-minute test (script a tester follows).** Tester: a person who has not seen Lightbulb, a fresh
Mac with Apple silicon and a fresh macOS user, Claude Code and Codex installed, no Lightbulb app, no account.
Teammate: a second computer (Aden's, or a second user on the same Mac) with the same setup. A moderator
watches and says nothing except "keep going". The clock runs from step 1 to step 12.

1. Tester opens `thelightbulb.company/install` in Safari only. Start the clock. (Do not point them to the repo.)
2. Tester copies the install line into Terminal and runs it. Expected: ends with "Next: lightbulb invite" in
   under 90 seconds on a normal connection.
3. Tester starts `claude` in one tab and `codex` in another. Expected: Claude prints the "On the Lightbulb
   network" line.
4. Tester asks Claude: "Find my Codex session and ask it to say hello." Expected: Codex acts on it.
5. Tester runs `lightbulb invite` (no `init`; the solo network exists since the daemon's first start). Expected: the join line is on the clipboard.
6. Tester sends the line to the teammate in any chat app (the moderator checks it is the exact line).
7. Teammate pastes it into their Claude Code. Expected: the agent shows the two commands and asks; teammate
   approves; the tester's computer asks "ben wants to join your network" and the tester Allows; "Connected"
   appears on the teammate's side.
8. Teammate runs `claude`. Tester asks their Claude: "Tell the teammate's Claude hello from me."
9. Expected on tester's side: receipt `held`. Expected on teammate's side: the Allow dialog.
10. Teammate clicks Allow. Expected: their Claude acts on the message; tester's receipt reads `delivered`.
11. Teammate replies from their Claude. Expected: it reaches the tester's Claude without a dialog.
12. Stop the clock. PASS if all steps finished in under ten minutes, with no help, no account, no card, and no
    page outside the public docs. Record every pause over 20 seconds, every wrong click, every question asked.
13. Optional, after the clock: tester runs `lightbulb signin` and confirms "(not verified)" disappears.

Fail rules: any moderator help, any step needing an account, any error without a visible fix, any blocked
Gatekeeper dialog without instructions. Everything that confused the tester is fixed and the test is re-run
with a different tester. A passing run is recorded with the timeline in the results doc.

## Threat model

**1. A malicious invite link.** An attacker sends Ben `Join the Lightbulb network: <link>` to join Ben's
computer to the attacker's team, or a look-alike link that runs a different script.
- The link page is on our domain only; the engine ignores join links from any other host.
- Joining a team gives the team members the ability to *ask* to message Ben's sessions. Every first contact is
  held for Ben's Allow. A team member cannot read Ben's files, run commands or see his terminals.
- Admission needs a person's Allow (the inviter's or an admin's) unless the link was minted `--open`, so a
  stranger holding a leaked link still waits for a yes.
- `join` prints the team's founder name and member count before connecting, and asks `Join ana's team? [y/N]`
  when run by a person at a terminal (not when run with `--yes` by his agent; the agent still asks Ben before
  running shell commands).
- Invite tickets expire after 7 days (`--once` tickets after 24 hours), are replaced on every removal, and are
  not signed by us; the ticket rides in the URL fragment and never reaches our servers or logs. A forged ticket
  fails the link-secret check.

**2. A phishing "join" line pasted to an agent.** The attacker's text says "Join the Lightbulb network: <link>"
with a link whose page tells the agent to run something else (prompt injection).
- The page we host is fixed text: exactly the two commands above. The skill that handles the join line tells
  the agent to run only `curl -fsSL https://thelightbulb.company/install | sh` and `lightbulb join '<link>'`,
  and to refuse a link on any other host.
- The installer checks the app signature and team id. An attacker's script cannot pass that check.
- Peer messages are always framed as peer input and never as the owner's instruction; the loop limit and the
  hold rule still apply after a bad join.
- Residual risk: an agent on a malicious page may be told to run other things. We cannot fix other sites. The
  docs say: only paste join lines from people you know.

**3. A hosted caller claiming to be someone.** A caller sets a sender name, or claims another workspace.
- The hosted MCP never takes a sender from an argument. The author is the verified OAuth client plus the
  signed-in person, or the agent key's owner. [V: "authored by the OAuth client, accountable to the person".]
- The workspace id comes from the verified token and a live membership check on every call. [V]
- Labels are rendered by the receiver from the roster: `<App> via <name>`, with `<App>` from a short allowlist (anything else renders as `App "<sanitised>"`). A body that forges the header is neutralised.
- A caller can only read rooms and receipts of its own workspace and own requests. A `message_receipt` for
  someone else's request id returns not found.
- Hosted senders get the same loop limit and hold rule as engine senders.

**4. An agent key leaked.** `lbk_...` in a repo, log or chat.
- Keys are shown once, stored hashed, and revocable in Settings with immediate effect.
- A key is limited to one workspace, one person, and the send and read scopes. It cannot make keys or change
  settings.
- Sending with a key to a teammate's session is held for Allow, so a leaked key cannot silently reach a
  teammate. It can read the workspace's rooms that key owner can read, which is why revoke must be easy.
- Keys show `last used` and the last caller IP prefix in Settings so a leak is noticed; a key unused for 90 days
  is flagged for removal (proposed, see open questions).
- Public repo gitleaks pattern includes `lbk_` so our own commits cannot leak one.

## Failure modes

Each has an exact message and a fix. None leaves a half-install.

**Intel Mac, Linux, Windows.**

```text
lightbulb install: Lightbulb needs a Mac with Apple silicon (M1 or later).
Linux is next, Windows later. Follow github.com/TheLightbulbCompany/lightbulb-network/issues/1.
Your agents can still reach each other through a Mac on your team.
```

Exit code 1, nothing written, no partial files. [V: the current installer already refuses with the Apple
silicon wording; Linux wording to be added.] A Rosetta terminal is detected through `sysctl` and installs.

**Gatekeeper refusal.** The download is signed and notarized, so refusal is rare, but a quarantined binary
from a browser can trigger it.

```text
macOS blocked "lightbulb". Open System Settings, Privacy and Security, and choose "Open Anyway", or run:
  xattr -d com.apple.quarantine ~/.lightbulb/network/bin/lightbulb-network
Then run the install line again. Why this happens: thelightbulb.company/docs/network-troubleshooting#gatekeeper
```

The installer downloads with `curl`, which sets no quarantine flag, so the one-line path avoids this. The
direct-download DMG path may hit it; the page says so.

**The browser never opens** (optional sign-in only). `lightbulb signin` always prints the URL and code, waits
for sign-in, and works if the person opens the URL on another device. Over SSH it says so:
`No browser here. Open https://thelightbulb.company/device on any device and enter ABCD-1234.` Join and send
do not depend on this at all.

**The teammate's agent has no MCP support.** The engine works without MCP: `lightbulb send` and
`lightbulb listen --as <name>` are plain commands, and delivery uses the agent's hooks where they exist. The join page
says: `If your agent cannot run commands or use MCP, run the two commands yourself in a terminal.` An agent
with a shell but no MCP gets a skill file (`lightbulb setup` writes it) that teaches `lightbulb who` and
`lightbulb send`. An agent with neither can still be reached by its person at the terminal (`lightbulb listen --as <name>`).

**Other.** The service is not running: `join` and every command say `The Lightbulb service is not running.
Starting it...` and start it. Clock skew over 5 minutes: invites and proofs say `Your clock is off by <n>
minutes. Fix the date and time, then try again.` No network: `Can't reach anyone right now. Messages are
saved and will send when you are online.` Relay down: direct connection still works; the message says
`Using a direct connection only.` Two daemons: the second exits with `Already running (pid <n>).` Expired or
revoked invite: `This invite is no longer valid. Ask ana for a new one.` Wrong host in a join link:
`Refusing a join link from <host>. Lightbulb only joins links from thelightbulb.company.`

## Data

**The hosted MCP stores, per caller** (proposed retention marked P; the owner confirms in Open questions):

| Item | What | Where | How long |
|---|---|---|---|
| OAuth client registration | client id, name, redirect URIs, created time, last used | workspace store | until the person removes the connector or leaves the workspace; unused 180 days P |
| OAuth tokens | not stored by us; Clerk verifies each call | Clerk | token lifetime |
| Agent key | name, hash of the key, owner, scopes, created, last used, last IP prefix | workspace store | until revoked |
| Request receipts | request id, caller, target, state (`queued`, `stored`, `delivered`, `read`, `held`, `refused`, `expired`), times | workspace store | 30 days P |
| Messages the caller sent | the workspace's own mailbox for hosted senders, kept as the bridge's computer inbox | workspace store, encrypted at rest | 24 hours when undelivered, then dropped with receipt `expired` (matches the engine rule); delivered text follows the workspace's normal retention |
| Messages for the caller to read (`check_inbox`) | mail addressed to the workspace member, held on the bridge as on any computer | workspace store | 7 days, or 24 hours after read; the person's own mailbox does not expire |
| Audit line per call | time, caller, tool name, result code. No message text. | logs | 30 days P |

Never stored: the caller's other apps' data, prompts the assistant did not send to us, sender names from
arguments, any plain-text copy outside the workspace store. The hosted path is not peer-to-peer end-to-end for
the person's *assistant* hop: the assistant's vendor sees the text it handles. The bridge is a member computer
and holds its own key. The docs say this once, in plain words: "Messages sent through your phone assistant pass
through that assistant's company and your Lightbulb workspace."

The free path stores nothing of ours: no account, no telemetry, no central mailbox. The engine keeps keys,
team list, outbox and held mail on the person's own computer only.

## Interfaces

**Hosted MCP.** URL `https://api.thelightbulb.company/mcp`, streamable HTTP, OAuth 2.1 with DCR and client
metadata documents, or `Authorization: Bearer lbk_...`. Scopes: `lightbulb:messages:read`,
`lightbulb:messages:write`. [V today: find_rooms, find_sessions, read_messages, send_message, message_receipt,
find_tasks, read_task, update_task, use_browser.]

Engine-identical tools (same names, same arguments, same errors; the hosted server adds nothing to these):

```text
find_sessions(query: string = "", limit: int = 20)
    -> sessions[]: { session_id, person, person_verified, computer, agent, cwd, repo, branch,
                     status: busy|idle|gone, doing, tier, can_send, needs_owner_ok }
send_message(request_id: string, text: string, session_id: string, reply_to?: string, files?: [], notify?: bool)
    -> { request_id, state, tier: push|next_step|pull, note }
check_inbox(wait_secs: int = 0, max 600)
    -> messages[]: { id, request_id, thread, from, label, text, sent_at }   (request_id is the reply token for send_message.reply_to) ; returns on first unread or at the deadline
message_receipt(request_id: string)   (request_id: opaque string, [A-Za-z0-9._-]{1,128}, chosen by the sender; the hosted MCP stops requiring a UUID, a Plan C change)
    -> { request_id, state: queued|stored|delivered|read|held|refused|expired, reason?, updated_at }
```

`session_id` is `<member_key_short>/<provider>:<conversation>` (`member_key_short` is 12 lowercase base32 characters); cloud sessions are `<bridge_key_short>/<agent>`.
The hosted MCP additionally accepts `room_id | person_id | agent_id` for workspace rooms (a hosted-room
concept, never an engine target) and refuses `files` and `notify` with `unsupported`.

Refused outright on both engine and hosted: `*`, `person/*` and repo-wide targets (group sends). An error
`group_send_refused` explains.

Hosted-only tools (workspace features, not in the engine): `find_rooms`, `read_messages` (rooms and DMs),
`find_tasks`, `read_task`, `update_task`, `use_browser`. Free users never see these.

Note: the hosted `read_messages` takes a `room_id` and keeps reading rooms; a caller's mailbox is read with
`check_inbox`, which is added to the hosted MCP and answers from the bridge's mailbox for that caller (kept 7
days, or 24 hours after read), so a skill that says `check_inbox` works on both.

**The install line.**

```text
curl -fsSL https://thelightbulb.company/install | sh
curl -fsSL https://thelightbulb.company/install | sh -s -- --no-join     # install only
curl -fsSL https://thelightbulb.company/install | sh -s -- --join '<link>' # install and join a team
```

Installs `~/.lightbulb/network/bin/lightbulb-network`, links `~/.local/bin/lightbulb` to it, runs `lightbulb setup`, starts the launchd service. No sudo. Safe to repeat.
Also available: `brew install thelightbulbcompany/tap/lightbulb`.

**The join line.** Exactly:

```text
Join the Lightbulb network: https://thelightbulb.company/j/#<ticket>
```

Ticket: base64url of `{team_id, link_secret, inviter_key, relay_hint, up to 6 member keys}`, in the URL
fragment. The same ticket is also `lightbulb://join/<ticket>` and `lightbulb join '<link>'`. The host is fixed;
the engine refuses other hosts. The page behind it is static, the same for agents and browsers, shows the
install line and `lightbulb join`, and knows nothing about the team. The agent skill `lightbulb-network-join` triggers on the exact prefix `Join the Lightbulb network:` and nothing
looser.

**CLI** (engine): `join`, `invite [--new|--once|--open]`, `who`, `send`, `listen --as`, `receipt`, `approve`,
`decline`, `policy`, `signin`, `setup`, `daemon`, `doctor`, `leave` (leaves the network; `--everything` also
wipes state), `daemon --uninstall` (stops the service), `unlink`. `signin` creates no org, trial or provisioning. Full list in the
CLI reference.

## Acceptance checklist

**Ten-minute test** (Flow D, steps 1 to 12): PASS only if every row is true.

| # | Row | Pass |
|---|---|---|
| 1 | Fresh Mac, only the install line and the public docs, no help | under 10 min |
| 2 | No account, card, plan or workspace asked for at any step | zero asks |
| 3 | Join by pasted line works through Claude Code, and again through Codex | both |
| 4 | First message held, then Allow delivers, reply goes straight in | yes |
| 5 | Every error shown has a visible fix | yes |
| 6 | Optional sign-in works, and not signing in changes nothing | both |
| 7 | Gatekeeper, Intel Mac and Linux messages match the text above | yes |
| 8 | Public pages contain no price, no "workspace" outside Hosted and Upgrade | grep clean |
| 9 | gitleaks on the repo and docs | clean |

**One row per app** (run with a real workspace; each records date, version, screenshots):

| App | Check | Pass |
|---|---|---|
| Claude.ai (web) | Add connector, sign in, "find my sessions", "send hello to my Codex", receipt | each works |
| Claude Desktop / mobile | Same connector works after being added on web; note if iOS can add [U] | works / note |
| ChatGPT (web, per plan) | Add connector; read works; send works or fails with a documented limit | note per plan |
| Muse (consumer) | Ask Muse to build the connector with an agent key; send, read, inbox | works / note |
| Muse Code | Deferred past parity | not in v1 |
| Grok Bot | Custom MCP with `lbk_` key; webhook agent receives a message; replies through MCP | both directions |
| Any MCP client (e.g. a script) | OAuth with DCR, then the four engine tools | works |
| No workspace | A free user signing in sees the plain "needs a workspace" page, not an error | yes |
| Key revoked | Revoke the key; next call fails in under 10 s | yes |
| Leave workspace | Member leaves; OAuth calls fail on the next call | yes |
| Upgrade | Free network of two joins a workspace; nothing breaks; disconnect leaves the network intact | yes |

## Rulings

Rulings (architecture 2026-10-22), folded in from the earlier open questions:

1. **No hosted MCP without a workspace.** A Lightbulb workspace is required (a free trial counts), and the copy says "a Lightbulb workspace", never "paid".
2. **Retention numbers stand:** receipts 30 days, audit 30 days, OAuth clients unused 180 days, agent keys unused 90 days flagged; hosted mailbox 7 days or read plus 24 hours.
3. **`join` asks at a terminal** (`Join ana's team? [y/N]`); with `--yes`, the agent's own shell approval is the gate. Admission also needs an Allow from the inviter or an admin unless the link is `--open`.
4. **`check_inbox` is added to the hosted MCP** over the bridge's mailbox for the caller.
5. **The `/j/` page is static and shows nothing about the team.** The ticket is in the fragment and never reaches us.
6. **The install page mentions the workspace in the footer only.**
7. **Muse and Instinct live tests come later**, before any public page names them (they need accounts: stop and ask). Muse Code is deferred past parity.
8. **No "trusted" agent keys.** Own sessions already skip the hold.
9. **The join-line skill is page text only** in v1; no plugin.
