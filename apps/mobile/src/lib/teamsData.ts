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

/**
 * A company member from `GET /teams/members` (`{members:[…]}`, contract §1).
 * `userId` is the **Clerk** user id; the BFF joins principal→Clerk and resolves
 * name/email/imageUrl. `name` already falls back full-name → email → id upstream,
 * so it is always a usable label. Powers human-assignee + comment-author names.
 */
export interface CompanyMember {
  userId: string;
  name?: string | null;
  email?: string | null;
  imageUrl?: string | null;
}

/**
 * A resolved assignee display descriptor — the differentiated card + detail row
 * render an agent monogram vs a human initials/photo avatar off `kind`.
 */
export interface AssigneeDisplay {
  name: string;
  kind: "agent" | "user";
  /** Clerk avatar URL for human assignees (null for agents / no photo). */
  imageUrl?: string | null;
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

/**
 * Best-effort `GET /teams/agents` → `TeamsAgent[]`. A failure resolves to `[]`
 * so the caller's primary (gate-bearing) data is never blanked by a name-source
 * hiccup. Shared by the board + issue-detail loads.
 */
function fetchAgents(config: PlatformAdapter): Promise<TeamsAgent[]> {
  return fetchJsonWithStatus(config, "/teams/agents")
    .then((r) => {
      const list = (r.body as { agents?: unknown } | null)?.agents;
      return Array.isArray(list) ? (list as TeamsAgent[]) : [];
    })
    .catch(() => [] as TeamsAgent[]);
}

/**
 * Best-effort `GET /teams/members` → `CompanyMember[]` (contract §1). Same
 * best-effort posture as {@link fetchAgents}: a members-fetch failure must NOT
 * blank the board/detail — it just degrades human names to their id fallback.
 */
function fetchMembers(config: PlatformAdapter): Promise<CompanyMember[]> {
  return fetchJsonWithStatus(config, "/teams/members")
    .then((r) => {
      const list = (r.body as { members?: unknown } | null)?.members;
      return Array.isArray(list) ? (list as CompanyMember[]) : [];
    })
    .catch(() => [] as CompanyMember[]);
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
      members: CompanyMember[];
      approvals: TeamsApproval[];
    };

export function useTeamsIssue(issueId: string | undefined) {
  const config = useTeamsConfig();
  const [state, setState] = useState<IssueDetailState>({ kind: "loading" });
  const loadSeq = useRef(0);
  // Latest-state mirror so the optimistic writers read the pre-write issue
  // without capturing it inside a setState updater (StrictMode-safe, mirrors
  // useTeamsBoard).
  const stateRef = useRef(state);
  stateRef.current = state;
  // Per-field in-flight latch for property PATCHes (status/priority/assignee),
  // plus separate latches for comment posting and child creation. All prevent
  // double-submit while a write is settling.
  const writingRef = useRef<Set<string>>(new Set());
  const postingRef = useRef(false);
  const creatingRef = useRef(false);
  // Per-approval in-flight latch for the issue-detail approve/reject actions.
  const decidingRef = useRef<Set<string>>(new Set());

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

      // Secondary reads are best-effort: a comments/children/agents/members
      // failure must not blank out an issue that loaded fine.
      const [comments, children, agents, members, approvals] = await Promise.all([
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
        fetchAgents(config),
        fetchMembers(config),
        // Per-issue approvals (contract §2) — best-effort like the other
        // secondary reads: a failure just hides the Approvals section, it never
        // blanks an issue that otherwise loaded.
        fetchIssueApprovals(config, issueId),
      ]);
      if (loadSeq.current !== seq) return;
      setState({ kind: "ready", issue, comments, children, agents, members, approvals });
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

  // Generic optimistic single-field PATCH: apply the field locally, write, then
  // silently reconcile via a refetch; on failure revert to the pre-write issue
  // and Alert. A per-field latch blocks a second write on the same field.
  const patchField = useCallback(
    async (
      fieldKey: string,
      optimistic: (issue: IssueDetail) => IssueDetail,
      write: () => Promise<void>,
    ) => {
      const current = stateRef.current;
      if (current.kind !== "ready" || !issueId) return;
      if (writingRef.current.has(fieldKey)) return; // no double-submit
      const prevIssue = current.issue;
      writingRef.current.add(fieldKey);
      setState((prev) =>
        prev.kind === "ready" ? { ...prev, issue: optimistic(prev.issue) } : prev,
      );
      try {
        await write();
        await load(); // silent reconcile (keeps ready state in place)
      } catch (err) {
        setState((prev) =>
          prev.kind === "ready" ? { ...prev, issue: prevIssue } : prev,
        );
        Alert.alert(
          "Couldn’t update issue",
          err instanceof Error ? err.message : "The change failed. Please try again.",
        );
      } finally {
        writingRef.current.delete(fieldKey);
      }
    },
    [issueId, load],
  );

  const setStatus = useCallback(
    (status: BoardStatus) =>
      patchField(
        "status",
        (issue) => ({ ...issue, status }),
        () => updateIssueStatus(config, issueId as string, status),
      ),
    [patchField, config, issueId],
  );

  const setPriority = useCallback(
    (priority: Priority) =>
      patchField(
        "priority",
        (issue) => ({ ...issue, priority }),
        () => updateIssuePriority(config, issueId as string, priority),
      ),
    [patchField, config, issueId],
  );

  const setAssignee = useCallback(
    (agentId: string | null) =>
      patchField(
        "assignee",
        (issue) => ({
          ...issue,
          assigneeAgentId: agentId,
          // agent + human assignees are mutually exclusive (contract §1) —
          // assigning an agent clears any human assignee locally too.
          assigneeUserId: null,
        }),
        () => updateIssueAssignee(config, issueId as string, agentId),
      ),
    [patchField, config, issueId],
  );

  // Assign a human member (or unassign). Shares the "assignee" field latch with
  // setAssignee so an agent-write and a human-write can't race — only ONE of the
  // two mutually-exclusive fields is ever set (contract §1).
  const setUserAssignee = useCallback(
    (userId: string | null) =>
      patchField(
        "assignee",
        (issue) => ({
          ...issue,
          assigneeUserId: userId,
          assigneeAgentId: null,
        }),
        () => updateIssueUserAssignee(config, issueId as string, userId),
      ),
    [patchField, config, issueId],
  );

  // Post a comment: append an optimistic temp row, POST, then reconcile via
  // refetch (which replaces the temp with the server row). On failure drop the
  // temp + Alert. Returns whether the post succeeded so the composer can clear.
  const addComment = useCallback(
    async (body: string): Promise<boolean> => {
      const trimmed = body.trim();
      if (!trimmed) return false;
      const current = stateRef.current;
      if (current.kind !== "ready" || !issueId) return false;
      if (postingRef.current) return false; // no double-submit
      postingRef.current = true;
      const temp: IssueComment = {
        id: `temp-${config.uuid()}`,
        body: trimmed,
        createdAt: new Date().toISOString(),
        authorKind: "user",
      };
      setState((prev) =>
        prev.kind === "ready"
          ? { ...prev, comments: [...prev.comments, temp] }
          : prev,
      );
      try {
        await postComment(config, issueId, trimmed);
        await load();
        return true;
      } catch (err) {
        setState((prev) =>
          prev.kind === "ready"
            ? { ...prev, comments: prev.comments.filter((c) => c.id !== temp.id) }
            : prev,
        );
        Alert.alert(
          "Couldn’t post comment",
          err instanceof Error ? err.message : "The comment failed to send.",
        );
        return false;
      } finally {
        postingRef.current = false;
      }
    },
    [config, issueId, load],
  );

  // Create a sub-issue under this issue, then refetch children. Ref-latched.
  const createChild = useCallback(
    async (input: CreateIssueInput): Promise<boolean> => {
      if (!issueId || creatingRef.current) return false;
      creatingRef.current = true;
      try {
        await createSubIssue(config, issueId, input);
        await load();
        return true;
      } catch (err) {
        Alert.alert(
          "Couldn’t create sub-issue",
          err instanceof Error ? err.message : "The sub-issue could not be created.",
        );
        return false;
      } finally {
        creatingRef.current = false;
      }
    },
    [config, issueId, load],
  );

  // Approve / reject an approval linked to THIS issue. Optimistically flip the
  // row's status to its resolved value (which hides its Approve/Reject buttons —
  // `isApprovalActionable` goes false), write, then reconcile via refetch. On
  // failure revert the whole approvals list and Alert. Per-approval ref-latch
  // blocks a double approve/reject.
  const decideApproval = useCallback(
    async (
      approvalId: string,
      resolvedStatus: ApprovalStatus,
      write: () => Promise<void>,
    ) => {
      const current = stateRef.current;
      if (current.kind !== "ready") return;
      if (decidingRef.current.has(approvalId)) return; // no double-submit
      const prevApprovals = current.approvals;
      decidingRef.current.add(approvalId);
      setState((prev) =>
        prev.kind === "ready"
          ? {
              ...prev,
              approvals: prev.approvals.map((a) =>
                a.id === approvalId ? { ...a, status: resolvedStatus } : a,
              ),
            }
          : prev,
      );
      try {
        await write();
        await load(); // silent reconcile
      } catch (err) {
        setState((prev) =>
          prev.kind === "ready" ? { ...prev, approvals: prevApprovals } : prev,
        );
        Alert.alert(
          "Couldn’t update approval",
          err instanceof Error ? err.message : "The decision failed. Please try again.",
        );
      } finally {
        decidingRef.current.delete(approvalId);
      }
    },
    [load],
  );

  const approveIssueApproval = useCallback(
    (approvalId: string) =>
      decideApproval(approvalId, "approved", () =>
        approveApproval(config, approvalId),
      ),
    [decideApproval, config],
  );

  const rejectIssueApproval = useCallback(
    (approvalId: string, reason: string) =>
      decideApproval(approvalId, "rejected", () =>
        rejectApproval(config, approvalId, reason),
      ),
    [decideApproval, config],
  );

  return {
    state,
    retry: load,
    reload: load,
    setStatus,
    setPriority,
    setAssignee,
    setUserAssignee,
    addComment,
    createChild,
    approveApproval: approveIssueApproval,
    rejectApproval: rejectIssueApproval,
  };
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

/** PATCH /teams/issues/{id} {priority}. */
export async function updateIssuePriority(
  config: PlatformAdapter,
  id: string,
  priority: Priority,
): Promise<void> {
  await createApiClient(config).patch(
    `/teams/issues/${encodeURIComponent(id)}`,
    { priority },
  );
}

/**
 * PATCH /teams/issues/{id} — assign an AGENT (or unassign with `null`). Sends
 * `assigneeUserId: null` alongside so the two mutually-exclusive assignee fields
 * (agent XOR human, contract §1) never both end up set: picking an agent clears
 * any human assignee server-side.
 */
export async function updateIssueAssignee(
  config: PlatformAdapter,
  id: string,
  agentId: string | null,
): Promise<void> {
  await createApiClient(config).patch(
    `/teams/issues/${encodeURIComponent(id)}`,
    { assigneeAgentId: agentId, assigneeUserId: null },
  );
}

/**
 * PATCH /teams/issues/{id} — assign a HUMAN member (or unassign with `null`).
 * Mirror of {@link updateIssueAssignee}: sends `assigneeAgentId: null` so picking
 * a person clears any agent assignee (mutual exclusivity, contract §1).
 */
export async function updateIssueUserAssignee(
  config: PlatformAdapter,
  id: string,
  userId: string | null,
): Promise<void> {
  await createApiClient(config).patch(
    `/teams/issues/${encodeURIComponent(id)}`,
    { assigneeUserId: userId, assigneeAgentId: null },
  );
}

/** PATCH /teams/issues/{id} with title and/or description (wire camelCase). */
export async function updateIssueField(
  config: PlatformAdapter,
  id: string,
  fields: { title?: string; description?: string },
): Promise<void> {
  await createApiClient(config).patch(
    `/teams/issues/${encodeURIComponent(id)}`,
    fields,
  );
}

/**
 * POST /teams/issues/{id}/comments {body}. Returns the created comment
 * (contract §4 — the BFF whitelists `body` ONLY).
 */
export async function postComment(
  config: PlatformAdapter,
  id: string,
  body: string,
): Promise<IssueComment> {
  const res = await createApiClient(config).post(
    `/teams/issues/${encodeURIComponent(id)}/comments`,
    { body },
  );
  return res as IssueComment;
}

/**
 * Shared create body for top-level issues AND sub-issues (contract §4 —
 * `CreateIssueBody`). `undefined` fields are dropped by JSON.stringify, so the
 * `extra=forbid` upstream never sees an unexpected key. `status` is the create-
 * locked 7 (== BOARD_STATUSES); `priority` the 4-value enum.
 */
export interface CreateIssueInput {
  title: string;
  description?: string;
  status?: BoardStatus;
  priority?: Priority;
  assigneeAgentId?: string;
  workMode?: "standard" | "ask" | "planning";
}

/** A freshly-created issue — only the id is load-bearing for the client (nav). */
export interface CreatedIssue {
  id?: string;
  identifier?: string | null;
  title?: string;
}

/** POST /teams/issues — create a top-level issue/task. */
export async function createIssue(
  config: PlatformAdapter,
  input: CreateIssueInput,
): Promise<CreatedIssue> {
  const res = await createApiClient(config).post("/teams/issues", input);
  return (res ?? {}) as CreatedIssue;
}

/** POST /teams/issues/{parentId}/children — create a sub-issue under a parent. */
export async function createSubIssue(
  config: PlatformAdapter,
  parentId: string,
  input: CreateIssueInput,
): Promise<CreatedIssue> {
  const res = await createApiClient(config).post(
    `/teams/issues/${encodeURIComponent(parentId)}/children`,
    input,
  );
  return (res ?? {}) as CreatedIssue;
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
  | {
      kind: "ready";
      issues: BoardIssue[];
      agents: TeamsAgent[];
      members: CompanyMember[];
    };

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
  // Single latch for the create-issue action (header "+").
  const creatingRef = useRef(false);

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
        // Agent + member names are best-effort — either failure resolves to []
        // and must not blank the board (it only degrades assignee names).
        const [agents, members] = await Promise.all([
          fetchAgents(config),
          fetchMembers(config),
        ]);
        if (loadSeq.current !== seq) return;
        setState({ kind: "ready", issues, agents, members });
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

  // Create a top-level issue, then reconcile the board via refetch. Ref-latched
  // so a double-tap can't create two. Returns the created issue (with id) on
  // success or null on failure (already Alerted).
  const createBoardIssue = useCallback(
    async (input: CreateIssueInput): Promise<CreatedIssue | null> => {
      if (creatingRef.current) return null;
      creatingRef.current = true;
      try {
        const created = await createIssue(config, input);
        await load(false);
        return created;
      } catch (err) {
        Alert.alert(
          "Couldn’t create issue",
          err instanceof Error ? err.message : "The issue could not be created.",
        );
        return null;
      } finally {
        creatingRef.current = false;
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
    createBoardIssue,
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
// Approvals (contract §2) — agent→human approval workflow. A global inbox
// (`GET /teams/approvals`) plus a per-issue list (`GET /teams/issues/{id}/
// approvals`), both `{approvals:[…]}`. Rows carry NO top-level title — the
// label is synthesized from `type` + `payload` (ported `approvalLabel`).
// Writes: POST .../approve `{note?}`, POST .../reject `{reason}` (required).
// ---------------------------------------------------------------------------

/** Upstream APPROVAL_STATUSES. `pending` + `revision_requested` are actionable. */
export type ApprovalStatus =
  | "pending"
  | "revision_requested"
  | "approved"
  | "rejected"
  | "cancelled";

/**
 * A row from `GET /teams/approvals` or `GET /teams/issues/{id}/approvals`
 * (`{approvals:[…]}`). The BFF forwards upstream's full (payload-redacted)
 * Approval verbatim, so `requestedByAgentId`/`requestedByUserId` are present on
 * list rows and let us name the requester via the shared agent/member resolvers.
 */
export interface TeamsApproval {
  id: string;
  issueId?: string | null;
  /** Upstream approval `type` (e.g. "hire_agent"); drives the row label. */
  type?: string | null;
  /** Redacted payload subset; the label reads title/name/summary/action from it. */
  payload?: Record<string, unknown> | null;
  status: ApprovalStatus;
  requestedByAgentId?: string | null;
  requestedByUserId?: string | null;
  createdAt?: string | null;
  decidedAt?: string | null;
}

/** Friendly names for the known upstream approval types (ported verbatim from web). */
const APPROVAL_TYPE_LABEL: Record<string, string> = {
  hire_agent: "Hire Agent",
  approve_ceo_strategy: "CEO Strategy",
  budget_override_required: "Budget Override",
  request_board_approval: "Board Approval",
};

function firstNonEmptyString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
  }
  return null;
}

/** The subject line drawn from a payload (title / name / summary / action). */
export function approvalSubject(
  payload?: Record<string, unknown> | null,
): string | null {
  return firstNonEmptyString(
    payload?.title,
    payload?.name,
    payload?.summary,
    payload?.recommendedAction,
  );
}

/**
 * Build a contextual label for an approval, e.g. "Hire Agent: Designer". Ported
 * from web's `approvalLabel` (type+payload → label). Falls back to the friendly
 * type name, then the raw type, then a generic "Approval requested".
 */
export function approvalLabel(
  approval: Pick<TeamsApproval, "type" | "payload">,
): string {
  const type = approval.type;
  if (!type) return "Approval requested";
  const base = APPROVAL_TYPE_LABEL[type] ?? type;
  const subject = approvalSubject(approval.payload);
  return subject ? `${base}: ${subject}` : base;
}

/**
 * Actionability triage (contract §2): `pending` AND `revision_requested` are
 * still actionable (the reviewer can approve/reject); the three terminal states
 * (approved/rejected/cancelled) are resolved.
 */
export function isApprovalActionable(
  approval: Pick<TeamsApproval, "status">,
): boolean {
  return (
    approval.status === "pending" || approval.status === "revision_requested"
  );
}

/** Resolve an approval's requesting agent/human to a display name, or null. */
export function approvalRequesterLabel(
  approval: Pick<TeamsApproval, "requestedByAgentId" | "requestedByUserId">,
  agents: TeamsAgent[],
  members: CompanyMember[] = [],
): string | null {
  if (approval.requestedByAgentId) {
    return (
      agents.find((a) => a.id === approval.requestedByAgentId)?.name ?? "Agent"
    );
  }
  if (approval.requestedByUserId) {
    return memberName(approval.requestedByUserId, members) ?? "Member";
  }
  return null;
}

/** Status → chip `{label, color, tint}` (theme tokens only). */
export function approvalStatusMeta(status: ApprovalStatus): {
  label: string;
  color: string;
  tint: string;
} {
  switch (status) {
    case "pending":
      return { label: "Pending", color: colors.warn, tint: colors.statusTintTodo };
    case "revision_requested":
      return {
        label: "Revision requested",
        color: colors.warn,
        tint: colors.statusTintTodo,
      };
    case "approved":
      return { label: "Approved", color: colors.forest, tint: colors.forestTint };
    case "rejected":
      return { label: "Rejected", color: colors.danger, tint: colors.statusTintBlocked };
    case "cancelled":
      return { label: "Cancelled", color: colors.mute, tint: colors.statusTintCancelled };
  }
}

function extractApprovals(body: unknown): TeamsApproval[] {
  const list = (body as { approvals?: unknown } | null)?.approvals;
  return Array.isArray(list) ? (list as TeamsApproval[]) : [];
}

/**
 * Best-effort `GET /teams/issues/{id}/approvals` → rows. A failure resolves to
 * `[]` so a per-issue approvals hiccup never fails the issue view.
 */
function fetchIssueApprovals(
  config: PlatformAdapter,
  issueId: string,
): Promise<TeamsApproval[]> {
  return fetchJsonWithStatus(
    config,
    `/teams/issues/${encodeURIComponent(issueId)}/approvals`,
  )
    .then((r) => extractApprovals(r.body))
    .catch(() => [] as TeamsApproval[]);
}

/** POST /teams/approvals/{id}/approve `{note?}` — one-click approve (note optional). */
export async function approveApproval(
  config: PlatformAdapter,
  id: string,
  note?: string,
): Promise<void> {
  const trimmed = note?.trim();
  await createApiClient(config).post(
    `/teams/approvals/${encodeURIComponent(id)}/approve`,
    trimmed ? { note: trimmed } : {},
  );
}

/** POST /teams/approvals/{id}/reject `{reason}` — reason is REQUIRED (1–2000). */
export async function rejectApproval(
  config: PlatformAdapter,
  id: string,
  reason: string,
): Promise<void> {
  await createApiClient(config).post(
    `/teams/approvals/${encodeURIComponent(id)}/reject`,
    { reason },
  );
}

// ---------------------------------------------------------------------------
// Global approvals hook — the Teams "Approvals" surface. One gate-bearing call
// (`/teams/approvals`) is both readiness probe AND data source; agents+members
// ride alongside (best-effort) for requester names. approve/reject optimistically
// REMOVE the row from the pending list (the global inbox only shows actionable
// rows), reconcile by refetch, and revert + Alert on failure. Per-approval
// ref-latch prevents double approve/reject.
// ---------------------------------------------------------------------------

export type ApprovalsState =
  | { kind: "loading" }
  | { kind: "gate"; gate: TeamsGate; message: string | null }
  | {
      kind: "ready";
      approvals: TeamsApproval[];
      agents: TeamsAgent[];
      members: CompanyMember[];
    };

export function useTeamsApprovals() {
  const config = useTeamsConfig();
  const [state, setState] = useState<ApprovalsState>({ kind: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const loadSeq = useRef(0);
  const stateRef = useRef(state);
  stateRef.current = state;
  const decidingRef = useRef<Set<string>>(new Set());

  const load = useCallback(
    async (isRefresh = false) => {
      const seq = loadSeq.current + 1;
      loadSeq.current = seq;
      if (isRefresh) setRefreshing(true);
      else setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
      try {
        const { status, body } = await fetchJsonWithStatus(config, "/teams/approvals");
        if (loadSeq.current !== seq) return;
        const gate = gateFromSuccess(status, body);
        if (gate) {
          setState({ kind: "gate", gate, message: null });
          return;
        }
        const approvals = extractApprovals(body);
        const [agents, members] = await Promise.all([
          fetchAgents(config),
          fetchMembers(config),
        ]);
        if (loadSeq.current !== seq) return;
        setState({ kind: "ready", approvals, agents, members });
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

  const decide = useCallback(
    async (id: string, write: () => Promise<void>) => {
      const current = stateRef.current;
      if (current.kind !== "ready") return;
      if (decidingRef.current.has(id)) return; // ref-latch: no double-submit
      const target = current.approvals.find((a) => a.id === id);
      if (!target) return;
      const prevApprovals = current.approvals;
      decidingRef.current.add(id);
      // Optimistic: drop the row from the pending inbox right away.
      setState((prev) =>
        prev.kind === "ready"
          ? { ...prev, approvals: prev.approvals.filter((a) => a.id !== id) }
          : prev,
      );
      try {
        await write();
        await load(false); // silent reconcile
      } catch (err) {
        setState((prev) =>
          prev.kind === "ready" ? { ...prev, approvals: prevApprovals } : prev,
        );
        Alert.alert(
          "Couldn’t update approval",
          err instanceof Error ? err.message : "The decision failed. Please try again.",
        );
      } finally {
        decidingRef.current.delete(id);
      }
    },
    [load],
  );

  const approve = useCallback(
    (id: string) => decide(id, () => approveApproval(config, id)),
    [decide, config],
  );
  const reject = useCallback(
    (id: string, reason: string) => decide(id, () => rejectApproval(config, id, reason)),
    [decide, config],
  );

  return {
    state,
    refreshing,
    retry: () => load(false),
    refresh: () => load(true),
    approve,
    reject,
  };
}

// ---------------------------------------------------------------------------
// Dashboard (contract §3) — a lean, glanceable overview. One round-trip
// (`GET /teams/dashboard` → `{dashboard, sidebar_badges}`) that fans out to the
// upstream summary + badges. Same `_ctx` readiness gate (202/402/503) as the
// inbox/board/approvals surfaces. The mobile surface renders ~4 stat tiles from
// the counts + a minimal run-activity summary; the heavier 14-day charts the
// web `DashboardPanel` draws are intentionally skipped on a phone.
// ---------------------------------------------------------------------------

/** One day of the ~14-day run-activity series (contract §3). */
export interface RunActivityDay {
  date: string;
  succeeded?: number | null;
  failed?: number | null;
  other?: number | null;
  total?: number | null;
}

/**
 * `dashboard` from `GET /teams/dashboard` (contract §3). All fields are
 * defensively optional — the BFF forwards upstream's summary verbatim and any
 * sub-object can be absent. The mobile tiles read `agents.active`, `tasks.open`,
 * `costs.monthSpendCents`, and `pendingApprovals (+ budgets.pendingApprovals)`.
 */
export interface DashboardSummary {
  agents?: {
    active?: number | null;
    running?: number | null;
    paused?: number | null;
    error?: number | null;
  } | null;
  tasks?: {
    open?: number | null;
    inProgress?: number | null;
    blocked?: number | null;
    done?: number | null;
  } | null;
  runActivity?: RunActivityDay[] | null;
  costs?: {
    monthSpendCents?: number | null;
    monthBudgetCents?: number | null;
    monthUtilizationPercent?: number | null;
  } | null;
  pendingApprovals?: number | null;
  budgets?: { pendingApprovals?: number | null } | null;
}

/**
 * Total actionable approvals for the "Pending approvals" tile: the plain
 * pending count PLUS budget-override approvals (contract §3 — web sums
 * `pendingApprovals + budgets.pendingApprovals`).
 */
export function totalPendingApprovals(dashboard: DashboardSummary): number {
  return (
    (dashboard.pendingApprovals ?? 0) + (dashboard.budgets?.pendingApprovals ?? 0)
  );
}

/** Sum a run-activity day's total (prefer `total`, else succeeded+failed+other). */
export function runDayTotal(day: RunActivityDay): number {
  if (day.total != null) return day.total;
  return (day.succeeded ?? 0) + (day.failed ?? 0) + (day.other ?? 0);
}

/** Cents → "$X.XX" (month-spend tile). Non-finite/absent → "$0.00". */
export function formatCents(cents: number | null | undefined): string {
  const value = typeof cents === "number" && Number.isFinite(cents) ? cents : 0;
  return `$${(value / 100).toFixed(2)}`;
}

export type DashboardState =
  | { kind: "loading" }
  | { kind: "gate"; gate: TeamsGate; message: string | null }
  | { kind: "ready"; dashboard: DashboardSummary };

function extractDashboard(body: unknown): DashboardSummary {
  const dashboard = (body as { dashboard?: unknown } | null)?.dashboard;
  return dashboard && typeof dashboard === "object"
    ? (dashboard as DashboardSummary)
    : {};
}

/**
 * Dashboard hook — the Teams "Overview" surface. One gate-bearing call
 * (`/teams/dashboard`) is both the readiness probe AND the data source, mirroring
 * the inbox/board/approvals hooks. Focus-refetch + pull-to-refresh; getTokenRef
 * keeps `load` stable (never `[getToken]`).
 */
export function useTeamsDashboard() {
  const config = useTeamsConfig();
  const [state, setState] = useState<DashboardState>({ kind: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const loadSeq = useRef(0);

  const load = useCallback(
    async (isRefresh = false) => {
      const seq = loadSeq.current + 1;
      loadSeq.current = seq;
      if (isRefresh) setRefreshing(true);
      else setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
      try {
        const { status, body } = await fetchJsonWithStatus(config, "/teams/dashboard");
        if (loadSeq.current !== seq) return;
        const gate = gateFromSuccess(status, body);
        if (gate) {
          setState({ kind: "gate", gate, message: null });
        } else {
          setState({ kind: "ready", dashboard: extractDashboard(body) });
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

/** Look up a company member by Clerk user id, or null. */
export function memberById(
  userId: string | null | undefined,
  members: CompanyMember[],
): CompanyMember | null {
  if (!userId) return null;
  return members.find((m) => m.userId === userId) ?? null;
}

/**
 * Resolve a Clerk user id to a human display name. Uses the members map's
 * upstream-resolved `name` (already full-name → email → id), falling back to a
 * short id prefix when the member isn't in the map (mirrors web's
 * `formatAssigneeUserLabel` id fallback). Returns null for an empty id.
 */
export function memberName(
  userId: string | null | undefined,
  members: CompanyMember[],
): string | null {
  if (!userId) return null;
  const name = memberById(userId, members)?.name?.trim();
  if (name) return name;
  // Unresolved (member dropped by the BFF, or members fetch failed). Clerk ids
  // are all `user_…`, so slicing them yields an identical "user_" for everyone
  // — a generic "Member" reads better than a false identifier.
  return "Member";
}

/**
 * Resolve an issue's assignee into a display descriptor (agent monogram vs
 * human initials/photo), or null when unassigned. Agent and human are mutually
 * exclusive; the agent field wins if (defensively) both are set.
 */
export function resolveAssignee(
  issue: Pick<BoardIssue, "assigneeAgentId" | "assigneeUserId">,
  agents: TeamsAgent[],
  members: CompanyMember[],
): AssigneeDisplay | null {
  if (issue.assigneeAgentId) {
    return {
      name: agents.find((a) => a.id === issue.assigneeAgentId)?.name ?? "Agent",
      kind: "agent",
    };
  }
  if (issue.assigneeUserId) {
    const member = memberById(issue.assigneeUserId, members);
    return {
      name: memberName(issue.assigneeUserId, members) ?? "Member",
      kind: "user",
      imageUrl: member?.imageUrl ?? null,
    };
  }
  return null;
}

/**
 * Resolve an issue-comment author into a display label. Agent authors resolve to
 * the agent name; human authors resolve to their member name (contract §1) — or
 * "You" when the author is the current Clerk user.
 */
export function commentAuthorLabel(
  comment: IssueComment,
  agents: TeamsAgent[],
  members: CompanyMember[] = [],
  currentUserId: string | null = null,
): string {
  if (comment.authorAgentId) {
    return agents.find((a) => a.id === comment.authorAgentId)?.name ?? "Agent";
  }
  if (comment.authorKind === "agent") return "Agent";
  if (comment.authorUserId) {
    if (currentUserId && comment.authorUserId === currentUserId) return "You";
    return memberName(comment.authorUserId, members) ?? "Member";
  }
  if (comment.authorKind === "user") return "You";
  return "Comment";
}

/** Heuristic: treat text with markdown markers as markdown, else plain. */
export function looksLikeMarkdown(text: string): boolean {
  return /(^|\n)\s{0,3}(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```)|\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)/.test(
    text,
  );
}
