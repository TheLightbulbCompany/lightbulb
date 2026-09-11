import type { RoomMessage } from "./types";

export const GROUP_WINDOW_MS = 5 * 60 * 1000;

export type TimelineItem =
  | { kind: "date"; key: string; label: string; date: Date }
  | { kind: "new"; key: string }
  | { kind: "message"; key: string; message: RoomMessage; collapsed: boolean };

const dayKey = (date: Date) => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;

export function dateDividerLabel(date: Date, now: Date): string {
  const startOf = (value: Date) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(date)) / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days > 1 && days < 7) return date.toLocaleDateString(undefined, { weekday: "long" });
  return date.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

export function buildTimeline(messages: readonly RoomMessage[], { readSeq, now = new Date() }: { readSeq: number | null; now?: Date }): TimelineItem[] {
  const items: TimelineItem[] = [];
  let lastDay: string | null = null;
  let previous: RoomMessage | null = null;
  let newLineDone = readSeq === null;
  for (const message of messages) {
    const created = new Date(message.createdAt);
    const valid = !Number.isNaN(created.getTime());
    let boundary = false;
    if (valid && dayKey(created) !== lastDay) {
      lastDay = dayKey(created);
      items.push({ kind: "date", key: `date:${lastDay}`, label: dateDividerLabel(created, now), date: created });
      boundary = true;
    }
    if (!newLineDone && readSeq !== null && message.messageSeq > readSeq) {
      items.push({ kind: "new", key: "new" });
      newLineDone = true;
      boundary = true;
    }
    const collapsed = !boundary && previous !== null && !message.deletedAt && !previous.deletedAt
      && previous.author.kind === message.author.kind && previous.author.id === message.author.id
      && valid && created.getTime() - new Date(previous.createdAt).getTime() <= GROUP_WINDOW_MS;
    items.push({ kind: "message", key: `message:${message.messageId}`, message, collapsed });
    previous = message;
  }
  return items;
}
