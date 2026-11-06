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
  /** The idea's picture in the list; an agent's suggestion without one has 💡. */
  emoji: string;
  title: string;
  why: string;
  deliverables: string[];
  /** Catalog ideas only. */
  ourTurnaround?: string;
  yourPart?: string;
  /** An agent added it for this company ("For you"). */
  suggested?: boolean;
  /** Filed by the workspace's first run: one of the owner's "Pick a first move" ideas (UX slice 6). */
  firstRun?: boolean;
  agentId: string;
  agentName: string;
}

export interface IdeasResponse {
  ideas: Idea[];
  /** The workspace has started an idea (open or finished). */
  anyStarted?: boolean;
  /** The workspace has hidden at least one idea. */
  anyDismissed?: boolean;
  /** The first run's task: its ideas arrive as it finishes, so a change to it re-reads this list. */
  firstRunTaskId?: string;
}

export interface IdeaStart {
  roomId: string;
  projectId?: string;
  issueId?: string;
  /** Super-agent: the Router request the idea started (open it, not the DM). */
  taskId?: string;
}

export type IdeasClient = Pick<MessagingClient, "listIdeas" | "startIdea" | "dismissIdea">;

export type IdeaSectionKey = "for-you" | "money" | "grow" | "run" | "build";

/** The list's sections, in order (owner, 2026-10-15: show the team's range, not a flat menu).
 *  An agent's suggestion is "For you"; a catalog idea's section follows the role that owns it. */
const SECTIONS: { key: IdeaSectionKey; title: string; roles: readonly string[] }[] = [
  { key: "for-you", title: "For you", roles: [] },
  { key: "money", title: "Money", roles: ["finance"] },
  { key: "grow", title: "Grow", roles: ["growth"] },
  { key: "run", title: "Run the day", roles: ["customer_operations", "ceo"] },
  { key: "build", title: "Build", roles: ["engineering"] },
];

export interface IdeaSection {
  key: IdeaSectionKey;
  title: string;
  ideas: Idea[];
}

export const ideaSectionKey = (idea: Pick<Idea, "suggested" | "ownerRole">): IdeaSectionKey =>
  (!idea.suggested && SECTIONS.find((s) => s.roles.includes(idea.ownerRole ?? ""))?.key) || "for-you";

/** The ideas in their sections, each keeping the given order; empty sections left out. */
export function ideaSections(ideas: readonly Idea[]): IdeaSection[] {
  return SECTIONS
    .map(({ key, title }) => ({ key, title, ideas: ideas.filter((idea) => ideaSectionKey(idea) === key) }))
    .filter((section) => section.ideas.length > 0);
}

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
    firstRunTaskId: data?.firstRunTaskId,
    loading: isLoading,
    error: error as unknown,
    refresh: () => mutate(),
    start,
    dismiss,
  }), [data, isLoading, error, mutate, start, dismiss]);
}
