import type { LocalSession } from "./types";

/** Which explicit state a running session without a terminal shows (spec 2026-10-07 §4 f, g).
 *  A Mac older than `terminalReason` sends none: a plain Claude still reads as `plain`. */
export function terminalStateReason(session: LocalSession): "plain" | "no-daemon" | null {
  if (session.terminal || session.status === "ended" || session.status === "offline") return null;
  if (session.terminalReason === "plain" || session.terminalReason === "no-daemon") return session.terminalReason;
  return !session.terminalReason && session.provider === "claude" ? "plain" : null;
}
