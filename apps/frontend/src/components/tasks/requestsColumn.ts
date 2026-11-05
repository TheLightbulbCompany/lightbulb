// A room's Requests column (beside the conversation, open by default): whether this person closed it. One choice
// per person on this browser, for every room. ponytail: localStorage; a server-side preference if it should follow
// the person across devices.

const KEY = "lightbulb.room.requestsOpen";

export function readRequestsOpen(viewerId: string): boolean {
  try { return localStorage.getItem(`${KEY}:${viewerId}`) !== "0"; } catch { return true; }
}

export function writeRequestsOpen(viewerId: string, open: boolean): void {
  try { localStorage.setItem(`${KEY}:${viewerId}`, open ? "1" : "0"); } catch { /* private mode: this session only */ }
}
