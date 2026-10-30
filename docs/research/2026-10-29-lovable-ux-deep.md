# Lovable's user experience, screen by screen (29 Oct 2026)

**Why this exists.** Lightbulb is adding Apps: a person asks an agent in a room for an app and gets a hosted, shareable one. This file walks Lovable's UX screen by screen, to see what makes app-building effortless for non-technical people. It goes deeper than `2026-10-29-how-lovable-builds.md` (dev versus prod, backend, agent loop) and `2026-10-27-small-software-stack-teardown.md` §2 (stack, Cloud, pricing) and does not repeat them. The subject here is the UI: what is on screen, the exact words, and where a person has to decide something.

**Sources and tags.** **[P]** primary: Lovable's own docs, changelog or site, fetched 2026-10-29. The whole docs tree came from one file, `https://docs.lovable.dev/llms-full.txt` (321 pages). Docs citations are written `docs/<path>` for `https://docs.lovable.dev/<path>`. **[S]** secondary. **[U]** unverified. lovable.dev's homepage returned 403 (Cloudflare), so homepage copy is secondary; lovable.dev/templates did load. No Reddit or X. Quotes are under 15 words.

**Caveat.** The docs show the product as Lovable intends it, not where people abandon it. Troubleshooting entries are the best evidence of real sticking points.

---

## 1. First session: landing, sign-up, first prompt, first build, first preview

**Landing.** The homepage leads with "Build something Lovable" and one central prompt box [S] https://www.saasui.design/pattern/landing/lovable. A floating prompt bar follows the visitor down the page [P] docs/changelog. A visitor types before having an account: "Lovable asks you to sign up when you submit, keeping your prompt through sign-up" [P] docs/introduction/getting-started. **The prompt comes before the account.**

**Sign-up.** Email, Google, GitHub, Apple, or SSO on Business+ [P] docs/introduction/create-an-account. No questionnaire, no tour. Lovable "automatically creates your first personal workspace on the free plan" (5 build credits a day, 30 a month, plus a daily chat allowance) and opens the dashboard [P] same. A fraud check can block sign-up ("Registration denied due to suspicious activity"), and the person cannot lift it [P] same.

**The dashboard prompt box.** "Front and center", with the placeholder "Ask Lovable to create…" [P] docs/introduction/getting-started. A June 2026 teardown captured the greeting "Got an idea, <name>?" [S] https://ai-design-patterns.pages.dev/teardowns/lovable/composer. Around the box [P] docs/introduction/dashboard-overview:
- **Microphone**: dictation that removes "filler words" [P] docs/features/projects/chat.
- **Mode picker**: **Build** ("Make changes directly", default, remembered per browser), **Plan** ("Detailed spec for complex builds"), **Chat** ("Ask anything, explore ideas"; the send button reads **Chat for free**).
- **`+` menu**: **Import project to Lovable** (guided exports from Replit, Manus, Bolt, Base44 and v0, a code ZIP, or a website URL), **Attach** (files or a browser screenshot), **Design** (template on Business+, design system on paid), **Connectors**, **Databases** (built-in backend or own Supabase, up front).

**Suggested prompts.** The docs describe no chips on the empty dashboard [U for the live homepage]. The Quick start offers four copyable starters (cafe site, equipment tracker, revenue dashboard, ceramics store), each ending "No login or database yet". The stated reason: "Keeping your first version free of logins and databases means there is less to go wrong" [P] docs/introduction/getting-started. **Lovable teaches people to leave the backend out of the first build.**

**Attachments and other starts.** Up to 10 files per message: images, PDF, Office files, audio. The person can draw on an image before sending ("circle the part you want changed") [P] docs/features/projects/chat. A dropped `.fig` is parsed in the browser to match style, "not to rebuild every screen". A Figma plugin and a live Figma MCP (desktop app) also exist [P] same, docs/integrations/figma. Other starts: remix a template, **Build with URL** (a link carrying a prompt), or a **Create a new project?** card from Chats [P] docs/features/projects/overview.

**Import reports in two status lines.** The chat shows **Ready** and **Still needed**. It says "code migration complete" until data and users are verified, then "migration complete". It asks **Next: add your existing data**, with **Skip for now** [P] docs/features/projects/import-project. Website import first asks the person to confirm ownership, then compares the original and the rebuild "side by side, at desktop and phone size" [P] docs/features/projects/import-website.

**Before the build: questions, not a form.** When the prompt leaves the look open, a card asks up to four questions before any code is written: **Color palette** as swatches, **Typography** as font pairs "shown in its own fonts", **Layout** as wireframes, and optionally the content [P] docs/features/design-guidance. The first option in each is pre-selected and each has a free-text field. **Skip all** means "Lovable continues with reasonable defaults" and is never taken "as approval for additional work" [P] docs/features/projects/chat. The questions are free.

**The first build.** "The first version takes a few minutes, and you can watch it take shape" [P] docs/introduction/getting-started. On screen [P] docs/features/projects/chat, docs/features/agent-mode:
- **Activity cards**, one per action (file edit, command, search, browser test, subagent), and **tasks** on bigger runs.
- A **Details view** that takes over the preview, with **Timeline** and **Changes** tabs, and **Hide details** to return.
- **Working for** and **Credits used** counters showing the amount actually charged, not an estimate.
- An offer of browser notifications. Closing the tab is safe: the "request runs on Lovable's servers".
- The live preview updating as it goes. This can be switched off so the preview only shows finished changes [P] docs/features/projects/preview.

**The first preview.** Chat on the left, the running app on the right. The first instruction is "Click around the preview". The reassurance: "nothing you do next can break it permanently" [P] docs/introduction/getting-started. Next-step **suggestion chips** appear by the input. A chip fills the box without sending it [P] docs/features/projects/chat. The Quick start names the "most common surprise for new users: your live site is a snapshot" [P] docs/introduction/getting-started.

## 2. The workspace layout (the editor)

**Two halves and a top bar.** "The chat panel on the left", "the preview on the right", and everything else "one or two clicks away in the top bar" [P] docs/features/projects/editor. On a phone the halves become swipeable views [P] docs/integrations/lovable-mobile-app.

**Top bar.**
- **Left**: the project name, which shows whether you are on main or a draft and opens the **draft switcher**. Next to it a **History** toggle and **Hide chat panel**.
- **Toolbar tabs**: **Preview**, **Files** (generated or uploaded documents), **Code**, **More**.
- **Right**: preview-toolbar toggle, **Comments**, collaborator avatars, **Share**, **Publish** [P] docs/features/projects/editor.

**More** holds the "running the app" tools: Analytics, Database, Users, Storage, AI, Payments, Emails, Connectors, Security, SEO & AI search, Usage, Settings. The key rule: "sections tied to a feature appear once you use it" [P] same. **The backend UI grows as the agent adds backend features.**

**Preview bar.** [P] docs/features/projects/preview
- **Device toggle**: **Desktop view**, **Tablet view**, **Mobile view**.
- **Page selector**: **Find page or enter path**. Each page has a **Social** card (link-share preview) and a **Search** card. This shows SEO without the word "metadata".
- **Refresh**: Shift-click restarts the environment.
- **Open in new tab**: needs a login.
- Idle pause: **Still building?** with **Keep building**.
- Errors, each with **Try again**: "Live preview couldn't start", "lost connection", "stopped responding". The docs give a ready sentence to send back to chat for each. **Every error state hands the person a sentence to send.**

**The preview toolbar replaced Visual Edits.** It "replaces the previous Visual edits experience. Instead of using a separate editing panel…" [P] docs/features/preview-toolbar. It floats, docked bottom-centre, and can be dragged, minimized or hidden.

| Mode | Key | What clicking does | Cost |
|---|---|---|---|
| **Select elements** | S | The element (Cmd-click for several) becomes a chip in the chat input; describe the change | Build credits |
| **Edit text inline** | T | Type on the page, then **Send** | 100 free per 24 h, then credits |
| **Draw annotation** | D | Sketch; "Rough shapes are recognized and cleaned up" | Build credits |
| **Add a comment** | C | Pin a thread to an element | Free; sending it to Lovable costs credits |

[P] same. **There are no colour pickers or spacing sliders.** The bet is point-then-describe: "'Make this the primary color' is enough" [P] same. The look is set by the design questions, **Design systems** (paid) and **Instructions**.

**Code tab.** File tree, search, and line references (click **+** on a line to cite it in chat). Read-only on Free (**Read only** + **Upgrade**). On paid plans, saving "doesn't use credits", "Each save becomes a version", and "Lovable doesn't review your manual edits" [P] docs/features/code-mode. It opens with "You never need the code editor to build with Lovable" [P] same.

**Chat panel tools.** The `+` **Chat actions** menu covers project tools (History, Instructions, Design system, Settings, GitHub), **Add context** (skills, other projects, connectors, code, Figma, **Take a screenshot**) and **Attach files**. `@` mentions; `/` runs a skill; `/goal` makes a long-running goal [P] docs/features/projects/chat.

**Settings.** Tabs **General**, **Instructions**, **Domains**, **Git**, plus **Secrets** and **Logs** once a backend exists [P] docs/features/projects/settings. General holds:
- URL subdomain.
- **Live preview**.
- **Hide Lovable badge** (Pro+, takes effect with no republish).
- **Visitor analytics** (on by default).
- **Auto-fix security issues**.
- **Public remixing** (off by default).
- Move or transfer.
- **Danger zone**: type the name to delete; restorable for 60 days.

The page says "Access control does not live here"; it is in **Share** [P] same.

**Instructions (formerly Knowledge).** Two text boxes up to 10,000 characters: **Workspace instructions** (admins) and **Project instructions** (editors), "always included in context". **Skills** load on demand [P] docs/features/knowledge. The docs' own example is a developer style guide (strict TypeScript, Zustand). This surface is written for teams with engineers.

**Visibility: two separate switches.** **Project access** ("who can view and edit your project in the editor") versus **Website access** ("who can visit the published app") [P] docs/features/project-visibility. The docs list worked combinations ("Internal team app: Workspace / Workspace"; "Private work-in-progress, public app: Restricted / Public"). Public projects were removed on 22 Apr 2026, and the default has been **Workspace** since Dec 2025 [P] same.

## 3. Iteration: change requests, fixing, cost, being told what happened

**A change request is plain chat.** "Describe the outcome you want rather than the implementation" [P] docs/features/projects/chat. "Make the whole site feel warmer and less corporate" counts as "a perfectly good prompt" [P] docs/introduction/getting-started. Precision comes from what is attached, from loosest to tightest: words, a selected element, a sketch, an annotated screenshot, `@file` or `@line`, then `@project` to reuse another app [P] docs/features/projects/chat, docs/features/code-mode.

**No waiting.** Follow-ups sent mid-run are picked up "at its next natural stopping point, usually within seconds", and show grayed out until then. Try to fix, inline text edits and comment threads are labelled **Runs after the current task**. One list still beats many messages: "more reliable with one list than with a stream" [P] docs/features/projects/chat.

**Mode boundaries are shown as cards.** In Chat mode, asking for a change shows a **Start building** card marked **(Uses credits)**. **Build** turns it into **Switched to Build mode**; **Skip** gives **Kept Chat mode**, and Lovable "describes the change instead". Describing a problem or an idea never triggers the card [P] docs/features/chat-mode. Plan costs a flat 1 credit plus research, and "can still end without a plan" [P] docs/features/plan-mode.

**How the person learns what happened.** Each reply "tells you what changed and what you can do next" [P] docs/features/projects/chat. Under it [P] same:
- **Undo latest edit** (confirmed with **Undo**), or **Revert to this version** on older replies.
- **Details**.
- **Bookmark in history**.
- Next-step chips.
- **Preview**, which shows the app "as it was right after that message". No revert needed.

Long runs end with a summary of "what is done and what remains" [P] docs/features/agent-mode. A thank-you-only message gets a thumbs-up and "doesn't use credits" [P] docs/features/chats.

**Rewrite instead of arguing.** Hover your own past message, **Edit message**, then **Revert and resend**: the project rewinds and the new text runs [P] docs/features/projects/chat. Nobody types "no, I meant…" into a growing thread.

**Errors.** A build error puts **Try to fix** on the activity card ("scans the logs, finds the issue, and attempts a fix"). A failing build swaps the Publish button for the same **Try to fix** [P] docs/features/projects/chat. When that is not enough, the docs give a three-step ladder:
1. Ask Lovable to investigate ("Walk me through what's happening").
2. Revert and re-prompt.
3. Plan before rebuilding.

**Credit cost per action, as the person meets it.**

| Action | Cost |
|---|---|
| Build message | Usage-based. Docs examples: "Make the button gray" 0.50, "Add authentication" 1.20, "Build a landing page with images" 2.00 [P] docs/introduction/credits-and-usage |
| Plan message | 1 credit plus research [P] same |
| Chat mode / Chats | Daily allowance first, shown with a **Free** badge; "temporary… through October 31, 2026" [P] same |
| `/goal` | "costs noticeably more" [P] same |
| Try to fix, security fixes | 10 free per account, each renewing 24 h after use [P] docs/features/projects/chat |
| Comments, drafts, manual code edits, scans, SEO review, publishing from the dialog | Free [P] docs/features/drafts, docs/features/code-mode, docs/features/security, docs/features/seo-aeo, docs/features/publish |
| Publishing by asking in chat | Build credits [P] docs/features/publish |

There is no upfront estimate, "because cost depends on work that is discovered during execution" [P] docs/features/agent-mode. Instead:
- A live **Credits used** counter.
- A **credit check-in** at 20 credits that pauses the run with **Continue** (optionally setting a new level) or **Finish up**, plus **Don't ask again** [P] docs/introduction/credits-and-usage.
- At zero, a card with **Add credits** or **Finish up**, then **Resume** [P] docs/introduction/out-of-credits.
- Members who cannot pay see **Workspace out of credits** [P] same.

**One balance pays for everything, including the live app.** Credits pay for building, chat, hosting and backend ("Run credits"), AI calls inside the app, and connectors [P] docs/introduction/credits-and-usage. At zero, "Building stops", in-app AI "stop[s] working" with "no message for the failed calls themselves", and "Built-in backend services pause" while static pages stay live. Recovery needs a manual **Wake up** [P] docs/introduction/out-of-credits. **A builder's balance can take a customer's app down.**

## 4. Backend moments

**The exact moment.** There is no "add a backend" step. "When a feature you ask for requires backend functionality… Lovable enables the backend automatically or asks for your approval in the project chat first", depending on the **Enable Cloud** permission (default **Always allow**) [P] docs/features/cloud. For most people, typing "add login" or "save the submissions" is the moment. The visible sign is **Database**, **Users** and **Storage** appearing under **More**. The `+` → **Databases** menu is the opt-in path for people who already know they need a backend [P] docs/introduction/dashboard-overview.

**What is asked.**
- **Region** (Americas, Europe, Asia Pacific) is shown only under "Ask each time". It is otherwise chosen silently, and "You cannot change a project's region after the backend is enabled" [P] docs/features/cloud. **A one-way choice sits behind a default.**
- **Ten agent permissions**, each Always allow, Ask each time or Never allow. All are Always allow except **Execute backward incompatible database migrations**, which is Never allow. Under Ask, "Lovable shows you the SQL and asks for your approval". They are a per-person setting in **Account settings → Agent permissions** [P] same.
- Enabling the database, users or storage "provisions all three". This cannot be done from a draft [P] same.

**Auth.** "Ask Lovable to add login, and it generates the signup and login pages… and protects user data with row level security" [P] docs/features/authentication. Email is the default. Google, Apple, Microsoft and "Sign in with Lovable" are **Managed by Lovable** with no credentials needed. The **Users** view has a **Signups** chart, a user list, **Add user** (**Send invitation** / **Create new user**) and **Sign in methods** with **Enabled**/**Disabled** badges [P] same. For internal tools on Business+, "Workspace identity reuse lets your app treat that existing login as its login", with no login page [P] docs/features/lovable-workspace-identity-reuse. The docs say four identity features "sound alike but do different jobs" and include a table to tell them apart, a sign of past confusion [P] same.

**Secrets.** "You usually don't add secrets by hand… Lovable prompts you… through a secure input in the project chat" [P] docs/features/secrets. The card shows:
- the variable name, with a preceding message on why it is needed and where to get it;
- an example value and a format warning;
- **Skip** ("Anything that depends on the secret won't work until you add it");
- for values the person invents, **Generate** and **Copy** ("you cannot view it again").

A key pasted into chat is saved as a secret anyway, and Lovable offers **Connect** to turn it into a connector [P] same. Yet the import guide says "Lovable never asks for… API keys… in the chat" [P] docs/features/projects/import-project. **In practice the agent asks through a form, and pasted keys get rescued.**

**Email.** Paid plans only. "Lovable handles domain verification, DNS configuration, email authentication (SPF… DKIM… DMARC)". There are six auth templates, and app emails are generated on request with unsubscribe added automatically. 50,000 emails a month are included, with hourly caps and a warning at 80% [P] docs/features/custom-emails.

**Payments, the most guided flow.** [P] docs/features/payments
1. The person asks ("Add a pricing page… $29/month subscription").
2. Lovable recommends **Paddle** (merchant of record, 5% + 50¢) or **Stripe**.
3. An **Enable payments** dialog appears, then **Continue**.
4. A short form creates the provider account. Paddle asks for a new email ("use a plus alias"), name, and project name ("statement descriptor"). Stripe asks for email, name and country.
5. Products, trials and discount codes are set up by chat.
6. The preview runs in **Test mode** with a banner and 4242 test cards; the published app is **Live**.
7. A **Go-live checklist**: **Run check** (privacy, terms and refund policy, "Site content is genuine"), then **Complete verification** (KYC), which can take days.

The **Payments** tab is always visible. Its empty state, **Accept payments** → **Explore payments**, hands the job back to chat [P] same. The docs warn that deleting a Lovable webhook means "purchases still succeed, but subscriptions never activate" [P] same.

**What Cloud costs, as seen.** Nothing at first: a 20-credit monthly Cloud grant. Cost drivers are listed in plain words (database size, data, network, storage, compute, realtime) [P] docs/introduction/credits-and-usage. **More → Usage** shows one project's cost with **Build** and **Run** filters. Asking "Why is this project using so many credits?" makes Lovable investigate [P] docs/introduction/out-of-credits.

| Automatic by default | Asked |
|---|---|
| Backend on, region, tables and policies, auth pages, edge functions, webhooks, email DNS, platform keys, page metadata | Third-party keys (secret form), payment account form, go-live verification, breaking schema changes (blocked), domain purchase (confirmation card plus checkout), website-copy ownership |

## 5. Publishing

**The dialog.** "Publishing takes two clicks, and the defaults work for most projects" [P] docs/features/publish. One page with three rows:
1. **Website URL**, pre-filled as `<subdomain>.lovable.app`.
2. The audience row (**Visible to anyone with the link**), which opens **Who can view your site?**: **Public** or **Invite only** (10 people) on Free and Pro; **Workspace** or **Custom** on Business+.
3. The scan line: **Running quick security scan**, then **No security issues found** or "2 critical security issues found". It does not block by default [P] same.

**Build errors need fixing** replaces the button with **Try to fix**. Success shows **Your website is live**, with copy and visit buttons and a search-result preview [P] same.

**Publishing by chat.** "Publish my app", "Ship it", "Go live", or "Publish to my-todos.lovable.app". "Lovable asks for approval before publishing unless you have set the publish tool to auto-approve", and it runs the same scan [P] same. **Publishing is a tool the agent calls under the same permission system as everything else.**

**Preview versus published.** Visitors see "a snapshot that only changes when you publish" [P] docs/features/projects/preview. Three signals keep this visible:
- A dot on **Publish** when the live app is behind.
- **Publish changes** replacing **Publish**.
- After metadata edits, a reply ending with a **Publish to update the live site** button [P] docs/features/publish.

"Accepting a draft is not publishing" is spelled out separately [P] same.

**Custom domains.** Paid plans only. Four entry points, including the Publish dialog's **Add domain** and chat ("find me a domain for my bakery"). "Lovable always shows a confirmation card before it buys or connects a domain" [P] docs/features/custom-domain. **Buy new domain** defaults to **1 year term** with **Auto-renew** on, and the domain belongs to the workspace. **Connect existing domain** goes through Entri, with manual DNS as the fallback. Statuses: **Setting up**, **Pending**, **Action required** (with **Complete setup**), **Failed** (with **Retry**), **Live**, **Offline**, **Suspended** [P] same. DNS jargon only shows up in failure states.

**SEO.** The title, description and favicon are generated per page while building, and edited through chat because "Metadata is set per page" [P] docs/features/publish. The person checks them in the **Social** and **Search** cards. **Run SEO scan** (free) gives plain findings, such as "Search engines see placeholder text", each with **Try to fix** [P] docs/features/seo-aeo.

**Analytics.** "Starts counting when you publish… with nothing to set up". Channels are All, SEO & AI, Social, Referral, Direct and Ads, plus Visits, Page views and Bounce rate. Every card has **Ask Lovable about this**, and **Get more visitors** opens a marketing conversation. Funnels need PostHog [P] docs/features/analytics. **Analytics leads into a conversation; it is not a dashboard to stare at.**

**After publish.** There is an optional **Trust center** at `/.well-known/trust.html` ("not an audit") [P] docs/features/trust-center. An "Edit with Lovable" badge shows unless the plan is Pro+ [P] docs/features/projects/settings.

**Unpublish.** From Settings, the dialog's ⋮ menu, or in bulk from the dashboard, which lists what is excluded "with the reason" [P] docs/features/publish, docs/introduction/dashboard-overview. "The live URL becomes inaccessible. Your project remains in the editor." Unpublish and delete are "different actions". Free projects are unpublished after 12 months without visits [P] docs/features/publish. On Enterprise, a policy can require approval: **Request approval** sends a note plus a screenshot to admins, and the states are **pending**, **approved**, **denied** and **expired** [P] same. **This is the closest Lovable gets to Lightbulb's approvals.**

## 6. Sharing and collaboration

**One Share button, five meanings.** [P] docs/features/share-project

| What | Gives | Lovable account? | Expiry |
|---|---|---|---|
| **Invite people** (email or group plus role) | Editor access: chat, code, settings | Yes | None |
| **Invite link** (**Edit access** / **View access**) | Collaborator access | Yes | 5 days; one link at a time |
| Workspace row (**Can edit** / **Can view** / **No access**) | Default access for all members | Yes | None |
| **Share a preview link** (named, **Allow comments**, password on Business+) | View-only latest unpublished version; guest comments | No | 7 days (Free/Pro); up to Never (Business+) |
| **Public remixing** (Settings, off by default) | Preview plus **Remix** (copies the code) | Only to remix | None |

The publish audience (§5) is a sixth setting. The docs keep repeating: "Inviting someone here makes them a project collaborator with access to the editor" [P] same. **This many kinds of sharing is the price of being an editor.** A non-technical owner has to tell a collaborator, a reviewer, a remixer and a visitor apart.

**Client review through preview links.** The guest clicks **Comment**, pins a note, and gives a name ("No account or email is required"). The owner gets an inbox notification. A password "is not a login" [P] same.

**Roles.** **Owner** (one; transfer, delete), **Admin** (paid), **Editor** (default; can publish), **Viewer** (paid; preview only) [P] docs/features/collaboration. "Editors can invite and remove others with the same role or lower" [P] docs/features/share-project. Projects share "the workspace's credits, no matter who sends the messages" [P] docs/features/collaboration.

**Working at the same time.** "Everyone sees changes as they happen", with presence avatars. "Each of you can work in your own draft". Everyone sees the same draft list, and edits land only on **Accept**, after "a one-line summary of what the draft changes" [P] docs/features/collaboration, docs/features/drafts. The docs teach the vocabulary as six words with a bakery example: **Main**, **Draft**, **Update**, **Accept**, **Delete**, **Publish** [P] docs/features/drafts. Drafts double as hand-off: "Let a teammate or client work in a draft, then review" [P] same.

**Comments become work.** Pins are anchored to elements and can be resolved. Typing `@Lovable` or pressing **Send to chat** hands the thread to the agent [P] docs/features/project-comments. Comments are free; sending one costs credits. **The comment thread is where human feedback becomes agent work, and it is the closest thing in Lovable to a room.**

**Remix.** The copy is independent. The dialog has **Project name** ("Remix of …"), **Target workspace**, and three off-by-default switches: **Include project history**, **Copy project files**, **Include custom knowledge**. Templates ask **Acknowledge and remix** [P] docs/features/projects/remix. Manual secrets are not copied, a Cloud copy brings "structure… without any information", and pre-remix versions cannot be reverted [P] docs/features/secrets, docs/features/cloud.

**Privacy drift.** Free projects were public by default until Nov 2025; public projects were abolished on 22 Apr 2026. One page repeats four times that "workspace owners have full access to all projects" [P] docs/features/project-visibility. Whether the shift came from leaks or complaints is [U].

**Beyond the browser.** Projects can be driven from Slack, Telegram, ChatGPT, MCP, desktop and mobile [P] docs/introduction/getting-started. "Chat elsewhere, build here" is already a supported path.

## 7. History and safety

**Code history.** "There is no save button" [P] docs/features/projects/history. The **History** panel has two tabs, **History** and **Bookmarks**. Clicking a version opens a snapshot view ("without changing anything") with **Back to latest**. Per version: **View code changes**, **Go to message in chat** ("remembering why a change happened"), **Revert** (confirmed with the date, with **View in chat** offered first) and **Bookmark**. The live version carries a **Published** badge [P] same. The docs advise: "Bookmark a known-good version before starting a big change" [P] same.

**What revert does not do.** "Reverting restores your project's code only… It does not restore or roll back your database data." It refunds no credits and is all or nothing. For a partial undo, the docs suggest a sentence: "Bring back the pricing section from before the redesign" [P] same. Very old versions say **Cannot revert this far back in history**.

**Data history is a separate, deeper screen.** **More → Database → Manage → Backups** keeps daily backups for about 14 days, each with **Restore to this backup**. The warning reads "Restoring cannot be undone", and the code "may not match the restored database". The suggested next step is to ask Lovable to test and fix the mismatch [P] docs/features/database-backups. **Undo for code is cheap and everywhere. Restore for data is rare, three levels deep, and irreversible.**

**Deletion.** Deleting a project means typing its name, and it can be restored for 60 days [P] docs/features/projects/settings. Removing the database "permanently deletes the database, its backups, your app's user accounts, and the files" [P] docs/features/cloud.

**Security.** [P] docs/features/security-view, docs/features/publish
- **Publish dialog**: a one-line, roughly 10-second Quick scan. It does not block unless an admin turns on **Block publishing with critical issues**.
- **More → Security**: two cards, **Deep security scan** (**Recommended**; 3–13 minutes, estimated up front) and **Quick security scan** (**Automatic**). Footers read **Not scanned yet**, **Up to date**, or **Run deep scan** after changes. Stale findings get a clock icon.
- **Findings are grouped by harm, not technology**: "Stop users from viewing or changing data that isn't theirs", "Protect your API keys…", "Safeguard your payment flows". Severity is **Critical** / **Warning** / **Info**, and a finding is downgraded when the app is workspace-only.
- **Opening a finding** shows **What could happen** in plain language, then **Technical details** linked to the line of code.
- **Actions**: **Try to fix**, **Try to fix all**, **Try to fix selected**, **Reference in chat**. **Ignore finding** needs a reason: **This is intended**, **I accept this risk**, **This is wrong**, **Custom**. Even "No issues found" comes with a note that the scanner "cannot catch every" issue.
- **Auto-fix** of eligible findings while building is a project toggle [P] docs/features/projects/settings. Business+ adds **Chat send protection** for personal data, with **Send redacted** [P] docs/features/sensitive-data-scanning.

**When something is dangerous, the person sees:**
- Breaking database changes refused by default, with the SQL shown under "Ask".
- Type-to-confirm on delete.
- "Restoring cannot be undone".
- A warning that public remixers "can view your project's source code" [P] docs/features/project-visibility.
- Confirmation cards before buying a domain or creating a project from chat.
- **(Uses credits)** on chat-to-build.
- Test-mode checkout until go-live.

Platform Rules violations (phishing, impersonation, malware, illegal goods, adult content, infringement) are blocked or removed, and appeals go to Trust & Safety [P] docs/introduction/support-policy.

## 8. Different kinds of app: does the UX change?

**The screens stay the same; different moments fire.** There is one prompt box, editor and publish dialog for everything, and no "choose a type" step. **Project category** sits deep in Settings [P] docs/features/projects/settings. The FAQ lists "SaaS products, internal tools, dashboards, CRMs and client portals, booking and marketplace apps, company websites", plus decks and documents [P] docs/introduction/faq.

| Kind | Moments that fire only for it | Source |
|---|---|---|
| **Marketing site / portfolio** | Design questions ("where the look matters most"); website import; then SEO scan, Social/Search cards, **Get more visitors**, domain | [P] docs/features/design-guidance, docs/features/projects/import-website, docs/features/analytics |
| **Internal tool / dashboard / admin** | No design questions ("use Lovable's standard build flow"); audience **Workspace** or **Invite only**; workspace identity reuse with no login page; data connectors (BigQuery, Snowflake, Salesforce); scan severity lowered | [P] docs/features/design-guidance, docs/features/lovable-workspace-identity-reuse, docs/features/security-view |
| **SaaS with auth and payments** | Backend switches on silently; **Users** view; payments flow (provider, form, test banner, go-live KYC); paid email; "Payments & billing" findings | [P] docs/features/cloud, docs/features/payments |
| **Physical e-commerce** | Shopify connector; "Physical products can use the Shopify integration" | [P] docs/features/payments |
| **AI app** | Built-in AI gateway with a generated key; Run credits; **More → AI**; publish the app as an MCP server | [P] docs/features/ai |
| **Mobile** | None. "Lovable builds web apps"; PWA, or Capacitor "outside of Lovable"; "does not generate projects in React Native" | [P] docs/introduction/faq |
| **Deck / document / image** | Generated into **Files**, not published | [P] docs/features/generate-files |

**Templates.** lovable.dev/templates has **All / Websites / Apps** tabs and categories such as Portfolio, SaaS, Internal Tools, Dashboards, Ecommerce, Events, Landing Page and Presentations. Example cards: "Therapist Website", "Caterer Website", "Food Truck Website", "Quiz Builder", "Survey Builder" [P] https://lovable.dev/templates. Each card has **Preview** and **Remix**, browsable while logged out [P] docs/features/projects/remix. The dashboard separates **Lovable templates** from **Workspace templates** (Business+, can be set as default) [P] docs/changelog. A template skips the design questions because "the design is already set" [P] docs/features/design-guidance. **The templates are aimed at small businesses, not developers.**

**Design systems.** Paid. A system is "a dedicated project" that other projects connect to. New releases show **Update available** in chat, and Lovable "catches raw colors, custom or customized components" and retries [P] docs/features/design-systems. The pattern worth keeping: house style is enforced by checking the agent's output, not by asking nicely.

**First-version advice differs by kind.** For sites: describe "the feel, not just the layout" and use real copy. For tools and SaaS: "build frontend-first. Start with sample data… connect the database once the product feels right" [P] docs/tips-tricks/from-idea-to-app.

## 9. Onboarding and education

**Teaching lives outside the product.** No tour or first-run overlay is documented [U]. What exists:
- The **Quick start**: 7 steps, about 10 minutes.
- **How to build a real product**: a worked "StudioBook" example that opens with a quoted user: "I have no clue what I'm doing... but I know exactly what I want to build" [P] docs/tips-tricks/from-idea-to-app.
- A four-phase **prompting playbook** and a **prompt library** of copyable recipes ("Set a calm look", "Build an internal tool without a login page") [P] docs/prompting/prompting-one, docs/prompting/prompting-library.
- **Lovable Academy**: an 11-lesson, one-hour path, plus **Blueprint (Beta)**, which turns "Your idea" into "a custom prompt chain" [P] https://academy.lovable.app/academy/learning-path. **Novices need the order of the prompts more than the prompts.**

**Habits taught** [P] docs/tips-tricks/from-idea-to-app:
1. Shape the idea in four questions (what, who, why, the one key action), and "tell a story instead of writing a spec".
2. Keep v1 to "one user, one action, one outcome".
3. Let Lovable interview you: "ask me the five questions… Don't write code yet."
4. Use Plan "for anything bigger than a tweak". Keep project instructions as "a one-page brief, not a wiki", and Lovable can draft them.
5. Frontend first; never "Lorem ipsum".
6. One change per prompt: "If you catch yourself writing 'and' more than twice, split it". Set guardrails, name the role, and bookmark each working feature.
7. "Describe the bug, not your frustration" [P] docs/prompting/prompting-debugging.

**Plan first?** Only gently. Build is the default [P] docs/introduction/dashboard-overview. Plan has a flat, predictable price. The real work is done by question cards and design questions that fire on their own when a prompt is open-ended. **Lovable does not make people plan. It makes the agent ask.**

**Nudges inside the product:**
- Next-step chips.
- A mobile check before publishing ("Most people will open your link on their phone") [P] docs/introduction/getting-started.
- Credit check-ins, and **Optimize your usage** when Run spend passes 5 credits a week [P] docs/introduction/out-of-credits.
- The publish dot, and publish buttons at the end of replies.
- SEO rerun and **Get more visitors**.
- Security auto-fix.
- Project monitoring briefs (opt-in) [P] docs/features/projects/settings.
- Commercial: an upgrade card, **Read only** + **Upgrade** in the code editor, and referral credits (10 to the invitee, 100 to the referrer on first payment) [P] docs/introduction/dashboard-overview.

**Things it refuses or holds back:**
- Platform Rules projects.
- Restricted products, steering to the other payment provider.
- Copying a website until ownership is confirmed.
- Breaking migrations; any "Never allow" action ("says so in the project chat") [P] docs/features/cloud.
- Code changes from Chat mode without the card.
- A plan when none is warranted.
- Re-importing an edited page ("Lovable refuses when the page no longer matches").
- Treating a skip as approval.
- Upfront cost estimates.

**The docs' voice.** Troubleshooting entries open with "Intended." or "Not intended." [P] docs/features/projects/import-project. The glossary splits Lovable terms from general web terms [P] docs/glossary. **The docs are written to be pasted back into the agent.**

## 10. Every user-facing concept: what it hides, what the person must still understand

| UI name | Hides | Person must still understand |
|---|---|---|
| **Project** | Repo, framework, build | One project = one app, in one workspace |
| **Build / Plan / Chat** | Agent loop, tools, subagents | Which mode changes the app, and what each costs |
| **Question card** | The spec | Skip means defaults |
| **Activity cards / Details** | Logs, diffs, commands | Nothing, unless curious |
| **Preview** | A pausing cloud dev environment | Private and latest; visitors see the snapshot |
| **Device toggle** | Breakpoints | Check mobile before sharing |
| **Preview toolbar** | DOM-to-file mapping | Which actions cost credits |
| **Try to fix** | Log triage | Once or twice, then change tactics |
| **Undo / Revert / Revert and resend** | Git history | Code only, never data |
| **Bookmark** | Git tags | Mark known-good states |
| **Draft / Accept / Update** | Branches and merges | Accept ≠ publish; one shared database |
| **Instructions / Skills** | System prompt, context | Always-on versus on-demand |
| **Publish / Publish changes** | Build, CDN, HTTPS, metadata | Live is a snapshot until republished |
| **Website URL / domain** | DNS, SSL, registrar | Paid; domains belong to the workspace |
| **Who can view your site?** | Hosting auth gate | Separate from project access |
| **Share** (four kinds) | ACLs, signed URLs | Collaborator ≠ reviewer ≠ visitor ≠ remixer |
| **Built-in backend** | Supabase, Postgres, RLS, region | It costs Run credits; the region is permanent |
| **Database / Users / Storage** | SQL, auth tables, buckets | Data is real and shared by preview and live |
| **Secret form** | Env vars, injection | Where to get a key; write-only |
| **Connectors** | OAuth, MCP, API clients | Whose account is used |
| **Payments** | Provider account, webhooks, sync | KYC takes days; legal pages needed |
| **Emails** | SMTP, SPF/DKIM/DMARC | Paid; hourly caps |
| **Security view** | RLS audits, CVEs, code review | "No issues" is not a guarantee |
| **Backups / Restore** | Postgres dumps | Irreversible; may break code |
| **Analytics** | Tracking script | Visits, not funnels |
| **Credits** (Build / Run / check-ins) | Metering, infrastructure billing | One balance builds the app *and* keeps it running |

## What Lightbulb copies, and what it must do differently in a room instead of an editor

**Copy**
1. **The prompt comes first.** In Lightbulb the room message *is* the prompt box. Nothing stands between "build me X" and the first version.
2. **The agent asks; the person doesn't plan.** Use a question card with up to four questions, options pre-selected, and **Skip all** meaning defaults, never approval for extra work. Ask design questions only where the look matters, never for tools.
3. **No backend in v1.** Build on sample data. Add data and logins when someone asks for something that must persist.
4. **The backend appears when a feature needs it, behind one permission.** Surface one-way choices such as region only when asking. Refuse breaking schema changes by default.
5. **Secrets go through a form; pasted keys get rescued.** A key never stays in the room transcript. Name the key, say why it is needed and where to find it, and offer **Skip** that says what will not work.
6. **Every reply says what changed and ends with the next action as a button**: undo, revert, **Publish to update the live site**.
7. **Live cost, no estimate, a check-in threshold**, with **Continue** or **Finish up**. Comments, drafts, the first fixes and publishing are free.
8. **Two nouns: "the draft" and "the live app."** Show a dot when live is behind. Revert is code-only and says so. Data restore is a separate, deliberate act.
9. **Security described as harms**, such as "Stop users from viewing data that isn't theirs". Quick check on every publish, **Try to fix** per finding, and a reason required to ignore one.
10. **Errors come with a sentence to send.** In a room, the agent sends that sentence itself.

**Do differently, because it is a room, not an editor**
11. **The room replaces Share.** Lovable needs five sharing mechanisms because an editor has no audience. A room's members already are the collaborators. "Who can use the app" should default to this room, with "anyone with the link" one explicit step up. Reuse room identity the way workspace identity reuse removes the login page.
12. **Comments are already messages.** Lovable's best pattern is a comment thread plus `@Lovable`. In a room, a reply to the app message is a change request. Borrow draft discipline so two members' asks do not collide: one draft per asker, and a one-line summary before accept.
13. **The preview is a link, not a pane.** Post a phone-first preview link (Lovable's own reasoning: "Most people will open your link on their phone"), update that same message as the build lands, and pin the live link. Pointing has to work from a screenshot reply, because there is no toolbar.
14. **Approvals belong to the room, not a settings page.** Lovable hides Ask/Always/Never in one person's preferences. In a room, consequential steps (go public, payments, buy a domain, restore data, spend past the threshold) should be approval cards the right member taps, like Lovable's Enterprise publish request with its note and screenshot.
15. **A builder's balance never silently takes a user's app down.** Lovable pauses the backend and in-app AI at zero credits, with no message to the app's users. Lightbulb apps run on the customer's own accounts, so give each app a visible running budget and warn in the room before anything pauses.
