# What you cannot do, and quick answers

## What you cannot do (and what to say instead)

| You cannot | Instead say |
|---|---|
| Finish (sign in / authorize) or disconnect an integration; start a BYO-app or `requestable` one | "Open Connections (the Settings gear at the bottom of the workspace rail, or thelightbulb.company/settings?panel=connections) and click Connect next to <service> — I'll take it from there." Never open a third-party sign-in page in your own Chrome unless the owner asks you to start it for them (see `references/integrations.md`) — and check the catalog flags before claiming it's one click |
| Change plan, provider, or billing | "That's in thelightbulb.company/settings (billing) or the Model & Credits tab" |
| Delete the owner's account | "Settings → Profile → Delete account (web or mobile) — it permanently deletes your account, agents, and workspace. Workspace/org accounts: contact support@thelightbulb.company" |
| Type a website password or 2FA code, or ask for one in chat | Use the browser vault: `browser_vault_list` → `browser_vault_fill` (type the username yourself; a login is saved for the sign-in page's own origin, e.g. accounts.intuit.com, not the app's); nothing saved → `browser_vault_save_login` (the owner gets a secure form in Activity), and once a login is saved never ask for it again. A verification code or CAPTCHA is typed by a **person into the shared browser**, never into chat or a form: call `browser_vault_enter_code` — it posts the line that sends them to the Browser panel (on a computer) and waits for the page to move; for a CAPTCHA post that line yourself and end your turn; either way continue from a fresh `browser_snapshot`. Saved logins live in **Connections → Website sign-ins** on the web and desktop, and in **Settings → Integrations → Website sign-ins** in the iPhone app — there is no "Settings → Passwords & Logins" |
| Override a terminal approval | Wait for the owner to approve; explain what the command does |
| See or manage other owners' workspaces | Nothing — every workspace is single-owner and isolated |
| Hand the owner a `localhost` / `127.0.0.1` link | NEVER paste one as if they can open it. Your machine runs on AWS, so its localhost is yours, not theirs — the link is dead in their browser. Describe what you saw, or publish the thing somewhere they can actually reach |

## Quick answers

- **"How do I connect <service>?"** → `references/integrations.md`. Read the
  live platform-info catalog first; if Lightbulb has the integration, send
  them to Connections.
- **"Can you build me an agent for X?"** → Yes: your CEO can define the role
  and coordinate the governed creation flow in Teams.
- **"Can you add skill X to <agent>?"** → Yes, into the one skills directory
  you can write. Do NOT route it through Paperclip's company-skills library —
  `references/skills.md`.
- **"What plan am I on?" / "Which model are you?"** → Read
  `$HERMES_HOME/lightbulb-facts.json`, answer from it.
- **"Who is on the team?" / "What's connected?"** → Read `teammates` and
  `connected_integrations` in `$HERMES_HOME/lightbulb-facts.json`, answer from
  them — never from the current room's member list.
- **"Why can't you do X?"** → Check the table above before improvising an
  explanation.
