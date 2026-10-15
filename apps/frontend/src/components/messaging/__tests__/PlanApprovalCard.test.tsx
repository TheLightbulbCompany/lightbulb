import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Approval } from "@/components/teams/shared/types";

const teams = vi.hoisted(() => ({ approval: null as Approval | null, post: vi.fn(), mutate: vi.fn() }));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: () => ({ data: teams.approval }), post: teams.post, mutate: teams.mutate }),
}));
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: [] }) }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => true }));
vi.mock("@/components/teams/inbox/ApprovalRow", () => ({ ApprovalRow: () => <div>generic approval row</div> }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

import { RoomAppLinks } from "../RoomAppLinks";

const plan = (status: Approval["status"], title = "Plan: Your CRM set up"): Approval => ({
  id: "ap-1", type: "request_board_approval", status,
  payload: { title, summary: "Import contacts, then three workflows. You approve the import. Done by Friday." },
});
const message = "The plan is waiting for you: https://thelightbulb.company/ISOA/approvals/ap-1";

beforeEach(() => {
  vi.clearAllMocks();
  teams.post.mockResolvedValue({});
});

describe("the plan card in the DM", () => {
  it("renders the agent's linked plan approval inline with Approve plan and Change something", async () => {
    teams.approval = plan("pending");
    render(<RoomAppLinks text={message} />);
    expect(screen.getByRole("region", { name: "Plan for approval" })).toBeInTheDocument();
    expect(screen.getByText("Plan: Your CRM set up")).toBeInTheDocument();
    expect(screen.getByText(/You approve the import/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Approve plan" }));
    expect(teams.post).toHaveBeenCalledWith("/approvals/ap-1/approve", {});
    await userEvent.click(screen.getByRole("button", { name: "Change something" }));
    await waitFor(() => expect(teams.post).toHaveBeenCalledWith("/approvals/ap-1/request-revision", {}));
  });

  it("after changes were asked, keeps Approve plan and says to write the change", () => {
    teams.approval = plan("revision_requested");
    render(<RoomAppLinks text={message} />);
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change something" })).not.toBeInTheDocument();
    expect(screen.getByText(/Say what to change below/)).toBeInTheDocument();
  });

  it("a decided plan shows its outcome and no buttons", () => {
    teams.approval = plan("approved");
    render(<RoomAppLinks text={message} />);
    expect(screen.getByText("Plan approved")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("any other approval keeps the generic row", () => {
    teams.approval = plan("pending", "Approve the budget");
    render(<RoomAppLinks text={message} />);
    expect(screen.getByText("generic approval row")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Plan for approval" })).not.toBeInTheDocument();
  });
});
