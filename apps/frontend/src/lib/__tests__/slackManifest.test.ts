import { describe, it, expect } from "vitest";

import { SLACK_APP_MANIFEST } from "../slackManifest";

// The manifest gates BOTH the Slack channel relay AND the agent's bundled `slack`
// tool (they share one bot token). The old 6-scope manifest silently broke the
// tool's react/pin/files/channel actions with missing_scope — guard the full
// canonical OpenClaw 6.6 set so a future edit can't quietly drop scopes again.
const REQUIRED_BOT_SCOPES = [
  "app_mentions:read",
  "assistant:write",
  "channels:history",
  "channels:read",
  "chat:write",
  "commands",
  "emoji:read",
  "files:read",
  "files:write",
  "groups:history",
  "groups:read",
  "im:history",
  "im:read",
  "im:write",
  "mpim:history",
  "mpim:read",
  "mpim:write",
  "pins:read",
  "pins:write",
  "reactions:read",
  "reactions:write",
  "usergroups:read",
  "users:read",
];

const REQUIRED_BOT_EVENTS = [
  "app_home_opened",
  "app_mention",
  "assistant_thread_context_changed",
  "assistant_thread_started",
  "channel_rename",
  "member_joined_channel",
  "member_left_channel",
  "message.channels",
  "message.groups",
  "message.im",
  "message.mpim",
  "pin_added",
  "pin_removed",
  "reaction_added",
  "reaction_removed",
];

describe("SLACK_APP_MANIFEST", () => {
  it("grants every canonical OpenClaw 6.6 bot scope", () => {
    for (const scope of REQUIRED_BOT_SCOPES) {
      expect(SLACK_APP_MANIFEST).toContain(`- ${scope}`);
    }
  });

  it("subscribes to every canonical bot event", () => {
    for (const event of REQUIRED_BOT_EVENTS) {
      expect(SLACK_APP_MANIFEST).toContain(`- ${event}`);
    }
  });

  it("enables socket mode + App Home + assistant view and stays Isol8-branded", () => {
    expect(SLACK_APP_MANIFEST).toContain("socket_mode_enabled: true");
    expect(SLACK_APP_MANIFEST).toContain("app_home:");
    expect(SLACK_APP_MANIFEST).toContain("assistant_view:");
    expect(SLACK_APP_MANIFEST).toContain("name: Isol8 Agent");
    expect(SLACK_APP_MANIFEST).not.toMatch(/OpenClaw/);
  });

  it("declares no slash command (OpenClaw's slash handler is disabled by default)", () => {
    expect(SLACK_APP_MANIFEST).not.toContain("slash_commands:");
  });
});
