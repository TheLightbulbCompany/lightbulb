import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Mock the stripe module so we control the instance returned by `new Stripe(...)`
vi.mock('stripe', () => {
  const mockSubscriptionsCancel = vi.fn();
  const mockSubscriptionsList = vi.fn();
  const mockCustomersList = vi.fn();

  const mockInstance = {
    customers: {
      list: mockCustomersList,
    },
    subscriptions: {
      list: mockSubscriptionsList,
      cancel: mockSubscriptionsCancel,
    },
  };

  const MockStripe = vi.fn(() => mockInstance);

  // Expose mocks via the constructor function so tests can access them
  (MockStripe as unknown as Record<string, unknown>)._instance = mockInstance;
  (MockStripe as unknown as Record<string, unknown>)._mocks = {
    subscriptionsCancel: mockSubscriptionsCancel,
    subscriptionsList: mockSubscriptionsList,
    customersList: mockCustomersList,
  };

  return { default: MockStripe };
});

// Reset module cache and mocks before each test
beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  process.env.STRIPE_SECRET_KEY = 'sk_test_unit_test_placeholder';
});

afterEach(() => {
  delete process.env.STRIPE_SECRET_KEY;
});

async function getMocks() {
  const Stripe = (await import('stripe')).default as unknown as {
    _mocks: {
      subscriptionsCancel: ReturnType<typeof vi.fn>;
      subscriptionsList: ReturnType<typeof vi.fn>;
      customersList: ReturnType<typeof vi.fn>;
    };
  };
  return Stripe._mocks;
}

describe('cancelSubscriptionIfExists', () => {
  it('no-op when no customers found', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [] });

    const { cancelSubscriptionIfExists } = await import('../stripe');
    await expect(cancelSubscriptionIfExists('nobody@example.com')).resolves.toBeUndefined();

    expect(mocks.subscriptionsList).not.toHaveBeenCalled();
    expect(mocks.subscriptionsCancel).not.toHaveBeenCalled();
  });

  it('no-op when customer has no subscriptions', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [{ id: 'cus_123' }] });
    mocks.subscriptionsList.mockResolvedValue({ data: [] });

    const { cancelSubscriptionIfExists } = await import('../stripe');
    await expect(cancelSubscriptionIfExists('test@example.com')).resolves.toBeUndefined();

    // A single list call with status: 'all' covers every non-canceled state.
    expect(mocks.subscriptionsList).toHaveBeenCalledTimes(1);
    expect(mocks.subscriptionsList).toHaveBeenCalledWith({
      customer: 'cus_123',
      status: 'all',
      limit: 100,
    });
    expect(mocks.subscriptionsCancel).not.toHaveBeenCalled();
  });

  it('cancels all non-canceled subscriptions returned by status=all', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [{ id: 'cus_123' }] });
    mocks.subscriptionsList.mockResolvedValue({
      data: [
        { id: 'sub_active', status: 'active' },
        { id: 'sub_past_due', status: 'past_due' },
        { id: 'sub_canceled', status: 'canceled' },
        { id: 'sub_incomplete_expired', status: 'incomplete_expired' },
      ],
    });
    mocks.subscriptionsCancel.mockResolvedValue({ status: 'canceled' });

    const { cancelSubscriptionIfExists } = await import('../stripe');
    await expect(cancelSubscriptionIfExists('test@example.com')).resolves.toBeUndefined();

    expect(mocks.subscriptionsList).toHaveBeenCalledTimes(1);
    expect(mocks.subscriptionsCancel).toHaveBeenCalledTimes(2);
    expect(mocks.subscriptionsCancel).toHaveBeenCalledWith('sub_active');
    expect(mocks.subscriptionsCancel).toHaveBeenCalledWith('sub_past_due');
    expect(mocks.subscriptionsCancel).not.toHaveBeenCalledWith('sub_canceled');
    expect(mocks.subscriptionsCancel).not.toHaveBeenCalledWith('sub_incomplete_expired');
  });

  it('continues cancelling other subscriptions when one cancel call throws', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [{ id: 'cus_123' }] });
    mocks.subscriptionsList.mockResolvedValue({
      data: [
        { id: 'sub_bad', status: 'active' },
        { id: 'sub_good', status: 'active' },
      ],
    });
    mocks.subscriptionsCancel
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce({ status: 'canceled' });

    const { cancelSubscriptionIfExists } = await import('../stripe');
    await expect(cancelSubscriptionIfExists('test@example.com')).resolves.toBeUndefined();

    expect(mocks.subscriptionsCancel).toHaveBeenCalledTimes(2);
  });
});

describe('waitForSubscriptionActive', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('resolves when is_subscribed is true', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ is_subscribed: true }),
    });

    const { waitForSubscriptionActive } = await import('../stripe');
    await expect(
      waitForSubscriptionActive('http://api', async () => 'token', 10000),
    ).resolves.toBeUndefined();
  });

  it('swallows 503 and continues polling', async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockResolvedValue({
        ok: true,
        json: async () => ({ is_subscribed: true }),
      });

    const { waitForSubscriptionActive } = await import('../stripe');
    await expect(
      Promise.all([
        waitForSubscriptionActive('http://api', async () => 'token', 30000),
        vi.runAllTimersAsync(),
      ]),
    ).resolves.toBeDefined();
    expect(global.fetch).toHaveBeenCalledTimes(2); // 503 then success
  });

  it('throws immediately on non-ok, non-503 response', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401 });

    const { waitForSubscriptionActive } = await import('../stripe');
    await expect(waitForSubscriptionActive('http://api', async () => 'token', 10000)).rejects.toThrow('401');
  });

  it('throws descriptive timeout error when deadline exceeded', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ is_subscribed: false }),
    });

    const { waitForSubscriptionActive } = await import('../stripe');
    await expect(
      Promise.all([
        waitForSubscriptionActive('http://api', async () => 'token', 100),
        vi.runAllTimersAsync(),
      ]),
    ).rejects.toThrow('timeout');
  });
});
