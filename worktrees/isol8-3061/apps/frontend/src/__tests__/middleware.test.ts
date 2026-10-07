import { describe, it, expect } from 'vitest';
import {
  DEFAULT_ADMIN_HOSTS,
  DEEP_LINK_SCHEME,
  PAPERCLIP_HOSTS,
  ROOT_DOMAIN,
  SITE_URL,
} from '@/lib/domain';
import { decideAdminHostRouting, decidePaperclipLinkRedirect } from '@/middleware';

const ADMIN_HOSTS = new Set([
  'admin.flickmeon.ai',
  'admin.dev.flickmeon.ai',
  'admin.localhost:3000',
]);

// The host sets are DERIVED, and none of the env vars that could override them
// is set in any deployed environment.
//
// The previous version of this block asserted the derivation equalled the
// the old apex exactly. It passed while `admin.dev.flickmeon.ai/admin`
// returned 404 in dev, because it was checking that the code agreed with
// itself, not that it agreed with DNS. `SITE_URL` is one literal shared by both
// deployments, so the moment dev and prod are on different domains, any list
// derived from it alone is wrong in exactly one of them.
//
// So: assert COVERAGE of every host DNS actually serves, not equality with one
// domain's worth. The retired apex is gone from these lists entirely.
describe('domain config', () => {
  it('derives the shipped values', () => {
    expect(SITE_URL).toBe('https://flickmeon.ai');
    expect(ROOT_DOMAIN).toBe('flickmeon.ai');
    expect(DEEP_LINK_SCHEME).toBe('flickmeon');
  });

  it('admits the admin host of EVERY product domain currently served', () => {
    for (const h of ADMIN_HOSTS) expect(DEFAULT_ADMIN_HOSTS).toContain(h);
  });

  it('admits the company host of EVERY product domain currently served', () => {
    for (const d of ['flickmeon.ai']) {
      expect(PAPERCLIP_HOSTS).toContain(`company.${d}`);
      expect(PAPERCLIP_HOSTS).toContain(`dev.company.${d}`);
    }
  });

  it('admits no duplicates', () => {
    expect(new Set(DEFAULT_ADMIN_HOSTS).size).toBe(DEFAULT_ADMIN_HOSTS.length);
    expect(new Set(PAPERCLIP_HOSTS).size).toBe(PAPERCLIP_HOSTS.length);
  });
});

describe('decideAdminHostRouting (admin host gate)', () => {
  it('passes through when an admin host hits an /admin path', () => {
    expect(
      decideAdminHostRouting('admin.flickmeon.ai', '/admin/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('returns 404 for an /admin path on a non-admin host', () => {
    expect(
      decideAdminHostRouting('app.flickmeon.ai', '/admin/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'not_found' });
  });

  it('redirects to /admin when an admin host requests a non-admin path', () => {
    expect(
      decideAdminHostRouting('admin.flickmeon.ai', '/chat', ADMIN_HOSTS),
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
      decideAdminHostRouting('app.flickmeon.ai', '/chat', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('matches /admin exactly (not just prefix-with-extra-letters)', () => {
    // /administrator should NOT be treated as an admin path
    expect(
      decideAdminHostRouting('app.flickmeon.ai', '/administrator', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('is case-insensitive on host comparison', () => {
    expect(
      decideAdminHostRouting('ADMIN.FLICKMEON.AI', '/admin/users', ADMIN_HOSTS),
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
      decideAdminHostRouting('admin.flickmeon.ai', '/sign-in', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('passes through /sign-up on admin hosts', () => {
    expect(
      decideAdminHostRouting('admin.flickmeon.ai', '/sign-up', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('passes through Clerk catchall sub-paths under /sign-in', () => {
    // Clerk renders /sign-in/factor-one, /sign-in/sso-callback, etc.
    expect(
      decideAdminHostRouting('admin.flickmeon.ai', '/sign-in/factor-one', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
    expect(
      decideAdminHostRouting('admin.flickmeon.ai', '/sign-up/verify-email-address', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('passes through the choose-organization session task on admin hosts', () => {
    expect(
      decideAdminHostRouting(
        'admin.flickmeon.ai',
        '/session-tasks/choose-organization',
        ADMIN_HOSTS,
      ),
    ).toEqual({ kind: 'passthrough' });
  });

  it('still redirects truly off-admin paths (e.g. /chat) on admin hosts', () => {
    // Regression guard — the original gate behavior for non-auth, non-admin
    // paths is preserved.
    expect(
      decideAdminHostRouting('admin.flickmeon.ai', '/chat', ADMIN_HOSTS),
    ).toEqual({ kind: 'redirect', to: '/admin' });
  });
});

// The bundled paperclip skill writes agent links as native Paperclip paths
// (e.g. `/ISOA/issues/ISOA-317`); a direct hit on one 404'd because the
// retired /teams/* tree never redirected them.
describe('decidePaperclipLinkRedirect (agent Paperclip-link redirect)', () => {
  it('redirects a direct hit on an agent-written issue path to the in-shell panel', () => {
    expect(decidePaperclipLinkRedirect('/ISOA/issues/ISOA-317')).toEqual({
      kind: 'redirect',
      to: '/chat?panel=teams-issue-detail&detailId=ISOA-317',
    });
  });

  it('leaves ordinary app paths alone', () => {
    expect(decidePaperclipLinkRedirect('/chat')).toBeNull();
    expect(decidePaperclipLinkRedirect('/settings')).toBeNull();
    expect(decidePaperclipLinkRedirect('/admin')).toBeNull();
  });
});
