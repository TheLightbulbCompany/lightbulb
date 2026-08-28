// Slack app manifest the setup wizard has users paste into "Create app from
// manifest". Mirrors OpenClaw 6.6's canonical buildSlackManifest
// (extensions/slack/src/setup-shared.ts) — 23 bot scopes / 15 events + App Home
// + assistant view — rebranded to "Isol8 Agent".
//
// These scopes gate BOTH the Slack channel relay AND the agent's bundled `slack`
// tool: the tool's react / pin / files / read / channel-post actions reuse this
// same bot token, so the old 6-scope manifest silently broke them with
// missing_scope at runtime (the action gates check config, not granted scopes).
//
// The slash command is intentionally omitted: OpenClaw's slash handler is
// disabled-by-default (channels.slack.slashCommand.enabled), so declaring one
// here would only surface a dead command. Re-add it together with that config
// flag if slash support is ever wanted.
//
// Guarded by slackManifest.test.ts so a future edit can't silently drop scopes.
export const SLACK_APP_MANIFEST = `display_information:
  name: Lightbulb Agent
  description: Connect Slack to your Lightbulb agents.
features:
  bot_user:
    display_name: Lightbulb Agent
    always_online: true
  app_home:
    home_tab_enabled: true
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  assistant_view:
    assistant_description: Lightbulb Agent connects Slack assistant threads to your Lightbulb agents.
    suggested_prompts:
      - title: What can you do?
        message: What can you help me with?
      - title: Summarize this channel
        message: Summarize the recent activity in this channel.
      - title: Draft a reply
        message: Help me draft a reply.
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - assistant:write
      - channels:history
      - channels:read
      - chat:write
      - commands
      - emoji:read
      - files:read
      - files:write
      - groups:history
      - groups:read
      - im:history
      - im:read
      - im:write
      - mpim:history
      - mpim:read
      - mpim:write
      - pins:read
      - pins:write
      - reactions:read
      - reactions:write
      - usergroups:read
      - users:read
settings:
  event_subscriptions:
    bot_events:
      - app_home_opened
      - app_mention
      - assistant_thread_context_changed
      - assistant_thread_started
      - channel_rename
      - member_joined_channel
      - member_left_channel
      - message.channels
      - message.groups
      - message.im
      - message.mpim
      - pin_added
      - pin_removed
      - reaction_added
      - reaction_removed
  socket_mode_enabled: true`;
