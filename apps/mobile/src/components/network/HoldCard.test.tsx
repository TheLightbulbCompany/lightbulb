import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { NetworkHold } from "@lightbulb/shared";

import { notificationHrefs } from "../push/NotificationTapRouter";
import { ApprovalsToggle, HoldCard } from "./HoldCard";

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

const api = { get: jest.fn(), post: jest.fn(), del: jest.fn() };
const message: NetworkHold = {
  hold_id: "h1", holding_member: "m1", holding_computer: "Studio", target_session: "studio/claude",
  from: "aden/claude", label: "Aden", kind: "message", created_at: "2026-10-23T10:00:00Z", agent: "Claude",
};
const permission: NetworkHold = { ...message, hold_id: "h2", kind: "permission", title: "Bash" };
const answers = () => screen.queryAllByRole("button").map((b) => b.props.accessibilityLabel);

beforeEach(() => { api.get.mockReset(); api.post.mockReset(); api.del.mockReset(); });

describe("HoldCard", () => {
  it("cardShowsNoMessageText", async () => {
    await render(<HoldCard hold={{ ...message, preview: "the secret plan" } as NetworkHold} api={api} />);
    expect(screen.getByText("Aden wants to message your Claude session on Studio.")).toBeTruthy();
    expect(screen.queryByText(/secret plan/)).toBeNull();
  });

  it("threeAnswersForAMessageHold", async () => {
    await render(<HoldCard hold={message} api={api} />);
    expect(answers()).toEqual(["Allow once", "Allow Aden always", "Decline"]);
  });

  it("alwaysAllowOnlyForOwnWorkspaceAgents", async () => {
    const { unmount } = await render(<HoldCard hold={{ ...message, own_cloud: true }} api={api} />);
    expect(answers()).toEqual(["Allow once", "Allow Aden always", "Always allow", "Decline"]);
    await unmount();
    await render(<HoldCard hold={{ ...message, own_cloud: false }} api={api} />);
    expect(answers()).not.toContain("Always allow");
  });

  it("permissionHoldOffersOnceAndDecline", async () => {
    await render(<HoldCard hold={permission} api={api} />);
    expect(screen.getByText("Claude on Studio asks permission: Bash.")).toBeTruthy();
    expect(answers()).toEqual(["Allow once", "Decline"]);
  });

  it("posts the answer and reads the result", async () => {
    api.post.mockResolvedValueOnce({ result: "not_open" });
    await render(<HoldCard hold={message} api={api} />);
    await fireEvent.press(screen.getByLabelText("Allow once"));
    expect(api.post).toHaveBeenCalledWith("/network/holds/h1/decision", { holding_member: "m1", verdict: "accept", scope: "once" });
    expect(await screen.findByText("Answered on your Mac.")).toBeTruthy();
    expect(answers()).toEqual([]);
  });

  it("a hold push opens Activity on its card", async () => {
    expect(notificationHrefs({ kind: "network_hold", holdId: "h1" } as never)).toEqual([
      { pathname: "/(app)/(tabs)/activity", params: { focus: "network_hold:h1" } },
    ]);
  });
});

describe("ApprovalsToggle", () => {
  it("approvalsSwitchWritesTheLink", async () => {
    api.get.mockResolvedValue({ approvals_linked: false });
    api.post.mockResolvedValue({});
    api.del.mockResolvedValue({});
    await render(<ApprovalsToggle api={api} />);
    const toggle = await screen.findByRole("switch", { name: "Answer Allow requests here" });
    expect(api.get).toHaveBeenCalledWith("/network/workspace");
    await fireEvent.press(toggle);
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/network/approvals", {}));
    await waitFor(() => expect(screen.getByRole("switch").props.accessibilityState).toEqual({ checked: true }));
    await fireEvent.press(screen.getByRole("switch"));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/network/approvals"));
  });
});
