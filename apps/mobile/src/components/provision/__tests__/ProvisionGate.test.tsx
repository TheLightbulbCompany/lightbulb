import { fireEvent, render, screen } from "@testing-library/react-native";
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
// Real (unmocked) module — only `react`-dependent, so it's safe to load for
// real here and lets this file prove ProvisionGate actually wires its
// polled status into the context, not just that its own mocked hook works.
import { useContainerStatusContext } from "../../../lib/containerStatusContext";

// NOTE (mobile onboarding-parity Slice 4): written against the same
// mocking conventions proven in FirstRunGate.test.tsx and
// ChatGPTConnectScreen.test.tsx (jest.mock @clerk/clerk-expo +
// react-native-safe-area-context; `await render(...)`). The routing cases
// below cover all four blocking kinds this gate serves — blocked (generic,
// oauth_required, byo_provider_required, and the adminOnly/ownerRole
// variants), teamPending, needsSignup and error.
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

  // adminOnly + ownerRole gating (Slice 4 fix): oauth_required is admin_only
  // for an org workspace, so a non-admin member polling the SAME gate must
  // fall through to the generic "ask your admin" interstitial instead of a
  // ChatGPT sign-in control they have no power to complete.
  it("does NOT render ChatGPTConnectScreen when oauth_required is adminOnly and the poller is a non-admin member", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "oauth_required",
        title: "Sign in with ChatGPT",
        message: "Complete the ChatGPT sign-in to start your container.",
        href: "/chat?provider=chatgpt_oauth",
        actionLabel: "Sign in with ChatGPT",
        adminOnly: true,
        ownerRole: "member",
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.queryByText("MOCK_CHATGPT_CONNECT_SCREEN")).toBeNull();
    expect(screen.getByText("Sign in with ChatGPT")).toBeTruthy();
    expect(
      screen.getByText("Ask your workspace admin to finish this on isol8.co."),
    ).toBeTruthy();
  });

  // The other two blocking states this gate routes. Both replace the tree
  // rather than banner over it (contract §B3: there is no working chat behind
  // either), and they differ in exactly one way — team-pending clears itself
  // and so offers NO outbound CTA, needs-signup needs the user to go finish
  // onboarding on the web.
  it("renders the pending-workspace interstitial for teamPending, with no outbound CTA", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "teamPending" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("Setting up your team workspace…")).toBeTruthy();
    expect(screen.getByText("Retry")).toBeTruthy();
    expect(screen.queryByText("APP")).toBeNull();
    // Nothing for the member to do on the web — their admin's provision is
    // already running and the poll clears this on its own.
    expect(screen.queryByText(/on isol8\.co/)).toBeNull();
  });

  it("renders the finish-on-web interstitial for needsSignup, with the outbound CTA", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "needsSignup" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("Finish setting up on isol8.co")).toBeTruthy();
    expect(screen.getByText("Manage on isol8.co")).toBeTruthy();
    expect(screen.queryByText("APP")).toBeNull();
  });

  it("renders ChatGPTConnectScreen when oauth_required is adminOnly but the poller IS the admin", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "oauth_required",
        title: "Sign in with ChatGPT",
        message: "Complete the ChatGPT sign-in to start your container.",
        href: "/chat?provider=chatgpt_oauth",
        actionLabel: "Sign in with ChatGPT",
        adminOnly: true,
        ownerRole: "admin",
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("MOCK_CHATGPT_CONNECT_SCREEN")).toBeTruthy();
    expect(screen.queryByText(/ask your workspace admin/i)).toBeNull();
  });
});

/** Renders what useContainerStatusContext() sees, so the assertions below
 * can prove ProvisionGate actually forwards its polled status rather than
 * just rendering children blindly. Narrows via `status.kind` (not the `in`
 * operator) so "key absent" and "key present but undefined" — both real
 * shapes useContainerStatus can produce — render identically, as "undefined". */
function StatusConsumer() {
  const status = useContainerStatusContext();
  if (!status) return <Text>STATUS:none</Text>;
  const onboarded =
    status.kind === "ready" || status.kind === "starting"
      ? String(status.onboarded)
      : "n/a";
  return <Text>{`STATUS:${status.kind}:${onboarded}`}</Text>;
}

// mobile onboarding-parity Slice 2: ProvisionGate now provides the SAME
// status it polls to descendants via ContainerStatusContext, so FirstRunGate
// can read `onboarded` without a second poller. This is the wiring proof —
// FirstRunGate's own tests mock this context hook directly and so would not
// catch a mistake here (e.g. forgetting the Provider, or wrapping with a
// stale value).
describe("ProvisionGate container-status context", () => {
  beforeEach(() => {
    mockRefetch.mockReset();
    mockUseContainerStatus.mockReset();
  });

  it("provides the polled ready status (with onboarded) to descendants", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "ready", onboarded: false },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <StatusConsumer />
      </ProvisionGate>,
    );

    expect(screen.getByText("STATUS:ready:false")).toBeTruthy();
  });

  // The banner text now comes from <ProvisioningStepper/> (mobile
  // onboarding-parity Slice 3), not an inline StartingBanner in the gate —
  // unchanged for the sub-10s window, which is where a freshly-rendered
  // component sits. The absent dot rail is the proof the gate still hands
  // children through non-blocking at t=0; the promotion time-box itself is
  // covered in ProvisioningStepper.test.tsx.
  it("still provides the status via context while starting (banner + context both active)", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "starting" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <StatusConsumer />
      </ProvisionGate>,
    );

    expect(screen.getByText("STATUS:starting:undefined")).toBeTruthy();
    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();
  });

  it("does NOT provide children (or the context) while blocked — the interstitial replaces the tree", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "credits_required",
        title: "Top up Claude credits to start your container",
        message: null,
        href: null,
        actionLabel: null,
        adminOnly: false,
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <StatusConsumer />
      </ProvisionGate>,
    );

    expect(screen.queryByText(/^STATUS:/)).toBeNull();
  });
});

// mobile onboarding-parity Slice 1: the one behavior change in this slice.
// Mobile previously had no surface for a backend-reported container
// failure (the backend reports phase="provisioning" for a failed container
// too, so it read as "starting" forever) — this unifies with web's
// ProvisioningStepper "Setup failed" card, which keys on the same raw
// `container.status === "error"`.
describe("ProvisionGate container-error interstitial", () => {
  beforeEach(() => {
    mockRefetch.mockReset();
    mockUseContainerStatus.mockReset();
  });

  it("renders a blocking 'Setup failed' interstitial with Retry + Sign out for status.kind === 'error'", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "error" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("Setup failed")).toBeTruthy();
    expect(screen.getByText("Retry")).toBeTruthy();
    expect(screen.getByText("Sign out")).toBeTruthy();
    // No outbound "Manage on isol8.co" CTA — there's nothing to manage for
    // a failed provision, unlike the blocked/needsSignup gate states.
    expect(screen.queryByText(/on isol8\.co/)).toBeNull();
  });

  it("does NOT render children while in the error state — the interstitial replaces the tree", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "error" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.queryByText("APP")).toBeNull();
  });

  it("Retry on the error interstitial calls refetch", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "error" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    // fireEvent.press is ASYNC on this @testing-library/react-native version
    // (see ByoKeyScreen.test.tsx's note) — must be awaited or the resulting
    // act() scope races the next statement.
    await fireEvent.press(screen.getByText("Retry"));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });
});
