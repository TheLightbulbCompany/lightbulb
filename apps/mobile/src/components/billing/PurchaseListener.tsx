/**
 * Links StoreKit transactions nobody on screen is handling: an unfinished
 * purchase redelivered at launch (the app died between payment and link), a
 * renewal, an Ask to Buy approval. Same rule as the paywall — link on the
 * server first, finish only after it confirms; a failed link stays unfinished
 * so StoreKit delivers it again next launch.
 *
 * A product the paywall is buying or restoring right now is marked in flight
 * (lib/appleIap) and skipped here, so one purchase is never linked twice.
 * Non-admins skip everything: the link route is admin-only and would 403.
 */
import { useAuth, useOrganization } from "@clerk/expo";
import { createApiClient } from "@lightbulb/shared";
import { useMemo, useRef } from "react";

import { makeAdapter } from "../../lib/adapter";
import { isInFlight, linkApplePurchase, PLAN_ORDER } from "../../lib/appleIap";
import { useSharedIAP } from "./IapProvider";

export function PurchaseListener() {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);

  // Read at delivery time: Clerk's membership can resolve after the hook subscribes.
  const isAdmin = useOrganization().membership?.role === "org:admin";
  const isAdminRef = useRef(isAdmin);
  isAdminRef.current = isAdmin;

  // useIAP returns finishTransaction, so the handler reads it through a ref.
  const finishRef = useRef<((args: { purchase: never; isConsumable: boolean }) => Promise<unknown>) | null>(
    null,
  );

  const { finishTransaction } = useSharedIAP({
    onPurchaseSuccess: (purchase) => {
      const token = purchase.purchaseToken;
      if (!isAdminRef.current || !token || !Object.hasOwn(PLAN_ORDER, purchase.productId)) return;
      if (isInFlight(purchase.productId)) return;
      void (async () => {
        try {
          await linkApplePurchase(api.post, token);
          await finishRef.current?.({ purchase: purchase as never, isConsumable: false });
        } catch (e) {
          console.warn("[iap] link failed; StoreKit will redeliver", e);
        }
      })();
    },
  });
  finishRef.current = finishTransaction as typeof finishRef.current;
  return null;
}
