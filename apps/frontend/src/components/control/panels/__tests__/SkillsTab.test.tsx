import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
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

  it("badges skills that pair with an integration", () => {
    mockData.mockReturnValue({
      skills: [
        {
          name: "gh-issues",
          description: "",
          source: "bundled",
          skillKey: "gh-issues",
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
    expect(screen.getByText(/works with github/i)).toBeInTheDocument();
  });

  it("renders no integration chip for unpaired skills", () => {
    mockData.mockReturnValue({
      skills: [
        {
          name: "weather",
          description: "",
          source: "bundled",
          skillKey: "weather",
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
    expect(screen.queryByText(/works with/i)).toBeNull();
  });

  it("reveals the create-custom-skill helper with a copyable prompt", () => {
    mockData.mockReturnValue({ skills: [] });
    render(<SkillsTab />);
    fireEvent.click(screen.getByRole("button", { name: /create custom skill/i }));
    expect(screen.getByText(/Use your skill-creator skill/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy prompt/i })).toBeInTheDocument();
  });
});
