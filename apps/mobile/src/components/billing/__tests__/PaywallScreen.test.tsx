import { act, render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { Linking } from "react-native";

import { PaywallScreen } from "../PaywallScreen";

let mockStorefront: Promise<string> = Promise.resolve("USA");

jest.mock(
  "expo-iap",
  () => ({
    ErrorCode: { UserCancelled: "user-cancelled" },
    getAvailablePurchases: jest.fn(async () => []),
    getStorefront: () => mockStorefront,
    useIAP: () => ({
      connected: true,
      subscriptions: [{ id: "retired.apple.sku", displayPrice: "$64.99" }],
      fetchProducts: jest.fn(async () => undefined),
      requestPurchase: jest.fn(async () => undefined),
      finishTransaction: jest.fn(async () => undefined),
      restorePurchases: jest.fn(async () => undefined),
    }),
  }),
  { virtual: true },
);

jest.mock("../../../lib/appleIap", () => ({
  APPLE_IAP_AVAILABLE: true,
  APPLE_SUBSCRIPTION_SKU: "retired.apple.sku",
  ownerAccountToken: jest.fn(async () => "retired-token"),
  linkApplePurchase: jest.fn(async () => undefined),
}), { virtual: true });

function makeApi(
  workspacePriceUsd: number | null = 50,
  checkoutUrl = "https://checkout.stripe.com/c/pay/cs_test_123",
) {
  return {
    get: jest.fn(async () =>
      workspacePriceUsd == null ? {} : { workspace_price_usd: workspacePriceUsd },
    ),
    post: jest.fn(async () => ({ checkout_url: checkoutUrl })),
  };
}

describe("PaywallScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockStorefront = Promise.resolve("USA");
  });

  it("does not advertise StoreKit purchase or restore even if a legacy SKU is present", async () => {
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);

    expect(screen.queryByText("$64.99")).toBeNull();
    expect(screen.queryByText("Subscribe")).toBeNull();
    expect(screen.queryByText("Restore purchases")).toBeNull();
    expect(await screen.findByText("Subscribe on flickmeon.ai")).toBeTruthy();
  });

  it("takes the workspace price from the server", async () => {
    const api = makeApi(42);
    await render(<PaywallScreen api={api} onPurchased={jest.fn()} />);

    expect(api.get).toHaveBeenCalledWith("/billing/account");
    expect(await screen.findByText(/\$42\/month/)).toBeTruthy();
  });

  it("shows an unpriced link when the server price is unavailable", async () => {
    await render(<PaywallScreen api={makeApi(null)} onPurchased={jest.fn()} />);

    expect(await screen.findByText("Subscribe on flickmeon.ai")).toBeTruthy();
    expect(screen.queryByText(/\$50\/month/)).toBeNull();
  });

  it("keeps the external checkout hidden outside the U.S. storefront", async () => {
    mockStorefront = Promise.resolve("GBR");
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);

    await act(async () => {
      await mockStorefront;
    });
    expect(screen.queryByText("Subscribe on flickmeon.ai")).toBeNull();
  });

  it("opens the workspace Checkout session in the default browser", async () => {
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(undefined as never);
    const u = userEvent.setup();
    const api = makeApi();
    await render(<PaywallScreen api={api} onPurchased={jest.fn()} />);

    await u.press(await screen.findByText("Subscribe on flickmeon.ai"));

    expect(api.post).toHaveBeenCalledWith("/billing/checkout", { plan: "workspace" });
    await waitFor(() =>
      expect(openURL).toHaveBeenCalledWith("https://checkout.stripe.com/c/pay/cs_test_123"),
    );
    openURL.mockRestore();
  });

  it("falls back to web billing when Checkout cannot be created", async () => {
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(undefined as never);
    const u = userEvent.setup();
    const api = makeApi();
    api.post.mockRejectedValueOnce(new Error("stripe down"));
    await render(<PaywallScreen api={api} onPurchased={jest.fn()} />);

    await u.press(await screen.findByText("Subscribe on flickmeon.ai"));

    await waitFor(() =>
      expect(openURL).toHaveBeenCalledWith("https://flickmeon.ai/settings?panel=billing"),
    );
    openURL.mockRestore();
  });
});
