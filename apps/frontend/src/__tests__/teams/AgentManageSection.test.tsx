// apps/frontend/src/__tests__/teams/AgentManageSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { CompanyAgent } from "@/components/teams/shared/types";

const setReportsTo = vi.fn();
const pause = vi.fn();
const resume = vi.fn();
const remove = vi.fn();
vi.mock("@/components/teams/agents/hooks/useAgentMutations", () => ({
  useAgentMutations: () => ({ setReportsTo, pause, resume, remove }),
}));

import { AgentManageSection, isSeededAgent } from "@/components/teams/agents/AgentManageSection";

const FRIDAY: CompanyAgent = { id: "f", name: "Friday", role: "ceo", status: "idle" };
const BOB: CompanyAgent = { id: "b", name: "Bob", role: "engineer", status: "idle" };
const AGENTS = [FRIDAY, BOB];

beforeEach(() => { setReportsTo.mockReset(); pause.mockReset(); resume.mockReset(); remove.mockReset(); });

describe("isSeededAgent", () => {
  it("flags ceo and Atlas", () => {
    expect(isSeededAgent(FRIDAY)).toBe(true);
    expect(isSeededAgent({ id: "x", name: "Atlas", role: "general" })).toBe(true);
    expect(isSeededAgent(BOB)).toBe(false);
  });
});

describe("AgentManageSection", () => {
  it("hides delete for seeded agents", () => {
    render(<AgentManageSection agent={FRIDAY} agents={AGENTS} />);
    expect(screen.queryByRole("button", { name: /delete/i })).toBeNull();
  });

  it("shows delete for regular agents and calls remove", async () => {
    remove.mockResolvedValue(undefined);
    render(<AgentManageSection agent={BOB} agents={AGENTS} />);
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("b"));
  });

  it("renders meta read-only (no role <select>)", () => {
    render(<AgentManageSection agent={BOB} agents={AGENTS} />);
    expect(screen.queryByLabelText(/^role$/i)).toBeNull();
    expect(screen.getByText("engineer")).toBeInTheDocument();
  });
});
