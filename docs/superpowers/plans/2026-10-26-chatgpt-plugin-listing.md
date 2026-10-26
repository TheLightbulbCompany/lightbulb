# ChatGPT plugin listing: Lightbulb in the ChatGPT Plugin Directory

**Status:** draft, 2026-10-26. Research: `docs/research/2026-10-26-chatgpt-plugins-and-app-directory.md`.
Belongs with the integrations work (`2026-10-24-integrations-master-plan.md`); it is one more app review.
It does not block the agent network release.

## Goal

ChatGPT users (Chat and Work, web, desktop, mobile) install "Lightbulb" from chatgpt.com/plugins and use
our network tools with `@Lightbulb`: find sessions, send a message, read the reply.

## Shape (fewest moving parts)

- One "With MCP" plugin on our existing hosted MCP, `https://api.thelightbulb.company/mcp`
  (OAuth through Clerk; already works as a claude.ai connector, 2026-10-26).
- Our existing skills go in the same plugin. No hooks (the directory drops them).
- The local engine stays out of the store. Inside Codex and the ChatGPT Mac app's Work mode, the
  engine's own `lightbulb` MCP already serves the tools.
- The Claude Code plugin (`packages/claude-plugin/lightbulb`) is unchanged: Task 4 (#4392) empties its
  `.mcp.json` on purpose. The ChatGPT plugin is its own small manifest that points at the hosted URL.
- Publisher: "Isol8 Inc., doing business as Lightbulb AI". US first.
- No plan prices, checkout or upgrade links anywhere in the plugin (directory rule).

## Steps

| # | Step | Owner | Needs | Done when |
|---|---|---|---|---|
| 1 | Add `readOnlyHint` / `destructiveHint` / `openWorldHint` to all 9 tools in `core/services/hosted_mcp.py` (reads: true/false/false; `send_message`, `update_task`, `use_browser`: destructive false, openWorld true) and per-tool `securitySchemes` | Claude | — | PR merged, test lists the hints |
| 2 | Add `GET /.well-known/openai-apps-challenge` (returns the token OpenAI gives us; token from config, not code) before the root mount in `main.py` | Claude | step 5 token | PR merged, curl shows the token |
| 3 | Check OAuth with ChatGPT's client: access token `aud` matches when `resource=` is sent; userinfo has `email_verified: true` | Claude | step 4 | written result; fix only what fails |
| 4 | Owner test, no review: chatgpt.com/plugins → "+" → "Add custom MCP server" → our URL; run find_sessions / send_message / check_inbox with `@Lightbulb` | Owner signs in to chatgpt.com once; Claude drives | — | the claude.ai test passes in ChatGPT Chat |
| 5 | OpenAI org verification (identity or business) at platform.openai.com | Owner | — | org shows verified |
| 6 | Submission package: privacy/terms/support/website URLs (exist on thelightbulb.company — confirm), 5 positive + 3 negative test cases, demo video, reviewer account (no MFA, no email codes, no magic link; sample sessions and a reply) | Claude drafts, owner records the video | steps 1-4 | package in this folder's PR |
| 7 | Submit at platform.openai.com/plugins, "With MCP" | Owner | steps 1-6 | submitted; date noted |
| 8 | Answer review feedback; expect 2 weeks to 4 months | Claude + owner | step 7 | listed |

## Risks

- Review is slow and not expedited.
- The MCP host is locked after approval: keep `api.thelightbulb.company/mcp`.
- `send_message` and `use_browser` read as broad: describe them plainly, keep `use_browser` out of the
  submitted tool list if a reviewer objects.
- Enterprise and Edu admins must enable plugins; Business has them on.
- Which plans may add a custom MCP server (step 4) is unclear: if the owner's plan cannot, step 4 moves
  after approval.

## Not now

- A Desktop-only plugin with the local stdio engine (needs an OpenAI rep to allowlist our org).
- Widgets / UI components.
- Countries beyond the US.
