import type { CanonicalRoomMessage } from "../messaging/types";

/**
 * The text of the first human reply to a card, or null while unanswered. Both clients
 * hold the room's messages, so no server field is needed: a click posts with
 * `replyToMessageId` = the card's id and the card's own thread root (reply.ts), and this
 * reads it back. The root must match too: a message typed in a top-level card's thread
 * also carries `replyToMessageId` = the card (the thread root), but it is not an answer.
 */
export function cardAnswerFor(
  messages: readonly CanonicalRoomMessage[],
  card: { messageId: string; threadRootMessageId: string | null },
): string | null {
  const reply = messages.find((m) => m.author.kind === "human" && !m.deletedAt
    && m.replyToMessageId === card.messageId && m.threadRootMessageId === card.threadRootMessageId);
  return reply ? reply.text : null;
}

/** Every answered card in one pass over the room: card message id -> the answer's text. */
export function cardAnswersFor(messages: readonly CanonicalRoomMessage[]): Map<string, string> {
  const answers = new Map<string, string>();
  for (const m of messages) {
    if (m.author.kind !== "agent") continue;
    const answer = cardAnswerFor(messages, m);
    if (answer !== null) answers.set(m.messageId, answer);
  }
  return answers;
}
