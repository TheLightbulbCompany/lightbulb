import { fireEvent, render, screen } from "@testing-library/react-native";
import { Linking, Text } from "react-native";

const mockRefetch = jest.fn();
const mockUseContainerStatus = jest.fn();

// `organization` decides whether this poller's container is personal or an
// organization workspace. Org billing stays on the existing admin handoff.
let mockOrganization: { id: string } | null = null;
let mockOrgLoaded = true;

jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ signOut: jest.fn() }),
  useOrganization: () => ({ organization: mockOrganization, isLoaded: mockOrgLoaded }),
}));

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

jest.mock("../../../lib/containerStatus", () => ({
  useContainerStatus: () => mockUseContainerStatus(),
}));

// Isolate ProvisionGate's routing decision from the ChatGPT screen internals.
jest.mock("../ChatGPTConnectScreen", () => {
  const { Text: RNText } = require("react-native");
  return {
    ChatGPTConnectScreen: ({ onConnected }: { onConnected: () => void }) => (
      <RNText onPress={onConnected}>MOCK_CHATGPT_CONNECT_SCREEN</RNText>
    ),
  };
});

// /billing/account, which the subscription gate reads for in-app products.
let mockAccount: unknown = {};
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({
    get: () => Promise.resolve(mockAccount),
    post: jest.fn(),
  }),
}));

// true = StoreKit has nothing to show (still loading, or none of the products).
let mockPaywallEmpty = false;
jest.mock("../../billing/PaywallScreen", () => {
  const { Text: RNText } = require("react-native");
  return {
    PaywallScreen: ({
      onPurchased,
      fallback,
      footer,
    }: {
      onPurchased: () => void;
      fallback?: unknown;
      footer?: unknown;
    }) =>
      mockPaywallEmpty ? (
        (fallback ?? null)
      ) : (
        <>
          <RNText onPress={onPurchased}>PAYWALL</RNText>
          {footer}
        </>
      ),
  };
});

jest.mock("../../workspace/WorkspaceGate", () => {
  const { Text: RNText } = require("react-native");
  return { WorkspaceChooser: () => <RNText>MOCK_WORKSPACE_CHOOSER</RNText> };
});

import { ProvisionGate } from "../ProvisionGate";
// Real (unmocked) module — only `react`-dependent, so it's safe to load for
// real here and lets this file prove ProvisionGate actually wires its
// polled status into the context, not just that its own mocked hook works.

// NOTE (mobile onboarding-parity Slice 4): written against the same
// mocking conventions proven in FirstRunGate.test.tsx and
// ChatGPTConnectScreen.test.tsx (jest.mock @clerk/expo +
// react-native-safe-area-context; `await render(...)`). The routing cases
// below cover all four blocking kinds this gate serves — blocked (generic,
// oauth_required, the legacy incomplete BYO gate, and the adminOnly/ownerRole
// variants), teamPending, needsSignup and error.
describe("ProvisionGate provider sign-in routing", () => {
  beforeEach(() => {
    mockRefetch.mockReset();
    mockUseContainerStatus.mockReset();
    mockOrganization = null;
    mockOrgLoaded = true;
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
  });

  it("does not offer new API-key setup for an incomplete legacy BYO workspace", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "byo_provider_required",
        title: "Finish setting up your inference provider",
        message: "This workspace has an incomplete saved API-key setup. Choose OpenRouter managed inference or sign in with ChatGPT to continue.",
        href: "/chat?panel=llm&action=change-provider",
        actionLabel: "Change provider",
        adminOnly: true,
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.queryByText(/API key/i)).toBeNull();
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
    expect(screen.getByText("View invitation on thelightbulb.company")).toBeTruthy();
    expect(screen.queryByText("MOCK_CHATGPT_CONNECT_SCREEN")).toBeNull();
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
        message: "Choose OpenRouter managed inference or sign in with ChatGPT to continue.",
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
      screen.getByText("Ask your workspace admin to finish this on thelightbulb.company."),
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

  // F4: the server's 409 `workspace_required` is a first-class state that
  // routes to the workspace chooser, not a non-blocking "unreachable".
  it("routes workspaceRequired to the workspace chooser and hides the app", async () => {
    mockUseContainerStatus.mockReturnValue({
      status: { kind: "workspaceRequired" },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.getByText("MOCK_WORKSPACE_CHOOSER")).toBeTruthy();
    expect(screen.queryByText("APP")).toBeNull();
  });

  it("hands needsSignup users to workspace-first web onboarding", async () => {
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
    const handoff = screen.getByText("Continue on thelightbulb.company");
    await fireEvent.press(handoff);

    expect(Linking.openURL).toHaveBeenCalledWith("https://thelightbulb.company/onboarding");
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
    mockOrgLoaded = true;
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
    // No outbound "Manage on thelightbulb.company" CTA — there's nothing to manage for
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
    // async on this testing-library version — await to keep act() scoped.
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
    mockOrgLoaded = true;
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

// Issue #1468: a failed first-run kickoff rendered NOTHING to the owner — the
// reason was on the contract and only the admin page read it. Non-blocking, the
// same shape as the Teams affordance above: the container is healthy, chat
// works, and messaging Friday directly is the recovery the owner actually has.
describe("ProvisionGate workspace onboarding routing is personal-owner only", () => {
  const purchaseGate = {
    kind: "blocked" as const,
    code: "team_billing_required",
    title: "Set up workspace billing",
    message: "Your workspace needs an active Stripe subscription to start.",
    href: "/settings/billing",
    actionLabel: "Set up billing",
    adminOnly: true,
  };

  it.each([
    ["team_billing_required"],
    ["payment_past_due"],
    ["subscription_required"],
    ["credits_required"],
  ])("never shows personal onboarding to an org owner on a %s gate", async (code) => {
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

    expect(screen.queryByText("Continue on thelightbulb.company")).toBeNull();
  });

  it("sends a personal owner through web workspace onboarding before billing", async () => {
    mockOrganization = null;
    mockUseContainerStatus.mockReturnValue({
      status: { ...purchaseGate, code: "trial_daily_cap", adminOnly: false },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    // App Store 3.1.3(f): this is workspace setup, not a purchase — "and
    // billing" made it read as a paywall, so it's gone from the body copy.
    expect(
      screen.getByText("Continue on the web to create or select the workspace your agents belong to."),
    ).toBeTruthy();
    expect(screen.queryByText(/and billing/i)).toBeNull();

    const handoff = screen.getByText("Continue on thelightbulb.company");
    await fireEvent.press(handoff);

    expect(Linking.openURL).toHaveBeenCalledWith("https://thelightbulb.company/onboarding");
  });

  // While Clerk is still resolving, `organization` is null — indistinguishable
  // from a personal owner. If /container/status resolves a purchase gate first,
  // an org admin would briefly see the personal onboarding handoff. Unknown must
  // read as not-personal.
  it("does not show personal onboarding while Clerk has not resolved the owner type", async () => {
    mockOrgLoaded = false;
    mockOrganization = null;
    mockUseContainerStatus.mockReturnValue({
      status: {
        kind: "blocked",
        code: "trial_daily_cap",
        title: "Daily free limit reached",
        message: "You've used today's free allowance — Upgrade to keep going.",
        href: "/settings?panel=billing",
        actionLabel: "Upgrade",
        adminOnly: false,
      },
      refetch: mockRefetch,
    });

    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );

    expect(screen.queryByText("Continue on thelightbulb.company")).toBeNull();
  });
});

// In-app purchase at the subscription gate (App Store 3.1.1): an admin whose
// workspace can buy in the app gets the StoreKit paywall; a member is told to
// ask the admin; without products it is the neutral state-only card as before.
describe("ProvisionGate in-app plans at the subscription gate", () => {
  const trialExpired = {
    kind: "blocked" as const,
    code: "trial_expired",
    title: "Pick a plan to keep your agent",
    message: "Pick a plan to keep your agent.",
    href: "/settings?panel=billing",
    actionLabel: "Upgrade",
    adminOnly: true,
  };
  const PRODUCTS = { apple_iap_product_ids: ["company.thelightbulb.mobile.base.monthly"] };

  beforeEach(() => {
    mockRefetch.mockReset();
    mockUseContainerStatus.mockReset();
    mockOrganization = { id: "org_abc" };
    mockOrgLoaded = true;
    mockPaywallEmpty = false;
  });

  it("is never a dead end: with nothing from StoreKit the admin keeps the card and Retry", async () => {
    mockAccount = PRODUCTS;
    mockPaywallEmpty = true;
    mockUseContainerStatus.mockReturnValue({
      status: { ...trialExpired, ownerRole: "admin" },
      refetch: mockRefetch,
    });
    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );
    expect(await screen.findByText(/Your team's record is all here/)).toBeTruthy();
    await fireEvent.press(screen.getByText("Retry"));
    expect(mockRefetch).toHaveBeenCalled();
    expect(screen.getAllByText("Sign out")).toHaveLength(1);
  });

  it("shows the paywall to an admin when products are offered, and a purchase re-probes", async () => {
    mockAccount = PRODUCTS;
    mockUseContainerStatus.mockReturnValue({
      status: { ...trialExpired, ownerRole: "admin" },
      refetch: mockRefetch,
    });
    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );
    await fireEvent.press(await screen.findByText("PAYWALL"));
    expect(mockRefetch).toHaveBeenCalled();
    expect(screen.getByText("Sign out")).toBeTruthy();
    expect(screen.queryByText("APP")).toBeNull();
  });

  it("tells a member to ask their admin when products are offered", async () => {
    mockAccount = PRODUCTS;
    mockUseContainerStatus.mockReturnValue({
      status: { ...trialExpired, ownerRole: "member" },
      refetch: mockRefetch,
    });
    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );
    expect(await screen.findByText("Ask your workspace admin to choose a plan.")).toBeTruthy();
    expect(screen.queryByText("PAYWALL")).toBeNull();
  });

  it("keeps the neutral local copy for an admin when no products are offered", async () => {
    mockAccount = { apple_iap_product_ids: [] };
    mockUseContainerStatus.mockReturnValue({
      status: { ...trialExpired, ownerRole: "admin" },
      refetch: mockRefetch,
    });
    await render(
      <ProvisionGate>
        <Text>APP</Text>
      </ProvisionGate>,
    );
    expect(await screen.findByText(/Your team's record is all here/)).toBeTruthy();
    expect(screen.queryByText("PAYWALL")).toBeNull();
    expect(screen.queryByText(/Pick a plan/)).toBeNull();
  });
});
