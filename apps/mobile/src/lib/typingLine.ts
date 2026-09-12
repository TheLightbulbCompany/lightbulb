/**
 * The "X is typing…" line above a room's composer — the exact wording
 * `RoomPanel.tsx`'s `.room-typing` renders on web, copied verbatim (including
 * the "and others" overflow and the ellipsis character) so a phone reads the
 * same sentence a browser would for the same room.
 */
export function typingLine(typingMembers: readonly { displayName: string }[]): string {
  if (typingMembers.length === 0) return "";
  const names = typingMembers.slice(0, 3).map((member) => member.displayName).join(", ");
  const overflow = typingMembers.length > 3 ? " and others" : "";
  const verb = typingMembers.length === 1 ? "is" : "are";
  return `${names}${overflow} ${verb} typing…`;
}
