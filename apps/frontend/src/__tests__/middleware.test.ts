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
  decidePaperclipLinkRedirect,
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
  it('passes through when an admin host hits an /admin path', () => {
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/admin/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'passthrough' });
  });

  it('returns 404 for an /admin path on a non-admin host', () => {
    expect(
      decideAdminHostRouting('app.thelightbulb.company', '/admin/users', ADMIN_HOSTS),
    ).toEqual({ kind: 'not_found' });
  });

  it('redirects to /admin when an admin host requests a non-admin path', () => {
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/chat', ADMIN_HOSTS),
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
      decideAdminHostRouting('ADMIN.THELIGHTBULB.COMPANY', '/admin/users', ADMIN_HOSTS),
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

  it('passes through the choose-organization session task on admin hosts', () => {
    expect(
      decideAdminHostRouting(
        'admin.thelightbulb.company',
        '/session-tasks/choose-organization',
        ADMIN_HOSTS,
      ),
    ).toEqual({ kind: 'passthrough' });
  });

  it('still redirects truly off-admin paths (e.g. /chat) on admin hosts', () => {
    // Regression guard — the original gate behavior for non-auth, non-admin
    // paths is preserved.
    expect(
      decideAdminHostRouting('admin.thelightbulb.company', '/chat', ADMIN_HOSTS),
    ).toEqual({ kind: 'redirect', to: '/admin' });
  });
});

// The bundled paperclip skill writes agent links as native Paperclip paths
// (e.g. `/ISOA/issues/ISOA-317`); a direct hit on one 404'd because the
// retired /teams/* tree never redirected them.
describe('decidePaperclipLinkRedirect (agent Paperclip-link redirect)', () => {
  it('redirects a direct hit on an agent-written issue path to the Lightbulb place', () => {
    expect(decidePaperclipLinkRedirect('/ISOA/issues/ISOA-317')).toEqual({
      kind: 'redirect',
      to: '/voice/lightbulb',
    });
  });

  it('leaves ordinary app paths alone', () => {
    expect(decidePaperclipLinkRedirect('/chat')).toBeNull();
    expect(decidePaperclipLinkRedirect('/settings')).toBeNull();
    expect(decidePaperclipLinkRedirect('/admin')).toBeNull();
  });

  it('redirects a dropped-prefix issue/approval link too', () => {
    // Agents sometimes hand-write an issue/approval link without the
    // required company prefix; those two sections tolerate it as a
    // defensive fallback (comment-style.md still requires the prefix).
    expect(decidePaperclipLinkRedirect('/issues/ISOA-317')).toEqual({
      kind: 'redirect',
      to: '/voice/lightbulb',
    });
    expect(decidePaperclipLinkRedirect('/approvals/ca6ba09d-b558')).toEqual({
      kind: 'redirect',
      to: '/voice/lightbulb',
    });
  });

  it('maps the retired /teams/tasks links and prefixed agent, run and goal links', () => {
    for (const path of ['/teams/tasks/ISOA-12', '/PAP/agents/cto', '/PAP/agents/cto/runs/run-9', '/PAP/goals/g-142']) {
      expect(decidePaperclipLinkRedirect(path)).toEqual({ kind: 'redirect', to: '/voice/lightbulb' });
    }
    expect(decidePaperclipLinkRedirect('/teams/inbox')).toBeNull();
    expect(decidePaperclipLinkRedirect('/PAP/sprints/1')).toBeNull();
  });

  it('still requires the prefix for agents/projects/goals', () => {
    expect(decidePaperclipLinkRedirect('/agents/cto')).toBeNull();
    expect(decidePaperclipLinkRedirect('/projects/growth')).toBeNull();
    expect(decidePaperclipLinkRedirect('/goals/g-142')).toBeNull();
  });
});

describe('protected routes', () => {
  it('protects /voice pages but not the public stock voice samples', async () => {
    const { createRouteMatcher } = await import('@clerk/nextjs/server');
    const { NextRequest } = await import('next/server');
    const { PROTECTED_ROUTES } = await import('@/middleware');
    const isProtected = createRouteMatcher(PROTECTED_ROUTES);
    const at = (path: string) => isProtected(new NextRequest(`https://thelightbulb.company${path}`));
    expect(at('/voice')).toBe(true);
    expect(at('/voice/agent')).toBe(true);
    expect(at('/voice/settings')).toBe(true);
    expect(at('/voice-samples/gpt-live-1-marin-en.wav')).toBe(false);
  });
});
