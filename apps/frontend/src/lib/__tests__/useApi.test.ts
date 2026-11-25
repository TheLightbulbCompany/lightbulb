import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useApi } from '@/lib/api';

const { mockGetToken } = vi.hoisted(() => ({
  mockGetToken: vi.fn((): Promise<string | null> => Promise.resolve('mock-jwt-token')),
}));

vi.mock('@clerk/nextjs', () => ({
  useAuth: () => ({ getToken: mockGetToken }),
}));

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

describe('useApi().post', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetToken.mockImplementation(() => Promise.resolve('mock-jwt-token'));
  });

  it('POSTs JSON with the bearer token', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: 'sent' }),
    });

    const { result } = renderHook(() => useApi());
    expect(await result.current.post('/os/referral', { message: 'hi' })).toEqual({ status: 'sent' });
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/os/referral'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ message: 'hi' }),
        headers: expect.objectContaining({ Authorization: 'Bearer mock-jwt-token' }),
      }),
    );
  });

  it('throws on a non-2xx response', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 500, headers: new Headers() });
    const { result } = renderHook(() => useApi());
    await expect(result.current.post('/os/referral', {})).rejects.toThrow('API 500');
  });

  it('throws when there is no token', async () => {
    mockGetToken.mockImplementation(() => Promise.resolve(null));
    const { result } = renderHook(() => useApi());
    await expect(result.current.post('/os/referral', {})).rejects.toThrow('No authentication token available');
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
