import { act, render } from "@testing-library/react-native";
import { useEffect } from "react";

// expo-iap's useIAP ends the GLOBAL StoreKit connection on unmount, so the
// contract is: one useIAP instance, owned by the provider, that a consumer
// unmounting (a paywall closing) never tears down.
const mockLifecycle = { mounts: 0, unmounts: 0 };
let mockHandlers: { onPurchaseSuccess?: (p: unknown) => void } = {};

jest.mock("expo-iap", () => ({
  useIAP: (handlers: typeof mockHandlers) => {
    mockHandlers = handlers;
    const { useEffect: useMockEffect } = jest.requireActual("react");
    useMockEffect(() => {
      mockLifecycle.mounts += 1;
      return () => void (mockLifecycle.unmounts += 1);
    }, []);
    return { connected: true };
  },
}));

import { IapProvider, useSharedIAP } from "../IapProvider";

function Consumer({ onPurchase }: { onPurchase: (p: unknown) => void }) {
  const iap = useSharedIAP({ onPurchaseSuccess: onPurchase });
  useEffect(() => expect(iap.connected).toBe(true), [iap]);
  return null;
}

it("shares one connection that outlives a consumer, and fans deliveries out", async () => {
  const listener = jest.fn();
  const paywall = jest.fn();
  const view = await render(
    <IapProvider>
      <Consumer onPurchase={listener} />
      <Consumer onPurchase={paywall} />
    </IapProvider>,
  );
  expect(mockLifecycle).toEqual({ mounts: 1, unmounts: 0 });

  await act(async () => mockHandlers.onPurchaseSuccess?.({ productId: "p" }));
  expect(listener).toHaveBeenCalledWith({ productId: "p" });
  expect(paywall).toHaveBeenCalledWith({ productId: "p" });

  // The paywall closes: the connection stays, and only it stops hearing deliveries.
  await view.rerender(
    <IapProvider>
      <Consumer onPurchase={listener} />
    </IapProvider>,
  );
  expect(mockLifecycle).toEqual({ mounts: 1, unmounts: 0 });
  await act(async () => mockHandlers.onPurchaseSuccess?.({ productId: "q" }));
  expect(listener).toHaveBeenLastCalledWith({ productId: "q" });
  expect(paywall).toHaveBeenCalledTimes(1);
});
