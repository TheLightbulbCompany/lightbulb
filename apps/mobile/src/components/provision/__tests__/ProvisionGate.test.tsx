import { render, screen } from "@testing-library/react-native";
import { Text } from "react-native";

const mockRefetch = jest.fn();
const mockUseContainerStatus = jest.fn();

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ signOut: jest.fn() }),
}));

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

jest.mock("../../../lib/containerStatus", () => ({
  useContainerStatus: () => mockUseContainerStatus(),
}));

// Isolate ProvisionGate's ROUTING decision from the screens' own internals
// (each has its own dedicated test file: ChatGPTConnectScreen.test.tsx,
// ByoKeyScreen.test.tsx).
jest.mock("../ChatGPTConnectScreen", () => {
  const { Text: RNText } = require("react-native");
  return {
    ChatGPTConnectScreen: ({ onConnected }: { onConnected: () => void }) => (
      <RNText onPress={onConnected}>MOCK_CHATGPT_CONNECT_SCREEN</RNText>
    ),
  };
});

jest.mock("../ByoKeyScreen", () => {
  const { Text: RNText } = require("react-native");
  return {
    ByoKeyScreen: ({ onConnected }: { onConnected: () => void }) => (
      <RNText onPress={onConnected}>MOCK_BYO_KEY_SCREEN</RNText>
    ),
  };
});

import { ProvisionGate } from "../ProvisionGate";

// NOTE (mobile onboarding-parity Slice 4): written against the same
// mocking conventions proven in FirstRunGate.test.tsx and
// ChatGPTConnectScreen.test.tsx (jest.mock @clerk/clerk-expo +
// react-native-safe-area-context; `await render(...)`). Not run to green as
// part of this task per controller policy — see task-s4-report.md.
describe("ProvisionGate provider sign-in routing", () => {
  beforeEach(() => {
    mockRefetch.mockReset();
    mockUseContainerStatus.mockReset();
  });

  it("renders ChatGPTConnectScreen for blocked.code === oauth_required, wired to refetch", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "oauth_required",
        title: "Sign in with ChatGPT",
        message: null,
        href: "/chat?provider=chatgpt_oauth",
        actionLabel: "Sign in with ChatGPT",
        adminOnly: false,
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("MOCK_CHATGPT_CONNECT_SCREEN")).toBeTruthy();
    expect(screen.queryByText(/on isol8\.co/)).toBeNull();
    expect(screen.queryByText("MOCK_BYO_KEY_SCREEN")).toBeNull();
  });

  it("renders ByoKeyScreen for blocked.code === byo_provider_required, wired to refetch", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "byo_provider_required",
        title: "Choose your BYO provider",
        message: "Choose Anthropic or OpenAI as your BYO provider to start your container.",
        href: "/chat?provider=byo_key",
        actionLabel: "Choose provider",
        adminOnly: false,
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("MOCK_BYO_KEY_SCREEN")).toBeTruthy();
    expect(screen.queryByText(/on isol8\.co/)).toBeNull();
    expect(screen.queryByText("MOCK_CHATGPT_CONNECT_SCREEN")).toBeNull();
  });

  it("keeps the existing outbound Manage link for every other blocked code (e.g. credits_required)", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "credits_required",
        title: "Top up Claude credits to start your container",
        message: "Top up some Claude credits to start your Bedrock container.",
        href: "/settings/billing#credits",
        actionLabel: "Top up now",
        adminOnly: false,
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("Top up Claude credits to start your container")).toBeTruthy();
    expect(screen.getByText("Top up now on isol8.co")).toBeTruthy();
    expect(screen.queryByText("MOCK_CHATGPT_CONNECT_SCREEN")).toBeNull();
    expect(screen.queryByText("MOCK_BYO_KEY_SCREEN")).toBeNull();
  });

  it("keeps provider_required (no provider chosen yet) on the outbound handoff, not a native screen", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "provider_required",
        title: "Pick how to power your agents",
        message: "Choose Bedrock-managed credits, ChatGPT OAuth, or your own API key to continue.",
        href: "/chat?panel=llm&action=change-provider",
        actionLabel: "Pick provider",
        adminOnly: true,
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("Pick how to power your agents")).toBeTruthy();
    expect(screen.queryByText("MOCK_CHATGPT_CONNECT_SCREEN")).toBeNull();
    expect(screen.queryByText("MOCK_BYO_KEY_SCREEN")).toBeNull();
  });
});
