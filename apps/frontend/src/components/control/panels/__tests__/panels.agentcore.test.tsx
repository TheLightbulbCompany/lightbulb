/**
 * M6 task 10-E: what Skills and Skill Proposals show on a migrated (AgentCore + Hermes)
 * workspace. Ruling M6-R50: Hermes's own skills and providers — nothing OpenClaw-shaped is
 * emulated, and nothing fires before the runtime is known. Sessions is NOT here: ruling
 * M6-R54 hides that panel on this runtime (see ControlPanelRouter.agentcore.test.tsx).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";

import { SkillsTab } from "../SkillsTab";
import { SkillProposalsPanel } from "../SkillProposalsPanel";

let runtimeHost: "ecs" | "agentcore" | undefined;
const rpcMethods: (string | null)[] = [];
const mockCallRpc = vi.fn();
const rpcData = vi.fn();

vi.mock("@lightbulb/shared", () => ({ useRuntimeHost: () => runtimeHost }));
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
  runtimeHost = "agentcore";
});

describe("SkillsTab on an AgentCore workspace", () => {
  it("holds skills.status until the runtime is known rather than guessing ECS", () => {
    runtimeHost = undefined;
    render(<SkillsTab />);
    expect(rpcMethods.every((method) => method === null)).toBe(true);
    expect(screen.queryByText("Skill Store")).toBeNull();
  });

  it("disables a skill through config.patch, carrying every name that stays off", async () => {
    rpcData.mockReturnValue({
      skills: [hermesSkill(), hermesSkill({ name: "shopify", skillKey: "shopify", disabled: true })],
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

  it("still sends skills.update on ECS", () => {
    runtimeHost = "ecs";
    rpcData.mockReturnValue({ skills: [hermesSkill()] });
    render(<SkillsTab />);
    fireEvent.click(screen.getByRole("button", { name: /^disable$/i }));
    expect(mockCallRpc).toHaveBeenCalledWith("skills.update", { skillKey: "pdf", enabled: false });
  });

  it("points the hub search at Hermes, not ClawHub", () => {
    rpcData.mockReturnValue({ skills: [hermesSkill()] });
    render(<SkillsTab />);
    fireEvent.change(screen.getByPlaceholderText(/search skills/i), {
      target: { value: "pdf" },
    });
    expect(screen.getByRole("button", { name: /Search Hermes skills hub for/i })).toBeInTheDocument();
  });
});

describe("SkillProposalsPanel on an AgentCore workspace", () => {
  it("explains that nothing can be proposed instead of implying an empty inbox", () => {
    rpcData.mockReturnValue({ proposals: [], reason: "write_approval_disabled" });
    render(<SkillProposalsPanel />);
    expect(screen.getByText(/your agents write skills directly/i)).toBeInTheDocument();
  });
});

