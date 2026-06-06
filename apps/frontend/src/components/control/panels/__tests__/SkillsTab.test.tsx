import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { SkillsTab } from "../SkillsTab";

const mockData = vi.fn();
const mockMutate = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({ data: mockData(), error: undefined, isLoading: false, mutate: mockMutate }),
  useGatewayRpcMutation: () => vi.fn(),
}));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

describe("SkillsTab", () => {
  beforeEach(() => {
    mockData.mockReset();
    mockMutate.mockReset();
  });

  it("renders the skill store header from a skills.status report", () => {
    mockData.mockReturnValue({
      skills: [
        {
          name: "memory",
          description: "",
          source: "bundled",
          skillKey: "memory",
          always: false,
          disabled: false,
          blockedByAllowlist: false,
          eligible: true,
          requirements: { bins: [], env: [], config: [], os: [] },
          missing: { bins: [], env: [], config: [], os: [] },
          install: [],
        },
      ],
    });
    render(<SkillsTab />);
    expect(screen.getByText("Skill Store")).toBeInTheDocument();
    expect(screen.getByText("memory")).toBeInTheDocument();
  });
});
