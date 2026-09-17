# First run: a better seed, connector widgets, and a context dump

**Status:** PROPOSED — awaiting owner review. No implementation until approved.
**Date:** 2026-09-17
**Scope:** what a brand-new owner sees the first time they open Lightbulb.
**Out of scope:** the mobile visual refresh, sidebar IA, tool-trace rendering.

---

## 1. What the owner asked for

Three things, in their words:

1. *"I really want to add a better seeding prompt. The first thing a user sees
   when they open up Lightbulb, the agent should be saying something else."*
2. *"There should be a pop-up with icons and buttons that lead you to connect
   certain very important connectors, like Google Workspace or GitHub. There
   should be choices there for you to automatically connect."*
3. *"There should be a place to dump context from previous sessions. If I use
   Claude and I want Claude to know about my business and I want to give that
   context to Lightbulb, there should be a really easy way to do that."*

## 2. ⚠️ This contradicts a prior design. Read before deciding.

`docs/superpowers/specs/2026-07-12-onboarding-aha-first-run-design.md` is still
**Draft for review** — never approved, never shipped — but it argues the
opposite of item 2, and the argument deserves a hearing rather than a silent
overwrite. Its position, drawn from Polsia:

> strip every connect-your-account step ("connect your GitHub" sounds sketchy
> on a platform claiming to run your business) and **provision everything
> instead**.

That doc's thesis is that the aha moment is an autonomous first run the user
*receives*, with the paywall after the demonstration. A connector wall is
precisely the friction it wanted removed.

**This spec does not assume that doc is wrong.** It assumes the two can
coexist, and the resolution below is the single most important thing for the
owner to rule on:

> **Connectors are offered, never required.** Nothing in the first run blocks
> on connecting anything. The connector widgets are an accelerant for the user
> who already knows they want their calendar read, not a gate the rest must
> pass. The context dump is the *low-trust* alternative that gets Lightbulb
> useful without any OAuth at all.

If the owner disagrees and wants connectors to be a prominent required-feeling
step, say so — it is a legitimate call, but it should be made knowingly,
because it reverses a documented position.

## 3. What exists today

Two separate first-run surfaces, and the owner's "whatever we use right now to
seed it" refers to the first:

**The CEO's welcome message** — `DEFAULT_ROOM_WELCOME` in
`apps/backend/core/services/messaging.py`. Posted by the CEO into the seeded
`#general` on a fresh workspace. Current text states the role, asks for what
the user is building / who for / what's in the way, and offers a
copy-pasteable prompt for people who would rather watch first. Its code
comment records two deliberate constraints:

- it refers to the CEO **by role, never by name**, because agent names are
  user-editable;
- it offers a sample prompt so a hesitant user has something to click.

Both constraints survive any rewrite.

**The Get Started card** — `apps/frontend/src/components/getstarted/getStartedConfig.ts`,
five steps: `meet` (read about the starter team), `chat`, `task`, `hire`,
`channel`. Note `channel` means Slack/Discord/Telegram **messaging channels**,
not data connectors — so connectors genuinely have no home in the current
first run. That is the gap.

Also relevant: per CLAUDE.md, the first normal human message in a fresh
workspace is captured once to create the initial goal/project and stage
`BRIEF.md`. **The context dump should feed the same machinery rather than
inventing a parallel one.**

## 4. Design

### 4.1 The seed message

Rewrite `DEFAULT_ROOM_WELCOME`. It must keep the role-not-name rule and keep a
one-click sample prompt. What changes is that it now names the two new doors —
"connect your tools" and "paste what you already have" — so the pop-up is not
the only way to discover them.

Deliberately unresolved here: the exact copy. Wording this is a judgement call
the owner should make or approve directly, and a spec is the wrong place to
freeze it. The implementation plan will propose two or three drafts.

### 4.2 The connector widget

A card, shown once, on first entry to a fresh workspace. Not a modal that
traps — the owner said "pop-up," but a blocking modal on a brand-new
workspace fights the autonomous-first-run thesis in §2. **Recommendation: an
in-timeline card in `#general` directly under the CEO's welcome, plus a
dismissible panel.** It is in the user's path without standing in it, and it
survives a reload because it is a message, not ephemeral UI state.

Contents: a small grid of connector tiles, each an icon, a name, and a connect
action. Candidates, taken from the real provider list in
`apps/backend/core/services/credentials/providers.yaml` (which already
supports: google, github, notion, linear, slack-adjacent channels, sentry,
attio, intercom, posthog, stripe, vercel, x, aws, apollo, cloudflare,
meta_ads, instagram, shopify, quickbooks):

| Tier | Providers | Why |
|---|---|---|
| Featured | Google Workspace, GitHub | the two the owner named |
| Secondary | Notion, Linear, Slack | most-asked-for team tools |
| Everything else | link through to the Connections rail | do not build a second connector directory |

Hard requirements:

- **Show real connection state.** A tile for an already-connected provider
  reads as connected. The state comes from the existing integrations rows, not
  from local storage.
- **Do not reimplement OAuth.** Each tile hands off to the existing connect
  flow on the Connections rail. This card is a shortcut, not a second
  implementation.
- **Dismissible and idempotent.** Dismissing is durable. Re-provisioning must
  not resurrect it.
- Google Workspace's connect modal copy is being fixed separately on
  `docs/google-workspace-connect-modal` — this card links into that same
  modal and inherits the fix.

### 4.3 The context dump

The highest-leverage item on the whole list, and the one with the least prior
art in the codebase. A user arrives having already explained their business to
some other assistant. Today they must retype it.

**Input:** a text area that accepts a large paste, plus a file drop that
accepts markdown / plain text. The messaging attachment pipeline already
handles upload, type validation and size caps
(`POST /messaging/rooms/{room_id}/attachments`, 20 MiB non-image,
`MESSAGE_ATTACHMENT_ACCEPT`) — reuse it rather than building a second uploader.

**Destination — this is the real design question.** Three options:

| Option | Where it lands | Trade-off |
|---|---|---|
| **A. Agent memory** | the workspace's persistent memory | every agent sees it on every turn forever; risks polluting context with a wall of stale text |
| **B. A workspace document** | staged like `BRIEF.md` | inspectable and editable by the owner; agents read it when relevant rather than always |
| **C. First-message capture** | fed through the existing first-message → goal/project → `BRIEF.md` path | zero new machinery; but that path is built for a couple of sentences, not a 40 KB dump |

**Recommendation: B, with C's summarisation step.** Store the raw dump as an
owner-visible document, and run it once through the CEO to produce the same
goal/project/`BRIEF.md` artifacts the first message would have produced. The
owner keeps something they can read and correct; the agents get a digest
rather than a wall.

Note the Hermes migration is actively changing where agent memory and profiles
live (`cf5931806` seeds agent memory with `memories/MEMORY.md` + `USER.md`;
profiles now live in S3 on AgentCore). **Confirm the current memory contract
against Hermes before implementing** — the pre-Hermes answer is likely stale.

**Privacy:** a business context dump is among the most sensitive things an
owner will ever paste. It must never leave the owner's own workspace storage,
must not be included in marketplace agent exports (`agent_export.py` filters
are the relevant guard), and must be removed by account teardown.

## 5. Sequencing

The three parts are independent and should ship as three PRs:

1. seed message rewrite (smallest, no new UI)
2. connector widget (reuses existing OAuth)
3. context dump (needs the destination decision, and a Hermes check)

## 6. Open questions for the owner

1. **The §2 contradiction** — connectors offered-not-required, as recommended?
   Or a deliberate reversal of the July position?
2. Which connectors are "featured"? The recommendation is Google Workspace and
   GitHub only, since those are the two you named — more tiles means less
   emphasis on each.
3. Context dump destination — A, B or C?
4. Should the first run differ for someone joining an existing workspace
   versus creating one? Right now this spec assumes workspace creators only; a
   joiner arriving to a populated workspace probably wants neither card.
