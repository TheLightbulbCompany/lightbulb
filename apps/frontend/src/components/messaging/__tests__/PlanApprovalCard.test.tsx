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
import { DmIdeaPlan, DmPlanContext } from "../PlanApprovalCard";
import { SWRConfig } from "swr";
import type { IdeaPlan, IdeasResponse } from "@lightbulb/shared";

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
  });

  it("Change something asks what should change, sends that note, and Cancel goes back", async () => {
    teams.approval = plan("pending");
    render(<RoomAppLinks text={message} />);
    await userEvent.click(screen.getByRole("button", { name: "Change something" }));
    const box = screen.getByRole("textbox", { name: "What should change?" });
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await userEvent.type(box, "   ");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeInTheDocument();
    expect(teams.post).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Change something" }));
    await userEvent.type(screen.getByRole("textbox", { name: "What should change?" }), "Start with the import, not the workflows");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(teams.post).toHaveBeenCalledWith(
      "/approvals/ap-1/request-revision", { decisionNote: "Start with the import, not the workflows" }));
  });

  it("after changes were asked, shows a status line and no buttons (the agent files a new plan)", () => {
    teams.approval = plan("revision_requested");
    render(<RoomAppLinks text={message} />);
    expect(screen.getByText("You asked for changes. The plan is being revised.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("draws nothing for a pasted link to the plan the DM already shows", () => {
    teams.approval = plan("pending");
    render(<DmPlanContext.Provider value="ap-1"><RoomAppLinks text={message} /></DmPlanContext.Provider>);
    expect(screen.queryByRole("region", { name: "Plan for approval" })).not.toBeInTheDocument();
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

describe("the plan card from GET /ideas", () => {
  const running = (plan: IdeaPlan | null, createdAt: string | null = new Date().toISOString()): IdeasResponse => ({
    ideas: [], running: [{ ideaId: "crm", title: "CRM", agentId: "g", agentName: "Growth",
      roomId: "dm-1", projectId: "p", starterId: "me", starterName: "Me", createdAt, plan }] });

  it("shows the plan matched to this DM's start without any link, and reports it", async () => {
    teams.approval = plan("pending");
    const client = { listIdeas: vi.fn(async () => running({ approvalId: "ap-1", status: "pending", title: "Plan: CRM" })),
      startIdea: vi.fn(), dismissIdea: vi.fn() };
    const onPlan = vi.fn();
    render(<SWRConfig value={{ provider: () => new Map() }}>
      <DmIdeaPlan client={client} scope="me:org" roomId="dm-1" userId="me" onPlan={onPlan} />
    </SWRConfig>);
    expect(await screen.findByRole("button", { name: "Approve plan" })).toBeInTheDocument();
    expect(onPlan).toHaveBeenCalledWith("ap-1");
  });

  it("asks again while the start has no plan yet", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const client = { listIdeas: vi.fn(async () => running(null)), startIdea: vi.fn(), dismissIdea: vi.fn() };
      render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <DmIdeaPlan client={client} scope="me:org" roomId="dm-1" userId="me" onPlan={vi.fn()} />
      </SWRConfig>);
      await waitFor(() => expect(client.listIdeas).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(10_500);
      await waitFor(() => expect(client.listIdeas).toHaveBeenCalledTimes(2));
      expect(screen.queryByRole("region", { name: "Plan for approval" })).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops asking five minutes after the Start, however late the DM is opened", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const sixMinutesAgo = new Date(Date.now() - 6 * 60_000).toISOString();
      const client = { listIdeas: vi.fn(async () => running(null, sixMinutesAgo)), startIdea: vi.fn(), dismissIdea: vi.fn() };
      render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <DmIdeaPlan client={client} scope="me:org" roomId="dm-1" userId="me" onPlan={vi.fn()} />
      </SWRConfig>);
      await waitFor(() => expect(client.listIdeas).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(30_000);
      expect(client.listIdeas).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
