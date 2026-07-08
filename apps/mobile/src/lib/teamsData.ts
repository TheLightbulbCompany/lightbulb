/**
 * Teams (Paperclip BFF) data layer — REST, bearer-only.
 *
 * Per the W4 contract (`.superpowers/sdd/w4-teams-contract.md` §1) EVERY
 * `/teams/*` endpoint works BEARER-ONLY: the BFF self-mints the Paperclip
 * session cookie server-side from the Clerk identity on every cold request,
 * so mobile never plumbs a cookie. All calls ride the same getToken bearer
 * as the rest of the app.
 *
 * Readiness gate (contract §1): the `_ctx` dependency in front of every
 * endpoint returns a NON-DATA status when the workspace isn't ready —
 *   • 202  → workspace provisioning (poll until active)
 *   • 402  → blocked gate (subscribe / finish setup on isol8.co)
 *   • 503  → provisioning failed / unavailable
 * These are workspace-STATUS, not data errors: the UI renders a readiness
 * state, never an error card. 202 is a *success* HTTP status, so it arrives
 * through `fetchJsonWithStatus`'s success return ({status, body}); 402/503
 * are non-2xx and throw an `ApiError` with `.status`. Both paths are mapped
 * to a single `TeamsGate` below.
 *
 * getTokenRef pattern (settings.tsx / overview.tsx lesson, MANDATORY): Clerk
 * hands out a NEW `getToken` reference every render. Pinning it in a ref keeps
 * the adapter + api client + `load` referentially stable so `useFocusEffect`
 * fires exactly once per focus instead of looping
 * load → setState → new getToken → reload forever. Do NOT put `[getToken]` in
 * any dependency array.
 */
import { useAuth } from "@clerk/clerk-expo";
import { useFocusEffect } from "expo-router";
import { useCallback, useMemo, useRef, useState } from "react";
import { Alert } from "react-native";
import {
  ApiError,
  createApiClient,
  fetchJsonWithStatus,
  type PlatformAdapter,
} from "@isol8/shared";

import { colors } from "../config/theme";
import { makeAdapter } from "./adapter";

// ---------------------------------------------------------------------------
// Contract shapes (w4 contract §2). Kept to the fields the mobile surfaces
// actually render — upstream forwards much richer rows verbatim.
// ---------------------------------------------------------------------------

/** A row from `GET /teams/inbox` (`{items:[…]}`) — a Paperclip issue. */
export interface InboxItem {
  id: string;
  identifier?: string | null;
  title: string;
  status?: string | null;
  priority?: string | null;
  updatedAt?: string | null;
  lastActivityAt?: string | null;
  createdAt?: string | null;
  assigneeAgentId?: string | null;
  assigneeUserId?: string | null;
  unread?: boolean | null;
}

/** Single issue from `GET /teams/issues/{id}` (bare upstream dict). */
export interface IssueDetail {
  id: string;
  identifier?: string | null;
  title: string;
  description?: string | null;
  status?: string | null;
  priority?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  lastActivityAt?: string | null;
  assigneeAgentId?: string | null;
  assigneeUserId?: string | null;
  createdByUserId?: string | null;
}

/** A comment from `GET /teams/issues/{id}/comments`. */
export interface IssueComment {
  id: string;
  body: string;
  createdAt: string;
  authorUserId?: string | null;
  authorAgentId?: string | null;
  authorKind?: "user" | "agent" | null;
}

/** A sub-issue from `GET /teams/issues/{id}/children` (`{issues:[…]}`). */
export interface ChildIssue {
  id: string;
  identifier?: string | null;
  title: string;
  status?: string | null;
}

/** An agent from `GET /teams/agents` (`{agents:[{…,seeded}]}`, adapter redacted). */
export interface TeamsAgent {
  id: string;
  name?: string | null;
  seeded?: boolean;
}

/** A per-label color chip descriptor (`types.ts:41-45` `IssueLabel`). */
export interface IssueLabel {
  id: string;
  name: string;
  /** Server-supplied per-label hex (`#rrggbb`); null → neutral fallback. */
  color?: string | null;
}

/**
 * A board row from `GET /teams/issues` (`{issues:[…]}`). Superset of the fields
 * the differentiated board card renders — status/priority/labels/assignee are
 * the differentiation axes (contract §3). `parentId` powers the client-side
 * subtask count (cheap: derived from the same list, no per-issue fetch).
 */
export interface BoardIssue {
  id: string;
  identifier?: string | null;
  title: string;
  status?: string | null;
  priority?: string | null;
  labels?: IssueLabel[] | null;
  assigneeAgentId?: string | null;
  assigneeUserId?: string | null;
  parentId?: string | null;
  updatedAt?: string | null;
  lastActivityAt?: string | null;
}

// ---------------------------------------------------------------------------
// Gate classification
// ---------------------------------------------------------------------------

/** Workspace readiness, derived from a `_ctx`-gated response (contract §1). */
export type TeamsGate =
  | "provisioning"
  | "needs_subscription"
  | "unavailable";

/** The 202 body FastAPI raises from `_ctx` when lazy-provisioning. */
const PROVISIONING_DETAIL = "team workspace provisioning";

/**
 * Inspect a 2xx `{status, body}` for the provisioning envelope. Returns
 * `"provisioning"` when the BFF answered 202 (or, defensively, a 200 carrying
 * the provisioning detail — mirrors the web read layer), else null (real data).
 */
function gateFromSuccess(status: number, body: unknown): TeamsGate | null {
  if (status === 202) return "provisioning";
  if (
    body &&
    typeof body === "object" &&
    (body as { detail?: unknown }).detail === PROVISIONING_DETAIL
  ) {
    return "provisioning";
  }
  return null;
}

/**
 * Map an `ApiError` from a `_ctx`-gated call onto a `TeamsGate`, or null when
 * it is a genuine data error (404, etc.) the caller should surface as an error.
 */
function gateFromError(err: unknown): TeamsGate | null {
  if (!(err instanceof ApiError)) return "unavailable";
  if (err.status === 202) return "provisioning"; // defensive; 202 rarely throws
  if (err.status === 402) return "needs_subscription";
  if (err.status === 503) return "unavailable";
  return null;
}

/** Build a stable ApiClientConfig around a ref-pinned getToken. */
function useTeamsConfig(): PlatformAdapter {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  return useMemo(() => makeAdapter(() => getTokenRef.current()), []);
}

// ---------------------------------------------------------------------------
// Inbox hook — the Teams index surface. One gate-bearing call (`/teams/inbox`)
// doubles as the workspace-readiness probe AND the data source.
// ---------------------------------------------------------------------------

export type InboxState =
  | { kind: "loading" }
  | { kind: "gate"; gate: TeamsGate; message: string | null }
  | { kind: "ready"; items: InboxItem[] };

function extractItems(body: unknown): InboxItem[] {
  const items = (body as { items?: unknown } | null)?.items;
  return Array.isArray(items) ? (items as InboxItem[]) : [];
}

export function useTeamsInbox() {
  const config = useTeamsConfig();
  const [state, setState] = useState<InboxState>({ kind: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const loadSeq = useRef(0);

  const load = useCallback(
    async (isRefresh = false) => {
      const seq = loadSeq.current + 1;
      loadSeq.current = seq;
      if (isRefresh) setRefreshing(true);
      else setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
      try {
        const { status, body } = await fetchJsonWithStatus(
          config,
          "/teams/inbox?tab=mine",
        );
        if (loadSeq.current !== seq) return;
        const gate = gateFromSuccess(status, body);
        if (gate) {
          setState({ kind: "gate", gate, message: null });
        } else {
          setState({ kind: "ready", items: extractItems(body) });
        }
      } catch (err) {
        if (loadSeq.current !== seq) return;
        const gate = gateFromError(err);
        setState({
          kind: "gate",
          gate: gate ?? "unavailable",
          message: err instanceof Error ? err.message : "Request failed",
        });
      } finally {
        if (loadSeq.current === seq && isRefresh) setRefreshing(false);
      }
    },
    [config],
  );

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  return {
    state,
    refreshing,
    retry: () => load(false),
    refresh: () => load(true),
  };
}

// ---------------------------------------------------------------------------
// Issue detail hook — issue + comments + agents (name resolution) in one load,
// plus a best-effort children summary. Comments/children/agents are best-effort:
// their failure never fails the issue view.
// ---------------------------------------------------------------------------

export type IssueDetailState =
  | { kind: "loading" }
  | { kind: "gate"; gate: TeamsGate; message: string | null }
  | { kind: "error"; message: string }
  | {
      kind: "ready";
      issue: IssueDetail;
      comments: IssueComment[];
      children: ChildIssue[];
      agents: TeamsAgent[];
    };

export function useTeamsIssue(issueId: string | undefined) {
  const config = useTeamsConfig();
  const [state, setState] = useState<IssueDetailState>({ kind: "loading" });
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    if (!issueId) {
      setState({ kind: "error", message: "Missing issue id" });
      return;
    }
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    try {
      // Primary call carries the readiness gate; if it 402/503/202s the whole
      // view degrades to the readiness state.
      const { status, body } = await fetchJsonWithStatus(
        config,
        `/teams/issues/${encodeURIComponent(issueId)}`,
      );
      if (loadSeq.current !== seq) return;
      const gate = gateFromSuccess(status, body);
      if (gate) {
        setState({ kind: "gate", gate, message: null });
        return;
      }
      const issue = body as IssueDetail;

      // Secondary reads are best-effort: a comments/children/agents failure
      // must not blank out an issue that loaded fine.
      const [comments, children, agents] = await Promise.all([
        // Upstream defaults to newest-first; the web thread view renders
        // ascending chronological, so request asc and defensively re-sort by
        // createdAt in case the query param isn't honored.
        fetchJsonWithStatus(config, `/teams/issues/${encodeURIComponent(issueId)}/comments?order=asc`)
          .then((r) => {
            const list = Array.isArray(r.body) ? (r.body as IssueComment[]) : [];
            return [...list].sort((a, b) => a.createdAt.localeCompare(b.createdAt));
          })
          .catch(() => [] as IssueComment[]),
        fetchJsonWithStatus(config, `/teams/issues/${encodeURIComponent(issueId)}/children`)
          .then((r) => {
            const issues = (r.body as { issues?: unknown } | null)?.issues;
            return Array.isArray(issues) ? (issues as ChildIssue[]) : [];
          })
          .catch(() => [] as ChildIssue[]),
        fetchJsonWithStatus(config, "/teams/agents")
          .then((r) => {
            const list = (r.body as { agents?: unknown } | null)?.agents;
            return Array.isArray(list) ? (list as TeamsAgent[]) : [];
          })
          .catch(() => [] as TeamsAgent[]),
      ]);
      if (loadSeq.current !== seq) return;
      setState({ kind: "ready", issue, comments, children, agents });
    } catch (err) {
      if (loadSeq.current !== seq) return;
      const gate = gateFromError(err);
      if (gate) {
        setState({ kind: "gate", gate, message: null });
      } else {
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : "Request failed",
        });
      }
    }
  }, [config, issueId]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  return { state, retry: load };
}

// ---------------------------------------------------------------------------
// Board model (contract §1/§3) — status column order, display labels, and the
// status/priority → theme-token color mappings the card + status sheet consume.
// ---------------------------------------------------------------------------

/** The 7 board columns, left→right, exactly as the web board lays them out. */
export const BOARD_STATUSES = [
  "backlog",
  "todo",
  "in_progress",
  "in_review",
  "blocked",
  "done",
  "cancelled",
] as const;

export type BoardStatus = (typeof BOARD_STATUSES)[number];

/** snake_case wire value → Title Case display label. */
export const STATUS_LABELS: Record<BoardStatus, string> = {
  backlog: "Backlog",
  todo: "Todo",
  in_progress: "In Progress",
  in_review: "In Review",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
};

/** Priority enum (contract §3) high→low, for the status/priority indicators. */
export const PRIORITIES = ["critical", "high", "medium", "low"] as const;
export type Priority = (typeof PRIORITIES)[number];

export const PRIORITY_LABELS: Record<Priority, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

/** Narrow an arbitrary wire status onto a board column, or null if off-board. */
export function toBoardStatus(status: string | null | undefined): BoardStatus | null {
  if (!status) return null;
  return (BOARD_STATUSES as readonly string[]).includes(status)
    ? (status as BoardStatus)
    : null;
}

/** Status → chip `{color, tint}` (theme tokens). Unknown statuses fall back to mute/cream. */
export function statusColors(status: string | null | undefined): {
  color: string;
  tint: string;
} {
  const key = toBoardStatus(status);
  if (key) return colors.status[key];
  return { color: colors.mute, tint: colors.creamDeeper };
}

/** Priority → accent color (theme token), or null when unset/unknown. */
export function priorityColor(priority: string | null | undefined): string | null {
  if (!priority) return null;
  if ((PRIORITIES as readonly string[]).includes(priority)) {
    return colors.priority[priority as Priority];
  }
  return null;
}

// ---------------------------------------------------------------------------
// Write wrapper — status move (the first mobile write, contract §4).
// PATCH /teams/issues/{id} {status}. Bearer-only, identical auth to reads; the
// shared client's `.patch` (createApiClient) already exists.
// ---------------------------------------------------------------------------

export async function updateIssueStatus(
  config: PlatformAdapter,
  id: string,
  status: BoardStatus,
): Promise<void> {
  const client = createApiClient(config);
  await client.patch(`/teams/issues/${encodeURIComponent(id)}`, { status });
}

// ---------------------------------------------------------------------------
// Board hook — one gate-bearing call (`/teams/issues`) is both the readiness
// probe AND the data source; agents ride alongside (best-effort) for assignee
// names. `moveIssue` does an optimistic local move, reconciles by refetch, and
// reverts + Alerts on failure. A per-issue ref-latch prevents double-submit.
// ---------------------------------------------------------------------------

export type BoardState =
  | { kind: "loading" }
  | { kind: "gate"; gate: TeamsGate; message: string | null }
  | { kind: "ready"; issues: BoardIssue[]; agents: TeamsAgent[] };

function extractIssues(body: unknown): BoardIssue[] {
  const issues = (body as { issues?: unknown } | null)?.issues;
  return Array.isArray(issues) ? (issues as BoardIssue[]) : [];
}

export function useTeamsBoard() {
  const config = useTeamsConfig();
  const [state, setState] = useState<BoardState>({ kind: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const loadSeq = useRef(0);
  // Latest state mirror — lets `moveIssue` read the pre-move status without
  // capturing side effects inside a setState updater (StrictMode-safe).
  const stateRef = useRef(state);
  stateRef.current = state;
  // Per-issue in-flight latch: a second move on the same card is ignored until
  // the first settles.
  const movingRef = useRef<Set<string>>(new Set());

  const load = useCallback(
    async (isRefresh = false) => {
      const seq = loadSeq.current + 1;
      loadSeq.current = seq;
      if (isRefresh) setRefreshing(true);
      else setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
      try {
        // Primary call carries the readiness gate. Sort newest-first so columns
        // read chronologically; limit high so the whole board loads in one shot.
        const { status, body } = await fetchJsonWithStatus(
          config,
          "/teams/issues?sortField=updated&sortDir=desc&limit=500",
        );
        if (loadSeq.current !== seq) return;
        const gate = gateFromSuccess(status, body);
        if (gate) {
          setState({ kind: "gate", gate, message: null });
          return;
        }
        const issues = extractIssues(body);
        // Agent names are best-effort — their failure must not blank the board.
        const agents = await fetchJsonWithStatus(config, "/teams/agents")
          .then((r) => {
            const list = (r.body as { agents?: unknown } | null)?.agents;
            return Array.isArray(list) ? (list as TeamsAgent[]) : [];
          })
          .catch(() => [] as TeamsAgent[]);
        if (loadSeq.current !== seq) return;
        setState({ kind: "ready", issues, agents });
      } catch (err) {
        if (loadSeq.current !== seq) return;
        const gate = gateFromError(err);
        setState({
          kind: "gate",
          gate: gate ?? "unavailable",
          message: err instanceof Error ? err.message : "Request failed",
        });
      } finally {
        if (loadSeq.current === seq && isRefresh) setRefreshing(false);
      }
    },
    [config],
  );

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const moveIssue = useCallback(
    async (id: string, next: BoardStatus) => {
      const current = stateRef.current;
      if (current.kind !== "ready") return;
      if (movingRef.current.has(id)) return; // ref-latch: no double-submit
      const target = current.issues.find((i) => i.id === id);
      if (!target) return;
      const prevStatus = target.status ?? null;
      if (prevStatus === next) return; // no-op move

      movingRef.current.add(id);
      // Optimistic: move the card locally right away.
      setState((prev) =>
        prev.kind === "ready"
          ? {
              ...prev,
              issues: prev.issues.map((i) =>
                i.id === id ? { ...i, status: next } : i,
              ),
            }
          : prev,
      );
      try {
        await updateIssueStatus(config, id, next);
        // Silent reconcile (load(false) keeps the ready state in place — no
        // pull-to-refresh spinner flash on every tap-move).
        await load(false);
      } catch (err) {
        // Revert the optimistic move and surface the failure.
        setState((prev) =>
          prev.kind === "ready"
            ? {
                ...prev,
                issues: prev.issues.map((i) =>
                  i.id === id ? { ...i, status: prevStatus } : i,
                ),
              }
            : prev,
        );
        Alert.alert(
          "Couldn’t move issue",
          err instanceof Error ? err.message : "The status change failed. Please try again.",
        );
      } finally {
        movingRef.current.delete(id);
      }
    },
    [config, load],
  );

  return {
    state,
    refreshing,
    retry: () => load(false),
    refresh: () => load(true),
    moveIssue,
  };
}

/** Count direct sub-issues per parent, derived from the already-loaded board. */
export function subtaskCounts(issues: BoardIssue[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const issue of issues) {
    if (issue.parentId) {
      counts[issue.parentId] = (counts[issue.parentId] ?? 0) + 1;
    }
  }
  return counts;
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

/** "in_progress" → "In progress". Humanizes upstream snake_case statuses. */
export function humanizeStatus(status: string | null | undefined): string {
  if (!status) return "—";
  const spaced = status.replace(/_/g, " ").trim();
  if (!spaced) return "—";
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Relative timestamp (port of web timeAgo — pure date math). */
export function timeAgo(value: string | null | undefined): string {
  if (!value) return "";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.round((Date.now() - then) / 1000);
  const MINUTE = 60;
  const HOUR = 60 * MINUTE;
  const DAY = 24 * HOUR;
  const WEEK = 7 * DAY;
  const MONTH = 30 * DAY;
  if (seconds < MINUTE) return "just now";
  if (seconds < HOUR) return `${Math.floor(seconds / MINUTE)}m ago`;
  if (seconds < DAY) return `${Math.floor(seconds / HOUR)}h ago`;
  if (seconds < WEEK) return `${Math.floor(seconds / DAY)}d ago`;
  if (seconds < MONTH) return `${Math.floor(seconds / WEEK)}w ago`;
  return `${Math.floor(seconds / MONTH)}mo ago`;
}

/** Resolve an issue-comment author into a display label (agent name or role). */
export function commentAuthorLabel(
  comment: IssueComment,
  agents: TeamsAgent[],
): string {
  if (comment.authorAgentId) {
    return agents.find((a) => a.id === comment.authorAgentId)?.name ?? "Agent";
  }
  if (comment.authorKind === "agent") return "Agent";
  if (comment.authorUserId || comment.authorKind === "user") return "You";
  return "Comment";
}

/** Resolve an issue's assignee into a display label, or null if unassigned. */
export function assigneeLabel(
  issue: Pick<IssueDetail, "assigneeAgentId" | "assigneeUserId">,
  agents: TeamsAgent[],
): string | null {
  if (issue.assigneeAgentId) {
    return agents.find((a) => a.id === issue.assigneeAgentId)?.name ?? "Agent";
  }
  if (issue.assigneeUserId) return "Assigned to member";
  return null;
}

/** Heuristic: treat text with markdown markers as markdown, else plain. */
export function looksLikeMarkdown(text: string): boolean {
  return /(^|\n)\s{0,3}(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```)|\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)/.test(
    text,
  );
}
