"use client";

import { useCallback, useEffect, useRef } from "react";
import { useSWRConfig } from "swr";
import { useUser } from "@clerk/nextjs";
import { useGateway } from "@/hooks/useGateway";
import { capture } from "@/lib/analytics";
import { readFirstRun } from "@/lib/firstRun";
import {
  acquireTeamsEvents,
  releaseTeamsEvents,
  resubscribeTeamsEvents,
} from "@/lib/teamsSubscription";

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

export const EVENT_KEY_MAP: Record<string, string[]> = {
  "teams.activity.logged": [
    "/teams/dashboard", "/teams/activity", "/teams/inbox", "/teams/issues",
    "/teams/approvals",
  ],
  "teams.agent.status": ["/teams/dashboard", "/teams/agents"],
  "teams.heartbeat.run.queued": ["/teams/dashboard", "/teams/inbox", "/teams/inbox/runs"],
  "teams.heartbeat.run.status": ["/teams/dashboard", "/teams/inbox", "/teams/inbox/runs"],
  // Run-event/log only matter when run-detail is open. SWR mutate on a
  // path-prefix is not natively supported; the panels for those routes
  // can subscribe themselves later. For now, no global invalidation.
  "teams.heartbeat.run.event": [],
  "teams.heartbeat.run.log": [],
};

export function TeamsEventsProvider({ children }: { children: React.ReactNode }) {
  const { isConnected, send, onEvent } = useGateway();
  const { mutate } = useSWRConfig();
  const { user } = useUser();
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

  // Refcounted hold on the shared per-connection subscription: another
  // consumer (RunNotifications) may still need teams events after this
  // panel-scoped provider unmounts. Declared BEFORE the resubscribe effect
  // per the teamsSubscription ordering contract.
  useEffect(() => {
    acquireTeamsEvents();
    return () => releaseTeamsEvents(send);
  }, [send]);

  // (re)subscribe + full invalidation on connect / reconnect.
  useEffect(() => {
    if (!isConnected) {
      wasConnectedRef.current = false;
      return;
    }
    resubscribeTeamsEvents(send);
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
      if (event === "teams.activity.logged") {
        // Prod telemetry proxy for "a board pulse reached this client"
        // during an active first-run show (spec §4.G gate 1). True e2e
        // latency needs a BFF-side emit timestamp, which activity.logged
        // payloads don't carry — a follow-up if pulse counts look wrong.
        const { started, completed } = readFirstRun(user);
        if (started && !completed) capture("first_run_board_pulse");
      }
      const keys = EVENT_KEY_MAP[event];
      if (!keys || keys.length === 0) return;
      for (const key of keys) mutateKeyPrefix(key);
    });
    return () => {
      unsub();
    };
  }, [onEvent, mutateKeyPrefix, user]);

  return <>{children}</>;
}
