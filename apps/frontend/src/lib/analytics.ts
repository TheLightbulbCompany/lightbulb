/**
 * The single PostHog touchpoint for the entire frontend.
 *
 * Every call into the PostHog SDK (init, identify, reset, capture,
 * pageview, captureException) lives behind a function in this file, and
 * the React tree's ``<Provider>`` is re-exported from here too. The rule
 * is: ``import "posthog-js"`` and ``import "posthog-js/react"`` only ever
 * appear in this module — every other file in ``apps/frontend/src/``
 * imports from ``@/lib/analytics``.
 *
 * Why: the previous shape had ``usePostHog()`` direct in 14 components,
 * which meant "switch analytics provider" or "add a global property to
 * every event" or "stop sending events from a specific page" all
 * required touching every component. The wrapper existed but was
 * bypassed — the seam was hypothetical, not real (per Matt Pocock's
 * ``improve-codebase-architecture`` skill: one adapter is a hypothetical
 * seam, two is a real one). Routing every caller through this module
 * makes the seam load-bearing and gives PostHog one mockable surface
 * for tests.
 *
 * What belongs here:
 *   - User intents (chat_message_sent, subscription_checkout_started)
 *   - User-visible outcomes (chat_completed, channel_link_submitted)
 *   - The React Provider wrapper (``Provider``) for the app's root layout
 *   - identify / reset / pageview / init bookkeeping for PostHogProvider
 *
 * What does NOT belong here:
 *   - RPC-call-level events (backend HTTP activity goes to CloudWatch —
 *     don't shadow it in PostHog)
 *   - PII beyond identify-level fields (specifically: no message content,
 *     no bot tokens, no API key values — capture lengths / types only)
 */

import posthog from "posthog-js";
import { PostHogProvider as PHProvider } from "posthog-js/react";

/**
 * The single PostHog client instance. Re-exported so
 * ``components/PostHogProvider.tsx`` can hand it to ``<Provider>``
 * without importing ``posthog-js`` itself.
 */
export const client = posthog;

/**
 * The PostHog React provider, re-exported so the app's root layout
 * doesn't need to ``import "posthog-js/react"`` directly.
 */
export const Provider = PHProvider;

/**
 * The registry of every product event we emit. Adding a new event?
 * Add it here first — TypeScript will flag every call site that needs
 * to be updated.
 *
 * Strict union (rather than ``string``) is the load-bearing piece: it
 * catches typos at compile time and makes "what events do we emit?" a
 * grep-able question with one answer.
 *
 * Grouped roughly by surface for readability; keep alphabetical within
 * each group when adding new entries so diffs stay reviewable.
 */
export type AnalyticsEvent =
  // Agent lifecycle
  | "agent_created"
  | "agent_deleted"
  | "agent_model_changed"
  | "agent_renamed"
  | "agent_selected"
  // Billing
  | "billing_portal_opened"
  | "subscription_checkout_started"
  // Catalog
  | "catalog_agent_deployed"
  | "catalog_agent_published"
  // Channels
  | "channel_connected"
  | "channel_link_submitted"
  // Chat surface
  | "chat_aborted"
  | "chat_completed"
  | "chat_file_uploaded"
  | "chat_message_sent"
  | "chat_stopped"
  // Container / control panel
  | "control_panel_opened"
  | "file_browser_opened"
  | "update_applied"
  | "update_scheduled"
  // Desktop app
  | "desktop_update_check_clicked"
  | "desktop_update_install_clicked"
  | "desktop_file_access_added"
  | "desktop_file_access_removed"
  // Cron
  | "cron_job_created"
  | "cron_job_deleted"
  | "cron_job_toggled"
  | "cron_job_triggered"
  // Landing
  | "landing_cta_clicked"
  | "landing_download_clicked"
  // MCP
  | "mcp_server_added"
  | "mcp_server_removed"
  // Onboarding
  | "onboarding_completed"
  | "onboarding_provider_completed"
  | "onboarding_step_completed"
  | "org_invitation_accepted"
  | "workspace_type_selected"
  // Skills
  | "skill_api_key_saved"
  | "skill_installed"
  | "skill_toggled";

/**
 * Type exposing the ``__loaded`` flag PostHog sets after ``init()``
 * resolves. Declared locally so we don't have to import a private type
 * — the field is part of the public contract (documented at
 * https://posthog.com/docs/libraries/js).
 */
interface PostHogLoadState {
  __loaded?: boolean;
}

function isLoaded(): boolean {
  return (posthog as unknown as PostHogLoadState).__loaded === true;
}

/**
 * Bootstrap PostHog. Idempotent at the SDK level (PostHog's own
 * ``init`` is a no-op on second call), but we also short-circuit on
 * SSR so a server render doesn't try to touch ``window``.
 *
 * Called from ``components/PostHogProvider.tsx`` exactly once at module
 * eval. Every other code path calls ``capture`` / ``identify`` /
 * ``reset`` / ``pageview`` / ``captureException`` and lets those guards
 * handle the "PostHog never initialised" case (e.g. when
 * ``NEXT_PUBLIC_POSTHOG_KEY`` is unset locally).
 */
export function init(
  apiKey: string,
  options: Parameters<typeof posthog.init>[1],
): void {
  if (typeof window === "undefined") return;
  posthog.init(apiKey, options);
}

/**
 * Capture a product event. Safe to call from hooks/components that may
 * render during SSR (returns early without touching PostHog) and from
 * environments where ``NEXT_PUBLIC_POSTHOG_KEY`` is unset (returns
 * early because ``init()`` was never called and ``__loaded`` is false).
 */
export function capture(
  event: AnalyticsEvent,
  properties?: Record<string, unknown>,
): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  posthog.capture(event, properties);
}

/**
 * Forward an unhandled browser error to PostHog's error tracking. Used
 * by the global error/unhandledrejection forwarders installed in
 * ``components/PostHogProvider.tsx``.
 */
export function captureException(error: unknown): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  // posthog-js >= 1.190 exposes captureException directly.
  posthog.captureException(error);
}

/**
 * Bind a stable distinctId (currently the Clerk user id) plus an
 * identify-payload of profile fields. Called from
 * ``components/PostHogProvider.tsx`` on the rising edge of
 * ``isSignedIn`` so events captured before identify are merged onto
 * the right person.
 */
export function identify(
  distinctId: string,
  properties?: Record<string, unknown>,
): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  posthog.identify(distinctId, properties);
}

/**
 * Clear the bound distinctId and reset the local PostHog state. Called
 * from ``components/PostHogProvider.tsx`` on sign-out.
 */
export function reset(): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  posthog.reset();
}

/**
 * Capture a manual pageview event. ``capture_pageview`` is disabled in
 * the ``init()`` options so the App-Router page tracking goes through
 * here instead — Next.js ``usePathname()``/``useSearchParams()`` give
 * us the URL without relying on PostHog's broken default
 * pushState-detection in App Router.
 */
export function pageview(url: string): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  posthog.capture("$pageview", { $current_url: url });
}
