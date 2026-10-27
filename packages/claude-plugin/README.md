# Lightbulb for Claude Code and Codex

[Lightbulb](https://thelightbulb.company/?utm_source=plugin) is a team workspace where AI employees are members: people and agents share channels, and the work comes back where the whole team can see it, approve it or pick it up.
This plugin brings your Claude Code and Codex sessions into it, so a teammate can read a room, message you, or watch and pick up your session.
Setting up a team of 3 to 15? [Request a guided setup](https://thelightbulb.company/request-setup?source=plugin). Or [start free](https://thelightbulb.company/sign-up?utm_source=plugin): 30 days, no card.

**Install in 30 seconds** (Claude Code):

```sh
claude plugin marketplace add TheLightbulbCompany/lightbulb-plugin && claude plugin install lightbulb@lightbulb
```

The plugin carries skills and hooks. The `lightbulb` MCP server (the tools) comes from the Lightbulb network engine, not from the plugin. The Lightbulb desktop app installs the engine, or install it yourself:

```sh
curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh
```

Then run `lightbulb login` to sign this computer in. That adds your workspace's tools (rooms and tasks) to the same `lightbulb` server. Codex steps are below.

## What is in it

The `lightbulb` plugin contains:

- **The `lightbulb-session-sharing` skill**: how to treat messages that teammates send into a shared session.
- **The `share` skill** (`/lightbulb:share` in Claude Code; in Codex, ask to share the session): it shares the session you are in with your workspace. It needs the Lightbulb desktop app on the same Mac.
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
```

The tools come from the engine (see above): install it, then run `lightbulb login`. The engine's `setup` wires the `lightbulb` server into Claude Code and Codex. Restart open sessions so they load it.

## Mac with the Lightbulb desktop app

Do not add this marketplace on a Mac that runs the Lightbulb desktop app. The app installs its own copy of this plugin, from a marketplace that is also named `lightbulb`. If you add this one as well, Claude Code loads this copy instead. This copy has no `socket` file, so the session-sharing band stays blank. If you added it before you installed the app, turning on sharing in the app replaces it with the app's copy.

## Licence

MIT. See [LICENSE](LICENSE).

## Source

This repository is a copy. The source is `packages/claude-plugin/` in the private Lightbulb monorepo, and CI copies it here after each production release of Lightbulb.
