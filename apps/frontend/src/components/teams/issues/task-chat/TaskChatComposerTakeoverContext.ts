// apps/frontend/src/components/teams/issues/task-chat/TaskChatComposerTakeoverContext.ts

// Ported verbatim from upstream Paperclip's composer takeover context
// (paperclip/ui/src/components/task-chat/TaskChatComposerTakeoverContext.ts @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). A pending interaction "takes over" the composer;
// the card portals its title and pagination into the takeover's shared top row
// and reads the composer's Skip button from here.

import {
  createContext,
  useContext,
  useLayoutEffect,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

interface TaskChatComposerTakeoverActions {
  skipButton: ReactNode;
  /** Hides the takeover without resolving it, returning the plain composer. */
  dismiss: () => void;
  headerSlot: HTMLElement | null;
  controlsSlot: HTMLElement | null;
  setHeaderClaimed: (claimed: boolean) => void;
}

export const TaskChatComposerTakeoverActionsContext =
  createContext<TaskChatComposerTakeoverActions | null>(null);

export function useTaskChatComposerTakeoverActions(): TaskChatComposerTakeoverActions | null {
  return useContext(TaskChatComposerTakeoverActionsContext);
}

/** Moves a takeover's semantic title into the composer's shared top row. */
export function TaskChatComposerTakeoverHeader({
  children,
}: {
  children: ReactNode;
}) {
  const actions = useTaskChatComposerTakeoverActions();
  useLayoutEffect(() => {
    if (!actions) return;
    actions.setHeaderClaimed(true);
    return () => actions.setHeaderClaimed(false);
  }, [actions?.setHeaderClaimed]);

  if (!actions) return children;
  return actions.headerSlot ? createPortal(children, actions.headerSlot) : null;
}

/** Moves pagination or similar compact controls beside pending count and X. */
export function TaskChatComposerTakeoverControls({
  children,
}: {
  children: ReactNode;
}) {
  const actions = useTaskChatComposerTakeoverActions();
  if (!actions) return children;
  return actions.controlsSlot
    ? createPortal(children, actions.controlsSlot)
    : null;
}
