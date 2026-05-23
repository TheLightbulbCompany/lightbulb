"use client";

import { useCallback, useEffect, useRef } from "react";
import { useSWRConfig } from "swr";
import { useGateway } from "@/hooks/useGateway";

/**
 * Mounts inside TeamsLayout (which is inside GatewayProvider). Subscribes
 * to Paperclip live events forwarded by the BFF and invalidates SWR cache
 * keys per event type so panels rerender automatically.
 *
 * Pattern mirrors Paperclip's own LiveUpdatesProvider — a single mount
 * point owns all realtime invalidation; panels themselves stay free of
 * realtime concerns.
 */

// Each entry below is a path PREFIX. `mutateKeyPrefix` invalidates any SWR
// cache key that exactly matches `${prefix}` OR starts with `${prefix}?` so
// query-stringed variants (e.g. `/teams/inbox?tab=mine`) get caught alongside
// the bare `/teams/inbox` key. The `?` boundary keeps `/teams/inbox-foo` from
// accidentally matching `/teams/inbox`.
const ALL_KEYS = [
  "/teams/dashboard",
  "/teams/activity",
  "/teams/inbox",
  "/teams/issues",
  "/teams/agents",
];

const EVENT_KEY_MAP: Record<string, string[]> = {
  "teams.activity.logged": [
    "/teams/dashboard", "/teams/activity", "/teams/inbox", "/teams/issues",
  ],
  "teams.agent.status": ["/teams/dashboard", "/teams/agents"],
  "teams.heartbeat.run.queued": ["/teams/dashboard", "/teams/inbox"],
  "teams.heartbeat.run.status": ["/teams/dashboard", "/teams/inbox"],
  // Run-event/log only matter when run-detail is open. SWR mutate on a
  // path-prefix is not natively supported; the panels for those routes
  // can subscribe themselves later. For now, no global invalidation.
  "teams.heartbeat.run.event": [],
  "teams.heartbeat.run.log": [],
};

export function TeamsEventsProvider({ children }: { children: React.ReactNode }) {
  const { isConnected, send, onEvent } = useGateway();
  const { mutate } = useSWRConfig();
  // null = never observed; false = observed disconnected; true = observed connected.
  // Full invalidation only fires on a tracked false→true transition (true reconnect),
  // NOT on the first mount when we were already connected.
  const wasConnectedRef = useRef<boolean | null>(null);

  // Invalidate every SWR key that matches `prefix` exactly OR starts with
  // `${prefix}?`. SWR's `mutate` accepts a predicate; this is the documented
  // path-prefix invalidation pattern. We use it (instead of plain
  // `mutate(prefix)`) so panels that compose query strings into their key
  // — e.g. the Inbox page's `/teams/inbox?tab=mine|recent|all` keys from
  // `useInboxData` — get refetched on the same events that already drove
  // the bare `/teams/inbox` key.
  const mutateKeyPrefix = useCallback(
    (prefix: string) => {
      mutate(
        (key) =>
          typeof key === "string" &&
          (key === prefix || key.startsWith(`${prefix}?`)),
      );
    },
    [mutate],
  );

  // (re)subscribe + full invalidation on connect / reconnect.
  useEffect(() => {
    if (!isConnected) {
      wasConnectedRef.current = false;
      return;
    }
    send({ type: "teams.subscribe" });
    if (wasConnectedRef.current === false) {
      // Reconnect path: refetch everything because Paperclip's WS has no
      // replay cursor and we may have missed events while disconnected.
      for (const key of ALL_KEYS) mutateKeyPrefix(key);
    }
    wasConnectedRef.current = true;
  }, [isConnected, send, mutateKeyPrefix]);

  // Wire event listener.
  useEffect(() => {
    const unsub = onEvent((event, _data) => {
      if (!event.startsWith("teams.")) return;
      if (event === "teams.stream.resumed") {
        for (const key of ALL_KEYS) mutateKeyPrefix(key);
        return;
      }
      const keys = EVENT_KEY_MAP[event];
      if (!keys || keys.length === 0) return;
      for (const key of keys) mutateKeyPrefix(key);
    });
    return () => {
      unsub();
    };
  }, [onEvent, mutateKeyPrefix]);

  // Best-effort unsubscribe on unmount.
  useEffect(() => {
    return () => {
      send({ type: "teams.unsubscribe" });
    };
  }, [send]);

  return <>{children}</>;
}
