import { typingLine as fromNames } from "@lightbulb/shared";

/**
 * The "X is typing…" line above a room's composer — the exact wording
 * `RoomPanel.tsx`'s `.room-typing` renders on web, copied verbatim (including
 * the "and others" overflow and the ellipsis character) so a phone reads the
 * same sentence a browser would for the same room. Delegates to the shared
 * implementation (also used by `useRoomActivity`) so there is one wording, one place.
 */
export const typingLine = (typingMembers: readonly { displayName: string }[]) =>
  fromNames(typingMembers.map((m) => m.displayName));
