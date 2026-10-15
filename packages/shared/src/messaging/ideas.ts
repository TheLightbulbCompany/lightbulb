/**
 * Ideas (spec 2026-10-14 §4.4): jobs a company usually pays an agency for, done by the team. An
 * idea is a pre-written request (owner, 2026-10-15): Let's do it (POST /ideas/{id}/start) makes
 * its project or task in Work (by size; a quick idea makes nothing) and posts the person's own
 * message to the agent. GET /ideas, start and
 * dismiss, shared by web and mobile. No prices: the server never sends them. No DOM here.
 */
import { useCallback, useMemo } from "react";
import useSWR from "swr";
import type { MessagingClient } from "./client";

export type IdeaSize = "project" | "task" | "quick";

export interface Idea {
  id: string;
  /** Catalog ideas only; an agent's suggestion has its agent instead. */
  ownerRole?: string;
  kind: "agency" | "everyday";
  /** project: a project in Work; task: one task in Work; quick: answered in one go. */
  size: IdeaSize;
  businessTypes: string[];
  title: string;
  why: string;
  deliverables: string[];
  /** Catalog ideas only. */
  ourTurnaround?: string;
  yourPart?: string;
  /** An agent added it for this company ("For you"). */
  suggested?: boolean;
  agentId: string;
  agentName: string;
}

export interface RunningIdea {
  ideaId: string;
  title: string;
  /** A quick idea is answered in one go and never runs. */
  size: "project" | "task";
  agentId: string | null;
  agentName: string | null;
  roomId: string | null;
  projectId: string | null;
  issueId: string | null;
  starterId: string;
  starterName: string;
  createdAt: string | null;
  /** Issue status -> count: the project's tasks, or the one task; absent when Work did not answer. */
  progress?: Record<string, number>;
}

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
  projectId?: string;
  issueId?: string;
}

export type IdeasClient = Pick<MessagingClient, "listIdeas" | "startIdea" | "dismissIdea">;

/** The card's one effort line; null when the idea has none (an agent's suggestion). */
export const ideaEffortLine = (idea: Pick<Idea, "ourTurnaround" | "yourPart">): string | null =>
  idea.ourTurnaround && idea.yourPart ? `Your team: ${idea.ourTurnaround} · You: ${idea.yourPart}` : null;

const TASK_STATUS: Record<string, string> = {
  // A task idea's issue is created in backlog; the agent moves it to in_progress when it starts.
  backlog: "Starting", todo: "To do", in_progress: "In progress", in_review: "In review", blocked: "Blocked",
};

/** A running idea's progress line: a project's done/total tasks, or a task's status. */
export function runningLine(item: Pick<RunningIdea, "size" | "progress">): string {
  if (item.size === "task") {
    const status = Object.keys(item.progress ?? {})[0];
    return status ? (TASK_STATUS[status] ?? status.replace(/_/g, " ")) : "To do";
  }
  const progress = ideaProgress(item.progress);
  return progress ? `${progress.done} of ${progress.total} tasks done` : "Planning";
}

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

const EMPTY: IdeasResponse = { ideas: [], running: [] };

/**
 * The workspace's ideas. `scope` (user:org) keys the cache; null waits for auth. `dismiss` is
 * optimistic and puts the idea back when the server refuses; `start` refetches so the idea shows
 * as running.
 */
export function useIdeas(client: IdeasClient, scope: string | null) {
  const { data, error, isLoading, mutate } = useSWR<IdeasResponse>(
    scope ? ["ideas", scope] : null,
    () => client.listIdeas(),
    { revalidateOnFocus: false },
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
