"use client";

import { createContext, useContext } from "react";
import type { MessageAttachment } from "@lightbulb/shared";

/**
 * What a card needs from the message it sits in. Provided by RoomMessageItem for an
 * AGENT's message only: without a provider a fence is plain code (a person's paste can
 * never become live buttons), and a card shown outside a room (a session transcript, a
 * document) renders read-only.
 */
export interface CardContextValue {
  messageId: string;
  roomId: string;
  authorId: string;
  threadRootMessageId: string | null;
  attachments: readonly MessageAttachment[];
  /** The person's answer once one exists: controls disable and show it. */
  answered: string | null;
  /** Posts `text` as the person in reply to this card; a retry passes the same id. Absent in a read-only place. */
  onReply?: (text: string, clientMessageId: string) => Promise<void>;
  attachmentUrl?: (roomId: string, attachmentId: string) => Promise<{ url: string }>;
}

export const CardContext = createContext<CardContextValue | null>(null);

export function useCardContext(): CardContextValue | null {
  return useContext(CardContext);
}

/** True when a control may post: there is a reply path and nobody answered yet. */
export function useCardLive(): boolean {
  const ctx = useCardContext();
  return !!ctx?.onReply && ctx.answered === null;
}
