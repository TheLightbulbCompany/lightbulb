import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useApi } from '@/lib/api';

const { mockGetToken } = vi.hoisted(() => ({
  mockGetToken: vi.fn((): Promise<string | null> => Promise.resolve('mock-jwt-token')),
}));

vi.mock('@clerk/nextjs', () => ({
  useAuth: () => ({
    isSignedIn: true,
    isLoaded: true,
    userId: 'user_test_123',
    getToken: mockGetToken,
  }),
}));

// Mock fetch globally for API tests
const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

describe('useApi hook', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetToken.mockImplementation(() => Promise.resolve('mock-jwt-token'));
  });

  describe('syncUser', () => {
    it('calls POST /users/sync', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ status: 'exists', user_id: 'user_test_123' }),
      });

      const { result } = renderHook(() => useApi());
      const response = await result.current.syncUser();

      expect(response).toEqual({
        status: 'exists',
        user_id: 'user_test_123',
      });
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/users/sync'),
        expect.objectContaining({ method: 'POST' }),
      );
    });
  });

  describe('get', () => {
    it('sends GET request with auth header', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ agents: [] }),
      });

      const { result } = renderHook(() => useApi());
      const response = await result.current.get('/agents');

      expect(response).toEqual({ agents: [] });
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/agents'),
        expect.objectContaining({
          method: 'GET',
          headers: expect.objectContaining({
            Authorization: 'Bearer mock-jwt-token',
          }),
        }),
      );
    });
  });

  describe('post', () => {
    it('sends POST request with body', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ status: 'ok' }),
      });

      const { result } = renderHook(() => useApi());
      const response = await result.current.post('/users/sync', {});

      expect(response).toHaveProperty('status');
    });
  });

  describe('Teams cookie opt-ins (credentials + retry-on-401)', () => {
    it('omits credentials by default (bearer-only callers)', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });

      const { result } = renderHook(() => useApi());
      await result.current.get('/agents');

      const init = mockFetch.mock.calls[0][1];
      expect(init.credentials).toBeUndefined();
    });

    it('sends credentials: "include" when opted in', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });

      const { result } = renderHook(() => useApi());
      await result.current.get('/teams/dashboard', { credentials: 'include' });

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/teams/dashboard'),
        expect.objectContaining({ credentials: 'include' }),
      );
    });

    it('retries exactly once on 401 when retryOn401 is set, then succeeds', async () => {
      // First call: 401 (backend reseeds a fresh cookie on this response).
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 401,
        headers: new Headers({ 'Content-Type': 'application/json' }),
        json: () => Promise.resolve({ detail: 'session refreshed, retry' }),
      });
      // Retry: succeeds with the freshly reseeded cookie.
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ data: 'ok' }),
      });

      const { result } = renderHook(() => useApi());
      const response = await result.current.get('/teams/dashboard', {
        credentials: 'include',
        retryOn401: true,
      });

      expect(response).toEqual({ data: 'ok' });
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });

    it('does not loop: a second 401 surfaces the error after one retry', async () => {
      mockFetch.mockResolvedValue({
        ok: false,
        status: 401,
        headers: new Headers({ 'Content-Type': 'application/json' }),
        json: () => Promise.resolve({ detail: 'session refreshed, retry' }),
      });

      const { result } = renderHook(() => useApi());
      await expect(
        result.current.get('/teams/dashboard', { credentials: 'include', retryOn401: true }),
      ).rejects.toMatchObject({ status: 401 });

      // Exactly one retry: the original attempt + a single retry = 2 calls.
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });

    it('does not retry a 401 when retryOn401 is not set', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 401,
        headers: new Headers({ 'Content-Type': 'application/json' }),
        json: () => Promise.resolve({ detail: 'nope' }),
      });

      const { result } = renderHook(() => useApi());
      await expect(result.current.get('/agents')).rejects.toMatchObject({ status: 401 });
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });

    it('does not retry non-401 errors even when retryOn401 is set', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 500,
        headers: new Headers({ 'Content-Type': 'application/json' }),
        json: () => Promise.resolve({ detail: 'boom' }),
      });

      const { result } = renderHook(() => useApi());
      await expect(
        result.current.get('/teams/dashboard', { credentials: 'include', retryOn401: true }),
      ).rejects.toMatchObject({ status: 500 });
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });
  });

  describe('error handling', () => {
    it('throws error when no token available', async () => {
      mockGetToken.mockImplementation(() => Promise.resolve(null));
      const { result } = renderHook(() => useApi());

      await expect(result.current.syncUser()).rejects.toThrow('No authentication token available');
    });

    it('throws error on non-ok response', async () => {
      // ApiError now reads Content-Type from response.headers to decide
      // whether to parse the body, so the mock must expose a headers map.
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 404,
        headers: new Headers({ 'Content-Type': 'application/json' }),
        json: () => Promise.resolve({ detail: 'Not found' }),
      });

      const { result } = renderHook(() => useApi());
      await expect(result.current.get('/nonexistent')).rejects.toThrow('Not found');
    });
  });
});
