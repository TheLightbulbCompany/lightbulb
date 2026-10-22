import type { LocalSession } from "./types";

/** A session that can never have a terminal: it runs inside an app, an IDE or an SDK, and is shared as its
 *  conversation. The Mac says `app`; one older than 2026-10-08 said `unknown`. */
export function conversationOnly(session: LocalSession): boolean {
  return !session.terminal && (session.terminalReason === "app" || session.terminalReason === "sdk" || session.terminalReason === "unknown");
}

/** Providers that only ever run in a terminal this app cannot show or open. */
export const CLI_ONLY: ReadonlySet<string> = new Set(["cursor", "grok"]);

/** What a running session with no terminal says, the same on the web, the Mac and the phone. A session that
 *  never has one says so above its conversation. Any other reached the workspace from an older Mac app (a
 *  current one shares a terminal session only once its terminal is live), so nobody gets a step to take. */
export function sessionScreenNotice(session: LocalSession, own: boolean, ownerName: string): { title: string; body: string } {
  // Cursor and Grok CLI run in the person's own terminal and share by hooks: there is never a live terminal to show.
  if (CLI_ONLY.has(session.provider)) return { title: "This session runs in the person's own terminal, and its live terminal is not shared.", body: "This is its conversation." };
  if (conversationOnly(session)) return { title: "This session runs inside an app, so there is no terminal to show.", body: "This is its conversation." };
  return own
    // Only me: nobody else sees anything of it, so the teammates clause would be false.
    ? { title: "This session's screen is not shared.", body: `Lightbulb cannot show this session's screen.${session.shared === false ? "" : " Teammates still see its status and what it changed."}` }
    : { title: `${ownerName} is working in this session, but its screen is not shared.`, body: "You can still follow its status and what it changed." };
}
