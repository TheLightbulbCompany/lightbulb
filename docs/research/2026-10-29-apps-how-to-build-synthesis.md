# How Lightbulb should build apps for people who don't know how apps are built

29 Oct 2026. Synthesis of six research notes, all from primary sources (each claim is cited there):
`2026-10-29-lovable-ux-deep.md`, `2026-10-29-lovable-agent-deep.md`, `2026-10-29-builder-agents-deep.md`
(Replit, Bolt, v0, Base44, Anything), `2026-10-29-templates-kinds-of-apps.md`, and the first passes
`2026-10-29-how-lovable-builds.md` and `2026-10-29-app-builder-workflows.md`.

## The answer

There is no "generative software in general". Every builder that works for non-technical people
does the same four things:

1. **One default starter for everything.** A React or Next.js app with Tailwind and shadcn. The kind of
   request switches on a database, sign-in or payments; it never picks a different stack. Bolt
   removed its template picker this month because a default beats a choice.
2. **The agent asks almost nothing.** At most a card of a few questions, first option preselected,
   "skip" means defaults. Design questions only for websites. Personalization comes from reading
   what already exists (a URL, a screenshot, a sheet, the company's brand), not from asking.
3. **The person sees a preview, not a plan.** The first answer to "build me X" is a working link.
   Every change request updates the same preview. Publishing is one tap or "ship it".
4. **The infrastructure is invisible.** Nobody sees "environment", "staging", "production",
   "migration" or "database" unless they go looking. The backend switches itself on when a feature
   needs it; secrets go through a form; data is protected by rules (add-only schema, backups), not
   by asking the person to manage stages.

The difference between them is reliability: how fast the first preview appears and how often it
works without the person debugging. That is decided by the agent's loop, not the UI.

## 1. What the person experiences in Lightbulb

The whole product, as the person sees it, in a room:

| Moment | What they do | What they see |
|---|---|---|
| Ask | One sentence: "build us a crew schedule" | The agent says it's on it. If the request is a website with no brand to read, one card: palette, fonts, layout, first option chosen, "Skip". Otherwise nothing. |
| First time only | Nothing, or one tap | If the workspace has no Stripe services account yet: one card, "To keep your apps on your own accounts, confirm your name, birth date and email once" (Stripe's form). Then one approval card for the app's database and hosting. |
| Preview | Opens the link | A working app with their real context (company name, logo, any sheet they attached), plus one line: what it does, what it reads, a short recording or screenshot of it working. Opens in Canvas. |
| Change | Replies in the thread: "add the truck" | The same preview updates; one line says what changed. |
| Ship | Says "ship it" or taps Publish in Canvas | It's live for the room. No settings. Canvas shows it. |
| Use | Anyone in the room opens it from Canvas | Already signed in. |
| Problems | "This is broken" / "undo that" | The agent fixes it, or rolls the code back. Data is never rolled back with code; data restores are a separate owner action. |
| Later | Nothing | One line if the app is unused for 30 days. Cost per app on its Canvas entry. |

Words the person ever sees: **preview, live, publish, undo, Canvas**. Never: environment, staging,
production, branch, migration, deploy, database, Vercel, Neon.

## 2. What the builder agent does every time

A fixed workflow, written into the starter's `AGENTS.md` and enforced by `lightbulb app` where it can
be, assembled from the best of each product:

1. **Read before asking.** The room thread, the workspace's name and brand, any URL, screenshot or
   file in the request. (Lovable import, Base44 attachments, Framer.)
2. **Ask at most one card**, only for what can't be read: the name if missing (accept a
   placeholder), the look for a website with no brand. Never the stack, hosting, database or stages.
   (Lovable question cards; Stripe's starter-to-product.)
3. **Build frontend first with sample data**, then persist data when the feature needs it, then
   sign-in when the data is private. (Lovable.) The kind of app sets which of these happen (§3).
4. **Verify cheapest-first**: build, logs, a small test whenever a rule changes (price, limit,
   permission, who can do what), then one pass in the shared browser. (Lovable Oct 2026, Replit App
   Testing.)
5. **"Done" is checked by code, not claimed.** `lightbulb app up` refuses to post a preview unless the
   app built, its health check passed and a browser pass ran after the last edit. (bolt.diy's server
   rejects "complete" without fresh evidence.)
6. **Cap blind fixes.** After two failed fixes on the same error, stop patching: list what was tried,
   find the root cause, or propose undo. Hard step cap; stop honestly. (Lovable, bolt.diy.)
7. **Safe by default.** Add-only schema changes; destructive changes need an approval that shows the
   SQL. Secrets only through a form, never in the thread. Generated apps start read-only or
   undoable; delete, pay, send, publish only on explicit request. (Lovable.)
8. **Post the proof with the link**: a recording or screenshot of the main flow working. (Replit.)
9. **Remember lessons itself.** After any resolved error loop, the agent writes what broke and what
   fixed it into the app's notes and reads them before touching that area again. (Lovable makes the
   person paste these; a persistent room agent shouldn't need to.)

## 3. Kinds of app: one starter, switches

One starter (Next.js, Tailwind, shadcn, Drizzle, the door). The kind of request sets switches:

| Kind | Database | Sign-in | Design card | Builds first | Done when |
|---|---|---|---|---|---|
| Landing page / small site | No (unless a form) | No | Yes, if no brand | The page, from the brand or existing URL; SEO defaults set | Builds, links work, checked at phone width |
| Form / intake | Yes | Submitters no; viewers yes | No | Form + table + gated submissions list | A test submission appears in the list |
| Internal tool on a table | Yes | Yes, with roles | No | Import attached sheet, else sample data; list → detail → edit | Two people can't see each other's private rows |
| Dashboard | Yes if file data | Yes if private | No | Summarise the data first, post it; then cards, charts, filters | One figure checked against the source |
| Customer portal | Yes | Yes, customer role | No | The customer's view of their own rows | A customer sees only their rows |
| SaaS with payments | Yes | Yes | Yes | Landing, sign-in, checkout, then logic | A test checkout completes |
| Mobile app | — | — | — | Offer the web app added to the home screen; a native app only on a second ask, as its own project | — |
| Automation | Shares the app's | — | — | A scheduled job, off by default, with a dry run | Dry run posts what it would do |

Content-heavy websites (blogs, CMS-driven marketing sites) are the one kind where website tools beat
app builders; decline those with a sentence, take one-to-five-page sites.

## 4. What we hide, and how

- **Accounts:** Stripe services account created lazily; one form the first time. Every app after
  that: one approval card for its resources. (Tested in prod on Demo Lab, 29 Oct.)
- **Stages:** the person sees preview and live. Underneath, Stripe's way (founder's ruling): a
  preview database and a live database per app; publish applies add-only schema changes to live;
  the agent never writes to live data. Lovable tried two databases and retired them because users
  lost track of which data was where; we avoid that by never showing the person the second one and
  by seeding preview with a copy of live data.
- **Backend switch-on:** automatic when a feature needs data, covered by the one approval card.
- **Rollback:** code only ("undo"). Data restore from per-publish backups is a separate, explicit
  owner action. Every builder does it this way; restoring data with code erases rows written since.
- **Cost:** shown per app on Canvas; the app never goes dark when Lightbulb credits run out, because
  it runs on the customer's own accounts (Lovable's backend pauses at zero credits with no message to
  the app's users).

## 5. Where a room beats an editor

- **The thread is the spec.** The agent sees the conversation the request came from.
- **Approvals go to the right person**, not whoever is typing (owner cards for money and accounts).
- **The agent persists.** It maintains the app, remembers its own lessons, and watches it after
  launch (errors, unused for 30 days).
- **Proof of done is shared**: the browser pass is visible to the room.
- **Sharing is the room.** Lovable has six kinds of share; we have one: who in the room can open it.

## 6. What changes in the Apps design

1. Rewrite it around §1 (the person's loop) and §2 (the agent's workflow); move infrastructure to an
   appendix.
2. The person's words are build, change, ship, undo. The agent's commands stay (`new`, `up`,
   `publish`, `rollback`, `status`, `destroy`), but `up` gains the done gate (§2.5) and `rollback`
   becomes code-only.
3. The starter carries `AGENTS.md` with the §2 workflow, a `prompts/` file per kind of app (§3),
   `design/DESIGN.md` + tokens, and the services switches.
4. Data safety: add-only migrations by default; per-publish backups; restore is an owner action.
5. Preview database the Stripe way, seeded from live; never mentioned to the person.

## 7. Open questions for the founder

1. **Websites:** take one-to-five-page sites and decline content-heavy ones, as above?
2. **Mobile:** web app on the home screen by default, native only on a second ask?
3. **The first app's question:** is one card (name or look) the right maximum, or zero questions,
   with the preview as the only question?
