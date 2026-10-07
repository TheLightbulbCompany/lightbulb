import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import { SkillsTab } from "../SkillsTab";

const mockData = vi.fn();
const mockMutate = vi.fn();
const mockMutation = vi.fn();
// The panel holds its RPC until a status body has named this workspace's runtime.
vi.mock("@lightbulb/shared", () => ({ useStatusLoaded: () => true }));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: () => ({ data: mockData(), error: undefined, isLoading: false, mutate: mockMutate }),
  useRuntimeRpcMutation: () => mockMutation,
}));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

function installedSkill(over: Record<string, unknown> = {}) {
  return {
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
    ...over,
  };
}

describe("SkillsTab", () => {
  beforeEach(() => {
    mockData.mockReset();
    mockMutate.mockReset();
    mockMutation.mockReset().mockResolvedValue({});
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

  it("explains an always-on (managed) skill instead of hiding the control silently", () => {
    mockData.mockReturnValue({ skills: [installedSkill({ always: true })] });
    render(<SkillsTab />);
    expect(screen.getByText(/always on/i)).toBeInTheDocument();
    // No toggle for a managed skill.
    expect(screen.queryByRole("button", { name: /^disable$/i })).toBeNull();
  });

  it("surfaces the reason when a skill toggle fails instead of swallowing it", async () => {
    // The catch path still console.error's the raw failure — silence it so the
    // test output stays pristine while we assert the user-facing message.
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockData.mockReturnValue({ skills: [installedSkill()], disabledNames: [] });
    // Runtime RPC rejections arrive as an Error whose message is a JSON error object.
    mockMutation.mockRejectedValueOnce(
      new Error(JSON.stringify({ message: "missing scope: operator.admin" })),
    );
    render(<SkillsTab />);
    fireEvent.click(screen.getByRole("button", { name: /^disable$/i }));
    expect(await screen.findByText(/couldn't disable this skill/i)).toBeInTheDocument();
    expect(screen.getByText(/missing scope: operator\.admin/i)).toBeInTheDocument();
    errSpy.mockRestore();
  });
});
