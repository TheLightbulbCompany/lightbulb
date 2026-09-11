export function messagePermalink(roomId: string, messageId: string, origin = window.location.origin): string {
  const params = new URLSearchParams({ panel: "chat", room: roomId, message: messageId });
  return `${origin}/chat?${params.toString()}`;
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
