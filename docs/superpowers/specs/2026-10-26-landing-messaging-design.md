# The homepage says messaging: landing page, positioning surfaces and measurement (design)

Status: draft for founder approval, 26 October 2026. Branch `feat/landing-messaging` off `origin/main` at `ba2753093`.
Companion documents: the workstream `docs/marketing/26-messaging-repositioning.md` (the research, the skills' findings,
the competitor profile, the launch plan, the tracking plan and the build tasks), the review (claude.ai/artifact/9pGATeq9PTMHHLJpKnXZtJ)
and the business-plan redlines (claude.ai/artifact/7Ne5GiseUy66PYPbUoSxNr). The redlines are applied by the founders in the Google Doc; they are
not code and not part of this build.

## Intent and why

Lightbulb stops presenting itself as a team workspace with AI employees and presents itself as messaging for
people and their AI agents. The free, open-source network (TheLightbulbCompany/cli, `/network`) leads the
homepage; the paid workspace is the layer that joins it and keeps agents working when every laptop is closed.

Why now: ten visitors a day, a Product Hunt launch that produced one external sign-up start, 33 of 38 outside
workspaces with one person in them, and a headline that Slack now uses too. The one thing nobody else sells from
one place (any vendor's agents talking to each other and to your teammates' agents, plus a workspace that never
logs off) is absent from the homepage, and `/network` is linked from nowhere. The founder ruled on 26 October:
come off the AI-employee angle, become messaging. The 22 October network spec already says "Lightbulb's public
face becomes this network"; this document is the website catching up.

## Goals

1. A developer who already runs two or more coding agents reads the hero and knows in one sentence what
   Lightbulb does and how to try it in the next minute.
2. The primary action is the install line (copy it, or paste the "tell your agent" line); the workspace is the
   clearly labelled second product with its own door; the founding-teams offer stays, one level down.
3. Every machine-read surface (title, description, JSON-LD, `llms.txt`, Open Graph image, the CLI README's first
   lines, the Product Hunt tagline, the X bio) says the same sentence.
4. Measurement that can tell the next change apart from noise: the headline test gone, first-touch source kept,
   the network's own usage counts joined to the Monday numbers.
5. A privacy story a Hacker News reader accepts: what the network stores, what relays see, how to turn usage
   counts off, what the workspace stores and for how long.

## Non-goals

- No new product features. Nothing in the engine, the bridge or the workspace changes for this.
- No comparison page and no mention of Reason Network or AgentRelay anywhere on the site (founder, 27 Oct).
- No change to `/pricing`'s workspace plan, the no-coupon rule, the founding offer or `/request-setup`.
- No Show HN or Product Hunt relaunch in this build. Those have their own gate (Part 2 §7 of the plan).
- Not the business-plan edits themselves.

## Anti-goals (a failure even if it works)

- Public copy that uses multiplayer, AI-native, orchestration, bot, prompt, autonomous, or an internal name
  (Paperclip, Hermes, AgentCore, Jev). `HomeVoice.test.tsx` enforces this list and stays.
- A claim the product cannot show today: a hold or "Allow" step (the network is flat, an invite is the
  approval, nobody is asked); Windows (WSL only); "no account" for the workspace (the network needs none; the
  workspace does).
- The workspace demoted to a footnote. It is the business; the page must make a visitor want it by the time
  they reach pricing.
- A page that reads as a developer tool only. The workspace band speaks to the founder with a team.
- Anything that fires an event with message text, names, keys, codes or folder paths in it.

## Constraints

- Voice (plan Part 2 §2): plain, specific, calm; short sentences; concrete nouns; no exclamation marks; say what
  is early. "AI employees" allowed for the workspace's ready-made agents, never in a headline.
- Facts that are true today and may be stated: install in one line (`curl … | sh`, or Homebrew, or npm); macOS 13+
  on Apple silicon and Linux (arm64, x86_64; WSL on Windows); wires Claude Code, Codex, Cursor, Grok CLI and
  OpenCode; `lightbulb invite` prints a four-word code (once, 10 minutes), a link and a join line (7 days);
  `lightbulb join` with any of them; a computer is on one network at a time; peer to peer with a relay fallback
  that carries ciphertext only; Ed25519 identity per computer; a signed roster no server decides; Apache-2.0;
  anonymous usage counts off with `lightbulb telemetry off` or `DO_NOT_TRACK=1`. Source: the CLI README and
  `docs/security.md` as of v0.6.4; `apps/frontend/src/content/docs/pages/agent-network*.md`.
- Facts that may not be stated: a hold or Allow on first contact; a stranger-safe network ("every member is
  trusted"; removal is how you take that back); ten-minute onboarding (unmeasured until T44). macOS builds are signed
  and notarized (README and `sign-cli-release.yml`), so that may be stated.
- The named founder signs every public page (Prasiddha for product copy). Agents draft, never publish.
- Fix forward: no interim line that promises something the code does not do.
- The `$2M+ combined annual revenue` band has no source attached (plan open decision 10). It comes off.
- Only Anna and Second Catch may be named (decision B2). Anna's cleared quote may be used.
- `/network` keeps its URL (it is in the CLI README and in agents' memories as the paste-to-your-agent line).

## Decisions

Answers to the builder check (27 Oct), all my calls:

- **Prices come from `getPricing()`, never literals.** The repo rule stands. The Workspace column and the JSON-LD Offer read the
  billing API; `homeJsonLd` becomes a function that takes the pricing result; when the API returns null the column shows
  no number and the Offer is omitted, as `/pricing` does today. `app/page.tsx` stays per-request for that reason even after
  the cookie read goes.
- **The transcript renders once.** It is the hero's right half on desktop; on phone it sits below the buttons. The "See it"
  section is dropped; its H2 and caption move into the hero as the transcript's heading and caption. Real content, not
  `aria-hidden`.
- **The trust lead's "a founder signed it" waits for the Termly rewrite.** Until then the lead ends at "use it to train
  models." and links to `/security`.
- **The workspace band keeps `lb-mock`** (#launch, Engineering, the Needs-you rail) and drops `band-lanterns.jpg`; the hero's
  `lb-app` markup is deleted with the old hero.
- **`/network` gets Copy buttons and an npm line** (a small client component); the footer's "Install the network" is a link,
  so the `footer` surface is removed from the event.
- **Measurement beyond the two website events is a hand-off** to Prasiddha and Gauge (ISOA-962 lineage): confirming the CLI's
  events land in project 380894 and adding the Monday rows. Not in this PR.
- **Product Hunt `sameAs` is `https://www.producthunt.com/products/lightbulb-2`** (returns 200).
- **OG image:** `og-lightbulb-v3.png` with the H1 goes on the homepage (`pageMetadata`) and the layout fallback; other pages
  keep the wordless mark.

Made by the founder (26 October): become messaging; the network leads; "parity with Reason Network, then improve"
stands (22 October charter) and the workspace is the difference.

My calls (change any of them without re-asking):

1. **The homepage is the install page's big sibling, not its replacement.** `/network` stays as the canonical
   install page and the paste-to-your-agent target; the hero carries the same install line from one shared
   constant and links to `/network` for the full guide.
2. **Headline written for a developer.** The workspace band is written for a founder with a team.
3. **One filled button in the hero: the copy button on the install line.** "Start a workspace" is an outline
   button; "request a guided setup" is a text link. The nav mirrors this.
4. **The headline A/C test is removed**, not paused. At ten visitors a day it cannot resolve, and the cookie,
   super property and form field complicate every read.
5. **The night-sky art and `string-lights` style illustrations go.** The hero's right half is a two-terminal
   transcript (HTML, not an image). The workspace band keeps a light framed copy of the real `/chat` UI (Aden's
   teardown version, already on main).
6. **Nav:** Network · Workspace · Docs · Pricing · GitHub · Log in · Start a workspace. Blog and Download move to the
   footer only.
7. **No competitor is named on the page** (founder, 27 Oct). The workspace band and the trust section carry the
   difference; the FAQ has four questions.
8. **The Ask AI block stays**, with its question rewritten for the new positioning.
9. **Open Graph image is regenerated** with the new headline; the current one carries the old sentence.

## The page

Section order: Hero (with the transcript) → Problem → How it works → Workspace → Proof → Pricing → Founding teams →
Trust and FAQ → Ask AI → Footer. Copy below is final unless the founder edits it. Where a line depends on a fact, the fact
is named in Constraints.

### Hero (`Hero.tsx`, `HeroCtas.tsx`)

- H1: **Your agents can talk to each other now.**
- Subhead: Lightbulb is messaging for people and their AI agents. One line to install, and your Claude Code talks
  to your cofounder's Codex, your Cursor, and the agents in your team's workspace. Peer to peer, end-to-end
  encrypted, free.
- Primary control: the install line in a monospace box with a **Copy** button:
  `curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh`
  Under it, smaller: *or tell your agent:* `Join the Lightbulb network: https://thelightbulb.company/network`
  with its own Copy. Both strings come from one constant shared with `/network`.
- Secondary: **Start a workspace** (outline) → `/sign-up`. Text link: *or request a guided setup* → `/request-setup`.
- Trust row (mono, small): Free · Apache-2.0 · No account · macOS and Linux · Claude Code, Codex, Cursor, Grok CLI, OpenCode
- Right half: the transcript (below) at hero size on desktop, with its heading and caption; on phone it drops below the buttons. It appears once on the page.

### Problem (`ProblemBand.tsx`, rewritten)

- Eyebrow: Sound familiar
- H2: Your agent remembers what you did. It has no idea what your cofounder's agent worked out an hour ago.
- Body: So you carry it. You paste one session's findings into another, write the summary for your cofounder, and
  check three tools to learn what the agents did. Every agent can only talk to the person who runs it, and you are
  the switchboard.

### The transcript (part of the hero; no separate band)

- Heading: A session on your Mac. A session on hers. One conversation.
- Two terminal panes, HTML text, the same font as the product's terminal. Left: `mara · claude · lightbulb/payments`
  sends to `jonah/codex`: "I moved the refund endpoint to /v2. Your tests reference /v1." with the receipt line
  `delivered · read`. Right: `jonah · codex · lightbulb/web` shows the message arriving, the agent updating three
  tests, and the reply "Fixed, PR #418 open. Want Friday to look at it?". Names are the plan's placeholder people
  (Mara Reyes, Jonah Pike), not customers. Mark nothing as a real session.
- Caption: Every session on every teammate's computer, who is busy, what they are doing. Replies land in the
  session, or wait on their computer if it is closed.

### How it works (`HowItWorks.tsx`, rewritten; keep three cards, drop the paintings)

1. **Install.** One line. It wires Claude Code, Codex, Cursor, Grok CLI and OpenCode, and starts a small
   background service. Homebrew and npm work too.
2. **Invite.** `lightbulb invite` prints a four-word code, a link and a join line. Send any of them; your
   teammate runs `lightbulb join`. An invite is the approval; nobody is asked.
3. **Talk.** Ask your agent to message theirs. Replies come back into the session. A message to a closed session
   waits on that computer.

### Workspace (`WorkspaceBand.tsx`, rewritten; keeps the light `/chat` frame)

- Eyebrow: The workspace
- H2: When the laptops close, the workspace keeps working.
- Body: Add a workspace to your network and you get agents that never log off: a CEO, Engineering, Finance, Growth
  and your own, in rooms your whole team can read. They email, run routines overnight, and anything that spends,
  sends or deploys waits for a yes on your phone. Your Claude Code and Codex sessions sit in the same rooms, so a
  teammate can watch one and pick it up.
- Three captions under the frame: **Rooms.** People and agents in the same channels, with one record. **Approval
  cards.** A plan, a spend or a new hire asks in the room and waits for a person. **Shared sessions.** Watch a
  teammate's Claude Code or Codex session live and pick it up where they stopped.
- Button: Start a workspace → `/sign-up`.

### Proof (`ProofStories.tsx` stays; `ProofBand.tsx` and `proofBandCopy.ts` are deleted)

- Anna's cleared quote with the attribution line "Her CEO agent drafts and sends her invoices from QuickBooks."
- Second Catch: "A two-founder team asked their CEO agent for a plan to win new sellers. In week one it delivered
  the plan and filed a spend of about $5,000 for the founders to approve. Nothing went out until they did."

### Pricing (`PricingThreeCard.tsx`, rewritten as three columns)

- H2: The network is free. The workspace is one price.
- **Network, $0.** Unlimited computers and people · peer to peer, end-to-end encrypted · open source, Apache-2.0 · no account.
- **Workspace.** The price line from `getPricing()` (today it renders "$50 a month for your workspace, up to five
  people" and the $20 add-on) · agents that keep working, rooms, approvals, a phone in the loop · any model:
  Lightbulb-managed, your ChatGPT account, or your own key · 30 days free, no card to start. Link: See pricing → `/pricing`.
- **Founding teams.** Rendered by `FoundingTeams.tsx` as today (45 minutes with a founder, ten spots, price locked
  two years, "Request a guided setup"). Unchanged copy.
- `/pricing` gains the same Network column above its workspace table; nothing else on it changes.

### Trust and FAQ (`HomeFaq.tsx`, rewritten; four questions, emitted as FAQPage JSON-LD word for word)

Lead: **What we can see: nothing in transit.** Messages between computers are encrypted end to end; a relay, when
one is needed, carries ciphertext only. Usage counts are anonymous and off with one command. Workspaces run in
their own isolated AWS environment; your content is never sold or used to train models. (After the Termly rewrite
ships: "The policy is short, and a founder signed it.")

- *We already have Slack.* Keep it. Lightbulb is where your agents talk; your team joins the rooms where something
  is happening.
- *Claude already does this for me.* For one person, it does. Lightbulb earns its place when a second person, or a
  second agent, needs what your session knows. Link: Sharing Claude Code sessions with a team.
- *Will it do things without asking?* Plans, spending and new hires come to you as approval cards, and the agent
  waits. On the network, every member you admit is trusted; `lightbulb remove` takes that back.
- *What happens to our data?* On the network, nothing passes through us; your computer keeps its own key, and the
  roster is signed by its members. In a workspace, each team runs in its own isolated cloud environment on AWS;
  we do not sell your content or use it to train models. Link: Your data (`/security`).

### Ask AI (`askAiPrompt.ts`)

Question text: Go to thelightbulb.company and read the site, then read github.com/TheLightbulbCompany/cli.
Lightbulb is messaging for people and their AI agents: a free, open-source network where the coding agents people
already run (Claude Code, Codex, Cursor and others) talk to each other and to teammates' agents, peer to peer and
end-to-end encrypted, plus a paid workspace that joins the network with agents that keep working when nobody is
logged in, rooms the whole team can read, and approval cards for anything that spends, sends or deploys. Who is
this for, and what would a team of three to fifteen that already uses two or more agents get out of it?

### Footer (`Footer.tsx`)

- H2: Your agents can talk to each other. Bring your team.
- Buttons: Install the network (→ `#hero` on the homepage, `/network` elsewhere) · Start a workspace.
- Columns: Product (Network, The workspace, Pricing, Docs, Compare, Download for Mac, Marketplace) · Company
  (Blog, Your data, Privacy Policy, Terms, Support, GitHub) · Legal (unchanged).
- Base line: © year Isol8 Inc., doing business as Lightbulb AI. · Messaging for people and their AI agents.

### Navbar (`Navbar.tsx`)

Network (`/network`) · Workspace (`/#workspace`) · Docs · Pricing · GitHub (external, `github.com/TheLightbulbCompany/cli`) ·
Log in · **Start a workspace** (filled, `/sign-up`). Mobile menu adds Request a guided setup and Download. CTA ids
`nav_sign_up` and `nav_request_setup` keep their values.

## Machine-read surfaces

- `lib/seo.tsx`: `HOME_TITLE` = "Lightbulb — Messaging for people and their AI agents". `HOME_DESCRIPTION` =
  "Your coding agents talk to each other and to your teammates' agents, free and end-to-end encrypted. Add a
  workspace and your agents keep working when you log off." The root layout inherits both.
- Homepage JSON-LD `@graph`: Organization (add `sameAs`: https://github.com/TheLightbulbCompany,
  https://www.producthunt.com/products/lightbulb-2, https://x.com/lightbulbai; keep the `disambiguatingDescription`
  and add "not the lightbulb-mcp package"), WebSite, one SoftwareApplication `#software` with two Offers (Network,
  price 0, `url` `/network`; Workspace, price and description from `getPricing()`, `url` `/pricing`, omitted when the
  API returns null), FAQPage from the five questions above, WebPage. No ratings, no reviews.
- `/llms.txt` and `/llms-full.txt` regenerate from `seo.tsx` and the docs index; check the output says the new
  sentence first and lists the agent-network docs pages before the workspace docs.
- Open Graph image: a new `og-lightbulb-v3.png` carrying the H1 on the homepage (`pageMetadata`'s `OG_IMAGE`) and as the
  layout fallback; the wordless mark stays on every other page.
- `/network`: the shared install constant; Copy buttons on the curl, brew, npm and agent lines (a small client
  component); an npm line; a line under the title linking back: "This is the install page. What the network is, in
  one minute: thelightbulb.company." `network-page.test.tsx` updated.
- Outside this repo, same sentence, done by the founders: the CLI README's first three lines, the GitHub repo
  description and topics, Homebrew tap and npm descriptions, Product Hunt tagline, X bio.

## Measurement

- Remove `lib/headlineVariant.ts` and every use: `app/page.tsx` (the cookie read; the page can go static again
  unless pricing needs per-request rendering), `PostHogProvider.tsx` (the super property), `Hero.tsx`,
  `HeroCtas.tsx`, `RequestSetupForm.tsx` (the hidden field), and `HeroCtas.test.tsx`. The `lb_hv` cookie is left to
  expire.
- New website events in `lib/analytics.ts`: `install_line_copied` (`surface`: hero | network, `kind`: curl |
  agent_line | brew | npm) and `network_guide_opened` (from the hero's "full guide" link). No other new events.
- Existing events keep their ids: `signup_cta_clicked` (`hero_start_free`, `nav_sign_up`), `setup_cta_clicked`
  (`hero_setup`, `nav_request_setup`), `landing_viewed`, `signup_started`, `chat_completed` (merged 27 Oct as
  #4240; production verification is ISOA-867).
- The CLI already reports anonymous counts to PostHog: `cli_installed`, `cli_updated`, `network_joined` (via),
  `invite_created` (via), `cli_daily` (messages sent and received, computers on the network, connected agents),
  keyed by a random `install-id`. Builder: confirm they land in project 380894 and not another project; if another,
  say which in the PR. Nothing joins an `install-id` to a person; the Monday readout adds aggregate rows only:
  installs, networks with 2+ computers, computers reporting at least one message sent, invites created.
- Network activation, defined for Appendix C of the plan: a `cli_daily` report with messages sent ≥ 1 and computers
  on the network ≥ 2. Reported as a count, never a rate, until n is above fifty.
- First-touch UTM persistence and the source question on `/request-setup` are already on main (T-tasks, 14 Oct);
  the builder verifies `first_touch_*` reaches `signup_started` and `install_line_copied` and fixes it if not.

## Privacy policy and the data page

- The public Privacy Policy is a Termly document (`LegalDocument` fetches policy `906a65b7-…` from Termly at render
  time). It is edited in Termly by a founder, not in this repo. This build does not change it; it adds the
  repo-owned plain-language page and gives the founder the rewrite outline.
- `/security` ("Your data", shipped 14 Oct as T33) gains a section **The network**, in this order, each a short
  paragraph: what is on your computer (`~/.lightbulb/connect/identity.key`, the sign-in proof `login.json` if you
  linked a workspace, the signed roster, an outbox for messages to computers that are offline); what travels (your
  messages, encrypted end to end between computers; a relay only when a direct route fails, and it carries
  ciphertext); what we never see (message text, names, keys, codes, folder paths); usage counts (the exact event
  list from the README, the random `install-id`, geoip off, and the three ways to turn it off); how to leave
  (`lightbulb leave`, `lightbulb setup --remove`). Source of truth: `docs/security.md` and the README's "Usage
  counts" in TheLightbulbCompany/cli; copy facts, do not paraphrase them looser.
- Rewrite outline for Termly (founder task, Prasiddha signs per decision B9): remove intent data, advertising
  partners, Do Not Track, Telegram/Discord/WhatsApp, and the contradictions in the state-law table; name
  subprocessors in one list (AWS, OpenRouter, OpenAI, Anthropic, Stripe, Clerk, PostHog, Termly); retention and
  deletion in days (the founder picks the numbers; the page must not say "in accordance with our retention
  practices"); a paragraph on the network that matches `/security`; a date and a name at the top.

## Rollout and acceptance

Gate to merge (all true):

1. `pnpm --filter frontend test`, `typecheck`, `lint` green; `HomeVoice`, `LandingHeadlines.spacing`, `HeroCtas`,
   `AskAI`, `network-page` tests updated and passing; `ProofBand.test.tsx` deleted with its component.
2. The page at 1440px and 375px: no horizontal scroll; the install line wraps or scrolls inside its box; dark mode
   reads.
3. Every fact in Constraints that the page states is checked against the CLI README at the version then current.
4. Prasiddha has read the copy and signed off in the PR.

Gate to deploy to production (after merge, `deploy.yml` dispatch):

5. A fresh Mac (not a founder's daily machine) runs the install line from the page, `lightbulb invite` prints a
   code, and a second computer joins with it. Aden or Leo does it and writes the result on the PR.
6. The new OG image renders in a link preview (X, Slack, iMessage).

Measure of success, read on Mondays from the first Monday after deploy: installs per week, networks with 2+
computers, `install_line_copied` → `cli_installed` within a day (aggregate), sign-up starts per week, and whether
any network adds a workspace. Compare against the baseline rows in the review (0 stars, 2 unique repo visitors,
about ten site visitors a day).

## Left to the builder

- Component boundaries, CSS and the transcript markup; whether `TranscriptBand` and the hero share one component.
- The copy-button implementation (clipboard with a selected-text fallback) and its toast.
- The OG image production (any tool; 1200×630; H1 and the lamp mark on the midnight ground).
- Whether `app/page.tsx` returns to static rendering once the cookie read is gone.
- Test fixtures and the exact assertions in the voice test for the new sections.
- Order of PRs: one PR is fine; if split, the headline-test removal goes first so PostHog reads stay clean.

## Open questions

The builder check (27 Oct) returned seven questions; all are settled under Decisions. Nothing is left that only the
founder can answer for this build. The founder-only questions for the wider change (who runs the fresh-Mac test, relays, retention numbers, the 90-day network targets, Product Hunt listing, pushing the
planning docs to main) are in `docs/marketing/26-messaging-repositioning.md`.

## Amendment, 27 October 2026: the approved homepage

The founder judged the mock on localhost and approved it as is ("you can ship it this way for now"). What ships differs from the design above in these ways:

- The hero stays in today's layout (words left, the light HTML copy of /chat right). H1 "Message your agents the way you message your team." Sub: "Messaging for small teams that run AI agents. Mention one in a room. It does the work and posts the result where your cofounder can read it and say yes." Primary "Start your workspace free" (ctaId `hero_start_free`), secondary "Request a guided setup"; trust row "Open source network · Any agent, any vendor · 30 days free, no card". No customer names in the hero.
- The hero's chat mock is a four-line room: Mara mentions @Finance and @Engineering; "Jonah's Codex" answers with a session tag; Finance posts an approval card ("12 invoices sent from QuickBooks. 2 are over $5,000 and wait for your OK." with Approve both / Open); Jonah approves. The sidebar lists a "jonah · codex" session.
- A new band (`NetworkBand.tsx`) sits right after the problem band: H2 "Start with the agents on your laptop.", one line of body, then two cards. Free: "Let your agents talk." with the install line (`CopyLine`, `install_line_copied` surface `network`) and a link to /network. 30 days free: "Give your team a place to watch." with "Start your workspace free" (`LayerCta.tsx`, ctaId `layers_start_free`, surface `layers`). No CEO agent, no "headless", no "orchestrator" on the page.
- Navbar gains Network before Pricing. The footer's two "AI employees" lines become "Your agents are already in the room." and "Messaging for people and their AI agents." The Ask-AI prompt leads with messaging.
- The stories section keeps Anna's Frozen Treats and Second Catch as they are live.
- Not built: the hero-led install line and transcript from the design above (rejected 27 Oct as CLI-led), the privacy-steps band for /network and /security (open, see docs/marketing/27).
