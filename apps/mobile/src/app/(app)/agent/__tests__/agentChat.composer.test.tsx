import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

// expo-router isn't in the jest transform allowlist; stub what AgentChat uses.
jest.mock("expo-router", () => ({
  router: {
    push: jest.fn(),
    back: jest.fn(),
    canGoBack: () => false,
    replace: jest.fn(),
    setParams: jest.fn(),
  },
  Stack: { Screen: () => null },
  useFocusEffect: jest.fn(),
  useLocalSearchParams: () => ({ agentId: "agent_a", name: "Friday" }),
}));

// user: null keeps history hydration + first-run kickoff inert (both wait on
// the Clerk user), so the screen renders without async fetches.
jest.mock("@clerk/clerk-expo", () => ({ useUser: () => ({ user: null }) }));

jest.mock("expo-crypto", () => ({
  randomUUID: () => "00000000-0000-4000-8000-000000000000",
}));

jest.mock("../../../../providers/gateway", () => ({
  useGateway: () => ({
    isConnected: true,
    error: null,
    onChatMessage: () => () => {},
    sendReq: jest.fn(async () => ({})),
    sendChat: jest.fn(),
  }),
}));

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  useGatewayRpc: () => ({ data: null, error: null, isLoading: false }),
}));

import AgentChat from "../[agentId]";

describe("AgentChat composer", () => {
  it("lets the input auto-grow natively (no controlled height, capped)", async () => {
    await render(<AgentChat />);
    const input = screen.getByPlaceholderText("Ask anything");
    const style = StyleSheet.flatten(input.props.style);
    // A controlled height pins the box at its measured size and doesn't track
    // typing reliably on Fabric iOS — native multiline auto-grow (no height,
    // maxHeight cap) is the contract.
    expect(style.height).toBeUndefined();
    expect(style.maxHeight).toBe(200);
  });

  it("lets a downward swipe on the timeline dismiss the keyboard", async () => {
    await render(<AgentChat />);
    const list = screen.getByTestId("chat-timeline");
    expect(list.props.keyboardDismissMode).toBe("interactive");
    // Taps on timeline controls (retry, tool pills) must land on the control
    // while the keyboard is up, not just dismiss it.
    expect(list.props.keyboardShouldPersistTaps).toBe("handled");
  });
});
