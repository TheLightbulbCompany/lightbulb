// apps/frontend/src/__tests__/teams/AgentOrgControls.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { CompanyAgent } from "@/components/teams/shared/types";

const setReportsTo = vi.fn();
const pause = vi.fn();
const resume = vi.fn();
const resetSession = vi.fn();
const clearError = vi.fn();
const remove = vi.fn();
const rename = vi.fn();
vi.mock("@/components/teams/agents/hooks/useAgentMutations", () => ({
  useAgentMutations: () => ({ setReportsTo, rename, pause, resume, resetSession, clearError, remove }),
}));

import { AgentOrgControls } from "@/components/teams/agents/AgentOrgControls";

const FRIDAY: CompanyAgent = { id: "f", name: "Friday", role: "ceo", status: "idle", seeded: true };
const BOB: CompanyAgent = { id: "b", name: "Bob", role: "engineer", status: "idle" };
const PAUSED_BOB: CompanyAgent = { ...BOB, status: "paused" };
const ERRORED_BOB: CompanyAgent = { ...BOB, status: "error" };
const AGENTS = [FRIDAY, BOB];

beforeEach(() => {
  setReportsTo.mockReset();
  pause.mockReset();
  resume.mockReset();
  resetSession.mockReset();
  clearError.mockReset();
  remove.mockReset();
  rename.mockReset();
});

describe("AgentOrgControls", () => {
  it("shows backend-authored marketplace provenance", () => {
    render(
      <AgentOrgControls
        agent={{
          ...BOB,
          origin: "Marketplace",
          marketplace: { listingId: "listing-1", slug: "forge", version: 3, seller: "seller-1" },
        }}
        agents={AGENTS}
      />,
    );
    expect(screen.getByText("Marketplace")).toBeInTheDocument();
    expect(screen.getByText("forge v3 · seller-1")).toBeInTheDocument();
  });

  it("changes reports-to via setReportsTo(agentId, managerId)", async () => {
    setReportsTo.mockResolvedValue(undefined);
    render(<AgentOrgControls agent={BOB} agents={AGENTS} />);
    // Bob picks Friday as manager.
    fireEvent.change(screen.getByLabelText(/reports to/i), { target: { value: "f" } });
    await waitFor(() => expect(setReportsTo).toHaveBeenCalledWith("b", "f"));
  });

  it("renames an agent and refreshes display-name consumers", async () => {
    rename.mockResolvedValue(undefined);
    const onUpdated = vi.fn();
    render(<AgentOrgControls agent={BOB} agents={AGENTS} onUpdated={onUpdated} />);

    fireEvent.click(screen.getByRole("button", { name: "Rename agent" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Agent name" }), {
      target: { value: "  Research Lead  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(rename).toHaveBeenCalledWith("b", "  Research Lead  "));
    expect(onUpdated).toHaveBeenCalledOnce();
    expect(screen.queryByRole("textbox", { name: "Agent name" })).not.toBeInTheDocument();
  });

  it("rejects a duplicate display name without calling the API", () => {
    render(<AgentOrgControls agent={BOB} agents={AGENTS} />);

    fireEvent.click(screen.getByRole("button", { name: "Rename agent" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Agent name" }), {
      target: { value: " friday " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.getByRole("alert")).toHaveTextContent("already uses that name");
    expect(rename).not.toHaveBeenCalled();
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

  it("offers delete for a starter agent and lets the backend enforce graph invariants", () => {
    render(<AgentOrgControls agent={FRIDAY} agents={AGENTS} />);
    expect(screen.getByRole("button", { name: /delete/i })).toBeInTheDocument();
  });

  it("shows two-step delete for a regular agent and calls remove", async () => {
    remove.mockResolvedValue(undefined);
    render(<AgentOrgControls agent={BOB} agents={AGENTS} />);
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("b"));
  });

  it("hides Clear error unless the agent is in error status", () => {
    render(<AgentOrgControls agent={BOB} agents={AGENTS} />);
    expect(screen.queryByRole("button", { name: /clear error/i })).toBeNull();
  });

  it("shows Clear error for an errored agent and calls clearError", async () => {
    clearError.mockResolvedValue(undefined);
    render(<AgentOrgControls agent={ERRORED_BOB} agents={AGENTS} />);
    fireEvent.click(screen.getByRole("button", { name: /clear error/i }));
    await waitFor(() => expect(clearError).toHaveBeenCalledWith("b"));
  });

  it("names the stored status so a stale one is visible, not just inferred from a dot", () => {
    render(<AgentOrgControls agent={{ ...BOB, status: "running" }} agents={AGENTS} />);
    expect(screen.getByText("running")).toBeInTheDocument();
    // A `live` status the owner disputes gets the "this may be stale" read.
    expect(screen.getByText(/shown as working everywhere it appears/i)).toBeInTheDocument();
  });

  it("does not claim a stale status on an idle agent", () => {
    render(<AgentOrgControls agent={BOB} agents={AGENTS} />);
    expect(screen.getByText("idle")).toBeInTheDocument();
    expect(screen.queryByText(/shown as working everywhere it appears/i)).toBeNull();
  });

  it("Reset session is two-step and calls resetSession on confirm", async () => {
    resetSession.mockResolvedValue(undefined);
    render(<AgentOrgControls agent={BOB} agents={AGENTS} />);
    fireEvent.click(screen.getByRole("button", { name: /reset session/i }));
    expect(resetSession).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() => expect(resetSession).toHaveBeenCalledWith("b"));
  });

  it("offers recovery for a seeded agent (Friday/Atlas can wedge too)", () => {
    render(<AgentOrgControls agent={{ ...FRIDAY, status: "error" }} agents={AGENTS} />);
    // Recovery and deletion are both available; the backend owns graph safety.
    expect(screen.getByRole("button", { name: /clear error/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reset session/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /delete/i })).toBeInTheDocument();
  });
});
