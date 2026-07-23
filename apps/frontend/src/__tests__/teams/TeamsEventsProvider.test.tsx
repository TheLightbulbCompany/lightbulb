import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { useEffect } from "react";

// Module-level handlers — captured from the mocked useGateway so the
// test can drive synthetic events into the provider.
let eventHandler: ((event: string, data: unknown) => void) | null = null;
const sendMock = vi.fn();
const setIsConnected = vi.fn();

const gatewayState = { isConnected: false };

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: gatewayState.isConnected,
    send: sendMock,
    onEvent: (handler: (event: string, data: unknown) => void) => {
      eventHandler = handler;
      return () => {
        eventHandler = null;
      };
    },
  }),
}));

const mutateMock = vi.fn();
vi.mock("swr", () => ({
  useSWRConfig: () => ({ mutate: mutateMock }),
}));

// Mirrors the `gatewayState` mutable-ref pattern above: each test sets
// `userState.current` before rendering to control what `readFirstRun`
// sees, overriding the global `@clerk/nextjs` mock from tests/setup.ts
// (which carries no `unsafeMetadata.firstRun`).
const userState: { current: { unsafeMetadata?: Record<string, unknown> } | null } = {
  current: { unsafeMetadata: {} },
};
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({ user: userState.current }),
}));

const { captureMock } = vi.hoisted(() => ({ captureMock: vi.fn() }));
vi.mock("@/lib/analytics", () => ({ capture: captureMock }));

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
  // see IssueRow/KanbanCard/DashboardPanel/TasksList — so `entityId` from the
  // activity payload is the key shape that actually needs to match.
  "/teams/issues/iss_1",
  "/teams/issues/iss_1/comments",
  "/teams/issues/iss_1/interactions",
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
import { acquireTeamsEvents, releaseTeamsEvents, _resetTeamsEventsForTests } from "@/lib/teamsSubscription";

beforeEach(() => {
  sendMock.mockReset();
  mutateMock.mockReset();
  setIsConnected.mockReset();
  captureMock.mockReset();
  eventHandler = null;
  gatewayState.isConnected = false;
  userState.current = { unsafeMetadata: {} };
  _resetTeamsEventsForTests();
});

describe("TeamsEventsProvider", () => {
  it("sends teams.subscribe when connected", async () => {
    gatewayState.isConnected = true;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    expect(sendMock).toHaveBeenCalledWith({ type: "teams.subscribe" });
  });

  it("invalidates inbox + dashboard + activity + issues on teams.activity.logged", async () => {
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
      gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    const keys = invalidatedKeys();
    expect(keys).toEqual(expect.arrayContaining([
      "/teams/routines", "/teams/projects", "/teams/goals", "/teams/agents",
    ]));
  });

  it("captures first_run_board_pulse on teams.activity.logged during an active first-run show", () => {
    gatewayState.isConnected = true;
    userState.current = { unsafeMetadata: { firstRun: { started: true, completed: false } } };
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    act(() => {
      eventHandler!("teams.activity.logged", {});
    });
    expect(captureMock).toHaveBeenCalledWith("first_run_board_pulse");
  });

  it("does not capture first_run_board_pulse once the first-run show has completed", () => {
    gatewayState.isConnected = true;
    userState.current = { unsafeMetadata: { firstRun: { started: true, completed: true } } };
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    act(() => {
      eventHandler!("teams.activity.logged", {});
    });
    expect(captureMock).not.toHaveBeenCalled();
  });

  it("does not capture first_run_board_pulse for a user who never entered the first-run show", () => {
    gatewayState.isConnected = true;
    userState.current = { unsafeMetadata: {} };
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    act(() => {
      eventHandler!("teams.activity.logged", {});
    });
    expect(captureMock).not.toHaveBeenCalled();
  });

  it("does not capture first_run_board_pulse for unrelated teams events during an active show", () => {
    gatewayState.isConnected = true;
    userState.current = { unsafeMetadata: { firstRun: { started: true, completed: false } } };
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    act(() => {
      eventHandler!("teams.agent.status", {});
    });
    expect(captureMock).not.toHaveBeenCalled();
  });

  it("invalidates dashboard + agents on teams.agent.status", () => {
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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

  it("ignores teams.plugin.* events (no UI bound in v1)", () => {
    gatewayState.isConnected = true;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile
    act(() => {
      eventHandler!("teams.plugin.ui.updated", {});
      eventHandler!("teams.plugin.worker.crashed", {});
    });
    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("invalidates every distinct mapped key on teams.stream.resumed", () => {
    gatewayState.isConnected = true;
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
    ]));
  });

  it("reconciles (invalidates all mapped keys) on FIRST subscribe when already connected", () => {
    // The gateway connection persists across navigation while this provider
    // mounts/unmounts with the Teams layout. Mounting already-connected means
    // any activity.logged emitted while the user was outside Teams was missed
    // (Paperclip WS has no replay) and the read layer's 15s dedup blocks the
    // mount-refetch — so first-subscribe MUST reconcile, not just reconnect.
    gatewayState.isConnected = true;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    expect(sendMock).toHaveBeenCalledWith({ type: "teams.subscribe" });
    const keys = invalidatedKeys();
    expect(keys).toEqual(
      expect.arrayContaining([
        "/teams/dashboard",
        "/teams/activity",
        "/teams/inbox",
        "/teams/issues",
        "/teams/agents",
        // The reconcile MUST cover every key any event maps to — including the
        // NeedsAttentionBand sources — or a missed approval/failed-run stays
        // stale on Teams re-entry. Regression guard for the ALL_KEYS drift.
        "/teams/approvals",
        "/teams/inbox/runs",
      ]),
    );
  });

  it("re-subscribes and invalidates on isConnected false→true transition", () => {
    gatewayState.isConnected = false;
    const { rerender } = render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    expect(sendMock).not.toHaveBeenCalled();

    gatewayState.isConnected = true;
    rerender(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    expect(sendMock).toHaveBeenCalledWith({ type: "teams.subscribe" });
    // And ALL mapped keys are invalidated as a "no backfill" safety net.
    const keys = invalidatedKeys();
    expect(keys).toEqual(expect.arrayContaining(["/teams/dashboard", "/teams/inbox"]));
  });

  it("sends teams.unsubscribe on unmount", () => {
    gatewayState.isConnected = true;
    const { unmount } = render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    sendMock.mockClear();
    unmount();
    expect(sendMock).toHaveBeenCalledWith({ type: "teams.unsubscribe" });
  });

  it("does NOT unsubscribe on unmount while another consumer holds the subscription", () => {
    gatewayState.isConnected = true;
    // Simulate RunNotifications holding the shared subscription.
    acquireTeamsEvents();
    const { unmount } = render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    sendMock.mockClear();
    unmount();
    expect(sendMock).not.toHaveBeenCalledWith({ type: "teams.unsubscribe" });
    // Drop the simulated holder so no state escapes this test.
    releaseTeamsEvents(sendMock);
  });

  it("ignores non-teams events (e.g. an OpenClaw event leaking through)", () => {
    gatewayState.isConnected = true;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    mutateMock.mockClear(); // drop the first-subscribe reconcile
    act(() => {
      eventHandler!("agent_chat", {});
      eventHandler!("openclaw.something", {});
    });
    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("invalidates query-stringed inbox keys (#3c useInboxData fan-out) on inbox events", () => {
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    // useGatewayRpc path which just drops suppressed events: these keys have
    // no refreshInterval and revalidateOnFocus is off globally, so a dropped
    // final event would leave the board stale until the next Teams re-entry.
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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
    gatewayState.isConnected = true;
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

// Coverage-only export to suppress unused-import warning on useEffect.
const _u = useEffect;
void _u;
