import type { LocalSession } from "./types";

/** Which explicit state a running session without a terminal shows (spec 2026-10-07 §4 f, g).
 *  A Mac older than `terminalReason` sends none: a plain Claude still reads as `plain`. */
export function terminalStateReason(session: LocalSession): "plain" | "no-daemon" | null {
  if (session.terminal || session.status === "ended" || session.status === "offline") return null;
  if (session.terminalReason === "plain" || session.terminalReason === "no-daemon") return session.terminalReason;
  return !session.terminalReason && session.provider === "claude" ? "plain" : null;
}

/** What a running session with no screen says, the same on the web, the Mac and the phone (ISOA-731).
 *  A teammate never gets terminal instructions; the owner gets the meaning first, then the one step
 *  that only they can do, in their own terminal (no surface can attach a plain session for them). */
export function sessionScreenNotice(session: LocalSession, own: boolean, ownerName: string): { title: string; body: string; how?: string; settings?: string } {
  const reason = terminalStateReason(session);
  if (!own) return {
    title: `${ownerName} is working in this session, but its screen is not shared.`,
    body: reason
      ? `Its screen shows in Lightbulb as soon as ${ownerName} shares it. Until then you can follow its status and what it changed.`
      : "You can still follow its status and what it changed.",
  };
  if (!reason) return { title: "This session's screen is not shared.", body: "Lightbulb cannot show this session's screen. Teammates still see its status and what it changed." };
  return {
    title: "This session's screen is not shared yet.",
    body: "Teammates can see that it is running, but not what is on its screen.",
    how: reason === "plain"
      ? "To share it, go to the terminal window where it runs and press the left arrow key (←) once. Teammates can then watch it live."
      : "To share it, close it and start it again by typing lightbulb codex in your terminal. Teammates can then watch it live.",
    ...(reason === "plain" ? { settings: "To share every new session without this step, turn on Share every new session in Settings." } : {}),
  };
}
