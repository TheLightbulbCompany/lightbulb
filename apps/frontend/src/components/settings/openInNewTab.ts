/**
 * Two-phase new-tab hand-off for the Stripe flows.
 *
 * A tab may only be opened from inside the user-activation task. `await`
 * ends that task: Safari blocks a `window.open` that runs after one, and
 * other browsers are inconsistent about it. So the tab is opened EMPTY and
 * synchronously in the click handler, then pointed at the URL once the API
 * has minted it — or closed again if the call fails.
 *
 * `window.open` returns `null` (it does not throw) when the popup is
 * blocked, which is how callers tell "opened" from "blocked" before treating
 * the hand-off as done — a blocked popup must not auto-approve a Teams
 * approval the owner never saw.
 *
 * The `noopener` window FEATURE cannot be used here: per spec it makes
 * `window.open` return `null` even on success, and it would also deny us the
 * handle this two-phase flow needs. Clearing `opener` on the returned window
 * gives the same severing with a usable return value.
 */
export function openPendingTab(): Window | null {
  const w = window.open("", "_blank");
  if (w) {
    w.opener = null;
  }
  return w;
}

export function navigatePendingTab(w: Window, url: string): void {
  w.location.href = url;
}

export function closePendingTab(w: Window | null): void {
  w?.close();
}
