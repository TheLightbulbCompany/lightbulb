// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

// The live conversion paywall must attach to the newest TRIAL-CODED error
// card — not the newest bubble. Reproduces the prod incident (2026-08-09
// 17:58:44): a mid-run daily-cap death emits the reclassified
// trial_daily_cap error and then OpenClaw's raw "all models rate-limited"
// frame 21ms later, so the cap card is reliably NOT the last bubble, and the
// newest-bubble predicate demoted the paywall to a settings link in exactly
// the scenario it exists for.

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ startCheckout: vi.fn() }),
}));
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: null, isLoaded: true }),
}));

function assistantErrorGroup(key: string, code: string | undefined, text: string): RenderGroup {
  return {
    side: "assistant",
    key,
    items: [
      {
        kind: "message",
        key: `${key}:m`,
        message: {
          role: "assistant",
          errorCode: code,
          content: [{ type: "text", text: `Error: ${text}` }],
        },
      },
    ],
  };
}

describe("MessageList — paywall attaches to the newest trial-coded card", () => {
  it("keeps the cap card interactive when a raw agent error lands after it", () => {
    render(
      <MessageList
        renderGroups={[
          assistantErrorGroup("g1", "trial_daily_cap", "You've hit the cap"),
          assistantErrorGroup("g2", "unknown", "All models are temporarily rate-limited."),
        ]}
        agentName="Friday"
      />,
    );
    // The picker renders (interactive paywall), not the settings link.
    expect(screen.getByRole("button", { name: /powered by claude/i })).toBeTruthy();
    expect(screen.queryByRole("link", { name: /upgrade to keep going/i })).toBeNull();
  });

  it("blesses only the newest of two trial-coded cards — no stacking", () => {
    render(
      <MessageList
        renderGroups={[
          assistantErrorGroup("g1", "trial_daily_cap", "cap one"),
          assistantErrorGroup("g2", "trial_daily_cap", "cap two"),
        ]}
        agentName="Friday"
      />,
    );
    // Exactly one live picker; the older card falls back to the linked form.
    expect(screen.getAllByRole("button", { name: /powered by claude/i })).toHaveLength(1);
    expect(screen.getByRole("link", { name: /upgrade to keep going/i })).toBeTruthy();
  });

  it("renders no paywall when no trial-coded card exists", () => {
    render(
      <MessageList
        renderGroups={[assistantErrorGroup("g1", "unknown", "boom")]}
        agentName="Friday"
      />,
    );
    expect(screen.queryByRole("button", { name: /powered by claude/i })).toBeNull();
  });
});
