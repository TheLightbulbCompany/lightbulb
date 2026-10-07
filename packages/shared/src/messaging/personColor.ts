/**
 * One colour per person, the same in the terminal and in the session's chat. A port of the desktop
 * mod's `colorFor` (apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/rules.ts): FNV-1a over the
 * Clerk user id, mod the same ANSI palette in the same order. The session's own person is the mod's
 * OWNER_COLOR (gray), here "owner". Keep the two in step.
 */
export const PERSON_COLORS = ["cyan", "magenta", "green", "yellow", "blue", "blueBright"] as const;
export type PersonColor = (typeof PERSON_COLORS)[number] | "owner";

export function personColor(userId: string, sessionOwnerId: string): PersonColor {
  if (userId === sessionOwnerId) return "owner";
  let h = 0x811c9dc5;
  for (let i = 0; i < userId.length; i++) h = Math.imul(h ^ userId.charCodeAt(i), 0x01000193);
  return PERSON_COLORS[(h >>> 0) % PERSON_COLORS.length];
}
