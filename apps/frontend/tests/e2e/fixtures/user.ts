/**
 * E2E user lifecycle helper.
 *
 * Tests call `createE2EUser(page)` in beforeAll, drive the UI flow, then
 * call `cleanupUser(user)` in afterAll. For a workspace user, cleanup
 * orchestrates Stripe -> backend -> Clerk in that order, then re-queries
 * Stripe + Clerk to hard-fail if anything was left behind. Fresh no-workspace
 * users need only Clerk cleanup. This keeps the dev environment clean across
 * runs.
 *
 * NOTE: the bare `e2eUser` Playwright fixture below is intentionally a trap
 * (it always throws). The pattern is for specs to call `createE2EUser`
 * directly inside `beforeAll`, not via a fixture — orgId needs to be
 * decided per-spec, and Playwright's worker fixtures don't compose cleanly
 * with that. Tasks 11/12 (the spec files) will use the explicit helpers.
 */

import { test as base, type Page } from '@playwright/test';
import crypto from 'crypto';
import {
  createUser,
  deleteUser,
  deleteOrg,
  findUserByEmail,
  listUserOrganizationIds,
} from './clerk-admin';
import {
  cancelSubsAndDeleteCustomer,
  findCustomerByEmail,
  findCustomersByOwnerId,
} from './stripe-admin';
import { AuthedFetch, AuthedFetchError } from './api';
import { DDBReader } from './ddb-reader';

export type E2EUser = {
  runId: string;
  email: string;
  password: string;
  clerkUserId: string;
  orgId?: string;
  page: Page;
  api: AuthedFetch;
  ddb: DDBReader;
};

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000/api/v1';

function makeRunId(): string {
  return `${Date.now()}-${crypto.randomBytes(3).toString('hex')}`;
}

export async function createE2EUser(
  page: Page,
): Promise<E2EUser> {
  const secretKey = process.env.CLERK_SECRET_KEY;
  if (!secretKey) {
    throw new Error('CLERK_SECRET_KEY required for createE2EUser');
  }

  const runId = makeRunId();
  const rand = crypto.randomBytes(6).toString('hex');
  const email = `isol8-e2e-${rand}@mailsac.com`;
  const password = crypto.randomBytes(24).toString('base64url');

  const clerkUserId = await createUser({ secretKey, email, password, runId });
  const api = new AuthedFetch(page, API_URL, runId);
  const ddb = new DDBReader(api, clerkUserId);

  return { runId, email, password, clerkUserId, page, api, ddb };
}

export async function cleanupUser(
  user: E2EUser,
  options: { cleanupWorkspace?: boolean } = {},
): Promise<void> {
  const cleanupWorkspace = options.cleanupWorkspace ?? true;
  const stripeKey = process.env.STRIPE_SECRET_KEY;
  const clerkKey = process.env.CLERK_SECRET_KEY;
  if (!clerkKey) throw new Error('CLERK_SECRET_KEY required for cleanupUser');

  // Each step is run independently and its failure is collected; we only
  // throw at the end. Without this, a backend 5xx (or a 401 because the tab
  // was on Stripe Checkout — see api.ts token() guard) used to abort
  // cleanupUser before the Clerk delete ran, leaking the Clerk user
  // (Codex P1 on PR #309). External-system leak verification still happens
  // last and contributes to the failure list.
  const failures: string[] = [];

  // An onboarding failure can happen after Clerk creates the workspace but
  // before onboardWorkspace records its ID. Discover memberships before
  // treating a missing in-memory ID as a no-workspace user. More than one is
  // unsafe to guess: report it while still deleting the isolated test user.
  let workspaceId = user.orgId;
  if (cleanupWorkspace && !workspaceId) {
    try {
      const organizationIds = await listUserOrganizationIds({
        secretKey: clerkKey,
        userId: user.clerkUserId,
      });
      if (organizationIds.length === 1) {
        [workspaceId] = organizationIds;
        user.orgId = workspaceId;
      } else if (organizationIds.length > 1) {
        failures.push(
          `workspace discovery: multiple memberships (${organizationIds.join(', ')})`,
        );
      }
    } catch (err) {
      failures.push(`workspace discovery: ${err}`);
    }
  }
  const shouldCleanupWorkspace = cleanupWorkspace && Boolean(workspaceId);
  if (shouldCleanupWorkspace && !stripeKey) {
    throw new Error('STRIPE_SECRET_KEY required for cleanupUser');
  }

  // 1. Stripe — cancels active subs then deletes the customer. Idempotent.
  // Pass ownerId so we catch customers whose email field is null (the
  // Clerk JWT template doesn't always include the email claim, so
  // backend's create_customer_for_owner can produce email=null records
  // that the email-based search would miss). The backend always tags the
  // customer with metadata.owner_id, so that lookup is the canonical one.
  // Only read under shouldCleanupWorkspace, which requires workspaceId.
  const stripeOwnerId = workspaceId!;
  if (shouldCleanupWorkspace) {
    try {
      await cancelSubsAndDeleteCustomer(stripeKey!, user.email, stripeOwnerId);
    } catch (err) {
      failures.push(`stripe: ${err}`);
    }
  }

  // 2. Backend — DELETE /debug/user-data wipes DDB rows + EFS workspace +
  //    ECS service for this active workspace. Only 404 is treated as success
  //    (idempotent). Every other error is recorded but does NOT abort
  //    Clerk cleanup below.
  //
  //    Navigate the page back to the app first so Clerk is loaded and
  //    AuthedFetch.token() can mint a JWT — without this, a spec that
  //    failed on Stripe Checkout would 401 here.
  if (shouldCleanupWorkspace) {
    try {
      const baseUrl = process.env.BASE_URL ?? user.page.url();
      await user.page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
      // domcontentloaded fires before Clerk's JS bootstraps the session, so
      // AuthedFetch.token() can throw if we issue the delete immediately.
      // Wait for window.Clerk.session to be live (Codex P2 on PR #309).
      await user.page.waitForFunction(
        () => {
          const w = window as Window & {
            Clerk?: { loaded?: boolean; session?: unknown };
          };
          return w.Clerk?.loaded === true && w.Clerk?.session != null;
        },
        { timeout: 30_000 },
      );
      await user.page.evaluate(async (organizationId) => {
        const w = window as Window & {
          Clerk?: {
            setActive?: (options: { organization: string }) => Promise<void>;
          };
        };
        if (!w.Clerk?.setActive) {
          throw new Error('Clerk.setActive unavailable during workspace cleanup');
        }
        await w.Clerk.setActive({ organization: organizationId });
      }, stripeOwnerId);
      await user.page.waitForFunction(
        (organizationId) => {
          const w = window as Window & {
            Clerk?: { organization?: { id?: string } };
          };
          return w.Clerk?.organization?.id === organizationId;
        },
        stripeOwnerId,
        { timeout: 30_000 },
      );
      await user.api.delete('/debug/user-data');
    } catch (err) {
      if (err instanceof AuthedFetchError && err.status === 404) {
        console.warn('[e2e] /debug/user-data 404 — treating as idempotent success');
      } else {
        failures.push(`backend: ${err}`);
      }
    }
  }

  // 3. Clerk — runs even if backend cleanup failed. Org first (if any),
  //    then the user. Both helpers treat 404 as success.
  try {
    if (shouldCleanupWorkspace) {
      await deleteOrg({ secretKey: clerkKey, orgId: stripeOwnerId });
    }
    await deleteUser({ secretKey: clerkKey, userId: user.clerkUserId });
  } catch (err) {
    failures.push(`clerk: ${err}`);
  }

  // 4. Settle window — Stripe + Clerk are eventually consistent on the read
  //    side. 5s is enough in practice; a tighter poll would just add flakes.
  await new Promise((r) => setTimeout(r, 5000));

  // 5. Hard-fail verification on external systems. An E2E email identifies a
  //    leaked customer even when Clerk confirms no workspace; owner_id is only
  //    available after resolving a workspace.
  if (stripeKey) {
    try {
      const stripeRemaining = await findCustomerByEmail(stripeKey!, user.email);
      if (stripeRemaining) {
        failures.push(`stripe leak: customer ${stripeRemaining.id} (by-email) not deleted`);
      }
      if (shouldCleanupWorkspace) {
        const ownerRemaining = await findCustomersByOwnerId(stripeKey, stripeOwnerId);
        for (const c of ownerRemaining) {
          failures.push(`stripe leak: customer ${c.id} (by-owner_id) not deleted`);
        }
      }
    } catch (err) {
      failures.push(`stripe verify: ${err}`);
    }
  }
  try {
    const clerkRemaining = await findUserByEmail({
      secretKey: clerkKey,
      email: user.email,
    });
    if (clerkRemaining) {
      failures.push(`clerk leak: user ${clerkRemaining.id} not deleted`);
    }
  } catch (err) {
    failures.push(`clerk verify: ${err}`);
  }

  if (failures.length > 0) {
    throw new Error(`cleanupUser: ${failures.length} step(s) failed:\n  - ${failures.join('\n  - ')}`);
  }
}

/**
 * Bare Playwright fixture export — intentionally throws. Specs should call
 * `createE2EUser` / `cleanupUser` directly in `beforeAll` / `afterAll` so the
 * orgId can be recorded as soon as the workspace is created. Kept as load-bearing
 * documentation for future contributors who go looking for `test.e2eUser`.
 */
// `object`, not `Record<string, never>`: the latter's string index signature
// makes Playwright resolve `e2eUser` against the *test*-scoped branch (value
// type `never`) instead of the worker-scoped one declared here.
export const test = base.extend<object, { e2eUser: E2EUser }>({
  e2eUser: [
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    async ({ browser: _browser }, use) => {
      void use;
      throw new Error(
        'Use createE2EUser(page) in beforeAll, not the bare e2eUser fixture',
      );
    },
    { scope: 'worker' },
  ],
});
