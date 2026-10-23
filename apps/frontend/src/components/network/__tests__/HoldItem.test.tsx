import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@lightbulb/shared";
import type { NetworkHold } from "@lightbulb/shared";
import { ApprovalsSwitch, HoldItem } from "../HoldItem";

const api = { get: vi.fn(), post: vi.fn(), del: vi.fn() };
vi.mock("@/lib/api", () => ({ useApi: () => api }));

const message: NetworkHold = {
  hold_id: "h1", holding_member: "m1", holding_computer: "Studio", target_session: "studio/claude",
  from: "aden/claude", label: "Aden", kind: "message", created_at: "2026-10-23T10:00:00Z", agent: "Claude",
};
const permission: NetworkHold = { ...message, hold_id: "h2", kind: "permission", title: "Bash" };
const buttons = () => screen.getAllByRole("button").map((b) => b.textContent);

beforeEach(() => { api.get.mockReset(); api.post.mockReset(); api.del.mockReset(); });

describe("HoldItem", () => {
  it("cardShowsNoMessageText", () => {
    render(<HoldItem hold={{ ...message, preview: "the secret plan" } as NetworkHold} />);
    expect(screen.getByText("Aden wants to message your Claude session on Studio.")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("the secret plan");
  });

  it("threeAnswersForAMessageHold", () => {
    render(<HoldItem hold={message} />);
    expect(buttons()).toEqual(["Allow once", "Allow Aden always", "Decline"]);
  });

  it("alwaysAllowOnlyForOwnWorkspaceAgents", () => {
    const { unmount } = render(<HoldItem hold={{ ...message, own_cloud: true }} />);
    expect(buttons()).toEqual(["Allow once", "Allow Aden always", "Always allow", "Decline"]);
    unmount();
    render(<HoldItem hold={{ ...message, own_cloud: false }} />);
    expect(screen.queryByRole("button", { name: "Always allow" })).toBeNull();
  });

  it("permissionHoldOffersOnceAndDecline", () => {
    render(<HoldItem hold={permission} />);
    expect(screen.getByText("Claude on Studio asks permission: Bash.")).toBeInTheDocument();
    expect(buttons()).toEqual(["Allow once", "Decline"]);
  });

  it("posts the answer and reads the result", async () => {
    api.post.mockResolvedValueOnce({ result: "applied" });
    const { unmount } = render(<HoldItem hold={message} />);
    await userEvent.click(screen.getByRole("button", { name: "Allow Aden always" }));
    expect(api.post).toHaveBeenCalledWith("/network/holds/h1/decision", { holding_member: "m1", verdict: "accept", scope: "person" });
    expect(await screen.findByText("Allowed.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    unmount();

    api.post.mockResolvedValueOnce({ result: "applied" });
    const second = render(<HoldItem hold={message} />);
    await userEvent.click(screen.getByRole("button", { name: "Decline" }));
    expect(api.post).toHaveBeenLastCalledWith("/network/holds/h1/decision", { holding_member: "m1", verdict: "refuse", scope: "once" });
    expect(await screen.findByText("Declined.")).toBeInTheDocument();
    second.unmount();

    api.post.mockResolvedValueOnce({ result: "not_open" });
    render(<HoldItem hold={message} />);
    await userEvent.click(screen.getByRole("button", { name: "Allow once" }));
    expect(await screen.findByText("Answered on your Mac.")).toBeInTheDocument();
  });
});

describe("hold edge cases", () => {
  it("a hold that is gone reads Answered on your Mac", async () => {
    api.post.mockRejectedValueOnce(new ApiError(404, { detail: "not_found" }));
    render(<HoldItem hold={message} />);
    await userEvent.click(screen.getByRole("button", { name: "Allow once" }));
    expect(await screen.findByText("Answered on your Mac.")).toBeInTheDocument();
  });

  it("a permission hold with no title still reads as a sentence", () => {
    render(<HoldItem hold={{ ...permission, title: undefined }} />);
    expect(screen.getByText("Claude on Studio asks permission.")).toBeInTheDocument();
  });
});

describe("ApprovalsSwitch", () => {
  it("approvalsSwitchWritesTheLink", async () => {
    api.get.mockResolvedValue({ approvals_linked: false });
    api.post.mockResolvedValue({});
    api.del.mockResolvedValue({});
    render(<ApprovalsSwitch />);
    const toggle = await screen.findByRole("checkbox", { name: "Answer Allow requests here" });
    expect(api.get).toHaveBeenCalledWith("/network/workspace");
    expect(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    expect(api.post).toHaveBeenCalledWith("/network/approvals", {});
    await waitFor(() => expect(toggle).toBeChecked());
    await userEvent.click(toggle);
    expect(api.del).toHaveBeenCalledWith("/network/approvals");
    await waitFor(() => expect(toggle).not.toBeChecked());
  });

  it("is hidden without a connected workspace", async () => {
    api.get.mockRejectedValue(new Error("404"));
    const { container } = render(<ApprovalsSwitch />);
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
