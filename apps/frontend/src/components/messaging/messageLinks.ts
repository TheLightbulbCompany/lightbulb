export function messagePermalink(roomId: string, messageId: string, origin = window.location.origin): string {
  const params = new URLSearchParams({ panel: "chat", room: roomId, message: messageId });
  return `${origin}/chat?${params.toString()}`;
}

export function roomPermalink(roomId: string, origin = window.location.origin): string {
  const params = new URLSearchParams({ panel: "chat", room: roomId });
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

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
