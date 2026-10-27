"use client";

import { useEffect, useRef, useState } from "react";
import { MessagingConversation } from "@/components/messaging/MessagingWorkspace";
import { useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { useAgents } from "@/hooks/useAgents";
import type { PanelProps } from "../panelComponents";
import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";
import { syncMessagesPanelAgent } from "../workspaceController";
import { cancelFocusComposerRequest, releaseFocusComposerRequest } from "@/lib/focusComposerRequest";

interface ChatPanelParams {
  agentId?: string;
  agentSelection?: "resolved";
  roomId?: string;
  messageId?: string;
  sessionId?: string;
  /** A turn of that session to open its conversation at. */
  seq?: string;
  sessionIntent?: string;
}

/**
 * Agent parameters are one-shot selection intents for the shared conversation.
 * Room changes must not snap back to the last agent requested by a deep link.
 */
export function ChatPanel(props: PanelProps): React.ReactElement {
  const params = props.params as ChatPanelParams;
  const { defaultId } = useAgents();
  const { selectAgentDm, roomProps, openMessage, openSession, freshLanding, activeRoomId, sessionFocus } = useMessagingWorkspace();
  const lastRequested = useRef<string | null>(null);
  const navigationRequest = useRef(0);
  // The agent the conversation mounted with is a cold load, not a pick.
  const restoredAgent = useRef<string | undefined>(params.agentId);
  const [focusDestination, setFocusDestination] = useState<{ agentId: string; roomId: string } | null>(null);
  const deliveredDestination = useRef<typeof focusDestination>(null);
  // A room permalink wins over any agent-selection request riding along with
  // it (durable params can transiently coexist during a param merge).
  // The default-agent fallback is for returning workspaces only: a fresh one
  // lands in #general (the provider opens it), and nothing is decided until
  // the room list's first load says which kind this is.
  const defaultRequest = params.agentId === DEFAULT_AGENT_SENTINEL;
  const requested = params.roomId || params.sessionId || params.agentSelection === "resolved" ? null
    : defaultRequest
      ? freshLanding === false ? defaultId ?? null : null
      : params.agentId ?? null;

  useEffect(() => {
    const restored = restoredAgent.current === requested;
    restoredAgent.current = undefined;
    if (!requested) {
      lastRequested.current = null;
      return;
    }
    if (lastRequested.current === requested) return;
    lastRequested.current = requested;
    const request = ++navigationRequest.current;
    void selectAgentDm(requested, !defaultRequest && !restored).then((roomId) => {
      if (navigationRequest.current !== request) return;
      if (roomId) setFocusDestination({ agentId: requested, roomId });
      else cancelFocusComposerRequest(requested);
    }).catch(() => {
      if (navigationRequest.current === request) {
        cancelFocusComposerRequest(requested);
        lastRequested.current = null;
      }
    });
  }, [requested, selectAgentDm, defaultRequest]);

  const lastOpenedRoom = useRef<string | null>(null);
  useEffect(() => {
    if (!params.roomId && !params.sessionId) {
      lastOpenedRoom.current = null;
      return;
    }
    const key = JSON.stringify([params.roomId, params.messageId, params.sessionId, params.seq, params.sessionIntent]);
    if (lastOpenedRoom.current === key) return;
    lastOpenedRoom.current = key;
    // A pending agent-DM focus must not supersede a room intent (a skipped replay is not one).
    if (params.sessionId) {
      ++navigationRequest.current;
      openSession(params.sessionId, false, Number(params.seq) || undefined);
    // The replay of a room the person just picked (ChatHistorySync wrote its URL)
    // must not re-open it: that would clear a scroll target and close a session view.
    } else if (params.roomId && (params.messageId || params.roomId !== activeRoomId || sessionFocus)) {
      ++navigationRequest.current;
      openMessage(params.roomId, params.messageId ?? null);
    }
  }, [params.roomId, params.messageId, params.sessionId, params.seq, params.sessionIntent, openMessage, openSession, activeRoomId, sessionFocus]);

  const roomId = roomProps?.roomId;
  const roomAgentId = roomProps?.composer.focusAgentId;
  const waitingForDefault = params.agentSelection !== "resolved" && defaultRequest
    && (freshLanding === null || (!freshLanding && !defaultId));
  useEffect(() => {
    // The initial fallback room must not consume a still-unresolved deep link.
    if (roomId && !waitingForDefault) syncMessagesPanelAgent(roomAgentId);
  }, [roomId, roomAgentId, params.agentId, params.agentSelection, waitingForDefault]);

  useEffect(() => {
    if (!focusDestination || focusDestination === deliveredDestination.current || roomId !== focusDestination.roomId) return;
    deliveredDestination.current = focusDestination;
    if (releaseFocusComposerRequest(focusDestination.agentId)) {
      window.dispatchEvent(new CustomEvent("getstarted:focus-composer", {
        detail: { agentId: focusDestination.agentId },
      }));
    }
  }, [focusDestination, roomId]);

  return <MessagingConversation />;
}
