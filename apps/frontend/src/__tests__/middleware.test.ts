import { describe, it, expect } from 'vitest';
import { decideAdminHostRouting } from '@/middleware';

const ADMIN_HOSTS = new Set([
  'admin.isol8.co',
  'admin.dev.isol8.co',
  'admin.localhost:3000',
]);

describe('decideAdminHostRouting (admin host gate)', () => {
  it('passes through when an admin host hits an /admin path', () => {
    expect(
      decideAdminHostRouting('admin.isol8.co', '/admin/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('returns 404 for an /admin path on a non-admin host', () => {
    expect(
      decideAdminHostRouting('app.isol8.co', '/admin/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'not_found' });
  });

  it('redirects to /admin when an admin host requests a non-admin path', () => {
    expect(
      decideAdminHostRouting('admin.isol8.co', '/chat', ADMIN_HOSTS),
    ).toEqual({ kind: 'redirect', to: '/admin' });
  });

  it('returns 404 for /admin on an unknown random host (defense in depth)', () => {
    expect(
      decideAdminHostRouting('attacker.example.com', '/admin', ADMIN_HOSTS),
    ).toEqual({ kind: 'not_found' });
  });

  it('allows admin.localhost:3000 (default admin host) for local dev', () => {
    expect(
      decideAdminHostRouting('admin.localhost:3000', '/admin/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('treats null/missing host as non-admin (404 on /admin)', () => {
    expect(
      decideAdminHostRouting(null, '/admin/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'not_found' });
  });

  it('passes through public hosts on public paths', () => {
    expect(
      decideAdminHostRouting('app.isol8.co', '/chat', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('matches /admin exactly (not just prefix-with-extra-letters)', () => {
    // /administrator should NOT be treated as an admin path
    expect(
      decideAdminHostRouting('app.isol8.co', '/administrator', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('is case-insensitive on host comparison', () => {
    expect(
      decideAdminHostRouting('ADMIN.ISOL8.CO', '/admin/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  // ---------------------------------------------------------------------------
  // Sign-in / sign-up passthrough on admin hosts.
  // Bug: AdminLayout server-component redirects unauth users to /sign-in.
  // The admin-host gate previously bounced /sign-in back to /admin, which
  // immediately triggered AdminLayout's redirect to /sign-in again → infinite
  // 307 loop (ERR_TOO_MANY_REDIRECTS). Clerk auth paths must passthrough so
  // the admin host can complete sign-in inline.
  // ---------------------------------------------------------------------------

  it('passes through /sign-in on admin hosts so AdminLayout can complete auth', () => {
    expect(
      decideAdminHostRouting('admin.isol8.co', '/sign-in', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('passes through /sign-up on admin hosts', () => {
    expect(
      decideAdminHostRouting('admin.isol8.co', '/sign-up', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('passes through Clerk catchall sub-paths under /sign-in', () => {
    // Clerk renders /sign-in/factor-one, /sign-in/sso-callback, etc.
    expect(
      decideAdminHostRouting('admin.isol8.co', '/sign-in/factor-one', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
    expect(
      decideAdminHostRouting('admin.isol8.co', '/sign-up/verify-email-address', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('still redirects truly off-admin paths (e.g. /chat) on admin hosts', () => {
    // Regression guard — the original gate behavior for non-auth, non-admin
    // paths is preserved.
    expect(
      decideAdminHostRouting('admin.isol8.co', '/chat', ADMIN_HOSTS),
    ).toEqual({ kind: 'redirect', to: '/admin' });
  });
});
