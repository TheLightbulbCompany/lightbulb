import { sessionLive, sessionState, sessionTitle } from "./localSessionLabels";
import type { LocalSession } from "./types";

/** One record of the network engine's `directory`, as `engine_ask` gives it to a page: named fields only. Times are seconds since the epoch. */
export interface EngineRecord {
  session_id: string;
  person: string;
  person_id: string;
  person_verified: boolean;
  computer: string;
  agent: string;
  folder: string;
  repo: string | null;
  branch: string | null;
  status: "busy" | "waiting" | "idle" | "ended";
  waiting_for?: string;
  doing: string;
  title: string | null;
  tier: string;
  online: boolean;
  seen_at?: number;
  wakes: boolean;
  user_id?: string;
  ended_at?: number;
  reason?: string;
  /** The engine holds a resume command; only native code reads it, since it names the folder's path. */
  can_resume?: boolean;
  handle?: string;
  hidden?: boolean;
  /** This computer's own session (the app adds it; the wire never carries it). */
  own?: boolean;
  job?: { id?: string; state?: string };
}

export type SessionState = "busy" | "waiting" | "idle" | "done" | "offline" | "parked" | "ended";

/** One session as a person sees it (spec 2026-10-30 §10.3). Times are milliseconds. */
export interface SessionRow {
  /** `networkSessionId` when known, else the backend `sessionId`. */
  id: string;
  /** The joined backend row; every action and detail reads it. */
  backend?: LocalSession;
  /** The joined directory record. */
  engine?: EngineRecord;
  /** `network`: no backend row, so no Open, Join, Resume or End. */
  source: "shared" | "network";
  person: { name: string; userId?: string; isViewer: boolean };
  computer?: string;
  agent: string;
  title: string;
  folder?: string;
  repo?: string;
  branch?: string;
  state: SessionState;
  waitingFor?: string;
  /** Offline or parked. */
  lastSeenAt?: number;
  endedAt?: number;
  endedReason?: string;
  /** The engine can resume it: the viewer's own ended rows only. */
  canResume?: boolean;
}

export interface SessionViewer {
  userId: string | null | undefined;
  /** A backend row's person by user id; a backend row carries no name. */
  nameOf?: (userId: string) => string;
}

const JOB_FINISHED = new Set(["done", "failed", "stopped"]);

// `ended` first: a session that ended stays ended while its computer is off.
function engineState(engine: EngineRecord): SessionState {
  if (engine.status === "ended") return "ended";
  if (!engine.online) return engine.wakes ? "parked" : "offline";
  if (engine.status === "idle" && JOB_FINISHED.has(engine.job?.state ?? "")) return "done";
  return engine.status;
}

function backendState(backend: LocalSession): SessionState {
  const state = sessionState(backend);
  if (state === "stopped") return "ended";
  if (state === "offline" || state === "waiting" || state === "done") return state;
  return backend.status === "busy" ? "busy" : "idle";
}

const millis = (iso: string | null | undefined) => {
  const at = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(at) ? undefined : at;
};

function toRow(backend: LocalSession | undefined, engine: EngineRecord | undefined, viewer: SessionViewer): SessionRow {
  const humanUserId = backend?.humanUserId;
  const userId = humanUserId ?? engine?.user_id;
  // Never undefined === undefined: a signed-out viewer and a record with no account are not one person.
  const isViewer = engine?.own === true || (viewer.userId != null && (backend ? backend.humanUserId === viewer.userId : engine?.user_id === viewer.userId));
  const engineSays = engine && engineState(engine);
  // `job` does not reach a page for a teammate's session; the backend row still knows it is done.
  const state = engineSays === "idle" && backend?.done ? "done" : engineSays ?? backendState(backend!);
  return {
    id: engine?.session_id ?? backend?.networkSessionId ?? backend!.sessionId,
    backend,
    engine,
    source: backend ? "shared" : "network",
    person: {
      name: engine?.person ?? viewer.nameOf?.(humanUserId!) ?? "Workspace member",
      ...(userId ? { userId } : {}),
      isViewer,
    },
    computer: engine?.computer || undefined,
    agent: engine?.agent || backend!.provider,
    // An owner's rename lives on the backend row, so it wins over the harness's title.
    title: sessionTitle({ displayTitle: backend?.displayTitle, title: engine?.title || backend?.title }),
    folder: engine?.folder || backend?.cwdName || undefined,
    repo: engine?.repo || undefined,
    branch: engine?.branch || undefined,
    state,
    waitingFor: state === "waiting" ? engine?.waiting_for || backend?.waitingFor : undefined,
    lastSeenAt: state === "offline" || state === "parked"
      ? (engine?.seen_at ? engine.seen_at * 1000 : millis(backend?.lastEventAt))
      : undefined,
    endedAt: state === "ended" ? (engine?.ended_at ? engine.ended_at * 1000 : millis(backend?.endedAt)) : undefined,
    endedReason: state === "ended" ? engine?.reason || backend?.endedReason || undefined : undefined,
    canResume: state === "ended" && isViewer && engine?.can_resume ? true : undefined,
  };
}

/**
 * The one list: every backend row, joined to its engine record by `networkSessionId`, then every
 * engine record with no backend row, each list in the order given, one row per conversation.
 * A conversation listed twice keeps its live row, else the first.
 */
export function mergeSessionRows(
  backendRows: readonly LocalSession[],
  engineRows: readonly EngineRecord[],
  viewer: SessionViewer,
): SessionRow[] {
  const engineById = new Map<string, EngineRecord>();
  for (const engine of engineRows) {
    const kept = engineById.get(engine.session_id);
    if (!kept || (kept.status === "ended" && engine.status !== "ended")) engineById.set(engine.session_id, engine);
  }
  const backendById = new Map<string, LocalSession>();
  for (const backend of backendRows) {
    const id = backend.networkSessionId ?? backend.sessionId;
    const kept = backendById.get(id);
    if (!kept || (!sessionLive(kept) && sessionLive(backend))) backendById.set(id, backend);
  }
  // ponytail: a Mac that has not updated sends no networkSessionId; delete once all have.
  const unjoined = [...backendById.values()].filter((b) => !b.networkSessionId);
  const twins = (e: EngineRecord) => !!e.title && unjoined.some((b) => b.title === e.title && b.cwdName === e.folder && b.provider === e.agent);
  return [
    ...[...backendById].map(([id, backend]) => toRow(backend, engineById.get(id), viewer)),
    ...[...engineById]
      .filter(([id, e]) => !backendById.has(id) && e.reason !== "moved-on" && !twins(e))
      .map(([, engine]) => toRow(undefined, engine, viewer)),
  ];
}
