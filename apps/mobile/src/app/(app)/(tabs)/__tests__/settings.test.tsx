import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { Linking } from "react-native";

// Per-test billing fixture the mocked api client serves (factory is lazy —
// the closure reads this at request time, after each test assigns it). An
// Error is served as a failed request.
let mockAccountFixture: Record<string, unknown> | Error;
const mockPost = jest.fn(async (): Promise<unknown> => ({ status: "deleted" }));
let mockMembershipCount = 1;
const mockPush = jest.fn();
// Per-test Clerk org context (null = no workspace).
let mockOrg: { organization: unknown; membership: unknown } = { organization: null, membership: null };

jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok"), signOut: jest.fn() }),
  useUser: () => ({
    user: {
      id: "user_1",
      fullName: "Test User",
      primaryEmailAddress: { emailAddress: "test@example.com" },
      get organizationMemberships() {
        return Array.from({ length: mockMembershipCount }, (_, i) => ({ id: `mem_${i}` }));
      },
    },
  }),
  useOrganization: () => mockOrg,
}));

// Real useFocusEffect needs a navigation container; an effect is equivalent
// for a single mount.
jest.mock("expo-router", () => {
  const { useEffect } = jest.requireActual("react");
  return {
    useFocusEffect: (cb: () => void) => useEffect(cb, [cb]),
    router: { push: (href: string) => mockPush(href) },
  };
});

jest.mock("../../../../lib/pushDevice", () => ({ unregisterDevice: jest.fn() }));

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({
    get: (path: string) =>
      path !== "/billing/account"
        ? Promise.reject(new Error("unavailable"))
        : mockAccountFixture instanceof Error
          ? Promise.reject(mockAccountFixture)
          : Promise.resolve(mockAccountFixture),
    del: () => Promise.resolve(),
    post: mockPost,
  }),
}));

import Settings from "../settings";

describe("Settings plan card", () => {
  it("renders the free tier as Free plan (never the raw trial_local status)", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "trial_local",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Lightbulb Free plan")).toBeTruthy();
    expect(screen.getByText(/daily free usage allowance/i)).toBeTruthy();
    // The chat product's model-provider card is gone from the voice app.
    expect(screen.queryByText("Lightbulb-managed models")).toBeNull();
    expect(screen.queryByText(/trial_local/i)).toBeNull();
    expect(screen.queryByText(/No provider selected/)).toBeNull();
    expect(screen.queryByText(/Claude Sonnet/)).toBeNull();
  });

  it("shows days left on an active free trial, from trial_end alone", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "trial_local",
      trial_end: Math.floor(Date.now() / 1000) + 5 * 86_400,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText(/Free trial · 5 days left/)).toBeTruthy();
  });

  it("shows the ended state once a free trial's trial_end has passed", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "trial_local",
      trial_end: Math.floor(Date.now() / 1000) - 86_400,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Free trial ended")).toBeTruthy();
    expect(screen.getByText(/This workspace is reading-only/)).toBeTruthy();
  });

  it("labels a paid owner who never picked a provider as managed, not unset", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      plan: "standard",
    };
    await render(<Settings />);
    expect(await screen.findByText("Lightbulb")).toBeTruthy();
    expect(screen.queryByText("Lightbulb-managed models")).toBeNull();
    expect(screen.queryByText(/No provider selected/)).toBeNull();
  });

  it("says when billing fails to load, and Retry asks again", async () => {
    mockAccountFixture = new Error("offline");
    await render(<Settings />);
    expect(await screen.findByText("Couldn’t load billing: offline")).toBeTruthy();

    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await fireEvent.press(screen.getByText("Retry"));
    expect(await screen.findByText("Lightbulb")).toBeTruthy();
    expect(screen.queryByText(/Couldn’t load billing/)).toBeNull();
  });

  it("renders a paid account", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: "chatgpt_oauth",
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Lightbulb")).toBeTruthy();
    expect(screen.getByText("Active")).toBeTruthy();
  });

  // App Store 3.1.3(f): "Manage subscription" opens the Stripe billing portal
  // for web-billed subscribers — that is a purchase-adjacent web handoff, so
  // it must not render for them. It stays for Apple-billed subscribers
  // (management only, via expo-iap's iOS Settings deep link).
  it("hides Manage subscription for a Stripe/web-billed subscriber", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      subscription_source: "stripe",
      trial_end: null,
      provider_choice: "openrouter_managed",
      byo_provider: null,
    };
    await render(<Settings />);
    await screen.findByText("Lightbulb");
    expect(screen.getByText("Billed on the web")).toBeTruthy();
    expect(screen.queryByText("Manage subscription")).toBeNull();
    expect(screen.queryByText(/billing portal/i)).toBeNull();
  });

  it("keeps Manage subscription for an Apple-billed subscriber", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      subscription_source: "apple_iap",
      trial_end: null,
      provider_choice: "openrouter_managed",
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Manage subscription")).toBeTruthy();
    expect(screen.getByText(/iOS Settings/)).toBeTruthy();
  });

  it("carries no top-up call to action next to the credits balance", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: "openrouter_managed",
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Workspace credits")).toBeTruthy();
    expect(screen.queryByText(/top up/i)).toBeNull();
  });

  it("labels a migrated owner's provider and credits as Lightbulb-managed", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: "openrouter_managed",
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Workspace credits")).toBeTruthy();
    expect(screen.queryByText("Lightbulb-managed models")).toBeNull();
    expect(screen.queryByText(/OpenRouter|API key/i)).toBeNull();
  });

  // App Store 3.1.3(f): the exemption holds only while the app carries no
  // call to action for purchase outside it. This screen used to have two —
  // "Upgrade on thelightbulb.company" and "Manage billing on thelightbulb.company" — plus the plan
  // price. Plan STATE still renders; nothing that points at a purchase does.
  it.each([
    ["trial_local" as const],
    ["active" as const],
    [null],
  ])("carries no purchase call to action for a %s account", async (status) => {
    mockAccountFixture = {
      is_subscribed: status === "active",
      subscription_status: status,
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    // getAllByText, not findByText: the managed-provider card's "Lightbulb-managed
    // models" line also matches /Lightbulb/, and findByText throws on >1 match.
    await waitFor(() =>
      expect(screen.getAllByText(/Lightbulb|No plan on this account/).length).toBeGreaterThan(0),
    );

    expect(screen.queryByText(/upgrade/i)).toBeNull();
    expect(screen.queryByText(/manage billing/i)).toBeNull();
    expect(screen.queryByText(/\$50/)).toBeNull();
    // The legal links are the ONLY thelightbulb.company references that may remain.
    expect(screen.queryByText(/isol8\.co\/settings/)).toBeNull();
  });
});

describe("Settings account controls (App Store readiness)", () => {
  it("renders in-app legal links (5.1.1(i))", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Privacy Policy")).toBeTruthy();
    expect(screen.getByText("Terms of Service")).toBeTruthy();
    expect(screen.getByText("Contact support")).toBeTruthy();
    expect(screen.getByText("Delete account")).toBeTruthy();
  });

  it("walks the two-step delete confirm and posts the typed email (5.1.1(v))", async () => {
    const { Alert } = jest.requireActual("react-native");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    const promptSpy = jest.spyOn(Alert, "prompt").mockImplementation(() => {});
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    fireEvent.press(await screen.findByText("Delete account"));

    // Step 1: permanence warning.
    const [title, , buttons] = alertSpy.mock.calls[0] as [
      string,
      string,
      { text: string; onPress?: () => void }[],
    ];
    expect(title).toBe("Delete account");
    buttons.find((b) => b.text === "Continue")?.onPress?.();

    // Step 2: typed-email prompt → posts to the self-serve endpoint.
    const promptButtons = promptSpy.mock.calls[0][2] as {
      text: string;
      onPress?: (v?: string) => void;
    }[];
    promptButtons.find((b) => b.text === "Delete forever")?.onPress?.("me@x.co");
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/users/me/delete", {
        confirm_email: "me@x.co",
      }),
    );
    alertSpy.mockRestore();
    promptSpy.mockRestore();
  });
});

// An Apple subscription OUTLIVES the account: wipe_account deletes the Stripe
// customer, the billing row and the Clerk user, but cannot touch StoreKit.
// Delete without cancelling and Apple keeps charging for something that now
// entitles nothing — and the appAccountToken binding means the purchase cannot
// be restored onto a replacement account either.
describe("deleting an Apple-billed account", () => {
  it("warns and offers iOS Settings before the irreversible step", async () => {
    const { Alert } = jest.requireActual("react-native");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      subscription_source: "apple_iap",
      trial_end: null,
      provider_choice: "openrouter_managed",
      byo_provider: null,
    };
    await render(<Settings />);
    fireEvent.press(await screen.findByText("Delete account"));

    const [title, body, buttons] = alertSpy.mock.calls[0] as [
      string,
      string,
      { text: string }[],
    ];
    expect(title).toMatch(/cancel your subscription first/i);
    expect(body).toMatch(/does not cancel/i);
    expect(buttons.map((b) => b.text)).toEqual(["Not now", "Open iOS Settings", "Delete anyway"]);
    alertSpy.mockRestore();
  });

  it("on Android, says where to cancel and offers no deep link (it would open Google Play)", async () => {
    const { Alert, Platform } = jest.requireActual("react-native");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    const os = Platform.OS;
    Platform.OS = "android";
    try {
      mockAccountFixture = {
        is_subscribed: true,
        subscription_status: "active",
        subscription_source: "apple_iap",
        trial_end: null,
        provider_choice: "openrouter_managed",
        byo_provider: null,
      };
      await render(<Settings />);
      expect(await screen.findByText(/on the iPhone or iPad that bought it/)).toBeTruthy();
      expect(screen.queryByText("Manage subscription")).toBeNull();
      fireEvent.press(await screen.findByText("Delete account"));
      const [title, body, buttons] = alertSpy.mock.calls[0] as [string, string, { text: string }[]];
      expect(title).toMatch(/cancel your subscription first/i);
      expect(body).toMatch(/iPhone or iPad that bought it/);
      expect(buttons.map((b) => b.text)).toEqual(["Not now", "Delete anyway"]);
    } finally {
      Platform.OS = os;
      alertSpy.mockRestore();
    }
  });

  it("goes straight to the normal confirmation for a Stripe subscriber", async () => {
    const { Alert } = jest.requireActual("react-native");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      subscription_source: "stripe",
      trial_end: null,
      provider_choice: "openrouter_managed",
      byo_provider: null,
    };
    await render(<Settings />);
    fireEvent.press(await screen.findByText("Delete account"));

    expect(alertSpy.mock.calls[0][0]).toBe("Delete account");
    alertSpy.mockRestore();
  });
});

// F4: someone in two or more workspaces needs a way between them; one
// workspace has nothing to switch to, so the row stays out of the way.
describe("Settings Switch workspace", () => {
  beforeEach(() => {
    mockAccountFixture = new Error("offline");
    mockPush.mockClear();
  });

  it("is hidden with a single workspace", async () => {
    mockMembershipCount = 1;
    await render(<Settings />);
    expect(screen.queryByText("Switch workspace")).toBeNull();
  });

  it("opens the workspace list with two or more", async () => {
    mockMembershipCount = 2;
    await render(<Settings />);
    await fireEvent.press(screen.getByText("Switch workspace"));
    expect(mockPush).toHaveBeenCalledWith("/workspaces");
  });
});

// Email invitations: the row is for everyone; any member may invite (D5).
describe("Settings Invite people", () => {
  it("opens the invite screen", async () => {
    mockAccountFixture = new Error("offline");
    mockPush.mockClear();
    await render(<Settings />);
    await fireEvent.press(screen.getByText("Invite people"));
    expect(mockPush).toHaveBeenCalledWith("/invite");
  });
});

// Deleting as the SOLE member ends the workspace and its paid subscription
// immediately (no refund); say so only when that is actually what happens.
describe("delete-account confirmation copy", () => {
  const NO_REFUND = "Your workspace and its subscription end immediately. There's no refund for the current billing period.";

  afterEach(() => {
    mockOrg = { organization: null, membership: null };
  });

  async function firstAlertBody(membersCount: number, status: string): Promise<string> {
    const { Alert } = jest.requireActual("react-native");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    mockOrg = { organization: { id: "org_1", name: "Acme", membersCount }, membership: { role: "org:admin" } };
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: status,
      subscription_source: "stripe",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    await screen.findByText(/^Lightbulb( Free plan)?$/); // billing loaded
    fireEvent.press(await screen.findByText("Delete account"));
    const body = alertSpy.mock.calls[0][1] as string;
    alertSpy.mockRestore();
    return body;
  }

  it.each(["active", "trialing"])("warns the sole member of a %s workspace", async (status) => {
    expect(await firstAlertBody(1, status)).toContain(NO_REFUND);
  });

  it("keeps the existing copy when other members remain", async () => {
    expect(await firstAlertBody(3, "active")).not.toContain(NO_REFUND);
  });

  it("keeps the existing copy on the free plan", async () => {
    expect(await firstAlertBody(1, "trial_local")).not.toContain(NO_REFUND);
  });
});

describe("delete-account refusal", () => {
  it("shows the server's 409 message verbatim", async () => {
    const { Alert } = jest.requireActual("react-native");
    const { ApiError } = jest.requireActual("@lightbulb/shared");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    const promptSpy = jest.spyOn(Alert, "prompt").mockImplementation(() => {});
    const message = "You're the admin of Acme. Make someone else an admin, or delete the workspace first.";
    mockPost.mockRejectedValueOnce(new ApiError(409, { detail: message }));
    mockAccountFixture = {
      is_subscribed: false,
      subscription_status: null,
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    fireEvent.press(await screen.findByText("Delete account"));
    (alertSpy.mock.calls[0][2] as { text: string; onPress?: () => void }[])
      .find((b) => b.text === "Continue")
      ?.onPress?.();
    (promptSpy.mock.calls[0][2] as { text: string; onPress?: (v?: string) => void }[])
      .find((b) => b.text === "Delete forever")
      ?.onPress?.("me@x.co");

    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("Couldn't delete account", message));
    alertSpy.mockRestore();
    promptSpy.mockRestore();
  });
});

// In-app purchase: the Plans row shows only when the server offers products
// and the workspace isn't already billed through Stripe (which the App Store
// can't see or change).
describe("Settings Plans row", () => {
  const PRODUCTS = ["company.thelightbulb.mobile.base.monthly"];
  // The free tier is not a paid subscription: /billing/account's
  // is_subscribed is the paid predicate.
  const free = {
    is_subscribed: false,
    subscription_status: "trial_local",
    trial_end: null,
    provider_choice: null,
    byo_provider: null,
  };

  beforeEach(() => {
    mockPush.mockClear();
    mockMembershipCount = 1;
  });

  it("shows for an admin on the free tier and opens the plans screen", async () => {
    mockAccountFixture = { ...free, apple_iap_product_ids: PRODUCTS };
    await render(<Settings />);
    await fireEvent.press(await screen.findByText("Plans"));
    expect(mockPush).toHaveBeenCalledWith("/plans");
  });

  it("is hidden when the server offers no products", async () => {
    mockAccountFixture = { ...free, apple_iap_product_ids: [] };
    await render(<Settings />);
    await screen.findByText("Lightbulb Free plan");
    expect(screen.queryByText("Plans")).toBeNull();
  });

  it("is hidden for a Stripe-billed workspace", async () => {
    mockAccountFixture = {
      ...free,
      is_subscribed: true,
      subscription_status: "active",
      subscription_source: null,
      apple_iap_product_ids: PRODUCTS,
    };
    await render(<Settings />);
    await screen.findByText("Billed on the web");
    expect(screen.queryByText("Plans")).toBeNull();
  });

  it("shows for an Apple-billed workspace", async () => {
    mockAccountFixture = {
      ...free,
      is_subscribed: true,
      subscription_status: "active",
      subscription_source: "apple_iap",
      apple_iap_product_ids: PRODUCTS,
    };
    await render(<Settings />);
    expect(await screen.findByText("Plans")).toBeTruthy();
  });
});

describe("Settings absorbs More", () => {
  beforeEach(() => {
    mockAccountFixture = { is_subscribed: true, subscription_status: "trial_local", trial_end: null, provider_choice: null, byo_provider: null };
    mockPush.mockClear();
  });

  it("has no switch for a pinned strip that is gone", async () => {
    await render(<Settings />);
    expect(await screen.findByText("Billing and phone settings")).toBeTruthy();
    expect(screen.queryByLabelText("Show running sessions in pinned")).toBeNull();
  });

  it("opens billing and phone settings on the web", async () => {
    const open = jest.spyOn(Linking, "openURL").mockResolvedValue(true);
    await render(<Settings />);
    await fireEvent.press(await screen.findByLabelText("Billing and phone settings"));
    expect(open).toHaveBeenCalledWith("https://thelightbulb.company/voice/settings");
  });

});
