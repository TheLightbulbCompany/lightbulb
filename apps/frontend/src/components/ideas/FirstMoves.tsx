"use client";

// First run (UX slice 6): the ideas the workspace's first run filed are the owner's first decision. They sit in the
// Lightbulb DM as cards (what, why, who; Not now · Do it), and while any is still open the one needs-you count holds
// one more item, "Pick a first move", that opens the DM.
// ponytail: "open" = still listed. GET /ideas hides a started idea only while its task is open, so one whose task has
// finished can count again; a per-idea "decided" mark on the server would close that.

import { useEffect, type ReactElement } from "react";
import { useAuth } from "@clerk/nextjs";
import { useIdeas as useSharedIdeas, type Idea, type IdeasClient } from "@lightbulb/shared";
import { IdeaItem } from "@/components/ideas/HomeIdeas";
import { useIdeas } from "@/hooks/useIdeas";
import { useOptionalLiveEvents } from "@/hooks/useLiveEvents";
import { openChatPanelWithDraft } from "@/components/workspace/workspaceController";
import "@/components/projects/projects.css";

const NONE: Idea[] = [];
export const FIRST_MOVE_LABEL = "Pick a first move";

/** The first run's ideas still open. The list is re-read when the first run's
 *  task changes, since its ideas arrive as it finishes. Shares the ideas cache with `useIdeas`. */
export function useFirstMoves(): Idea[] {
  const live = useOptionalLiveEvents();
  const { isLoaded, userId, orgId, orgRole } = useAuth();
  // The owner's decision only: a workspace admin (a personal workspace is its own). Nobody else sees the cards or the +1.
  const owner = !orgId || orgRole === "org:admin";
  const key = owner && live?.messaging && isLoaded && userId ? `${userId}:${orgId ?? userId}` : null;
  // Without the live provider the key is null, so the client is never called.
  const ideas = useSharedIdeas(live?.messaging as IdeasClient, key);
  const { firstRunTaskId, refresh } = ideas;
  const onEvent = live?.onEvent;
  useEffect(() => {
    if (!onEvent || !firstRunTaskId) return;
    return onEvent((event) => {
      if (event.type === "task.changed" && event.payload.taskId === firstRunTaskId) void refresh();
    });
  }, [onEvent, firstRunTaskId, refresh]);
  const moves = ideas.ideas.filter((idea) => idea.firstRun);
  return key && moves.length ? moves : NONE;
}

/** Opens the Lightbulb DM, where the first moves are. */
export const openFirstMoves = () => { openChatPanelWithDraft(""); };

/** In the Lightbulb DM, under the conversation: Lightbulb's line and one card per first move. */
export function FirstMoves(): ReactElement | null {
  const moves = useFirstMoves();
  return moves.length ? <FirstMovesList moves={moves} /> : null;
}

function FirstMovesList({ moves }: { moves: readonly Idea[] }): ReactElement {
  const { startAndOpen, dismiss } = useIdeas();
  return <section className="first-moves px-4 py-2" aria-label="First moves">
    <p><span className="font-semibold">Lightbulb</span></p>
    <p>{moves.length === 1 ? "One first move left. Pick it and I'll start it:" : `${moves.length === 3 ? "Three" : "Two"} first moves for you. Pick one and I'll start it:`}</p>
    <ul>{moves.map((idea) => <IdeaItem key={idea.id} idea={idea} onStart={startAndOpen} onDismiss={dismiss} />)}</ul>
  </section>;
}
