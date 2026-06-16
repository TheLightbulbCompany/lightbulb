import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { ApiError, deriveWebSocketUrl } from '../api';

describe('deriveWebSocketUrl', () => {
  const originalEnv = process.env.NEXT_PUBLIC_WS_URL;

  beforeEach(() => {
    delete process.env.NEXT_PUBLIC_WS_URL;
  });

  afterEach(() => {
    if (originalEnv === undefined) {
      delete process.env.NEXT_PUBLIC_WS_URL;
    } else {
      process.env.NEXT_PUBLIC_WS_URL = originalEnv;
    }
  });

  it('rewrites prod apex hostname: api.isol8.co → ws.isol8.co', () => {
    // Regression: before the fix this returned `wss://api.isol8.co` because the
    // old `.replace("api-", "ws-")` only matched env-prefixed hostnames. Prod
    // silently routed WebSocket traffic to the ALB and failed.
    expect(deriveWebSocketUrl('https://api.isol8.co/api/v1')).toBe(
      'wss://ws.isol8.co',
    );
  });

  it('rewrites dev env-prefixed hostname: api-dev.isol8.co → ws-dev.isol8.co', () => {
    expect(deriveWebSocketUrl('https://api-dev.isol8.co/api/v1')).toBe(
      'wss://ws-dev.isol8.co',
    );
  });

  it('rewrites staging env-prefixed hostname', () => {
    expect(deriveWebSocketUrl('https://api-staging.isol8.co/api/v1')).toBe(
      'wss://ws-staging.isol8.co',
    );
  });

  it('leaves localhost untouched and strips /api/v1', () => {
    expect(deriveWebSocketUrl('http://localhost:8000/api/v1')).toBe(
      'ws://localhost:8000',
    );
  });

  it('downgrades https→wss and http→ws', () => {
    expect(deriveWebSocketUrl('http://api-dev.isol8.co/api/v1')).toBe(
      'ws://ws-dev.isol8.co',
    );
  });

  it('NEXT_PUBLIC_WS_URL override short-circuits derivation', () => {
    process.env.NEXT_PUBLIC_WS_URL = 'wss://custom.example.com';
    expect(deriveWebSocketUrl('https://api.isol8.co/api/v1')).toBe(
      'wss://custom.example.com',
    );
  });

  it('does not rewrite an unrelated hostname that happens to start with "api"', () => {
    // "apiary.com" must not become "wsary.com" — the lookahead guards against
    // matching substrings of longer labels.
    expect(deriveWebSocketUrl('https://apiary.com/api/v1')).toBe(
      'wss://apiary.com',
    );
  });
});

describe('ApiError', () => {
  it('captures the status code and parsed body', () => {
    const err = new ApiError(402, { blocked: { code: 'credits_required' } });
    expect(err.status).toBe(402);
    expect(err.body).toEqual({ blocked: { code: 'credits_required' } });
    expect(err.name).toBe('ApiError');
  });

  it('uses a default message when none provided', () => {
    const err = new ApiError(503, null);
    expect(err.message).toBe('API 503');
  });

  it('uses the provided message when given', () => {
    const err = new ApiError(404, null, 'Resource not found');
    expect(err.message).toBe('Resource not found');
  });

  it('captures the backend request id when provided', () => {
    const err = new ApiError(500, null, 'boom', 'req_abc');
    expect(err.requestId).toBe('req_abc');
  });

  it('leaves requestId undefined when omitted (back-compat with existing call sites)', () => {
    const err = new ApiError(500, null, 'boom');
    expect(err.requestId).toBeUndefined();
  });

  it('is throwable and instanceof Error', () => {
    expect(() => {
      throw new ApiError(400, { error: 'bad' });
    }).toThrow(ApiError);
    try {
      throw new ApiError(400, { error: 'bad' });
    } catch (e) {
      expect(e).toBeInstanceOf(Error);
      expect(e).toBeInstanceOf(ApiError);
    }
  });
});
