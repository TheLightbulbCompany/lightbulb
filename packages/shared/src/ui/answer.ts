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

const replyKey = (replyTo: string, root: string | null) => `${replyTo}\u0000${root ?? ""}`;

/**
 * Every answered card in the room: card message id -> the answer's text. Two linear passes
 * (the first human reply per (reply-to, thread root), then the agent messages), because both
 * room surfaces rebuild this on every message change over every page a viewer has loaded.
 */
export function cardAnswersFor(messages: readonly CanonicalRoomMessage[]): Map<string, string> {
  const firstReply = new Map<string, string>();
  for (const m of messages) {
    if (m.author.kind !== "human" || m.deletedAt || m.replyToMessageId === null) continue;
    const key = replyKey(m.replyToMessageId, m.threadRootMessageId);
    if (!firstReply.has(key)) firstReply.set(key, m.text);
  }
  const answers = new Map<string, string>();
  if (firstReply.size === 0) return answers;
  for (const m of messages) {
    if (m.author.kind !== "agent") continue;
    const answer = firstReply.get(replyKey(m.messageId, m.threadRootMessageId));
    if (answer !== undefined) answers.set(m.messageId, answer);
  }
  return answers;
}

const norm = (s: string) => s.trim();

/**
 * Whether a message's answer came from THIS group of options. The answer is the message's
 * (one per card), so a Form's "Sent." must not make the FollowUps beside it say "You picked
 * Your name: Ada", and a Choice's pick must not make the Form beside it read "Sent.".
 */
export function answerIsFrom(answered: string | null, options: readonly string[], multi = false): boolean {
  if (answered === null) return false;
  const opts = new Set(options.map(norm));
  if (!multi) return opts.has(norm(answered));
  const parts = answered.split(",").map(norm).filter(Boolean);
  return parts.length > 0 && parts.every((p) => opts.has(p));
}

/** A Form's reply is `Label: value` lines (reply.ts); it is this form's when every line names one of its labels. */
export function answerIsFormOf(answered: string | null, labels: readonly string[]): boolean {
  if (answered === null) return false;
  const known = new Set(labels.map(norm));
  const lines = answered.split("\n").map(norm).filter(Boolean);
  return lines.length > 0 && lines.every((l) => known.has(norm(l.slice(0, l.indexOf(":") < 0 ? 0 : l.indexOf(":")))));
}

/** An EditableTable's reply is a markdown table (reply.ts); it is this table's when the header row is its columns. */
export function answerIsTableOf(answered: string | null, columns: readonly string[]): boolean {
  if (answered === null) return false;
  const first = answered.split("\n")[0] ?? "";
  return first.startsWith("|") && columns.every((c) => first.includes(`| ${norm(c)} `) || first.includes(`| ${norm(c)} |`));
}
