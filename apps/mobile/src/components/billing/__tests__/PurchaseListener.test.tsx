import { act, render, waitFor } from "@testing-library/react-native";

// Same fake as PaywallScreen.test: the listener's contract is the ORDER of
// link and finish, and that nothing is finished without the server's word.
const mockFinish = jest.fn();
let mockHandlers: { onPurchaseSuccess?: (p: unknown) => void } = {};

jest.mock("expo-iap", () => ({
  useIAP: (handlers: typeof mockHandlers) => {
    mockHandlers = handlers;
    return { finishTransaction: mockFinish };
  },
}));

const mockPost = jest.fn();
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ post: mockPost }),
}));

let mockRole: string | undefined = "org:admin";
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
  useOrganization: () => ({ membership: mockRole ? { role: mockRole } : null }),
}));

import { IapProvider } from "../IapProvider";
import { PurchaseListener } from "../PurchaseListener";
import { clearInFlight, markInFlight } from "../../../lib/appleIap";

const BASE = "company.thelightbulb.mobile.base.monthly";
const purchase = { productId: BASE, purchaseToken: "jws", transactionId: "t1" };

async function deliver(p: unknown = purchase) {
  await render(
    <IapProvider>
      <PurchaseListener />
    </IapProvider>,
  );
  await act(async () => mockHandlers.onPurchaseSuccess?.(p));
}

describe("PurchaseListener", () => {
  let warn: jest.SpyInstance;
  beforeEach(() => {
    jest.clearAllMocks();
    mockRole = "org:admin";
    warn = jest.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(() => warn.mockRestore());

  it("links a delivered purchase, then finishes it", async () => {
    const order: string[] = [];
    mockPost.mockImplementation(async (path: string) => {
      order.push(path);
      return { plan: "base", status: "active" };
    });
    mockFinish.mockImplementation(async () => void order.push("finish"));
    await deliver();
    await waitFor(() => expect(mockFinish).toHaveBeenCalledWith({ purchase, isConsumable: false }));
    expect(mockPost).toHaveBeenCalledWith("/billing/apple/link", { signed_transaction: "jws" });
    expect(order).toEqual(["/billing/apple/link", "finish"]);
  });

  it("leaves the transaction unfinished when the link fails", async () => {
    mockPost.mockRejectedValue(new Error("503"));
    await deliver();
    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(mockFinish).not.toHaveBeenCalled();
  });

  it("ignores a product that isn't one of ours", async () => {
    await deliver({ productId: "com.other.thing", purchaseToken: "jws" });
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockFinish).not.toHaveBeenCalled();
  });

  it("skips a product the paywall is buying right now", async () => {
    markInFlight(BASE);
    try {
      await deliver();
    } finally {
      clearInFlight(BASE);
    }
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockFinish).not.toHaveBeenCalled();
  });

  it("does nothing for a non-admin member (the link would 403)", async () => {
    mockRole = "org:member";
    await deliver();
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockFinish).not.toHaveBeenCalled();
  });
});
