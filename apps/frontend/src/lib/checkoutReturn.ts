// "This tab just came back from Stripe Checkout", remembered past the moment the shell strips ?checkout=success from
// the URL: a screen that mounts later (the onboarding ChatGPT step after the provider picker) still knows the plan was
// paid for and waits for the webhook instead of selling the plan again. sessionStorage: this tab only, short-lived.

const KEY = "lb:checkout-return";
const TTL_MS = 10 * 60_000;

export function markCheckoutReturn(now = Date.now()): void {
  try { sessionStorage.setItem(KEY, String(now)); } catch { /* storage blocked: the URL signal still works */ }
}

export function recentCheckoutReturn(now = Date.now()): boolean {
  try {
    const at = Number(sessionStorage.getItem(KEY));
    return Number.isFinite(at) && at > 0 && now - at < TTL_MS;
  } catch {
    return false;
  }
}
