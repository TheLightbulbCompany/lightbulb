// The join page parks a workspace join-link token in this tab's sessionStorage
// while a signed-out holder authenticates. It never rides in a URL (#2615).
const PREFIX = "lightbulb:workspace-join:";

export const joinTokenKey = (orgId: string) => `${PREFIX}${orgId}`;

/** The org whose join token is parked in this tab, if any. */
export function pendingJoinOrgId(): string | null {
  try {
    const store = window.sessionStorage;
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (key?.startsWith(PREFIX) && store.getItem(key)) return key.slice(PREFIX.length);
    }
  } catch {
    // Storage blocked (private mode, some webviews): nothing can be parked.
  }
  return null;
}

/** A pasted join link → `/join/<orgId>#<token>`, or null. Accepts the full URL from any host
 *  (dev, prod, a copy from a chat app) or the bare path; the token rides in the fragment. */
export function joinLinkTarget(value: string): string | null {
  let url: URL;
  try { url = new URL(value.trim(), "https://placeholder.invalid"); } catch { return null; }
  return /^\/join\/org_[A-Za-z0-9]+\/?$/.test(url.pathname) && url.hash.length > 1 ? `${url.pathname.replace(/\/$/, "")}${url.hash}` : null;
}
