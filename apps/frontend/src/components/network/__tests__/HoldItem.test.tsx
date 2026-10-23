import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { NetworkHold } from "@lightbulb/shared";
import { ApprovalsSwitch, HoldItem } from "../HoldItem";

// The copy and the answers are tested once, in packages/shared (network/holds.test.ts).
const api = { get: vi.fn(), post: vi.fn(), del: vi.fn() };
vi.mock("@/lib/api", () => ({ useApi: () => api }));

const message: NetworkHold = {
  hold_id: "h1", holding_member: "m1", holding_computer: "Studio", target_session: "studio/claude",
  from: "aden/claude", label: "Aden", kind: "message", created_at: "2026-10-23T10:00:00Z", agent: "Claude",
};
const fresh = ({ children }: { children: ReactNode }) => <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>;

beforeEach(() => { api.get.mockReset(); api.post.mockReset(); api.del.mockReset(); });

describe("HoldItem", () => {
  it("cardShowsNoMessageText", () => {
    render(<HoldItem hold={{ ...message, preview: "the secret plan" } as NetworkHold} />);
    expect(screen.getByText("Aden wants to message your Claude session on Studio.")).toBeInTheDocument();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Allow once", "Allow Aden always", "Decline"]);
    expect(document.body).not.toHaveTextContent("the secret plan");
  });

  it("posts the answer and shows the result in place of the buttons", async () => {
    api.post.mockResolvedValueOnce({ result: "applied" });
    render(<HoldItem hold={message} />);
    await userEvent.click(screen.getByRole("button", { name: "Allow Aden always" }));
    expect(api.post).toHaveBeenCalledWith("/network/holds/h1/decision", { holding_member: "m1", verdict: "accept", scope: "person" });
    expect(await screen.findByText("Allowed.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("ApprovalsSwitch", () => {
  it("approvalsSwitchWritesTheLink", async () => {
    api.get.mockResolvedValue({ approvals_linked: false });
    api.post.mockResolvedValue({});
    api.del.mockResolvedValue({});
    render(<ApprovalsSwitch />, { wrapper: fresh });
    const toggle = await screen.findByRole("checkbox", { name: "Answer Allow requests here" });
    expect(api.get).toHaveBeenCalledWith("/network/workspace");
    expect(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    expect(api.post).toHaveBeenCalledWith("/network/approvals", {});
    await waitFor(() => expect(toggle).toBeChecked());
    await waitFor(() => expect(toggle).toBeEnabled());
    await userEvent.click(toggle);
    expect(api.del).toHaveBeenCalledWith("/network/approvals");
    await waitFor(() => expect(toggle).not.toBeChecked());
  });

  it("a failed save leaves the switch at the server value and says so", async () => {
    api.get.mockResolvedValue({ approvals_linked: false });
    api.post.mockRejectedValue(new Error("500"));
    render(<ApprovalsSwitch />, { wrapper: fresh });
    const toggle = await screen.findByRole("checkbox", { name: "Answer Allow requests here" });
    await userEvent.click(toggle);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t save this setting. Try again.");
    expect(toggle).not.toBeChecked();
  });

  it("is hidden without a connected workspace", async () => {
    api.get.mockRejectedValue(new Error("404"));
    const { container } = render(<ApprovalsSwitch />, { wrapper: fresh });
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
