/**
 * ONE StoreKit connection for the whole signed-in app.
 *
 * expo-iap's `useIAP` calls the GLOBAL `endConnection()` when the component
 * using it unmounts. With PurchaseListener (always mounted) and PaywallScreen
 * (mounted while a paywall is on screen) each calling it, closing the paywall
 * tore down the listener's connection too, and redelivered purchases stopped
 * arriving for the rest of the session. So `useIAP` is called exactly once,
 * here, and every consumer shares its return value through `useSharedIAP`.
 *
 * `useIAP` takes the purchase callbacks as options, so the provider fans each
 * delivery out to every registered consumer; each one decides whether it is
 * theirs (the paywall only while buying, the listener otherwise).
 */
import { createContext, type ReactNode, useContext, useEffect, useRef } from "react";
import { useIAP } from "expo-iap";

type IapOptions = NonNullable<Parameters<typeof useIAP>[0]>;
type Handlers = Pick<IapOptions, "onPurchaseSuccess" | "onPurchaseError">;
type HandlerRef = { current: Handlers };
type Shared = { iap: ReturnType<typeof useIAP>; handlers: Set<HandlerRef> };

const IapContext = createContext<Shared | null>(null);

export function IapProvider({ children }: { children?: ReactNode }) {
  const handlers = useRef(new Set<HandlerRef>()).current;
  const iap = useIAP({
    onPurchaseSuccess: (purchase) => handlers.forEach((h) => h.current.onPurchaseSuccess?.(purchase)),
    onPurchaseError: (error) => handlers.forEach((h) => h.current.onPurchaseError?.(error)),
  });
  return <IapContext.Provider value={{ iap, handlers }}>{children}</IapContext.Provider>;
}

/** The shared `useIAP` value; `own` receives every purchase delivery while mounted. */
export function useSharedIAP(own: Handlers = {}): ReturnType<typeof useIAP> {
  const shared = useContext(IapContext);
  if (!shared) throw new Error("useSharedIAP must be used inside <IapProvider>");
  const ref = useRef(own);
  ref.current = own;
  const { handlers } = shared;
  useEffect(() => {
    handlers.add(ref);
    return () => void handlers.delete(ref);
  }, [handlers]);
  return shared.iap;
}
