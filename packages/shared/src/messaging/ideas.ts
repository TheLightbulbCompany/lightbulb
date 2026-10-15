/**
 * Ideas (spec 2026-10-14 §4.4): jobs a company usually pays an agency for, done by the team.
 * GET /ideas, POST /ideas/{id}/start and /dismiss, shared by web and mobile. No prices: the
 * server never sends them (owner, 2026-10-15). No DOM or react-native here.
 */
import { useCallback, useMemo } from "react";
import useSWR from "swr";
import type { MessagingClient } from "./client";

export interface Idea {
  id: string;
  ownerRole: string;
  kind: "agency" | "everyday";
  businessTypes: string[];
  title: string;
  why: string;
  deliverables: string[];
  ourTurnaround: string;
  yourPart: string;
  agentId: string;
  agentName: string;
}

export interface RunningIdea {
  ideaId: string;
  title: string;
  agentId: string | null;
  agentName: string | null;
  roomId: string | null;
  projectId: string;
  starterId: string;
  starterName: string;
  createdAt: string | null;
  /** Issue status -> count in the idea's project; absent when Work did not answer. */
  progress?: Record<string, number>;
  /** The plan the agent filed for approval: null before it has, absent when Work did not answer. */
  plan?: IdeaPlan | null;
}

export interface IdeaPlan {
  approvalId: string;
  status: string;
  title: string;
  /** When it was decided (e.g. a revision asked); null while pending. */
  decidedAt?: string | null;
}

/** The DM's plan polling: ask this often while a start waits for a (new) plan. */
export const PLAN_POLL_MS = 10_000;
/** No plan yet: for this long after the Start. */
const PLAN_FIRST_FOR_MS = 5 * 60_000;
/** Changes asked: for this long after the revision, until the agent files a new pending plan. */
const PLAN_REVISED_FOR_MS = 10 * 60_000;

/** The viewer's own running starts in this DM, newest first: one plan card each. */
export function myStartsIn(data: Pick<IdeasResponse, "running"> | undefined, roomId: string, userId: string | null | undefined): RunningIdea[] {
  return (data?.running ?? [])
    .filter((r) => r.roomId === roomId && r.starterId === userId)
    .sort((a, b) => (Date.parse(b.createdAt ?? "") || 0) - (Date.parse(a.createdAt ?? "") || 0));
}

/** Whether the DM should keep asking for this start's plan: before the first one, and after a
 *  revision request until a newer pending plan replaces it. Both windows are bounded. */
export function waitsForPlan(start: RunningIdea, now = Date.now()): boolean {
  const since = (iso: string | null | undefined) => now - (Date.parse(iso ?? "") || -Infinity);
  if (!start.plan) return since(start.createdAt) < PLAN_FIRST_FOR_MS;
  if (start.plan.status === "revision_requested") return since(start.plan.decidedAt ?? start.createdAt) < PLAN_REVISED_FOR_MS;
  return false;
}

/** The refresh interval for a DM's starts: PLAN_POLL_MS while any of them waits, else 0. */
export const plansPollMs = (starts: readonly RunningIdea[], now = Date.now()) =>
  starts.some((start) => waitsForPlan(start, now)) ? PLAN_POLL_MS : 0;

export interface IdeasResponse {
  ideas: Idea[];
  running: RunningIdea[];
  /** The workspace has started an idea (running or finished). */
  anyStarted?: boolean;
  /** The workspace has hidden at least one idea. */
  anyDismissed?: boolean;
}

export interface IdeaStart {
  roomId: string;
  projectId: string;
}

export type IdeasClient = Pick<MessagingClient, "listIdeas" | "startIdea" | "dismissIdea">;

/** The card's one effort line. */
export const ideaEffortLine = (idea: Pick<Idea, "ourTurnaround" | "yourPart">) =>
  `Your team: ${idea.ourTurnaround} · You: ${idea.yourPart}`;

/** Done / total tasks of a running idea (cancelled tasks count for neither); null before any task exists. */
export function ideaProgress(progress: Record<string, number> | undefined): { done: number; total: number } | null {
  if (!progress) return null;
  let done = 0;
  let total = 0;
  for (const [status, count] of Object.entries(progress)) {
    if (status === "cancelled") continue;
    total += count;
    if (status === "done") done += count;
  }
  return total > 0 ? { done, total } : null;
}

/** The server's order (agency jobs first), with `agentId`'s ideas moved to the front (opened from its DM). */
export function orderIdeas(ideas: readonly Idea[], agentId?: string | null): Idea[] {
  if (!agentId) return [...ideas];
  return [...ideas.filter((i) => i.agentId === agentId), ...ideas.filter((i) => i.agentId !== agentId)];
}

/** The teammates who own at least one idea, in first-seen order: the filter chips. */
export function ideaTeammates(ideas: readonly Idea[]): { agentId: string; agentName: string }[] {
  const seen = new Map<string, string>();
  for (const idea of ideas) if (!seen.has(idea.agentId)) seen.set(idea.agentId, idea.agentName);
  return [...seen].map(([agentId, agentName]) => ({ agentId, agentName }));
}

const EMPTY: IdeasResponse = { ideas: [], running: [] };

/**
 * The workspace's ideas. `scope` (user:org) keys the cache; null waits for auth. `dismiss` is
 * optimistic and puts the idea back when the server refuses; `start` refetches so the idea shows
 * as running.
 */
export function useIdeas(
  client: IdeasClient,
  scope: string | null,
  /** Poll while this says so (ms; 0 stops), e.g. while a DM waits for its plan. */
  refreshInterval?: (latest: IdeasResponse | undefined) => number,
) {
  const { data, error, isLoading, mutate } = useSWR<IdeasResponse>(
    scope ? ["ideas", scope] : null,
    () => client.listIdeas(),
    { revalidateOnFocus: false, ...(refreshInterval ? { refreshInterval } : {}) },
  );
  const start = useCallback(async (ideaId: string) => {
    const started = await client.startIdea(ideaId);
    void mutate();
    return started;
  }, [client, mutate]);
  const dismiss = useCallback(async (ideaId: string) => {
    await mutate(async (current) => {
      await client.dismissIdea(ideaId);
      return current && { ...current, ideas: current.ideas.filter((i) => i.id !== ideaId), anyDismissed: true };
    }, {
      optimisticData: (current) => ({
        ...(current ?? EMPTY), ideas: (current ?? EMPTY).ideas.filter((i) => i.id !== ideaId), anyDismissed: true,
      }),
      rollbackOnError: true,
      revalidate: false,
    });
  }, [client, mutate]);
  return useMemo(() => ({
    ideas: data?.ideas ?? EMPTY.ideas,
    running: data?.running ?? EMPTY.running,
    anyStarted: !!data?.anyStarted,
    anyDismissed: !!data?.anyDismissed,
    loading: isLoading,
    error: error as unknown,
    refresh: () => mutate(),
    start,
    dismiss,
  }), [data, isLoading, error, mutate, start, dismiss]);
}
