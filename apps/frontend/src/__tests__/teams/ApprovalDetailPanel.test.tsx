import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const read = vi.fn();
const approve = vi.fn().mockResolvedValue(undefined);
const reject = vi.fn().mockResolvedValue(undefined);
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

import { ApprovalDetailPanel } from "@/components/teams/panels/ApprovalDetailPanel";

function mockReads(approval: Record<string, unknown>) {
  read.mockImplementation((path: string) => {
    if (path === "/approvals/a1")
      return { data: approval, isLoading: false, error: null, mutate: vi.fn() };
    if (path === "/agents")
      return { data: { agents: [{ id: "ag1", name: "Atlas" }] }, isLoading: false, error: null, mutate: vi.fn() };
    return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
  });
}

beforeEach(() => { read.mockReset(); approve.mockClear(); reject.mockClear(); });

describe("ApprovalDetailPanel", () => {
  it("renders type, status, requester, and toggles the payload", () => {
    mockReads({ id: "a1", type: "exec_command", status: "pending",
      requestedByAgentId: "ag1", payload: { cmd: "ls -la" }, createdAt: "2026-05-01T00:00:00Z" });
    render(<ApprovalDetailPanel approvalId="a1" />);
    expect(screen.getByText(/exec command/i)).toBeInTheDocument();
    expect(screen.getByText(/pending/i)).toBeInTheDocument();
    expect(screen.getByText(/Atlas/)).toBeInTheDocument();
    expect(screen.queryByText(/ls -la/)).toBeNull(); // payload hidden until toggled
    fireEvent.click(screen.getByRole("button", { name: /payload/i }));
    expect(screen.getByText(/ls -la/)).toBeInTheDocument();
  });

  it("approves a pending approval", async () => {
    mockReads({ id: "a1", type: "exec_command", status: "pending", payload: {} });
    render(<ApprovalDetailPanel approvalId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a1"));
  });

  it("hides actions for a resolved approval", () => {
    mockReads({ id: "a1", type: "exec_command", status: "approved", payload: {} });
    render(<ApprovalDetailPanel approvalId="a1" />);
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
    expect(screen.getByText(/approved/i)).toBeInTheDocument();
  });
});
