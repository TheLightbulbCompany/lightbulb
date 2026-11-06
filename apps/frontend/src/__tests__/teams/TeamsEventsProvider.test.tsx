import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { LiveEvent } from "@lightbulb/shared";

// Module-level handler captured from the live-events provider so tests can
// drive canonical Paperclip envelopes into the invalidation layer.
let eventHandler: ((event: string, data: unknown) => void) | null = null;
const liveState = { connectionRevision: 0 };

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    connectionRevision: liveState.connectionRevision,
    onEvent: (handler: (event: LiveEvent) => void) => {
      eventHandler = (eventName, payload) => handler({
        schemaVersion: 1,
        eventId: `event-${eventName}`,
        type: "paperclip.event",
        occurredAt: "2026-09-09T00:00:00.000Z",
        payload: { eventName, payload },
      } as LiveEvent);
      return () => {
        eventHandler = null;
      };
    },
  }),
}));

const mutateMock = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ mutate: mutateMock }),
}));

/**
 * The provider switched from `mutate(stringKey)` to `mutate(predicate)` so
 * query-stringed cache keys (e.g. `/teams/inbox?tab=mine`) get invalidated
 * alongside the bare `/teams/inbox` key. Tests below assert the predicate
 * behavior — not the literal predicate function. We probe each captured
 * predicate against an extended fixture set covering both shapes:
 *   - bare keys: `/teams/inbox`
 *   - query-stringed keys: `/teams/inbox?tab=mine`
 * If ANY captured predicate accepts a candidate key, that key is treated as
 * invalidated. Helper below collapses N captured predicates into the set of
 * fixture keys they collectively match.
 */
const FIXTURE_KEYS = [
  "/teams/dashboard",
  "/teams/activity",
  "/teams/inbox",
  "/teams/inbox?tab=mine",
  "/teams/inbox?tab=recent",
  "/teams/inbox?tab=all",
  "/teams/inbox/runs", // distinct key (NOT matched by the `/teams/inbox` prefix)
  "/teams/issues",
  "/teams/issues?status=open",
  "/teams/approvals",
  "/teams/agents",
  "/teams/inbox-foo", // boundary check: must NOT match `/teams/inbox`
  // Issue-detail subtree. Production openers all pass `issue.id` (a UUID) —
  // see IssueRow/KanbanCard/TasksList — so `entityId` from the
  // activity payload is the key shape that actually needs to match.
  "/teams/issues/iss_1",
  "/teams/issues/iss_1/comments",
  "/teams/issues/iss_1/interactions",
  "/teams/issues/iss_1/runs", // IssueRunLedger — must refresh on run status events
  "/teams/issues/iss_1/external-objects", // the rail's GitHub PR / issue rows
  "/teams/issues/external-object-summaries?iss_1,iss_other", // the task list's rollups

  "/teams/issues/ADM-481", // identifier-keyed variant (details.identifier)
  "/teams/issues/iss_other", // a DIFFERENT issue: must NOT be invalidated
  // Sibling detail panels. Every production opener for these routes passes the
  // row's UUID (`r.id`/`p.id`/`node.id`/`run.id`) — none has a human
  // identifier field at all — so the activity payload's entityId interpolates
  // straight into the cache key.
  "/teams/routines",
  "/teams/routines/rt_1",
  "/teams/routines/rt_1/runs",
  "/teams/routines/rt_1/revisions",
  "/teams/routines/rt_other",
  "/teams/projects",
  "/teams/projects/pr_1",
  "/teams/goals",
  "/teams/goals/g_1",
  // Run detail reads a primary key plus a legacy fallback, and its
  // sub-resources carry query strings.
  "/teams/heartbeat-runs/run_1",
  "/teams/heartbeat-runs/run_1/events?limit=1000",
  "/teams/runs/run_1",
] as const;

type Predicate = (key: unknown) => boolean;

function invalidatedKeys(): string[] {
  const predicates = mutateMock.mock.calls.map((c) => c[0]) as Predicate[];
  const out = new Set<string>();
  for (const p of predicates) {
    if (typeof p !== "function") continue;
    for (const k of FIXTURE_KEYS) {
      if (p(k)) out.add(k);
    }
  }
  return [...out];
}

import { TeamsEventsProvider, EVENT_KEY_MAP } from "@/components/teams/TeamsEventsProvider";

beforeEach(() => {
  mutateMock.mockReset();
  eventHandler = null;
  liveState.connectionRevision = 0;
});

describe("TeamsEventsProvider", () => {
  it("invalidates inbox + dashboard + activity + issues on teams.activity.logged", async () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    expect(eventHandler).not.toBeNull();
    // Isolate the event's invalidation from the first-subscribe reconcile
    // (which invalidates ALL keys on mount-while-connected).
    mutateMock.mockClear();

    act(() => {
      eventHandler!("teams.activity.logged", { actor: "u1" });
    });

    const keys = invalidatedKeys();
    expect(keys).toEqual(expect.arrayContaining([
      "/teams/dashboard", "/teams/activity", "/teams/inbox", "/teams/issues",
    ]));
    // Query-stringed inbox keys (from useInboxData) also invalidate.
    expect(keys).toEqual(expect.arrayContaining([
      "/teams/inbox?tab=mine", "/teams/inbox?tab=recent", "/teams/inbox?tab=all",
    ]));
    // Boundary: similar-but-distinct keys must NOT match.
    expect(keys).not.toContain("/teams/inbox-foo");
    // Non-listed keys must not be invalidated either.
    expect(keys).not.toContain("/teams/agents");
  });

  it("invalidates the open issue's detail subtree on an issue activity event", () => {
    // The regression this guards: an agent (or another user) changing a task
    // left the OPEN task panel stale forever. `activity.logged` fires, but the
    // list-prefix predicate (`=== /teams/issues` or `startsWith /teams/issues?`)
    // never matches the detail key `/teams/issues/{id}`, and nothing else
    // rescues it — `revalidateOnFocus` is globally off (SwrProvider) and
    // useIssueDetail sets no refreshInterval.
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile

    act(() => {
      eventHandler!("teams.activity.logged", {
        action: "issue.updated",
        entityType: "issue",
        entityId: "iss_1",
        details: { identifier: "ADM-481", status: "todo" },
      });
    });

    const keys = invalidatedKeys();
    // The row itself AND its sub-resources — a comment added by an agent must
    // refresh `/comments`, not just the issue row.
    expect(keys).toEqual(expect.arrayContaining([
      "/teams/issues/iss_1",
      "/teams/issues/iss_1/comments",
      "/teams/issues/iss_1/interactions",
    ]));
    // details.identifier is only present on ~29 of 68 issue activity sites,
    // so it is belt-and-braces for identifier-keyed panels, not the primary.
    expect(keys).toContain("/teams/issues/ADM-481");
    // The list keys still invalidate as before.
    expect(keys).toEqual(expect.arrayContaining(["/teams/issues", "/teams/inbox"]));
    // Precision: a DIFFERENT issue's cached detail must be left alone.
    expect(keys).not.toContain("/teams/issues/iss_other");
  });

  it("does not touch issue-detail keys for non-issue activity events", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();

    act(() => {
      eventHandler!("teams.activity.logged", {
        action: "agent.updated",
        entityType: "agent",
        entityId: "iss_1", // same-looking id, different entityType
      });
    });

    const keys = invalidatedKeys();
    expect(keys).not.toContain("/teams/issues/iss_1");
    expect(keys).toContain("/teams/issues"); // list still refreshes
  });

  it("survives an activity payload with no entityId", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();

    expect(() => {
      act(() => {
        eventHandler!("teams.activity.logged", { entityType: "issue" });
      });
    }).not.toThrow();
    expect(invalidatedKeys()).toContain("/teams/issues");
  });

  it.each([
    ["routine", "rt_1", ["/teams/routines/rt_1", "/teams/routines/rt_1/runs", "/teams/routines/rt_1/revisions"], "/teams/routines"],
    ["project", "pr_1", ["/teams/projects/pr_1"], "/teams/projects"],
    ["goal", "g_1", ["/teams/goals/g_1"], "/teams/goals"],
  ])(
    "invalidates the open %s detail subtree + its list on an entity activity event",
    (entityType, entityId, expectedDetailKeys, listKey) => {
      liveState.connectionRevision = 1;
      render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
      mutateMock.mockClear(); // drop the first-subscribe reconcile

      act(() => {
        eventHandler!("teams.activity.logged", {
          action: `${entityType}.updated`,
          entityType,
          entityId,
        });
      });

      const keys = invalidatedKeys();
      expect(keys).toEqual(expect.arrayContaining(expectedDetailKeys));
      // The matching LIST key refreshes too — activity.logged previously
      // refreshed no routines/projects/goals key at all, so these lists went
      // stale on create/delete just like the detail panels did.
      expect(keys).toContain(listKey);
      // Entity-scoped: an unrelated entity type's list is untouched.
      expect(keys).not.toContain("/teams/agents");
    },
  );

  it("invalidates both the primary and legacy run keys on a heartbeat_run activity event", () => {
    // RunDetailPanel reads `/teams/heartbeat-runs/{id}` with a `/teams/runs/{id}`
    // legacy fallback, and its sub-resources carry query strings.
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();

    act(() => {
      eventHandler!("teams.activity.logged", {
        action: "heartbeat.cancelled",
        entityType: "heartbeat_run",
        entityId: "run_1",
      });
    });

    const keys = invalidatedKeys();
    expect(keys).toEqual(expect.arrayContaining([
      "/teams/heartbeat-runs/run_1",
      "/teams/heartbeat-runs/run_1/events?limit=1000",
      "/teams/runs/run_1",
    ]));
  });

  it("refreshes the agent roster on an agent activity event (no per-agent detail key exists)", () => {
    // AgentInspectorPanel has no `/teams/agents/{id}` key — it scans the
    // roster list. Only `teams.agent.status` refreshed that list before, so an
    // agent.updated/paused/renamed activity left the inspector stale.
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();

    act(() => {
      eventHandler!("teams.activity.logged", {
        action: "agent.paused",
        entityType: "agent",
        entityId: "ag_1",
      });
    });

    expect(invalidatedKeys()).toContain("/teams/agents");
  });

  it("ignores entity types with no mapped surface", () => {
    // Paperclip's plugin SDK can emit an arbitrary entityType/entityId
    // (plugin-host-services.ts), and built-in agents can pass a slug instead
    // of a UUID. Both must be inert, not throw and not over-invalidate.
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();

    act(() => {
      eventHandler!("teams.activity.logged", {
        entityType: "company_skill",
        entityId: "some-plugin-slug",
      });
    });

    const keys = invalidatedKeys();
    expect(keys).not.toContain("/teams/routines");
    expect(keys).not.toContain("/teams/projects");
    expect(keys).not.toContain("/teams/agents");
    expect(keys).toContain("/teams/inbox"); // unconditional list keys still fire
  });

  it("reconcile covers the entity list keys too (ALL_KEYS drift guard)", () => {
    // ALL_KEYS must stay the union of EVERY key any path can invalidate. If
    // the entity prefixes are omitted, a routine/project/goal changed while
    // the user was outside Teams stays stale on re-entry, because the read
    // layer's 15s dedup blocks the mount-refetch.
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    const keys = invalidatedKeys();
    expect(keys).toEqual(expect.arrayContaining([
      "/teams/routines", "/teams/projects", "/teams/goals", "/teams/agents",
    ]));
  });

  it("invalidates dashboard + agents on teams.agent.status", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile
    act(() => {
      eventHandler!("teams.agent.status", {});
    });
    const keys = invalidatedKeys();
    expect(keys).toEqual(expect.arrayContaining(["/teams/dashboard", "/teams/agents"]));
    expect(keys).not.toContain("/teams/inbox");
  });

  it("invalidates dashboard + inbox on teams.heartbeat.run.queued", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile
    act(() => {
      eventHandler!("teams.heartbeat.run.queued", {});
    });
    const keys = invalidatedKeys();
    expect(keys).toEqual(expect.arrayContaining([
      "/teams/dashboard", "/teams/inbox",
      "/teams/inbox?tab=mine", "/teams/inbox?tab=recent", "/teams/inbox?tab=all",
    ]));
  });

  it.each(["teams.heartbeat.run.queued", "teams.heartbeat.run.status"])(
    "invalidates open per-issue runs lists on %s",
    (event) => {
      // Regression: a run that dies inside the gateway writes no issue
      // activity, and `/teams/issues/{id}/runs` sits one segment deeper than
      // the `?`-bounded prefixes in EVENT_KEY_MAP — so an open Task panel
      // kept showing a terminated run as "Running" until remount. Run
      // lifecycle events must refresh cached per-issue runs lists directly.
      liveState.connectionRevision = 1;
      render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
      mutateMock.mockClear(); // drop the first-subscribe reconcile
      act(() => {
        eventHandler!(event, {});
      });
      const keys = invalidatedKeys();
      expect(keys).toContain("/teams/issues/iss_1/runs");
      // Scoped to the `/runs` leaf under issues: the issue row itself and
      // other entities' run lists are not this event's concern.
      expect(keys).not.toContain("/teams/issues/iss_1");
      expect(keys).not.toContain("/teams/routines/rt_1/runs");
    },
  );

  it("refetches the external-object reads on teams.stream.resumed (an update missed while the stream was down)", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile
    act(() => {
      eventHandler!("teams.stream.resumed", {});
    });
    expect(invalidatedKeys()).toEqual(expect.arrayContaining([
      "/teams/issues/external-object-summaries?iss_1,iss_other",
      "/teams/issues/iss_1/external-objects",
    ]));
  });

  it("refreshes the external-object reads on teams.external_object.updated", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();
    act(() => {
      eventHandler!("teams.external_object.updated", { objectId: "obj_1", liveness: "fresh" });
    });
    expect(invalidatedKeys().sort()).toEqual([
      "/teams/issues/external-object-summaries?iss_1,iss_other",
      "/teams/issues/iss_1/external-objects",
    ]);
  });

  it("ignores teams.plugin.* events (no UI bound in v1)", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile
    act(() => {
      eventHandler!("teams.plugin.ui.updated", {});
      eventHandler!("teams.plugin.worker.crashed", {});
    });
    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("invalidates every distinct mapped key on teams.stream.resumed", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile
    act(() => {
      eventHandler!("teams.stream.resumed", {});
    });
    const keys = invalidatedKeys();
    // Must include each unique key from the event map (bare + qs variants),
    // including the NeedsAttentionBand sources — stream.resumed reconciles
    // over the same derived ALL_KEYS as first-subscribe.
    expect(keys).toEqual(expect.arrayContaining([
      "/teams/dashboard", "/teams/activity", "/teams/inbox", "/teams/issues", "/teams/agents",
      "/teams/inbox?tab=mine", "/teams/inbox?tab=recent", "/teams/inbox?tab=all",
      "/teams/approvals", "/teams/inbox/runs",
      // Per-issue runs lists are outside ALL_KEYS' prefix matching and must
      // be reconciled explicitly (see mutateIssueRunLists).
      "/teams/issues/iss_1/runs",
    ]));
  });

  it("reconciles all mapped keys on provider entry", () => {
    // AppSync has no replay. Entering Teams must reconcile reads even if the
    // private user subscription was already live while this provider was absent.
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    const keys = invalidatedKeys();
    expect(keys).toEqual(
      expect.arrayContaining([
        "/teams/dashboard",
        "/teams/activity",
        "/teams/inbox",
        "/teams/issues",
        "/teams/agents",
        "/teams/approvals",
        "/teams/inbox/runs",
        "/teams/issues/iss_1/runs",
      ]),
    );
  });

  it("reconciles when the live connection revision advances", () => {
    liveState.connectionRevision = 0;
    const { rerender } = render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();

    liveState.connectionRevision = 1;
    rerender(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    const keys = invalidatedKeys();
    expect(keys).toEqual(expect.arrayContaining(["/teams/dashboard", "/teams/inbox"]));
  });

  it("waits for a /teams read in flight before reconciling over it (W14-4)", () => {
    vi.useFakeTimers();
    try {
      const cache = new Map<string, unknown>([["k", { isValidating: true, _k: ["/teams/agents", "user", "org"] }]]);
      liveState.connectionRevision = 1;
      render(<SWRConfig value={{ provider: () => cache as never }}><TeamsEventsProvider><div /></TeamsEventsProvider></SWRConfig>);
      expect(mutateMock).not.toHaveBeenCalled();
      cache.set("k", { isValidating: false, data: {}, _k: ["/teams/agents", "user", "org"] });
      act(() => { vi.advanceTimersByTime(300); });
      expect(invalidatedKeys()).toEqual(expect.arrayContaining(["/teams/agents", "/teams/inbox"]));
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores non-teams events (e.g. an OpenClaw event leaking through)", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile
    act(() => {
      eventHandler!("agent_chat", {});
      eventHandler!("openclaw.something", {});
    });
    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("invalidates query-stringed inbox keys (#3c useInboxData fan-out) on inbox events", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile

    // Drive the two events that are mapped to /teams/inbox.
    act(() => {
      eventHandler!("teams.activity.logged", {});
      eventHandler!("teams.heartbeat.run.queued", {});
    });

    // Each captured predicate must accept all three useInboxData SWR keys.
    // Predicates are pure — invoke them directly to assert the contract
    // without relying on the FIXTURE_KEYS probe set.
    const predicates = mutateMock.mock.calls.map(
      (c) => c[0],
    ) as Predicate[];
    const inboxQsKeys = [
      "/teams/inbox?tab=mine",
      "/teams/inbox?tab=recent",
      "/teams/inbox?tab=all",
    ];
    for (const qsKey of inboxQsKeys) {
      const matched = predicates.some((p) => typeof p === "function" && p(qsKey));
      expect(
        matched,
        `expected at least one captured mutate-predicate to match ${qsKey}`,
      ).toBe(true);
    }

    // Boundary: a predicate matching `/teams/inbox` must NOT match a sibling
    // path like `/teams/inbox-foo` (the `?` boundary in the prefix check).
    const inboxPredicateMatchedSibling = predicates.some(
      (p) => typeof p === "function" && p("/teams/inbox-foo"),
    );
    expect(inboxPredicateMatchedSibling).toBe(false);
  });
});

/**
 * Throttling of the EVENT path. `mutate(predicate)` deletes SWR's
 * concurrent-request markers, so useTeamsApi's 15s dedupingInterval never
 * applies and every event was a real BFF round trip — one activity.logged
 * fanning out to five prefixes, continuously, for a busy company.
 */
describe("TeamsEventsProvider revalidation throttle", () => {
  // Mirrors REVALIDATE_THROTTLE_MS in the provider. Written as a literal
  // rather than imported so a change to the window has to be made here too,
  // deliberately, instead of silently redefining what these tests assert.
  const REVALIDATE_WINDOW_MS = 10_000;

  /** How many captured predicates match `key` (i.e. revalidations of it). */
  function countMatching(key: string): number {
    return (mutateMock.mock.calls.map((c) => c[0]) as Predicate[]).filter(
      (p) => typeof p === "function" && p(key),
    ).length;
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("coalesces a burst on one key into a leading fire plus ONE trailing flush", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile

    act(() => {
      for (let i = 0; i < 10; i++) eventHandler!("teams.activity.logged", {});
    });

    // Leading edge only — the other nine are coalesced, not nine round trips.
    expect(countMatching("/teams/inbox")).toBe(1);

    act(() => {
      vi.advanceTimersByTime(REVALIDATE_WINDOW_MS);
    });

    // Trailing flush lands exactly once, carrying the whole burst.
    expect(countMatching("/teams/inbox")).toBe(2);
  });

  it("does not drop the LAST event of a burst", () => {
    // The reason this throttle has a trailing edge at all, unlike the sibling
    // useRuntimeRpc path which just drops suppressed events: these keys have
    // no refreshInterval and revalidateOnFocus is off globally, so a dropped
    // final event would leave the board stale until the next Teams re-entry.
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);

    const issueActivity = {
      action: "issue.updated",
      entityType: "issue",
      entityId: "iss_1",
    };

    act(() => {
      eventHandler!("teams.activity.logged", issueActivity);
    });
    mutateMock.mockClear(); // drop reconcile + the leading-edge fire

    act(() => {
      eventHandler!("teams.activity.logged", issueActivity);
    });
    // Suppressed for now...
    expect(countMatching("/teams/issues/iss_1")).toBe(0);

    act(() => {
      vi.advanceTimersByTime(REVALIDATE_WINDOW_MS);
    });
    // ...but it still lands.
    expect(countMatching("/teams/issues/iss_1")).toBe(1);
  });

  it("throttles per key — a busy key does not suppress a quiet one", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();

    act(() => {
      eventHandler!("teams.activity.logged", {}); // hits /teams/inbox
    });
    expect(countMatching("/teams/inbox")).toBe(1);

    act(() => {
      eventHandler!("teams.activity.logged", {}); // suppressed
      eventHandler!("teams.agent.status", {}); // different key, first hit
    });
    // /teams/agents was never fired before, so it goes through immediately
    // even though /teams/inbox is mid-window.
    expect(countMatching("/teams/agents")).toBe(1);
    expect(countMatching("/teams/inbox")).toBe(1);
  });

  it("lets a single isolated event through immediately (leading edge)", () => {
    // Throttling must not add latency to the common case: one change, one
    // live update. The board should not feel laggy.
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();

    act(() => {
      eventHandler!("teams.heartbeat.run.queued", {});
    });
    expect(countMatching("/teams/inbox/runs")).toBe(1);
  });

  it("leaves the reconcile paths unthrottled", () => {
    // stream.resumed is the missed-event catch-up, not a per-mutation signal.
    // Throttling it would reopen the staleness window it exists to close.
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear();

    act(() => {
      eventHandler!("teams.stream.resumed", {});
      eventHandler!("teams.stream.resumed", {});
    });

    expect(countMatching("/teams/inbox")).toBe(2);
  });
});

/**
 * Board keeper. A raw setInterval that revalidates the board keys every 20s
 * while the tab is visible — the central backstop for the failure mode where a
 * panel's self-rescheduling SWR `refreshInterval` chain is throttle-killed by
 * Chrome occlusion and `revalidateOnFocus` never gets its focus transition, so
 * the board sits stale with data already present. A raw interval survives the
 * throttle where the SWR chain does not (same reason ProvisioningStepper's
 * watchdog uses one).
 */
describe("TeamsEventsProvider board keeper", () => {
  // Mirrors BOARD_KEEPER_INTERVAL_MS in the provider — a local literal (like
  // REVALIDATE_WINDOW_MS above) so a change to the cadence has to be made here
  // too, deliberately, rather than silently redefining what these assert.
  const KEEPER_INTERVAL_MS = 20_000;

  // The keeper reads document.visibilityState on every tick. jsdom exposes it
  // as a prototype getter; we shadow it with an instance property so each test
  // controls it, then remove the override in afterEach.
  let visibility: DocumentVisibilityState = "visible";

  beforeEach(() => {
    vi.useFakeTimers();
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    delete (document as unknown as { visibilityState?: unknown }).visibilityState;
  });

  it("revalidates the board keys after 20s while the tab is visible", () => {
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile

    // Timer-driven, not mount-driven: nothing fires before the interval elapses.
    expect(mutateMock).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(KEEPER_INTERVAL_MS);
    });

    // The keeper reconciles the same board keys the provider already manages
    // (ALL_KEYS) — issues + goals + projects + inbox/dashboard among them.
    const keys = invalidatedKeys();
    expect(keys).toEqual(
      expect.arrayContaining([
        "/teams/issues",
        "/teams/goals",
        "/teams/projects",
        "/teams/inbox",
        "/teams/dashboard",
      ]),
    );
  });

  it("does NOT revalidate while the tab is hidden (background tabs must not spin)", () => {
    visibility = "hidden";
    liveState.connectionRevision = 1;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile

    act(() => {
      vi.advanceTimersByTime(KEEPER_INTERVAL_MS * 3);
    });

    expect(mutateMock).not.toHaveBeenCalled();
  });
});

describe("EVENT_KEY_MAP triage wiring", () => {
  it("activity.logged invalidates approvals", () => {
    expect(EVENT_KEY_MAP["teams.activity.logged"]).toContain("/teams/approvals");
  });
  it("heartbeat run events invalidate the failed-runs source", () => {
    expect(EVENT_KEY_MAP["teams.heartbeat.run.queued"]).toContain("/teams/inbox/runs");
    expect(EVENT_KEY_MAP["teams.heartbeat.run.status"]).toContain("/teams/inbox/runs");
  });
});
