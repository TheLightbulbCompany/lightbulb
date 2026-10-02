import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { AppState, Linking } from "react-native";

// expo-iap is a native module; drive it from a fake so the purchase flow's
// ORDER can be asserted. The server must confirm before the transaction is
// finished, or a crash mid-flow loses a purchase Apple has already charged.
const mockRequestPurchase = jest.fn();
const mockFinish = jest.fn();
const mockFetchProducts = jest.fn();
let mockHandlers: {
  onPurchaseSuccess?: (p: unknown) => void;
  onPurchaseError?: (e: unknown) => void;
} = {};
let mockSubscriptions: { id: string; title: string; displayPrice: string }[] = [];

const mockGetAvailablePurchases = jest.fn();

jest.mock("expo-iap", () => ({
  ErrorCode: { UserCancelled: "user-cancelled" },
  getAvailablePurchases: () => mockGetAvailablePurchases(),
  useIAP: (handlers: typeof mockHandlers) => {
    mockHandlers = handlers;
    return {
      connected: true,
      subscriptions: mockSubscriptions,
      fetchProducts: mockFetchProducts,
      requestPurchase: mockRequestPurchase,
      finishTransaction: mockFinish,
      restorePurchases: jest.fn(),
    };
  },
}));

import { IapProvider } from "../IapProvider";
import { PaywallScreen } from "../PaywallScreen";
import { isInFlight } from "../../../lib/appleIap";

const BASE = "company.thelightbulb.mobile.base.monthly";
const WORKSPACE = "company.thelightbulb.mobile.plan.monthly";
const ULTRA = "company.thelightbulb.mobile.ultra.monthly";
const ALL = [BASE, WORKSPACE, ULTRA];
const INTENT = { app_account_token: "tok", product_ids: ALL };

/** Intent succeeds; the link answers with `link` (a value, or a rejection). */
function postWith(link: () => Promise<unknown>) {
  return jest.fn(async (path: string) => (path === "/billing/apple/purchase-intent" ? INTENT : link()));
}

/** Tap Buy on Base, then have StoreKit deliver the Base purchase. */
async function buyBase(purchase: Record<string, unknown> = { productId: BASE, purchaseToken: "jws" }) {
  await fireEvent.press(screen.getAllByText("Buy")[0]);
  await waitFor(() => expect(mockRequestPurchase).toHaveBeenCalled());
  await act(async () => mockHandlers.onPurchaseSuccess?.(purchase));
  return purchase;
}

function makeApi(post: jest.Mock = jest.fn().mockResolvedValue(INTENT)) {
  return { get: jest.fn(), post };
}

async function renderPaywall(
  opts: {
    api?: ReturnType<typeof makeApi>;
    productIds?: string[];
    currentProductId?: string | null;
    onPurchased?: jest.Mock;
    onWebReturn?: jest.Mock;
    account?: Record<string, number | string> | null;
  } = {},
) {
  const onPurchased = opts.onPurchased ?? jest.fn();
  const api = opts.api ?? makeApi();
  const view = await render(
    <IapProvider>
      <PaywallScreen
        api={api}
        productIds={opts.productIds ?? ALL}
        currentProductId={opts.currentProductId ?? null}
        onPurchased={onPurchased}
        account={opts.account ?? null}
        onWebReturn={opts.onWebReturn}
      />
    </IapProvider>,
  );
  return { ...view, api, onPurchased };
}

describe("PaywallScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockHandlers = {};
    mockGetAvailablePurchases.mockResolvedValue([]);
    // Deliberately out of plan order: the screen must sort.
    mockSubscriptions = [
      { id: ULTRA, title: "Ultra", displayPrice: "$179.99" },
      { id: BASE, title: "Base", displayPrice: "$23.99" },
      { id: WORKSPACE, title: "Workspace", displayPrice: "$59.99" },
    ];
  });

  it("renders nothing when the server offers no products", async () => {
    const { toJSON } = await renderPaywall({ productIds: [] });
    expect(toJSON()).toBeNull();
    expect(mockFetchProducts).not.toHaveBeenCalled();
  });

  it("renders nothing when StoreKit returns none of the products", async () => {
    mockSubscriptions = [];
    const { toJSON } = await renderPaywall();
    expect(toJSON()).toBeNull();
  });

  it("lists StoreKit prices in plan order, with no web option when the account has no prices", async () => {
    await renderPaywall();
    expect(mockFetchProducts).toHaveBeenCalledWith({ skus: ALL, type: "subs" });
    const prices = screen.getAllByText(/\$\d+\.\d\d\/month/).map((n) => n.props.children);
    expect(prices.join(" ")).toMatch(/\$23\.99.*\$59\.99.*\$179\.99/);
    expect(screen.queryByText(/flickmeon|web/i)).toBeNull();
  });

  it("buys with the server's account token", async () => {
    const { api } = await renderPaywall();
    await fireEvent.press(screen.getAllByText("Buy")[0]);
    await waitFor(() =>
      expect(mockRequestPurchase).toHaveBeenCalledWith({
        type: "subs",
        request: { apple: { sku: BASE, appAccountToken: "tok" } },
      }),
    );
    expect(api.post).toHaveBeenCalledWith("/billing/apple/purchase-intent", {});
  });

  it("a second tap while busy starts one purchase", async () => {
    let resolveIntent: (v: unknown) => void = () => {};
    const post = jest.fn(() => new Promise((r) => (resolveIntent = r)));
    await renderPaywall({ api: makeApi(post) });
    await fireEvent.press(screen.getAllByText("Buy")[0]);
    await fireEvent.press(screen.getAllByText("Buy")[1]);
    await act(async () => resolveIntent(INTENT));
    expect(post).toHaveBeenCalledTimes(1);
    expect(mockRequestPurchase).toHaveBeenCalledTimes(1);
  });

  it("finishes only after the link succeeds", async () => {
    const order: string[] = [];
    const post = jest.fn(async (path: string) => {
      order.push(path);
      return path === "/billing/apple/purchase-intent" ? INTENT : { plan: "base", status: "active" };
    });
    mockFinish.mockImplementation(async () => {
      order.push("finish");
    });
    const onPurchased = jest.fn(() => order.push("onPurchased"));
    await renderPaywall({ api: makeApi(post), onPurchased });
    const purchase = await buyBase();
    await waitFor(() => expect(onPurchased).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith("/billing/apple/link", { signed_transaction: "jws" });
    expect(mockFinish).toHaveBeenCalledWith({ purchase, isConsumable: false });
    expect(order).toEqual(["/billing/apple/purchase-intent", "/billing/apple/link", "finish", "onPurchased"]);
  });

  it("marks the product in flight while buying, so the launch listener skips it", async () => {
    let resolveLink: (v: unknown) => void = () => {};
    const post = postWith(() => new Promise((r) => (resolveLink = r)));
    await renderPaywall({ api: makeApi(post) });
    await buyBase();
    expect(isInFlight(BASE)).toBe(true);
    await act(async () => resolveLink({ plan: "base", status: "active" }));
    await waitFor(() => expect(isInFlight(BASE)).toBe(false));
  });

  it("a delivery of another plan mid-buy is linked, finished, and ends the purchase", async () => {
    const post = postWith(async () => ({ plan: "ultra_pro", status: "active" }));
    const { onPurchased } = await renderPaywall({ api: makeApi(post) });
    const ultra = { productId: ULTRA, purchaseToken: "jws-ultra" };
    await buyBase(ultra);
    await waitFor(() => expect(onPurchased).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith("/billing/apple/link", { signed_transaction: "jws-ultra" });
    expect(mockFinish).toHaveBeenCalledWith({ purchase: ultra, isConsumable: false });
    expect(ALL.some(isInFlight)).toBe(false);
    // Busy cleared: Buy is tappable again.
    await fireEvent.press(screen.getAllByText("Buy")[0]);
    await waitFor(() => expect(mockRequestPurchase).toHaveBeenCalledTimes(2));
  });

  it("leaves a delivery it did not start to the launch listener", async () => {
    const post = postWith(async () => ({ plan: "base", status: "active" }));
    await renderPaywall({ api: makeApi(post) });
    await act(async () => mockHandlers.onPurchaseSuccess?.({ productId: BASE, purchaseToken: "jws" }));
    expect(post).not.toHaveBeenCalled();
    expect(mockFinish).not.toHaveBeenCalled();
  });

  it("a failed link leaves the transaction unfinished", async () => {
    const post = postWith(() => Promise.reject(new Error("503")));
    const { onPurchased } = await renderPaywall({ api: makeApi(post) });
    await buyBase();
    await screen.findByText(/It will finish the next time you open the app/);
    expect(mockFinish).not.toHaveBeenCalled();
    expect(onPurchased).not.toHaveBeenCalled();
  });

  it.each([400, 403])("a link the server refuses (%i) says so instead of promising a retry", async (status) => {
    const post = postWith(() => Promise.reject({ status, body: { detail: "nope" } }));
    const { onPurchased } = await renderPaywall({ api: makeApi(post) });
    await buyBase();
    await screen.findByText(
      "We couldn't apply this purchase to this workspace. Contact support@thelightbulb.company.",
    );
    expect(screen.queryByText(/next time you open the app/)).toBeNull();
    expect(mockFinish).not.toHaveBeenCalled();
    expect(onPurchased).not.toHaveBeenCalled();
  });

  it("a link refused because Stripe now bills the workspace stays unfinished and points at a refund", async () => {
    const post = postWith(() => Promise.reject({ status: 409, body: { detail: "stripe_managed" } }));
    const { onPurchased } = await renderPaywall({ api: makeApi(post) });
    await buyBase();
    await screen.findByText(
      "This workspace is already billed another way, so this App Store purchase wasn't applied. Contact support@thelightbulb.company for a refund.",
    );
    expect(mockFinish).not.toHaveBeenCalled();
    expect(onPurchased).not.toHaveBeenCalled();
  });

  it("a stripe-billed workspace never reaches StoreKit", async () => {
    const post = jest.fn().mockRejectedValue({ status: 409, body: { detail: "stripe_managed" } });
    await renderPaywall({ api: makeApi(post) });
    await fireEvent.press(screen.getAllByText("Buy")[0]);
    await screen.findByText(/billed outside the App Store/);
    expect(mockRequestPurchase).not.toHaveBeenCalled();
  });

  it("tells a non-admin that only an admin can choose a plan, before StoreKit", async () => {
    const post = jest.fn().mockRejectedValue({ status: 403, body: { detail: "not_admin" } });
    await renderPaywall({ api: makeApi(post) });
    await fireEvent.press(screen.getAllByText("Buy")[0]);
    await screen.findByText("Only a workspace admin can choose a plan.");
    expect(mockRequestPurchase).not.toHaveBeenCalled();
  });

  it("restore links a matching purchase, then finishes, then reports success", async () => {
    const order: string[] = [];
    const purchase = { productId: WORKSPACE, purchaseToken: "jws-restore" };
    mockGetAvailablePurchases.mockResolvedValue([purchase]);
    const post = jest.fn(async (path: string) => {
      order.push(path);
      return { plan: "standard", status: "active" };
    });
    mockFinish.mockImplementation(async () => {
      order.push("finish");
    });
    const onPurchased = jest.fn(() => order.push("onPurchased"));
    await renderPaywall({ api: makeApi(post), onPurchased });
    await fireEvent.press(screen.getByText("Restore purchases"));
    await waitFor(() => expect(onPurchased).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith("/billing/apple/link", { signed_transaction: "jws-restore" });
    expect(mockFinish).toHaveBeenCalledWith({ purchase, isConsumable: false });
    expect(order).toEqual(["/billing/apple/link", "finish", "onPurchased"]);
  });

  it("restore refused because Stripe bills the workspace shows the refund copy and does not finish", async () => {
    mockGetAvailablePurchases.mockResolvedValue([{ productId: WORKSPACE, purchaseToken: "jws-restore" }]);
    const post = jest.fn().mockRejectedValue({ status: 409, body: { detail: "stripe_managed" } });
    const { onPurchased } = await renderPaywall({ api: makeApi(post) });
    await fireEvent.press(screen.getByText("Restore purchases"));
    await screen.findByText(
      "This workspace is already billed another way, so this App Store purchase wasn't applied. Contact support@thelightbulb.company for a refund.",
    );
    expect(mockFinish).not.toHaveBeenCalled();
    expect(onPurchased).not.toHaveBeenCalled();
  });

  it("cancelling the Apple sheet shows no error", async () => {
    await renderPaywall();
    await act(async () => mockHandlers.onPurchaseError?.({ code: "user-cancelled" }));
    expect(screen.queryByText(/didn't complete/)).toBeNull();
  });

  it("the current plan has no buy button", async () => {
    await renderPaywall({ currentProductId: BASE });
    expect(screen.getByText("Current plan")).toBeTruthy();
    expect(screen.getAllByText("Buy")).toHaveLength(2);
  });

  describe("pay on the web", () => {
    const ACCOUNT = { base_price_usd: 20, workspace_price_usd: 50, ultra_price_usd: 150 };
    const CHECKOUT = "https://checkout.stripe.com/c/pay/cs_test";
    let urlListener: ((e: { url: string }) => void) | undefined;
    let appStateListener: ((s: string) => void) | undefined;

    beforeEach(() => {
      urlListener = undefined;
      appStateListener = undefined;
      jest.spyOn(Linking, "openURL").mockResolvedValue(true);
      jest.spyOn(Linking, "addEventListener").mockImplementation(((_: string, fn: never) => {
        urlListener = fn;
        return { remove: jest.fn() };
      }) as never);
      jest.spyOn(AppState, "addEventListener").mockImplementation(((_: string, fn: never) => {
        appStateListener = fn;
        return { remove: jest.fn() };
      }) as never);
    });
    afterEach(() => jest.restoreAllMocks());

    const checkoutApi = () =>
      makeApi(
        jest.fn(async (path: string) =>
          path === "/billing/checkout" ? { checkout_url: CHECKOUT } : INTENT,
        ),
      );

    it("offers each plan's web price from the account", async () => {
      await renderPaywall({ account: ACCOUNT });
      expect(screen.getByText("Or pay $20/month on the web")).toBeTruthy();
      expect(screen.getByText("Or pay $50/month on the web")).toBeTruthy();
      expect(screen.getByText("Or pay $150/month on the web")).toBeTruthy();
    });

    it("an App Store-billed workspace gets no web option", async () => {
      await renderPaywall({ account: { ...ACCOUNT, subscription_source: "apple_iap" } });
      expect(screen.queryByText(/Or pay/)).toBeNull();
    });

    it("a web return uses onWebReturn when given, not onPurchased", async () => {
      const onWebReturn = jest.fn();
      const { onPurchased } = await renderPaywall({ account: ACCOUNT, onWebReturn });
      await act(async () => urlListener?.({ url: "https://thelightbulb.company/checkout_redirect/success" }));
      expect(onWebReturn).toHaveBeenCalledTimes(1);
      expect(onPurchased).not.toHaveBeenCalled();
    });

    it("a failed open does not refetch on the next foreground", async () => {
      (Linking.openURL as jest.Mock).mockRejectedValue(new Error("no browser"));
      const { onPurchased } = await renderPaywall({ api: checkoutApi(), account: ACCOUNT });
      await fireEvent.press(screen.getByText("Or pay $50/month on the web"));
      await screen.findByText("Couldn't open checkout. Try again in a moment.");
      await act(async () => appStateListener?.("active"));
      expect(onPurchased).not.toHaveBeenCalled();
    });

    it("opens Stripe Checkout in the browser, never StoreKit", async () => {
      const api = checkoutApi();
      await renderPaywall({ api, account: ACCOUNT });
      await fireEvent.press(screen.getByText("Or pay $50/month on the web"));
      await waitFor(() => expect(Linking.openURL).toHaveBeenCalledWith(CHECKOUT));
      expect(api.post).toHaveBeenCalledWith("/billing/checkout", { plan: "workspace", return_to: "app" });
      expect(mockRequestPurchase).not.toHaveBeenCalled();
    });

    it("a checkout already open or App Store billing says so", async () => {
      const post = jest.fn().mockRejectedValue({ status: 409, body: { detail: "checkout_in_progress" } });
      await renderPaywall({ api: makeApi(post), account: ACCOUNT });
      await fireEvent.press(screen.getByText("Or pay $20/month on the web"));
      await screen.findByText(
        "A checkout is already open, or this workspace is billed through the App Store.",
      );
      expect(Linking.openURL).not.toHaveBeenCalled();
    });

    it("any other checkout failure asks to try again", async () => {
      const post = jest.fn().mockRejectedValue(new Error("503"));
      await renderPaywall({ api: makeApi(post), account: ACCOUNT });
      await fireEvent.press(screen.getByText("Or pay $20/month on the web"));
      await screen.findByText("Couldn't open checkout. Try again in a moment.");
    });

    it("the success return link refetches; a cancel does not", async () => {
      const { onPurchased } = await renderPaywall({ account: ACCOUNT });
      await act(async () => urlListener?.({ url: "https://thelightbulb.company/checkout_redirect/cancel" }));
      expect(onPurchased).not.toHaveBeenCalled();
      await act(async () => urlListener?.({ url: "https://thelightbulb.company/checkout_redirect/success" }));
      expect(onPurchased).toHaveBeenCalledTimes(1);
    });

    it("returning to the app after opening a checkout refetches", async () => {
      const { onPurchased } = await renderPaywall({ api: checkoutApi(), account: ACCOUNT });
      await act(async () => appStateListener?.("active"));
      expect(onPurchased).not.toHaveBeenCalled(); // no checkout opened yet
      await fireEvent.press(screen.getByText("Or pay $50/month on the web"));
      await waitFor(() => expect(Linking.openURL).toHaveBeenCalled());
      await act(async () => appStateListener?.("active"));
      expect(onPurchased).toHaveBeenCalledTimes(1);
    });

    it("each row's App Store Buy comes before its web option", async () => {
      await renderPaywall({ account: ACCOUNT });
      const labels = screen
        .getAllByRole("button")
        .map((b) => (b.props.accessibilityLabel as string | undefined) ?? "")
        .filter(Boolean);
      expect(labels.slice(0, 6)).toEqual([
        "Buy Base",
        "Or pay $20/month on the web",
        "Buy Workspace",
        "Or pay $50/month on the web",
        "Buy Ultra",
        "Or pay $150/month on the web",
      ]);
    });

    it("the web option is not tappable while a purchase is busy", async () => {
      let resolveIntent: (v: unknown) => void = () => {};
      const post = jest.fn((path: string) =>
        path === "/billing/checkout"
          ? Promise.resolve({ checkout_url: CHECKOUT })
          : new Promise((r) => (resolveIntent = r)),
      );
      await renderPaywall({ api: makeApi(post), account: ACCOUNT });
      await fireEvent.press(screen.getAllByText("Buy")[0]);
      await fireEvent.press(screen.getByText("Or pay $50/month on the web"));
      await act(async () => resolveIntent(INTENT));
      expect(post).not.toHaveBeenCalledWith("/billing/checkout", expect.anything());
      expect(Linking.openURL).not.toHaveBeenCalled();
    });
  });
});
