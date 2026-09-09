import { describe, expect, it } from "vitest";

import {
  aggregateHumanPresence,
  type NormalizedPresenceConnection,
  type NormalizedPresenceEvidence,
} from "./presence";

const NOW_MS = Date.parse("2026-09-09T12:00:00.000Z");
const SNAPSHOT_AT_MS = Date.parse("2026-09-09T11:59:59.000Z");

function connection(
  overrides: Partial<NormalizedPresenceConnection> = {},
): NormalizedPresenceConnection {
  return {
    personId: "alice",
    kind: "native_human",
    lastSeenAtMs: NOW_MS - 1_000,
    visibility: "visible",
    lastInputAtMs: NOW_MS - 1_000,
    viewsCurrentRoom: false,
    ...overrides,
  };
}

function evidence(
  connections: readonly NormalizedPresenceConnection[],
  overrides: Partial<NormalizedPresenceEvidence> = {},
): NormalizedPresenceEvidence {
  return {
    inventoryStatus: "fresh_complete",
    observedAtMs: SNAPSHOT_AT_MS,
    connections,
    ...overrides,
  };
}

describe("aggregateHumanPresence", () => {
  it.each([
    ["an 89.999-second heartbeat", NOW_MS - 89_999, NOW_MS - 299_999, "available"],
    ["a heartbeat at exactly 90 seconds", NOW_MS - 90_000, NOW_MS - 1_000, "offline"],
    ["input at 299.999 seconds", NOW_MS - 1_000, NOW_MS - 299_999, "available"],
    ["input at exactly five minutes", NOW_MS - 1_000, NOW_MS - 300_000, "away"],
  ] as const)("applies the liveness and activity boundary for %s", (_label, lastSeenAtMs, lastInputAtMs, state) => {
    expect(
      aggregateHumanPresence(
        evidence([connection({ lastSeenAtMs, lastInputAtMs })]),
        ["alice"],
        NOW_MS,
      ),
    ).toEqual([
      {
        personId: "alice",
        state,
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);
  });

  it.each([
    ["future", NOW_MS + 1],
    ["not-a-number", Number.NaN],
    ["infinite", Number.POSITIVE_INFINITY],
  ])("does not treat a %s server last-seen timestamp as complete absence proof", (_label, lastSeenAtMs) => {
    expect(
      aggregateHumanPresence(
        evidence([connection({ lastSeenAtMs })]),
        ["alice"],
        NOW_MS,
      ),
    ).toEqual([
      {
        personId: "alice",
        state: "unknown",
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);
  });

  it("lets valid positive evidence win over a malformed sibling connection", () => {
    expect(
      aggregateHumanPresence(
        evidence([
          connection({ lastSeenAtMs: Number.NaN }),
          connection({ lastSeenAtMs: NOW_MS - 1_000 }),
        ]),
        ["alice"],
        NOW_MS,
      ),
    ).toEqual([
      {
        personId: "alice",
        state: "available",
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);
  });

  it("keeps malformed timing scoped to its person", () => {
    expect(
      aggregateHumanPresence(
        evidence([connection({ personId: "alice", lastSeenAtMs: Number.NaN })]),
        ["alice", "bob"],
        NOW_MS,
      ),
    ).toEqual([
      {
        personId: "alice",
        state: "unknown",
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
      {
        personId: "bob",
        state: "offline",
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);
  });

  it.each([
    ["a hidden connection", { visibility: "hidden" as const }],
    ["missing activity", { lastInputAtMs: null }],
    ["future activity", { lastInputAtMs: NOW_MS + 1 }],
    ["invalid activity", { lastInputAtMs: Number.NaN }],
  ])("keeps %s away", (_label, overrides) => {
    expect(
      aggregateHumanPresence(evidence([connection(overrides)]), ["alice"], NOW_MS),
    ).toEqual([
      {
        personId: "alice",
        state: "away",
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);
  });

  it("lets an available tab win without an expired tab overriding it", () => {
    expect(
      aggregateHumanPresence(
        evidence([
          connection({ lastSeenAtMs: NOW_MS - 90_000 }),
          connection({ lastSeenAtMs: NOW_MS - 2_000, viewsCurrentRoom: true }),
        ]),
        ["alice"],
        NOW_MS,
      ),
    ).toEqual([
      {
        personId: "alice",
        state: "available",
        inRoom: true,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);
  });

  it("allows a visible inactive person to be away while still viewing the room", () => {
    expect(
      aggregateHumanPresence(
        evidence([
          connection({
            lastInputAtMs: NOW_MS - 300_000,
            viewsCurrentRoom: true,
          }),
        ]),
        ["alice"],
        NOW_MS,
      ),
    ).toEqual([
      {
        personId: "alice",
        state: "away",
        inRoom: true,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);
  });

  it("does not turn a visible background subscription into an in-room viewer", () => {
    expect(
      aggregateHumanPresence(
        evidence([connection({ viewsCurrentRoom: false })]),
        ["alice"],
        NOW_MS,
      )[0]?.inRoom,
    ).toBe(false);
  });

  it("uses explicit fresh evidence from an incomplete inventory and leaves other people unknown", () => {
    expect(
      aggregateHumanPresence(
        evidence([connection()], {
          inventoryStatus: "fresh_incomplete",
        }),
        ["bob", "alice", "bob"],
        NOW_MS,
      ),
    ).toEqual([
      {
        personId: "bob",
        state: "unknown",
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
      {
        personId: "alice",
        state: "available",
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);
  });

  it.each(["failed", "stale"] as const)(
    "does not trust positive rows from a %s inventory",
    (inventoryStatus) => {
      expect(
        aggregateHumanPresence(
          evidence([connection({ viewsCurrentRoom: true })], { inventoryStatus }),
          ["alice"],
          NOW_MS,
        ),
      ).toEqual([
        {
          personId: "alice",
          state: "unknown",
          inRoom: false,
          observedAt: "2026-09-09T11:59:59.000Z",
        },
      ]);
    },
  );

  it("uses only a fresh complete empty inventory to establish that the last tab is gone", () => {
    expect(aggregateHumanPresence(evidence([]), ["alice"], NOW_MS)).toEqual([
      {
        personId: "alice",
        state: "offline",
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);
  });

  it("excludes machine connections and people outside the current authorization list", () => {
    expect(
      aggregateHumanPresence(
        evidence([
          connection({ personId: "alice", kind: "machine" }),
          connection({ personId: "alice", kind: "operator" }),
          connection({ personId: "alice", kind: "node" }),
          connection({ personId: "revoked", viewsCurrentRoom: true }),
        ]),
        ["alice"],
        NOW_MS,
      ),
    ).toEqual([
      {
        personId: "alice",
        state: "offline",
        inRoom: false,
        observedAt: "2026-09-09T11:59:59.000Z",
      },
    ]);

    expect(
      aggregateHumanPresence(
        evidence([connection({ personId: "revoked" })]),
        [],
        NOW_MS,
      ),
    ).toEqual([]);
  });

  it("keeps source observation time across projection reruns and uses null when none exists", () => {
    const snapshot = evidence([connection()]);

    expect(aggregateHumanPresence(snapshot, ["alice"], NOW_MS)[0]?.observedAt).toBe(
      "2026-09-09T11:59:59.000Z",
    );
    expect(aggregateHumanPresence(snapshot, ["alice"], NOW_MS + 1_000)[0]?.observedAt).toBe(
      "2026-09-09T11:59:59.000Z",
    );
    expect(
      aggregateHumanPresence(
        evidence([], { observedAtMs: null }),
        ["alice"],
        NOW_MS,
      )[0]?.observedAt,
    ).toBeNull();
  });
});
