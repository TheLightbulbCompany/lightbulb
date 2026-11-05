export function messagePermalink(roomId: string, messageId: string, origin = window.location.origin): string {
  const params = new URLSearchParams({ panel: "chat", room: roomId, message: messageId });
  return `${origin}/chat?${params.toString()}`;
}

export function roomPermalink(roomId: string, origin = window.location.origin): string {
  const params = new URLSearchParams({ panel: "chat", room: roomId });
  return `${origin}/chat?${params.toString()}`;
}

/** A super-agent thread: the pane beside the chat (`?panel=task-detail&taskId=`). */
export function taskPermalink(taskId: string, origin = window.location.origin): string {
  const params = new URLSearchParams({ panel: "task-detail", taskId });
  return `${origin}/chat?${params.toString()}`;
}

export function sessionPermalink(sessionId: string, origin = window.location.origin): string {
  return `${origin}/chat?${new URLSearchParams({ panel: "session", session: sessionId })}`;
}

// Sessions are workspace-scoped: a `room` param (links minted while sessions
// lived in rooms) is accepted and ignored. Also accepts the legacy
// `panel=chat` form (links minted before the session dock panel shipped).
export function parseSessionPermalink(href: string, origin = window.location.origin): { sessionId: string } | null {
  try {
    const url = new URL(href, origin);
    const p = url.searchParams;
    const keys = [...p.keys()].sort().join(",");
    if (url.origin !== origin || url.pathname !== "/chat" || url.hash || !["session", "chat"].includes(p.get("panel") ?? "")
      || (keys !== "panel,session" && keys !== "panel,room,session")) return null;
    const sessionId = p.get("session");
    return sessionId ? { sessionId } : null;
  } catch { return null; }
}

/** Why a workspace join link could not be copied: the row exists but its plaintext cannot be
 *  shown (a legacy hash-only row, or a token the current key cannot decrypt) — only a deliberate
 *  Regenerate in Settings → Members replaces it, since that turns the old link off for everyone —
 *  or the browser refused the clipboard write. */
export class JoinLinkCopyError extends Error {
  constructor(public readonly reason: "unavailable" | "clipboard") { super(reason); }
}

/** Copies text that is still being fetched. Started inside the click, so Safari and the desktop
 *  webview still count the press: they refuse a clipboard write that begins after an await. */
export async function copyTextWhenReady(text: Promise<string>): Promise<boolean> {
  try {
    if (typeof ClipboardItem === "undefined" || !navigator.clipboard?.write) return copyText(await text);
    const blob = text.then(t => { if (!t) throw new Error("nothing to copy"); return new Blob([t], { type: "text/plain" }); });
    await navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
    return true;
  } catch {
    return false;
  }
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
