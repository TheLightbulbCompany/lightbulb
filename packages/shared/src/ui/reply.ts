/** The text a card click posts as the person. Plain words: the agent reads it as any message. */

import type { CreateMessageRequest } from "../messaging/types";

export interface CardReply {
  text: string;
  /** The card's author: mentioned so the reply wakes it as addressed. */
  agentId: string;
  /** The card's message: `replyToMessageId`, which is also how the card learns it was answered. */
  messageId: string;
  /** The card's own thread, copied so the answer lands where the card is. */
  threadRootMessageId: string | null;
  /** One id per (card, text) until it lands, so a retry after an uncertain send never posts twice. */
  clientMessageId: string;
}

/** The message a press sends: the words as the person, addressed to the card's author, under the card. */
export function cardReplyRequest(reply: CardReply): CreateMessageRequest {
  return {
    clientMessageId: reply.clientMessageId,
    text: reply.text,
    mentions: [{ kind: "agent", id: reply.agentId }],
    replyToMessageId: reply.messageId,
    threadRootMessageId: reply.threadRootMessageId,
  };
}

export function choiceReply(picked: readonly string[]): string {
  return picked.map((p) => p.trim()).filter(Boolean).join(", ");
}

export function formReply(fields: readonly { label: string; value: string }[]): string {
  return fields
    .map((f) => ({ label: f.label.trim(), value: f.value.trim() }))
    .filter((f) => f.value)
    .map((f) => `${f.label}: ${f.value}`)
    .join("\n");
}

const cell = (value: string) => value.replace(/\|/g, "\\|").replace(/\r?\n/g, " ").trim();

export function tableReply(columns: readonly string[], rows: readonly (readonly string[])[]): string {
  const line = (cells: readonly string[]) => `| ${cells.map(cell).join(" | ")} |`;
  return [line(columns), `| ${columns.map(() => "---").join(" | ")} |`, ...rows.map(line)].join("\n");
}
