import Stripe from 'stripe';

let _client: Stripe | undefined;
function client(secretKey: string): Stripe {
  if (!_client) {
    _client = new Stripe(secretKey, {
      apiVersion: '2025-01-27.acacia' as Stripe.StripeConfig['apiVersion'],
    });
  }
  return _client;
}

export async function findCustomerByEmail(
  secretKey: string,
  email: string,
): Promise<Stripe.Customer | null> {
  const list = await client(secretKey).customers.list({ email, limit: 100 });
  return list.data[0] ?? null;
}

/**
 * Find every customer that the backend created for this owner_id,
 * regardless of whether email was attached. The backend tags every
 * Stripe customer with `metadata.owner_id` (verified billing_service.py),
 * so this is the canonical way to find a test's customer for teardown.
 *
 * Email-based lookup misses customers whose email was never set —
 * which happens whenever the Clerk JWT template doesn't include the
 * email claim. Verified leak from PR #309 deploy: cus_UMsUjHET7fJ1NG.
 */
export async function findCustomersByOwnerId(
  secretKey: string,
  ownerId: string,
): Promise<Stripe.Customer[]> {
  const escaped = ownerId.replace(/'/g, "\\'");
  const result = await client(secretKey).customers.search({
    query: `metadata['owner_id']:'${escaped}'`,
    limit: 100,
  });
  return result.data;
}

export async function setCustomerMetadata(
  secretKey: string,
  customerId: string,
  metadata: Record<string, string>,
): Promise<void> {
  await client(secretKey).customers.update(customerId, { metadata });
}

export async function cancelSubsAndDeleteCustomer(
  secretKey: string,
  email: string,
  ownerId?: string,
): Promise<void> {
  // Collect candidates from both lookups — email-list (legacy path) AND
  // metadata-search (catches the email=null case). Dedupe by id.
  const seen = new Map<string, Stripe.Customer>();
  const byEmail = await client(secretKey).customers.list({ email, limit: 100 });
  for (const c of byEmail.data) seen.set(c.id, c);
  if (ownerId) {
    const byOwner = await findCustomersByOwnerId(secretKey, ownerId);
    for (const c of byOwner) seen.set(c.id, c);
  }

  for (const customer of seen.values()) {
    const subs = await client(secretKey).subscriptions.list({
      customer: customer.id,
      status: 'all',
      limit: 100,
    });
    for (const sub of subs.data) {
      if (sub.status === 'canceled' || sub.status === 'incomplete_expired') continue;
      try {
        await client(secretKey).subscriptions.cancel(sub.id);
      } catch (err) {
        console.error(`[e2e] cancel sub ${sub.id} failed:`, err);
      }
    }
    try {
      await client(secretKey).customers.del(customer.id);
    } catch (err) {
      console.error(`[e2e] delete customer ${customer.id} failed:`, err);
    }
  }
}
