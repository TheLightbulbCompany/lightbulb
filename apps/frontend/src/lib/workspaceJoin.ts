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
