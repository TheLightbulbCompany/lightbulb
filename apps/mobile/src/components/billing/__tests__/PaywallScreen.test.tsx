import { render, screen, userEvent, waitFor } from "@testing-library/react-native";

// expo-iap is a native module; drive it from a fake so the purchase flow's
// ORDER can be asserted. That order is the load-bearing part: the server must
// verify before the transaction is finished, or a crash mid-flow loses a
// purchase the customer has already paid for.
const mockRequestPurchase = jest.fn(async () => undefined);
const mockFinishTransaction = jest.fn(async () => undefined);
const mockFetchProducts = jest.fn(async () => undefined);
const mockRestorePurchases = jest.fn(async () => undefined);
// What the MODULE-level getAvailablePurchases resolves to. Restore must read
// the transactions from here, never from the hook's `availablePurchases`
// state: that state is captured by the callback's closure at the render where
// Restore was tapped, so on a fresh launch it is empty and always will be —
// no state update can reach a closure that has already been created. The hook
// mock below therefore reports an empty array FOREVER, so any code that goes
// back to reading it fails these tests instead of shipping.
let mockRestoredPurchases: { id: string; purchaseToken?: string | null }[] = [];
const mockGetAvailablePurchases = jest.fn(async () => mockRestoredPurchases);
let capturedOnSuccess: ((p: { purchaseToken?: string | null }) => void) | null = null;
let mockSubscriptions: { id: string; displayPrice: string }[] = [];
let mockStorefront: Promise<string> = Promise.resolve("USA");

jest.mock(
  "expo-iap",
  () => ({
    ErrorCode: { UserCancelled: "user-cancelled" },
    getStorefront: () => mockStorefront,
    // Wrapped rather than referenced directly: the factory runs at require
    // time, before the const above is initialized, so a bare reference sits in
    // the temporal dead zone and the export comes out undefined.
    getAvailablePurchases: () => mockGetAvailablePurchases(),
    useIAP: (opts: { onPurchaseSuccess?: (p: unknown) => void }) => {
      capturedOnSuccess = opts.onPurchaseSuccess as never;
      return {
        connected: true,
        subscriptions: mockSubscriptions,
        fetchProducts: mockFetchProducts,
        requestPurchase: mockRequestPurchase,
        finishTransaction: mockFinishTransaction,
        restorePurchases: mockRestorePurchases,
        // Deliberately always empty — see mockRestoredPurchases above.
        availablePurchases: [],
      };
    },
  }),
  { virtual: true },
);

/** Authenticated-client stub; /billing/account carries the web price. */
function makeApi(flatPriceUsd: number | null = 50) {
  return {
    get: jest.fn(async () =>
      flatPriceUsd == null ? {} : { flat_price_usd: flatPriceUsd },
    ),
    post: jest.fn(async () => ({})),
  };
}

jest.mock("../../../lib/appleIap", () => ({
  APPLE_IAP_AVAILABLE: true,
  APPLE_SUBSCRIPTION_SKU: "co.isol8.mobile.plan.monthly",
  ownerAccountToken: jest.fn(async (id: string) => `token-for-${id}`),
  linkApplePurchase: jest.fn(async () => undefined),
  createWebCheckout: jest.fn(async () => "https://checkout.stripe.com/c/pay/cs_test_123"),
}));

import { Linking } from "react-native";

import { createWebCheckout, linkApplePurchase } from "../../../lib/appleIap";
import { PaywallScreen } from "../PaywallScreen";

const mockLink = linkApplePurchase as jest.Mock;
const mockWebCheckout = createWebCheckout as jest.Mock;

describe("PaywallScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    capturedOnSuccess = null;
    mockStorefront = Promise.resolve("USA");
    mockRestoredPurchases = [];
    // clearAllMocks clears CALLS but not implementations, so a rejection set by
    // one test leaks into every later one. Restore the happy path explicitly.
    mockLink.mockReset().mockResolvedValue(undefined);
    mockWebCheckout.mockReset().mockResolvedValue("https://checkout.stripe.com/c/pay/cs_test_123");
    mockFinishTransaction.mockReset().mockResolvedValue(undefined);
    mockSubscriptions = [
      { id: "co.isol8.mobile.plan.monthly", displayPrice: "$64.99" },
    ];
  });

  // Apple requires the STORE's localized price. A hardcoded number is wrong in
  // every storefront but one, and stale the moment pricing changes.
  it("renders StoreKit's own localized price, not a hardcoded one", async () => {
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);
    expect(screen.getByText("$64.99")).toBeTruthy();
  });

  // Legal on the U.S. storefront under 3.1.1(a), which App Review restated in
  // the rejection, and which Apple approved for Spotify in May 2025 — both
  // channels side by side, both priced.
  it("offers the web purchase option alongside IAP, with both prices", async () => {
    await render(<PaywallScreen api={makeApi(50)} onPurchased={jest.fn()} />);

    expect(screen.getByText("$64.99")).toBeTruthy();
    expect(screen.getByText("Subscribe")).toBeTruthy();
    expect(await screen.findByText(/\$50\/month/)).toBeTruthy();
    expect(screen.getByText("Subscribe on isol8.co")).toBeTruthy();
  });

  // The web price is server-owned. A figure baked into the binary goes stale
  // the moment pricing changes, and a wrong external price is a 5.1.1
  // violation rather than a cosmetic bug.
  it("takes the web price from the server, not a constant", async () => {
    const api = makeApi(42);
    await render(<PaywallScreen api={api} onPurchased={jest.fn()} />);

    expect(api.get).toHaveBeenCalledWith("/billing/account");
    expect(await screen.findByText(/\$42\/month/)).toBeTruthy();
  });

  // An unpriced link is permitted; a WRONG price is not. So a failed fetch
  // must drop the figure and keep the link, never fall back to a guess.
  it("shows the link without a price when the server price is unavailable", async () => {
    await render(<PaywallScreen api={makeApi(null)} onPurchased={jest.fn()} />);

    expect(await screen.findByText("Subscribe on isol8.co")).toBeTruthy();
    expect(screen.queryByText(/\$50\/month/)).toBeNull();
  });

  // Outside the U.S. the no-steering rule in 3.1.3 still bites, and this
  // screen would be a rejectable violation.
  it("hides the web option entirely outside the U.S. storefront", async () => {
    mockStorefront = Promise.resolve("GBR");

    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);
    await screen.findByText("$64.99");

    expect(screen.queryByText("Subscribe on isol8.co")).toBeNull();
    expect(screen.queryByText(/\$50\/month/)).toBeNull();
  });

  // Fail closed: until the storefront is known, the link must not render.
  // Gating on app availability instead would put the decision in App Store
  // Connect, where someone can change it without ever opening this file.
  it("hides the web option while the storefront is unresolved", async () => {
    mockStorefront = new Promise(() => {}); // never settles

    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);
    await screen.findByText("$64.99");

    expect(screen.queryByText("Subscribe on isol8.co")).toBeNull();
  });

  it("requests the subscription product on subscribe", async () => {
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);

    await u.press(screen.getByText("Subscribe"));

    expect(mockRequestPurchase).toHaveBeenCalledWith({
      type: "subs",
      request: { apple: { sku: "co.isol8.mobile.plan.monthly", appAccountToken: undefined } },
    });
  });

  // appAccountToken is the ONLY cross-account binding the server can trust: it
  // rides inside the payload Apple signs, so a replay by another account cannot
  // strip it. The server REQUIRES it, so a purchase that ships without one is
  // rejected — and the server's check was dead code for exactly as long as the
  // client sent nothing.
  it("stamps the owner's account token on the purchase", async () => {
    const u = userEvent.setup();
    await render(
      <PaywallScreen api={makeApi()} ownerId="user_abc" onPurchased={jest.fn()} />,
    );

    await u.press(screen.getByText("Subscribe"));

    await waitFor(() =>
      expect(mockRequestPurchase).toHaveBeenCalledWith({
        type: "subs",
        request: {
          apple: {
            sku: "co.isol8.mobile.plan.monthly",
            appAccountToken: "token-for-user_abc",
          },
        },
      }),
    );
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

    await render(<PaywallScreen api={makeApi()} onPurchased={onPurchased} />);
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

    await render(<PaywallScreen api={makeApi()} onPurchased={onPurchased} />);
    await capturedOnSuccess?.({ purchaseToken: "jws-blob" });

    // Unfinished means StoreKit redelivers on next launch, which is the
    // recovery path — finishing here would discard the purchase for good.
    expect(mockFinishTransaction).not.toHaveBeenCalled();
    expect(onPurchased).not.toHaveBeenCalled();
    expect(await screen.findByText(/couldn't activate it yet/i)).toBeTruthy();
  });

  it("treats a purchase with no token as failed rather than granting anything", async () => {
    const onPurchased = jest.fn();

    await render(<PaywallScreen api={makeApi()} onPurchased={onPurchased} />);
    await capturedOnSuccess?.({ purchaseToken: null });

    expect(mockLink).not.toHaveBeenCalled();
    expect(mockFinishTransaction).not.toHaveBeenCalled();
    expect(onPurchased).not.toHaveBeenCalled();
  });

  // The web option opens a real Stripe Checkout session, not the settings
  // panel — and it must go to the DEFAULT BROWSER. Apple's own wording in our
  // rejection is that U.S. apps "may link out to the default browser"; an
  // embedded WebView checkout is not linking out, it is the in-app storefront
  // 3.1.1 forbids.
  it("opens a Stripe Checkout session in the default browser", async () => {
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(undefined as never);
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);
    await screen.findByText("Subscribe on isol8.co");

    await u.press(screen.getByText("Subscribe on isol8.co"));

    expect(mockWebCheckout).toHaveBeenCalled();
    await waitFor(() =>
      expect(openURL).toHaveBeenCalledWith("https://checkout.stripe.com/c/pay/cs_test_123"),
    );
    openURL.mockRestore();
  });

  // A failed session must not leave a dead button — fall back to the billing
  // page rather than stranding someone who is trying to pay us.
  it("falls back to the billing page when the session cannot be created", async () => {
    mockWebCheckout.mockRejectedValueOnce(new Error("stripe down"));
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(undefined as never);
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);
    await screen.findByText("Subscribe on isol8.co");

    await u.press(screen.getByText("Subscribe on isol8.co"));

    await waitFor(() =>
      expect(openURL).toHaveBeenCalledWith("https://isol8.co/settings?panel=billing"),
    );
    openURL.mockRestore();
  });

  // Required by Apple for any non-consumable / subscription purchase.
  it("offers restore purchases", async () => {
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);

    await u.press(screen.getByText("Restore purchases"));

    expect(mockRestorePurchases).toHaveBeenCalled();
  });

  it("does not offer a disabled Subscribe button before the product loads", async () => {
    mockSubscriptions = [];
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);

    await u.press(screen.getByText("Subscribe"));

    expect(mockRequestPurchase).not.toHaveBeenCalled();
  });
});

// Restore is the path a reinstall or new device depends on. restorePurchases()
// only refreshes the hook's available-purchases state — it does NOT fire the
// success handler — so calling it and stopping there made Restore appear to
// work while sending nothing to the server, leaving a paying customer unable
// to recover what they had bought.
describe("restore purchases", () => {
  it("submits each restored token through verify-then-finish", async () => {
    mockRestoredPurchases = [
      { id: "co.isol8.mobile.plan.monthly", purchaseToken: "restored-jws" },
    ];
    const onPurchased = jest.fn();
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={onPurchased} />);

    await u.press(screen.getByText("Restore purchases"));

    await waitFor(() =>
      expect(mockLink).toHaveBeenCalledWith(expect.anything(), "restored-jws"),
    );
    expect(mockFinishTransaction).toHaveBeenCalled();
    expect(onPurchased).toHaveBeenCalled();
  });

  it("ignores restored purchases for other products", async () => {
    mockRestoredPurchases = [{ id: "com.someone.else.pro", purchaseToken: "other-jws" }];
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);

    await u.press(screen.getByText("Restore purchases"));

    // Asserted on THIS token rather than "never called at all": a restore from
    // an earlier test can still be settling against the shared mock, and what
    // matters here is that a foreign product is never submitted as ours.
    await waitFor(() => expect(mockRestorePurchases).toHaveBeenCalled());
    expect(mockLink).not.toHaveBeenCalledWith(expect.anything(), "other-jws");
    expect(await screen.findByText(/No previous purchase found/)).toBeTruthy();
  });

  it("says so when there is nothing to restore, rather than failing silently", async () => {
    mockRestoredPurchases = [];
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);

    await u.press(screen.getByText("Restore purchases"));

    expect(await screen.findByText(/No previous purchase found/)).toBeTruthy();
  });

  it("reports a restore that could not be recorded, since the customer already paid", async () => {
    mockRestoredPurchases = [
      { id: "co.isol8.mobile.plan.monthly", purchaseToken: "restored-jws" },
    ];
    mockLink.mockRejectedValueOnce(new Error("server down"));
    const onPurchased = jest.fn();
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={onPurchased} />);

    await u.press(screen.getByText("Restore purchases"));

    expect(await screen.findByText(/couldn't restore it/i)).toBeTruthy();
    expect(onPurchased).not.toHaveBeenCalled();
  });
});

// The restore bug that survived one round of "fixing": the transactions were
// read from the hook's `availablePurchases` state, which the running callback
// captured at the render where Restore was tapped. Refreshing that state
// cannot reach an already-created closure, so on a fresh launch it was empty
// and a paying customer was told they had never bought anything.
describe("restore reads the transactions it just fetched", () => {
  it("uses the value getAvailablePurchases resolves to, not hook state", async () => {
    mockRestoredPurchases = [
      { id: "co.isol8.mobile.plan.monthly", purchaseToken: "fresh-jws" },
    ];
    const onPurchased = jest.fn();
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={onPurchased} />);

    await u.press(screen.getByText("Restore purchases"));

    // The hook's availablePurchases is hardcoded empty in this suite, so a
    // link call here can only have come from the module-level return value.
    await waitFor(() =>
      expect(linkApplePurchase).toHaveBeenCalledWith(expect.anything(), "fresh-jws"),
    );
    expect(mockGetAvailablePurchases).toHaveBeenCalled();
    await waitFor(() => expect(onPurchased).toHaveBeenCalled());
  });

  it("syncs with the store before reading, which is what a reinstall needs", async () => {
    const order: string[] = [];
    mockRestorePurchases.mockImplementation(async () => {
      order.push("sync");
      return undefined;
    });
    mockGetAvailablePurchases.mockImplementation(async () => {
      order.push("read");
      return mockRestoredPurchases;
    });
    const u = userEvent.setup();
    await render(<PaywallScreen api={makeApi()} onPurchased={jest.fn()} />);

    await u.press(screen.getByText("Restore purchases"));

    await waitFor(() => expect(order).toEqual(["sync", "read"]));
  });
});
