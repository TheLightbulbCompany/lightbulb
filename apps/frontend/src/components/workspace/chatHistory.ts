// Room, DM and session picks join the shell's URL command channel so browser
// history — and the rail's Back/Forward — steps through them. This module only
// WRITES the URL; WorkspaceShell's reconciler stays the one executor and replays
// it (spec §6: the URL is a one-way command channel). Desktop shell only: the
// messaging provider is shared with the mobile web shell and must not write URLs.
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";
import { parseCommand } from "./urlChannel";

export interface ChatView {
  roomId: string | null;
  sessionId: string | null;
}

export function chatViewSearch(view: ChatView): string | null {
  if (view.sessionId) return new URLSearchParams({ panel: "chat", session: view.sessionId }).toString();
  if (view.roomId) return new URLSearchParams({ panel: "chat", room: view.roomId }).toString();
  return null;
}

function params(search: string): URLSearchParams {
  return new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
}

function urlShows(search: string, view: ChatView): boolean {
  const p = params(search);
  const panel = p.get("panel");
  // ?panel=session&session= is the session link's own form of the same view.
  if (view.sessionId) return (panel === "chat" || panel === "session") && p.get("session") === view.sessionId;
  return panel === "chat" && !p.get("session") && p.get("room") === view.roomId;
}

const sameView = (a: ChatView, b: ChatView) => a.roomId === b.roomId && a.sessionId === b.sessionId;

export function planChatHistoryWrite(
  prev: ChatView | null,
  next: ChatView,
  currentSearch: string,
): { replace?: string; push?: string } | null {
  const target = chatViewSearch(next);
  if (!target || urlShows(currentSearch, next)) return null;
  const p = params(currentSearch);
  // An agent deep link (?agent= or ?panel=chat&agent=) just resolved to this
  // room: same navigation, so it is rewritten in place, not stacked.
  if ((p.get("panel") === "chat" || (!p.get("panel") && p.get("agent"))) && !p.get("room") && !p.get("session")) {
    return { replace: target };
  }
  // A bare /chat entry carries no command, so Back onto it would show nothing
  // new; it becomes the view it was actually showing.
  const replace = prev && !sameView(prev, next) && parseCommand(currentSearch) === null
    ? chatViewSearch(prev)
    : null;
  return replace ? { replace, push: target } : { push: target };
}

// Module state, like workspaceController's dock singleton: one desktop shell per page.
let current: ChatView | null = null;

function apply(write: { replace?: string; push?: string }): void {
  if (write.replace) replaceSearchParams(new URLSearchParams(write.replace));
  if (write.push) pushSearchParams(new URLSearchParams(write.push));
}

/** `explicit`: the person picked this view. Automatic picks are only remembered —
 *  writing them would replay chooseRoom, mark an explicit choice, and stop a fresh
 *  workspace auto-opening #general. */
export function recordChatView(next: ChatView, explicit: boolean): void {
  if (!chatViewSearch(next)) return;
  const prev = current;
  current = next;
  if (!explicit) {
    // A closed session must not reopen on reload: the URL becomes the room it left.
    if (prev?.sessionId && !next.sessionId && urlShows(window.location.search, prev)) {
      replaceSearchParams(new URLSearchParams(chatViewSearch(next)!));
    }
    return;
  }
  const write = planChatHistoryWrite(prev, next, window.location.search);
  if (write) apply(write);
}

/** The rail's Messages button is a navigation to the chat already open. */
export function recordMessagesNavigation(): void {
  if (!current) return;
  // A bare /chat already shows this chat: name it in place rather than stack a duplicate.
  if (parseCommand(window.location.search) === null) {
    replaceSearchParams(new URLSearchParams(chatViewSearch(current)!));
    return;
  }
  const write = planChatHistoryWrite(current, current, window.location.search);
  if (write) apply(write);
}

export function resetChatView(): void {
  current = null;
}
