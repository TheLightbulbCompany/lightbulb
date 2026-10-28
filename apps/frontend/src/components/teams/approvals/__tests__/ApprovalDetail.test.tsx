import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ApprovalComment, ApprovalDetail as ApprovalRecord } from "@/components/teams/shared/types";

const { postSpy, approve, reject, remove, data } = vi.hoisted(() => ({
  postSpy: vi.fn(),
  approve: vi.fn(),
  reject: vi.fn(),
  remove: vi.fn(),
  data: {} as Record<string, unknown>,
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => ({ data: data[path], isLoading: false, error: null, mutate: vi.fn() }),
    post: postSpy,
  }),
}));
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));
vi.mock("@/components/teams/agents/hooks/useAgentMutations", () => ({
  useAgentMutations: () => ({ remove }),
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: vi.fn(), get: vi.fn() }) }));

import { ApprovalDetail } from "@/components/teams/approvals/ApprovalDetail";

const ID = "apv_1";
const COMMENT: ApprovalComment = {
  id: "c1",
  approvalId: ID,
  authorAgentId: "agent_1",
  authorUserId: null,
  body: "Please **confirm** the budget",
  createdAt: "2026-09-24T00:00:00.000Z",
};

function seed(approval: Partial<ApprovalRecord>) {
  data[`/approvals/${ID}`] = {
    id: ID,
    type: "hire_agent",
    status: "pending",
    requestedByAgentId: "agent_1",
    payload: { name: "Designer", role: "designer", agentId: "agent_new" },
    ...approval,
  };
  data[`/approvals/${ID}/comments`] = [COMMENT];
  data[`/approvals/${ID}/issues`] = [{ id: "iss_1", identifier: "ISO-7", title: "Hire a designer", status: "todo" }];
  data["/agents"] = { agents: [{ id: "agent_1", name: "CEO" }] };
}

beforeEach(() => {
  for (const k of Object.keys(data)) delete data[k];
  postSpy.mockReset().mockResolvedValue({});
  approve.mockReset().mockResolvedValue(undefined);
  reject.mockReset().mockResolvedValue(undefined);
  remove.mockReset().mockResolvedValue(undefined);
});

describe("ApprovalDetail (ported)", () => {
  it("renders the typed payload, linked tasks and the comment thread", () => {
    seed({});
    render(<ApprovalDetail approvalId={ID} />);
    expect(screen.getByRole("heading", { name: "Hire Agent: Designer" })).toBeInTheDocument();
    expect(screen.getByText("designer")).toBeInTheDocument(); // HireAgentPayload Role row
    expect(screen.getByText("Hire a designer").closest("a")).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=ISO-7",
    );
    expect(screen.getByText("Comments (1)")).toBeInTheDocument();
    expect(screen.getByText("confirm").tagName).toBe("STRONG");
    expect(screen.getAllByText("CEO").length).toBeGreaterThan(0);
  });

  it("posts a comment through the BFF", async () => {
    const user = userEvent.setup();
    seed({});
    render(<ApprovalDetail approvalId={ID} />);
    await user.type(screen.getByPlaceholderText("Add a comment..."), "  looks good  ");
    await user.click(screen.getByRole("button", { name: "Post comment" }));
    expect(postSpy).toHaveBeenCalledWith(`/approvals/${ID}/comments`, { body: "looks good" });
  });

  it("pending: Approve / Reject / Request revision, and no resubmit", async () => {
    const user = userEvent.setup();
    seed({});
    render(<ApprovalDetail approvalId={ID} />);
    expect(screen.queryByRole("button", { name: "Mark resubmitted" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Request revision" }));
    expect(postSpy).toHaveBeenCalledWith(`/approvals/${ID}/request-revision`, {});

    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(approve).toHaveBeenCalledWith(ID);

    await user.click(screen.getByRole("button", { name: "Reject" }));
    await user.type(screen.getByLabelText("Reason for rejection"), "not now");
    await user.click(screen.getByRole("button", { name: "Confirm reject" }));
    expect(reject).toHaveBeenCalledWith(ID, "not now");
  });

  it("revision_requested: Mark resubmitted, and no Request revision", async () => {
    const user = userEvent.setup();
    seed({ status: "revision_requested" });
    render(<ApprovalDetail approvalId={ID} />);
    expect(screen.queryByRole("button", { name: "Request revision" })).toBeNull();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mark resubmitted" }));
    expect(postSpy).toHaveBeenCalledWith(`/approvals/${ID}/resubmit`, {});
  });

  it("rejected hire: only Delete disapproved agent", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    seed({ status: "rejected" });
    render(<ApprovalDetail approvalId={ID} />);
    for (const name of ["Approve", "Reject", "Request revision", "Mark resubmitted"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    await user.click(screen.getByRole("button", { name: "Delete disapproved agent" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("agent_new"));
  });

  it("keeps Lightbulb's own cards on their inbox actions (Open Stripe settings + Approve)", () => {
    seed({
      type: "request_board_approval",
      payload: { isol8_action: "company_stripe", title: "Set up Northwind on Stripe" },
    });
    render(<ApprovalDetail approvalId={ID} />);
    expect(screen.queryByRole("button", { name: "Set up Stripe" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Connect existing" })).toBeNull();
    expect(screen.getByRole("button", { name: "Open Stripe settings" })).toBeInTheDocument();
    // Exactly one Approve: the card's own, not the hidden generic row's.
    expect(screen.getAllByRole("button", { name: "Approve" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Request revision" })).toBeNull();
    expect(screen.queryByRole("link", { name: /Connect Stripe/ })).toBeNull();
  });
});
