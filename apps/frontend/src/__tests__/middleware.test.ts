import { describe, it, expect } from 'vitest';
import {
  DEFAULT_ADMIN_HOSTS,
  DEEP_LINK_SCHEME,
  ROOT_DOMAIN,
  SITE_URL,
} from '@/lib/domain';
import {
  decideAdminHostRouting,
  decideCanonicalHostRedirect,
} from '@/middleware';

const ADMIN_HOSTS = new Set([
  'admin.thelightbulb.company',
  'admin.dev.thelightbulb.company',
  'admin.localhost:3000',
]);

// The host sets are DERIVED, and none of the env vars that could override them
// is set in any deployed environment.
//
// The previous version of this block asserted the derivation equalled the
// the old apex exactly. It passed while `admin.dev.thelightbulb.company/admin`
// returned 404 in dev, because it was checking that the code agreed with
// itself, not that it agreed with DNS. `SITE_URL` is one literal shared by both
// deployments, so the moment dev and prod are on different domains, any list
// derived from it alone is wrong in exactly one of them.
//
// So: assert COVERAGE of every host DNS actually serves, not equality with one
// domain's worth. The retired apex is gone from these lists entirely.
describe('domain config', () => {
  it('derives the shipped values', () => {
    expect(SITE_URL).toBe('https://thelightbulb.company');
    expect(ROOT_DOMAIN).toBe('thelightbulb.company');
    expect(DEEP_LINK_SCHEME).toBe('flickmeon');
  });

  it('admits the admin host of EVERY product domain currently served', () => {
    for (const h of ADMIN_HOSTS) expect(DEFAULT_ADMIN_HOSTS).toContain(h);
  });

  it('admits the admin hosts of the domain dev moved to', () => {
    expect(DEFAULT_ADMIN_HOSTS).toContain('admin.dev.thelightbulb.company');
    expect(DEFAULT_ADMIN_HOSTS).toContain('admin.thelightbulb.company');
  });

  it('admits no duplicates', () => {
    expect(new Set(DEFAULT_ADMIN_HOSTS).size).toBe(DEFAULT_ADMIN_HOSTS.length);
  });
});

describe('decideCanonicalHostRedirect (SEO host consolidation)', () => {
  it('permanently consolidates the www host onto the apex while preserving path and query', () => {
    expect(
      decideCanonicalHostRedirect(
        'www.thelightbulb.company',
        '/blog/persistent-ai-agent-workspace',
        '?utm_source=google',
      ),
    ).toEqual({
      kind: 'redirect',
      to: 'https://thelightbulb.company/blog/persistent-ai-agent-workspace?utm_source=google',
    });
  });
});

describe('decideAdminHostRouting (admin host gate)', () => {
  // Owner, 2026-11-25: old links never redirect, they are a 404. The host does not matter.
  it.each([
    ['admin.thelightbulb.company', '/admin/spark'],
    ['admin.thelightbulb.company', '/admin'],
    ['app.thelightbulb.company', '/admin/users'],
    ['attacker.example.com', '/admin'],
    [null, '/admin'],
  ])('returns 404 for an /admin path (host %s, path %s)', (host, path) => {
    expect(decideAdminHostRouting(host, path, ADMIN_HOSTS)).toEqual({ kind: 'not_found' });
  });
  it('rewrites the root of an admin host to /admin', () => {
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/', ADMIN_HOSTS),
    ).toEqual({ kind: 'rewrite', to: '/admin' });
  });
  it('rewrites /spark and nested paths on an admin host under /admin', () => {
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/spark', ADMIN_HOSTS),
    ).toEqual({ kind: 'rewrite', to: '/admin/spark' });
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/users/u_1/agents', ADMIN_HOSTS),
    ).toEqual({ kind: 'rewrite', to: '/admin/users/u_1/agents' });
  });
  it('does not rewrite Next internals or API routes on an admin host', () => {
    for (const p of ['/_next/static/x.js', '/api/v1/admin/me', '/trpc/x', '/api', '/ingest/e']) {
      expect(decideAdminHostRouting('admin.thelightbulb.company', p, ADMIN_HOSTS)).toEqual({
        kind: 'passthrough',
      });
    }
  });

  it('rewrites a non-admin path on an admin host under /admin', () => {
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/chat', ADMIN_HOSTS),
    ).toEqual({ kind: 'rewrite', to: '/admin/chat' });
  });

  it('allows admin.localhost:3000 (default admin host) for local dev', () => {
    expect(
      decideAdminHostRouting('admin.localhost:3000', '/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'rewrite', to: '/admin/users' });
  });

  it('passes through public hosts on public paths', () => {
    expect(
      decideAdminHostRouting('app.thelightbulb.company', '/chat', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('matches /admin exactly (not just prefix-with-extra-letters)', () => {
    // /administrator should NOT be treated as an admin path
    expect(
      decideAdminHostRouting('app.thelightbulb.company', '/administrator', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('is case-insensitive on host comparison', () => {
    expect(
      decideAdminHostRouting('ADMIN.THELIGHTBULB.COMPANY', '/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'rewrite', to: '/admin/users' });
  });

  // Clerk's auth paths pass through, or AdminLayout's bounce to /sign-in would loop.

  it('passes through /sign-in on admin hosts so AdminLayout can complete auth', () => {
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/sign-in', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('passes through /sign-up on admin hosts', () => {
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/sign-up', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('passes through Clerk catchall sub-paths under /sign-in', () => {
    // Clerk renders /sign-in/factor-one, /sign-in/sso-callback, etc.
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/sign-in/factor-one', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/sign-up/verify-email-address', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });
});
