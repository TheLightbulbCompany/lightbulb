import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { ComponentType, ReactNode } from "react";
import type { NetworkHold } from "@lightbulb/shared";

import { notificationHrefs } from "../push/NotificationTapRouter";
import { ApprovalsToggle, HoldCard, useNetworkHolds } from "./HoldCard";

// The copy and the answers are tested once, in packages/shared (network/holds.test.ts).
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ getToken: async () => "tok" }) }));
// Like SwiftUI's ToggleView: the switch keeps its own `checked`, flips on a tap, and
// re-syncs only when the `isOn` prop changes, so a failed save must remount it.
jest.mock("../ui/swiftui", () => {
  const react = require("react");
  const { Pressable, Text } = require("react-native");
  function Toggle({ label, isOn, onIsOnChange }: { label: string; isOn: boolean; onIsOnChange: (on: boolean) => void }) {
    const [checked, setChecked] = react.useState(isOn);
    react.useEffect(() => setChecked(isOn), [isOn]);
    return react.createElement(
      Pressable,
      {
        accessibilityRole: "switch",
        accessibilityLabel: label,
        accessibilityState: { checked },
        onPress: () => { setChecked(!checked); onIsOnChange(!checked); },
      },
      react.createElement(Text, null, label),
    );
  }
  return { ...jest.requireActual("../ui/swiftui"), Toggle };
});

const api = { get: jest.fn(), post: jest.fn(), del: jest.fn() };
const message: NetworkHold = {
  hold_id: "h1", holding_member: "m1", holding_computer: "Studio", target_session: "studio/claude",
  from: "aden/claude", label: "Aden", kind: "message", created_at: "2026-10-23T10:00:00Z", agent: "Claude",
};
const answers = () => screen.queryAllByRole("button").map((b) => b.props.accessibilityLabel);
// swr comes with @lightbulb/shared (its useApprovalsLink); a fresh cache per test keeps tests apart.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { SWRConfig } = require("swr") as {
  SWRConfig: ComponentType<{ value: { provider: () => Map<string, unknown> }; children: ReactNode }>;
};
const fresh = ({ children }: { children: ReactNode }) => <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>;
const checked = () => screen.getByRole("switch").props.accessibilityState.checked;

beforeEach(() => { api.get.mockReset(); api.post.mockReset(); api.del.mockReset(); });

describe("HoldCard", () => {
  it("cardShowsNoMessageText", async () => {
    await render(<HoldCard hold={{ ...message, preview: "the secret plan" } as NetworkHold} api={api} />);
    expect(screen.getByText("Aden wants to message your Claude session on Studio.")).toBeTruthy();
    expect(answers()).toEqual(["Allow once", "Allow Aden always", "Decline"]);
    expect(screen.queryByText(/secret plan/)).toBeNull();
  });

  it("posts the answer and shows the result in place of the buttons", async () => {
    api.post.mockResolvedValueOnce({ result: "not_open" });
    await render(<HoldCard hold={message} api={api} />);
    await fireEvent.press(screen.getByLabelText("Allow once"));
    expect(api.post).toHaveBeenCalledWith("/network/holds/h1/decision", { holding_member: "m1", verdict: "accept", scope: "once" });
    expect(await screen.findByText("Answered on your Mac.")).toBeTruthy();
    expect(answers()).toEqual([]);
  });

  it("a hold push opens Activity on its card", () => {
    expect(notificationHrefs({ kind: "network_hold", holdId: "h1" } as never)).toEqual([
      { pathname: "/(app)/(tabs)/activity", params: { focus: "network_hold:h1" } },
    ]);
  });
});

describe("useNetworkHolds", () => {
  it("reads the holds again when a hold push names a new card", async () => {
    api.get.mockResolvedValue([message]);
    function Probe({ reloadKey }: { reloadKey?: string }) {
      useNetworkHolds(api, true, reloadKey);
      return null;
    }
    const view = await render(<Probe />);
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
    await view.rerender(<Probe reloadKey="network_hold:h9" />);
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    expect(api.get).toHaveBeenLastCalledWith("/network/holds");
    await view.unmount();
  });
});

describe("ApprovalsToggle", () => {
  it("approvalsSwitchWritesTheLink", async () => {
    api.get.mockResolvedValue({ approvals_linked: false });
    api.post.mockResolvedValue({});
    api.del.mockResolvedValue({});
    await render(<ApprovalsToggle api={api} />, { wrapper: fresh });
    const toggle = await screen.findByRole("switch", { name: "Answer Allow requests here" });
    expect(api.get).toHaveBeenCalledWith("/network/workspace");
    await fireEvent.press(toggle);
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/network/approvals", {}));
    await waitFor(() => expect(checked()).toBe(true));
    await fireEvent.press(screen.getByRole("switch"));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/network/approvals"));
  });

  it("is hidden without a connected workspace", async () => {
    api.get.mockRejectedValue(new Error("404"));
    await render(<ApprovalsToggle api={api} />, { wrapper: fresh });
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("one write at a time: a tap during a save is ignored", async () => {
    api.get.mockResolvedValue({ approvals_linked: false });
    api.post.mockReturnValue(new Promise(() => {}));
    await render(<ApprovalsToggle api={api} />, { wrapper: fresh });
    await fireEvent.press(await screen.findByRole("switch"));
    await fireEvent.press(screen.getByRole("switch"));
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.del).not.toHaveBeenCalled();
  });

  it("a failed save remounts the toggle at the server value, twice in a row", async () => {
    api.get.mockResolvedValue({ approvals_linked: false });
    api.post.mockRejectedValue(new Error("500"));
    await render(<ApprovalsToggle api={api} />, { wrapper: fresh });
    await fireEvent.press(await screen.findByRole("switch"));
    expect(await screen.findByText("Couldn’t save this setting. Try again.")).toBeTruthy();
    await waitFor(() => expect(checked()).toBe(false));
    await fireEvent.press(screen.getByRole("switch"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(checked()).toBe(false));
    expect(screen.getByText("Couldn’t save this setting. Try again.")).toBeTruthy();
  });
});
