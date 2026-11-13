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
