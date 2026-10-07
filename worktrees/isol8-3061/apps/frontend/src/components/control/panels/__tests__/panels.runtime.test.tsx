/**
 * What Skills and Skill Proposals show: Hermes's own skills and providers, and
 * nothing fires before a status body has named this workspace's runtime.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";

import { SkillsTab } from "../SkillsTab";

let statusLoaded = false;
const rpcMethods: (string | null)[] = [];
const mockCallRpc = vi.fn();
const rpcData = vi.fn();

vi.mock("@lightbulb/shared", () => ({ useStatusLoaded: () => statusLoaded }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    rpcMethods.push(method);
    return {
      data: method === null ? undefined : rpcData(method),
      error: undefined,
      isLoading: false,
      mutate: vi.fn(),
    };
  },
  useRuntimeRpcMutation: () => mockCallRpc,
}));

function hermesSkill(over: Record<string, unknown> = {}) {
  return {
    name: "pdf",
    description: "Read PDFs",
    source: "productivity",
    skillKey: "pdf",
    always: false,
    disabled: false,
    blockedByAllowlist: false,
    eligible: true,
    requirements: { bins: [], env: [], config: [], os: [] },
    missing: { bins: [], env: [], config: [], os: [] },
    install: [],
    ...over,
  };
}

beforeEach(() => {
  rpcMethods.length = 0;
  mockCallRpc.mockReset().mockResolvedValue({});
  rpcData.mockReset().mockReturnValue(undefined);
  statusLoaded = true;
});

describe("SkillsTab", () => {
  it("holds skills.status until the runtime is known", () => {
    statusLoaded = false;
    render(<SkillsTab />);
    expect(rpcMethods.every((method) => method === null)).toBe(true);
    expect(screen.queryByText("Skill Store")).toBeNull();
  });

  it("disables a skill through config.patch, carrying every name that stays off", async () => {
    rpcData.mockReturnValue({
      skills: [hermesSkill(), hermesSkill({ name: "shopify", skillKey: "shopify", disabled: true })],
      disabledNames: ["shopify"],
    });
    render(<SkillsTab />);

    fireEvent.click(screen.getByRole("button", { name: /^disable$/i }));

    // NOT skills.update: the bridge refuses it, because a config.yaml write from the
    // microVM never reaches S3. A deep merge replaces a list wholesale, so "shopify"
    // (already off) has to ride along or it would be silently re-enabled.
    expect(mockCallRpc).toHaveBeenCalledWith("config.patch", {
      patch: { skills: { disabled: ["pdf", "shopify"] } },
    });
  });

  // Review I3. The bridge scans ONE profile's disk, so a skill another agent wrote and the
  // owner disabled renders in NO row here. Rebuilding the list from rows dropped it, and a
  // wholesale list replace turns that drop into a silent re-enable on every profile.
  it("keeps a disabled name that has no row, instead of silently re-enabling it", () => {
    rpcData.mockReturnValue({
      skills: [hermesSkill()],
      disabledNames: ["written-by-another-agent"],
    });
    render(<SkillsTab />);
    fireEvent.click(screen.getByRole("button", { name: /^disable$/i }));
    expect(mockCallRpc).toHaveBeenCalledWith("config.patch", {
      patch: { skills: { disabled: ["pdf", "written-by-another-agent"] } },
    });
  });

  // Review N3. Falling back to the rendered rows IS the truncating path I3 closed, so a
  // runtime that reports no `disabledNames` gets a refusal, not a guess at the right list.
  it("refuses the toggle when the runtime reports no disabledNames", async () => {
    rpcData.mockReturnValue({
      skills: [hermesSkill(), hermesSkill({ name: "shopify", skillKey: "shopify", disabled: true })],
    });
    render(<SkillsTab />);
    fireEvent.click(screen.getByRole("button", { name: /^disable$/i }));
    expect(await screen.findByText(/could silently re-enable another/i)).toBeInTheDocument();
    expect(mockCallRpc).not.toHaveBeenCalled();
  });

  // Ruling M6-R56: hub search + install are cut on this runtime. The panel is a READ.
  it("offers no hub search", () => {
    rpcData.mockReturnValue({ skills: [hermesSkill()] });
    render(<SkillsTab />);
    fireEvent.change(screen.getByPlaceholderText(/search skills/i), { target: { value: "pdf" } });
    expect(screen.queryByRole("button", { name: /Search ClawHub for/i })).toBeNull();
  });
});


