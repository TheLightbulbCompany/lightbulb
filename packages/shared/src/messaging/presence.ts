const CONNECTION_FRESH_MS = 90_000;
const AVAILABLE_INPUT_MS = 300_000;

export type PresenceInventoryStatus =
  | "fresh_complete"
  | "fresh_incomplete"
  | "failed"
  | "stale";

export type PresenceConnectionKind =
  | "native_human"
  | "machine"
  | "operator"
  | "node";

export interface NormalizedPresenceConnection {
  /** Verified person identity supplied by the authenticated server adapter. */
  personId: string;
  /** Server-qualified connection kind; only native_human is public presence. */
  kind: PresenceConnectionKind;
  /** Last server heartbeat in the same clock domain as aggregate nowMs. */
  lastSeenAtMs: number;
  /** Server-normalized foreground visibility for this connection. */
  visibility: "visible" | "hidden";
  /** Last explicit human input; heartbeat and missing activity are null. */
  lastInputAtMs: number | null;
  /** True only for an authorized, foreground view of the current room. */
  viewsCurrentRoom: boolean;
}

export interface NormalizedPresenceEvidence {
  /** Fresh completeness is adapter-qualified; incomplete includes truncation. */
  inventoryStatus: PresenceInventoryStatus;
  /** Source inventory observation time, never projection time. */
  observedAtMs: number | null;
  /** Application-normalized rows; never pass an upstream payload directly. */
  connections: readonly NormalizedPresenceConnection[];
}

export type HumanPresenceState = "available" | "away" | "offline" | "unknown";

export interface HumanPresenceRow {
  personId: string;
  state: HumanPresenceState;
  inRoom: boolean;
  /** ISO source observation time, or null when the source supplied none/invalid. */
  observedAt: string | null;
}

function isPastTimestamp(timestampMs: number, nowMs: number): boolean {
  return (
    Number.isFinite(timestampMs) &&
    timestampMs <= nowMs &&
    !Number.isNaN(new Date(timestampMs).getTime())
  );
}

function isRecent(timestampMs: number, nowMs: number, maxAgeMs: number): boolean {
  return isPastTimestamp(timestampMs, nowMs) && nowMs - timestampMs < maxAgeMs;
}

/** Project server-qualified evidence into authorization-filtered public presence. */
export function aggregateHumanPresence(
  evidence: NormalizedPresenceEvidence,
  authorizedPersonIds: readonly string[],
  nowMs: number,
): HumanPresenceRow[] {
  const observedAt =
    evidence.observedAtMs !== null && isPastTimestamp(evidence.observedAtMs, nowMs)
      ? new Date(evidence.observedAtMs).toISOString()
      : null;
  const trustsRows =
    evidence.inventoryStatus === "fresh_complete" ||
    evidence.inventoryStatus === "fresh_incomplete";

  return [...new Set(authorizedPersonIds)].map((personId) => {
    let hasFreshConnection = false;
    let available = false;
    let inRoom = false;

    if (trustsRows) {
      for (const connection of evidence.connections) {
        if (
          connection.personId !== personId ||
          connection.kind !== "native_human" ||
          !isRecent(connection.lastSeenAtMs, nowMs, CONNECTION_FRESH_MS)
        ) {
          continue;
        }

        hasFreshConnection = true;
        if (connection.visibility !== "visible") continue;
        inRoom ||= connection.viewsCurrentRoom;
        available ||=
          connection.lastInputAtMs !== null &&
          isRecent(connection.lastInputAtMs, nowMs, AVAILABLE_INPUT_MS);
      }
    }

    const state: HumanPresenceState = hasFreshConnection
      ? available
        ? "available"
        : "away"
      : evidence.inventoryStatus === "fresh_complete"
        ? "offline"
        : "unknown";

    return { personId, state, inRoom, observedAt };
  });
}
