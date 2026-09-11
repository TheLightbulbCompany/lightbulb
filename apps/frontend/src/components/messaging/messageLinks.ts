export function messagePermalink(roomId: string, messageId: string, origin = window.location.origin): string {
  const params = new URLSearchParams({ panel: "chat", room: roomId, message: messageId });
  return `${origin}/chat?${params.toString()}`;
}

export function sessionPermalink(roomId: string, sessionId: string, origin = window.location.origin): string {
  return `${origin}/chat?${new URLSearchParams({ panel: "chat", room: roomId, session: sessionId })}`;
}

export function parseSessionPermalink(href: string, origin = window.location.origin): { roomId: string; sessionId: string } | null {
  try {
    const url = new URL(href, origin);
    const p = url.searchParams;
    if (url.origin !== origin || url.pathname !== "/chat" || url.hash || p.get("panel") !== "chat"
      || [...p.keys()].sort().join(",") !== "panel,room,session") return null;
    const roomId = p.get("room"), sessionId = p.get("session");
    return roomId && sessionId ? { roomId, sessionId } : null;
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
