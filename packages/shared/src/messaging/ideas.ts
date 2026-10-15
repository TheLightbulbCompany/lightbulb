/**
 * Ideas (spec 2026-10-14 §4.4): jobs a company usually pays an agency for, done by the team. An
 * idea is a pre-written request (owner, 2026-10-15): Let's do it (POST /ideas/{id}/start) makes
 * its project or task in Work (by size; a quick idea makes nothing) and posts the person's own
 * message to the agent; the idea then leaves the list while that work is open. GET /ideas, start and
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

export interface IdeasResponse {
  ideas: Idea[];
  /** The workspace has started an idea (open or finished). */
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

/** The server's order (agency jobs first), with `agentId`'s ideas moved to the front (opened from its DM). */
export function orderIdeas(ideas: readonly Idea[], agentId?: string | null): Idea[] {
  if (!agentId) return [...ideas];
  return [...ideas.filter((i) => i.agentId === agentId), ...ideas.filter((i) => i.agentId !== agentId)];
}

const EMPTY: IdeasResponse = { ideas: [] };

/**
 * The workspace's ideas. `scope` (user:org) keys the cache; null waits for auth. `dismiss` is
 * optimistic and puts the idea back when the server refuses; `start` refetches, so a started
 * idea leaves the list.
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
    anyStarted: !!data?.anyStarted,
    anyDismissed: !!data?.anyDismissed,
    loading: isLoading,
    error: error as unknown,
    refresh: () => mutate(),
    start,
    dismiss,
  }), [data, isLoading, error, mutate, start, dismiss]);
}
