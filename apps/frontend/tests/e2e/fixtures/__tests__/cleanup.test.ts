import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../clerk-admin', () => ({
  createUser: vi.fn(),
  deleteUser: vi.fn(),
  deleteOrg: vi.fn(),
  findUserByEmail: vi.fn(),
}));

vi.mock('../stripe-admin', () => ({
  cancelSubsAndDeleteCustomer: vi.fn(),
  findCustomerByEmail: vi.fn(),
  findCustomersByOwnerId: vi.fn(),
}));

import { cleanupUser, type E2EUser } from '../user';
import { AuthedFetchError } from '../api';
import {
  deleteUser,
  deleteOrg,
  findUserByEmail,
} from '../clerk-admin';
import {
  cancelSubsAndDeleteCustomer,
  findCustomerByEmail,
  findCustomersByOwnerId,
} from '../stripe-admin';

const ENV_KEYS = {
  STRIPE_SECRET_KEY: 'sk_test_stripe',
  CLERK_SECRET_KEY: 'sk_test_clerk',
};

function makeUser(overrides: Partial<E2EUser> = {}): E2EUser {
  return {
    runId: '1776572400000-abcdef',
    email: 'isol8-e2e-test@mailsac.com',
    password: 'pw',
    clerkUserId: 'user_abc',
    page: {
      goto: vi.fn().mockResolvedValue(null),
      url: vi.fn().mockReturnValue('http://localhost:3000/chat'),
      waitForFunction: vi.fn().mockResolvedValue(null),
    } as unknown as E2EUser['page'],
    api: { delete: vi.fn().mockResolvedValue({ deleted: {} }) } as unknown as E2EUser['api'],
    ddb: {} as E2EUser['ddb'],
    ...overrides,
  };
}

describe('cleanupUser', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    process.env.STRIPE_SECRET_KEY = ENV_KEYS.STRIPE_SECRET_KEY;
    process.env.CLERK_SECRET_KEY = ENV_KEYS.CLERK_SECRET_KEY;
    vi.mocked(cancelSubsAndDeleteCustomer).mockReset().mockResolvedValue();
    vi.mocked(deleteUser).mockReset().mockResolvedValue();
    vi.mocked(deleteOrg).mockReset().mockResolvedValue();
    vi.mocked(findCustomerByEmail).mockReset().mockResolvedValue(null);
    vi.mocked(findCustomersByOwnerId).mockReset().mockResolvedValue([]);
    vi.mocked(findUserByEmail).mockReset().mockResolvedValue(null);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('runs Stripe → backend → Clerk → verification in order', async () => {
    const user = makeUser();

    const promise = cleanupUser(user);
    await vi.runAllTimersAsync();
    await promise;

    const stripeOrder = vi.mocked(cancelSubsAndDeleteCustomer).mock.invocationCallOrder[0];
    const backendOrder = vi.mocked(user.api.delete).mock.invocationCallOrder[0];
    const clerkOrder = vi.mocked(deleteUser).mock.invocationCallOrder[0];
    const verifyStripeOrder = vi.mocked(findCustomerByEmail).mock.invocationCallOrder[0];
    const verifyClerkOrder = vi.mocked(findUserByEmail).mock.invocationCallOrder[0];

    expect(stripeOrder).toBeLessThan(backendOrder);
    expect(backendOrder).toBeLessThan(clerkOrder);
    expect(clerkOrder).toBeLessThan(verifyStripeOrder);
    expect(verifyStripeOrder).toBeLessThan(verifyClerkOrder);

    expect(cancelSubsAndDeleteCustomer).toHaveBeenCalledWith(
      ENV_KEYS.STRIPE_SECRET_KEY,
      user.email,
      user.clerkUserId, // owner_id == clerkUserId in personal context
    );
    expect(user.api.delete).toHaveBeenCalledWith('/debug/user-data');
    expect(deleteUser).toHaveBeenCalledWith({
      secretKey: ENV_KEYS.CLERK_SECRET_KEY,
      userId: user.clerkUserId,
    });
    expect(deleteOrg).not.toHaveBeenCalled();
  });

  it('deletes the org before the user when orgId is set', async () => {
    const user = makeUser({ orgId: 'org_abc' });

    const promise = cleanupUser(user);
    await vi.runAllTimersAsync();
    await promise;

    const orgOrder = vi.mocked(deleteOrg).mock.invocationCallOrder[0];
    const userOrder = vi.mocked(deleteUser).mock.invocationCallOrder[0];
    expect(orgOrder).toBeLessThan(userOrder);
    expect(deleteOrg).toHaveBeenCalledWith({
      secretKey: ENV_KEYS.CLERK_SECRET_KEY,
      orgId: 'org_abc',
    });
  });

  it('deletes only the Clerk user for a member of a shared workspace', async () => {
    const user = makeUser({ orgId: 'org_shared' });

    const promise = cleanupUser(user, { cleanupWorkspace: false });
    await vi.runAllTimersAsync();
    await promise;

    expect(cancelSubsAndDeleteCustomer).not.toHaveBeenCalled();
    expect(user.api.delete).not.toHaveBeenCalled();
    expect(deleteOrg).not.toHaveBeenCalled();
    expect(deleteUser).toHaveBeenCalledWith({
      secretKey: ENV_KEYS.CLERK_SECRET_KEY,
      userId: user.clerkUserId,
    });
    expect(findCustomerByEmail).not.toHaveBeenCalled();
    expect(findCustomersByOwnerId).not.toHaveBeenCalled();
    expect(findUserByEmail).toHaveBeenCalledWith({
      secretKey: ENV_KEYS.CLERK_SECRET_KEY,
      email: user.email,
    });
  });

  it('throws when Stripe customer still exists after teardown', async () => {
    vi.mocked(findCustomerByEmail).mockResolvedValue({
      id: 'cus_leaked',
    } as Awaited<ReturnType<typeof findCustomerByEmail>>);

    const promise = cleanupUser(makeUser());
    const settled = promise.catch((err) => err);
    await vi.runAllTimersAsync();
    const err = await settled;

    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toMatch(/stripe leak.*cus_leaked/);
  });

  it('throws when Clerk user still exists after teardown', async () => {
    vi.mocked(findUserByEmail).mockResolvedValue({
      id: 'user_leaked',
      email_addresses: [{ email_address: 'isol8-e2e-test@mailsac.com' }],
    });

    const promise = cleanupUser(makeUser());
    const settled = promise.catch((err) => err);
    await vi.runAllTimersAsync();
    const err = await settled;

    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toMatch(/clerk leak.*user_leaked/);
  });

  it('treats 404 from /debug/user-data as success (idempotent re-run)', async () => {
    const user = makeUser();
    vi.mocked(user.api.delete).mockRejectedValue(
      new AuthedFetchError('DELETE', '/debug/user-data', 404, 'not found'),
    );

    const promise = cleanupUser(user);
    await vi.runAllTimersAsync();
    await expect(promise).resolves.toBeUndefined();

    expect(cancelSubsAndDeleteCustomer).toHaveBeenCalledOnce();
    expect(deleteUser).toHaveBeenCalledOnce();
    expect(findCustomerByEmail).toHaveBeenCalledOnce();
    expect(findUserByEmail).toHaveBeenCalledOnce();
  });

  it('records non-404 backend errors but still runs Clerk cleanup', async () => {
    // Codex P1 on PR #309: a backend failure (5xx, 401 from Stripe-Checkout
    // tab) must NOT abort cleanup before Clerk delete runs — the Clerk user
    // would leak. cleanupUser collects per-step failures and only throws
    // at the end, so each step still gets to run.
    const user = makeUser();
    vi.mocked(user.api.delete).mockRejectedValue(
      new AuthedFetchError('DELETE', '/debug/user-data', 500, 'server error: nested 404 message'),
    );

    const promise = cleanupUser(user);
    const settled = promise.catch((err) => err);
    await vi.runAllTimersAsync();
    const err = await settled;

    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toMatch(/backend.*500/);
    // Critical: Clerk delete still ran despite the backend failure.
    expect(deleteUser).toHaveBeenCalledOnce();
  });

  it('throws when STRIPE_SECRET_KEY is missing', async () => {
    delete process.env.STRIPE_SECRET_KEY;
    await expect(cleanupUser(makeUser())).rejects.toThrow(/STRIPE_SECRET_KEY/);
  });

  it('throws when CLERK_SECRET_KEY is missing', async () => {
    delete process.env.CLERK_SECRET_KEY;
    await expect(cleanupUser(makeUser())).rejects.toThrow(/CLERK_SECRET_KEY/);
  });
});
