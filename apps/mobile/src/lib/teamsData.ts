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
import { ApiError, fetchJsonWithStatus, type PlatformAdapter } from "@isol8/shared";

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
        fetchJsonWithStatus(config, `/teams/issues/${encodeURIComponent(issueId)}/comments`)
          .then((r) => (Array.isArray(r.body) ? (r.body as IssueComment[]) : []))
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
