import Stripe from 'stripe';

const POLL_INTERVAL_MS = 5000;

function getStripe(): Stripe {
  const stripeKey = process.env.STRIPE_SECRET_KEY;
  if (!stripeKey) throw new Error('STRIPE_SECRET_KEY env var is required');
  return new Stripe(stripeKey, {
    apiVersion: '2025-01-27.acacia' as Stripe.StripeConfig['apiVersion'],
  });
}

let _stripe: Stripe | undefined;
function stripe(): Stripe {
  if (!_stripe) _stripe = getStripe();
  return _stripe;
}

/**
 * Cancel any active/trialing/incomplete subscriptions for a given email.
 * Safe to call when no subscription exists (no-op).
 */
export async function cancelSubscriptionIfExists(email: string): Promise<void> {
  const customers = await stripe().customers.list({ email, limit: 100 });
  if (customers.data.length === 0) return; // no customer → nothing to cancel
  for (const customer of customers.data) {
    // `status: 'all'` returns subscriptions in every state (active, trialing,
    // incomplete, past_due, paused, etc.) except already-canceled ones.
    const subs = await stripe().subscriptions.list({ customer: customer.id, status: 'all', limit: 100 });
    for (const sub of subs.data) {
      if (sub.status === 'canceled' || sub.status === 'incomplete_expired') continue;
      try {
        await stripe().subscriptions.cancel(sub.id);
      } catch (err) {
        // Don't let one bad subscription block cleanup of the others.
        console.error(`[e2e] Failed to cancel subscription ${sub.id} (status=${sub.status}):`, err);
      }
    }
  }
}

/**
 * Poll GET ${apiUrl}/billing/account until is_subscribed === true.
 * Throws on non-ok responses (except 503 which is swallowed).
 * Throws with descriptive error if timeout is exceeded.
 */
export async function waitForSubscriptionActive(
  apiUrl: string,
  getToken: () => Promise<string>,
  timeoutMs: number,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastIsSubscribed: unknown = undefined;
  while (Date.now() < deadline) {
    const token = await getToken();
    const res = await fetch(`${apiUrl}/billing/account`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.ok) {
      const data = await res.json();
      lastIsSubscribed = data.is_subscribed;
      if (data.is_subscribed === true) return;
    } else if (res.status !== 503) {
      throw new Error(`waitForSubscriptionActive: unexpected response ${res.status}`);
    }
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
  }
  throw new Error(`waitForSubscriptionActive: timeout after ${timeoutMs}ms (last is_subscribed=${lastIsSubscribed})`);
}
