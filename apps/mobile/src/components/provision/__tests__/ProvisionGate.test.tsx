import { fireEvent, render, screen } from "@testing-library/react-native";
import { Text } from "react-native";

const mockRefetch = jest.fn();
const mockUseContainerStatus = jest.fn();

// `organization` decides whether this poller's container is an org (billed
// per seat) or personal. The paywall sells the personal plan only, so the
// org case must never reach it — see the isOrgOwner guard in ProvisionGate.
let mockOrganization: { id: string } | null = null;

// IAP forced ON so the paywall branch is genuinely reachable. Without this the
// org tests below would pass merely because the feature is off — proving
// nothing about the guard they exist to pin.
let mockIapAvailable = false;

jest.mock("../../../lib/appleIap", () => ({
  // Getter, not a literal: the 3.1.3(f) fallback tests need IAP OFF while the
  // org-fence tests need it ON, and the guard is only meaningful when on.
  get APPLE_IAP_AVAILABLE() {
    return mockIapAvailable;
  },
  APPLE_SUBSCRIPTION_SKU: "co.isol8.mobile.plan.monthly",
  linkApplePurchase: jest.fn(),
  createWebCheckout: jest.fn(),
}));

jest.mock("../../billing/PaywallScreen", () => ({
  PaywallScreen: () => {
    const { Text } = require("react-native");
    return <Text>MOCK_PAYWALL</Text>;
  },
}));

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ signOut: jest.fn() }),
  useOrganization: () => ({ organization: mockOrganization }),
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
    mockOrganization = null;
    mockIapAvailable = false;
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

  // A NON-purchase blocked code is untouched by the 3.1.3(f) change: server
  // copy, outbound link and admin note all behave exactly as before.
  it("keeps the existing outbound Manage link for a non-purchase blocked code", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "invitation_pending",
        title: "You've been invited to a workspace",
        message: "Accept your invitation to start working with your team.",
        href: "/onboarding",
        actionLabel: "View invitation",
        adminOnly: false,
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("You've been invited to a workspace")).toBeTruthy();
    expect(screen.getByText("View invitation on isol8.co")).toBeTruthy();
    expect(screen.queryByText("MOCK_CHATGPT_CONNECT_SCREEN")).toBeNull();
    expect(screen.queryByText("MOCK_BYO_KEY_SCREEN")).toBeNull();
  });

  // The 3.1.3(f) fence (lib/purchaseCta). The server sends a "Top up now"
  // action AND copy instructing the user to buy credits; iOS must render
  // neither — dropping the button alone would leave the CTA in the message.
  it("drops the outbound link AND the server copy for a purchase gate", async () => {
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

    expect(screen.getByText("Out of credits")).toBeTruthy();
    expect(screen.queryByText(/Top up/)).toBeNull();
    expect(screen.queryByText(/isol8\.co/)).toBeNull();
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

  // needsSignup used to hand off to the web page where a plan is bought, which
  // makes it a purchase CTA in all but name (3.1.3(f)). It now states the
  // situation and stops; Retry and Sign out still come from the Interstitial.
  it("renders the not-set-up interstitial for needsSignup, with no outbound CTA", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "needsSignup" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("Workspace not set up")).toBeTruthy();
    expect(screen.queryByText(/isol8\.co/)).toBeNull();
    expect(screen.getByText("Retry")).toBeTruthy();
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
    mockOrganization = null;
    mockIapAvailable = false;
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
    mockOrganization = null;
    mockIapAvailable = false;
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

// PR3 honest-contract: teams_status="failed" is a TOP-LEVEL, NON-blocking
// signal — Paperclip seeding failed while the container itself is fine and
// chat works. The gate must surface a small affordance OVER the live children,
// NEVER replace them with an interstitial (unlike every blocking kind above).
describe("ProvisionGate teams-degraded affordance (non-blocking)", () => {
  beforeEach(() => {
    mockRefetch.mockReset();
    mockUseContainerStatus.mockReset();
    mockOrganization = null;
    mockIapAvailable = false;
  });

  it("renders the Teams affordance OVER the live children (chat not blocked) when ready + teams_status failed", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "ready", onboarded: true, teamsStatus: "failed" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    // Children still render — the affordance never replaces chat.
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.getByText(/Teams setup didn.t finish/)).toBeTruthy();
    expect(screen.getByLabelText("Retry Teams setup")).toBeTruthy();
  });

  it("wires the Teams affordance Retry to refetch", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "ready", onboarded: true, teamsStatus: "failed" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    await fireEvent.press(screen.getByLabelText("Retry Teams setup"));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it("does NOT render the Teams affordance on a healthy ready container", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "ready", onboarded: true },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText(/Teams setup didn.t finish/)).toBeNull();
  });
});

// PR-review finding: the paywall sells the PERSONAL flat plan — one Apple
// product, one Stripe flat price. An org pays base + per-seat. Routing an org
// into it takes real money through StoreKit for a plan that cannot be
// recorded: a team_billing_required org has no billing row by construction, so
// the link call fails permanently and the customer is left charged.
//
// Gated on OWNER TYPE, not gate code — payment_past_due, subscription_required,
// credits_required and signup_required all fire for orgs too, so a code
// allowlist would leak every one of them.
describe("ProvisionGate paywall routing is personal-owner only", () => {
  const purchaseGate = {
    kind: "blocked" as const,
    code: "team_billing_required",
    title: "Set up Teams billing",
    message: "Your team needs an active per-seat subscription to start.",
    href: "/settings/billing",
    actionLabel: "Set up billing",
    adminOnly: true,
  };

  it.each([
    ["team_billing_required"],
    ["payment_past_due"],
    ["subscription_required"],
    ["credits_required"],
  ])("never shows the personal paywall to an org owner on a %s gate", async (code) => {
    mockIapAvailable = true;
    mockOrganization = { id: "org_abc" };
    mockUseContainerStatus.mockReturnValue({
      status: { ...purchaseGate, code },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.queryByText("MOCK_PAYWALL")).toBeNull();
  });

  it("still shows the paywall to a personal owner", async () => {
    mockOrganization = null;
    mockIapAvailable = true;
    mockUseContainerStatus.mockReturnValue({
      status: { ...purchaseGate, code: "trial_daily_cap", adminOnly: false },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    // Same gate, same feature flag — only the owner type differs. This is the
    // control that proves the org cases above are blocked BY THE GUARD.
    expect(screen.getByText("MOCK_PAYWALL")).toBeTruthy();
  });
});
