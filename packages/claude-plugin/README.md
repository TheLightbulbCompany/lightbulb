# Lightbulb for Claude Code and Codex

The `lightbulb` plugin connects Claude Code and Codex to your Lightbulb workspace. It contains:

- **The Lightbulb MCP server** (`https://api.thelightbulb.company/mcp`): find rooms and shared sessions, read messages, and send messages to a room, a person or a session.
- **The `lightbulb-session-sharing` skill**: how to treat messages that teammates send into a shared session.
- **The session-sharing band** (Claude Code only): it shows who is watching a shared session and its join requests. It shows only when the Lightbulb desktop app shares the session. Without the app it shows nothing.

## Install

Claude Code:

```sh
claude plugin marketplace add TheLightbulbCompany/lightbulb-plugin
claude plugin install lightbulb@lightbulb
```

Or, at the Claude Code prompt:

```
/plugin install lightbulb --marketplace TheLightbulbCompany/lightbulb-plugin
```

Codex:

```sh
codex plugin marketplace add TheLightbulbCompany/lightbulb-plugin
codex plugin add lightbulb@lightbulb
codex mcp login lightbulb
```

The first time Claude Code or Codex uses the `lightbulb` server, it opens a browser. Sign in to Lightbulb and choose your workspace.

Clerk's token endpoint sits behind Cloudflare and rejects requests that carry a default library User-Agent (for example `Python-urllib`), so a custom MCP client must send its own User-Agent; Claude Code and Codex already do.

## Mac with the Lightbulb desktop app

Do not add this marketplace on a Mac that runs the Lightbulb desktop app. The app installs its own copy of this plugin, from a marketplace that is also named `lightbulb`. If you add this one as well, Claude Code loads this copy instead. This copy has no `socket` file, so the session-sharing band stays blank. If you added it before you installed the app, turning on sharing in the app replaces it with the app's copy.

## Source

This repository is a copy. The source is `packages/claude-plugin/` in the private Lightbulb monorepo, and CI copies it here after each production release of Lightbulb.
