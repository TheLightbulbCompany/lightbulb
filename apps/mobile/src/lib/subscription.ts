import { useAuth } from "@clerk/clerk-expo";
import { createApiClient, type ListenMode } from "@lightbulb/shared";
import { useEffect, useRef, useState } from "react";

import { makeAdapter } from "./adapter";

/**
 * The workspace's `is_subscribed` from `GET /billing/account` — what the web
 * reads for Listening (`useBilling`). `undefined` until it lands or when the
 * read fails; every gate reads `=== true`, so unknown is never paid.
 */
export function useSubscribed(): boolean | undefined {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const [subscribed, setSubscribed] = useState<boolean | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    createApiClient(makeAdapter(() => getTokenRef.current()))
      .get("/billing/account")
      .then((body) => alive && setSubscribed((body as { is_subscribed?: boolean }).is_subscribed === true))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  return subscribed;
}

/** A newly added agent's mode: Listening on paid, Mentions otherwise (Listening is paid). */
export function defaultAgentListenMode(subscribed: boolean | undefined): ListenMode {
  return subscribed === true ? "active" : "mentions";
}
