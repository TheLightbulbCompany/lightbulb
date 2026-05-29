import { describe, it, expect, vi, beforeEach } from "vitest";
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
  "/teams/issues",
  "/teams/issues?status=open",
  "/teams/agents",
  "/teams/inbox-foo", // boundary check: must NOT match `/teams/inbox`
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
  sendMock.mockReset();
  mutateMock.mockReset();
  setIsConnected.mockReset();
  eventHandler = null;
  gatewayState.isConnected = false;
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

  it("invalidates dashboard + agents on teams.agent.status", () => {
    gatewayState.isConnected = true;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
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
    act(() => {
      eventHandler!("teams.plugin.ui.updated", {});
      eventHandler!("teams.plugin.worker.crashed", {});
    });
    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("invalidates every distinct mapped key on teams.stream.resumed", () => {
    gatewayState.isConnected = true;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    act(() => {
      eventHandler!("teams.stream.resumed", {});
    });
    const keys = invalidatedKeys();
    // Must include each unique key from the event map (bare + qs variants).
    expect(keys).toEqual(expect.arrayContaining([
      "/teams/dashboard", "/teams/activity", "/teams/inbox", "/teams/issues", "/teams/agents",
      "/teams/inbox?tab=mine", "/teams/inbox?tab=recent", "/teams/inbox?tab=all",
    ]));
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

  it("ignores non-teams events (e.g. an OpenClaw event leaking through)", () => {
    gatewayState.isConnected = true;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    act(() => {
      eventHandler!("agent_chat", {});
      eventHandler!("openclaw.something", {});
    });
    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("invalidates query-stringed inbox keys (#3c useInboxData fan-out) on inbox events", () => {
    gatewayState.isConnected = true;
    render(<TeamsEventsProvider><div /></TeamsEventsProvider>);

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
