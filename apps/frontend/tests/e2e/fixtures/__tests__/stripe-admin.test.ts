import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the stripe SDK so we control the instance returned by `new Stripe(...)`.
vi.mock('stripe', () => {
  const mockSubscriptionsCancel = vi.fn();
  const mockSubscriptionsList = vi.fn();
  const mockCustomersList = vi.fn();
  const mockCustomersUpdate = vi.fn();
  const mockCustomersDel = vi.fn();

  const mockInstance = {
    customers: {
      list: mockCustomersList,
      update: mockCustomersUpdate,
      del: mockCustomersDel,
    },
    subscriptions: {
      list: mockSubscriptionsList,
      cancel: mockSubscriptionsCancel,
    },
  };

  const MockStripe = vi.fn(() => mockInstance);
  (MockStripe as unknown as Record<string, unknown>)._instance = mockInstance;
  (MockStripe as unknown as Record<string, unknown>)._mocks = {
    subscriptionsCancel: mockSubscriptionsCancel,
    subscriptionsList: mockSubscriptionsList,
    customersList: mockCustomersList,
    customersUpdate: mockCustomersUpdate,
    customersDel: mockCustomersDel,
  };

  return { default: MockStripe };
});

const FAKE_KEY = 'sk_test_unit_test_placeholder';

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});

async function getMocks() {
  const Stripe = (await import('stripe')).default as unknown as {
    _mocks: {
      subscriptionsCancel: ReturnType<typeof vi.fn>;
      subscriptionsList: ReturnType<typeof vi.fn>;
      customersList: ReturnType<typeof vi.fn>;
      customersUpdate: ReturnType<typeof vi.fn>;
      customersDel: ReturnType<typeof vi.fn>;
    };
  };
  return Stripe._mocks;
}

describe('findCustomerByEmail', () => {
  it('returns the first customer when one exists', async () => {
    const mocks = await getMocks();
    const customer = { id: 'cus_abc', email: 'a@b.com' };
    mocks.customersList.mockResolvedValue({ data: [customer] });

    const { findCustomerByEmail } = await import('../stripe-admin');
    const result = await findCustomerByEmail(FAKE_KEY, 'a@b.com');

    expect(result).toEqual(customer);
    expect(mocks.customersList).toHaveBeenCalledWith({ email: 'a@b.com', limit: 100 });
  });

  it('returns null when no customer matches', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [] });

    const { findCustomerByEmail } = await import('../stripe-admin');
    const result = await findCustomerByEmail(FAKE_KEY, 'missing@example.com');

    expect(result).toBeNull();
  });
});

describe('setCustomerMetadata', () => {
  it('PATCHes the customer with the metadata payload', async () => {
    const mocks = await getMocks();
    mocks.customersUpdate.mockResolvedValue({});

    const { setCustomerMetadata } = await import('../stripe-admin');
    await setCustomerMetadata(FAKE_KEY, 'cus_abc', { e2e_run_id: 'run_123', owner: 'user_x' });

    expect(mocks.customersUpdate).toHaveBeenCalledWith('cus_abc', {
      metadata: { e2e_run_id: 'run_123', owner: 'user_x' },
    });
  });
});

describe('cancelSubsAndDeleteCustomer', () => {
  it('is a no-op (idempotent) when no customer matches', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [] });

    const { cancelSubsAndDeleteCustomer } = await import('../stripe-admin');
    await expect(cancelSubsAndDeleteCustomer(FAKE_KEY, 'nobody@example.com')).resolves.toBeUndefined();

    expect(mocks.subscriptionsList).not.toHaveBeenCalled();
    expect(mocks.subscriptionsCancel).not.toHaveBeenCalled();
    expect(mocks.customersDel).not.toHaveBeenCalled();
  });

  it('cancels active subscriptions THEN deletes the customer (in that order)', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [{ id: 'cus_123' }] });
    mocks.subscriptionsList.mockResolvedValue({
      data: [
        { id: 'sub_active', status: 'active' },
        { id: 'sub_past_due', status: 'past_due' },
      ],
    });
    mocks.subscriptionsCancel.mockResolvedValue({ status: 'canceled' });
    mocks.customersDel.mockResolvedValue({ deleted: true });

    const callOrder: string[] = [];
    mocks.subscriptionsCancel.mockImplementation(async (id: string) => {
      callOrder.push(`cancel:${id}`);
      return { status: 'canceled' };
    });
    mocks.customersDel.mockImplementation(async (id: string) => {
      callOrder.push(`del:${id}`);
      return { deleted: true };
    });

    const { cancelSubsAndDeleteCustomer } = await import('../stripe-admin');
    await cancelSubsAndDeleteCustomer(FAKE_KEY, 'test@example.com');

    expect(mocks.subscriptionsList).toHaveBeenCalledWith({
      customer: 'cus_123',
      status: 'all',
      limit: 100,
    });
    expect(callOrder).toEqual(['cancel:sub_active', 'cancel:sub_past_due', 'del:cus_123']);
  });

  it('skips canceled / incomplete_expired subscriptions but still deletes the customer', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [{ id: 'cus_456' }] });
    mocks.subscriptionsList.mockResolvedValue({
      data: [
        { id: 'sub_canceled', status: 'canceled' },
        { id: 'sub_dead', status: 'incomplete_expired' },
      ],
    });
    mocks.customersDel.mockResolvedValue({ deleted: true });

    const { cancelSubsAndDeleteCustomer } = await import('../stripe-admin');
    await cancelSubsAndDeleteCustomer(FAKE_KEY, 'test@example.com');

    expect(mocks.subscriptionsCancel).not.toHaveBeenCalled();
    expect(mocks.customersDel).toHaveBeenCalledWith('cus_456');
  });

  it('still deletes the customer when a cancel call throws', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [{ id: 'cus_789' }] });
    mocks.subscriptionsList.mockResolvedValue({
      data: [{ id: 'sub_bad', status: 'active' }],
    });
    mocks.subscriptionsCancel.mockRejectedValueOnce(new Error('boom'));
    mocks.customersDel.mockResolvedValue({ deleted: true });

    const { cancelSubsAndDeleteCustomer } = await import('../stripe-admin');
    await expect(cancelSubsAndDeleteCustomer(FAKE_KEY, 'test@example.com')).resolves.toBeUndefined();

    expect(mocks.customersDel).toHaveBeenCalledWith('cus_789');
  });

  it('swallows a delete error and does not throw', async () => {
    const mocks = await getMocks();
    mocks.customersList.mockResolvedValue({ data: [{ id: 'cus_xyz' }] });
    mocks.subscriptionsList.mockResolvedValue({ data: [] });
    mocks.customersDel.mockRejectedValueOnce(new Error('cannot delete'));

    const { cancelSubsAndDeleteCustomer } = await import('../stripe-admin');
    await expect(cancelSubsAndDeleteCustomer(FAKE_KEY, 'test@example.com')).resolves.toBeUndefined();
  });
});
