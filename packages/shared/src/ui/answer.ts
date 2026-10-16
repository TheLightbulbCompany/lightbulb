import type { CanonicalRoomMessage } from "../messaging/types";

/**
 * The text of the first human reply to a card, or null while unanswered. Both clients
 * hold the room's messages, so no server field is needed: a click posts with
 * `replyToMessageId` = the card's id (reply.ts), and this reads it back.
 */
export function cardAnswerFor(messages: readonly CanonicalRoomMessage[], messageId: string): string | null {
  const reply = messages.find((m) => m.author.kind === "human" && m.replyToMessageId === messageId && !m.deletedAt);
  return reply ? reply.text : null;
}
