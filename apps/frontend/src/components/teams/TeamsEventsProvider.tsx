"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSWRConfig, type Cache } from "swr";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useTeamsApi } from "@/hooks/useTeamsApi";

/**
 * Mounts inside the authenticated live-events provider. Paperclip events arrive
 * on the private user subscription and invalidate scoped SWR cache keys.
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
    // The unified "needs you" feed. Every mutation that can raise or resolve
    // a decision publishes `activity.logged`, so this is what keeps the tab's
    // badge honest without polling — a decision raised while the user sits on
    // another tab still lights the count.
    "/teams/attention",
  ],
  "teams.agent.status": ["/teams/dashboard", "/teams/agents"],
  "teams.heartbeat.run.queued": ["/teams/dashboard", "/teams/inbox", "/teams/inbox/runs"],
  "teams.heartbeat.run.status": ["/teams/dashboard", "/teams/inbox", "/teams/inbox/runs"],
  // Run-event/log only matter when run-detail is open. SWR mutate on a
  // path-prefix is not natively supported; the panels for those routes
  // can subscribe themselves later. For now, no global invalidation.
  "teams.heartbeat.run.event": [],
  "teams.heartbeat.run.log": [],
  // A GitHub PR / issue a task mentions changed state. Empty ON PURPOSE: these
  // values feed ALL_KEYS, which the board keeper revalidates every 20s, and the
  // external-object reads are not worth a poll. `mutateExternalObjects`
  // refreshes them instead: on the event itself, and on every recovery path
  // (provider entry, reconnect, stream.resumed) for an event missed meanwhile.
  "teams.external_object.updated": [],
};

// The reconcile set for the "refetch everything, we may have missed events"
// paths (first subscribe, reconnect, and teams.stream.resumed). Derived as the
// deduped union of every key any event can invalidate — NOT a second literal
// list — so the catch-all can never drift out of sync with EVENT_KEY_MAP and
// silently skip a live key. That drift is exactly what would strand
// /teams/approvals and /teams/inbox/runs (both feed Inbox's "Needs you" band —
// InboxNeedsYou, via useAttentionFeed and useInboxTriage) behind the read
// layer's 15s dedup window on Teams re-entry.
/**
 * `activity.logged` is Paperclip's ONLY signal that a specific row changed —
 * there are no `issue.*` / `routine.*` / `project.*` live-event types. Every
 * mutation publishes one generic `activity.logged` carrying the discriminator
 * in its payload (paperclip/server/src/services/activity-log.ts), and until
 * now nothing downstream read it, so the ROW-level caches never refreshed.
 *
 * Maps `entityType` → the SWR path prefix(es) its rows live under. The list
 * key IS the prefix; the detail key is `${prefix}/${entityId}`.
 *
 * Why `entityId` is enough: every one of these detail panels is opened with
 * the row's UUID — RoutinesPanel `r.id`, ProjectsListPanel `p.id`, GoalsPanel
 * `node.id`, FailedRunRow `run.id`. None of these row types even HAS a human
 * identifier field (only `Issue` does). So `entityId` interpolates straight
 * into the cache key with no lookup.
 *
 * Two upstream escape hatches can emit a non-UUID `entityId` — the plugin SDK
 * (`plugin-host-services.ts`, arbitrary entityType/entityId) and built-in
 * agents (`built-in-agents.ts`, slug fallback when the UUID is null). Both are
 * inert here: they produce a key that matches nothing in cache.
 */
const ACTIVITY_ENTITY_PREFIXES: Record<string, string[]> = {
  issue: ["/teams/issues"],
  routine: ["/teams/routines"],
  project: ["/teams/projects"],
  goal: ["/teams/goals"],
  // No per-agent detail key exists — AgentInspectorPanel finds its agent by
  // scanning the roster — so this refreshes the LIST only. Worth wiring
  // anyway: `teams.agent.status` (a run-status event) was the only thing that
  // refreshed the roster, so an agent.paused/renamed activity left it stale.
  agent: ["/teams/agents"],
  // RunDetailPanel reads the first with the second as a legacy fallback.
  heartbeat_run: ["/teams/heartbeat-runs", "/teams/runs"],
};

// The reconcile set must stay the union of every key ANY path can invalidate,
// entity prefixes included — otherwise a routine/project/goal changed while
// the user was outside Teams stays stale on re-entry, since the read layer's
// 15s dedup blocks the mount-refetch. Same drift trap the comment above
// EVENT_KEY_MAP warns about.
const ALL_KEYS = [
  ...new Set([
    ...Object.values(EVENT_KEY_MAP).flat(),
    ...Object.values(ACTIVITY_ENTITY_PREFIXES).flat(),
  ]),
];

// Cadence of the board keeper (see the keeper effect for the full rationale).
// 20s trades a small, deduped BFF cost for a hard ceiling on how long a
// throttle-killed board can sit stale with data already present.
const BOARD_KEEPER_INTERVAL_MS = 20_000;

/**
 * Event-driven revalidation is throttled per cache key, mirroring the gateway
 * RPC path (`useRuntimeRpc` in @lightbulb/shared, which throttles the same way and
 * for the same reason). The read layer gives these zero protection on its own:
 * `mutate(predicate)` explicitly deletes SWR's concurrent-request markers, so
 * useTeamsApi's 15s `dedupingInterval` never applies and EVERY event becomes a
 * real proxied, token-refreshed BFF round trip. One `activity.logged` fans out
 * to five prefixes, and a busy company emits them continuously.
 *
 * Leading edge fires immediately, so a single change still lands live. Further
 * hits on that key inside the window are coalesced into ONE trailing flush —
 * unlike the RPC path, which simply drops them. The trailing edge is required
 * here: these keys carry no `refreshInterval` and `revalidateOnFocus` is off
 * globally (SwrProvider), so a dropped LAST event of a burst would leave the
 * board stale until the next Teams re-entry reconcile.
 */
const REVALIDATE_THROTTLE_MS = 10_000;

type ThrottleState = {
  /** When each key last actually revalidated. */
  lastAt: Map<string, number>;
  /** Coalesced trailing flush for keys hit again inside their window. */
  pending: Map<string, () => void>;
  timer: ReturnType<typeof setTimeout> | undefined;
};

/**
 * Drain due entries from `pending`. A single shared timer serves all pending
 * keys, so it can fire before some of them are due; re-check each against its
 * OWN window and reschedule the stragglers rather than flushing them early.
 */
function flushPending(st: ThrottleState) {
  st.timer = undefined;
  const now = Date.now();
  let soonest = Infinity;
  for (const [key, run] of [...st.pending]) {
    const dueIn = REVALIDATE_THROTTLE_MS - (now - (st.lastAt.get(key) ?? 0));
    if (dueIn <= 0) {
      st.pending.delete(key);
      st.lastAt.set(key, now);
      run();
    } else {
      soonest = Math.min(soonest, dueIn);
    }
  }
  if (st.pending.size > 0 && soonest !== Infinity) {
    st.timer = setTimeout(() => flushPending(st), soonest);
  }
}

type ActivityPayload = {
  entityType?: unknown;
  entityId?: unknown;
  details?: { identifier?: unknown } | null;
};

/**
 * Resolve an activity payload to the concrete cache-key bases it should
 * invalidate: `{prefix}` (the list) and `{prefix}/{id}` (the detail subtree).
 *
 * For issues we also try `details.identifier` (e.g. "ADM-481") — belt and
 * braces for any identifier-keyed panel, NOT the mechanism: it is set at only
 * ~29 of the 68 issue activity sites, whereas `entityId` is always present.
 */
export function activityTargets(data: unknown): {
  lists: string[];
  details: string[];
} {
  const payload = (data ?? {}) as ActivityPayload;
  const entityType =
    typeof payload.entityType === "string" ? payload.entityType : "";
  const prefixes = ACTIVITY_ENTITY_PREFIXES[entityType];
  if (!prefixes) return { lists: [], details: [] };

  const ids = [
    ...new Set(
      [
        payload.entityId,
        entityType === "issue" ? payload.details?.identifier : undefined,
      ].filter((v): v is string => typeof v === "string" && v.length > 0),
    ),
  ];

  return {
    lists: prefixes,
    details: prefixes.flatMap((p) => ids.map((id) => `${p}/${id}`)),
  };
}

/** A `/teams` read still on its way. `_k` is the original key SWR keeps beside each entry (its own `mutate(filter)`
 *  matches on it). */
function teamsReadInFlight(cache: Cache): boolean {
  for (const serialized of cache.keys()) {
    const state = cache.get(serialized) as { isValidating?: boolean; _k?: unknown } | undefined;
    const key = state?._k;
    if (state?.isValidating && Array.isArray(key) && typeof key[0] === "string" && key[0].startsWith("/teams")) return true;
  }
  return false;
}

export function TeamsEventsProvider({ children }: { children: React.ReactNode }) {
  const { connectionRevision, onEvent } = useLiveEvents();
  const { mutate } = useTeamsApi();
  const { cache } = useSWRConfig();
  const reconciledRevisionRef = useRef<number | null>(null);
  const [settleCheck, setSettleCheck] = useState(0);

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

  // The per-issue runs lists (`/teams/issues/{id}/runs`, IssueRunLedger).
  // These sit one segment DEEPER than the `?`-bounded prefix matcher reaches,
  // and a run that dies inside the gateway (e.g. a compaction timeout surfaced
  // as openclaw_gateway_wait_error) writes no issue activity — so neither the
  // run-status keys nor the activity subtree ever refreshed them, and an open
  // Task panel kept showing "Running" after the run failed. Matched across all
  // issues rather than by payload id: `mutate(predicate)` only refetches keys
  // already in cache, so the fan-out is bounded by panels actually open.
  const mutateIssueRunLists = useCallback(() => {
    mutate(
      (key) =>
        typeof key === "string" &&
        key.startsWith("/teams/issues/") &&
        // `/live-runs` + `/active-run` feed the task chat's live tail.
        (key.endsWith("/runs") || key.endsWith("/live-runs") || key.endsWith("/active-run")),
    );
  }, [mutate]);

  // The external-object reads: each open task's rows
  // (`/teams/issues/{id}/external-objects`) and the task lists' rollups
  // (`/teams/issues/external-object-summaries?…`). The event names the object,
  // not the tasks that mention it, so every cached one refetches — bounded,
  // like the runs lists above, by what is actually in cache.
  const mutateExternalObjects = useCallback(() => {
    mutate(
      (key) =>
        typeof key === "string" &&
        key.startsWith("/teams/issues/") &&
        (key.endsWith("/external-objects") ||
          key.startsWith("/teams/issues/external-object-summaries?")),
    );
  }, [mutate]);

  // Per-key throttle state for the event path — see REVALIDATE_THROTTLE_MS.
  const throttleRef = useRef<ThrottleState>({
    lastAt: new Map(),
    pending: new Map(),
    timer: undefined,
  });

  const throttledMutate = useCallback((key: string, run: () => void) => {
    const st = throttleRef.current;
    const elapsed = Date.now() - (st.lastAt.get(key) ?? 0);
    if (elapsed >= REVALIDATE_THROTTLE_MS) {
      st.lastAt.set(key, Date.now());
      run();
      return;
    }
    st.pending.set(key, run);
    if (st.timer === undefined) {
      st.timer = setTimeout(
        () => flushPending(st),
        REVALIDATE_THROTTLE_MS - elapsed,
      );
    }
  }, []);

  useEffect(() => {
    const state = throttleRef.current;
    reconciledRevisionRef.current = null;
    return () => {
      if (state.timer !== undefined) clearTimeout(state.timer);
      state.timer = undefined;
      state.pending.clear();
      state.lastAt.clear();
    };
  }, [mutate]);

  // AppSync is ephemeral. Reconcile current scoped reads on provider entry and
  // after each private-user subscription reconnect. Not before the first ack (revision 0):
  // that is a cold load, every read is already on its first fetch, and `mutate(predicate)`
  // bypasses SWR's dedupe, so it only doubled each /teams read. The first ack reconciles.
  // Never over a read in flight: `mutate` starts a newer request and SWR drops the older one's answer, so the first
  // ack used to throw away every cold /teams read and make it wait for a second round trip (W14-4). Look again shortly.
  useEffect(() => {
    if (connectionRevision === 0) return;
    if (reconciledRevisionRef.current === connectionRevision) return;
    if (teamsReadInFlight(cache)) {
      const timer = setTimeout(() => setSettleCheck((n) => n + 1), 250);
      return () => clearTimeout(timer);
    }
    reconciledRevisionRef.current = connectionRevision;
    for (const key of ALL_KEYS) mutateKeyPrefix(key);
    mutateIssueRunLists();
    mutateExternalObjects();
  }, [connectionRevision, cache, settleCheck, mutateKeyPrefix, mutateIssueRunLists, mutateExternalObjects]);

  // Wire event listener.
  useEffect(() => {
    let active = true;
    const unsub = onEvent((liveEvent) => {
      if (!active || liveEvent.type !== "paperclip.event") return;
      const event = liveEvent.payload.eventName;
      const data = liveEvent.payload.payload;
      // Reconcile paths stay UNTHROTTLED (here and in the resubscribe effect):
      // they are the missed-event catch-up, they fire once per Teams entry /
      // stream resume rather than per mutation, and throttling them would
      // reopen exactly the staleness window they exist to close.
      if (event === "teams.stream.resumed") {
        for (const key of ALL_KEYS) mutateKeyPrefix(key);
        mutateIssueRunLists();
        mutateExternalObjects();
        return;
      }
      if (event === "teams.heartbeat.run.queued" || event === "teams.heartbeat.run.status") {
        // Not expressible in EVENT_KEY_MAP: its values are `?`-bounded
        // prefixes, and these are per-issue leaf keys.
        throttledMutate("/teams/issues/*/runs", mutateIssueRunLists);
      }
      if (event === "teams.external_object.updated") {
        throttledMutate("/teams/issues/*/external-objects", mutateExternalObjects);
      }
      if (event === "teams.activity.logged") {
        // Keep the OPEN detail panel live. Without this the unconditional
        // list keys below refresh but the row's own key never does, so an
        // agent changing a task/routine/project/goal (or another user editing
        // it) leaves the panel showing stale values until a hard reload —
        // `revalidateOnFocus` is off globally and none of these detail hooks
        // sets a refreshInterval.
        const targets = activityTargets(data);
        for (const key of targets.lists) {
          throttledMutate(key, () => mutateKeyPrefix(key));
        }
        for (const base of targets.details) {
          throttledMutate(base, () => mutateEntitySubtree(base));
        }
      }
      const keys = EVENT_KEY_MAP[event];
      if (!keys || keys.length === 0) return;
      for (const key of keys) {
        throttledMutate(key, () => mutateKeyPrefix(key));
      }
    });
    return () => {
      active = false;
      unsub();
    };
  }, [onEvent, mutateKeyPrefix, mutateEntitySubtree, mutateIssueRunLists, mutateExternalObjects, throttledMutate]);

  // ── Board keeper ──────────────────────────────────────────────────────────
  // A raw setInterval backstop that revalidates the board keys every 20s while
  // the tab is visible. It exists because the two mechanisms that are supposed
  // to keep a board fresh both have a hole a merely-occluded tab falls through:
  //
  //   • The panels' own SWR `refreshInterval` (IssuesPanel = 30s, etc.) is a
  //     SELF-RESCHEDULING setTimeout chain — each revalidation schedules the
  //     next only after it resolves. Chrome's occlusion throttling (a visible
  //     tab fully covered by another window) can drop a tick, and because the
  //     chain only schedules forward, ONE dropped tick ends it PERMANENTLY: the
  //     poll never fires again, even after the tab is uncovered.
  //   • `revalidateOnFocus` can't heal that tab either — it needs a focus
  //     TRANSITION (blur→focus) to fire on, and an occluded-but-never-blurred
  //     tab never gets one. (It's off globally in SwrProvider anyway; the panels
  //     that opt back in still depend on the transition that never comes.)
  //
  // Verified live on dev, twice today: 3 issues in the DB, GET /issues returning
  // them, the Tasks board stuck on "No tasks yet" for 60s+ — through a real
  // click into the page — because the panel's 30s chain was throttle-killed and
  // no focus transition ever came. A raw setInterval is NOT a self-rescheduling
  // chain (a dropped/delayed tick does not cancel the next one), so it survives
  // the throttle — the same reason ProvisioningStepper's poll watchdog uses one.
  //
  // Cost: reuses ALL_KEYS (the very reconcile set the resubscribe / stream-
  // resume paths already use), and `mutate(predicate)` only refetches keys
  // already in cache — so on a board view this is ~3 cheap BFF list calls per
  // 20s while visible: the insurance premium. useTeamsApi's 15s
  // `dedupingInterval` still collapses any overlap with a panel poll that IS
  // firing. Skipped entirely while hidden so backgrounded tabs don't spin.
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      for (const key of ALL_KEYS) mutateKeyPrefix(key);
      mutateIssueRunLists();
    }, BOARD_KEEPER_INTERVAL_MS);
    return () => clearInterval(id);
  }, [mutateKeyPrefix, mutateIssueRunLists]);

  return <>{children}</>;
}
