// apps/frontend/src/__tests__/teams/AgentOrgControls.test.tsx
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

import { AgentOrgControls } from "@/components/teams/agents/AgentOrgControls";

const FRIDAY: CompanyAgent = { id: "f", name: "Friday", role: "ceo", status: "idle", seeded: true };
const BOB: CompanyAgent = { id: "b", name: "Bob", role: "engineer", status: "idle" };
const PAUSED_BOB: CompanyAgent = { ...BOB, status: "paused" };
const AGENTS = [FRIDAY, BOB];

beforeEach(() => {
  setReportsTo.mockReset();
  pause.mockReset();
  resume.mockReset();
  remove.mockReset();
});

describe("AgentOrgControls", () => {
  it("changes reports-to via setReportsTo(agentId, managerId)", async () => {
    setReportsTo.mockResolvedValue(undefined);
    render(<AgentOrgControls agent={BOB} agents={AGENTS} />);
    // Bob picks Friday as manager.
    fireEvent.change(screen.getByLabelText(/reports to/i), { target: { value: "f" } });
    await waitFor(() => expect(setReportsTo).toHaveBeenCalledWith("b", "f"));
  });

  it("shows Pause and calls pause for a running agent", async () => {
    pause.mockResolvedValue(undefined);
    render(<AgentOrgControls agent={BOB} agents={AGENTS} />);
    const pauseBtn = screen.getByRole("button", { name: /pause/i });
    expect(pauseBtn).toBeInTheDocument();
    fireEvent.click(pauseBtn);
    await waitFor(() => expect(pause).toHaveBeenCalledWith("b"));
  });

  it("shows Resume and calls resume for a paused agent", async () => {
    resume.mockResolvedValue(undefined);
    render(<AgentOrgControls agent={PAUSED_BOB} agents={AGENTS} />);
    const resumeBtn = screen.getByRole("button", { name: /resume/i });
    expect(resumeBtn).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^pause$/i })).toBeNull();
    fireEvent.click(resumeBtn);
    await waitFor(() => expect(resume).toHaveBeenCalledWith("b"));
  });

  it("hides delete for a seeded agent", () => {
    render(<AgentOrgControls agent={FRIDAY} agents={AGENTS} />);
    expect(screen.queryByRole("button", { name: /delete/i })).toBeNull();
  });

  it("shows two-step delete for a regular agent and calls remove", async () => {
    remove.mockResolvedValue(undefined);
    render(<AgentOrgControls agent={BOB} agents={AGENTS} />);
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("b"));
  });
});
