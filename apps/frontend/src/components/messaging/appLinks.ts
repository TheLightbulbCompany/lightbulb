import { isDocumentLink } from "@lightbulb/shared";
import { shellUrlFromPaperclipPath } from "@/lib/shellNav";
import { parseSessionPermalink } from "./messageLinks";

// Our own product: thelightbulb.company and every subdomain (app, dev., company[-dev].).
// Both the product domain and the retired one (which redirects to it).
const OWN_HOST = /(^|\.)(thelightbulb\.company|flickmeon\.ai)$/i;

/** What a link to our own app points at, resolved to the native in-app card for it. */
export type AppLink = { href: string } & (
  | { kind: "document" }
  | { kind: "agent"; id: string }
  | { kind: "message"; roomId: string; messageId: string }
  | { kind: "room"; roomId: string }
  | { kind: "session"; sessionId: string }
  | { kind: "connect"; provider: string }
  | { kind: "invite" }
  | { kind: "other" }
);

/**
 * An absolute link to our own product (or a relative Paperclip path like
 * `/ISOA/issues/ISOA-1`) → its in-shell URL and what it points at; null for
 * anything else (an external site). `href` is always an in-shell `/chat?…`
 * URL so a click stays in the app instead of opening a browser tab.
 */
export function appLinkTarget(raw: string | null | undefined, origin = typeof window === "undefined" ? "" : window.location.origin): AppLink | null {
  if (!raw) return null;
  let url: URL;
  try {
    url = new URL(raw, origin || "https://thelightbulb.company");
  } catch {
    return null;
  }
  const own = OWN_HOST.test(url.hostname) || (!!origin && url.origin === origin);
  const relative = raw.startsWith("/");
  if (!own && !relative) return null;
  const path = `${url.pathname}${url.search}${url.hash}`;
  const document = isDocumentLink(raw) || isDocumentLink(path);
  const shell = shellUrlFromPaperclipPath(path) ?? (url.pathname === "/chat" ? path : null);
  if (!shell) return relative ? null : { kind: "other", href: url.href };
  if (document) return { kind: "document", href: shell };
  const shellUrl = new URL(shell, "https://thelightbulb.company");
  const p = shellUrl.searchParams;
  const panel = p.get("panel");
  if (panel === "connections" && p.get("provider")) return { kind: "connect", provider: p.get("provider")!, href: shell };
  if (panel === "invite") return { kind: "invite", href: shell };
  if (panel === "agent-inspector" && p.get("agent")) return { kind: "agent", id: p.get("agent")!, href: shell };
  const session = parseSessionPermalink(shellUrl.href, shellUrl.origin);
  if (session) return { kind: "session", sessionId: session.sessionId, href: shell };
  const roomId = p.get("room");
  if (panel === "chat" && roomId) {
    const messageId = p.get("message");
    return messageId ? { kind: "message", roomId, messageId, href: shell } : { kind: "room", roomId, href: shell };
  }
  return { kind: "other", href: shell };
}

/** Every own-app link in a message's text, in order, de-duplicated by target. */
export function extractAppLinks(text: string, max = 3): AppLink[] {
  const links: AppLink[] = [];
  // A link inside a fenced block belongs to the block (a Sources card draws its own); no chip for it.
  for (const [match] of text.replace(/(`{3,}|~{3,})[\s\S]*?(\1|$)/g, " ").matchAll(/https?:\/\/[^\s<>()[\]"'`]+/gi)) {
    const link = appLinkTarget(match.replace(/[.,;:!?*_~]+$/, ""));
    if (!link || links.some((l) => l.href === link.href)) continue;
    links.push(link);
    if (links.length >= max) break;
  }
  return links;
}
