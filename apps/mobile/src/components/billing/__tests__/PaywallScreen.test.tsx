import { render, screen, userEvent, waitFor } from "@testing-library/react-native";

// expo-iap is a native module; drive it from a fake so the purchase flow's
// ORDER can be asserted. That order is the load-bearing part: the server must
// verify before the transaction is finished, or a crash mid-flow loses a
// purchase the customer has already paid for.
const mockRequestPurchase = jest.fn(async () => undefined);
const mockFinishTransaction = jest.fn(async () => undefined);
const mockFetchProducts = jest.fn(async () => undefined);
const mockRestorePurchases = jest.fn(async () => undefined);
let capturedOnSuccess: ((p: { purchaseToken?: string | null }) => void) | null = null;
let mockSubscriptions: { id: string; displayPrice: string }[] = [];

jest.mock(
  "expo-iap",
  () => ({
    useIAP: (opts: { onPurchaseSuccess?: (p: unknown) => void }) => {
      capturedOnSuccess = opts.onPurchaseSuccess as never;
      return {
        connected: true,
        subscriptions: mockSubscriptions,
        fetchProducts: mockFetchProducts,
        requestPurchase: mockRequestPurchase,
        finishTransaction: mockFinishTransaction,
        restorePurchases: mockRestorePurchases,
      };
    },
  }),
  { virtual: true },
);

jest.mock("../../../lib/appleIap", () => ({
  APPLE_IAP_AVAILABLE: true,
  APPLE_SUBSCRIPTION_SKU: "co.isol8.mobile.plan.monthly",
  linkApplePurchase: jest.fn(async () => undefined),
}));

import { linkApplePurchase } from "../../../lib/appleIap";
import { PaywallScreen } from "../PaywallScreen";

const mockLink = linkApplePurchase as jest.Mock;

describe("PaywallScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    capturedOnSuccess = null;
    mockSubscriptions = [
      { id: "co.isol8.mobile.plan.monthly", displayPrice: "$64.99" },
    ];
  });

  // Apple requires the STORE's localized price. A hardcoded number is wrong in
  // every storefront but one, and stale the moment pricing changes.
  it("renders StoreKit's own localized price, not a hardcoded one", async () => {
    await render(<PaywallScreen post={jest.fn()} onPurchased={jest.fn()} />);
    expect(screen.getByText("$64.99")).toBeTruthy();
  });

  // Legal on the U.S. storefront under 3.1.1(a), which App Review restated in
  // the rejection. Both channels are offered side by side.
  it("offers the web purchase option alongside IAP", async () => {
    await render(<PaywallScreen post={jest.fn()} onPurchased={jest.fn()} />);
    expect(screen.getByText("Subscribe")).toBeTruthy();
    expect(screen.getByText("Subscribe on isol8.co")).toBeTruthy();
  });

  it("requests the subscription product on subscribe", async () => {
    const u = userEvent.setup();
    await render(<PaywallScreen post={jest.fn()} onPurchased={jest.fn()} />);

    await u.press(screen.getByText("Subscribe"));

    expect(mockRequestPurchase).toHaveBeenCalledWith({
      type: "subs",
      request: { apple: { sku: "co.isol8.mobile.plan.monthly" } },
    });
  });

  it("sends the signed transaction for server verification, then finishes it", async () => {
    const onPurchased = jest.fn();
    const order: string[] = [];
    mockLink.mockImplementation(async () => {
      order.push("verify");
    });
    mockFinishTransaction.mockImplementation(async () => {
      order.push("finish");
    });

    await render(<PaywallScreen post={jest.fn()} onPurchased={onPurchased} />);
    // The handler is fire-and-forget (StoreKit's listener is not awaited), so
    // settle on the observable end state rather than a single microtask tick.
    capturedOnSuccess?.({ purchaseToken: "jws-blob" });
    await waitFor(() => expect(onPurchased).toHaveBeenCalled());

    expect(mockLink).toHaveBeenCalledWith(expect.anything(), "jws-blob");
    // Verify BEFORE finish — reversing these loses a paid purchase on a crash.
    expect(order).toEqual(["verify", "finish"]);
  });

  it("does not finish the transaction when server verification fails", async () => {
    mockLink.mockRejectedValue(new Error("verification failed"));
    const onPurchased = jest.fn();

    await render(<PaywallScreen post={jest.fn()} onPurchased={onPurchased} />);
    await capturedOnSuccess?.({ purchaseToken: "jws-blob" });

    // Unfinished means StoreKit redelivers on next launch, which is the
    // recovery path — finishing here would discard the purchase for good.
    expect(mockFinishTransaction).not.toHaveBeenCalled();
    expect(onPurchased).not.toHaveBeenCalled();
    expect(await screen.findByText(/couldn't activate it yet/i)).toBeTruthy();
  });

  it("treats a purchase with no token as failed rather than granting anything", async () => {
    const onPurchased = jest.fn();

    await render(<PaywallScreen post={jest.fn()} onPurchased={onPurchased} />);
    await capturedOnSuccess?.({ purchaseToken: null });

    expect(mockLink).not.toHaveBeenCalled();
    expect(mockFinishTransaction).not.toHaveBeenCalled();
    expect(onPurchased).not.toHaveBeenCalled();
  });

  // Required by Apple for any non-consumable / subscription purchase.
  it("offers restore purchases", async () => {
    const u = userEvent.setup();
    await render(<PaywallScreen post={jest.fn()} onPurchased={jest.fn()} />);

    await u.press(screen.getByText("Restore purchases"));

    expect(mockRestorePurchases).toHaveBeenCalled();
  });

  it("does not offer a disabled Subscribe button before the product loads", async () => {
    mockSubscriptions = [];
    const u = userEvent.setup();
    await render(<PaywallScreen post={jest.fn()} onPurchased={jest.fn()} />);

    await u.press(screen.getByText("Subscribe"));

    expect(mockRequestPurchase).not.toHaveBeenCalled();
  });
});
