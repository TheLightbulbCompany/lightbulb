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

// Each value below is a path PREFIX. `mutateKeyPrefix` invalidates any SWR
// cache key that exactly matches `${prefix}` OR starts with `${prefix}?` so
// query-stringed variants (e.g. `/teams/inbox?tab=mine`) get caught alongside
// the bare `/teams/inbox` key. The `?` boundary keeps `/teams/inbox-foo` from
// accidentally matching `/teams/inbox`.
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

// The reconcile set for the "refetch everything, we may have missed events"
// paths (first subscribe, reconnect, and teams.stream.resumed). Derived as the
// deduped union of every key any event can invalidate — NOT a second literal
// list — so the catch-all can never drift out of sync with EVENT_KEY_MAP and
// silently skip a live key. That drift is exactly what would strand
// /teams/approvals and /teams/inbox/runs (both feed NeedsAttentionBand via
// useInboxTriage) behind the read layer's 15s dedup window on Teams re-entry.
const ALL_KEYS = [...new Set(Object.values(EVENT_KEY_MAP).flat())];

/**
 * `activity.logged` is Paperclip's ONLY signal that a specific issue row
 * changed — there is no `issue.*` live-event type. Every issue mutation
 * (status, assignee, comment, reviewers, …) publishes one generic
 * `activity.logged` carrying the discriminator in its payload
 * (paperclip/server/src/services/activity-log.ts).
 *
 * `entityId` is the issue's UUID and is always present; `details.identifier`
 * (the human key, e.g. "ADM-481") is only set at ~29 of the 68 issue activity
 * sites, so it is a bonus match, not something to depend on. Every production
 * opener — IssueRow, KanbanCard, DashboardPanel, TasksList, ParentLink —
 * routes by `issue.id`, so the UUID is the key shape that matters.
 */
type ActivityPayload = {
  entityType?: unknown;
  entityId?: unknown;
  details?: { identifier?: unknown } | null;
};

export function issueIdsFromActivity(data: unknown): string[] {
  const payload = (data ?? {}) as ActivityPayload;
  if (payload.entityType !== "issue") return [];
  return [
    ...new Set(
      [payload.entityId, payload.details?.identifier].filter(
        (v): v is string => typeof v === "string" && v.length > 0,
      ),
    ),
  ];
}

export function TeamsEventsProvider({ children }: { children: React.ReactNode }) {
  const { isConnected, send, onEvent } = useGateway();
  const { mutate } = useSWRConfig();
  const { user } = useUser();
  // null = never observed; false = observed disconnected; true = observed connected.
  // Full invalidation fires on any transition INTO connected — both the first
  // subscribe (null→true) and a reconnect (false→true) — but never on a
  // redundant effect re-run while already connected (true→true). See the
  // resubscribe effect for why first-subscribe must reconcile too.
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

  // Invalidate ONE entity's detail key plus its sub-resource keys
  // (`/comments`, `/interactions`, …). `mutateKeyPrefix` above deliberately
  // stops at the `?` boundary so `/teams/inbox` can't match `/teams/inbox-foo`
  // — but that also means it never matches detail keys, which live one path
  // segment DEEPER (`/teams/issues/{id}`). Hence this second matcher, scoped
  // to a single id so other cached issue details are left alone.
  const mutateEntitySubtree = useCallback(
    (base: string) => {
      mutate(
        (key) =>
          typeof key === "string" &&
          (key === base ||
            key.startsWith(`${base}/`) ||
            key.startsWith(`${base}?`)),
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

  // (re)subscribe + full invalidation on every entry into a connected+subscribed
  // state (first subscribe AND reconnect).
  useEffect(() => {
    if (!isConnected) {
      wasConnectedRef.current = false;
      return;
    }
    resubscribeTeamsEvents(send);
    if (wasConnectedRef.current !== true) {
      // Reconcile whenever we (re)subscribe from a not-yet-subscribed state —
      // first subscribe (null) OR reconnect (false). Paperclip's WS has no
      // replay cursor, and the Teams read layer's 15s dedup window
      // (useTeamsApi) suppresses refetch-on-navigation by design, delegating
      // freshness to these event-driven mutations. That trust only holds while
      // continuously subscribed: any activity.logged emitted before this
      // subscription — e.g. while the user was outside Teams, since this
      // provider mounts/unmounts with the Teams layout while the gateway
      // connection persists — is lost, and the dedup blocks the mount-refetch
      // that would recover it. Refetching everything on (re)subscribe closes
      // that missed-event window. Fires once per Teams entry (the provider is
      // layout-scoped, not per-panel), so intra-Teams panel switches stay
      // deduped.
      for (const key of ALL_KEYS) mutateKeyPrefix(key);
    }
    wasConnectedRef.current = true;
  }, [isConnected, send, mutateKeyPrefix]);

  // Wire event listener.
  useEffect(() => {
    const unsub = onEvent((event, data) => {
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

        // Keep an OPEN task panel live. Without this the list keys below
        // refresh but the detail key never does, so an agent moving a task
        // (or another user editing it) leaves the panel showing stale
        // status/assignee until a hard reload — `revalidateOnFocus` is off
        // globally and useIssueDetail sets no refreshInterval.
        for (const id of issueIdsFromActivity(data)) {
          mutateEntitySubtree(`/teams/issues/${id}`);
        }
      }
      const keys = EVENT_KEY_MAP[event];
      if (!keys || keys.length === 0) return;
      for (const key of keys) mutateKeyPrefix(key);
    });
    return () => {
      unsub();
    };
  }, [onEvent, mutateKeyPrefix, mutateEntitySubtree, user]);

  return <>{children}</>;
}
